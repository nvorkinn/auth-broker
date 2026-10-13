"""Naming a screen, which asks the orchestrator for its renderer: the /device/name page, the API behind
it, the orchestrator's endpoints, and the renderer claiming its screen."""

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
    return lambda: app.test_client()


@pytest.fixture
def screen(app, client):
    """A screen that registered and was paired by the signed-in user, which has no renderer yet: its device_id."""
    device_id = client.post("/api/devices/register", json={"secret": DISPLAY}).get_json()["device_id"]
    token = pairing.token_for(hash_secret(DISPLAY))
    assert client.post("/pair/token", json={"token": token}).status_code == 200
    return device_id


def _list(orchestrator):
    response = orchestrator().get("/api/orchestrator/assignments", headers=_bearer(TOKEN))
    assert response.status_code == 200
    return response.get_json()["assignments"]


def _assignment(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id).assignment


# --- naming the screen ------------------------------------------------------------------------------


def test_a_new_screen_is_sent_to_name_itself_first(client, screen):
    response = client.get("/device")

    assert response.status_code == 302
    assert response.headers["Location"] == f"/device/{screen}/name"
    assert b"Name your screen" in client.get(response.headers["Location"]).data
    assert b"Name your screen" in client.get("/device/name").data


def test_naming_the_screen_creates_its_assignment_and_opens_the_settings(app, client, screen):
    response = client.post("/device/name", data={"slug": "  Lisa "})

    assert response.status_code == 302
    assert response.headers["Location"] == f"/device/{screen}"
    assignment = _assignment(app, screen)
    assert (assignment.slug, assignment.status) == (f"{USERNAME}-lisa", "pending")
    assert assignment.claim_token
    assert client.get("/device").status_code == 200
    assert client.get("/device/name").headers["Location"] == f"/device/{screen}"


@pytest.mark.parametrize("slug", ["", "-lisa", "li sa", "Li$a", "x" * 33])
def test_a_bad_slug_is_refused_and_nothing_is_created(app, client, screen, slug):
    response = client.post("/device/name", data={"slug": slug})

    assert response.status_code == 400
    assert _assignment(app, screen) is None


def test_a_slug_is_unique_across_the_whole_broker(app, client, screen):
    client.post("/device/name", data={"slug": "lisa"})
    other = app.test_client()
    other.environ_base["HTTP_X_AUTHENTIK_UID"] = "user-2"
    other.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = USERNAME
    other_id = other.post("/api/devices/register", json={"secret": "another-display-0123456789"})
    other_id = other_id.get_json()["device_id"]
    token = pairing.token_for(hash_secret("another-display-0123456789"))
    other.post("/pair/token", json={"token": token})

    response = other.post("/device/name", data={"slug": "lisa"})

    assert response.status_code == 400
    assert b"taken" in response.data
    assert _assignment(app, other_id) is None


def test_a_device_with_its_own_renderer_is_never_asked_to_name_itself(paired_client):
    client, device_id, _ = paired_client
    assert client.get("/device").status_code == 200
    assert client.get("/device/name").headers["Location"] == f"/device/{device_id}"


def test_the_device_page_says_the_renderer_is_starting(client, screen):
    client.post("/device/name", data={"slug": "lisa"})
    assert b"Starting up" in client.get("/device").data


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
    client.post("/api/devices/register", json={"secret": DISPLAY})
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
    client.post("/device/name", data={"slug": "lisa"})

    [item] = _list(orchestrator)

    assignment = _assignment(app, screen)
    assert item == {
        "id": assignment.id,
        "slug": f"{USERNAME}-lisa",
        "claim_token": assignment.claim_token,
        "status": "pending",
    }


def test_the_renderer_claims_its_screen_with_the_token(app, orchestrator, client, screen):
    client.post("/device/name", data={"slug": "lisa"})
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
    client.post("/device/name", data={"slug": "lisa"})
    token = _list(orchestrator)[0]["claim_token"]
    client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": token})

    for secret, claim in [("second-renderer-0123456789", token), ("third-renderer-0123456789", "nonsense")]:
        response = client.post("/api/renderers/claim", json={"secret": secret, "claim_token": claim})
        assert response.status_code == 403


def test_the_orchestrator_reports_progress(app, orchestrator, client, screen):
    client.post("/device/name", data={"slug": "lisa"})
    assignment_id = _assignment(app, screen).id
    url = f"/api/orchestrator/assignments/{assignment_id}/status"

    ok = orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "provisioned"})
    failed = orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "failed", "detail": "no image"})

    assert (ok.status_code, failed.status_code) == (200, 200)
    assignment = _assignment(app, screen)
    assert (assignment.status, assignment.detail) == ("failed", "no image")
    assert _list(orchestrator)[0]["status"] == "failed"


