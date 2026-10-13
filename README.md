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
2. **Pairing:** a device is paired with an Authentik user, who becomes its owner
   (`devices.owner`, the user id the login proxy passes in `X-Authentik-Uid`) and
   sees its settings at `/device` after logging in. A screen is a device from its first
   `POST /api/devices/register`, so it can be paired before any renderer exists. A new screen's
   owner is sent to `/device/name` first to name it (see [Naming a screen](#naming-a-screen)).
   There are two ways in:
   - **Token (main path).** After WiFi setup the device redirects its owner's
     browser to `/pair#<token>`, where `token` is the hex SHA-256 of
     `"pairing:" + hex SHA-256 of the device secret`. It's one-way, so it's safe
     in a URL, and the fragment never reaches a server log (`/pair?t=<token>` also
     works). The broker stores it when the device registers, so the device sends
     nothing extra. The page logs the user in, then waits (polling
     `POST /pair/token`) until the device has registered, which is normally
     seconds, and goes to `/device`. It gives up after 90 seconds and offers the
     code instead. Someone else's device is refused (409).
   - **Code (fallback).** The Pi polls `GET /api/config`; while unpaired, the
     response carries a short-lived `pairing_code` (a fresh one whenever the last
     expires) for its screen. The user enters it at `/pair`. A code also hands a
     device to a different user, since it proves they're at the device: get a
     fresh one with `flask devices code <device_id>` (below) or from the Pi via
     `POST /api/devices/pairing-code`.

   Once paired, `pairing_code` is `null`. `setup_missing` in the same response
   lists what the device still needs before it's worth showing (a weather
   location, a postcode, a bus or tube stop), so the Pi can tell the recipient.
   A user can own several screens. Each has its own pages at `/device/<id>` (settings),
   `/device/<id>/name`, `/device/<id>/spotify/disconnect` and `/device/<id>/display/unlink`, and the
   same paths without the id (`/device`, ...) are for the one paired most recently. A screen that isn't
   theirs is a 404. Every page shows a switcher with their screens, named by the slug, and a link to pair
   another. "Connect Spotify" is started for one screen (`/auth/spotify/login?device=<id>`), and the
   screen it was started for is remembered through the round trip to Spotify. Devices paired
   before owners existed need `flask devices owner <device_id> <user id>`.
   While a screen has no weather location, `/device` prefills the field with the biggest town or city
   near where the visitor's IP is, marked as a suggestion; it's stored only when they save. The lookup
   is local, so no IP is sent anywhere: DB-IP's [IP to City Lite](https://db-ip.com/db/download/ip-to-city-lite)
   database and [GeoNames](https://www.geonames.org)' towns of over 15,000 people (both CC BY 4.0) are
   fetched into the image when it's built (`scripts/fetch_geodata.py`), so they're as fresh as the
   release. Running locally without them there's no suggestion; run
   `python scripts/fetch_geodata.py geodata` for one.
3. **Spotify:** from `/device`, "Connect Spotify" kicks off the OAuth flow.
   The server exchanges the code for tokens and keeps them — the Pi never
   sees a Spotify token, only its own device secret.
4. **Ongoing:** the Pi polls `GET /api/config` (device settings +
   shared app-level API keys like the TfL/weather ones) and `GET
   /api/spotify/now-playing` (server refreshes the Spotify token
   server-side and proxies back just the now-playing payload, shaped to match
   `SpotifyClient.get_current_track()` in `countdown` so the Pi-side swap is a
   drop-in). `GET /api/spotify/queue` works the same way and returns the
   upcoming Spotify queue as a JSON list of the same track shape.
   `GET /api/spotify/top/<artists|tracks>` is served the same way.

Every browser page shows the signed-in user's username at the top, from the
`X-Authentik-Username` header the login proxy sets next to `X-Authentik-Uid` (set
`AUTH_USERNAME_HEADER` to change the name). It is only shown and used to name their
screens; the user's identity stays the uid.

Every device-facing endpoint is authenticated with `Authorization: Bearer
<secret>`; every browser-facing page needs an Authentik login: Caddy
forward-auths them and sets `X-Authentik-Uid` (setting `AUTH_USER_HEADER`
changes the name), which the broker trusts, so the port must stay on loopback. Bearer secrets mean HTTPS is required for any
non-local deployment (Caddy terminates TLS here; see [Caddy](#caddy)).

## Renderer and display

A device has up to two secrets, one per role:

- **renderer**: whatever draws the frames. A renderer process on the server, started by the
  orchestrator.
- **display**: a thin screen (e.g. an ESP32) that only fetches finished frames.

**The secret is the device's identity.** Each client generates its own long
random secret and sends it, never its hash, as `Authorization: Bearer
<secret>` on every request. The broker stores only its SHA-256, which is
unique per device, so the secret alone finds the device.

### Registering

A screen and a renderer register on different routes, since they do very different things.

**Screen: `POST /api/devices/register {"secret": ..., "width": ..., "height": ...}`.** Safe to call again with the same secret,
e.g. on every boot. It becomes a device at once (201 `{device_id}`; a repeat is 200) with no renderer.
Its owner pairs with it and names it, which asks the orchestrator for a renderer (see
[Naming a screen](#naming-a-screen)). Anyone can call it, so it is throttled.

**Renderer: `POST /api/renderers/claim {"secret": ..., "claim_token": ...}`.** From a renderer the
orchestrator started. The token belongs to one assignment and is single-use; the secret is attached to
that assignment's device (201 `{device_id}`) and the token is spent. A repeat with the same secret
(e.g. after a restart) is 200. The unguessable token is the protection, so this route is not throttled.
There is no other way to get a renderer: one started by hand has no assignment to claim.

A screen therefore needs no logic beyond:

1. On boot: `POST /api/devices/register {"secret": ..., "width": ..., "height": ...}`
   (201 straight away; the screen has a device even before its renderer runs).
2. Loop on `GET /api/frame` with `If-None-Match`, handling the status and then
   sleeping for the response's `Retry-After` seconds:
   - 200: draw the frame. `Retry-After` is the device's refresh interval
     (set on `/device`).
   - 304: unchanged. Same `Retry-After`.
   - 404: no frame yet (its renderer is still being set up). `Retry-After: 5`.
   - 401: unknown secret (an unpaired screen is forgotten after 24 hours, or the
     screen was unlinked), so register again.

`/register` answers:

| Code | Meaning | Client should |
|---|---|---|
| 201 / 200 | `{device_id}`: a new device, or a repeat of a registered secret | start polling |
| 400 | bad secret | fix the request; a firmware bug |
| 409 | secret already registered as a renderer | generate a new secret |
| 429 | over 5 calls a minute from this IP | wait `Retry-After` (lockouts double, up to 1h) |
| 503 | too many unpaired screens | wait `Retry-After` (60s) |

`/api/renderers/claim` answers 201 / 200 `{device_id}`, 400 for a bad secret or no `claim_token`,
403 for an unknown or spent token, and 409 for a secret already registered as a screen.

A renderer polls `GET /api/config`, which carries its `device_id` and a `pairing_code` to draw. From
then on it can use the ID-less routes for everything (`PUT /api/frame`, `/api/spotify/...`), so it
never has to handle its `device_id`. The broker still logs each request with
the `device_id` it found from the secret. A screen can also ask for a pairing code
(`POST /api/devices/pairing-code`), which a screen with no renderer yet has nothing
else to show.

What anyone can add is bounded:
- `/register` allows 5 calls a minute per IP, every call counting (429 with
  `Retry-After`, lockouts doubling like `/pair`).
- At most 20 screens may sit unpaired with no renderer or assignment at once (503).
- A screen unpaired after 24 hours is dropped.

### Naming a screen

A new screen's owner is sent to `/device/name` right after pairing, because the
screen's renderer is started per screen and named after it. They pick the end of a
**slug**; the broker puts their username in front, as `<username>-<their choice>`
(the username lowercased, anything but `a-z 0-9 _ -` turned into a dash, and cut to
16 characters, or `user` if nothing is left). The whole slug is at most 32
characters (`[a-z0-9][a-z0-9_-]{1,31}`), unique across the broker, and fixed once
set, even if their username changes later. It creates a *renderer assignment* (also
`POST /api/displays/<device_id>/renderer {"slug": ...}` for a signed-in owner, where
the slug is again just their part: 201 with the full slug, 400 bad slug, 404 not
theirs, 409 taken or already assigned). `/device` shows nothing else until then. The slug lives in the
broker, not Authentik: it belongs to a screen, and a user can have several.

The [orchestrator](https://github.com/nvorkinn/pi-dashboard/blob/main/countdown/orchestrator/README.md)
polls the assignments with `Authorization: Bearer <ORCHESTRATOR_TOKEN>`
(`secrets/broker.env`; unset keeps these endpoints off with a 404, and Caddy sends
`/api/orchestrator/*` past the login):

- `GET /api/orchestrator/assignments`: `{"assignments": [{"id", "slug", "claim_token", "status"}]}`.
  `status` is `pending`, `provisioned`, `suspended`, `failed` or `deleting`. `claim_token` is
  `null` once the renderer has claimed it.
- `POST /api/orchestrator/assignments/<id>/status` `{"status": "provisioned" | "failed" | "deleted", "detail": ""}`.
  `deleted` removes the assignment, and is only accepted for one that is `deleting` (409
  otherwise). A `provisioned` or `failed` report for an assignment that has since been suspended or
  queued for deletion is ignored.

The broker owns the suspension policy: when listing, an assignment whose screen
hasn't made a request (`devices.display_last_seen_at`) for 24 hours becomes `suspended`, and
`pending` again once the screen is back. `flask devices forget` marks the device's assignment
`deleting`, which outlives the device until the orchestrator confirms the renderer and its volumes are gone.

### Which role may call what

| Route | Roles |
|---|---|
| `GET /api/config` | renderer |
| `GET /api/spotify/<now-playing \| queue \| top/<artists\|tracks>>` | renderer |
| `POST /api/devices/pairing-code` | renderer |
| `PUT /api/frame` (raw 800x480 1-bit, 48000 bytes) | renderer |
| `GET /api/frame` (ETag / `If-None-Match` → 304) | renderer, display |
| `POST /api/frame` (JSON `{"metrics": "<InfluxDB line protocol>", "logs": [...]}`; answers like the GET) | renderer, display |

`POST /api/frame` is the screen's poll with its report attached. The broker forwards
`metrics` to VictoriaMetrics' `/write` after replacing every literal `{device_id}` in it with the
device's id, and `logs` to Fluent Bit's HTTP input (`127.0.0.1:9880`), both in the background and
best-effort: a destination being down is logged and never fails the poll.

A wrong or missing secret gets 401. A valid secret for the wrong role gets
403.

"Unlink screen" on `/device` revokes the screen's secret. Bringing a
replacement screen back to an existing device isn't built yet: registering
it creates a new device.

## Email (Resend)

Authentik sends two kinds of email: the confirmation link a new user has to follow to finish
signing up (the account stays inactive until then), and the password-recovery link. Both use
Authentik's global email settings, which this repo sets for [Resend](https://resend.com)'s SMTP:
the host, port, username (`resend`), TLS and the From address (`noreply@nikolaivorkinn.com`) are
`AUTHENTIK_EMAIL__*` in `docker-compose.yml`, and the password is Resend's API key,
`AUTHENTIK_EMAIL__PASSWORD` in `secrets/authentik.env`.

To set it up (once):

1. In Resend, add the domain `nikolaivorkinn.com` and create the DNS records it shows (SPF and DKIM,
   and DMARC if you want it) where the domain's DNS is hosted. Mail from the domain lands in spam
   until they verify.
2. Create an API key (sending access is enough) and put it in the encrypted file, without it
   passing through anywhere else: `sops secrets/authentik.env`, add `AUTHENTIK_EMAIL__PASSWORD=<key>`,
   commit the file. **Do it before releasing**: with no password nothing is sent, and no new user
   can finish signing up.
3. After the release is deployed, sign up with a real address and follow the link. To use another
   provider, change the host, port and username in `docker-compose.yml`, and the password.

## Tracing requests (QA)

`LOG_REQUEST_HEADERS` in `docker-compose.yml` (shared by the broker and Caddy through one
setting) turns on logging of every request's headers, off by default. With `"true"`:

- the broker logs one JSON line per request (method, path, query, every header, the
  response's status and `Location`), see `broker/request_log.py`;
- Caddy writes its JSON access log for `auth.` and `sso.`, with the request and
  response headers, which is where each sign-in redirect's `Location` shows.

`Authorization` and `Cookie` values are never logged (Caddy also redacts `Set-Cookie`). Edit the
setting and run `docker compose up -d` to restart both. The pairing token is in the request URL
while it's on.

## Monitoring devices

`GET /api/status/devices` is a read-only view of every device, meant for a
Home Assistant RESTful sensor. HA polls it from the home network, so it also
covers devices on other people's Wi-Fi, which MQTT can't reach. It's off
(404) unless `STATUS_TOKEN` is set in `secrets/broker.env`, and then needs
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
cp secrets/broker.env.example .env  # fill in Spotify app credentials + a random FLASK_SECRET_KEY
export SPOTIFY_REDIRECT_URI=https://auth.nikolaivorkinn.com/auth/spotify/callback
export $(grep -v '^#' .env | xargs)
FLASK_DEBUG=1 python wsgi.py  # debug mode only when asked for; never on a reachable host
```

Then, in another terminal, simulate a screen and walk through pairing yourself:

```bash
python scripts/simulate_device.py
# prints a device_id and a pairing code
# -> open http://127.0.0.1:5000/pair and enter the code
# (or `--token` to try the redirect flow instead, `--secret <random string>` to be the same screen across runs)
```

It works against the deployed broker too, with no hardware
(`python scripts/simulate_device.py https://auth.nikolaivorkinn.com`); there
`/pair` asks for your Authentik login as usual.

## Resetting a device while developing

The database is a SQLite file (`data/broker.db`). To replay pairing from the
start, use the admin CLI inside the container; it has no network surface.
These use plain `docker exec` (the container is always named `auth-broker`), so
they work wherever the container runs, Compose or not:

```bash
docker exec auth-broker flask --app wsgi devices list
docker exec auth-broker flask --app wsgi devices code <device_id>              # a fresh code, e.g. to hand the device to another user
docker exec auth-broker flask --app wsgi devices owner <device_id> <user id>   # pair a device with a user
docker exec auth-broker flask --app wsgi devices unpair <device_id>            # show a pairing code again
docker exec auth-broker flask --app wsgi devices unpair <device_id> --config   # ...and reset its settings and linked accounts
docker exec auth-broker flask --app wsgi devices forget <device_id>            # delete it; it must register again
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
`.github/workflows/release.yml`, which builds five Docker images, side by
side, and pushes each with the release tag (and `:latest`):

- `ghcr.io/nvorkinn/auth-broker:<tag>`: the broker itself (`Dockerfile`).
- `ghcr.io/nvorkinn/auth-broker-caddy:<tag>`: [Caddy](https://caddyserver.com/)
  with this repo's `caddy/Caddyfile` baked in. It terminates TLS for
  `auth.nikolaivorkinn.com` and reverse-proxies to the broker.
- `ghcr.io/nvorkinn/auth-broker-fluent-bit:<tag>`: Fluent Bit with this repo's
  `fluent-bit/` config baked in (see [Logs](#logs)).
- `ghcr.io/nvorkinn/auth-broker-victoria-logs:<tag>`: single-node
  VictoriaLogs, listening on loopback only (see [Logs](#logs)).
- `ghcr.io/nvorkinn/auth-broker-victoria-metrics:<tag>`: single-node
  VictoriaMetrics, listening on loopback only (see [Metrics](#metrics)).

Deploying needs Docker Compose v2 (the `docker compose` subcommand). Ubuntu's
`docker.io` package doesn't include it; install it with
`sudo apt install docker-compose-v2`.

On the Oracle Cloud instance, `/opt/auth-broker/` holds `docker-compose.yml`,
the `.env` file and the `data/` directory. Each service's secrets are in its
own env file next to them (`broker.env`, `caddy.env`, `authentik.env`), which
the deploy jobs decrypt from the repo (see [Secrets](#secrets)); don't edit
them on the host. Pin all five
versions in that `.env` (compose reads it from there) rather than tracking
`:latest`, so a restart never picks up a release you didn't choose. They're
released together, so they're normally the same tag:

```bash
AUTH_BROKER_VERSION=v1.8.0
CADDY_VERSION=v1.8.0
FLUENT_BIT_VERSION=v1.8.0
VICTORIA_LOGS_VERSION=v1.8.0
VICTORIA_METRICS_VERSION=v1.8.0
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

While the images build, `release.yml` packages the release's deploy bundle,
`deploy-<tag>.tar.gz` (this release's `docker-compose.yml`,
`scripts/deploy.sh`, `authentik/authentik-config.yaml` and encrypted secrets),
and attaches it to the GitHub Release. Then it has four deploy jobs, one per
environment: `deploy-caddy`, `deploy-config`, `deploy-authentik` and
`deploy-authentik-config`. Each one waits, paused, until someone approves it on
the workflow run page, and each is approved on its own. The two Authentik jobs
run upstream's image, so they only wait for the bundle, not this repo's images.
An approved job SSHes into the host and runs `scripts/fetch-release.sh`, which
downloads the bundle from the release into `/opt/auth-broker/releases/<tag>/`
(checking its SHA-256, and skipping the download when that release is already
there) and runs the bundle's own `deploy.sh`, which:

1. decrypts the bundle's `secrets/*.env` into `/opt/auth-broker/` (see
   [Secrets](#secrets)),
2. installs the bundle's `docker-compose.yml` in `/opt/auth-broker/` (replacing
   the host's copy, so make compose changes in the repo, not on the host),
3. for the config server, backs up the database to
   `data/broker.db.bak-<tag>` (SQLite's backup API, safe while running),
4. pins that image's `<SERVICE>_VERSION` (e.g. `CADDY_VERSION`) in
   `/opt/auth-broker/.env` to the release's tag (never `latest`), keeping
   only the `*_VERSION` lines there,
5. pulls that image and recreates just that container
   (`docker compose up -d --no-deps --force-recreate <service>`).

Deploying the config server then lists the devices, as a check that the
database came through. `deploy-config` goes on to deploy Fluent Bit,
VictoriaLogs and VictoriaMetrics the same way. The Authentik jobs skip steps
4–5: they bring the three Authentik services up on the pinned
`AUTHENTIK_VERSION` (recreating only what changed). `deploy-authentik-config`
then installs the bundle's `authentik/authentik-config.yaml` into
`/opt/auth-broker/authentik/` (mounted into the worker at
`/blueprints/custom/`) and applies it with
`docker compose exec authentik-worker ak apply_blueprint custom/authentik-config.yaml`,
which validates the whole blueprint before changing anything. It also installs
`authentik-assets/countdown-pill.svg` (the brand logo) into
`/opt/auth-broker/authentik-assets/`, which the server mounts read-only at
`/web/dist/assets/countdown`, so it is public at
`/static/dist/assets/countdown/`. Only brand assets belong there; the blueprint
stays in `authentik/`. The blueprint is
labelled `blueprints.goauthentik.io/instantiate: 'false'`, so the worker never
applies it on its own. It takes the providers' client secrets with `!Env` from
`authentik.env` (`BLUEPRINT_LOGS_CLIENT_SECRET`,
`BLUEPRINT_METRICS_CLIENT_SECRET`), and likewise the Google login source's OAuth
client (`BLUEPRINT_GOOGLE_CLIENT_ID`, `BLUEPRINT_GOOGLE_CLIENT_SECRET`; its
redirect URI is `https://sso.nikolaivorkinn.com/source/oauth/callback/google/`); never commit them in the YAML. It leaves
users alone: no users, tokens, TOTP or passkey devices, or group members are
in it, so manage those in Authentik's UI. It holds only what this repo owns
(the recovery flow, the sign-up flow, passkey login, the brands, the Logs and Metrics proxy
providers and apps, the embedded outpost's providers, the Google source).
The sign-up flow (`broker-enrollment`) takes a username, email and password, then
**requires a second factor**, a passkey or an authenticator app, before logging the new user in
(no email verification yet). A passkey also lets that user sign in later without typing a username.
It's only offered to a login that came from the broker's `/pair` page, which a policy checks in
the login's `state` token. That keeps it off the Logs and Metrics logins and off the login page,
so the way in is to open `/pair` (or enter the pairing code there) and sign up when sent to the
login. The check isn't a security boundary, as the query can be forged: a new account is an
internal user in no group, so it can't reach Logs or Metrics, which stay admin-only. The login
flow itself still asks for a second factor only from users who have one.
Authentik's own default and system blueprints own the stock flows, stages,
policies, mappings and groups: look those up with `!Find` by name or slug, and
patch the few this file customises, and never paste in a UI export, whose exported
pks exist only on one instance and clash with the stock objects' names. Blueprints
only create and update, so removing an entry doesn't delete the object; add a
`state: absent` entry or delete it in the UI. Entries are applied in order,
so an object has to come before anything that points at it. Re-running
a job redeploys the same tag; to roll back, re-run the deploy jobs of the older
release's workflow run, which deploys that release's own bundle. Old releases
stay under `releases/`; they're a few KB each.

The login screen's recovery link uses `default-recovery-flow`: identify by
email or username, follow the emailed link (valid for 30 minutes), verify an
already-enrolled TOTP authenticator or WebAuthn device, then enter and confirm
a new password. Recovery uses the existing password-change policy and denies
users without either supported second factor; it does not enroll new factors
or reactivate disabled accounts. Both recovery and the sign-up's email
confirmation need working email: see [Email](#email-resend). The blueprint uses `!Env` tags, so
generic YAML schema validators cannot parse it unchanged; the deploy job uses
Authentik's own `ak apply_blueprint`, which applies changes, not just validates.

Passkey login: the login screen's identification stage offers a passkey
(Face ID / Touch ID) button, backed by the `default-authentication-passkey-validation`
stage (WebAuthn only, user verification required). A passkey identifies the user
and authenticates them in one step, with no username, password or second factor;
the default flow's bindings skip those for `auth_webauthn_pwl` logins. Enroll a
passkey from the user settings (`default-authenticator-webauthn-setup`, now
requiring a discoverable credential and user verification). Passkeys enrolled
before this change that aren't discoverable still work as a second factor only.

One-time setup in the repo's **Settings**:

- **Blueprint validation** in the Authentik config workflow (which only runs when
  `authentik/`, its validator or `docker-compose.yml` changes) needs no secrets.
  `scripts/validate_authentik_config.py` starts a throwaway Authentik and
  Postgres with Docker Compose (`scripts/authentik-validate/`, on the
  `AUTHENTIK_VERSION` that `docker-compose.yml` defaults to), migrates it, applies
  Authentik's default and system blueprints, then applies the candidate with
  `ak apply_blueprint`, with validation-only values for the `!Env` tags, and tears
  everything down. It is a real apply, not `--dry-run`: the dry run can't resolve
  `!KeyOf` references to objects the blueprint is about to create. It takes about
  2 to 4 minutes, so run it locally with `python scripts/validate_authentik_config.py`
  (it needs Docker).
- **Environments**: create `deploy-caddy`, `deploy-config`, `deploy-authentik`
  and `deploy-authentik-config`, and on each tick
  **Required reviewers** and add yourself. That rule is what keeps the jobs
  from running on their own. Optionally limit each to `v*` tags under
  **Deployment branches and tags**.
- **Secrets**, either on every environment (so only an approved deploy can
  read them) or once under **Secrets and variables > Actions**:
  - `DEPLOY_HOST`: the instance's public IP or hostname.
  - `DEPLOY_USER`: the SSH user, e.g. `ubuntu`. It must be able to run
    `docker` (in the `docker` group) and own `/opt/auth-broker/` and its `.env`.
  - `ORACLE_SSH_TOKEN`: a private key, made just for this
    (`ssh-keygen -t ed25519 -f deploy -N ''`), whose public half is in that
    user's `~/.ssh/authorized_keys`.
  - `DEPLOY_SSH_KNOWN_HOSTS`: the output of `ssh-keyscan <host>`, so the job
    only talks to your host.

The host also needs its age key, `/opt/auth-broker/age.key` (see
[Secrets](#secrets)), before the first automated deploy. After that the
jobs keep `docker-compose.yml` in step with each release. The host needs
`curl`, which Ubuntu has.

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

### Secrets

Every secret the host needs is in `secrets/`, encrypted with
[SOPS](https://github.com/getsops/sops) and age, one file per service:
`broker.env`, `caddy.env` and `authentik.env`. The `.example` files next to
them list what each one holds. `.sops.yaml` lists who can decrypt them: your
laptop's age key, to edit them, and the host's, to deploy them. Each release
ships the encrypted files in its deploy bundle, and `deploy.sh` decrypts all of
them on every deploy into `/opt/auth-broker/<name>.env` (mode 600), with
`/opt/auth-broker/age.key` and a pinned, checksum-checked `sops` it downloads
into `/opt/auth-broker/bin/`. The Tests workflow fails if any value in
`secrets/` isn't encrypted.

To change a secret, from a checkout on your laptop:

```bash
sops secrets/broker.env    # opens the decrypted file in $EDITOR, re-encrypts on save
```

then commit, release and deploy as usual. Settings that aren't secret (cookie
domain, redirect URI, database names) are in `docker-compose.yml`.

One-time setup:

1. On your laptop, `brew install sops age` and
   `age-keygen -o ~/.config/sops/age/keys.txt`. Keep a copy of that file
   somewhere safe, like a password manager: it is the only key that can edit
   the secrets, so it is also their backup.
2. On the host, `sudo apt install age` and
   `age-keygen -o /opt/auth-broker/age.key && chmod 600 /opt/auth-broker/age.key`
   as the deploy user.
3. Put both public keys (`age1...`) in `.sops.yaml`. When a key changes, e.g.
   a rebuilt host, update `.sops.yaml` and run `sops updatekeys secrets/*.env`
   from your laptop.

### Caddy

Caddy runs as the `caddy` compose service with host networking, binding ports
80 and 443 (TCP, plus UDP 443 for HTTP/3) on the host. That way `127.0.0.1` in
the Caddyfile means the host: it reaches the broker (also host-networked, bound to
`127.0.0.1:5000`), VictoriaLogs on `127.0.0.1:9428` and VictoriaMetrics on
`127.0.0.1:8428`. The logs and metrics sites
share the Caddyfile's `admin_only` snippet: an Authentik login in the admin
group, then the upstream. A Caddyfile change ships like code: edit
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

The compose file also runs Fluent Bit, which tails every container's Docker
log and ships it to VictoriaLogs (`127.0.0.1:9428`). Its config
(`fluent-bit/`) is baked into `ghcr.io/nvorkinn/auth-broker-fluent-bit:<tag>`,
released alongside the other two images, so nothing needs copying to the host.
The `deploy-config` job also deploys it, pinning `FLUENT_BIT_VERSION` in `.env`. Query in
VictoriaLogs with e.g. `{service="caddy"}`.

Every log line, from every source, carries the same fields (stream fields are `service,host`):

| Field | Meaning |
|---|---|
| `service` | Source name: the compose service for container logs (set by the shared `x-logging` anchor, whose `tag: "{{.Name}}"` lands in each record's `attrs.tag`); whatever the sender supplies on the `:9880` HTTP input (default `auth-broker`) or OTLP input (default `otlp`) |
| `host` | The machine's host name, unless the sender supplies its own (e.g. a device id) |
| `level` | `trace`/`debug`/`info`/`warn`/`error`/`fatal`: from the JSON log's `level`, else a level word at the start of the line, else `info` |
| `_msg` | The message |

Senders to `:9880` (the broker, countdown devices) should include `service`, `host` and
`level` in each record. The logic is in `fluent-bit/normalize.lua`.

Container logs are rotated by Docker (10 MB x 3 files per container). The `tag` only applies to
containers created after the change, so recreate them (`docker compose up -d`). Logs from before
the change stay in the old `container_id` streams.

The `victoria-logs` compose service is upstream's single-node VictoriaLogs
(`ghcr.io/nvorkinn/auth-broker-victoria-logs:<tag>`). It keeps a week of logs
(upstream's default) in the named volume `victoria_logs_data`, with a 512 MB
memory limit, and listens on `127.0.0.1:9428` only. Caddy serves it at
`logs.nikolaivorkinn.com`: `/insert/*` takes
`Authorization: Bearer <LOGS_INGEST_TOKEN>`, and everything else (the UI at
`/select/vmui` and the query API) needs the Authentik admin login. The
`deploy-config` job deploys it, pinning `VICTORIA_LOGS_VERSION` in `.env`.

VictoriaLogs used to run by hand from its own compose project in
`/opt/victorialogs/`. Before the first deploy that includes this service,
stop that one so port 9428 is free (its data isn't carried over):
`docker compose -f /opt/victorialogs/compose.yaml down`.

### Authentik's Postgres

The `authentik-postgresql` service runs `postgres:18-alpine`, with the named
volume `authentik_db` mounted at `/var/lib/postgresql` (18+ images keep the
data in a per-major-version subdirectory, so the parent is the mount point).
A 16 data directory can't be opened by 18, so moving from 16 means starting
with an empty database (a real upgrade would need `pg_upgrade` or a
dump and restore). Before the first deploy that includes this, on the host:

```bash
cd /opt/auth-broker
docker compose stop authentik-server authentik-worker authentik-postgresql
docker compose rm -f authentik-postgresql
docker volume rm authentik_db
```

Authentik then bootstraps from scratch on the next `deploy-authentik`.

### Metrics

The `victoria-metrics` compose service is upstream's single-node
VictoriaMetrics (`ghcr.io/nvorkinn/auth-broker-victoria-metrics:<tag>`). It
scrapes nothing; metrics are pushed to it. It keeps a month of data (upstream's default) in the
named volume `victoria_metrics_data`, with a 512 MB memory limit. It listens on
`127.0.0.1:8428` only. Caddy serves it at `metrics.nikolaivorkinn.com`, like
the logs site: the push endpoints (`/api/v1/write`, `/api/v1/import/*`,
`/influx/*`) take `Authorization: Bearer <LOGS_INGEST_TOKEN>`, the same ingest
token as logs, and everything else (the UI at `/vmui` and the query API)
needs the Authentik admin login. The `deploy-config` job deploys it, pinning
`VICTORIA_METRICS_VERSION` in `.env`.

## Not built yet

- The Pi-side migration in `countdown` — swapping `spotify_client.py`'s direct
  `spotipy` calls, and the local Flask config page, for calls to this
  service's device-facing API. Left for a follow-up so this could be stood
  up and tested on its own first.
- Bringing a replacement screen back to an existing device: a short-lived
  link code from `/device` that a registering screen sends to join that
  device instead of becoming a new one.
- Any provider tokens beyond Spotify and Glowmarkt (a future per-user token
  would follow the same shape: its own table, proxied the same way through
  the device-facing API).
