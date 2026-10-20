"""The config site's Apple Music: connecting a device (broker/routes/apple_music_link.py) and switching it on.

What the music provider fetches with the token is in tests/services/test_music.py and
tests/providers/test_music_provider.py.
"""

from broker.db import db
from broker.models import AppleMusicToken, DeviceConfig


def test_developer_token_404s_when_not_set_up(paired_client):
    client, _device_id, _ = paired_client

    assert client.get("/device/apple-music/developer-token").status_code == 404


def test_developer_token_for_musickit_js(paired_client, musickit):
    client, _device_id, _ = paired_client

    body = client.get("/device/apple-music/developer-token").get_json()

    assert body["app_name"] == "Countdown"
    assert body["developer_token"].count(".") == 2


def test_connect_stores_the_token_encrypted_and_disconnect_forgets_it_and_the_plays(app, paired_client, musickit):
    client, device_id, _ = paired_client

    assert client.post("/device/apple-music/connect", data={"music_user_token": " mut "}).status_code == 302
    with app.app_context():
        stored = db.session.get(AppleMusicToken, device_id)
        assert stored.token_encrypted != "mut"
        assert stored.token == "mut"
    assert b"Connected" in client.get("/device").data

    client.post("/device/apple-music/disconnect")
    with app.app_context():
        assert db.session.get(AppleMusicToken, device_id) is None


def test_connect_needs_a_token(paired_client):
    client, _device_id, _ = paired_client

    assert client.post("/device/apple-music/connect", data={}).status_code == 400


def test_another_users_device_404s(client, register_device):
    device_id, _ = register_device()

    assert client.post(f"/device/{device_id}/apple-music/connect", data={"music_user_token": "x"}).status_code == 404


def test_the_config_page_switches_both_services_on_and_sends_both(app, paired_client, musickit, config_of):
    client, device_id, _secret = paired_client

    page = client.get("/device").data
    assert b'name="apple_music_enabled"' in page
    client.post("/device", data={"interval": "20", "spotify_enabled": "on", "apple_music_enabled": "on"})

    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert (config.spotify_enabled, config.apple_music_enabled) == (True, True)
    config = config_of(device_id)
    assert config["spotify"]["enabled"]
    assert config["apple_music"] == {"enabled": True}


def test_the_config_page_hides_apple_music_when_not_set_up(paired_client):
    client, _device_id, _ = paired_client

    assert b'name="apple_music_enabled"' not in client.get("/device").data