def test_reports_are_validated(app, orchestrator, client, screen):
    client.post("/device/name", data={"slug": "lisa"})
    url = f"/api/orchestrator/assignments/{_assignment(app, screen).id}/status"

    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "pending"}).status_code == 400
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "failed", "detail": 5}).status_code == 400
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={}).status_code == 400
    missing = "/api/orchestrator/assignments/nope/status"
    assert orchestrator().post(missing, headers=_bearer(TOKEN), json={"status": "failed"}).status_code == 404
    # Nothing is being deleted, so nothing may be reported deleted.
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "deleted"}).status_code == 409


def test_a_screen_unseen_for_a_day_is_suspended_and_returns_as_pending(app, orchestrator, client, screen):
    client.post("/device/name", data={"slug": "lisa"})
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
    client.post("/device/name", data={"slug": "lisa"})
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
    client.post("/device/name", data={"slug": "lisa"})
    client.get("/api/frame", headers=_bearer(DISPLAY))
    with app.app_context():
        assert db.session.get(Device, screen).display_last_seen_at is not None
    assert _list(orchestrator)[0]["status"] == "pending"


def test_forgetting_a_device_has_its_renderer_removed(app, orchestrator, client, screen, cli):
    client.post("/device/name", data={"slug": "lisa"})
    assignment_id = _assignment(app, screen).id

    assert cli("forget", screen).exit_code == 0

    [item] = _list(orchestrator)
    assert (item["id"], item["status"], item["claim_token"]) == (assignment_id, "deleting", None)
    url = f"/api/orchestrator/assignments/{assignment_id}/status"
    assert orchestrator().post(url, headers=_bearer(TOKEN), json={"status": "deleted"}).status_code == 200
    assert _list(orchestrator) == []
    # The slug is free again.
    with app.app_context():
        assert db.session.get(RendererAssignment, assignment_id) is None


def test_a_slug_taken_between_the_check_and_the_insert_is_refused(app, client, screen, monkeypatch):
    """Two owners naming their screens the same thing at once: both pass the check, the second insert hits
    the unique index."""
    other = app.test_client()
    other.environ_base["HTTP_X_AUTHENTIK_UID"] = "user-2"
    other.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = USERNAME
    other_id = other.post("/api/devices/register", json={"secret": "another-display-0123456789"})
    other_id = other_id.get_json()["device_id"]
    other.post("/pair/token", json={"token": pairing.token_for(hash_secret("another-display-0123456789"))})
    assert client.post("/device/name", data={"slug": "lisa"}).status_code == 302
    real_scalar = assignments.db.session.scalar

    def scalar_that_misses_the_slug_check(statement, *args, **kwargs):
        if "renderer_assignments.id" in str(statement):
            return None
        return real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(assignments.db.session, "scalar", scalar_that_misses_the_slug_check)

    response = other.post("/device/name", data={"slug": "lisa"})

    assert response.status_code == 400
    assert b"taken" in response.data
    assert _assignment(app, other_id) is None


def test_naming_a_screen_that_got_its_renderer_meanwhile_goes_to_the_settings(client, screen, monkeypatch):
    def already_has_one(device, slug, username):
        raise assignments.NotNeeded(device.device_id)

    monkeypatch.setattr(assignments, "create", already_has_one)

    response = client.post("/device/name", data={"slug": "lisa"})

    assert (response.status_code, response.headers["Location"]) == (302, f"/device/{screen}")


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
        assert assignments.SLUG_RE.match(slug) and len(slug) <= assignments.SLUG_MAX
        with pytest.raises(assignments.InvalidSlug):
            assignments.full_slug(username, suffix + "s")


def test_the_name_page_shows_the_prefix_the_user_cannot_change(client, screen):
    page = client.get("/device/name").data.decode()

    assert f"{USERNAME}-" in page
    assert f'maxlength="{assignments.max_suffix_length(USERNAME)}"' in page


def test_a_suffix_too_long_for_the_prefix_is_refused(app, client, screen):
    too_long = "s" * (assignments.max_suffix_length(USERNAME) + 1)

    assert client.post("/device/name", data={"slug": too_long}).status_code == 400
    assert _assignment(app, screen) is None


def test_the_slug_uses_the_username_at_the_time_of_naming(app, client, screen):
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = "Nikolai V."

    client.post("/device/name", data={"slug": "kitchen"})

    assert _assignment(app, screen).slug == "nikolai-v-kitchen"


def test_a_user_without_a_username_header_gets_the_fallback_prefix(app, client, screen):
    del client.environ_base["HTTP_X_AUTHENTIK_USERNAME"]

    client.post("/device/name", data={"slug": "kitchen"})

    assert _assignment(app, screen).slug == "user-kitchen"


# --- the username on the page ---------------------------------------------------------------------------


def test_the_username_is_shown_at_the_top_of_the_pages(client, screen):
    for path in ["/device/name", "/pair"]:
        assert f'<div class="whoami" title="Signed in as {USERNAME}">{USERNAME}</div>' in client.get(path).data.decode()


def test_the_username_is_escaped(client, screen):
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = "<script>alert(1)</script>"

    page = client.get("/pair").data.decode()

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_no_username_shows_nothing(client):
    del client.environ_base["HTTP_X_AUTHENTIK_USERNAME"]
    assert 'class="whoami"' not in client.get("/pair").data.decode()
