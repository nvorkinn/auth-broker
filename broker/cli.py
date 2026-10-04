"""Manual admin for the broker's devices, for development. Run it inside the container:

docker compose exec auth-broker flask --app wsgi devices list
docker compose exec auth-broker flask --app wsgi devices code <device_id>
docker compose exec auth-broker flask --app wsgi devices unpair <device_id> [--config]
docker compose exec auth-broker flask --app wsgi devices forget <device_id>

Like any `flask` command it loads the app, so it needs the app's env vars (the container has them).
"""

import json
from datetime import datetime

import click
from flask.cli import AppGroup
from sqlalchemy import select

from .db import db
from .models import Device, DeviceConfig
from .services import pairing

devices = AppGroup("devices", help="Manual device admin for development.")


def _get_device(device_id: str) -> Device:
    device = db.session.get(Device, device_id)
    if device is None:
        raise click.ClickException(f"No device {device_id}.")
    return device


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


@devices.command("list")
def list_devices() -> None:
    """Show every device and whether it's paired."""
    all_devices = db.session.scalars(select(Device).order_by(Device.created_at)).all()
    if not all_devices:
        click.echo("No devices.")
    for device in all_devices:
        code = pairing.live_code(device.device_id)
        config = device.config
        name = device.device_name or "unnamed"
        paired = f"paired {_timestamp(device.paired_at)}" if device.paired_at else "NOT paired"
        code_field = f"  code {code}" if code else ""
        weather = (config.weather_location if config else "") or "-"
        stops = json.dumps(config.tfl_stop_ids) if config else None
        created = f"created {_timestamp(device.created_at)}"
        fields = [f"{name} ({device.device_id})", created, paired, f"weather={weather}", f"stops={stops}"]
        click.echo("  ".join(fields) + code_field)


@devices.command()
@click.argument("device_id")
def code(device_id: str) -> None:
    """Issue a fresh pairing code, e.g. to link a new browser to an already-paired device."""
    _get_device(device_id)
    new_code = pairing.issue_code(device_id)
    minutes = int(pairing.CODE_TTL.total_seconds() // 60)
    click.echo(f"{new_code}: enter it at /pair within {minutes} minutes (the device shows it too).")


@devices.command()
@click.argument("device_id")
@click.option("--config", "wipe_config", is_flag=True, help="Also reset its settings and linked accounts.")
def unpair(device_id: str, wipe_config: bool) -> None:
    """Make a device show a pairing code again."""
    device = _get_device(device_id)
    device.paired_at = None
    device.pairing_codes.clear()
    if wipe_config:
        # Reset to the model's defaults by replacing the row. The flush matters: without it SQLAlchemy
        # turns the delete + insert of the same key into one UPDATE, which leaves the old values in place.
        device.config = None
        db.session.flush()
        device.config = DeviceConfig()
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
