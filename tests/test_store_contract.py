"""The broker's side of the store contract (countdown-api's store.yaml, the vendor/countdown-api submodule).

Every key the broker writes or reads that pi-dashboard reads or writes is the contract's, with the contract's TTL, and
what it writes is the contract's canonical example (countdown_api.fixtures), which pi-dashboard's tests read too. The
other modules' tests round-trip most of the values through those examples (test_config_store, test_pairing_key,
test_render_screens, test_render_jobs, test_frame_record, test_glow, test_spotify_provider, test_album_art,
test_developer_provider, test_weather_place); here are the subscriptions, the pace hints, and the keys themselves.
"""

from datetime import UTC, datetime, timedelta

import fakeredis
from countdown_api import fixtures, scripts
from countdown_api import keys as store_keys

from broker import album_art, developer_provider, provider_loop, spotify_provider
from broker.db import db
from broker.models import Device, DeviceConfig, GlowmarktCredentials
from broker.providers import glow
from broker.routes import pages
from broker.services import config_store, device_keys, frame_record, pace, pairing, pairing_key, render_jobs

CONFIG = fixtures.load("config_store.json")
PAIRING = fixtures.load("pairing_key.json")
SUBSCRIPTIONS = fixtures.load("subscriptions.json")


def test_the_keys_are_the_contracts():
    device_id = CONFIG["device_id"]
    assert config_store.key(device_id) == store_keys.config(device_id) == CONFIG["key"]
    assert config_store.PREFIX == store_keys.CONFIG_PREFIX
    assert pairing_key.key(PAIRING["device_id"]) == store_keys.pairing(PAIRING["device_id"]) == PAIRING["key"]
    assert frame_record.key(device_id) == store_keys.frame(device_id)
    assert render_jobs.queued_key(device_id) == store_keys.render_queued(device_id)
    assert (render_jobs.JOBS, render_jobs.SCREENS) == (store_keys.RENDER_JOBS, store_keys.RENDER_SCREENS)
    assert glow.energy_key(device_id) == store_keys.energy(device_id)
    assert spotify_provider.player_key(device_id) == store_keys.spotify(device_id)
    assert spotify_provider.top_key(device_id) == store_keys.spotify_top(device_id)
    assert developer_provider.stored_key(device_id) == store_keys.developer(device_id)
    assert (
        developer_provider.pace_key(device_id)
        == pace.key("developer", device_id)
        == store_keys.pace("developer", device_id)
    )
    url = "https://i.scdn.co/image/ab01300"
    assert album_art.art_key(url) == store_keys.album_art(url)
    assert device_keys.music(device_id) == [store_keys.spotify(device_id), store_keys.spotify_top(device_id)]
    assert provider_loop.SUB_CHANGED == store_keys.SUB_CHANGED
    assert scripts.ENQUEUE.source == render_jobs._ENQUEUE


def test_the_ttls_are_the_contracts():
    assert render_jobs.QUEUED_TTL_MS == store_keys.RENDER_QUEUED_TTL_MS == 60_000
    assert pairing.CODE_TTL == timedelta(seconds=store_keys.PAIRING_TTL_S) == timedelta(minutes=10)
    assert glow.TTL_S == store_keys.ENERGY_TTL_S == 24 * 60 * 60
    assert spotify_provider.PLAYER_TTL_S == store_keys.SPOTIFY_TTL_S == 3 * spotify_provider.INTERVAL_S
    assert spotify_provider.TOP_TTL_S == store_keys.SPOTIFY_TOP_TTL_S == 4 * spotify_provider.TOP_INTERVAL_S
    assert album_art.ART_TTL_S == store_keys.ALBUM_ART_TTL_S == 5 * 60
    assert developer_provider.TTL_S == store_keys.DEVELOPER_TTL_S == 15 * 60
    assert developer_provider.PACE_TTL_S == store_keys.PACE_TTL_S == 60


def _add_example_devices(app) -> None:
    """Add the devices of config_store.json (paired, with a Glowmarkt account) and pairing_key.json (not yet paired)."""
    with app.app_context():
        now = datetime.now(UTC)
        settings = dict(CONFIG["settings"])
        glowmarkt = settings.pop("glowmarkt")
        paired = Device(device_id=CONFIG["device_id"], created_at=now, paired_at=now, owner="user-1")
        paired.config = DeviceConfig(**settings)
        unpaired = Device(device_id=PAIRING["device_id"], created_at=now)
        unpaired.config = DeviceConfig(**PAIRING["settings"])
        db.session.add_all([paired, unpaired])
        db.session.add(GlowmarktCredentials(device_id=CONFIG["device_id"], username=glowmarkt["username"]))
        db.session.get(GlowmarktCredentials, CONFIG["device_id"]).password = glowmarkt["password"]
        db.session.commit()


def test_the_subscriptions_written_are_the_contracts_example(app, fake_redis):
    _add_example_devices(app)

    with app.test_request_context():
        pages._write_subscriptions_to_redis()

    assert SUBSCRIPTIONS["devices"] == ["config_store.json", "pairing_key.json"]
    assert {key: sorted(members) for key, members in fake_redis.sets.items()} == {
        key: sorted(members) for key, members in SUBSCRIPTIONS["sets"].items()
    }
    assert fake_redis.published == [(SUBSCRIPTIONS["channel"], SUBSCRIPTIONS["message"])]


def test_the_pace_a_provider_writes_is_read_as_a_hint(app):
    # As pi-dashboard's provider runner and the broker's developer provider write it: seconds, with the pace TTL.
    store = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = store
    device_id = CONFIG["device_id"]
    store.set(store_keys.pace(store_keys.DEVELOPER, device_id), 20, ex=store_keys.PACE_TTL_S)
    now = datetime.now(UTC)

    with app.app_context():
        hints = pace._stored_hints([pace.key(store_keys.DEVELOPER, device_id)], now)

    assert [hint.seconds for hint in hints] == [20]
    assert now < hints[0].until <= now + timedelta(seconds=store_keys.PACE_TTL_S)
