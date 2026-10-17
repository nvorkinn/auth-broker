"""Test the sign-up gate in terraform/policies, run here the way Authentik does.

Its text is the body of a function of `request`. It offers the sign-up only to a login that will return to the
broker's /pair page.
"""

import base64
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

GATE = "terraform/policies/broker-enrollment-from-broker.expression"


def _gate():
    body = Path(GATE).read_text()
    namespace = {}
    exec("def gate(request):\n" + textwrap.indent(body, "    "), namespace)  # noqa: S102 -- builds the gate from the policy text under test
    return namespace["gate"]


def _state(redirect):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'typ': 'JWT'})}.{part({'redirect': redirect})}.signature"


def _request(**query):
    inner = urlencode(query)
    return SimpleNamespace(http_request=SimpleNamespace(GET={"query": inner}))


@pytest.mark.parametrize(
    ("redirect", "allowed"),
    [
        ("https://auth.nikolaivorkinn.com/pair", True),
        ("https://auth.nikolaivorkinn.com/pair?t=" + "a" * 64, True),
        ("https://auth.nikolaivorkinn.com/device", False),
        ("https://auth.nikolaivorkinn.com/", False),
        ("https://auth.nikolaivorkinn.com/pair/token", False),
        ("https://logs.nikolaivorkinn.com/pair", False),
        ("https://evil.example/pair", False),
        ("", False),
    ],
)
def test_only_a_login_returning_to_pair_gets_the_sign_up(redirect, allowed):
    assert _gate()(_request(state=_state(redirect))) is allowed


@pytest.mark.parametrize(
    "request_",
    [
        SimpleNamespace(http_request=None),
        SimpleNamespace(http_request=SimpleNamespace(GET={})),
        _request(client_id="x"),
        _request(state="not-a-token"),
        _request(state="a.b.c"),
        _request(state="a." + base64.urlsafe_b64encode(b"[1, 2]").decode() + ".c"),
        _request(state="a." + base64.urlsafe_b64encode(b"not json").decode() + ".c"),
    ],
)
def test_anything_unreadable_is_refused_rather_than_raising(request_):
    assert _gate()(request_) is False
