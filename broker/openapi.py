"""The device API's OpenAPI document, built from the routes that declare themselves with `documented`.

    @bp.get("/config")
    @require_device_auth(roles={"renderer"})
    @documented("The device's config.", response=ConfigResponse, device_auth=True)
    def get_config(device_id): ...

The routes and the document can't drift: the document is read off the Flask URL map. A route not
declared isn't in it yet. `openapi.json` at the repo root is the committed copy the renderer
generates its models from; regenerate it with `python scripts/export_openapi.py` (a test fails when it's stale)."""

import json
import re
from collections.abc import Callable
from typing import Any

from flask import Flask
from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema

from .contract import API_VERSION, Error

_ATTRIBUTE = "__openapi__"
_FLASK_PARAMETER = re.compile(r"<(?:[^:>]+:)?([^>]+)>")
_DEVICE_AUTH = "deviceSecret"


class _PlainSchema(GenerateJsonSchema):
    """Pydantic titles every field with its name reformatted ("Api Key"), which only adds noise to the
    spec and to the models generated from it."""

    def field_title_should_be_set(self, schema) -> bool:
        return False


def documented(
    summary: str, *, response: type[BaseModel], device_auth: bool = False, not_found: bool = False
) -> Callable:
    """Declares a view's 200 response model. `device_auth` adds the bearer-secret scheme and its
    401 and 403; `not_found` adds a 404."""

    def decorator(view: Callable) -> Callable:
        setattr(
            view,
            _ATTRIBUTE,
            {"summary": summary, "response": response, "device_auth": device_auth, "not_found": not_found},
        )
        return view

    return decorator


def _error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{Error.__name__}"}}},
    }


def _operation(rule_path: str, meta: dict[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = {"summary": meta["summary"]}
    parameters = [
        {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
        for name in _FLASK_PARAMETER.findall(rule_path)
    ]
    if parameters:
        operation["parameters"] = parameters
    responses: dict[str, Any] = {
        "200": {
            "description": "OK",
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{meta['response'].__name__}"}}},
        }
    }
    if meta["device_auth"]:
        operation["security"] = [{_DEVICE_AUTH: []}]
        responses["401"] = _error_response("Missing or unknown secret.")
        responses["403"] = _error_response("A valid secret for a role this endpoint doesn't allow.")
    if meta["not_found"]:
        responses["404"] = _error_response("Not found.")
    operation["responses"] = dict(sorted(responses.items()))
    return operation


def build_spec(app: Flask) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    models: dict[str, type[BaseModel]] = {Error.__name__: Error}
    for rule in app.url_map.iter_rules():
        meta = getattr(app.view_functions[rule.endpoint], _ATTRIBUTE, None)
        if meta is None:
            continue
        path = _FLASK_PARAMETER.sub(r"{\1}", rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            paths.setdefault(path, {})[method.lower()] = _operation(rule.rule, meta)
        models[meta["response"].__name__] = meta["response"]

    schemas: dict[str, Any] = {}
    for model in models.values():
        schema = model.model_json_schema(
            mode="serialization", ref_template="#/components/schemas/{model}", schema_generator=_PlainSchema
        )
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "auth-broker device API",
            "version": API_VERSION,
            "description": "What a renderer polls the broker for. Generated from broker/contract.py; do not edit.",
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {_DEVICE_AUTH: {"type": "http", "scheme": "bearer"}},
            "schemas": dict(sorted(schemas.items())),
        },
    }


def render_spec(spec: dict[str, Any]) -> str:
    """The text of openapi.json: stable, so a regeneration only diffs when the contract changed."""
    return json.dumps(spec, indent=2) + "\n"
