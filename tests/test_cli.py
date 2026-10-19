from unittest.mock import patch

from sqlalchemy import func, select

from broker.clients import weather
from broker.db import db
from broker.models import Device, DeviceConfig, GitHubToken, GlowmarktCredentials, SpotifyToken
from broker.services import pairing, renderer_config, weather_place


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


def _shown(app, device_id):
    """Return what the render pool draws the screen from: its config, with its live pairing code if it has one."""
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        return renderer_config.build(config, pairing_code=pairing.live_code(device_id)).model_dump(mode="json")


def _issue(app, device_id):
    with app.app_context():
        return pairing.issue_code(device_id)


def _paired_device(app, client, register_device):
    device_id, secret = register_device()
    client.post("/pair", data={"code": _issue(app, device_id)})
    return device_id, secret


def test_list_shows_each_device_and_whether_it_is_paired(app, client, register_device, cli):
    paired_id, _ = _paired_device(app, client, register_device)
    unpaired_id, _secret = register_device("another-very-long-device-secret")
    code = _issue(app, unpaired_id)

    result = cli("list")

    assert result.exit_code == 0
    out = result.output
    assert f"{paired_id}" in out
    assert "NOT paired" in out
    assert f"{unpaired_id}" in out
    assert code in out


def test_list_with_no_devices(cli):
    result = cli("list")
    assert result.exit_code == 0
    assert "No devices." in result.output


def test_list_shows_device_name_when_set_and_unnamed_otherwise(app, client, register_device, cli):
    named_id, _secret = register_device()
    with app.app_context():
        db.session.get(Device, named_id).device_name = "camilla"  # from before the screens' names were their slugs
        db.session.commit()
    screen = {"secret": "a-screen-with-no-name-yet-at-all", "width": 800, "height": 480}
    unnamed_id = client.post("/api/devices/register", json=screen).get_json()["device_id"]

    result = cli("list")

    assert result.exit_code == 0
    out = result.output
    assert f"camilla ({named_id})" in out
    assert f"unnamed ({unnamed_id})" in out


def test_unpair_brings_the_device_back_to_showing_a_code_but_keeps_its_settings(app, client, register_device, cli):
    device_id, _secret = _paired_device(app, client, register_device)
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location = "London"
        config.clock_format = "12h"
        db.session.commit()
    assert _shown(app, device_id)["pairing_code"] is None

    result = cli("unpair", device_id)

    assert result.exit_code == 0
    config = _shown(app, device_id)
    assert len(config["pairing_code"]) == 6
    assert config["pairing_code"] in result.output
    assert config["weather"]["location"] == "London"
    assert config["clock_format"] == "12h"


def test_unpair_with_config_also_resets_settings_and_linked_accounts(app, client, register_device, cli):
    device_id, _secret = _paired_device(app, client, register_device)
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location, config.tfl_stop_ids = "London", ["940GZZLUKNG"]
        config.clock_format = "12h"
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="r"))
        db.session.add(GlowmarktCredentials(device_id=device_id, username="me"))
        db.session.add(GitHubToken(device_id=device_id, token_encrypted="x"))
        db.session.commit()

    assert cli("unpair", device_id, "--config").exit_code == 0

    config = _shown(app, device_id)
    assert config["pairing_code"] is not None
    assert (config["weather"]["location"], config["tfl"]["stop_ids"]) == ("", [])
    assert config["clock_format"] == "24h"
    assert config["glowmarkt"] == {"enabled": False}
    assert config["notice_board"] == {"postcode": None, "country": None, "style": "airy"}
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(SpotifyToken)) == 0
        assert db.session.scalar(select(func.count()).select_from(GitHubToken)) == 0


def test_forget_deletes_the_device_so_it_must_register_again(app, client, register_device, cli):
    device_id, secret = _paired_device(app, client, register_device)

    assert cli("forget", device_id).exit_code == 0

    assert client.get("/api/frame", headers=_headers(secret)).status_code == 401
    with app.app_context():
        # Its name (the assignment) goes too.
        for table in db.metadata.sorted_tables:
            assert db.session.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_forget_leaves_other_devices_alone(app, client, register_device, cli):
    gone, _ = _paired_device(app, client, register_device)
    kept, kept_secret = register_device("another-very-long-device-secret")

    cli("forget", gone)

    assert client.get("/api/frame", headers=_headers(kept_secret)).status_code == 404  # known, with no frame yet
    with app.app_context():
        assert db.session.get(Device, kept).assignment is not None


