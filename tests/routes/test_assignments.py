"""Test naming a screen: automatically at pairing, and by its owner on its settings page.

Also the API that named a screen by a slug, the orchestrator's endpoints and the renderer claiming its screen, on a
screen paired before screens were named automatically (`screen`), until those go with the per-screen renderers.
"""

from datetime import UTC, datetime, timedelta

import pytest

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, RendererAssignment
from broker.services import assignments, pairing
from tests.conftest import USERNAME

DISPLAY = "display-secret-0123456789"
RENDERER = "renderer-secret-0123456789"
TOKEN = "orchestrator-token-0123456789"


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def orchestrator(app, client):
    app.config["ORCHESTRATOR_TOKEN"] = TOKEN
    return app.test_client


@pytest.fixture
def paired(app, client):
    """Register a screen and pair the signed-in user with it by its token: return its device_id."""
    device_id = client.post("/api/devices/register", json={"secret": DISPLAY, "width": 480, "height": 800}).get_json()[
        "device_id"
    ]
    token = pairing.token_for(hash_secret(DISPLAY))
    assert client.post("/pair/token", json={"token": token}).status_code == 200
    return device_id


@pytest.fixture
def screen(app, paired):
    """Return a paired screen with no name yet, as one paired before screens were named automatically."""
    with app.app_context():
        db.session.delete(db.session.get(Device, paired).assignment)
        db.session.commit()
    return paired


def _name(client, device_id, slug):
    """Name the screen by a slug through the API, as the owner's naming page did."""
    return client.post(f"/api/displays/{device_id}/renderer", json={"slug": slug})


def _list(orchestrator):
    response = orchestrator().get("/api/orchestrator/assignments", headers=_bearer(TOKEN))
    assert response.status_code == 200
    return response.get_json()["assignments"]


def _assignment(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id).assignment


# --- naming the screen ------------------------------------------------------------------------------


def test_pairing_names_the_screen_by_its_own_id_and_opens_its_settings(app, client, paired):
    assignment = _assignment(app, paired)
    # The stable name the pool lists it under and its metrics are labelled with: never shown.
    assert assignment.slug == paired
    assert client.get("/device").status_code == 200
    assert client.get("/device/name").headers["Location"] == f"/device/{paired}"
    assert client.post(f"/device/{paired}/name", data={"slug": "lisa"}).headers["Location"] == f"/device/{paired}"


def _register(client, secret):
    return client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800}).get_json()[
        "device_id"
    ]


def test_pairing_by_code_and_the_owner_command_name_the_screen_too(app, client, cli):
    by_code = _register(client, "a-screen-paired-by-a-code-0123")
    with app.app_context():
        code = pairing.issue_code(by_code)
    client.post("/pair", data={"code": code})
    by_command = _register(client, "a-screen-given-an-owner-0123")
    cli("owner", by_command, "user-2")

    assert _assignment(app, by_code).slug == by_code
    assert _assignment(app, by_command).slug == by_command


def test_a_screen_paired_before_automatic_names_gets_one_on_its_settings_page(app, client, screen):
    assert _assignment(app, screen) is None

    assert client.get("/device").status_code == 200

    assert _assignment(app, screen).slug == screen


def test_a_new_screen_is_called_screen_and_its_id_until_its_owner_names_it(client, paired):
    assert f">Screen {paired[:6]}</a>" in client.get("/device").data.decode()


def test_the_owner_names_and_renames_the_screen_on_its_settings_page(app, client, paired):
    assert client.post("/device", data={"screen_name": "  Kitchen   wall "}).status_code == 302
    assert ">Kitchen wall</a>" in client.get("/device").data.decode()
    assert 'name="screen_name" value="Kitchen wall"' in client.get("/device").data.decode()

    client.post("/device", data={"screen_name": "Hall"})

    with app.app_context():
        device = db.session.get(Device, paired)
        assert device.device_name == "Hall"
        assert device.assignment.slug == paired  # the stable name never moves


def test_a_blank_name_clears_it_and_a_save_without_one_keeps_it(app, client, paired):
    client.post("/device", data={"screen_name": "Hall"})
    client.post("/device", data={"style": "card"})  # a form without the field
    with app.app_context():
        assert db.session.get(Device, paired).device_name == "Hall"

    client.post("/device", data={"screen_name": "   "})

    with app.app_context():
        assert db.session.get(Device, paired).device_name is None


def test_a_name_too_long_is_refused_and_nothing_is_saved(app, client, paired):
    response = client.post("/device", data={"screen_name": "x" * (assignments.NAME_MAX + 1), "style": "card"})

    assert response.status_code == 400
    assert b"at most 40 characters" in response.data
    with app.app_context():
        assert db.session.get(Device, paired).device_name is None


def test_a_screen_named_by_a_slug_before_keeps_it_as_its_name(app, client, screen):
    assert _name(client, screen, "lisa").status_code == 201

    assert f">{USERNAME}-lisa</a>" in client.get("/device").data.decode()


