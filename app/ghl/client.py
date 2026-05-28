"""
GoHighLevel API v2 client.

Handles OAuth2 token management, request retry/backoff, and all GHL REST calls
needed by the foundation layer (contact upsert, custom fields, tags, pipelines).

Auth model:
  - App-level credentials:   ``GHL_OAUTH_CLIENT_ID`` + ``GHL_OAUTH_CLIENT_SECRET``
  - Per-location tokens:     ``locations.ghl_oauth_token_cache`` JSONB
      {
          "access_token":  str,
          "refresh_token": str,
          "token_type":    "Bearer",
          "expires_at":    "2026-05-29T12:00:00+00:00"  // ISO-8601 UTC
      }
  - Tokens are refreshed automatically when ``expires_at`` is within 5 minutes.

Rate limiting:
  GHL API v2 allows ~100 requests per 10 seconds per location.
  A simple async semaphore (10 concurrent requests) is used per client instance.
  On HTTP 429, the client waits ``Retry-After`` seconds and retries once.

Base URL: https://services.leadconnectorhq.com
Required headers: Authorization: Bearer {token}, Version: 2021-07-28

References:
  - https://highlevel.stoplight.io/docs/integrations
  - requirements_v2/00_master_overview.md §GHL Data Model
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

import httpx

from app.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.db.models.location import Location

logger = logging.getLogger(__name__)

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"
GHL_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"

# Refresh token when fewer than this many seconds remain
_TOKEN_REFRESH_HEADROOM_SECONDS = 300  # 5 minutes

# Concurrency guard — max simultaneous requests per client instance
_MAX_CONCURRENT_REQUESTS = 10

# Retry on 429
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


class GhlAuthError(Exception):
    """Raised when GHL OAuth tokens are missing or cannot be refreshed."""


class GhlApiError(Exception):
    """Raised when the GHL API returns an unexpected error response."""

    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"GHL API {status} @ {url}: {body[:200]}")


class GhlClient:
    """
    Async GHL API v2 client for one location.

    Usage::

        async with GhlClient(location, db) as client:
            contact_id = await client.upsert_contact(email="x@y.com", ...)

    The client reads tokens from ``location.ghl_oauth_token_cache`` and refreshes
    them as needed, writing updated tokens back to the DB.
    """

    def __init__(self, location: "Location", db: "AsyncSession") -> None:
        self._location = location
        self._db = db
        self._http: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._field_key_cache: dict[str, str] | None = None  # key → field_id

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "GhlClient":
        self._http = httpx.AsyncClient(
            base_url=GHL_BASE_URL,
            timeout=httpx.Timeout(30.0),
            headers={"Version": GHL_API_VERSION},
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── Token management ───────────────────────────────────────────────────────

    async def _get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        cache: dict | None = self._location.ghl_oauth_token_cache
        if not cache or not cache.get("access_token"):
            raise GhlAuthError(
                f"GHL tokens not configured for location {self._location.id}. "
                "Complete the OAuth flow via /api/v1/oauth/ghl/callback."
            )

        expires_at_str: str | None = cache.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(tz=timezone.utc)
            if expires_at - now > timedelta(seconds=_TOKEN_REFRESH_HEADROOM_SECONDS):
                return cache["access_token"]  # Still valid

        # Need to refresh
        logger.info("ghl_client: refreshing OAuth token for location %s", self._location.id)
        new_cache = await self._refresh_token(cache["refresh_token"])
        await self._persist_token_cache(new_cache)
        return new_cache["access_token"]

    async def _refresh_token(self, refresh_token: str) -> dict:
        """Call the GHL token endpoint to obtain a new access token."""
        settings = get_settings()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                GHL_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.ghl_oauth_client_id,
                    "client_secret": settings.ghl_oauth_client_secret,
                    "refresh_token": refresh_token,
                },
            )
        if resp.status_code != 200:
            raise GhlAuthError(
                f"GHL token refresh failed: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=data.get("expires_in", 86400))
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": expires_at.isoformat(),
        }

    async def _persist_token_cache(self, cache: dict) -> None:
        """Write updated tokens to the DB (does not commit — caller must)."""
        from sqlalchemy import update  # noqa: PLC0415
        from app.db.models.location import Location  # noqa: PLC0415

        await self._db.execute(
            update(Location)
            .where(Location.id == self._location.id)
            .values(ghl_oauth_token_cache=cache)
        )
        # Update in-memory too
        self._location.ghl_oauth_token_cache = cache
        logger.debug("ghl_client: token cache updated for location %s", self._location.id)

    # ── HTTP request helper ────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict | list:
        """
        Authenticated GHL API request with retry on 429.

        Returns parsed JSON body.
        Raises GhlApiError on non-2xx responses (after retries).
        """
        if self._http is None:
            raise RuntimeError("GhlClient must be used as a context manager")

        token = await self._get_access_token()

        for attempt in range(_MAX_RETRIES):
            headers = {"Authorization": f"Bearer {token}"}

            # Acquire semaphore only for the actual HTTP call — not during sleep.
            # Holding the semaphore across asyncio.sleep() would starve other
            # concurrent requests for the full back-off duration.
            async with self._semaphore:
                resp = await self._http.request(
                    method,
                    path,
                    headers=headers,
                    json=json,
                    params=params,
                )

            if resp.status_code == 429:
                retry_after = float(
                    resp.headers.get("Retry-After", _BASE_BACKOFF_SECONDS * (2 ** attempt))
                )
                logger.warning(
                    "ghl_client: 429 rate limit on %s %s — waiting %.1fs (attempt %d/%d)",
                    method, path, retry_after, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(retry_after)
                # Refresh token in case it expired during the wait
                token = await self._get_access_token()
                continue

            if resp.status_code == 401:
                # Token may have just expired — try one forced refresh
                if attempt == 0:
                    logger.warning("ghl_client: 401 on %s — forcing token refresh", path)
                    cache = self._location.ghl_oauth_token_cache or {}
                    new_cache = await self._refresh_token(cache.get("refresh_token", ""))
                    await self._persist_token_cache(new_cache)
                    token = new_cache["access_token"]
                    continue
                raise GhlAuthError(f"GHL returned 401 after token refresh for {path}")

            if not (200 <= resp.status_code < 300):
                raise GhlApiError(resp.status_code, resp.text, path)

            # Empty body (e.g. 204 No Content)
            if not resp.content:
                return {}

            return resp.json()

        raise GhlApiError(429, "Max retries exceeded", path)

    # ── Custom field ID resolution ─────────────────────────────────────────────

    async def get_custom_field_map(self) -> dict[str, str]:
        """
        Fetch all custom fields for this sub-account and return
        a dict of ``{fieldKey: fieldId}``.

        Result is cached for the lifetime of this client instance.
        """
        if self._field_key_cache is not None:
            return self._field_key_cache

        location_id = self._location.ghl_subaccount_id
        data = await self._request("GET", f"/locations/{location_id}/customFields")
        fields: list[dict] = data.get("customFields", []) if isinstance(data, dict) else []
        self._field_key_cache = {f["fieldKey"]: f["id"] for f in fields if "fieldKey" in f and "id" in f}
        logger.debug(
            "ghl_client: loaded %d custom field mappings for location %s",
            len(self._field_key_cache),
            location_id,
        )
        return self._field_key_cache

    async def _build_custom_fields_payload(
        self, fields: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Convert a ``{fieldKey: value}`` dict to the GHL customFields array format.

        Skips fields whose key is not registered in the sub-account (logs a warning).
        """
        field_map = await self.get_custom_field_map()
        payload: list[dict[str, Any]] = []
        for key, value in fields.items():
            field_id = field_map.get(key)
            if field_id is None:
                logger.warning(
                    "ghl_client: custom field key %r not found in location %s — skipping",
                    key,
                    self._location.ghl_subaccount_id,
                )
                continue
            payload.append({"id": field_id, "value": value})
        return payload

    # ── Contact endpoints ──────────────────────────────────────────────────────

    async def search_contact_by_email(self, email: str) -> dict | None:
        """
        Search for a GHL contact by email in this sub-account.

        Returns the first matching contact dict, or None if not found.
        """
        location_id = self._location.ghl_subaccount_id
        data = await self._request(
            "GET",
            "/contacts/search",
            params={"locationId": location_id, "email": email},
        )
        contacts: list[dict] = (
            data.get("contacts", []) if isinstance(data, dict) else []
        )
        return contacts[0] if contacts else None

    async def create_contact(
        self,
        *,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> str:
        """
        Create a new GHL contact in this sub-account.

        Returns the new GHL contact ID.
        """
        location_id = self._location.ghl_subaccount_id
        body: dict[str, Any] = {"locationId": location_id}
        if email:
            body["email"] = email
        if first_name:
            body["firstName"] = first_name
        if last_name:
            body["lastName"] = last_name
        if phone:
            body["phone"] = phone
        if custom_fields:
            body["customFields"] = await self._build_custom_fields_payload(custom_fields)

        data = await self._request("POST", "/contacts/", json=body)
        contact_id: str = (
            data.get("contact", {}).get("id")  # type: ignore[union-attr]
            if isinstance(data, dict)
            else None
        )
        if not contact_id:
            raise GhlApiError(200, f"No contact ID in response: {str(data)[:200]}", "/contacts/")

        logger.info(
            "ghl_client: created contact %s for %s in location %s",
            contact_id, email, location_id,
        )
        return contact_id

    async def update_contact(
        self,
        contact_id: str,
        *,
        custom_fields: dict[str, Any] | None = None,
    ) -> None:
        """
        Update an existing GHL contact's custom fields (delta only).
        """
        if not custom_fields:
            return

        body: dict[str, Any] = {
            "customFields": await self._build_custom_fields_payload(custom_fields)
        }
        if not body["customFields"]:
            return  # All fields were skipped (not registered in sub-account)

        await self._request("PUT", f"/contacts/{contact_id}", json=body)
        logger.debug(
            "ghl_client: updated %d custom fields on contact %s",
            len(body["customFields"]),
            contact_id,
        )

    async def upsert_contact(
        self,
        *,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """
        Find or create a GHL contact.

        Returns ``(contact_id, created)`` where ``created`` is True if a new
        contact was created.
        """
        # Contacts without email cannot be de-duplicated — create directly.
        # Passing an empty string to search_contact_by_email is unpredictable
        # (GHL may return all contacts or a server error).
        if not email:
            contact_id = await self.create_contact(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                custom_fields=custom_fields,
            )
            return contact_id, True

        existing = await self.search_contact_by_email(email)
        if existing:
            contact_id = existing["id"]
            if custom_fields:
                await self.update_contact(contact_id, custom_fields=custom_fields)
            return contact_id, False

        contact_id = await self.create_contact(
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            custom_fields=custom_fields,
        )
        return contact_id, True

    # ── Tag endpoints ──────────────────────────────────────────────────────────

    async def add_tags(self, contact_id: str, tags: list[str]) -> None:
        """Apply one or more tags to a GHL contact."""
        if not tags:
            return
        await self._request(
            "POST",
            f"/contacts/{contact_id}/tags",
            json={"tags": tags},
        )
        logger.debug("ghl_client: added tags %s to contact %s", tags, contact_id)

    async def remove_tags(self, contact_id: str, tags: list[str]) -> None:
        """Remove one or more tags from a GHL contact."""
        if not tags:
            return
        await self._request(
            "DELETE",
            f"/contacts/{contact_id}/tags",
            json={"tags": tags},
        )
        logger.debug("ghl_client: removed tags %s from contact %s", tags, contact_id)

    # ── Pipeline / opportunity endpoints ───────────────────────────────────────

    async def get_pipelines(self) -> list[dict]:
        """Fetch all pipelines for this sub-account."""
        location_id = self._location.ghl_subaccount_id
        data = await self._request(
            "GET", "/opportunities/pipelines", params={"locationId": location_id}
        )
        return data.get("pipelines", []) if isinstance(data, dict) else []  # type: ignore[return-value]

    async def search_opportunity(
        self,
        contact_id: str,
        pipeline_id: str,
    ) -> dict | None:
        """Find an existing opportunity for a contact in a given pipeline."""
        location_id = self._location.ghl_subaccount_id
        data = await self._request(
            "GET",
            "/opportunities/search",
            params={
                "location_id": location_id,
                "contact_id": contact_id,
                "pipeline_id": pipeline_id,
            },
        )
        opps: list[dict] = data.get("opportunities", []) if isinstance(data, dict) else []
        return opps[0] if opps else None

    async def create_opportunity(
        self,
        *,
        contact_id: str,
        pipeline_id: str,
        stage_id: str,
        name: str,
    ) -> str:
        """Create a new pipeline opportunity. Returns the opportunity ID."""
        location_id = self._location.ghl_subaccount_id
        data = await self._request(
            "POST",
            "/opportunities/",
            json={
                "locationId": location_id,
                "contactId": contact_id,
                "pipelineId": pipeline_id,
                "pipelineStageId": stage_id,
                "name": name,
            },
        )
        opp_id: str = (
            data.get("opportunity", {}).get("id")  # type: ignore[union-attr]
            if isinstance(data, dict)
            else None
        )
        if not opp_id:
            raise GhlApiError(200, f"No opportunity ID in response: {str(data)[:200]}", "/opportunities/")
        return opp_id

    async def move_opportunity_stage(
        self,
        opportunity_id: str,
        *,
        pipeline_id: str,
        stage_id: str,
    ) -> None:
        """Move a pipeline opportunity to a new stage."""
        await self._request(
            "PUT",
            f"/opportunities/{opportunity_id}",
            json={"pipelineId": pipeline_id, "pipelineStageId": stage_id},
        )
        logger.debug(
            "ghl_client: moved opportunity %s to stage %s",
            opportunity_id, stage_id,
        )
