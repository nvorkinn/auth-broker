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
non-local deployment (Caddy terminates TLS here; see [Caddy](#caddy)).

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
2. Loop on `GET /api/frame` with `If-None-Match`, handling the status and then
   sleeping for the response's `Retry-After` seconds:
   - 200: draw the frame. `Retry-After` is the device's refresh interval
     (set on `/device`).
   - 304: unchanged. Same `Retry-After`.
   - 202: not matched yet. `Retry-After: 30`.
   - 404: matched, but nothing rendered yet. `Retry-After: 5`.
   - 401: unknown secret (it expired from the pool, or the screen was
     unlinked), so register again.

`/register` answers:

| Code | Meaning | Client should |
|---|---|---|
| 201 / 200 | `{device_id}`: a new device, or a repeat of a registered secret | start polling |
| 202 | waiting in the pool (`Retry-After: 30`) | poll `/api/frame` or `/api/config`, not `/register` |
| 400 | bad role or secret, or a standalone display | fix the request; a firmware bug |
| 409 | secret already registered with the other role | generate a new secret |
| 429 | over 5 calls a minute from this IP | wait `Retry-After` (lockouts double, up to 1h) |
| 503 | pool full | wait `Retry-After` (60s) |

A renderer polls `GET /api/config` the same way. Once matched, that response
carries its `device_id` and a `pairing_code` to draw. From then on it can use
the ID-less routes for everything (`PUT /api/frame`, `/api/spotify/...`), so it
never has to handle its `device_id`. The broker still logs each request with
the `device_id` it found from the secret. Renderers are started by
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
| `GET /api/spotify/<now-playing \| queue \| top/<artists\|tracks>>`, and the same under `/api/devices/<id>/` | renderer |
| `POST /api/devices/<id>/pairing-code` | renderer |
| `PUT /api/frame`, `PUT /api/frames/<id>/frame` (raw 800x480 1-bit, 48000 bytes) | renderer |
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
FLASK_DEBUG=1 python wsgi.py  # debug mode only when asked for; never on a reachable host
```

Then, in another terminal, simulate a Pi and walk through pairing yourself:

```bash
python scripts/simulate_device.py
# prints a device_id and a pairing code
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
`.github/workflows/release.yml`, which builds two Docker images and pushes
each with the release tag (and `:latest`):

- `ghcr.io/nvorkinn/auth-broker:<tag>`: the broker itself (`Dockerfile`).
- `ghcr.io/nvorkinn/auth-broker-caddy:<tag>`: [Caddy](https://caddyserver.com/)
  with this repo's `caddy/Caddyfile` baked in. It terminates TLS for
  `auth.nikolaivorkinn.com` and reverse-proxies to the broker.

Deploying needs Docker Compose v2 (the `docker compose` subcommand). Ubuntu's
`docker.io` package doesn't include it; install it with
`sudo apt install docker-compose-v2`.

On the Oracle Cloud instance, copy `docker-compose.yml` into
`/opt/auth-broker/` next to the `.env` file and `data/` directory. Caddy reads its
own secrets from `caddy.env` in the same directory, kept apart from the broker's
`.env`: create it from `caddy.env.example` first, or the `caddy` service won't
start. Pin both
versions in that `.env` (compose reads it from there) rather than tracking
`:latest`, so a restart never picks up a release you didn't choose. They're
released together, so they're normally the same tag:

```bash
AUTH_BROKER_VERSION=v1.8.0
CADDY_VERSION=v1.8.0
```

Then:

```bash
cd /opt/auth-broker
docker compose up -d
```

`up -d` pulls the images and recreates a container only if its image or
config changed, so the same command handles first deploy, upgrades, and picking
up `.env` changes. Without a version it runs `:latest`. To touch only one
service, name it: `docker compose up -d caddy`.

### Deploying a release from GitHub Actions

After the images are pushed, `release.yml` has two deploy jobs, `deploy-caddy`
and `deploy-config`. Each one waits, paused, until someone approves it on the
workflow run page, and each is approved on its own. An approved job SSHes into
the host and runs `scripts/deploy.sh`, which:

1. for the config server, backs up the database to
   `data/broker.db.bak-<tag>` (SQLite's backup API, safe while running),
2. pins `AUTH_BROKER_VERSION` or `CADDY_VERSION` in `/opt/auth-broker/.env` to
   the release's tag (never `latest`),
3. pulls that image and recreates just that container
   (`docker compose up -d --no-deps --force-recreate <service>`).

`deploy-config` then lists the devices, as a check that the database came
through. Re-running a job redeploys the same tag; to roll back, re-run the
deploy jobs of the older release's workflow run.

One-time setup in the repo's **Settings**:

- **Environments**: create `deploy-caddy` and `deploy-config`, and on each tick
  **Required reviewers** and add yourself. That rule is what keeps the jobs
  from running on their own. Optionally limit each to `v*` tags under
  **Deployment branches and tags**.
- **Secrets**, either on both environments (so only an approved deploy can
  read them) or once under **Secrets and variables > Actions**:
  - `DEPLOY_HOST`: the instance's public IP or hostname.
  - `DEPLOY_USER`: the SSH user, e.g. `ubuntu`. It must be able to run
    `docker` (in the `docker` group) and write `/opt/auth-broker/.env`.
  - `ORACLE_SSH_TOKEN`: a private key, made just for this
    (`ssh-keygen -t ed25519 -f deploy -N ''`), whose public half is in that
    user's `~/.ssh/authorized_keys`.
  - `DEPLOY_SSH_KNOWN_HOSTS`: the output of `ssh-keyscan <host>`, so the job
    only talks to your host.

The host still needs the one-time setup above (compose file, `.env`, and the
move off the systemd Caddy) before the first automated deploy. The jobs don't
copy `docker-compose.yml`; copy it by hand when it changes.

To upgrade by hand instead:

```bash
cd /opt/auth-broker
cp data/broker.db data/broker.db.bak-$(date +%F)   # 1. back up the database
# 2. set AUTH_BROKER_VERSION and CADDY_VERSION in .env to the new tag
docker compose pull                                # 3. fetch the new images
docker rm -f auth-broker                           # 4. only if it was started by hand with `docker run`
docker compose up -d                               # 5. start the new version
docker exec auth-broker flask --app wsgi devices list   # 6. check every device is still there
```

The `./data` volume mount is what makes device/token data (SQLite at `data/broker.db`,
override with `BROKER_DB_PATH`) survive a container restart or image update —
back that directory up.

### Caddy

Caddy runs as the `caddy` compose service, publishing ports 80 and 443 (TCP,
plus UDP 443 for HTTP/3) on the host. It reaches the broker over the compose
network as `auth-broker:5000`; the broker's `127.0.0.1:5000` mapping stays
for local debugging. A Caddyfile change ships like code: edit
`caddy/Caddyfile`, tag a release, bump `CADDY_VERSION`.

Its certificates and ACME account live in the named volume `caddy_data`
(config state in `caddy_config`). Never replace those with throwaway storage
or remove them (`docker compose down -v` would): Caddy would re-request every
certificate on start, and Let's Encrypt rate-limits repeated issuance for the
same domain. The volumes have fixed names, so they survive moving or renaming
`/opt/auth-broker`. `docker compose down` without `-v` keeps them.

#### Moving from the systemd Caddy

Caddy used to run on the host under systemd. To switch over once, keeping the
existing certificates so nothing is re-issued:

```bash
cd /opt/auth-broker
docker compose pull caddy                                   # 1. fetch the image first, to keep downtime short
docker compose create caddy                                 # 2. create the container and its volumes, unstarted
sudo systemctl disable --now caddy                          # 3. free ports 80/443
sudo docker run --rm -v caddy_data:/data \
  -v /var/lib/caddy/.local/share/caddy:/src:ro \
  alpine sh -c 'mkdir -p /data/caddy && cp -a /src/. /data/caddy/'   # 4. copy the host's certificates into the volume
docker compose up -d                                        # 5. start Caddy in its container
docker compose logs caddy                                   # 6. check it loaded the certificate rather than requesting one
```

`/var/lib/caddy/.local/share/caddy` is where the Debian/Ubuntu `caddy`
package keeps its data; check `systemctl cat caddy` if yours differs. If the
copy is skipped, Caddy simply requests a fresh certificate once, which is
fine. To roll back, `docker compose stop caddy && sudo systemctl enable --now caddy`.
Once happy, `sudo apt remove caddy` (its old data directory can stay as a backup).

If the GHCR package `auth-broker-caddy` comes up private after the first
release, give it the same visibility as the `auth-broker` package in its package settings, or
`docker login ghcr.io` on the host.

### Logs

The compose file also runs Fluent Bit (`fluent-bit/`), which tails every
container's Docker log and ships it to VictoriaLogs on the host
(`127.0.0.1:9428`). Copy the `fluent-bit/` directory to `/opt/auth-broker/`
alongside `docker-compose.yml`, then `docker compose up -d`. Query in
VictoriaLogs with e.g. `{container_id="abc123def456"}`; `docker ps` maps IDs to names.

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
