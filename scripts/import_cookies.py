#!/usr/bin/env python3
"""Import Eversports session cookies into the foundation database.

Usage:
    # From a JSON string (pipe-friendly):
    python scripts/import_cookies.py \\
        --location-id <UUID> \\
        --cookies-json '[{"name":"eversports-manager.sid","value":"...", \
"domain":"app.eversportsmanager.com",...}]'

    # From a file exported by Cookie-Editor:
    python scripts/import_cookies.py \\
        --location-id <UUID> \\
        --cookies-file /path/to/cookies.json

After running this, the scraper will pick up the cookies on its next run and
set eversports_cookie_state = 'ok' if authentication succeeds.

Cookie expiry:
    Eversports admin sessions last approximately 30 days. Re-export and re-run
    this script roughly once a month, or whenever the scraper reports:
    "Eversports session expired — please re-export cookies."
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import click
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models.location import Location


async def _import(
    location_id: str,
    cookies: list[dict],
    database_url: str,
) -> None:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        result = await session.execute(
            select(Location).where(Location.id == location_id)  # type: ignore[arg-type]
        )
        loc = result.scalar_one_or_none()
        if loc is None:
            raise click.ClickException(f"Location not found: {location_id}")

        await session.execute(
            update(Location)
            .where(Location.id == location_id)  # type: ignore[arg-type]
            .values(
                eversports_cookie_cache=cookies,
                eversports_cookie_state="ok",
            )
        )
        await session.commit()

    await engine.dispose()

    click.echo(f"✓ Cookies imported for location {location_id}")
    click.echo("  cookie_state  : ok")
    click.echo(f"  cookies_count : {len(cookies)}")
    names = [c.get("name", "?") for c in cookies]
    click.echo(f"  cookie_names  : {', '.join(names)}")


@click.command()
@click.option("--location-id", required=True, help="locations.id UUID")
@click.option(
    "--cookies-json",
    default=None,
    help="Cookie-Editor JSON array as a string",
)
@click.option(
    "--cookies-file",
    default=None,
    type=click.Path(exists=True, readable=True),
    help="Path to Cookie-Editor JSON export file",
)
@click.option(
    "--database-url",
    default=None,
    help="Override DATABASE_URL from env/config",
)
def main(
    location_id: str,
    cookies_json: str | None,
    cookies_file: str | None,
    database_url: str | None,
) -> None:
    """Import Cookie-Editor session cookies into the Eversports connector DB."""

    if cookies_json is None and cookies_file is None:
        raise click.UsageError("Provide either --cookies-json or --cookies-file.")
    if cookies_json is not None and cookies_file is not None:
        raise click.UsageError("Provide only one of --cookies-json or --cookies-file.")

    if cookies_file is not None:
        raw = Path(cookies_file).read_text(encoding="utf-8")
    else:
        raw = cookies_json  # type: ignore[assignment]

    try:
        cookies: list[dict] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON: {exc}") from exc

    if not isinstance(cookies, list):
        raise click.ClickException("Expected a JSON array of cookie objects.")
    if len(cookies) == 0:
        raise click.ClickException("Cookie array is empty.")

    url = database_url or get_settings().database_url
    asyncio.run(_import(location_id, cookies, url))


if __name__ == "__main__":
    main()
