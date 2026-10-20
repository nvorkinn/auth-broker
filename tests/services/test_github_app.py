import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import fakeredis
import pytest
import responses

from broker.db import db
from broker.models import GitHubAppToken
from broker.services import github_app

GRANT = {
    "access_token": "ghu_new",
    "expires_in": 28800,
    "refresh_token": "ghr_new",
    "refresh_token_expires_in": 15897600,
}


@pytest.fixture
def ctx(app):
    app.config.update(
        GITHUB_APP_CLIENT_ID="Iv1.client", GITHUB_APP_CLIENT_SECRET="shh", GITHUB_APP_WEBHOOK_SECRET="hook"
    )
    app.extensions["redis"] = fakeredis.FakeRedis(decode_responses=True)
    with app.app_context():
        yield app


def stored(expires_in: timedelta, refresh_in: timedelta = timedelta(days=30)) -> None:
    now = datetime.now(UTC)
    row = GitHubAppToken(user_id="user-1", expires_at=now + expires_in, refresh_expires_at=now + refresh_in)
    row.access_token = "ghu_old"
    row.refresh_token = "ghr_old"
    db.session.add(row)
    db.session.commit()


def test_sign_in_goes_to_github_with_the_client_and_state(ctx):
    url = urlsplit(github_app.authorize_url("st4te"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == github_app.AUTHORIZE_URL
    assert parse_qs(url.query) == {"client_id": ["Iv1.client"], "state": ["st4te"]}
    assert github_app.configured()


@responses.activate
def test_connecting_keeps_both_tokens_encrypted(ctx):
    grant = responses.post(github_app.TOKEN_URL, json=GRANT)
    github_app.connect("user-1", "the-code")
    assert parse_qs(grant.calls[0].request.body) == {
        "client_id": ["Iv1.client"],
        "client_secret": ["shh"],
        "code": ["the-code"],
    }
    row = db.session.get(GitHubAppToken, "user-1")
    assert "ghu_new" not in row.access_token_encrypted
    assert (row.access_token, row.refresh_token) == ("ghu_new", "ghr_new")
    assert github_app.connected("user-1")
    assert github_app.token("user-1") == "ghu_new"


@responses.activate
def test_a_refused_code_connects_nothing(ctx):
    responses.post(github_app.TOKEN_URL, json={"error": "bad_verification_code"})
    with pytest.raises(github_app.NotConnectedError):
        github_app.connect("user-1", "stale")
    assert not github_app.connected("user-1")


def test_no_sign_in_is_not_connected(ctx):
    with pytest.raises(github_app.NotConnectedError, match="Connect GitHub"):
        github_app.token("user-1")


@responses.activate
def test_a_token_about_to_expire_is_renewed_once_under_a_lock(ctx):
    stored(timedelta(minutes=2))
    renewal = responses.post(github_app.TOKEN_URL, json=GRANT)
    assert github_app.token("user-1") == "ghu_new"
    assert parse_qs(renewal.calls[0].request.body)["refresh_token"] == ["ghr_old"]
    assert github_app.token("user-1") == "ghu_new"
    assert len(renewal.calls) == 1
    assert not ctx.extensions["redis"].exists(github_app.RENEW_LOCK)


@responses.activate
def test_a_refused_renewal_disconnects(ctx):
    stored(timedelta(minutes=-1))
    responses.post(github_app.TOKEN_URL, json={"error": "bad_refresh_token"})
    with pytest.raises(github_app.NotConnectedError):
        github_app.token("user-1")
    assert not github_app.connected("user-1")


def test_an_expired_refresh_token_disconnects_without_asking_github(ctx):
    stored(timedelta(minutes=-1), refresh_in=timedelta(seconds=-1))
    with pytest.raises(github_app.NotConnectedError):
        github_app.token("user-1")
    assert not github_app.connected("user-1")


def test_a_fresh_token_is_used_as_it_is(ctx):
    stored(timedelta(hours=1))
    assert github_app.token("user-1") == "ghu_old"


def test_waiting_on_another_workers_renewal_times_out(ctx, monkeypatch):
    stored(timedelta(minutes=1))
    ctx.extensions["redis"].set(github_app.RENEW_LOCK, "1")
    monkeypatch.setattr(github_app, "RENEW_WAIT_S", 0)
    with pytest.raises(TimeoutError):
        github_app.token("user-1")


def sign(body: bytes, secret: str = "hook") -> str:  # noqa: S107 -- the tests' webhook secret
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_only_the_webhook_secrets_signature_is_valid(ctx):
    assert github_app.valid_signature(b"{}", sign(b"{}"))
    assert not github_app.valid_signature(b"{}", sign(b"{}", "other"))
    assert not github_app.valid_signature(b"{ }", sign(b"{}"))
    assert not github_app.valid_signature(b"{}", "")
    ctx.config["GITHUB_APP_WEBHOOK_SECRET"] = ""
    assert not github_app.valid_signature(b"{}", "sha256=" + hmac.new(b"", b"{}", hashlib.sha256).hexdigest())


REPO = {"repository": {"full_name": "alice/project"}}


@pytest.mark.parametrize(
    ("event", "payload", "changes"),
    [
        ("pull_request", REPO | {"pull_request": {"number": 4}}, [{"kind": "pull", "id": "4"}]),
        ("pull_request_review", REPO | {"pull_request": {"number": 4}}, [{"kind": "pull", "id": "4"}]),
        (
            "check_run",
            REPO | {"check_run": {"pull_requests": [{"number": 4}, {"number": 5}]}},
            [{"kind": "pull", "id": "4"}, {"kind": "pull", "id": "5"}],
        ),
        ("check_suite", REPO | {"check_suite": {"pull_requests": []}}, [{"kind": "repo"}]),
        ("workflow_run", REPO | {"workflow_run": {"id": 9, "name": "Release"}}, [{"kind": "run", "id": "9"}]),
        ("workflow_run", REPO | {"workflow_run": {"id": 9, "name": "Tests"}}, []),
        (
            "workflow_job",
            REPO | {"workflow_job": {"run_id": 9, "workflow_name": "Release"}},
            [{"kind": "run", "id": "9"}],
        ),
        ("workflow_job", REPO | {"workflow_job": {"run_id": 9, "workflow_name": "Tests"}}, []),
        ("status", REPO, [{"kind": "repo"}]),
        ("push", REPO, [{"kind": "repo"}]),
        ("star", REPO, []),
        ("ping", {"zen": "Keep it logically awesome."}, []),
    ],
)
def test_deliveries_name_what_to_re_read(event, payload, changes):
    assert github_app.changes_in(event, payload) == [{"repo": "alice/project"} | change for change in changes]


def test_installation_changes_ask_for_the_repositories_again():
    assert github_app.changes_in("installation_repositories", {}) == [{"kind": "repositories"}]


def test_the_feed_starts_at_its_newest_entry_and_returns_what_follows(ctx):
    github_app.record([{"repo": "alice/project", "kind": "repo"}])
    start = github_app.changes_after(None)
    assert start["changes"] == []
    github_app.record([{"repo": "alice/project", "kind": "pull", "id": "4"}])
    feed = github_app.changes_after(start["last"])
    assert feed["changes"] == [{"repo": "alice/project", "kind": "pull", "id": "4"}]
    assert feed["reset"] is False
    assert github_app.changes_after(feed["last"])["changes"] == []


def test_an_empty_feed_starts_at_zero(ctx):
    assert github_app.changes_after(None) == {"last": "0-0", "changes": [], "reset": False}
    github_app.record([{"repo": "alice/project", "kind": "repo"}])
    assert len(github_app.changes_after("0-0")["changes"]) == 1


def test_a_reader_behind_the_trimmed_feed_or_ahead_of_an_emptied_one_reads_everything(ctx):
    redis = ctx.extensions["redis"]
    github_app.record([{"repo": "alice/project", "kind": "repo"}])
    first = github_app.changes_after(None)["last"]
    github_app.record([{"repo": "alice/project", "kind": "repo"}] * 3)
    redis.xtrim(github_app.CHANGES, maxlen=2, approximate=False)
    assert github_app.changes_after(first) == {
        "last": redis.xrevrange(github_app.CHANGES)[0][0],
        "changes": [],
        "reset": True,
    }
    later = github_app.changes_after(None)["last"]
    redis.delete(github_app.CHANGES)
    assert github_app.changes_after(later)["reset"] is True
