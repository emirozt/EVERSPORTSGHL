#!/usr/bin/env python
"""Provision a new studio location in the foundation database.

Usage:
    python scripts/onboard_location.py \\
        --studio-name "Flow Pilates" \\
        --location-name "Flow Pilates — Mariahilf" \\
        --timezone "Europe/Vienna" \\
        --eversports-studio-id "Yneu3U" \\
        --ghl-subaccount-id "abc123" \\
        --ghl-oauth-token-ref "secret://ghl/abc123" \\
        --eversports-credentials-ref "secret://eversports/login/abc123" \\
        --studio-owner-email "owner@studio.com"

All --*-ref flags accept placeholder strings at M1 (credentials stored in the
secrets manager, not in Postgres). Real secret references are wired up during
M3 (GHL client) and M2 (scraper).
"""

import asyncio
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

import click
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models.location import Location


async def _insert_location(
    session: AsyncSession,
    *,
    studio_name: str,
    location_name: str,
    timezone: str,
    country: str,
    eversports_studio_id: str,
    eversports_location_id: str | None,
    ghl_subaccount_id: str,
    ghl_oauth_token_ref: str,
    eversports_credentials_ref: str,
    studio_owner_email: str,
    late_cancel_window_hours: int,
    ai_monthly_budget_usd: float,
    consent_default_locale: str,
    writeback_mode: str,
) -> Location:
    existing = await session.execute(
        select(Location).where(Location.ghl_subaccount_id == ghl_subaccount_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise click.ClickException(
            f"A location with ghl_subaccount_id='{ghl_subaccount_id}' already exists."
        )

    loc = Location(
        studio_name=studio_name,
        location_name=location_name,
        timezone=timezone,
        country=country,
        eversports_studio_id=eversports_studio_id,
        eversports_location_id=eversports_location_id,
        ghl_subaccount_id=ghl_subaccount_id,
        ghl_oauth_token_ref=ghl_oauth_token_ref,
        eversports_credentials_ref=eversports_credentials_ref,
        studio_owner_email=studio_owner_email,
        late_cancel_window_hours=late_cancel_window_hours,
        ai_monthly_budget_usd=ai_monthly_budget_usd,  # type: ignore[arg-type]
        consent_default_locale=consent_default_locale,
        writeback_mode=writeback_mode,
    )
    session.add(loc)
    await session.commit()
    await session.refresh(loc)
    return loc


@click.command()
@click.option("--studio-name", required=True, help="Studio brand name (used in AI prompts)")
@click.option(
    "--location-name",
    required=True,
    help='Location display name, e.g. "Flow Pilates — Mariahilf"',
)
@click.option("--timezone", required=True, help="IANA timezone, e.g. Europe/Vienna")
@click.option(
    "--country",
    default="DE",
    show_default=True,
    type=click.Choice(["DE", "AT", "CH"]),
    help="ISO 3166-1 alpha-2 country code — used as default region for phone normalisation",
)
@click.option(
    "--eversports-studio-id",
    required=True,
    help="Eversports studio ID (from export URLs)",
)
@click.option("--ghl-subaccount-id", required=True, help="GoHighLevel sub-account ID")
@click.option(
    "--ghl-oauth-token-ref",
    required=True,
    help='Secrets manager reference, e.g. "secret://ghl/abc123"',
)
@click.option(
    "--eversports-credentials-ref",
    required=True,
    help='Secrets manager reference, e.g. "secret://eversports/login/abc123"',
)
@click.option("--studio-owner-email", required=True, help="Owner email for admin notifications")
@click.option(
    "--eversports-location-id",
    default=None,
    help="Eversports location ID (optional — multi-location studios only)",
)
@click.option(
    "--late-cancel-window-hours",
    default=24,
    show_default=True,
    type=int,
    help="Hours before session that triggers late-cancel policy",
)
@click.option(
    "--ai-monthly-budget-usd",
    default=200.0,
    show_default=True,
    type=float,
    help="Monthly AI spend hard cap in USD",
)
@click.option(
    "--consent-default-locale",
    default="de-AT",
    show_default=True,
    help="Default locale for consent capture forms (de-AT | de-DE | en)",
)
@click.option(
    "--writeback-mode",
    default="auto_execute",
    show_default=True,
    type=click.Choice(["auto_execute", "admin_task"]),
    help="How UC04/UC05 execute Eversports actions. Requires DPA attestation for auto_execute.",
)
@click.option(
    "--database-url",
    default=None,
    help="Override DATABASE_URL from env/config (useful for one-off provisioning)",
)
def main(
    studio_name: str,
    location_name: str,
    timezone: str,
    country: str,
    eversports_studio_id: str,
    ghl_subaccount_id: str,
    ghl_oauth_token_ref: str,
    eversports_credentials_ref: str,
    studio_owner_email: str,
    eversports_location_id: str | None,
    late_cancel_window_hours: int,
    ai_monthly_budget_usd: float,
    consent_default_locale: str,
    writeback_mode: str,
    database_url: str | None,
) -> None:
    """Provision a new studio location in the foundation database."""

    if writeback_mode == "auto_execute":
        click.echo(
            "⚠  writeback_mode=auto_execute requires the DPA studio-attestation clause to be\n"
            "   signed by the studio owner before the first writeback runs"
            " (see 08_consent_model.md).\n"
            "   Use --writeback-mode admin_task if the DPA has not yet been signed.",
            err=True,
        )
        if not click.confirm("Has the studio signed the DPA attestation clause?", default=False):
            click.echo("Aborted. Provision with --writeback-mode admin_task until DPA is signed.")
            raise SystemExit(1)

    url = database_url or get_settings().database_url
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run() -> None:
        async with factory() as session:
            loc = await _insert_location(
                session,
                studio_name=studio_name,
                location_name=location_name,
                timezone=timezone,
                country=country,
                eversports_studio_id=eversports_studio_id,
                eversports_location_id=eversports_location_id,
                ghl_subaccount_id=ghl_subaccount_id,
                ghl_oauth_token_ref=ghl_oauth_token_ref,
                eversports_credentials_ref=eversports_credentials_ref,
                studio_owner_email=studio_owner_email,
                late_cancel_window_hours=late_cancel_window_hours,
                ai_monthly_budget_usd=ai_monthly_budget_usd,
                consent_default_locale=consent_default_locale,
                writeback_mode=writeback_mode,
            )
        await engine.dispose()
        click.echo("✓ Location provisioned")
        click.echo(f"  id                  : {loc.id}")
        click.echo(f"  studio_name         : {loc.studio_name}")
        click.echo(f"  location_name       : {loc.location_name}")
        click.echo(f"  ghl_subaccount_id   : {loc.ghl_subaccount_id}")
        click.echo(f"  timezone            : {loc.timezone}")
        click.echo(f"  country             : {loc.country}")
        click.echo(f"  writeback_mode      : {loc.writeback_mode}")
        click.echo(f"  historical_sync_flag: {loc.historical_sync_flag}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
