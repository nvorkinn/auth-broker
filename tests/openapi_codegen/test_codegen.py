import importlib
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from flask import Flask
from pydantic import ValidationError

from tools.openapi_codegen.__main__ import main

ROOT = Path(__file__).resolve().parents[2]

ERROR = {"$ref": "#/components/schemas/Error"}


def _json(schema):
    return {"application/json": {"schema": schema}}


RICH_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Pets", "version": "1"},
    "paths": {
        "/pets": {
            "get": {
                "summary": "List pets.",
                "parameters": [
                    {"name": "tag", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}},
                    {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": _json({"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}),
                    }
                },
            },
            "post": {
                "summary": "Create a pet.",
                "requestBody": {"required": True, "content": _json({"$ref": "#/components/schemas/NewPet"})},
                "responses": {
                    "201": {"description": "Created", "content": _json({"$ref": "#/components/schemas/Pet"})},
                    "422": {"description": "Bad", "content": _json(ERROR)},
                },
            },
        },
        "/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {
                "responses": {
                    "200": {"description": "OK", "content": _json({"$ref": "#/components/schemas/Pet"})},
                    "404": {"description": "Missing", "content": _json(ERROR)},
                },
            },
            "put": {
                "parameters": [{"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"content": _json({"$ref": "#/components/schemas/NewPet"})},
                "responses": {"200": {"description": "OK", "content": _json({"$ref": "#/components/schemas/Pet"})}},
            },
            "delete": {"responses": {"204": {"description": "Gone"}}},
        },
    },
    "components": {
        "schemas": {
            "Error": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
                "required": ["error"],
            },
            "NewPet": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "kind": {"enum": ["cat", "dog"], "type": "string"},
                    "birthDate": {"type": "string", "format": "date"},
                    "nick-name": {"type": "string"},
                    "weight": {"type": "number", "default": 1.5},
                    "parent": {"$ref": "#/components/schemas/Pet"},
                },
                "required": ["name", "kind"],
            },
            "Pet": {
                "allOf": [
                    {"$ref": "#/components/schemas/NewPet"},
                    {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "token": {"type": "string", "format": "uuid"},
                            "owner": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                        "required": ["id", "owner"],
                    },
                ],
            },
        }
    },
}


class FlaskSession(requests.Session):
    """A requests session whose calls are answered by a Flask test client."""

    def __init__(self, app):
        super().__init__()
        self.client = app.test_client()
        self.calls = []

    def request(self, method, url, params=None, headers=None, json=None, cookies=None, timeout=None, **_):
        self.calls.append((method, url))
        response = self.client.open(urlsplit(url).path, method=method, query_string=params, headers=headers, json=json)
        result = requests.Response()
        result.status_code = response.status_code
        result._content = response.data
        result.headers.update(response.headers)
        return result


def _generate(tmp_path, spec, name):
    source = tmp_path / f"{name}.json"
    source.write_text(json.dumps(spec))
    out = tmp_path / name
    assert main([str(source), "--out", str(out)]) == 0
    sys.path.insert(0, str(tmp_path))
    try:
        yield importlib.import_module(name)
    finally:
        sys.path.remove(str(tmp_path))
        for module in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
            del sys.modules[module]


@pytest.fixture
def device_api(tmp_path):
    yield from _generate(tmp_path, json.loads((ROOT / "openapi.json").read_text()), "device_api")


@pytest.fixture
def pets_api(tmp_path):
    yield from _generate(tmp_path, RICH_SPEC, "pets_api")


def _config(models, **overrides):
    values = {
        "device_id": "d1",
        "interval": 60,
        "screen": {"orientation": "horizontal", "width": 10, "height": 20},
        "weather": {"api_key": "k", "location": "L"},
        "notice_board": {"postcode": None},
        "tfl": {"app_key": "k", "stop_ids": ["a"]},
        "spotify": {"enabled": True},
        "glowmarkt": {"username": None, "password": None},
        "pairing_code": None,
        "setup_missing": [],
    }
    return models.ConfigResponse(**{**values, **overrides})


