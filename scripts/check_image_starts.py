"""Start the broker's image the way docker-compose.yml runs it, and fail if any of its services can't start.

    python3 scripts/check_image_starts.py <image>

The image job in .github/workflows/tests.yml runs this on the broker image it has just built. Every service in
docker-compose.yml on the broker's image gets a container with that service's command, environment, network and data
directory (read-only where compose mounts it so), and dummy values for the secrets in its env_files. The broker
(gunicorn, which reads gunicorn.conf.py and runs its hooks) has to answer GET /about and each provider has to log that
it has started; then each has to stop cleanly on SIGTERM, as `docker compose stop` stops it, having logged nothing at
error level. There's no Redis: what they write is lost and logged, as on a host whose Redis is down.

So a service that can't start fails the job, as v1.34.0's broker would have: gunicorn refused gunicorn.conf.py
("Invalid value for post_worker_init"), and nothing else in CI ran gunicorn.

Standard library only, as it runs on the CI runner's own Python (3.12 on ubuntu-latest, which evaluates annotations
as it defines a function, so none here may name a TYPE_CHECKING import); the image reads docker-compose.yml for it.
"""

import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_NAME = "ghcr.io/nvorkinn/auth-broker"

# How each service on the broker's image shows it has started: a URL that answers 200, or a line it logs once it's
# running (and handles SIGTERM). In start order: the broker first, as it creates and migrates the database the
# providers read. The check fails on a service compose has and this hasn't, so a new one gets a line here.
STARTED = {
    "auth-broker": "http://127.0.0.1:5000/about",
    "spotify-provider": "Starting the music provider",
    "developer-provider": "Starting the developer provider",
    "glow-provider": "Fetching Glowmarkt readings",
}

# Stand-ins for what deploy.sh decrypts into each env_file on the host: the secrets broker.create_app() and
# provider_loop.read_only_app() can't start without. Leaving out the optional ones keeps what they turn on off.
ENV_FILES = {
    "broker.env": {
        "FLASK_SECRET_KEY": "ci",
        "SPOTIFY_CLIENT_ID": "ci",
        "SPOTIFY_CLIENT_SECRET": "ci",
        "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),  # a Fernet key
    },
    "redis.env": {"REDIS_PASSWORD": "ci"},
}

START_TIMEOUT_S = 60
STOP_TIMEOUT_S = 20
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# The JSON log lines' `level`s (broker/log_format.py) that fail the check.
FAILED_LEVELS = {"error", "fatal"}


class CheckFailedError(Exception):
    """A service didn't start, or didn't stop cleanly."""


def run_docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a docker command, whatever its exit status."""
    # The arguments come from this file and docker-compose.yml.
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, check=False)  # noqa: S603, S607


def docker(*args: str, stdin: str | None = None) -> str:
    """Run a docker command and return its stdout; raise CheckFailedError if it fails."""
    result = run_docker(*args, stdin=stdin)
    if result.returncode != 0:
        msg = f"docker {shlex.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        raise CheckFailedError(msg)
    return result.stdout


def compose_services(image: str) -> dict[str, dict]:
    """Return docker-compose.yml's services that run the broker's image, as the image's own PyYAML reads them."""
    to_json = "import json, sys, yaml; json.dump(yaml.safe_load(sys.stdin), sys.stdout)"
    compose_yaml = (ROOT / "docker-compose.yml").read_text()
    compose = json.loads(docker("run", "--rm", "--interactive", image, "python", "-c", to_json, stdin=compose_yaml))
    return {
        name: service
        for name, service in compose["services"].items()
        if service.get("image", "").split(":")[0] == IMAGE_NAME
    }


def run_options(name: str, service: dict, deploy_dir: Path) -> list[str]:
    """Return the `docker run` options that start `service` as compose does, with `deploy_dir` as compose's ./."""
    options = ["--detach", "--name", container(name)]
    if "network_mode" in service:
        options += ["--network", service["network_mode"]]
    env_files = service.get("env_file", [])
    for env_file in [env_files] if isinstance(env_files, str) else env_files:
        if env_file not in ENV_FILES:
            msg = f"{name}: no stand-ins for the secrets in its env_file {env_file}; add them to ENV_FILES"
            raise CheckFailedError(msg)
        options += [f"--env={key}={value}" for key, value in ENV_FILES[env_file].items()]
    environment = service.get("environment", {})
    pairs = environment.items() if isinstance(environment, dict) else (item.split("=", 1) for item in environment)
    options += [f"--env={key}={value}" for key, value in pairs]
    for volume in service.get("volumes", []):
        source, _, target = volume.partition(":")
        if not source.startswith("./"):
            msg = f"{name}: can't mount {volume}, only a directory in the deploy directory (./...)"
            raise CheckFailedError(msg)
        (deploy_dir / source).mkdir(parents=True, exist_ok=True)
        options += ["--volume", f"{deploy_dir / source}:{target}"]
    return options


