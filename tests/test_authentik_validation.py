import re
import subprocess
from unittest.mock import Mock

import pytest

from scripts import validate_authentik_config as validator


def test_blueprint_object_types_use_fully_qualified_model():
    object_types = [
        line.strip().partition(": ")[2]
        for line in validator.BLUEPRINT.read_text().splitlines()
        if line.strip().startswith("object_type:")
    ]
    assert all(t.startswith("authentik_") and "." in t for t in object_types)


def test_blueprint_holds_no_exported_identifiers():
    """Entries are looked up by name or slug; exported pks only exist on one instance."""
    assert not re.search(r"^\s+pk:", validator.BLUEPRINT.read_text(), re.MULTILINE)


def test_version_comes_from_compose_default():
    assert validator.authentik_version() in validator.ROOT.joinpath("docker-compose.yml").read_text()


def test_applies_blueprint_after_stock_blueprints_and_always_tears_down(monkeypatch):
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(validator, "compose", compose)
    validator.validate_blueprint("1.2.3")
    assert [c[0] for c in calls] == ["up", "up", "exec", "exec", "exec", "down"]
    assert calls[2][-3:] == ("python", "-m", "lifecycle.migrate")
    assert "default/*.yaml" in calls[3][-1]
    assert calls[4][-3:] == ("ak", "apply_blueprint", "custom/authentik-config.yaml")
    assert "--dry-run" not in calls[4]


def test_tears_down_when_the_apply_fails(monkeypatch):
    calls = []

    def compose(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "exec" and "custom/authentik-config.yaml" in args:
            raise subprocess.CalledProcessError(1, ["docker"])

    monkeypatch.setattr(validator, "compose", compose)
    with pytest.raises(subprocess.CalledProcessError):
        validator.validate_blueprint("1.2.3")
    assert calls[-1] == "down"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.CalledProcessError(1, ["docker", "compose", "exec", "ak"]), "exited 1"),
        (subprocess.TimeoutExpired("docker", 900), "timed out"),
    ],
)
def test_reports_failure(monkeypatch, capsys, error, message):
    monkeypatch.setattr(validator, "validate_blueprint", Mock(side_effect=error))
    assert validator.main() == 1
    assert message in capsys.readouterr().err
