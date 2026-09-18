# auth-broker

Cloud-hosted companion service for [countdown](https://github.com/nvorkinn/countdown),
a gifted Raspberry Pi e-paper display. Each recipient's Pi sits on their own
home WiFi with no shared network or stable address, and each has their own
Spotify account — so the one-time OAuth handshake (which needs a public HTTPS
redirect URI) can't happen on the device itself. This service is the one
public HTTPS endpoint that handles that handshake, hosts the recipient-facing
config site, and brokers ongoing Spotify calls so no device ever has to hold
a Spotify token.

## How a device gets set up

1. **First boot:** the Pi generates its own `device_id`/`device_secret` pair
   and calls `POST /api/devices/register` once. The server only ever stores a
   hash of the secret; the Pi is the only place the plaintext secret lives.
2. **Pairing:** the Pi asks for a short-lived code (`POST
   /api/devices/<id>/pairing-code`) and shows it on its screen. The recipient
   goes to the site, enters the code at `/pair`, and that binds their browser
   session to that device for `/device` (settings) and Spotify linking.
3. **Spotify:** from `/device`, "Connect Spotify" kicks off the OAuth flow.
   The server exchanges the code for tokens and keeps them — the Pi never
   sees a Spotify token, only its own device secret.
4. **Ongoing:** the Pi polls `GET /api/devices/<id>/config` (device settings +
   shared app-level API keys like the TfL/weather ones) and `GET
   /api/devices/<id>/now-playing` (server refreshes the Spotify token
   server-side and proxies back just the now-playing payload, shaped to match
   `SpotifyClient.get_current_track()` in `countdown` so the Pi-side swap is a
   drop-in).

Every device-facing endpoint is authenticated with `Authorization: Bearer
<device_secret>`; every browser-facing page is gated on the signed session
cookie set at pairing time.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in Spotify app credentials + a random FLASK_SECRET_KEY
export $(cat .env | xargs)
python wsgi.py
```

Then, in another terminal, simulate a Pi and walk through pairing yourself:

```bash
python scripts/simulate_device.py
# prints a device_id, device_secret, and a pairing code
# -> open http://127.0.0.1:5000/pair and enter the code
```

## Deploying

Runs behind [Caddy](https://caddyserver.com/) for automatic TLS — see the
included `Caddyfile`. Point `auth.nikolaivorkinn.com` at the Oracle Cloud
instance, run this app under `gunicorn` (or similar) via `systemd`, and let
Caddy terminate TLS and reverse-proxy to `127.0.0.1:5000`. Data lives in a
single SQLite file (`data/broker.db` by default, override with
`BROKER_DB_PATH`) — back that file up.

## Not built yet

- The Pi-side migration in `countdown` — swapping `spotify_client.py`'s direct
  `spotipy` calls, and the local Flask config page, for calls to this
  service's device-facing API. Left for a follow-up so this could be stood
  up and tested on its own first.
- Any provider tokens beyond Spotify (e.g. a future per-user token would
  follow the same shape: its own blueprint, its own table, proxied the same
  way through the device-facing API).