def command(service: dict) -> list[str]:
    """Return the service's command, as arguments."""
    value = service.get("command", [])
    return shlex.split(value) if isinstance(value, str) else list(value)


def container(name: str) -> str:
    """Name the container the check runs `name` in."""
    return f"check-{name}"


def logs(name: str) -> str:
    """Return everything the service's container has written, stdout and stderr together."""
    result = run_docker("logs", container(name))
    return result.stdout + result.stderr


def has_started(name: str, started: str) -> bool:
    """Return whether the service shows it has started: its URL answers 200, or it has logged its line."""
    if not started.startswith("http://"):
        return started in logs(name)
    try:
        # Straight to it, never through a proxy the environment names.
        with DIRECT.open(started, timeout=2) as response:
            return response.status == HTTPStatus.OK
    except OSError:  # refused while it starts, or an HTTP error
        return False


def wait_until_started(name: str, started: str) -> None:
    """Wait for the service to show it has started; raise if its container exits first, or it takes too long."""
    deadline = time.monotonic() + START_TIMEOUT_S
    while not has_started(name, started):
        if docker("inspect", "--format", "{{.State.Running}}", container(name)).strip() != "true":
            msg = f"{name} exited before it started"
            raise CheckFailedError(msg)
        if time.monotonic() > deadline:
            msg = f"{name} didn't start within {START_TIMEOUT_S}s (waiting for {started})"
            raise CheckFailedError(msg)
        time.sleep(0.5)


def failed_lines(output: str) -> list[str]:
    """Return the JSON log lines whose `level` is in FAILED_LEVELS."""
    failed = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("level") in FAILED_LEVELS:
            failed.append(line)
    return failed


def report(name: str) -> list[str]:
    """Print the service's logs, and return what's wrong with how it ran: nothing if it started and stopped cleanly."""
    try:
        state = json.loads(docker("inspect", "--format", "{{json .State}}", container(name)))
    except CheckFailedError:
        return []  # never made: the error that ended the check says why
    output = logs(name)
    print(f"::group::{name}'s logs\n{output}\n::endgroup::", flush=True)
    problems = []
    if state["Running"]:
        problems.append(f"{name} was still running when the check ended")
    elif state["ExitCode"] != 0:
        problems.append(f"{name} exited with {state['ExitCode']}, not 0")
    problems += [f"{name} logged: {line}" for line in failed_lines(output)]
    return problems


def check(image: str) -> list[str]:
    """Start every service on `image`, then stop them; return what went wrong (nothing if they all ran cleanly)."""
    services = compose_services(image)
    if services.keys() != STARTED.keys():
        missing, stale = sorted(services.keys() - STARTED.keys()), sorted(STARTED.keys() - services.keys())
        return [f"STARTED doesn't match docker-compose.yml's services on {IMAGE_NAME}: add {missing}, drop {stale}"]
    problems = []
    made = []
    # What the containers write there is root's; a leftover file mustn't hide the result.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as deploy_dir:
        try:
            for name, started in STARTED.items():
                options = run_options(name, services[name], Path(deploy_dir))
                made.append(name)
                docker("run", *options, image, *command(services[name]))
                print(f"Started {name}, waiting for {started}", flush=True)
                wait_until_started(name, started)
        except CheckFailedError as e:
            problems.append(str(e))
        finally:
            # Whether or not they all started, so each one's exit status says how it stops.
            if made:
                print("Stopping them, as `docker compose stop` does", flush=True)
                run_docker("stop", "--time", str(STOP_TIMEOUT_S), *map(container, made))
            for name in made:
                problems += report(name)
                run_docker("rm", "--force", container(name))
    return problems


def main() -> None:
    """Check the image named on the command line."""
    if len(sys.argv) != 2:  # noqa: PLR2004
        sys.exit(f"usage: {sys.argv[0]} <image>")
    try:
        problems = check(sys.argv[1])
    except CheckFailedError as e:
        problems = [str(e)]
    for problem in problems:
        print(f"::error::{problem}", flush=True)
    if problems:
        sys.exit(1)
    print(f"Every service on the image started, and stopped cleanly: {', '.join(STARTED)}")


if __name__ == "__main__":
    main()