def test_code_hands_an_already_paired_device_to_another_user(app, client, register_device, cli):
    device_id, _secret = _paired_device(app, client, register_device)
    with app.app_context():
        db.session.get(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    result = cli("code", device_id)

    assert result.exit_code == 0
    code = result.output.split(":")[0]
    assert _shown(app, device_id)["pairing_code"] == code  # the device shows it too
    new_owner = app.test_client()
    new_owner.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"
    new_owner.post("/pair", data={"code": code})
    assert new_owner.get("/device").status_code == 200
    assert client.get("/device").status_code == 302  # one owner at a time
    assert _shown(app, device_id)["clock_format"] == "12h"
    assert b'name="clock_format" value="12h" checked' in new_owner.get("/device").data


def test_owner_pairs_a_device_with_a_user(app, client, register_device, cli):
    device_id, _ = register_device()

    result = cli("owner", device_id, "user-1")

    assert result.exit_code == 0
    assert client.get("/device").status_code == 200
    assert "owner=user-1" in cli("list").output


def test_unpair_removes_the_owner(app, client, register_device, cli):
    device_id, _ = _paired_device(app, client, register_device)

    cli("unpair", device_id)

    assert client.get("/device").status_code == 302


def test_unknown_device_is_an_error(cli):
    for command in ("code", "unpair", "forget"):
        result = cli(command, "nope")
        assert result.exit_code == 1
        assert "No device nope." in result.stderr


# --- flask weather backfill -----------------------------------------------------------------------------------------

LONDON = {
    "id": 2643743,
    "name": "London",
    "admin1": "England",
    "country": "United Kingdom",
    "country_code": "GB",
    "latitude": 51.50853,
    "longitude": -0.12574,
    "label": "London, England, United Kingdom",
}


def _backfill(app):
    return app.test_cli_runner().invoke(args=["weather", "backfill"])


def _set_locations(app, locations):
    with app.app_context():
        for device_id, location in locations.items():
            db.session.get(DeviceConfig, device_id).weather_location = location
        db.session.commit()


def _search(name, **_):
    return {"london": [LONDON], "nowhere": []}[name.casefold()]


def test_weather_backfill_gives_each_free_text_location_a_place(app, register_device):
    london, _ = register_device()
    nowhere, _ = register_device("another-very-long-device-secret")
    blank, _ = register_device("a-third-very-long-device-secret")
    _set_locations(app, {london: "london", nowhere: "Nowhere", blank: ""})

    with patch("broker.clients.weather.search_places", side_effect=_search) as search:
        result = _backfill(app)

    assert result.exit_code == 0, result.output
    assert f"{london}: 'london' is London, England, United Kingdom (london-gb-2643743)." in result.output
    assert f"{nowhere}: Open-Meteo finds nothing for 'Nowhere'; skipped." in result.output
    assert "1 given a place, 1 not found, 0 not checked." in result.output
    assert sorted(call.args[0] for call in search.call_args_list) == ["Nowhere", "london"]
    with app.app_context():
        config = db.session.get(DeviceConfig, london)
        # What renderers are sent stays as it was.
        assert (config.weather_location, config.weather_place_id, config.weather_country_code) == (
            "london",
            2643743,
            "GB",
        )
        assert (config.weather_lat, config.weather_lon) == (51.50853, -0.12574)
        assert db.session.get(DeviceConfig, nowhere).weather_place_id is None
        assert db.session.get(DeviceConfig, blank).weather_place_id is None


def test_weather_backfill_is_safe_to_run_again(app, register_device):
    device_id, _ = register_device()
    _set_locations(app, {device_id: "London"})
    with patch("broker.clients.weather.search_places", side_effect=_search):
        _backfill(app)

    with patch("broker.clients.weather.search_places", side_effect=_search) as search:
        result = _backfill(app)

    assert result.exit_code == 0
    search.assert_not_called()
    assert "0 given a place, 0 not found, 0 not checked." in result.output


def test_weather_backfill_rewrites_the_subscriptions_by_the_places_it_gave(app, register_device, fake_redis):
    """So the weather provider fetches by the new keys without waiting for someone to save a config."""
    london, _ = register_device()
    nowhere, _ = register_device("another-very-long-device-secret")
    _set_locations(app, {london: "london", nowhere: "Nowhere"})

    with patch("broker.clients.weather.search_places", side_effect=_search):
        result = _backfill(app)

    assert "Rewrote the subscriptions" in result.output
    assert fake_redis.sets["v1:sub:weather"] == {"london-gb-2643743", "nowhere"}
    assert fake_redis.published == [("v1:sub:changed", "")]


def test_weather_backfill_rewrites_the_subscriptions_even_with_nothing_to_backfill(app, register_device, fake_redis):
    """Run after deploying a broker that subscribes by place, it moves the old subscriptions over."""
    device_id, _ = register_device()
    with app.app_context():
        weather_place.store(db.session.get(DeviceConfig, device_id), LONDON)
        db.session.commit()
    fake_redis.sets["v1:sub:weather"] = {"London"}  # as an older broker subscribed it

    with patch("broker.clients.weather.search_places") as search:
        result = _backfill(app)

    search.assert_not_called()
    assert result.exit_code == 0, result.output
    assert fake_redis.sets["v1:sub:weather"] == {"london-gb-2643743"}


def test_weather_backfill_skips_and_reports_what_open_meteo_couldnt_be_asked_about(app, register_device):
    device_id, _ = register_device()
    _set_locations(app, {device_id: "London"})

    with patch("broker.clients.weather.search_places", side_effect=weather.GeocodingUnavailableError):
        result = _backfill(app)

    assert result.exit_code == 1
    assert f"{device_id}: couldn't ask Open-Meteo about 'London'; skipped." in result.output
    assert "run it again later" in result.output
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).weather_place_id is None
