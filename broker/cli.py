"""Manual admin for the broker's devices, for development.

Run it inside the container:

docker exec auth-broker flask --app wsgi devices list
docker exec auth-broker flask --app wsgi devices code <device_id>
docker exec auth-broker flask --app wsgi devices owner <device_id> <user id>
docker exec auth-broker flask --app wsgi devices unpair <device_id> [--config]
docker exec auth-broker flask --app wsgi devices forget <device_id>
docker exec auth-broker flask --app wsgi weather backfill

Like any `flask` command it loads the app, so it needs the app's env vars (the container has them).
"""

import json
from datetime import UTC, datetime

import click
from flask.cli import AppGroup
from sqlalchemy import select

from .clients import weather as weather_client
from .db import db
from .models import Device, DeviceConfig
from .services import assignments, pairing, weather_place

devices = AppGroup("devices", help="Manual device admin for development.")
weather = AppGroup("weather", help="The screens' weather places.")


def _get_device(device_id: str) -> Device:
    device = db.session.get(Device, device_id)
    if device is None:
        msg = f"No device {device_id}."
        raise click.ClickException(msg)
    return device


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


@devices.command("list")
def list_devices() -> None:
    """Show every device and whether it's paired, and to whom."""
    all_devices = db.session.scalars(select(Device).order_by(Device.created_at)).all()
    if not all_devices:
        click.echo("No devices.")
    for device in all_devices:
        pairing_code = pairing.live_code(device.device_id)
        config = device.config
        name = device.device_name or "unnamed"
        paired = f"paired {_timestamp(device.paired_at)} owner={device.owner}" if device.paired_at else "NOT paired"
        code_field = f"  code {pairing_code}" if pairing_code else ""
        weather = (config.weather_location if config else "") or "-"
        stops = json.dumps(config.tfl_stop_ids) if config else None
        created = f"created {_timestamp(device.created_at)}"
        fields = [f"{name} ({device.device_id})", created, paired, f"weather={weather}", f"stops={stops}"]
        click.echo("  ".join(fields) + code_field)


@devices.command()
@click.argument("device_id")
def code(device_id: str) -> None:
    """Issue a fresh pairing code, e.g. to hand a device to a different user."""
    _get_device(device_id)
    new_code = pairing.issue_code(device_id)
    minutes = int(pairing.CODE_TTL.total_seconds() // 60)
    click.echo(f"{new_code}: enter it at /pair within {minutes} minutes (the device shows it too).")


@devices.command()
@click.argument("device_id")
@click.argument("user")
def owner(device_id: str, user: str) -> None:
    """Make a user (their Authentik user id) the owner of a device, e.g. one paired before owners existed."""
    device = _get_device(device_id)
    device.owner = user
    if device.paired_at is None:
        device.paired_at = datetime.now(UTC)
    db.session.commit()
    click.echo(f"{device_id} now belongs to {user}.")


@devices.command()
@click.argument("device_id")
@click.option("--config", "wipe_config", is_flag=True, help="Also reset its settings and linked accounts.")
def unpair(device_id: str, *, wipe_config: bool) -> None:
    """Make a device show a pairing code again."""
    device = _get_device(device_id)
    device.paired_at = None
    device.owner = None
    device.pairing_codes.clear()
    if wipe_config:
        # Reset to the model's defaults by replacing the row. The flush matters: without it SQLAlchemy
        # turns the delete + insert of the same key into one UPDATE, which leaves the old values in place.
        device.config = None
        db.session.flush()
        device.config = DeviceConfig()
        device.spotify_token = None
        device.glowmarkt_credentials = None
        device.github_token = None
        device.apple_music_token = None
        device.apple_music_plays = []
        device.frame = None
    db.session.commit()
    click.echo(f"{device_id} is unpaired; it will show a new pairing code on its next poll.")


@devices.command()
@click.argument("device_id")
def forget(device_id: str) -> None:
    """Delete a device entirely (it must register again)."""
    device = _get_device(device_id)
    # Its renderer, if the orchestrator started one, is removed too; the assignment outlives the device for that.
    assignments.mark_deleting(device)
    # The relationships cascade, so the device's codes, config, tokens and frame go with it.
    db.session.delete(device)
    db.session.commit()
    click.echo(f"{device_id} forgotten; delete its credentials file so it registers again.")


@weather.command()
def backfill() -> None:
    """Give every config with a weather location but no picked place the place Open-Meteo finds first for it.

    That's the place a renderer looking the name up finds too. weather_location itself is left as it is, so what
    renderers are sent doesn't change. Safe to run again: a config with a place is skipped. One Open-Meteo can't
    find is reported and left without a place; if Open-Meteo can't be asked, run it again later.
    """
    configs = db.session.scalars(
        select(DeviceConfig)
        .where(DeviceConfig.weather_location != "", DeviceConfig.weather_place_id.is_(None))
        .order_by(DeviceConfig.device_id)
    ).all()
    resolved = not_found = unavailable = 0
    for config in configs:
        try:
            results = weather_client.search_places(config.weather_location, count=1)
        except weather_client.GeocodingUnavailableError:
            unavailable += 1
            click.echo(f"{config.device_id}: couldn't ask Open-Meteo about {config.weather_location!r}; skipped.")
            continue
        if not results:
            not_found += 1
            click.echo(f"{config.device_id}: Open-Meteo finds nothing for {config.weather_location!r}; skipped.")
            continue
        weather_place.store(config, results[0], keep_location=True)
        resolved += 1
        key = weather_place.weather_key(config)
        click.echo(f"{config.device_id}: {config.weather_location!r} is {results[0]['label']} ({key}).")
    db.session.commit()
    click.echo(f"{resolved} given a place, {not_found} not found, {unavailable} not checked.")
    if unavailable:
        msg = f"Open-Meteo couldn't be asked about {unavailable}; run it again later."
        raise click.ClickException(msg)
