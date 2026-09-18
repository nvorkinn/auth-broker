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

## Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest --cov=broker --cov-report=term-missing
```

External calls (Spotify, TfL) are mocked in tests — nothing hits the network.
`.github/workflows/tests.yml` runs this on every push/PR to `main`. (GitHub's
native code-coverage-on-PRs feature would be nicer than a log to scroll
through, but it's gated behind a paid Code Quality plan we tried and
confirmed 404s on this account — worth revisiting if that ever changes.)

## Deploying

Tagging a release (`git tag v0.1.0 && git push --tags`) triggers
`.github/workflows/release.yml`, which builds this into a Docker image and
pushes it to `ghcr.io/nvorkinn/auth-broker:<tag>` (and `:latest`).

On the Oracle Cloud instance:

```bash
docker run -d --name auth-broker --restart unless-stopped \
  -p 127.0.0.1:5000:5000 \
  -v /opt/auth-broker/data:/app/data \
  --env-file /opt/auth-broker/.env \
  ghcr.io/nvorkinn/auth-broker:latest
```

The `-v` mount is what makes device/token data (SQLite at `data/broker.db`,
override with `BROKER_DB_PATH`) survive a container restart or image update —
back that directory up. [Caddy](https://caddyserver.com/) still runs directly
on the host for TLS — see the included `Caddyfile` — reverse-proxying
`auth.nikolaivorkinn.com` to `127.0.0.1:5000`, which Docker's `-p` mapping
above publishes to, so no Caddy config changes needed when moving to a new
image tag.

## Not built yet

- The Pi-side migration in `countdown` — swapping `spotify_client.py`'s direct
  `spotipy` calls, and the local Flask config page, for calls to this
  service's device-facing API. Left for a follow-up so this could be stood
  up and tested on its own first.
- Any provider tokens beyond Spotify and Glowmarkt (a future per-user token
  would follow the same shape: its own table, proxied the same way through
  the device-facing API).
