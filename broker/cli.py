"""Manual admin for the broker's devices, for development. Run it inside the container:

docker compose exec auth-broker flask --app wsgi devices list
docker compose exec auth-broker flask --app wsgi devices unpair <device_id> [--config]
docker compose exec auth-broker flask --app wsgi devices forget <device_id>

Like any `flask` command it loads the app, so it needs the app's env vars (the container has them).
"""

import json
from datetime import UTC, datetime

import click
from flask.cli import AppGroup
from sqlalchemy import select

from .db import db
from .models import Device, PairingCode

devices = AppGroup("devices", help="Manual device admin for development.")


def _get_device(device_id: str) -> Device:
    device = db.session.get(Device, device_id)
    if device is None:
        raise click.ClickException(f"No device {device_id}.")
    return device


@devices.command("list")
def list_devices() -> None:
    """Show every device and whether it's paired."""
    now = datetime.now(UTC).isoformat()
    all_devices = db.session.scalars(select(Device).order_by(Device.created_at)).all()
    if not all_devices:
        click.echo("No devices.")
    for device in all_devices:
        code = db.session.scalar(
            select(PairingCode.code).where(PairingCode.device_id == device.device_id, PairingCode.expires_at > now)
        )
        config = device.config
        name = device.device_name or "unnamed"
        paired = f"paired {device.paired_at}" if device.paired_at else "NOT paired"
        code_field = f"  code {code}" if code else ""
        weather = (config.weather_location if config else "") or "-"
        stops = json.dumps(config.tfl_stop_ids) if config else None
        fields = [f"{name} ({device.device_id})", f"created {device.created_at}", paired, f"weather={weather}"]
        click.echo("  ".join([*fields, f"stops={stops}"]) + code_field)


@devices.command()
@click.argument("device_id")
@click.option("--config", "wipe_config", is_flag=True, help="Also reset its settings and linked accounts.")
def unpair(device_id: str, wipe_config: bool) -> None:
    """Make a device show a pairing code again."""
    device = _get_device(device_id)
    device.paired_at = None
    device.pairing_codes.clear()
    if wipe_config:
        config = device.config
        if config is not None:
            config.interval, config.weather_location, config.postcode = 15, "", ""
            config.spotify_enabled, config.tfl_stop_ids = False, []
        device.spotify_token = None
        device.glowmarkt_credentials = None
        device.frame = None
    db.session.commit()
    click.echo(f"{device_id} is unpaired; it will show a new pairing code on its next poll.")


@devices.command()
@click.argument("device_id")
def forget(device_id: str) -> None:
    """Delete a device entirely (it must register again)."""
    # The relationships cascade, so the device's codes, config, tokens and frame go with it.
    db.session.delete(_get_device(device_id))
    db.session.commit()
    click.echo(f"{device_id} forgotten; delete its credentials file so it registers again.")
