"""Validate the candidate blueprint inside the running Authentik worker."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

BLUEPRINT = Path(__file__).resolve().parents[1] / "authentik/authentik-config.yaml"
WORKER_COMMAND = """
set -eu
directory=$(mktemp -d /tmp/authentik-validation-XXXXXXXX)
blueprint="$directory/candidate.yaml"
trap 'rm -f "$blueprint"; rmdir "$directory"' EXIT
cat > "$blueprint"
BLUEPRINT_LOGS_CLIENT_SECRET=validation-only \
BLUEPRINT_METRICS_CLIENT_SECRET=validation-only \
AUTHENTIK_BLUEPRINTS_DIR="$directory" ak apply_blueprint --dry-run candidate.yaml
"""
REMOTE_COMMAND = "cd /opt/auth-broker && docker compose exec -T authentik-worker sh -c " + shlex.quote(WORKER_COMMAND)


def validate_blueprint(target: str, identity_file: str) -> None:
    if not target or target.startswith("-") or any(c.isspace() for c in target):
        raise ValueError("DEPLOY_TARGET must be a valid SSH user@host target.")
    if not identity_file:
        raise ValueError("DEPLOY_SSH_IDENTITY_FILE must be set.")
    with BLUEPRINT.open("rb") as blueprint:
        subprocess.run(
            [
                "ssh",
                "-i",
                identity_file,
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                target,
                REMOTE_COMMAND,
            ],
            stdin=blueprint,
            check=True,
            timeout=180,
        )


def main() -> int:
    try:
        validate_blueprint(
            os.environ.get("DEPLOY_TARGET", ""),
            os.environ.get("DEPLOY_SSH_IDENTITY_FILE", ""),
        )
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("Authentik validation timed out.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"Authentik SSH validation failed with exit code {exc.returncode}.",
            file=sys.stderr,
        )
        return 1
    print("Authentik blueprint dry-run validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
