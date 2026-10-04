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

Every device-facing endpoint is authenticated with `Authorization: Bearer
<device_secret>`; every browser-facing page is gated on the signed session
cookie set at pairing time.

## Code layout

```
broker/
  routes/     Flask blueprints: read the request, call a service or client, return a response
  services/   the broker's own logic that more than one route needs (pairing)
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
start, use the admin CLI inside the container; it has no network surface:

```bash
docker compose exec auth-broker flask --app wsgi devices list
docker compose exec auth-broker flask --app wsgi devices code <device_id>             # a fresh code, e.g. for a new browser
docker compose exec auth-broker flask --app wsgi devices unpair <device_id>            # show a pairing code again
docker compose exec auth-broker flask --app wsgi devices unpair <device_id> --config   # ...and reset its settings and linked accounts
docker compose exec auth-broker flask --app wsgi devices forget <device_id>            # delete it; it must register again
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

On the Oracle Cloud instance, copy `docker-compose.yml` into
`/opt/auth-broker/` next to the `.env` file and `data/` directory, then:

```bash
cd /opt/auth-broker
docker compose up -d                              # deploy/upgrade to :latest
AUTH_BROKER_VERSION=v1.2.0 docker compose up -d   # ...or pin a specific tag
```

`up -d` always pulls the image first and recreates the container only if the
image or config changed, so the same command handles first deploy, upgrades,
and picking up `.env` changes. To pin a version permanently, add
`AUTH_BROKER_VERSION=v1.2.0` to that `.env` (compose reads it from there too).
If a container was previously started by hand with `docker run`, remove it
once (`docker rm -f auth-broker`) before the first `docker compose up`.

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
- Any provider tokens beyond Spotify and Glowmarkt (a future per-user token
  would follow the same shape: its own table, proxied the same way through
  the device-facing API).
