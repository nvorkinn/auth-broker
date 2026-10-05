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

1. **First boot:** the Pi generates its own secret and calls
   `POST /api/devices/register` once, getting back its `device_id`. The server only ever stores a
   hash of the secret; the Pi is the only place the plaintext secret lives.
2. **Pairing:** the Pi polls `GET /api/devices/<id>/config`. While the
   device is unpaired, the response carries a short-lived `pairing_code` (the
   server issues a fresh one whenever the last expires), which the Pi shows on
   its screen. The recipient enters it at `/pair`, which binds their browser
   session to that device for `/device` (settings) and Spotify linking, and
   marks the device paired (`devices.paired_at`): from then on `pairing_code`
   is `null`. `setup_missing` in the same response lists what the device still
   needs before it's worth showing (a weather location, a postcode, a bus or tube stop),
   so the Pi can tell the recipient. To link another browser to an
   already-paired device (or get back in after the 30-day session expires),
   get a fresh code: "Link another browser" on `/device` from a browser that's
   still paired, `flask devices code <device_id>` (below), or the Pi itself via
   `POST /api/devices/<id>/pairing-code`. Any live code also appears on the
   Pi's screen until it expires or is used.
3. **Spotify:** from `/device`, "Connect Spotify" kicks off the OAuth flow.
   The server exchanges the code for tokens and keeps them — the Pi never
   sees a Spotify token, only its own device secret.
4. **Ongoing:** the Pi polls `GET /api/devices/<id>/config` (device settings +
   shared app-level API keys like the TfL/weather ones) and `GET
   /api/devices/<id>/now-playing` (server refreshes the Spotify token
   server-side and proxies back just the now-playing payload, shaped to match
   `SpotifyClient.get_current_track()` in `countdown` so the Pi-side swap is a
   drop-in). `GET /api/devices/<id>/queue` works the same way and returns the
   upcoming Spotify queue as a JSON list of the same track shape.
   These Spotify routes (and `top/<artists|tracks>`) are also served under
   `/api/spotify/<id>/...`, where they're moving; the `/api/devices/` paths
   stay until the Pis have switched.

Every device-facing endpoint is authenticated with `Authorization: Bearer
<secret>`; every browser-facing page is gated on the signed session
cookie set at pairing time. Bearer secrets mean HTTPS is required for any
non-local deployment (Caddy terminates TLS here).

## Renderer and display roles

A device has up to two secrets, one per role:

- **renderer**: whatever draws the frames. On a standalone Pi that's the Pi
  itself; in a split deployment it's a renderer process on the server.
- **display**: a thin screen (e.g. an ESP32) that only fetches finished frames.

**The secret is the device's identity.** Each client generates its own long
random secret and sends it, never its hash, as `Authorization: Bearer
<secret>` on every request. The broker stores only its SHA-256, which is
unique per device, so the secret alone finds the device. The routes shaped
`/api/devices/<id>/...` and `/api/frames/<id>/frame` keep working too, and
then the secret must belong to that device. Secrets registered before this
change are werkzeug hashes; each is rewritten as SHA-256 the first time its
Pi authenticates on a path route.

### Registering

`POST /api/devices/register` is safe to call again with the same secret,
e.g. on every boot. A repeat returns 200 `{device_id}` once the client is
matched, or 202 while it's still waiting.

- **Standalone Pi:** `{"role": "renderer", "standalone": true, "secret": ...}`
  becomes a device at once (201 `{device_id}`). The old body,
  `{"device_secret": ...}`, means the same.
- **Split deployment:** a renderer and a screen each send
  `{"role": "renderer" | "display", "secret": ...}`, in either order. Each
  waits in a pending pool (202) until a client of the other role arrives.
  The broker then matches the longest-waiting one into a new device (201
  `{device_id}` to whoever completed the match).

A screen therefore needs no logic beyond:

1. On boot: `POST /api/devices/register {"role": "display", "secret": ...}`.
2. Loop on `GET /api/frame` with `If-None-Match`:
   - 200: draw the frame.
   - 304: unchanged.
   - 202: not matched yet.
   - 404: matched, but nothing rendered yet.
   - 401: unknown secret (it expired from the pool, or the screen was
     unlinked), so register again.

