from sqlalchemy import func, select

from broker.db import db
from broker.models import DeviceConfig, GlowmarktCredentials, SpotifyToken


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
    assert f"{paired_id}" in out and "NOT paired" in out
    assert f"{unpaired_id}" in out and code in out


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
        db.session.get(DeviceConfig, device_id).weather_location = "London"
        db.session.commit()
    assert _config(client, device_id, secret)["pairing_code"] is None

    assert cli("unpair", device_id).exit_code == 0

    config = _config(client, device_id, secret)
    assert len(config["pairing_code"]) == 6
    assert config["weather"]["location"] == "London"


def test_unpair_with_config_also_resets_settings_and_linked_accounts(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location, config.tfl_stop_ids, config.interval = "London", ["940GZZLUKNG"], 60
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="r"))
        db.session.add(GlowmarktCredentials(device_id=device_id, username="me"))
        db.session.commit()

    assert cli("unpair", device_id, "--config").exit_code == 0

    config = _config(client, device_id, secret)
    assert config["pairing_code"] is not None
    assert (config["interval"], config["weather"]["location"], config["tfl"]["stop_ids"]) == (15, "", [])
    assert config["glowmarkt"] == {"username": None, "password": None}
    assert config["setup_missing"] == ["a weather location", "a postcode", "a bus or tube stop"]
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(SpotifyToken)) == 0


def test_forget_deletes_the_device_so_it_must_register_again(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)

    assert cli("forget", device_id).exit_code == 0

    assert client.get("/api/config", headers=_headers(secret)).status_code == 401
    with app.app_context():
        for table in db.metadata.sorted_tables:
            assert db.session.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_forget_leaves_other_devices_alone(app, client, register_device, cli):
    gone, _ = _paired_device(client, register_device)
    kept, kept_secret = register_device("another-very-long-device-secret")

    cli("forget", gone)

    assert _config(client, kept, kept_secret)["pairing_code"] is not None


def test_code_links_a_new_browser_to_an_already_paired_device(app, client, register_device, cli):
    device_id, secret = _paired_device(client, register_device)

    result = cli("code", device_id)

    assert result.exit_code == 0
    code = result.output.split(":")[0]
    assert _config(client, device_id, secret)["pairing_code"] == code  # the device shows it too
    new_browser = app.test_client()
    new_browser.post("/pair", data={"code": code})
    assert new_browser.get("/device").status_code == 200
    assert client.get("/device").status_code == 200  # the first browser keeps its access


def test_code_replaces_the_previous_code(app, client, register_device, cli):
    device_id, _ = _paired_device(client, register_device)
    first = cli("code", device_id).output.split(":")[0]
    cli("code", device_id)

    new_browser = app.test_client()
    new_browser.post("/pair", data={"code": first})
    assert new_browser.get("/device").status_code == 302


def test_unknown_device_is_an_error(cli):
    for command in ("code", "unpair", "forget"):
        result = cli(command, "nope")
        assert result.exit_code == 1
        assert "No device nope." in result.stderr
