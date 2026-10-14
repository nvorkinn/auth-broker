"""Writes openapi.json at the repo root: the device API's contract, generated from broker/contract.py and
the routes that declare themselves (see broker/openapi.py). Run it after changing either; a test fails
while the committed file is stale.

    python scripts/export_openapi.py

Needs no real settings: building the app only reads placeholders, and its database is a throwaway."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLACEHOLDERS = {
    "FLASK_SECRET_KEY": "x",
    "SPOTIFY_CLIENT_ID": "x",
    "SPOTIFY_CLIENT_SECRET": "x",
    "SPOTIFY_REDIRECT_URI": "https://example.com/auth/spotify/callback",
}


def main() -> None:
    from cryptography.fernet import Fernet

    with tempfile.TemporaryDirectory() as tmp:
        for name, value in PLACEHOLDERS.items():
            os.environ[name] = value
        os.environ["CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ["BROKER_DB_PATH"] = str(Path(tmp) / "broker.db")

        from broker import create_app
        from broker.db import db
        from broker.openapi import build_spec, render_spec

        app = create_app()
        spec = build_spec(app)
        with app.app_context():
            db.engine.dispose()

    (ROOT / "openapi.json").write_text(render_spec(spec))
    print("Wrote openapi.json")


if __name__ == "__main__":
    main()