def test_the_screens_name_is_never_logged(app, client, paired, caplog):
    client.post("/device", data={"screen_name": "Mum's flat in Kennington"})

    with caplog.at_level("INFO"):
        client.get("/api/frame", headers=_bearer(DISPLAY))

    assert f"GET /api/frame device={paired} ({paired}) role=display" in caplog.messages
    assert not any("Kennington" in message for message in caplog.messages)


def test_the_api_names_the_screen(app, client, screen):
    response = client.post(f"/api/displays/{screen}/renderer", json={"slug": "lisa"})

    assert response.status_code == 201
    assert response.get_json() == {"id": _assignment(app, screen).id, "slug": f"{USERNAME}-lisa", "status": "pending"}


def test_the_api_refuses_a_bad_or_taken_slug_and_a_second_assignment(client, screen):
    assert client.post(f"/api/displays/{screen}/renderer", json={"slug": "Bad Slug"}).status_code == 400
    assert client.post(f"/api/displays/{screen}/renderer", json={"slug": ["x"]}).status_code == 400
    assert client.post(f"/api/displays/{screen}/renderer", json={"slug": "lisa"}).status_code == 201
    assert client.post(f"/api/displays/{screen}/renderer", json={"slug": "other"}).status_code == 409


def test_the_api_is_only_for_the_screens_owner(app, anonymous_client, client, screen):
    stranger = app.test_client()
    stranger.environ_base["HTTP_X_AUTHENTIK_UID"] = "user-2"

    assert stranger.post(f"/api/displays/{screen}/renderer", json={"slug": "lisa"}).status_code == 404
    assert client.post("/api/displays/nope/renderer", json={"slug": "lisa"}).status_code == 404
    assert anonymous_client.post(f"/api/displays/{screen}/renderer", json={"slug": "lisa"}).status_code == 401
    assert _assignment(app, screen) is None


def test_a_screen_can_show_a_pairing_code_before_its_renderer_exists(client):
    client.post("/api/devices/register", json={"secret": DISPLAY, "width": 480, "height": 800})
    response = client.post("/api/devices/pairing-code", headers=_bearer(DISPLAY))
    assert response.status_code == 200
    assert len(response.get_json()["code"]) == 6


# --- the orchestrator ---------------------------------------------------------------------------------


def test_the_orchestrator_endpoints_are_off_without_a_token(client):
    assert client.get("/api/orchestrator/assignments").status_code == 404


def test_the_orchestrator_needs_its_own_token(orchestrator, client, screen):
    assert orchestrator().get("/api/orchestrator/assignments").status_code == 401
    assert orchestrator().get("/api/orchestrator/assignments", headers=_bearer("wrong")).status_code == 401
    assert orchestrator().get("/api/orchestrator/assignments", headers=_bearer(DISPLAY)).status_code == 401
    assert orchestrator().post("/api/orchestrator/assignments/x/status", json={}).status_code == 401


def test_the_orchestrator_lists_a_pending_assignment_with_its_claim_token(app, orchestrator, client, screen):
    _name(client, screen, "lisa")

    [item] = _list(orchestrator)

    assignment = _assignment(app, screen)
    assert item == {
        "id": assignment.id,
        "slug": f"{USERNAME}-lisa",
        "claim_token": assignment.claim_token,
        "status": "pending",
        "device_id": screen,
    }


def test_the_renderer_claims_its_screen_with_the_token(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    [item] = _list(orchestrator)

    response = client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": item["claim_token"]})

    assert (response.status_code, response.get_json()) == (201, {"device_id": screen})
    with app.app_context():
        assert db.session.get(Device, screen).renderer_secret_hash == hash_secret(RENDERER)
    assert _list(orchestrator)[0]["claim_token"] is None
    # Its own secret is its identity from here on, so a repeat doesn't need the token.
    assert client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": "x"}).status_code == 200
    assert client.get("/api/config", headers=_bearer(RENDERER)).get_json()["device_id"] == screen
    assert assignments.needs_renderer(_device(app, screen)) is False


def _device(app, device_id):
    with app.app_context():
        device = db.session.get(Device, device_id)
        device.assignment  # noqa: B018 -- loaded before the session goes
        return device


def test_a_spent_or_unknown_claim_token_is_refused(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    token = _list(orchestrator)[0]["claim_token"]
    client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": token})

    for secret, claim in [("second-renderer-0123456789", token), ("third-renderer-0123456789", "nonsense")]:
        response = client.post("/api/renderers/claim", json={"secret": secret, "claim_token": claim})
        assert response.status_code == 403


def test_the_orchestrator_reports_progress(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    assignment_id = _assignment(app, screen).id
    url = f"/api/orchestrator/assignments/{assignment_id}/status"

    ok = orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "provisioned"})
    failed = orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "failed", "detail": "no image"})

    assert (ok.status_code, failed.status_code) == (200, 200)
    assignment = _assignment(app, screen)
    assert (assignment.status, assignment.detail) == ("failed", "no image")
    assert _list(orchestrator)[0]["status"] == "failed"


