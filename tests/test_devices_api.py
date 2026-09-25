from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from broker.db import get_db


def test_register_creates_device(client):
    response = client.post("/api/devices/register", json={"device_secret": "a-very-long-device-secret-value"})
    assert response.status_code == 201
    assert "device_id" in response.get_json()


def test_register_rejects_short_secret(client):
    response = client.post("/api/devices/register", json={"device_secret": "short"})
    assert response.status_code == 400


def test_register_rejects_missing_body(client):
    response = client.post("/api/devices/register")
    assert response.status_code == 400


def test_pairing_code_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code")
    assert response.status_code == 401


def test_pairing_code_rejects_wrong_secret(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_pairing_code_rejects_unknown_device(client, register_device):
    _, secret = register_device()
    response = client.post("/api/devices/does-not-exist/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401


def test_pairing_code_uses_unambiguous_alphabet(client, register_device):
    device_id, secret = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["code"]) == 6
    assert body["expires_in_seconds"] == 600
    for ambiguous_char in "0O1IL":
        assert ambiguous_char not in body["code"]


def test_requesting_new_pairing_code_invalidates_old_one(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}

    first = client.post(f"/api/devices/{device_id}/pairing-code", headers=headers).get_json()["code"]
    client.post(f"/api/devices/{device_id}/pairing-code", headers=headers)

    response = client.post("/pair", data={"code": first})
    assert b"invalid or has expired" in response.data


def test_get_config_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.get(f"/api/devices/{device_id}/config")
    assert response.status_code == 401


def test_get_config_returns_defaults_and_shared_keys(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "interval": 15,
        "weather": {"api_key": "test-weather-key", "location": ""},
        "notice_board": {"postcode": None},
        "tfl": {"app_key": "test-tfl-key", "stop_ids": []},
        "spotify": {"enabled": False},
        "glowmarkt": {"username": None, "password": None},
        "pairing_code": body["pairing_code"],
        "setup_missing": ["a weather location", "a postcode", "a bus or tube stop"],
    }


def _get_pairing_code(client, device_id, secret):
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    return response.get_json()["pairing_code"]


def test_get_config_returns_active_pairing_code(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_persists_until_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code
    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_cleared_once_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _get_pairing_code(client, device_id, secret) is None


def test_get_config_replaces_an_expired_code_for_an_unpaired_device(app, client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE pairing_codes SET expires_at = ? WHERE device_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), device_id),
        )
        db.commit()

    fresh = _get_pairing_code(client, device_id, secret)
    assert fresh is not None
    assert fresh != code


def test_get_config_pairing_code_is_the_latest_one(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.post(f"/api/devices/{device_id}/pairing-code", headers=headers)
    second = client.post(f"/api/devices/{device_id}/pairing-code", headers=headers).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == second


def test_get_config_pairing_code_not_leaked_to_other_devices(client, register_device):
    device_id, secret = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})

    assert _get_pairing_code(client, other_id, other_secret) != _get_pairing_code(client, device_id, secret)


