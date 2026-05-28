"""
Tests for the M6 consent layer.

Covers:
  - app/consent/stop_detector.py  — STOP keyword detection (multilingual, ASCII-fold, custom)
  - app/consent/gate.py           — ConsentGate: ALLOW/DENY paths + transactional bypass
  - app/consent/record.py         — append-only audit record helpers
  - app/consent/tokens.py         — signed preference-centre tokens
  - app/api/v1/admin/consent.py   — REST endpoints (gate, grant, revoke, preference-centre, sweep)
  - app/api/v1/webhooks/ghl_inbound.py — inbound STOP handler

All DB tests use SQLite in-memory (aiosqlite) — no Postgres required.
All API tests use FastAPI TestClient with the real app (lifespan skipped via
dependency-override on get_db).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.consent.gate import ConsentDecision, ConsentResult, consent_gate
from app.consent.record import (
    VALID_ACTORS,
    VALID_CHANNELS,
    VALID_EVENTS,
    record_blocked_send,
    record_grant,
    record_preference_centre_update,
    record_revocation,
)
from app.consent.stop_detector import (
    DEFAULT_STOP_REGEX,
    get_opt_out_confirmation,
    is_stop_keyword,
)
from app.consent.tokens import (
    TOKEN_TTL_SECONDS,
    TokenError,
    TokenPayload,
    generate_token,
    verify_token,
)
from app.db.models.base import Base
from app.db.models.consent_audit import ConsentAudit
from app.db.models.location import Location


# ═══════════════════════════════════════════════════════════════════════════════
# Shared DB fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def location(db: AsyncSession) -> Location:
    loc = Location(
        id=uuid.uuid4(),
        eversports_studio_id="test-studio-consent",
        ghl_subaccount_id=f"ghl-{uuid.uuid4().hex[:8]}",
        ghl_oauth_token_ref="secret://test/ghl",
        eversports_credentials_ref="secret://test/eversports",
        timezone="Europe/Vienna",
        studio_owner_email="owner@test.com",
        studio_name="Consent Test Studio",
        location_name="Consent Test Studio — Main",
        eversports_cookie_state="ok",
        historical_sync_flag="pending",
    )
    db.add(loc)
    await db.flush()
    return loc


# ═══════════════════════════════════════════════════════════════════════════════
# 1. STOP keyword detector
# ═══════════════════════════════════════════════════════════════════════════════


class TestStopDetector:
    """Tests for is_stop_keyword() in app/consent/stop_detector.py."""

    # ── Positive matches (should return True) ─────────────────────────────────

    def test_stop_uppercase(self):
        assert is_stop_keyword("STOP") is True

    def test_stop_lowercase(self):
        assert is_stop_keyword("stop") is True

    def test_stop_mixed_case(self):
        assert is_stop_keyword("Stop") is True

    def test_stopp_german(self):
        assert is_stop_keyword("STOPP") is True

    def test_aufhoeren_umlaut(self):
        assert is_stop_keyword("aufhören") is True

    def test_aufhoeren_ascii_fold(self):
        """'aufhoeren' must match via ASCII folding of 'aufhören'."""
        assert is_stop_keyword("aufhoeren") is True

    def test_aufhoeren_uppercase_fold(self):
        assert is_stop_keyword("AUFHOEREN") is True

    def test_abmelden(self):
        assert is_stop_keyword("Abmelden") is True

    def test_keine_werbung(self):
        assert is_stop_keyword("keine werbung") is True

    def test_keine_werbung_uppercase(self):
        assert is_stop_keyword("KEINE WERBUNG") is True

    def test_unsubscribe(self):
        assert is_stop_keyword("unsubscribe") is True

    def test_opt_out_hyphen(self):
        assert is_stop_keyword("opt-out") is True

    def test_opt_out_space(self):
        assert is_stop_keyword("opt out") is True

    def test_stop_with_surrounding_whitespace(self):
        """Leading/trailing whitespace must be stripped before matching."""
        assert is_stop_keyword("  STOP  ") is True

    def test_stop_with_leading_newline(self):
        assert is_stop_keyword("\nstop\n") is True

    # ── Negative matches (should return False) ────────────────────────────────

    def test_empty_string(self):
        assert is_stop_keyword("") is False

    def test_whitespace_only(self):
        assert is_stop_keyword("   ") is False

    def test_stop_in_sentence(self):
        """Anchored match — must not fire on STOP in the middle of a sentence."""
        assert is_stop_keyword("I want to stop going to the gym") is False

    def test_prefix_stop(self):
        assert is_stop_keyword("stopnow") is False

    def test_normal_message(self):
        assert is_stop_keyword("Danke für den Kurs!") is False

    def test_cancel_word(self):
        """'cancel' is not in our keyword list."""
        assert is_stop_keyword("cancel") is False

    def test_partial_match_abmelden(self):
        assert is_stop_keyword("bitte abmelden mich") is False

    # ── Custom pattern ────────────────────────────────────────────────────────

    def test_custom_pattern_matches(self):
        assert is_stop_keyword("NEIN", custom_pattern=r"^nein$") is True

    def test_custom_pattern_no_match_on_custom(self):
        """'xyz' doesn't match either the custom pattern or the default."""
        assert is_stop_keyword("xyz", custom_pattern=r"^nein$") is False

    def test_custom_pattern_additive_default_still_fires(self):
        """STOP must still match even when a custom pattern is configured."""
        assert is_stop_keyword("stop", custom_pattern=r"^nein$") is True

    def test_invalid_custom_pattern_falls_back_to_default(self):
        """Invalid regex falls back to default; 'STOP' should still match."""
        assert is_stop_keyword("STOP", custom_pattern="[[[invalid") is True

    def test_custom_pattern_none_uses_default(self):
        assert is_stop_keyword("abmelden", custom_pattern=None) is True


