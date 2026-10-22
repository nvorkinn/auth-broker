"""scripts/check_image_starts.py, which CI's image job runs on the broker image: what it starts, and how.

Starting the containers needs Docker, so CI's image job is what runs the script itself; these pin that it follows
docker-compose.yml.
"""

import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

_spec = importlib.util.spec_from_file_location("check_image_starts", ROOT / "scripts" / "check_image_starts.py")
assert _spec is not None
assert _spec.loader is not None
check_image_starts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_image_starts)


def _on_broker_image() -> dict[str, dict]:
    return {
        name: service
        for name, service in COMPOSE["services"].items()
        if service.get("image", "").startswith("ghcr.io/nvorkinn/auth-broker:")
    }


def test_it_knows_how_every_service_on_the_broker_image_shows_it_has_started():
    """Else the image job fails on the new service: each needs its line in STARTED."""
    assert set(check_image_starts.STARTED) == set(_on_broker_image())
    assert next(iter(check_image_starts.STARTED)) == "auth-broker"  # first: it migrates the database the others read


def test_a_service_is_started_with_compose_s_command_network_environment_and_mounts(tmp_path):
    service = _on_broker_image()["glow-provider"]

    options = check_image_starts.run_options("glow-provider", service, tmp_path)

    assert options[:3] == ["--detach", "--name", "check-glow-provider"]
    assert options[options.index("--network") + 1] == "host"
    assert options[options.index("--volume") + 1] == f"{tmp_path / 'data'}:/app/data:ro"
    env = dict(option.removeprefix("--env=").split("=", 1) for option in options if option.startswith("--env="))
    assert env.keys() == check_image_starts.ENV_FILES["broker.env"].keys() | {"REDIS_PASSWORD"}
    assert check_image_starts.command(service) == ["python", "-m", "broker.providers.glow"]


def test_the_broker_gets_its_compose_environment_too(tmp_path):
    options = check_image_starts.run_options("auth-broker", _on_broker_image()["auth-broker"], tmp_path)

    assert "--env=SPOTIFY_REDIRECT_URI=https://auth.nikolaivorkinn.com/auth/spotify/callback" in options
    assert check_image_starts.command(_on_broker_image()["auth-broker"])[0] == "gunicorn"


def test_the_stand_in_secrets_are_ones_the_real_env_files_have():
    for env_file, values in check_image_starts.ENV_FILES.items():
        example = (ROOT / "secrets" / f"{env_file}.example").read_text()
        assert set(values) <= set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.MULTILINE)), env_file


def test_error_and_fatal_log_lines_fail_it_but_warnings_and_plain_text_dont():
    lines = [
        json.dumps({"level": "warn", "_msg": "Couldn't publish subscriptions to Redis"}),
        json.dumps({"level": "error", "_msg": "Music: the cycle failed; trying again next tick"}),
        "Traceback (most recent call last):",
        json.dumps({"level": "fatal", "_msg": "Shutting down"}),
        json.dumps(["not", "a", "record"]),
    ]

    assert check_image_starts.failed_lines("\n".join(lines)) == [lines[1], lines[3]]
