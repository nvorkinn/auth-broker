import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
# What broker.create_app() cannot start without: config.load() reads each of these with os.environ[...].
REQUIRED = re.findall(r'os\.environ\["(\w+)"\]', (ROOT / "broker" / "config.py").read_text())
# And what provider_loop.read_only_app() can't (CREDENTIAL_ENCRYPTION_KEY).
REQUIRED_READ_ONLY = re.findall(r'os\.environ\["(\w+)"\]', (ROOT / "broker" / "provider_loop.py").read_text())


def _env_file_names(service: dict) -> set[str]:
    """Return the variables a service's env_files provide, by the names secrets/<file>.example lists."""
    names: set[str] = set()
    for env_file in service.get("env_file", []):
        example = ROOT / "secrets" / f"{env_file}.example"
        names |= set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example.read_text(), re.MULTILINE))
    return names


def _providers(*markers: str) -> dict[str, dict]:
    """Return the services run with `python -m <module>` whose module's source has any of `markers`."""
    found = {}
    for name, service in COMPOSE["services"].items():
        command = service.get("command") or []
        module = command[2] if command[:2] == ["python", "-m"] else None
        if module is None:
            continue
        source = (ROOT / f"{module.replace('.', '/')}.py").read_text()
        if any(marker in source for marker in markers):
            found[name] = service
    return found


def _broker_apps() -> dict[str, dict]:
    """Return the providers that start the broker's own app (broker.create_app()), which migrates the database."""
    return _providers("from broker import create_app")


def _read_only_apps() -> dict[str, dict]:
    """Return the providers that read the database in provider_loop.read_only_app().

    The ones run in provider_loop.serve(), which uses it unless given broker.create_app (the music provider).
    """
    return {name: s for name, s in _providers("provider_loop.serve").items() if name not in _broker_apps()}


def test_the_services_that_start_an_app_are_found():
    assert {"spotify-provider"} <= set(_broker_apps())
    assert {"glow-provider", "developer-provider"} <= set(_read_only_apps())
    assert not set(_broker_apps()) & set(_read_only_apps())


@pytest.mark.parametrize(("apps", "required"), [(_broker_apps, REQUIRED), (_read_only_apps, REQUIRED_READ_ONLY)])
def test_every_service_that_starts_an_app_has_the_variables_it_needs(apps, required):
    """A provider copied from another without its `environment:` crash-loops on a KeyError once deployed."""
    assert required
    missing = {
        name: sorted(set(required) - set(service.get("environment", {})) - _env_file_names(service))
        for name, service in apps().items()
    }
    assert {name: names for name, names in missing.items() if names} == {}


def test_the_providers_that_only_read_the_database_mount_it_read_only():
    """So nothing in them can write to it, whatever the code does."""
    apps = _read_only_apps()
    mounts = {name: [v for v in s.get("volumes", []) if v.startswith("./data:")] for name, s in apps.items()}
    assert mounts == {name: ["./data:/app/data:ro"] for name in apps}