class TestOptOutConfirmation:
    """Tests for get_opt_out_confirmation()."""

    def test_german_austria(self):
        msg = get_opt_out_confirmation("Anna", "de-AT")
        assert "Anna" in msg
        assert "abgemeldet" in msg

    def test_german_germany(self):
        msg = get_opt_out_confirmation("Max", "de-DE")
        assert "Max" in msg

    def test_german_fallback(self):
        msg = get_opt_out_confirmation("Lisa", "de")
        assert "abgemeldet" in msg

    def test_english(self):
        msg = get_opt_out_confirmation("John", "en")
        assert "unsubscribed" in msg.lower()
        assert "John" in msg

    def test_unknown_locale_falls_back_to_de_at(self):
        msg = get_opt_out_confirmation("Test", "fr")
        assert "abgemeldet" in msg

    def test_empty_first_name(self):
        msg = get_opt_out_confirmation("", "de-AT")
        # Should not crash; placeholder replaced with empty string
        assert "{first_name}" not in msg

    def test_no_first_name_passed_as_none_safe(self):
        msg = get_opt_out_confirmation("", "en")
        assert "{first_name}" not in msg


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Consent gate
# ═══════════════════════════════════════════════════════════════════════════════


class TestConsentGate:
    """Tests for consent_gate() in app/consent/gate.py."""

    @pytest.mark.asyncio
    async def test_allow_with_consent(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-001",
            location_id=location.id,
            channel="whatsapp",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_whatsapp": True},
        )
        assert result.decision == ConsentDecision.ALLOW
        assert result.allowed is True
        assert result.denied is False

    @pytest.mark.asyncio
    async def test_deny_opted_out_tag(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-002",
            location_id=location.id,
            channel="email",
            contact_tags=["opted-out", "trial-expired"],
            contact_custom_fields={"consent_marketing_email": True},
        )
        assert result.decision == ConsentDecision.DENY
        assert "opted-out" in result.reason

    @pytest.mark.asyncio
    async def test_deny_missing_consent_field(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-003",
            location_id=location.id,
            channel="email",
            contact_tags=[],
            contact_custom_fields={},   # no consent_marketing_email
        )
        assert result.decision == ConsentDecision.DENY
        assert "no consent" in result.reason

    @pytest.mark.asyncio
    async def test_deny_consent_field_false(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-004",
            location_id=location.id,
            channel="whatsapp",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_whatsapp": False},
        )
        assert result.decision == ConsentDecision.DENY

    @pytest.mark.asyncio
    async def test_deny_consent_field_string_false(self, db: AsyncSession, location: Location):
        """'false' string must be treated as no-consent."""
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-005",
            location_id=location.id,
            channel="voice",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_voice": "false"},
        )
        assert result.decision == ConsentDecision.DENY

    @pytest.mark.asyncio
    async def test_deny_consent_field_zero(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-006",
            location_id=location.id,
            channel="email",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_email": "0"},
        )
        assert result.decision == ConsentDecision.DENY

    @pytest.mark.asyncio
    async def test_transactional_bypass_no_audit(self, db: AsyncSession, location: Location):
        """Transactional=True bypasses gate and writes NO audit row."""
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-007",
            location_id=location.id,
            channel="whatsapp",
            contact_tags=["opted-out"],          # would normally deny
            contact_custom_fields={},
            transactional=True,
        )
        assert result.decision == ConsentDecision.ALLOW
        assert "transactional" in result.reason

        # Flush and verify no consent_audit row was written
        await db.flush()
        from sqlalchemy import select  # noqa: PLC0415
        rows = (await db.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_deny_unknown_channel(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-008",
            location_id=location.id,
            channel="telegram",         # unsupported
            contact_tags=[],
            contact_custom_fields={"consent_marketing_telegram": True},
        )
        assert result.decision == ConsentDecision.DENY

    @pytest.mark.asyncio
    async def test_deny_opted_out_writes_blocked_send_row(self, db: AsyncSession, location: Location):
        """DENY due to opted-out tag must write a blocked-send row to consent_audit."""
        from sqlalchemy import select  # noqa: PLC0415

        await consent_gate(
            db,
            ghl_contact_id="ghl-contact-009",
            location_id=location.id,
            channel="whatsapp",
            contact_tags=["opted-out"],
            contact_custom_fields={},
        )
        await db.flush()
        rows = (await db.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event == "blocked-send"
        assert row.channel == "whatsapp"
        assert row.ghl_contact_id == "ghl-contact-009"

    @pytest.mark.asyncio
    async def test_deny_no_consent_writes_blocked_send_row(self, db: AsyncSession, location: Location):
        """DENY due to missing consent also writes a blocked-send row."""
        from sqlalchemy import select  # noqa: PLC0415

        await consent_gate(
            db,
            ghl_contact_id="ghl-contact-010",
            location_id=location.id,
            channel="email",
            contact_tags=[],
            contact_custom_fields={},
        )
        await db.flush()
        rows = (await db.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 1
        assert rows[0].event == "blocked-send"

    @pytest.mark.asyncio
    async def test_allow_writes_no_audit_row(self, db: AsyncSession, location: Location):
        """ALLOW must NOT write any audit row (too noisy)."""
        from sqlalchemy import select  # noqa: PLC0415

        await consent_gate(
            db,
            ghl_contact_id="ghl-contact-011",
            location_id=location.id,
            channel="email",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_email": True},
        )
        await db.flush()
        rows = (await db.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_voice_channel_allow(self, db: AsyncSession, location: Location):
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-012",
            location_id=location.id,
            channel="voice",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_voice": True},
        )
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_deny_consent_field_no_string(self, db: AsyncSession, location: Location):
        """'no' string must be treated as no-consent (same as False)."""
        result = await consent_gate(
            db,
            ghl_contact_id="ghl-contact-013",
            location_id=location.id,
            channel="email",
            contact_tags=[],
            contact_custom_fields={"consent_marketing_email": "no"},
        )
        assert result.decision == ConsentDecision.DENY

    @pytest.mark.asyncio
    async def test_deny_with_contact_id_sets_fk_on_blocked_send_row(
        self, db: AsyncSession, location: Location
    ):
        """Blocked-send row written on DENY must carry the optional contact_id FK."""
        from sqlalchemy import select  # noqa: PLC0415

        internal_contact_id = uuid.uuid4()
        await consent_gate(
            db,
            ghl_contact_id="ghl-contact-014",
            location_id=location.id,
            channel="whatsapp",
            contact_tags=["opted-out"],
            contact_custom_fields={},
            contact_id=internal_contact_id,
        )
        await db.flush()
        rows = (await db.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 1
        assert rows[0].contact_id == internal_contact_id


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Consent audit record helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestConsentRecord:
    """Tests for record_* helpers in app/consent/record.py."""

    @pytest.mark.asyncio
    async def test_record_grant_inserts_row(self, db: AsyncSession, location: Location):
        from sqlalchemy import select  # noqa: PLC0415

        row = await record_grant(
            db,
            ghl_contact_id="ghl-g-001",
            location_id=location.id,
            channel="whatsapp",
            source="double-opt-in",
            actor="customer",
        )
        await db.flush()

        assert isinstance(row.id, uuid.UUID)
        assert row.event == "granted"
        assert row.value is True
        assert row.channel == "whatsapp"
        assert row.source == "double-opt-in"
        assert row.actor == "customer"

        db_row = (
            await db.execute(select(ConsentAudit).where(ConsentAudit.id == row.id))
        ).scalar_one()
        assert db_row.event == "granted"

    @pytest.mark.asyncio
    async def test_record_revocation_inserts_row(self, db: AsyncSession, location: Location):
        row = await record_revocation(
            db,
            ghl_contact_id="ghl-r-001",
            location_id=location.id,
            channel="email",
            source="stop-keyword",
            actor="customer",
            message_shown="STOP",
        )
        await db.flush()

        assert row.event == "revoked"
        assert row.value is False
        assert row.message_shown == "STOP"

    @pytest.mark.asyncio
    async def test_record_blocked_send_inserts_row(self, db: AsyncSession, location: Location):
        row = await record_blocked_send(
            db,
            ghl_contact_id="ghl-b-001",
            location_id=location.id,
            channel="whatsapp",
        )
        await db.flush()

        assert row.event == "blocked-send"
        assert row.value is None
        assert row.actor == "system"

    @pytest.mark.asyncio
    async def test_record_preference_centre_update_grant(self, db: AsyncSession, location: Location):
        row = await record_preference_centre_update(
            db,
            ghl_contact_id="ghl-p-001",
            location_id=location.id,
            channel="email",
            new_value=True,
            ip="192.168.1.1",
        )
        await db.flush()

        assert row.event == "preference-centre-update"
        assert row.value is True
        assert row.source == "preference-centre"
        assert row.ip == "192.168.1.1"

    @pytest.mark.asyncio
    async def test_record_preference_centre_update_revoke(self, db: AsyncSession, location: Location):
        row = await record_preference_centre_update(
            db,
            ghl_contact_id="ghl-p-002",
            location_id=location.id,
            channel="voice",
            new_value=False,
        )
        await db.flush()
        assert row.value is False

    @pytest.mark.asyncio
    async def test_invalid_channel_raises(self, db: AsyncSession, location: Location):
        with pytest.raises(ValueError, match="Invalid channel"):
            await record_grant(
                db,
                ghl_contact_id="ghl-x-001",
                location_id=location.id,
                channel="telegram",     # invalid
                source="double-opt-in",
            )

    @pytest.mark.asyncio
    async def test_invalid_source_raises(self, db: AsyncSession, location: Location):
        with pytest.raises(ValueError, match="Invalid source"):
            await record_grant(
                db,
                ghl_contact_id="ghl-x-002",
                location_id=location.id,
                channel="email",
                source="unknown-form",  # invalid
            )

    @pytest.mark.asyncio
    async def test_invalid_actor_raises(self, db: AsyncSession, location: Location):
        with pytest.raises(ValueError, match="Invalid actor"):
            await record_grant(
                db,
                ghl_contact_id="ghl-x-003",
                location_id=location.id,
                channel="email",
                source="double-opt-in",
                actor="robot",          # invalid
            )

    @pytest.mark.asyncio
    async def test_append_only_multiple_rows(self, db: AsyncSession, location: Location):
        """Multiple records can coexist — no unique constraint prevents append."""
        from sqlalchemy import select  # noqa: PLC0415

        for i in range(3):
            await record_grant(
                db,
                ghl_contact_id="ghl-multi-001",
                location_id=location.id,
                channel="email",
                source="double-opt-in",
            )
        await db.flush()
        rows = (
            await db.execute(
                select(ConsentAudit).where(ConsentAudit.ghl_contact_id == "ghl-multi-001")
            )
        ).scalars().all()
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_record_grant_with_optional_fields(self, db: AsyncSession, location: Location):
        contact_uuid = uuid.uuid4()
        row = await record_grant(
            db,
            ghl_contact_id="ghl-opt-001",
            location_id=location.id,
            channel="whatsapp",
            source="onboarding-form",
            actor="studio-staff",
            contact_id=contact_uuid,
            message_shown="You agree to receive marketing messages.",
            ip="10.0.0.1",
        )
        await db.flush()
        assert row.contact_id == contact_uuid
        assert row.message_shown == "You agree to receive marketing messages."
        assert row.ip == "10.0.0.1"
        assert row.actor == "studio-staff"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Preference-centre tokens
# ═══════════════════════════════════════════════════════════════════════════════


class TestPreferenceCentreTokens:
    """Tests for generate_token / verify_token in app/consent/tokens.py."""

    def _with_test_secret(self, func, *args, **kwargs):
        """Run func with SECRET_KEY patched to a stable test value."""
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32-bytes-long!!"):
            return func(*args, **kwargs)

    def test_generate_and_verify_roundtrip(self):
        contact_id = "ghl-contact-abc"
        location_id = str(uuid.uuid4())

        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            token = generate_token(contact_id, location_id)
            payload = verify_token(token)

        assert payload.ghl_contact_id == contact_id
        assert payload.location_id == location_id
        assert isinstance(payload.expires_at, int)
        assert payload.expires_at > int(time.time())

    def test_token_expires_90_days_from_now(self):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            before = int(time.time())
            token = generate_token("c1", "l1")
            payload = verify_token(token)
            after = int(time.time())

        min_expires = before + TOKEN_TTL_SECONDS
        max_expires = after + TOKEN_TTL_SECONDS
        assert min_expires <= payload.expires_at <= max_expires

    def test_expired_token_raises(self):
        """Verify that an already-expired token raises TokenError."""
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            with patch("app.consent.tokens.time") as mock_time:
                # Generate token at time T
                mock_time.time.return_value = 1000.0
                token = generate_token("c-exp", "l-exp")
                # Verify at T + TTL + 1 (past expiry)
                mock_time.time.return_value = 1000 + TOKEN_TTL_SECONDS + 1
                with pytest.raises(TokenError, match="expired"):
                    verify_token(token)

    def test_tampered_token_raises(self):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            token = generate_token("c-tam", "l-tam")
        # Flip one byte in the token
        token_bytes = list(token)
        token_bytes[-2] = "x" if token_bytes[-2] != "x" else "y"
        bad_token = "".join(token_bytes)
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            with pytest.raises(TokenError):
                verify_token(bad_token)

    def test_garbage_token_raises(self):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            with pytest.raises(TokenError):
                verify_token("not-a-valid-token!!!")

    def test_different_secrets_reject(self):
        """Token signed with secret A must be rejected by secret B."""
        with patch("app.consent.tokens._get_secret", return_value=b"secret-AAAAAAAAAAAAAAAAAAAAAAAA"):
            token = generate_token("c-s", "l-s")
        with patch("app.consent.tokens._get_secret", return_value=b"secret-BBBBBBBBBBBBBBBBBBBBBBBB"):
            with pytest.raises(TokenError, match="signature"):
                verify_token(token)

    def test_token_payload_frozen(self):
        """TokenPayload must be immutable."""
        payload = TokenPayload(
            ghl_contact_id="c",
            location_id="l",
            expires_at=9999999999,
        )
        with pytest.raises(Exception):
            payload.ghl_contact_id = "changed"  # type: ignore[misc]

    def test_empty_string_token_raises(self):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            with pytest.raises(TokenError):
                verify_token("")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Consent REST API endpoints
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


def _make_test_app(session_factory):
    """
    Return the real FastAPI app with:
      - get_db overridden to use the in-memory SQLite session factory.
      - Lifespan components (DB engine, worker tasks, scheduler) patched out
        so TestClient doesn't try to connect to Postgres.
    """
    from contextlib import asynccontextmanager  # noqa: PLC0415

    from fastapi import FastAPI                  # noqa: PLC0415

    from app.api.v1.admin.consent import router as consent_router          # noqa: PLC0415
    from app.api.v1.webhooks.ghl_inbound import router as ghl_inbound_router  # noqa: PLC0415
    from app.db.session import get_db                                       # noqa: PLC0415

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield  # no DB engine, no workers, no scheduler

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(consent_router)
    app.include_router(ghl_inbound_router)

    async def _override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return app


class TestConsentAPIEndpoints:
    """Integration tests for the consent REST API via TestClient."""

    @pytest_asyncio.fixture(autouse=True)
    async def _setup(self, engine):
        self._factory = async_sessionmaker(engine, expire_on_commit=False)

        # Create a Location row for the tests
        async with self._factory() as sess:
            self._loc = Location(
                id=uuid.uuid4(),
                eversports_studio_id="api-test-studio",
                ghl_subaccount_id=f"ghl-api-{uuid.uuid4().hex[:8]}",
                ghl_oauth_token_ref="secret://api/ghl",
                eversports_credentials_ref="secret://api/eversports",
                timezone="Europe/Vienna",
                studio_owner_email="owner@api.test",
                studio_name="API Test Studio",
                location_name="API Test Studio — Main",
                eversports_cookie_state="ok",
                historical_sync_flag="pending",
            )
            sess.add(self._loc)
            await sess.commit()

    @pytest.fixture
    def client(self):
        app = _make_test_app(self._factory)
        # Disable lifespan (no worker tasks in tests)
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c

    # ── POST /api/v1/consent/gate ─────────────────────────────────────────────

    def test_gate_allow(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/gate",
            json={
                "ghl_contact_id": "ghl-api-001",
                "location_id": str(self._loc.id),
                "channel": "whatsapp",
                "contact_tags": [],
                "contact_custom_fields": {"consent_marketing_whatsapp": True},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "allow"

    def test_gate_deny_opted_out(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/gate",
            json={
                "ghl_contact_id": "ghl-api-002",
                "location_id": str(self._loc.id),
                "channel": "email",
                "contact_tags": ["opted-out"],
                "contact_custom_fields": {"consent_marketing_email": True},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "deny"

    def test_gate_deny_no_consent(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/gate",
            json={
                "ghl_contact_id": "ghl-api-003",
                "location_id": str(self._loc.id),
                "channel": "email",
                "contact_tags": [],
                "contact_custom_fields": {},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "deny"

    def test_gate_transactional_allow(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/gate",
            json={
                "ghl_contact_id": "ghl-api-004",
                "location_id": str(self._loc.id),
                "channel": "whatsapp",
                "contact_tags": ["opted-out"],
                "contact_custom_fields": {},
                "transactional": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "allow"

    def test_gate_unknown_location_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/gate",
            json={
                "ghl_contact_id": "ghl-api-x",
                "location_id": str(uuid.uuid4()),  # non-existent
                "channel": "email",
                "contact_tags": [],
                "contact_custom_fields": {},
            },
        )
        # Gate doesn't look up location — returns deny for unknown location
        # (gate is stateless; it just evaluates the passed contact state)
        assert resp.status_code == 200

    # ── POST /api/v1/consent/grant ────────────────────────────────────────────

    def test_grant_returns_201(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/grant",
            json={
                "ghl_contact_id": "ghl-grant-001",
                "location_id": str(self._loc.id),
                "channel": "email",
                "source": "double-opt-in",
                "actor": "customer",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["event"] == "granted"
        assert data["channel"] == "email"
        assert uuid.UUID(data["audit_id"])  # valid UUID

    def test_grant_unknown_location_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/grant",
            json={
                "ghl_contact_id": "ghl-grant-002",
                "location_id": str(uuid.uuid4()),
                "channel": "email",
                "source": "double-opt-in",
            },
        )
        assert resp.status_code == 404

    # ── POST /api/v1/consent/revoke ───────────────────────────────────────────

    def test_revoke_returns_201(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/revoke",
            json={
                "ghl_contact_id": "ghl-revoke-001",
                "location_id": str(self._loc.id),
                "channel": "whatsapp",
                "source": "stop-keyword",
                "actor": "customer",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["event"] == "revoked"
        assert data["channel"] == "whatsapp"

    def test_revoke_unknown_location_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/consent/revoke",
            json={
                "ghl_contact_id": "ghl-revoke-002",
                "location_id": str(uuid.uuid4()),
                "channel": "email",
                "source": "stop-keyword",
            },
        )
        assert resp.status_code == 404

    # ── GET /api/v1/consent/preference-centre/{token} ─────────────────────────

    def test_preference_centre_get_valid_token(self, client: TestClient):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            token = generate_token("ghl-pref-001", str(self._loc.id))
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            resp = client.get(f"/api/v1/consent/preference-centre/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ghl_contact_id"] == "ghl-pref-001"
        assert "preference_centre_token" in data  # fresh token included

    def test_preference_centre_get_invalid_token_400(self, client: TestClient):
        resp = client.get("/api/v1/consent/preference-centre/not-a-real-token")
        assert resp.status_code == 400

    # ── PATCH /api/v1/consent/preference-centre/{token} ──────────────────────

    def test_preference_centre_patch_updates(self, client: TestClient):
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            token = generate_token("ghl-pref-002", str(self._loc.id))
            resp = client.patch(
                f"/api/v1/consent/preference-centre/{token}",
                json={
                    "consent_marketing_email": False,
                    "consent_marketing_whatsapp": True,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "email" in data["updated_channels"]
        assert "whatsapp" in data["updated_channels"]
        assert len(data["audit_ids"]) == 2

    def test_preference_centre_patch_no_changes(self, client: TestClient):
        """PATCH with all None fields → no updates."""
        with patch("app.consent.tokens._get_secret", return_value=b"test-secret-key-32bytes-long!!x"):
            token = generate_token("ghl-pref-003", str(self._loc.id))
            resp = client.patch(
                f"/api/v1/consent/preference-centre/{token}",
                json={},  # all fields absent (None default)
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated_channels"] == []
        assert data["audit_ids"] == []

    def test_preference_centre_patch_invalid_token_400(self, client: TestClient):
        resp = client.patch(
            "/api/v1/consent/preference-centre/bad-token-here",
            json={"consent_marketing_email": True},
        )
        assert resp.status_code == 400

    # ── POST /api/v1/consent/sweep/{location_id} ──────────────────────────────

    def test_sweep_returns_200(self, client: TestClient):
        resp = client.post(f"/api/v1/consent/sweep/{self._loc.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["location_id"] == str(self._loc.id)
        assert "contacts_queued" in data

    def test_sweep_unknown_location_404(self, client: TestClient):
        resp = client.post(f"/api/v1/consent/sweep/{uuid.uuid4()}")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 6. GHL inbound webhook (STOP handler)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGHLInboundWebhook:
    """Integration tests for POST /api/v1/webhooks/ghl/inbound."""

    @pytest_asyncio.fixture(autouse=True)
    async def _setup(self, engine):
        self._factory = async_sessionmaker(engine, expire_on_commit=False)

        async with self._factory() as sess:
            self._loc = Location(
                id=uuid.uuid4(),
                eversports_studio_id="webhook-test-studio",
                ghl_subaccount_id=f"ghl-wb-{uuid.uuid4().hex[:8]}",
                ghl_oauth_token_ref="webhook-secret",
                eversports_credentials_ref="secret://wb/eversports",
                timezone="Europe/Vienna",
                studio_owner_email="owner@webhook.test",
                studio_name="Webhook Test Studio",
                location_name="Webhook Test Studio — Main",
                eversports_cookie_state="ok",
                historical_sync_flag="pending",
            )
            sess.add(self._loc)
            await sess.commit()

    @pytest.fixture
    def client(self):
        app = _make_test_app(self._factory)
        with patch("app.config.get_settings") as mock_settings:
            settings = mock_settings.return_value
            settings.ghl_webhook_skip_sig_check = True  # skip signature in tests
            with TestClient(app, raise_server_exceptions=True) as c:
                yield c

    def _payload(self, message: str = "hello", channel: str = "whatsapp") -> dict:
        return {
            "type": "InboundMessage",
            "locationId": self._loc.ghl_subaccount_id,
            "contactId": "ghl-inbound-001",
            "channel": channel,
            "messageBody": message,
            "firstName": "Test",
            "locale": "de-AT",
        }

    # ── Non-STOP message passes through ───────────────────────────────────────

    def test_non_stop_message_returns_false(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("Danke für die Info!"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_stop"] is False
        assert data["ghl_actions"] == []

    def test_non_stop_no_confirmation_message(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("Ich komme nächste Woche"),
        )
        data = resp.json()
        assert data["confirmation_message"] is None

    # ── STOP message triggers full opt-out flow ───────────────────────────────

    def test_stop_keyword_returns_true(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_stop"] is True

    def test_stop_returns_ghl_actions(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("stop"),
        )
        actions = resp.json()["ghl_actions"]
        action_types = [a["action"] for a in actions]
        assert "update_contact_field" in action_types
        assert "apply_tag" in action_types
        assert "remove_from_all_sequences" in action_types
        assert "send_message" in action_types

    def test_stop_sets_consent_field_false(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP", channel="whatsapp"),
        )
        actions = resp.json()["ghl_actions"]
        field_actions = [a for a in actions if a["action"] == "update_contact_field"]
        consent_field = next(
            (a for a in field_actions if a["field"] == "consent_marketing_whatsapp"), None
        )
        assert consent_field is not None
        assert consent_field["value"] is False

    def test_stop_applies_opted_out_tag(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP"),
        )
        actions = resp.json()["ghl_actions"]
        tag_actions = [a for a in actions if a["action"] == "apply_tag"]
        assert any(a["tag"] == "opted-out" for a in tag_actions)

    def test_stop_returns_localised_confirmation(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP"),
        )
        data = resp.json()
        assert data["confirmation_message"] is not None
        assert "abgemeldet" in data["confirmation_message"]  # de-AT locale

    def test_german_stop_keyword_stopp(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOPP"),
        )
        assert resp.json()["is_stop"] is True

    def test_german_stop_keyword_abmelden(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("Abmelden"),
        )
        assert resp.json()["is_stop"] is True

    def test_sms_channel_normalised_to_whatsapp(self, client: TestClient):
        """SMS channel must be normalised to 'whatsapp' for consent purposes."""
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP", channel="sms"),
        )
        assert resp.json()["channel"] == "whatsapp"

    def test_unknown_location_returns_404(self, client: TestClient):
        payload = {
            "type": "InboundMessage",
            "locationId": "ghl-nonexistent-location",
            "contactId": "ghl-inbound-x",
            "channel": "whatsapp",
            "messageBody": "STOP",
        }
        resp = client.post("/api/v1/webhooks/ghl/inbound", json=payload)
        assert resp.status_code == 404

    # ── Channel normalization ─────────────────────────────────────────────────

    def test_email_channel_stays_email(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("stop", channel="email"),
        )
        assert resp.json()["channel"] == "email"

    def test_voice_channel_stays_voice(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("stop", channel="voice"),
        )
        assert resp.json()["channel"] == "voice"

    def test_unknown_channel_normalised_to_whatsapp(self, client: TestClient):
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("hello", channel="telegram"),
        )
        assert resp.json()["channel"] == "whatsapp"

    def test_stop_returns_exactly_five_ghl_actions(self, client: TestClient):
        """STOP flow must return exactly 5 ghl_actions (no missing, no extras)."""
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP"),
        )
        actions = resp.json()["ghl_actions"]
        assert len(actions) == 5

    def test_stop_includes_revoked_at_timestamp_action(self, client: TestClient):
        """STOP must stamp consent_revoked_{channel}_at = '__now__'."""
        resp = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json=self._payload("STOP", channel="whatsapp"),
        )
        actions = resp.json()["ghl_actions"]
        field_actions = [a for a in actions if a.get("action") == "update_contact_field"]
        ts_action = next(
            (a for a in field_actions if a.get("field") == "consent_revoked_whatsapp_at"),
            None,
        )
        assert ts_action is not None, "consent_revoked_whatsapp_at action missing"
        assert ts_action["value"] == "__now__"

    @pytest.mark.asyncio
    async def test_stop_writes_revocation_row_to_db(self):
        """STOP webhook must persist a 'revoked' row to consent_audit."""
        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: PLC0415

        eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(eng, expire_on_commit=False)

        # Create location
        async with factory() as sess:
            loc = Location(
                id=uuid.uuid4(),
                eversports_studio_id="db-test-studio",
                ghl_subaccount_id=f"ghl-db-{uuid.uuid4().hex[:8]}",
                ghl_oauth_token_ref="secret",
                eversports_credentials_ref="secret",
                timezone="Europe/Vienna",
                studio_owner_email="owner@db.test",
                studio_name="DB Test Studio",
                location_name="DB Test Studio — Main",
                eversports_cookie_state="ok",
                historical_sync_flag="pending",
            )
            sess.add(loc)
            await sess.commit()

        app = _make_test_app(factory)
        with patch("app.config.get_settings") as mock_s:
            mock_s.return_value.ghl_webhook_skip_sig_check = True
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.post(
                    "/api/v1/webhooks/ghl/inbound",
                    json={
                        "type": "InboundMessage",
                        "locationId": loc.ghl_subaccount_id,
                        "contactId": "ghl-db-contact-001",
                        "channel": "whatsapp",
                        "messageBody": "STOP",
                        "locale": "de-AT",
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["is_stop"] is True

        # Verify DB row
        async with factory() as sess:
            rows = (await sess.execute(select(ConsentAudit))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event == "revoked"
        assert row.channel == "whatsapp"
        assert row.ghl_contact_id == "ghl-db-contact-001"
        assert row.source == "stop-keyword"
        assert row.actor == "customer"
        assert row.message_shown == "STOP"

        await eng.dispose()
