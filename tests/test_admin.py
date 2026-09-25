import json

from broker import admin
from broker.db import get_db


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


def _config(client, device_id, secret):
    return client.get(f"/api/devices/{device_id}/config", headers=_headers(secret)).get_json()


def _paired_device(client, register_device):
    device_id, secret = register_device()
    client.post("/pair", data={"code": _config(client, device_id, secret)["pairing_code"]})
    return device_id, secret


def test_list_shows_each_device_and_whether_it_is_paired(client, register_device, capsys):
    paired_id, _ = _paired_device(client, register_device)
    unpaired_id, secret = register_device("another-very-long-device-secret")
    code = _config(client, unpaired_id, secret)["pairing_code"]

    assert admin.main(["list"]) == 0

    out = capsys.readouterr().out
    assert f"{paired_id}" in out and "NOT paired" in out
    assert f"{unpaired_id}" in out and code in out


def test_list_with_no_devices(app, capsys):
    assert admin.main(["list"]) == 0
    assert "No devices." in capsys.readouterr().out


def test_list_shows_device_name_when_set_and_unnamed_otherwise(app, client, register_device, capsys):
    named_id, secret = register_device()
    client.get(f"/api/devices/{named_id}/config", headers={**_headers(secret), "X-Device-Name": "camilla"})
    unnamed_id, _ = register_device("another-very-long-device-secret")

    assert admin.main(["list"]) == 0

    out = capsys.readouterr().out
    assert f"camilla ({named_id})" in out
    assert f"unnamed ({unnamed_id})" in out


def test_unpair_brings_the_device_back_to_showing_a_code_but_keeps_its_settings(app, client, register_device):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE device_config SET weather_location = 'London' WHERE device_id = ?", (device_id,))
        db.commit()
    assert _config(client, device_id, secret)["pairing_code"] is None

    assert admin.main(["unpair", device_id]) == 0

    config = _config(client, device_id, secret)
    assert len(config["pairing_code"]) == 6
    assert config["weather"]["location"] == "London"


def test_unpair_with_config_also_resets_settings_and_linked_accounts(app, client, register_device):
    device_id, secret = _paired_device(client, register_device)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE device_config SET weather_location = 'London', tfl_stop_ids = ?, interval = 60 WHERE device_id = ?",
            (json.dumps(["940GZZLUKNG"]), device_id),
        )
        db.execute("INSERT INTO spotify_tokens (device_id, refresh_token) VALUES (?, 'r')", (device_id,))
        db.execute("INSERT INTO glowmarkt_credentials (device_id, username) VALUES (?, 'me')", (device_id,))
        db.commit()

    assert admin.main(["unpair", device_id, "--config"]) == 0

    config = _config(client, device_id, secret)
    assert config["pairing_code"] is not None
    assert (config["interval"], config["weather"]["location"], config["tfl"]["stop_ids"]) == (15, "", [])
    assert config["glowmarkt"] == {"username": None, "password": None}
    assert config["setup_missing"] == ["a weather location", "a postcode", "a bus or tube stop"]
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM spotify_tokens").fetchone()[0] == 0


def test_forget_deletes_the_device_so_it_must_register_again(app, client, register_device):
    device_id, secret = _paired_device(client, register_device)

    assert admin.main(["forget", device_id]) == 0

    assert client.get(f"/api/devices/{device_id}/config", headers=_headers(secret)).status_code == 401
    with app.app_context():
        db = get_db()
        for table in ("devices", *admin.DEVICE_TABLES):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_forget_leaves_other_devices_alone(app, client, register_device):
    gone, _ = _paired_device(client, register_device)
    kept, kept_secret = register_device("another-very-long-device-secret")

    admin.main(["forget", gone])

    assert _config(client, kept, kept_secret)["pairing_code"] is not None


def test_unknown_device_is_an_error(app, capsys):
    assert admin.main(["unpair", "nope"]) == 1
    assert admin.main(["forget", "nope"]) == 1
    assert "No device nope." in capsys.readouterr().err
