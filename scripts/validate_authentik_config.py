"""Apply the candidate blueprint to a throwaway Authentik and Postgres.

A real apply, not --dry-run: the dry run can't resolve !KeyOf references to objects the
blueprint is about to create, so it can't check a blueprint on an instance that lacks them.
The instance is migrated and given Authentik's default and system blueprints first, as a
running Authentik would have, so the stock objects the blueprint looks up exist.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "authentik/authentik-config.yaml"
COMPOSE_FILE = ROOT / "scripts/authentik-validate/compose.yml"
STOCK_BLUEPRINTS = "cd /blueprints && ls default/*.yaml system/*.yaml"
TIMEOUT = 900


def authentik_version() -> str:
    """Return the upstream release docker-compose.yml deploys by default."""
    match = re.search(r"goauthentik/server:\$\{AUTHENTIK_VERSION:-([^}]+)\}", (ROOT / "docker-compose.yml").read_text())
    if not match:
        msg = "No default AUTHENTIK_VERSION in docker-compose.yml."
        raise ValueError(msg)
    return match.group(1)


def compose(*args: str, version: str, check: bool, capture_output: bool = False) -> subprocess.CompletedProcess[bytes]:
    """Run `docker compose` on the validation project with Authentik `version`."""
    env = {**os.environ, "AUTHENTIK_VERSION": version}
    command = ["docker", "compose", "-p", "authentik-validate", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(command, env=env, timeout=TIMEOUT, check=check, capture_output=capture_output)  # noqa: S603 -- fixed docker command, args from this script


def validate_blueprint(version: str) -> None:
    """Apply the blueprint to a fresh Authentik `version`, then tear the instance down."""
    try:
        compose("up", "-d", "--wait", "postgresql", version=version, check=True)
        compose("up", "-d", "authentik", version=version, check=True)
        compose("exec", "-T", "authentik", "python", "-m", "lifecycle.migrate", version=version, check=True)
        script = f'for f in $({STOCK_BLUEPRINTS}); do ak apply_blueprint "$f" > /dev/null || exit 1; done'
        compose("exec", "-T", "authentik", "sh", "-c", script, version=version, check=True)
        compose(
            "exec",
            "-T",
            "authentik",
            "ak",
            "apply_blueprint",
            "custom/authentik-config.yaml",
            version=version,
            check=True,
        )
    finally:
        compose("down", "-v", "--remove-orphans", version=version, check=False, capture_output=True)


def main() -> int:
    """Validate the blueprint and report the outcome; return the exit status."""
    try:
        validate_blueprint(authentik_version())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("Authentik validation timed out.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Authentik validation failed: {exc.cmd[-3:]} exited {exc.returncode}.", file=sys.stderr)
        return 1
    print("Authentik blueprint applied to a throwaway instance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