def test_models_required_and_nullable(device_api):
    from device_api import models

    config = _config(models)
    assert config.pairing_code is None
    assert config.screen.orientation == "horizontal"
    with pytest.raises(ValidationError):
        models.ConfigResponse.model_validate({**config.model_dump(), "pairing_code": ...})
    data = config.model_dump()
    del data["pairing_code"]
    with pytest.raises(ValidationError):
        models.ConfigResponse.model_validate(data)
    with pytest.raises(ValidationError):
        models.Screen(orientation="diagonal", width=1, height=1)
    assert models.Error(error="x").error == "x"


def test_client_and_blueprint_round_trip(device_api):
    from device_api import models
    from device_api.blueprints import api_config as blueprint
    from device_api.clients import ApiConfigClient, ApiError

    seen = {}

    class View(blueprint.ApiConfigView):
        def authenticate(self, security):
            if self.bearer_token() != "secret":
                raise blueprint.ApiHTTPError(401, message="Missing or unknown secret.")

        def handle_get(self, *, x_device_name=None):
            seen["name"] = x_device_name
            return _config(models)

    app = Flask(__name__)
    app.register_blueprint(blueprint.create_blueprint(View))
    session = FlaskSession(app)

    client = ApiConfigClient("http://broker", session, auth_token="secret")
    config = client.get(x_device_name="kitchen")
    assert isinstance(config, models.ConfigResponse)
    assert config.device_id == "d1"
    assert seen == {"name": "kitchen"}
    assert session.calls == [("GET", "http://broker/api/config")]

    with pytest.raises(ApiError) as caught:
        ApiConfigClient("http://broker", session, auth_token="wrong").get()
    assert caught.value.status_code == 401
    assert caught.value.body == models.Error(error="Missing or unknown secret.")


def test_default_view_is_not_implemented_and_fails_closed(device_api):
    from device_api.blueprints import register_all
    from device_api.clients import ApiConfigClient, ApiError

    app = Flask(__name__)
    register_all(app)
    with pytest.raises(ApiError) as caught:
        ApiConfigClient("http://broker", FlaskSession(app), auth_token="secret").get()
    assert caught.value.status_code == 501


def test_register_all_matches_subclasses(device_api):
    from device_api.blueprints import api_config, register_all

    class View(api_config.ApiConfigView):
        def authenticate(self, security):
            return None

    app = Flask(__name__)
    register_all(app, View, url_prefix="/v1")
    assert [rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"] == ["/v1/api/config"]
    with pytest.raises(ValueError):
        register_all(Flask(__name__), View, View)
    with pytest.raises(ValueError):
        register_all(Flask(__name__), int)  # type: ignore[arg-type]


def test_client_without_session_uses_requests_request(device_api, monkeypatch):
    from device_api import models
    from device_api.clients import ApiConfigClient

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["headers"]))
        response = requests.Response()
        response.status_code = 200
        response._content = _config(models).model_dump_json().encode()
        return response

    monkeypatch.setattr(requests, "request", fake_request)
    ApiConfigClient("http://broker/", auth_token="t").get(x_device_name="n")
    assert calls == [("GET", "http://broker/api/config", {"Authorization": "Bearer t", "X-Device-Name": "n"})]


def test_rich_models(pets_api):
    from pets_api import models

    pet = models.NewPet(name="Tom", kind="cat")
    assert pet.weight == 1.5
    assert pet.birthDate is None and pet.parent is None
    with pytest.raises(ValidationError):
        models.NewPet(name="", kind="cat")
    with pytest.raises(ValidationError):
        models.NewPet(name="x", kind="fish")
    nick = models.NewPet.model_validate({"name": "a", "kind": "dog", "nick-name": "z"})
    assert nick.model_dump(by_alias=True, exclude_unset=True)["nick-name"] == "z"
    full = models.Pet(id=1, owner=None, name="a", kind="dog", token=uuid.uuid4())
    assert isinstance(full, models.NewPet)
    assert (
        models.Pet.model_validate(
            {
                "id": 1,
                "owner": "me",
                "name": "a",
                "kind": "cat",
                "parent": {"id": 2, "owner": None, "name": "b", "kind": "dog"},
            }
        ).parent.id
        == 2
    )


