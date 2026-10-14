import json
from pathlib import Path

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
