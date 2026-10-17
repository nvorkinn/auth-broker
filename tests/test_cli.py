from unittest.mock import patch

from sqlalchemy import func, select

from broker.clients import weather
from broker.db import db
from broker.models import DeviceConfig, GitHubToken, GlowmarktCredentials, SpotifyToken


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


def _config(client, device_id, secret):
    return client.get("/api/config", headers=_headers(secret)).get_json()


def _paired_device(client, register_device):
    device_id, secret = register_device()
    client.post("/pair", data={"code": _config(client, device_id, secret)["pairing_code"]})
    return device_id, secret


def test_list_shows_each_device_and_whether_it_is_paired(client, register_device, cli):
    paired_id, _ = _paired_device(client, register_device)
    unpaired_id, secret = register_device("another-very-long-device-secret")
    code = _config(client, unpaired_id, secret)["pairing_code"]

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


def test_list_shows_device_name_when_set_and_unnamed_otherwise(client, register_device, cli):
    named_id, secret = register_device()
    client.get("/api/config", headers={**_headers(secret), "X-Device-Name": "camilla"})
    unnamed_id, _ = register_device("another-very-long-device-secret")

    result = cli("list")

    assert result.exit_code == 0
    out = result.output
    assert f"camilla ({named_id})" in out
    assert f"unnamed ({unnamed_id})" in out


def test_unpair_brings_the_device_back_to_showing_a_code_but_keeps_its_settings(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location = "London"
        config.clock_format = "12h"
        db.session.commit()
    assert _config(client, device_id, secret)["pairing_code"] is None

    assert cli("unpair", device_id).exit_code == 0

    config = _config(client, device_id, secret)
    assert len(config["pairing_code"]) == 6
    assert config["weather"]["location"] == "London"
    assert config["clock_format"] == "12h"


def test_unpair_with_config_also_resets_settings_and_linked_accounts(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location, config.tfl_stop_ids, config.interval = "London", ["940GZZLUKNG"], 60
        config.clock_format = "12h"
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="r"))
        db.session.add(GlowmarktCredentials(device_id=device_id, username="me"))
        db.session.add(GitHubToken(device_id=device_id, token_encrypted="x"))
        db.session.commit()

    assert cli("unpair", device_id, "--config").exit_code == 0

    config = _config(client, device_id, secret)
    assert config["pairing_code"] is not None
    assert (config["interval"], config["weather"]["location"], config["tfl"]["stop_ids"]) == (20, "", [])
    assert config["clock_format"] == "24h"
    assert config["glowmarkt"] == {"username": None, "password": None}
    assert config["notice_board"] == {"postcode": None, "country": None, "style": "airy"}
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(SpotifyToken)) == 0
        assert db.session.scalar(select(func.count()).select_from(GitHubToken)) == 0


def test_forget_deletes_the_device_so_it_must_register_again(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)

    assert cli("forget", device_id).exit_code == 0

    assert client.get("/api/config", headers=_headers(secret)).status_code == 401
    with app.app_context():
        # The assignment outlives the device, so the orchestrator can remove the renderer.
        for table in db.metadata.sorted_tables:
            expected = 1 if table.name == "renderer_assignments" else 0
            assert db.session.scalar(select(func.count()).select_from(table)) == expected, table.name


def test_forget_leaves_other_devices_alone(app, client, register_device, cli):
    gone, _ = _paired_device(client, register_device)
    kept, kept_secret = register_device("another-very-long-device-secret")

    cli("forget", gone)

    assert _config(client, kept, kept_secret)["pairing_code"] is not None


def test_code_hands_an_already_paired_device_to_another_user(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        db.session.get(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    result = cli("code", device_id)

    assert result.exit_code == 0
    code = result.output.split(":")[0]
    assert _config(client, device_id, secret)["pairing_code"] == code  # the device shows it too
    new_owner = app.test_client()
    new_owner.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"
    new_owner.post("/pair", data={"code": code})
    assert new_owner.get("/device").status_code == 200
    assert client.get("/device").status_code == 302  # one owner at a time
    assert _config(client, device_id, secret)["clock_format"] == "12h"
    assert b'name="clock_format" value="12h" checked' in new_owner.get("/device").data


def test_owner_pairs_a_device_with_a_user(app, client, register_device, cli):
    device_id, _ = register_device()

    result = cli("owner", device_id, "user-1")

    assert result.exit_code == 0
    assert client.get("/device").status_code == 200
    assert "owner=user-1" in cli("list").output


def test_unpair_removes_the_owner(client, register_device, cli):
    device_id, _ = _paired_device(client, register_device)

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