def test_reports_are_validated(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    url = f"/api/orchestrator/assignments/{_assignment(app, screen).id}/status"

    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "pending"}).status_code == 400
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "failed", "detail": 5}).status_code == 400
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={}).status_code == 400
    missing = "/api/orchestrator/assignments/nope/status"
    assert orchestrator().post(missing, headers=_bearer(TOKEN), json={"status": "failed"}).status_code == 404
    # Nothing is being deleted, so nothing may be reported deleted.
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "deleted"}).status_code == 409


def test_a_screen_unseen_for_a_day_is_suspended_and_returns_as_pending(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    url = f"/api/orchestrator/assignments/{_assignment(app, screen).id}/status"
    orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "provisioned"})
    long_ago = datetime.now(UTC) - assignments.SUSPEND_AFTER - timedelta(minutes=1)
    with app.app_context():
        db.session.get(Device, screen).display_last_seen_at = long_ago
        db.session.get(Device, screen).created_at = long_ago
        db.session.get(RendererAssignment, _assignment(app, screen).id).created_at = long_ago
        db.session.commit()

    assert _list(orchestrator)[0]["status"] == "suspended"

    assert client.get("/api/frame", headers=_bearer(DISPLAY)).status_code == 404  # the screen is back
    assert _list(orchestrator)[0]["status"] == "pending"


def test_a_late_report_does_not_undo_a_suspension(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    assignment_id = _assignment(app, screen).id
    with app.app_context():
        db.session.get(RendererAssignment, assignment_id).status = "suspended"
        db.session.commit()

    response = orchestrator().post(
        f"/api/orchestrator/assignments/{assignment_id}/status", headers=_bearer(TOKEN), json={"status": "provisioned"}
    )

    assert response.status_code == 200
    assert _assignment(app, screen).status == "suspended"


def test_a_screen_that_is_seen_regularly_is_never_suspended(app, orchestrator, client, screen):
    _name(client, screen, "lisa")
    client.get("/api/frame", headers=_bearer(DISPLAY))
    with app.app_context():
        assert db.session.get(Device, screen).display_last_seen_at is not None
    assert _list(orchestrator)[0]["status"] == "pending"


def test_forgetting_a_device_has_its_renderer_removed(app, orchestrator, client, screen, cli):
    _name(client, screen, "lisa")
    assignment_id = _assignment(app, screen).id

    assert cli("forget", screen).exit_code == 0

    [item] = _list(orchestrator)
    assert (item["id"], item["status"], item["claim_token"]) == (assignment_id, "deleting", None)
    # The device is gone, so there's no screen to hand to the render pool.
    assert item["device_id"] is None
    url = f"/api/orchestrator/assignments/{assignment_id}/status"
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "deleted"}).status_code == 200
    assert _list(orchestrator) == []
    # The slug is free again.
    with app.app_context():
        assert db.session.get(RendererAssignment, assignment_id) is None


# --- the username prefix ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "prefix"),
    [
        ("nvorkinn", "nvorkinn"),
        ("Nikolai V.", "nikolai-v"),
        ("n.vorkinn@gmail.com", "n-vorkinn-gmail-com"[: assignments.PREFIX_MAX].rstrip("-")),
        ("--A__", "a"),
        ("x" * 40, "x" * assignments.PREFIX_MAX),
        ("!!!", "user"),
        ("", "user"),
        (None, "user"),
    ],
)
def test_the_prefix_is_the_username_made_safe_and_cut_to_fit(username, prefix):
    assert assignments.slug_prefix(username) == prefix


def test_a_full_slug_always_fits_the_orchestrators_limit():
    for username in ["a", "x" * 100, "Nikolai V."]:
        suffix = "s" * assignments.max_suffix_length(username)
        slug = assignments.full_slug(username, suffix)
        assert assignments.SLUG_RE.match(slug)
        assert len(slug) <= assignments.SLUG_MAX
        with pytest.raises(assignments.InvalidSlugError):
            assignments.full_slug(username, suffix + "s")


# --- the username on the page ---------------------------------------------------------------------------


def test_the_username_is_shown_at_the_top_of_the_pages(client, screen):
    for path in ["/device", "/pair"]:
        assert f'<div class="whoami" title="Signed in as {USERNAME}">{USERNAME}</div>' in client.get(path).data.decode()


def test_the_username_is_escaped(client, screen):
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = "<script>alert(1)</script>"

    page = client.get("/pair").data.decode()

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_no_username_shows_nothing(client):
    del client.environ_base["HTTP_X_AUTHENTIK_USERNAME"]
    assert 'class="whoami"' not in client.get("/pair").data.decode()