def test_rich_signatures(pets_api):
    import inspect

    from pets_api.clients import pets, pets_by_pet_id

    assert (
        str(inspect.signature(pets.PetsClient.get))
        == "(self, *, limit: 'int', tag: 'list[str] | None' = None) -> 'list[Pet]'"
    )
    assert list(inspect.signature(pets.PetsClient.post).parameters) == ["self", "body"]
    assert list(inspect.signature(pets_by_pet_id.PetsByPetIdClient.get).parameters) == ["self", "pet_id"]
    assert list(inspect.signature(pets_by_pet_id.PetsByPetIdClient.put).parameters) == [
        "self",
        "pet_id",
        "*",
        "if_match",
        "body",
    ] or list(inspect.signature(pets_by_pet_id.PetsByPetIdClient.put).parameters) == [
        "self",
        "pet_id",
        "if_match",
        "body",
    ]


def test_rich_round_trip(pets_api):
    from pets_api import models
    from pets_api.blueprints import pets as pets_bp
    from pets_api.blueprints import pets_by_pet_id as by_id_bp
    from pets_api.blueprints import register_all
    from pets_api.clients import ApiError
    from pets_api.clients.pets import PetsClient
    from pets_api.clients.pets_by_pet_id import PetsByPetIdClient

    store = {}

    class Pets(pets_bp.PetsView):
        def handle_get(self, *, limit, tag=None):
            return [pet for pet in store.values() if tag is None or pet.name in tag][:limit]

        def handle_post(self, *, body):
            pet = models.Pet(id=len(store) + 1, owner=None, **body.model_dump())
            store[pet.id] = pet
            return pet, 201

    class ById(by_id_bp.PetsByPetIdView):
        def handle_get(self, *, pet_id):
            if pet_id not in store:
                raise by_id_bp.ApiHTTPError(404, message="no such pet")
            return store[pet_id]

        def handle_put(self, *, pet_id, if_match, body=None):
            assert if_match == "v1"
            store[pet_id] = store[pet_id].model_copy(update={"name": body.name})
            return store[pet_id]

        def handle_delete(self, *, pet_id):
            del store[pet_id]

    app = Flask(__name__)
    register_all(app, Pets, ById)
    session = FlaskSession(app)
    pets = PetsClient("http://x", session)
    by_id = PetsByPetIdClient("http://x", session)

    created = pets.post(models.NewPet(name="Tom", kind="cat"))
    assert created.id == 1
    assert [p.name for p in pets.get(limit=5)] == ["Tom"]
    assert pets.get(limit=5, tag=["nobody"]) == []
    assert by_id.get(1).name == "Tom"
    assert by_id.put(1, if_match="v1", body=models.NewPet(name="Tim", kind="cat")).name == "Tim"
    assert by_id.delete(1) is None
    with pytest.raises(ApiError) as caught:
        by_id.get(1)
    assert caught.value.status_code == 404

    bad = app.test_client().post("/pets", json={"name": "", "kind": "cat"})
    assert bad.status_code == 400
    assert app.test_client().get("/pets").status_code == 400


def test_check_detects_stale_output(tmp_path):
    out = tmp_path / "gen"
    spec = ROOT / "openapi.json"
    assert main([str(spec), "--out", str(out)]) == 0
    assert main([str(spec), "--out", str(out), "--check"]) == 0
    (out / "models.py").write_text("# edited\n")
    assert main([str(spec), "--out", str(out), "--check"]) == 1
    assert (out / "models.py").read_text() == "# edited\n"
    assert main([str(spec), "--out", str(out)]) == 0
    assert main([str(spec), "--out", str(out), "--check"]) == 0
