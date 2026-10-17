import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
# What broker.create_app() cannot start without: config.load() reads each of these with os.environ[...].
REQUIRED = re.findall(r'os\.environ\["(\w+)"\]', (ROOT / "broker" / "config.py").read_text())


def _env_file_names(service: dict) -> set[str]:
    """Return the variables a service's env_files provide, by the names secrets/<file>.example lists."""
    names: set[str] = set()
    for env_file in service.get("env_file", []):
        example = ROOT / "secrets" / f"{env_file}.example"
        names |= set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example.read_text(), re.MULTILINE))
    return names


def _broker_apps() -> dict[str, dict]:
    """Return the services that start the broker's own app: the broker, and the providers using broker.create_app()."""
    apps = {}
    for name, service in COMPOSE["services"].items():
        command = service.get("command") or []
        module = command[2] if command[:2] == ["python", "-m"] else None
        if module is None:
            continue
        source = (ROOT / f"{module.replace('.', '/')}.py").read_text()
        # The music provider imports it; the others run in provider_loop, which does. (Glowmarkt's has its own app.)
        if "from broker import create_app" in source or "provider_loop" in source:
            apps[name] = service
    return apps


def test_the_services_that_start_the_broker_app_are_found():
    assert {"spotify-provider", "developer-provider"} <= set(_broker_apps())


def test_every_service_that_starts_the_broker_app_has_the_variables_it_needs():
    """A provider copied from another without its `environment:` crash-loops on a KeyError once deployed."""
    assert REQUIRED
    missing = {
        name: sorted(set(REQUIRED) - set(service.get("environment", {})) - _env_file_names(service))
        for name, service in _broker_apps().items()
    }
    assert {name: names for name, names in missing.items() if names} == {}
