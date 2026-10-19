"""Test naming a screen, which puts it on the render pool under its slug (services/assignments.py).

Covers the /device/name page and the slug's rules. Which named screens the pool draws is
tests/services/test_render_screens.py's.
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


@pytest.fixture
def screen(app, client):
    """Register a screen paired by the signed-in user, with no renderer yet: return its device_id."""
    device_id = client.post("/api/devices/register", json={"secret": DISPLAY, "width": 480, "height": 800}).get_json()[
        "device_id"
    ]
    token = pairing.token_for(hash_secret(DISPLAY))
    assert client.post("/pair/token", json={"token": token}).status_code == 200
    return device_id


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
    assert assignment.slug == f"{USERNAME}-lisa"
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
    other_id = other.post(
        "/api/devices/register", json={"secret": "another-display-0123456789", "width": 480, "height": 800}
    )
    other_id = other_id.get_json()["device_id"]
    token = pairing.token_for(hash_secret("another-display-0123456789"))
    other.post("/pair/token", json={"token": token})

    response = other.post("/device/name", data={"slug": "lisa"})

    assert response.status_code == 400
    assert b"taken" in response.data
    assert _assignment(app, other_id) is None


def test_a_named_screen_is_never_asked_to_name_itself_again(paired_client):
    client, device_id, _ = paired_client
    assert client.get("/device").status_code == 200
    assert client.get("/device/name").headers["Location"] == f"/device/{device_id}"


def test_forgetting_a_device_frees_its_slug(app, client, screen, cli):
    client.post("/device/name", data={"slug": "lisa"})
    assignment_id = _assignment(app, screen).id

    assert cli("forget", screen).exit_code == 0

    with app.app_context():
        assert db.session.get(RendererAssignment, assignment_id) is None


def test_a_slug_taken_between_the_check_and_the_insert_is_refused(app, client, screen, monkeypatch):
    """Check two owners naming their screens the same thing at once.

    Both pass the check, the second insert hits the unique index.
    """
    other = app.test_client()
    other.environ_base["HTTP_X_AUTHENTIK_UID"] = "user-2"
    other.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = USERNAME
    other_id = other.post(
        "/api/devices/register", json={"secret": "another-display-0123456789", "width": 480, "height": 800}
    )
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


def test_naming_a_screen_that_got_its_name_meanwhile_goes_to_the_settings(client, screen, monkeypatch):
    def already_has_one(device, slug, username):
        raise assignments.NotNeededError(device.device_id)

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


def test_a_full_slug_always_fits_the_slug_limit():
    for username in ["a", "x" * 100, "Nikolai V."]:
        suffix = "s" * assignments.max_suffix_length(username)
        slug = assignments.full_slug(username, suffix)
        assert assignments.SLUG_RE.match(slug)
        assert len(slug) <= assignments.SLUG_MAX
        with pytest.raises(assignments.InvalidSlugError):
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
