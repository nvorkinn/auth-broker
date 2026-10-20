"""Admin commands for the broker's devices, screens' weather places, the configs in the store and Redis itself.

Run them inside the container:

docker exec auth-broker flask --app wsgi devices list
docker exec auth-broker flask --app wsgi devices code <device_id>
docker exec auth-broker flask --app wsgi devices owner <device_id> <user id>
docker exec auth-broker flask --app wsgi devices unpair <device_id> [--config]
docker exec auth-broker flask --app wsgi devices forget <device_id>
docker exec auth-broker flask --app wsgi weather backfill
docker exec auth-broker flask --app wsgi config publish
docker exec auth-broker flask --app wsgi redis subs [source]
docker exec auth-broker flask --app wsgi redis keys [pattern] [--limit N]

Like any `flask` command it loads the app, so it needs the app's env vars (the container has them).
"""

import json
from datetime import UTC, datetime
from itertools import islice

import click
from countdown_api import keys as store_keys
from flask import current_app
from flask.cli import AppGroup
from redis import RedisError
from sqlalchemy import select

from .clients import weather as weather_client
from .db import db
from .models import Device, DeviceConfig
from .routes.pages import _write_subscriptions_to_redis
from .services import assignments, config_store, device_keys, frame_record, pairing, pairing_key, weather_place

devices = AppGroup("devices", help="Manual device admin for development.")
weather = AppGroup("weather", help="The screens' weather places.")
config = AppGroup("config", help="The screens' configs in the store.")
store = AppGroup("redis", help="Look at what the broker keeps in Redis.")


def publish_store() -> None:
    """Write the subscriptions and every screen's config to the store from the database.

    Run as the broker starts (gunicorn.conf.py) and by `flask config publish`. Needs an app context; an unreachable
    Redis is logged, not raised.
    """
    _write_subscriptions_to_redis()
    config_store.publish_all()


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
    # The render pool shows the code (its pairing key); the config's copy goes from the store while it's live.
    config_store.publish(device_id)
    minutes = int(pairing.CODE_TTL.total_seconds() // 60)
    click.echo(f"{new_code}: enter it at /pair within {minutes} minutes (the device shows it too).")


@devices.command()
@click.argument("device_id")
@click.argument("user")
def owner(device_id: str, user: str) -> None:
    """Make a user (their Authentik user id) the owner of a device."""
    device = _get_device(device_id)
    previous = device.owner
    device.owner = user
    if device.paired_at is None:
        device.paired_at = datetime.now(UTC)
    # As pairing does: named, so the render pool draws it, and no code left to show.
    assignments.auto_name(device)
    device.pairing_codes.clear()
    db.session.commit()
    if previous != user:
        # Nothing the store has from before is the new owner's to see (its pairing code's key included).
        device_keys.forget(device_id)
    else:
        pairing_key.delete(device_id)
    config_store.publish(device_id)
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
    db.session.commit()
    # Its data in the store, so the next owner never sees the last one's. With its accounts kept (no --config), the
    # providers write their keys again from them.
    device_keys.forget(device_id)
    if wipe_config:
        frame_record.forget(device_id)
    # A code for the pool to draw, as an unpaired device has no config in the store to draw.
    new_code = pairing.issue_code(device_id)
    config_store.publish(device_id)
    minutes = int(pairing.CODE_TTL.total_seconds() // 60)
    click.echo(
        f"{device_id} is unpaired; it shows the pairing code {new_code} for {minutes} minutes "
        "(`devices code` for another), or pair it by its token."
    )


@devices.command()
@click.argument("device_id")
def forget(device_id: str) -> None:
    """Delete a device entirely (it must register again)."""
    device = _get_device(device_id)
    # The relationships cascade, so the device's codes, config, tokens and name go with it.
    db.session.delete(device)
    db.session.commit()
    frame_record.forget(device_id)
    device_keys.forget(device_id)
    config_store.publish(device_id)
    click.echo(f"{device_id} forgotten; delete its credentials file so it registers again.")


@weather.command()
def backfill() -> None:
    """Give every config with a weather location but no picked place the place Open-Meteo finds first for it.

    weather_location itself is left as it is. Safe to run again: a config with a place is skipped. One Open-Meteo
    can't find is reported and left without a place; if Open-Meteo can't be asked, run it again later.

    Then it rewrites the subscriptions and the configs in the store, as a place changes the key a screen's weather is
    subscribed and read under (weather_place.weather_key).
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
    publish_store()
    click.echo("Rewrote the subscriptions and the configs in the store (the log warns if Redis couldn't be reached).")
    if unavailable:
        msg = f"Open-Meteo couldn't be asked about {unavailable}; run it again later."
        raise click.ClickException(msg)


@config.command()
def publish() -> None:
    """Write every screen's config, and the subscriptions, to the store from the database.

    The broker does this on every config save and as it starts, and rewrites the configs every minute; this restores
    the subscriptions at once after Redis has lost them (it keeps nothing on disk). Safe to run at any time: only the
    screens whose config changed are drawn again.
    """
    publish_store()
    click.echo("Wrote the configs and the subscriptions to the store (the log warns if Redis couldn't be reached).")


@store.command("subs")
@click.argument("source", required=False, type=click.Choice(store_keys.SOURCES))
def subs(source: str | None) -> None:
    """Show how many ids each subscription set holds, or, given a source, the ids in its set.

    A source's set is what its provider fetches for. The postcodes are only counted, never shown: they are never logged.
    """
    redis = current_app.extensions["redis"]
    try:
        if source is None:
            for name in store_keys.SOURCES:
                click.echo(f"{name}: {redis.scard(store_keys.subscription(name))}")
        elif source == store_keys.POSTCODE:
            click.echo(f"{source}: {redis.scard(store_keys.subscription(source))} (not shown)")
        else:
            members = sorted(redis.smembers(store_keys.subscription(source)))
            click.echo(f"{source}: {len(members)}")
            for member in members:
                click.echo(f"  {member}")
    except RedisError as error:
        msg = f"Couldn't ask Redis ({type(error).__name__})."
        raise click.ClickException(msg) from None


@store.command("keys")
@click.argument("pattern", default=f"{store_keys.PREFIX}:*")
@click.option("--limit", default=200, show_default=True, help="Stop after this many keys.")
def keys(pattern: str, limit: int) -> None:
    """List the keys matching PATTERN with their type and time to live, never their values.

    Some values are secrets, or a pairing code that is live. A pattern is Redis's glob, e.g. 'v1:sub:*'.
    """
    redis = current_app.extensions["redis"]
    try:
        found = list(islice(redis.scan_iter(match=pattern, count=1000), limit + 1))
        for name in sorted(found[:limit]):
            ttl = redis.ttl(name)
            click.echo(f"{name}  {redis.type(name)}  {'no TTL' if ttl < 0 else f'{ttl}s'}")
    except RedisError as error:
        msg = f"Couldn't ask Redis ({type(error).__name__})."
        raise click.ClickException(msg) from None
    click.echo(f"{min(len(found), limit)} keys" + (f" (stopped at --limit {limit})" if len(found) > limit else ""))