A renderer polls `GET /api/config` the same way. Once matched, that response
carries its `device_id` and a `pairing_code` to draw, and the renderer then
uses the path routes for Spotify and frame PUTs. Renderers are started by
hand for now: `flask devices pending` (below) shows a screen waiting for one.

The pool is open to anyone, so it's bounded:
- `/register` allows 5 calls a minute per IP, every call counting (429 with
  `Retry-After`, lockouts doubling like `/pair`).
- At most 20 clients may wait at once (503).
- A client unmatched after 24 hours is dropped.

The matching runs in one transaction that starts with a write, so SQLite's
single-writer lock makes it safe across threads, workers and the CLI.

### Which role may call what

| Route | Roles |
|---|---|
| `GET /api/config`, `GET /api/devices/<id>/config` | renderer |
| `POST /api/devices/<id>/pairing-code`, Spotify routes | renderer |
| `PUT /api/frames/<id>/frame` (raw 800x480 1-bit, 48000 bytes) | renderer |
| `GET /api/frame`, `GET /api/frames/<id>/frame` (ETag / `If-None-Match` → 304) | renderer, display |

A wrong or missing secret gets 401. A valid secret for the wrong role gets
403. A secret still waiting in the pool gets 202 on the ID-less routes.

"Unlink screen" on `/device` revokes the screen's secret. Bringing a
replacement screen back to an existing device isn't built yet: registering
it puts it in the pool as a new device.

## Monitoring devices

`GET /api/status/devices` is a read-only view of every device, meant for a
Home Assistant RESTful sensor. HA polls it from the home network, so it also
covers devices on other people's Wi-Fi, which MQTT can't reach. It's off
(404) unless `STATUS_TOKEN` is set in `.env`, and then needs
`Authorization: Bearer <STATUS_TOKEN>`. It only reads: resetting or deleting a
device stays with the admin CLI.

The response is keyed by device id:

```json
{
  "c30dfc0f0f92": {
    "device_name": "camilla",
    "created_at": "2026-09-01T10:00:00+00:00",
    "paired_at": "2026-09-01T10:05:00+00:00",
    "last_seen_at": "2026-10-05T18:01:00+00:00",
    "setup_missing": []
  }
}
```

`last_seen_at` is when the device last made any authenticated request (config,
Spotify, frame). It's updated at most once a minute, and is `null` until the
device's first request after this was added. `setup_missing` is the same list
the config poll returns, or `null` if the device has no settings row.

A sensor for one device's last check-in:

```yaml
rest:
  - resource: https://auth.nikolaivorkinn.com/api/status/devices
    headers:
      Authorization: !secret auth_broker_status_token   # "Bearer <STATUS_TOKEN>"
    scan_interval: 300
    sensor:
      - name: Countdown last seen
        value_template: "{{ value_json['c30dfc0f0f92'].last_seen_at }}"
        device_class: timestamp
```

## Code layout

```
broker/
  routes/     Flask blueprints: read the request, call a service or client, return a response
  services/   the broker's own logic that more than one route needs (pairing, registration, setup)
  clients/    everything that talks to an outside API (Spotify, TfL, Open-Meteo)
  models.py   the schema; db.py and migrations/ manage it
  auth.py     the decorators that guard routes
  config.py   environment variables → app.config
  cli.py      `flask devices ...` admin commands
```

`tests/` mirrors this layout.

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

## Resetting a device while developing

The database is a SQLite file (`data/broker.db`). To replay pairing from the
start, use the admin CLI inside the container; it has no network surface.
These use plain `docker exec` (the container is always named `auth-broker`), so
they work wherever the container runs, Compose or not:

```bash
docker exec auth-broker flask --app wsgi devices list
docker exec auth-broker flask --app wsgi devices code <device_id>              # a fresh code, e.g. for a new browser
docker exec auth-broker flask --app wsgi devices unpair <device_id>            # show a pairing code again
docker exec auth-broker flask --app wsgi devices unpair <device_id> --config   # ...and reset its settings and linked accounts
docker exec auth-broker flask --app wsgi devices forget <device_id>            # delete it; it must register again
docker exec auth-broker flask --app wsgi devices pending                       # renderers/screens waiting to be matched
```

`unpair` keeps the device's identity, so the Pi just shows a new code on its
next poll. After `forget`, also make the Pi register as a new device:

```bash
sudo rm /opt/countdown/.auth_broker_device && sudo systemctl restart countdown
```

