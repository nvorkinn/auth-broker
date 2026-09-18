# auth-broker

Small cloud-hosted service that handles the parts of OAuth that need a public
HTTPS endpoint, so that [countdown](https://github.com/nvorkinn/countdown)
(running on a Raspberry Pi on a local network with no TLS) doesn't have to.

## Why this exists

Spotify's OAuth flow requires the redirect URI to be either HTTPS, or the
literal loopback address `127.0.0.1`. A Pi on the LAN, reached from a phone or
laptop at an address like `192.168.1.50`, doesn't qualify for either — so the
one-time "click allow" step needs to happen somewhere with real TLS.

Refreshing an already-issued token afterwards is just an outbound HTTPS POST
from the Pi to Spotify's token endpoint — that doesn't need inbound TLS at
all. So this service only handles the one-time handshake:

1. `GET /auth/spotify/login` — redirects to Spotify's consent screen.
2. `GET /auth/spotify/callback` — Spotify redirects back here with a code;
   this exchanges it for a refresh token and stores it.
3. `GET /api/tokens/<provider>` — the Pi calls this once (bearer-auth'd) to
   fetch the refresh token, then owns refreshing it locally from then on.

The countdown app is expected to keep doing its own token refresh — this
service is not in the hot path for normal operation, only initial setup or a
forced re-auth.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in Spotify app credentials + a random BROKER_API_KEY
export $(cat .env | xargs)
python app.py
```

## Deploying

Runs behind [Caddy](https://caddyserver.com/) for automatic TLS — see the
included `Caddyfile`. Point `auth.nikolaivorkinn.com` at the Oracle Cloud
instance, run this app under something like `gunicorn` or `systemd`, and let
Caddy terminate TLS and reverse-proxy to it on `127.0.0.1:5000`.

## Next steps / not built yet

- Hosting the config-management site (currently a local Flask page in
  `countdown`) on the same domain — deliberately left out for now until its
  shape is decided.
- Support for additional provider tokens (e.g. TfL) beyond Spotify.
