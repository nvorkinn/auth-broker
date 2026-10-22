"""Test naming a screen: its stable name at pairing (services/assignments.py), and the friendly name its owner sets.

Which named screens the pool draws is tests/services/test_render_screens.py's.
"""

import pytest

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, RendererAssignment
from broker.services import assignments, pairing
from tests.conftest import USERNAME

DISPLAY = "display-secret-0123456789"


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def _register(client, secret=DISPLAY):
    return client.post(
        "/api/devices/register", json={"secret": secret, "width": 480, "height": 800, "format": "mono-1bpp-packed"}
    ).get_json()["device_id"]


@pytest.fixture
def paired(app, client):
    """Register a screen and pair the signed-in user with it by its token: return its device_id."""
    device_id = _register(client)
    assert client.post("/pair/token", json={"token": pairing.token_for(hash_secret(DISPLAY))}).status_code == 200
    return device_id


def _assignment(app, device_id):
    with app.app_context():
        return db.session.get_one(Device, device_id).assignment


# --- its stable name, at pairing --------------------------------------------------------------------------------------


def test_pairing_names_the_screen_by_its_own_id_and_opens_its_settings(app, client, paired):
    # The stable name the pool lists it under and its metrics are labelled with: never shown.
    assert _assignment(app, paired).slug == paired
    assert client.get("/device").status_code == 200
    # /device/name only redirects to the settings page, where the name is set.
    assert client.get("/device/name").headers["Location"] == f"/device/{paired}"
    assert client.post(f"/device/{paired}/name", data={"slug": "lisa"}).headers["Location"] == f"/device/{paired}"


def test_pairing_by_code_and_the_owner_command_name_the_screen_too(app, client, cli):
    by_code = _register(client, "a-screen-paired-by-a-code-0123")
    with app.app_context():
        code = pairing.issue_code(by_code)
    client.post("/pair", data={"code": code})
    by_command = _register(client, "a-screen-given-an-owner-0123")
    cli("owner", by_command, "user-2")

    assert _assignment(app, by_code).slug == by_code
    assert _assignment(app, by_command).slug == by_command


def test_a_screen_paired_before_automatic_names_gets_one_on_its_settings_page(app, client, paired):
    with app.app_context():
        db.session.delete(db.session.get_one(Device, paired).assignment)
        db.session.commit()

    assert client.get("/device").status_code == 200

    assert _assignment(app, paired).slug == paired


def test_naming_again_changes_nothing(app, paired):
    with app.app_context():
        assert assignments.auto_name(db.session.get_one(Device, paired)) is None
    assert _assignment(app, paired).slug == paired


def test_forgetting_a_device_frees_its_slug(app, client, paired, cli):
    assignment_id = _assignment(app, paired).id

    assert cli("forget", paired).exit_code == 0

    with app.app_context():
        assert db.session.get(RendererAssignment, assignment_id) is None


# --- its friendly name ------------------------------------------------------------------------------------------------


def test_a_new_screen_is_called_screen_and_its_id_until_its_owner_names_it(client, paired):
    assert f">Screen {paired[:6]}</a>" in client.get("/device").data.decode()


def test_the_owner_names_and_renames_the_screen_on_its_settings_page(app, client, paired):
    assert client.post("/device", data={"screen_name": "  Kitchen   wall "}).status_code == 302
    page = client.get("/device").data.decode()
    assert ">Kitchen wall</a>" in page
    assert 'name="screen_name" value="Kitchen wall"' in page

    client.post("/device", data={"screen_name": "Hall"})

    with app.app_context():
        device = db.session.get_one(Device, paired)
        assert device.device_name == "Hall"
        assert device.assignment is not None
        assert device.assignment.slug == paired  # the stable name never moves


def test_a_blank_name_clears_it_and_a_save_without_one_keeps_it(app, client, paired):
    client.post("/device", data={"screen_name": "Hall"})
    client.post("/device", data={"style": "card"})  # a form without the field
    with app.app_context():
        assert db.session.get_one(Device, paired).device_name == "Hall"

    client.post("/device", data={"screen_name": "   "})

    with app.app_context():
        assert db.session.get_one(Device, paired).device_name is None


def test_a_name_too_long_is_refused_and_nothing_is_saved(app, client, paired):
    response = client.post("/device", data={"screen_name": "x" * (assignments.NAME_MAX + 1), "style": "card"})

    assert response.status_code == 400
    assert b"at most 40 characters" in response.data
    with app.app_context():
        assert db.session.get_one(Device, paired).device_name is None


def test_a_screen_named_by_a_slug_before_keeps_it_as_its_name(app, client, paired):
    with app.app_context():
        assignment = db.session.get_one(Device, paired).assignment
        assert assignment is not None
        assignment.slug = f"{USERNAME}-lisa"
        db.session.commit()

    assert f">{USERNAME}-lisa</a>" in client.get("/device").data.decode()


def test_the_screens_name_is_never_logged(client, paired, caplog):
    client.post("/device", data={"screen_name": "Mum's flat in Kennington"})

    with caplog.at_level("INFO"):
        client.post("/api/frame", headers=_bearer(DISPLAY))

    assert f"POST /api/frame device={paired} ({paired}) role=display" in caplog.messages
    assert not any("Kennington" in message for message in caplog.messages)


# --- the username on the page ---------------------------------------------------------------------------


def test_the_username_is_shown_at_the_top_of_the_pages(client, paired):
    for path in ["/device", "/pair"]:
        assert f'<div class="whoami" title="Signed in as {USERNAME}">{USERNAME}</div>' in client.get(path).data.decode()


def test_the_username_is_escaped(client, paired):
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = "<script>alert(1)</script>"

    page = client.get("/pair").data.decode()

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_no_username_shows_nothing(client):
    del client.environ_base["HTTP_X_AUTHENTIK_USERNAME"]
    assert 'class="whoami"' not in client.get("/pair").data.decode()