When the `paired_at` column is first added to an existing database, devices
with an unredeemed code stay unpaired and every other device is marked paired.

## Changing the database schema

Tables are SQLAlchemy models in `broker/models.py`; migrations live in
`broker/migrations/versions/` and are managed with
[Flask-Migrate](https://flask-migrate.readthedocs.io/) (Alembic). To change the schema:

```bash
# 1. edit broker/models.py
# 2. generate a migration from the difference (needs your .env loaded, as it builds the app)
flask --app wsgi db migrate -m "add nickname to devices"
# 3. read the generated file in broker/migrations/versions/ -- autogenerate misses some
#    things (renames look like drop + add) and never backfills data -- then commit it
```

The app runs any pending migrations itself on startup, so deploying the new
image is all a migration needs. That assumes a single gunicorn worker (the
current default): with several, move the upgrade into a step that runs once
before gunicorn starts. A database from before migrations existed is adopted
by the first migration on its first start, keeping its data.
`test_migrations_match_the_models` fails if a model changes without a migration.

## Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest --cov=broker --cov-report=term-missing
```

Lint and format checks (config in `ruff.toml`, also run in CI):

```bash
ruff check .          # add --fix to auto-fix
ruff format .         # use --check to verify without changing files
```

External calls (Spotify, TfL) are mocked in tests — nothing hits the network.
`.github/workflows/tests.yml` runs this on every push/PR to `main` and uploads
`coverage.xml` to [Codecov](https://app.codecov.io/gh/nvorkinn/auth-broker),
which posts a coverage comment and status check on each PR (config in
`codecov.yml`). The upload needs a `CODECOV_TOKEN` repository secret.

## Deploying

Tagging a release (`git tag v0.1.0 && git push --tags`) triggers
`.github/workflows/release.yml`, which builds this into a Docker image and
pushes it to `ghcr.io/nvorkinn/auth-broker:<tag>` (and `:latest`).

Deploying needs Docker Compose v2 (the `docker compose` subcommand). Ubuntu's
`docker.io` package doesn't include it; install it with
`sudo apt install docker-compose-v2`.

On the Oracle Cloud instance, copy `docker-compose.yml` into
`/opt/auth-broker/` next to the `.env` file and `data/` directory. Pin the
version in that `.env` (compose reads it from there) rather than tracking
`:latest`, so a restart never picks up a release you didn't choose:

```bash
AUTH_BROKER_VERSION=v1.8.0
```

Then:

```bash
cd /opt/auth-broker
docker compose up -d
```

`up -d` pulls the image and recreates the container only if the image or
config changed, so the same command handles first deploy, upgrades, and picking
up `.env` changes. Without `AUTH_BROKER_VERSION` it runs `:latest`.

To upgrade:

```bash
cd /opt/auth-broker
cp data/broker.db data/broker.db.bak-$(date +%F)   # 1. back up the database
# 2. set AUTH_BROKER_VERSION in .env to the new tag
docker compose pull                                # 3. fetch the new image
docker rm -f auth-broker                           # 4. only if it was started by hand with `docker run`
docker compose up -d                               # 5. start the new version
docker exec auth-broker flask --app wsgi devices list   # 6. check every device is still there
```

The `./data` volume mount is what makes device/token data (SQLite at `data/broker.db`,
override with `BROKER_DB_PATH`) survive a container restart or image update —
back that directory up. [Caddy](https://caddyserver.com/) still runs directly
on the host for TLS — see the included `Caddyfile` — reverse-proxying
`auth.nikolaivorkinn.com` to `127.0.0.1:5000`, which the compose
file's `ports` mapping publishes to, so no Caddy config changes needed when moving to a new
image tag.

## Not built yet

- The Pi-side migration in `countdown` — swapping `spotify_client.py`'s direct
  `spotipy` calls, and the local Flask config page, for calls to this
  service's device-facing API. Left for a follow-up so this could be stood
  up and tested on its own first.
- Bringing a replacement screen back to an existing device: a short-lived
  link code from `/device` that a registering screen sends to join that
  device instead of the pool.
- A supervisor that keeps an idle renderer waiting in the pool, instead of
  starting each one by hand.
- Any provider tokens beyond Spotify and Glowmarkt (a future per-user token
  would follow the same shape: its own table, proxied the same way through
  the device-facing API).
