import json
from pathlib import Path

import pytest

from broker.openapi import build_spec, render_spec

SPEC_FILE = Path(__file__).resolve().parent.parent / "openapi.json"


def test_the_committed_spec_is_current(app):
    """The renderer generates its models from openapi.json, so changing the wire format (broker/contract.py)
    or a documented route means regenerating it: `python scripts/export_openapi.py`."""
    assert SPEC_FILE.read_text() == render_spec(build_spec(app)), (
        "openapi.json is stale; run `python scripts/export_openapi.py` and commit it"
    )


def _refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def test_every_reference_in_the_spec_resolves(app):
    spec = build_spec(app)
    for ref in _refs(spec):
        node = spec
        for part in ref.removeprefix("#/").split("/"):
            node = node[part]


def test_the_config_route_is_in_the_spec_with_device_auth(app):
    operation = build_spec(app)["paths"]["/api/config"]["get"]
    assert operation["security"] == [{"deviceSecret": []}]
    assert {"200", "401", "403", "404"} <= operation["responses"].keys()


def test_every_config_field_is_required(app):
    """A response always carries every field, which is what lets the renderer reject one that doesn't."""
    schema = build_spec(app)["components"]["schemas"]["ConfigResponse"]
    assert set(schema["required"]) == set(schema["properties"])


def test_a_live_response_has_exactly_the_specs_fields(client, register_device):
    _, secret = register_device()
    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()
    schema = json.loads(SPEC_FILE.read_text())["components"]["schemas"]["ConfigResponse"]
    assert set(body) == set(schema["properties"])


def test_a_path_parameter_is_declared_and_the_flask_syntax_converted():
    """No documented route has one yet, so this uses a stand-in."""
    from flask import Flask
    from pydantic import BaseModel

    from broker.openapi import documented

    class Thing(BaseModel):
        name: str

    stand_in = Flask(__name__)

    @stand_in.get("/things/<int:thing_id>")
    @documented("A thing.", response=Thing)
    def get_thing(thing_id): ...

    spec = build_spec(stand_in)

    operation = spec["paths"]["/things/{thing_id}"]["get"]
    assert operation["parameters"] == [
        {"name": "thing_id", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    assert "security" not in operation
    assert operation["responses"].keys() == {"200"}
    assert "Thing" in spec["components"]["schemas"]


def test_the_config_route_declares_the_optional_device_name_header(app):
    parameters = build_spec(app)["paths"]["/api/config"]["get"]["parameters"]
    assert [(p["name"], p["in"], p["required"]) for p in parameters] == [("X-Device-Name", "header", False)]


def test_the_header_the_route_declares_is_the_one_it_reads(client, register_device):
    from broker.routes.devices import DEVICE_NAME

    _, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}", DEVICE_NAME.name: "camilla"}
    assert client.get("/api/config", headers=headers).status_code == 200
    from broker.db import db
    from broker.models import Device

    with client.application.app_context():
        assert db.session.query(Device).one().device_name == "camilla"


def test_a_required_header_comes_after_path_parameters_and_is_marked_required():
    from flask import Flask
    from pydantic import BaseModel

    from broker.openapi import Header, documented

    class Thing(BaseModel):
        name: str

    stand_in = Flask(__name__)

    @stand_in.get("/things/<thing_id>")
    @documented("A thing.", response=Thing, headers=[Header("X-Trace", "A trace id.", required=True)])
    def get_thing(thing_id): ...

    parameters = build_spec(stand_in)["paths"]["/things/{thing_id}"]["get"]["parameters"]

    assert [(p["name"], p["in"], p["required"]) for p in parameters] == [
        ("thing_id", "path", True),
        ("X-Trace", "header", True),
    ]


@pytest.mark.parametrize("name", ["Authorization", "authorization", "Content-Type", "Accept"])
def test_headers_openapi_says_to_ignore_are_refused(name):
    from pydantic import BaseModel

    from broker.openapi import Header, documented

    class Thing(BaseModel):
        name: str

    with pytest.raises(ValueError, match="can't be a header parameter"):
        documented("A thing.", response=Thing, headers=[Header(name, "x")])
