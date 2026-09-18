import functools
import json
import os
import secrets
import urllib.parse
from pathlib import Path

import requests
from flask import Flask, redirect, request, jsonify

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("BROKER_DATA_DIR", Path(__file__).parent / "data"))
TOKENS_FILE = DATA_DIR / "tokens.json"

SPOTIFY_CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
SPOTIFY_REDIRECT_URI = os.environ["SPOTIFY_REDIRECT_URI"]
SPOTIFY_SCOPE = "user-read-currently-playing user-read-playback-state"

BROKER_API_KEY = os.environ["BROKER_API_KEY"]


def require_api_key(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") != BROKER_API_KEY:
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)
    return wrapped


def _read_tokens() -> dict:
    if not TOKENS_FILE.exists():
        return {}
    return json.loads(TOKENS_FILE.read_text())


def _write_tokens(tokens: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


@app.get("/auth/spotify/login")
def spotify_login():
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPE,
        "state": secrets.token_urlsafe(16),
    }
    return redirect(f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}")


@app.get("/auth/spotify/callback")
def spotify_callback():
    error = request.args.get("error")
    if error:
        return f"Spotify authorization failed: {error}", 400

    code = request.args.get("code")
    if not code:
        return "Missing authorization code", 400

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()

    tokens = _read_tokens()
    tokens["spotify"] = {"refresh_token": payload["refresh_token"]}
    _write_tokens(tokens)

    return "Spotify linked successfully. You can close this tab."


@app.get("/api/tokens/<provider>")
@require_api_key
def get_token(provider: str):
    tokens = _read_tokens()
    if provider not in tokens:
        return jsonify(error="not found"), 404
    return jsonify(tokens[provider])


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
