"""Generates the broker's side of the countdown API (broker/countdown_api/) from the contract.

    python scripts/generate_api.py

The contract is the latest release of https://github.com/nvorkinn/countdown-api, the same file pi-dashboard
generates its client from. Neither the file nor what comes out of it is committed: run this after cloning, and in
every build (CI does, and so does the Dockerfile). A build therefore fails when the contract moves to something
the broker no longer implements.

COUNTDOWN_API_SPEC points it at another file or URL instead, e.g. a countdown-api change that isn't released yet."""

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

LATEST = "https://github.com/nvorkinn/countdown-api/releases/latest/download/openapi.yaml"

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "contract" / "openapi.yaml"
OUT = ROOT / "broker" / "countdown_api"
# What a server needs: the Flask views and the Pydantic models they use, no client.
PARTS = "blueprints"


def fetch(source: str) -> None:
    SPEC.parent.mkdir(exist_ok=True)
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as response:
            SPEC.write_bytes(response.read())
    else:
        shutil.copyfile(source, SPEC)


def main() -> None:
    source = os.environ.get("COUNTDOWN_API_SPEC", LATEST)
    fetch(source)
    digest = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    version = next((line.split(":", 1)[1].strip() for line in SPEC.read_text().splitlines() if "version:" in line), "?")
    print(f"countdown API {version} (sha256 {digest}) from {source}")
    subprocess.run([sys.executable, "-m", "openapi_codegen", str(SPEC), "--out", str(OUT), "--only", PARTS], check=True)


if __name__ == "__main__":
    main()