def test_get_config_stores_device_name_from_header(app, client, register_device):
    device_id, secret = register_device()
    client.get(
        f"/api/devices/{device_id}/config",
        headers={"Authorization": f"Bearer {secret}", "X-Device-Name": "camilla"},
    )

    with app.app_context():
        row = get_db().execute("SELECT device_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    assert row["device_name"] == "camilla"


def test_get_config_without_device_name_header_leaves_it_unset(app, client, register_device):
    device_id, secret = register_device()
    client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})

    with app.app_context():
        row = get_db().execute("SELECT device_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    assert row["device_name"] is None


def test_get_config_device_name_self_heals_on_rename(app, client, register_device):
    """A renamed Pi's next poll updates the stored name -- no re-registration needed."""
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "old-name"})

    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "new-name"})

    with app.app_context():
        row = get_db().execute("SELECT device_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    assert row["device_name"] == "new-name"


def test_get_config_blank_device_name_header_does_not_overwrite(app, client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "camilla"})

    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "  "})

    with app.app_context():
        row = get_db().execute("SELECT device_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    assert row["device_name"] == "camilla"


def test_get_config_returns_decrypted_glowmarkt_credentials(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"})

    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["glowmarkt"] == {"username": "someone@example.com", "password": "hunter2"}


def test_now_playing_returns_null_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_returns_null_when_enabled_but_not_linked(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_proxies_spotify_module(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    track = {
        "song": "A Song",
        "artist": "An Artist",
        "album": "An Album",
        "album_image": "http://x",
        "is_playing": True,
    }
    with patch("broker.devices_api.spotify_module.get_current_track", return_value=track) as mocked:
        response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == track


def test_now_playing_unknown_device_rejected(client, register_device):
    _, secret = register_device()
    response = client.get("/api/devices/does-not-exist/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401


def test_queue_requires_auth(client, register_device):
    device_id, _ = register_device()
    assert client.get(f"/api/devices/{device_id}/queue").status_code == 401


def test_queue_returns_empty_list_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == []


def test_queue_returns_empty_list_when_enabled_but_not_linked(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == []


def test_queue_proxies_spotify_module(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    queue = [{"song": "A Song", "artist": "An Artist", "album": "An Album", "album_image": "http://x"}]
    with patch("broker.devices_api.spotify_module.get_queue", return_value=queue) as mocked:
        response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == queue


def test_spotify_view_is_not_called_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()

    with (
        patch("broker.devices_api.spotify_module.get_current_track") as now_playing,
        patch("broker.devices_api.spotify_module.get_queue") as queue,
    ):
        client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
        client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})

    now_playing.assert_not_called()
    queue.assert_not_called()


def test_spotify_disabled_check_runs_after_auth(client, register_device):
    """A caller without the device secret must get a 401, not the disabled default,
    otherwise anyone could probe which devices have Spotify switched on."""
    device_id, _ = register_device()

    for path in ("now-playing", "queue", "top/tracks"):
        assert client.get(f"/api/devices/{device_id}/{path}").status_code == 401
        wrong = client.get(f"/api/devices/{device_id}/{path}", headers={"Authorization": "Bearer wrong-secret"})
        assert wrong.status_code == 401


def test_spotify_endpoints_return_404_when_device_has_no_config_row(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM device_config WHERE device_id = ?", (device_id,))
        db.commit()

    for path in ("now-playing", "queue", "top/tracks"):
        response = client.get(f"/api/devices/{device_id}/{path}", headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 404


def test_top_returns_null_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/top/tracks", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_top_proxies_spotify_module_with_query_params(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    top_items = {"items": [{"name": "A Song"}]}
    with patch("broker.devices_api.spotify_module.get_users_top_items", return_value=top_items) as mocked:
        response = client.get(
            f"/api/devices/{device_id}/top/tracks?limit=5&time_range=short_term",
            headers={"Authorization": f"Bearer {secret}"},
        )

    mocked.assert_called_once_with(device_id, "tracks", {"limit": "5", "time_range": "short_term"})
    assert response.get_json() == top_items


def test_top_passes_empty_params_when_no_query_string(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    with patch("broker.devices_api.spotify_module.get_users_top_items", return_value=None) as mocked:
        response = client.get(f"/api/devices/{device_id}/top/artists", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id, "artists", {})
    assert response.status_code == 200
    assert response.get_json() is None


def test_get_config_issues_a_code_to_an_unpaired_device_and_keeps_it_stable(client, register_device):
    device_id, secret = register_device()

    code = _get_pairing_code(client, device_id, secret)

    assert len(code) == 6
    assert not set(code) & set("0O1IL")
    assert _get_pairing_code(client, device_id, secret) == code


def test_a_paired_device_gets_no_code_and_none_is_issued_again(app, client, register_device):
    device_id, secret = register_device()
    code = _get_pairing_code(client, device_id, secret)
    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _get_pairing_code(client, device_id, secret) is None
    assert _get_pairing_code(client, device_id, secret) is None
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT paired_at FROM devices WHERE device_id = ?", (device_id,)).fetchone()["paired_at"]
        assert db.execute("SELECT COUNT(*) FROM pairing_codes WHERE device_id = ?", (device_id,)).fetchone()[0] == 0


def test_a_failed_pairing_attempt_does_not_mark_the_device_paired(app, client, register_device):
    device_id, secret = register_device()
    _get_pairing_code(client, device_id, secret)

    client.post("/pair", data={"code": "WRONG1"})

    with app.app_context():
        row = get_db().execute("SELECT paired_at FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        assert row["paired_at"] is None


def test_a_forced_code_for_a_paired_device_is_shown_until_redeemed(client, register_device):
    device_id, secret = register_device()
    client.post("/pair", data={"code": _get_pairing_code(client, device_id, secret)})
    forced = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == forced

    client.post("/pair", data={"code": forced})
    assert _get_pairing_code(client, device_id, secret) is None


def _setup_missing(client, device_id, secret):
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    return response.get_json()["setup_missing"]


def test_setup_missing_lists_what_is_still_needed(app, client, register_device):
    device_id, secret = register_device()
    assert _setup_missing(client, device_id, secret) == ["a weather location", "a postcode", "a bus or tube stop"]

    def configure(**columns):
        with app.app_context():
            db = get_db()
            for column, value in columns.items():
                db.execute(f"UPDATE device_config SET {column} = ? WHERE device_id = ?", (value, device_id))
            db.commit()

    configure(weather_location="London")
    assert _setup_missing(client, device_id, secret) == ["a postcode", "a bus or tube stop"]

    configure(postcode="SW1A 1AA")
    assert _setup_missing(client, device_id, secret) == ["a bus or tube stop"]

    configure(weather_location="", tfl_stop_ids='["940GZZLUKNG"]')
    assert _setup_missing(client, device_id, secret) == ["a weather location"]

    configure(weather_location="London", postcode="")
    assert _setup_missing(client, device_id, secret) == ["a postcode"]

    configure(postcode="SW1A 1AA")
    assert _setup_missing(client, device_id, secret) == []


def test_get_config_returns_the_saved_postcode(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db = get_db()
        db.execute("UPDATE device_config SET postcode = 'SW1A 1AA' WHERE device_id = ?", (device_id,))
        db.commit()

    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["notice_board"] == {"postcode": "SW1A 1AA"}


def test_a_blank_weather_location_still_counts_as_missing(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db = get_db()
        db.execute("UPDATE device_config SET weather_location = '   ' WHERE device_id = ?", (device_id,))
        db.commit()

    assert "a weather location" in _setup_missing(client, device_id, secret)
