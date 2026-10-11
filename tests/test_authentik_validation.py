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
    assert object_types
    assert set(object_types) == {"authentik_core.user"}


def test_validates_candidate_over_ssh(monkeypatch):
    def run(command, **kwargs):
        assert command[:3] == ["ssh", "-i", "/key"]
        assert "StrictHostKeyChecking=yes" in command
        assert "BatchMode=yes" in command
        assert command[-2] == "user@host"
        assert command[-1] == validator.REMOTE_COMMAND
        assert b"!Env BLUEPRINT_LOGS_CLIENT_SECRET" in kwargs["stdin"].read()
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 180

    monkeypatch.setattr(validator.subprocess, "run", run)
    validator.validate_blueprint("user@host", "/key")


def test_worker_command_is_dry_run_with_cleanup():
    assert "mktemp -d /tmp/authentik-validation-" in validator.WORKER_COMMAND
    assert """trap 'rm -f "$blueprint"; rmdir "$directory"' EXIT""" in validator.WORKER_COMMAND
    assert 'cat > "$blueprint"' in validator.WORKER_COMMAND
    assert "BLUEPRINT_LOGS_CLIENT_SECRET=validation-only" in validator.WORKER_COMMAND
    assert "BLUEPRINT_METRICS_CLIENT_SECRET=validation-only" in validator.WORKER_COMMAND
    assert (
        'AUTHENTIK_BLUEPRINTS_DIR="$directory" ak apply_blueprint --dry-run candidate.yaml' in validator.WORKER_COMMAND
    )


@pytest.mark.parametrize("exit_code", [0, 1])
def test_worker_cleans_up_after_validation(tmp_path, exit_code):
    command = validator.WORKER_COMMAND.replace("/tmp/", f"{tmp_path}/")
    fake_ak = f"""
ak() {{
    test "$1" = apply_blueprint
    test "$2" = --dry-run
    test "$3" = candidate.yaml
    test "$(cat "$AUTHENTIK_BLUEPRINTS_DIR/$3")" = candidate
    test "$BLUEPRINT_LOGS_CLIENT_SECRET" = validation-only
    test "$BLUEPRINT_METRICS_CLIENT_SECRET" = validation-only
    return {exit_code}
}}
"""
    result = subprocess.run(
        ["sh", "-c", fake_ak + command],
        input=b"candidate",
        capture_output=True,
    )
    assert result.returncode == exit_code, result.stderr.decode()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", ["", "-oProxyCommand=bad", "user@host command"])
def test_rejects_invalid_target(monkeypatch, target):
    run = Mock()
    monkeypatch.setattr(validator.subprocess, "run", run)
    with pytest.raises(ValueError, match="DEPLOY_TARGET"):
        validator.validate_blueprint(target, "/key")
    run.assert_not_called()


def test_requires_identity():
    with pytest.raises(ValueError, match="DEPLOY_SSH_IDENTITY_FILE"):
        validator.validate_blueprint("user@host", "")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.CalledProcessError(1, "ssh"), "exit code 1"),
        (subprocess.TimeoutExpired("ssh", 180), "timed out"),
    ],
)
def test_reports_remote_failure(monkeypatch, capsys, error, message):
    monkeypatch.setenv("DEPLOY_TARGET", "user@host")
    monkeypatch.setenv("DEPLOY_SSH_IDENTITY_FILE", "/key")
    monkeypatch.setattr(validator.subprocess, "run", Mock(side_effect=error))
    assert validator.main() == 1
    assert message in capsys.readouterr().err
