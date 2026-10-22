# auth-broker

The backend for **countdown**, the e-paper screens (an ESP32 driving a Waveshare panel) whose frames are drawn by
[pi-dashboard](https://github.com/nvorkinn/pi-dashboard)'s render pool. It:

- registers screens and pairs each with its owner's login;
- hosts the settings site at **https://auth.nikolaivorkinn.com**;
- holds every account a screen's owner links (Spotify, Apple Music, Glowmarkt, GitHub) and runs the providers that use
  them, so no credential leaves the broker;
- writes each screen's config and subscriptions to Redis for pi-dashboard, and serves each screen the frame the render
  pool drew;
- runs the shared infrastructure on the Oracle Cloud ARM host: Caddy, authentik, Redis, Fluent Bit, VictoriaLogs and
  VictoriaMetrics.

It also serves a personal [GitHub dashboard](#github-dashboard) at `/github`.

[docs/architecture.md](docs/architecture.md) shows how the pieces fit together and which secret is which;
[docs/database.md](docs/database.md) has the tables.

## Services

Everything runs from `docker-compose.yml` on one host, in `/opt/auth-broker/`.

| Service | What it does |
|---|---|
| `auth-broker` | The Flask app (gunicorn on `127.0.0.1:5000`). State is one SQLite file, `data/broker.db`. |
| `glow-provider` | Fetches each screen's electricity readings from Glowmarkt every 15 minutes into `v1:energy:<device id>`. |
| `spotify-provider` | Fetches what each screen's Spotify and Apple Music are playing (every minute), their top lists (every 30 minutes) and the playing tracks' covers. |
| `developer-provider` | Polls each screen's GitHub and AI plans for the developer panel (every 2 minutes, every 20 seconds while a release runs). |
| `caddy` | TLS and routing for `auth.`, `sso.`, `logs.` and `metrics.nikolaivorkinn.com`, and the authentik sign-in gate. |
| `authentik-server`, `-worker`, `-postgresql` | Sign-in, sign-up, passkeys and Google login, at `sso.nikolaivorkinn.com`. |
| `redis` | The store shared with pi-dashboard. A cache only: nothing on disk. |
| `fluent-bit`, `victoria-logs`, `victoria-metrics` | Logs and metrics from every container, the render pool and the screens. |

The three providers run the broker's own image with another command and the broker's secrets. The glow and developer
providers open the database read-only; the music provider writes refreshed Spotify tokens back. Each wakes at once when
a config is saved (`v1:sub:changed`).

## The screen API

A screen generates a long random secret on first boot and keeps it. The broker stores only its SHA-256, which
identifies the device. Two calls are all a screen needs:

1. **`POST /api/devices/register`** `{"secret", "width", "height", "format"}`, on every boot and after any 401.
   `format` is the format the screen takes its frame in, `mono-1bpp-packed` or `png`; the renderer draws in it. A new
   secret becomes a device at once (`201 {device_id}`); a known one is a `200`, which changes nothing. Anyone can call
   it, so it's throttled: 5 calls a minute per IP (`429` with `Retry-After`), at most 20 unpaired screens at once
   (`503`), and a screen still unpaired after 24 hours is dropped.
2. **`POST /api/frame`** with `Authorization: Bearer <secret>` and `If-None-Match`, in a loop, sleeping for each
   answer's `Retry-After`:

| Answer | Meaning |
|---|---|
| `200` | The 1-bit frame (800x480, 48,000 bytes) with its `ETag`. |
| `304` | Unchanged. |
| `404` | No frame yet (not paired, or Redis restarted). `Retry-After` is at most 10 s. |
| `401` | Unknown secret (the device was forgotten or unlinked, or sat unpaired for over 24 hours): register again. |

`Retry-After` wakes the screen just after each half-minute (about :03 and :33), as renders land on the minute, or every
10 seconds while a provider's data is bursty. The broker works it out (`broker/services/pace.py`); see
[Pace](docs/architecture.md#pace).

The body of the poll carries the screen's report: `{"metrics": "<InfluxDB line protocol>", "logs": [...]}`. The broker
forwards metrics to VictoriaMetrics (replacing `{device_id}` in them) and logs to VictoriaLogs, in the background; a
failure never fails the poll.

## Pairing and settings

Every browser page needs an authentik login. Caddy forward-auths the request and sets `X-Authentik-Uid` and
`X-Authentik-Username`, which the broker trusts (so it only listens on loopback). The uid becomes the device's `owner`.

- **By token (the normal way).** After Wi-Fi setup the screen sends its owner's browser to `/pair?t=<token>` (or
  `/pair#<token>`), where the token is `sha256("pairing:" + sha256hex(secret))`. The page waits, polling
  `POST /pair/token`, until the screen has registered (giving up after 90 seconds), then pairs it and opens its
  settings. Someone else's device is a `409`.
- **By code (a fallback).** `flask devices code <device_id>` issues a 6-character code, valid 10 minutes, which the
  owner enters at `/pair`. The render pool draws it on the screen if the screen has been paired before (it only draws
  named screens); for a new screen, read it from the command's output. A code can also hand a device to a new owner.
  Wrong codes are throttled per IP.

Pairing gives the screen its internal name (its slug, which is its device id), puts it on the render pool's list and
queues its first render. Its owner can give it a friendly name on its settings page.

Each screen's settings are at `/device/<id>` (and `/device` for the most recently paired one): departure stops (TfL and
Entur), the weather place (an Open-Meteo search), a postcode for the notice board, Glowmarkt, Spotify and Apple Music,
a GitHub token for the developer panel, language, style, clock format and name. `/device/<id>/screen` shows
the screen's current frame. "Unlink screen" revokes the screen's secret; a replacement screen can't yet join an
existing device, so it registers as a new one.

Until a weather place is picked, the page suggests the biggest town near the visitor's IP, looked up locally in DB-IP's
[IP to City Lite](https://db-ip.com/db/download/ip-to-city-lite) and [GeoNames](https://www.geonames.org) (both CC BY
4.0), which `scripts/fetch_geodata.py` downloads into the image when it's built.

When a device changes hands (`unpair`, `forget`, or paired by someone else) or an account is removed from its settings,
the providers' data for it is deleted from Redis too (`broker/services/device_keys.py`).

## What's in Redis

SQLite is the source of truth; Redis holds what pi-dashboard reads. The broker writes:

- `v1:sub:<source>`: what the providers should fetch (stops, weather places, postcodes, devices), then publishes
  `v1:sub:changed`;
- `v1:config:<device id>`: each paired screen's config (no credentials in it);
- `v1:pairing:<device id>`: the config with the live pairing code, for its 10 minutes;
- `v1:render:screens`: the screens the pool draws (device id to slug), leaving out any not seen for 24 hours;
- render jobs on `v1:render:jobs`, for each screen whose config changed.

The subscriptions are rewritten in full on every config save, when the broker starts and by `flask config publish`.
The configs and the screen list are rewritten then too, and also every minute, writing only what differs. A failed
write is logged and the save still succeeds. The providers
write their data with a TTL, and the broker reads each screen's frame (`v1:frame:<device id>`) and pace hints
(`v1:pace:<source>:<id>`) from the pool and providers.

Every key, its value and TTL is in the store contract, [countdown-api](https://github.com/nvorkinn/countdown-api), used
here as the `vendor/countdown-api` submodule. Its fixtures are what both sides' tests read
(`tests/test_store_contract.py`). To change a stored shape, change countdown-api first, then bump the submodule here
(`git -C vendor/countdown-api checkout <commit>`, `uv lock`). Add fields rather than change them: older readers ignore
fields they don't know.

## GitHub dashboard

`/github` shows every open PR across your repositories, their checks, the release workflows running (as a job graph,
with **Approve** buttons for waiting environments), and a **New Release** button per project. It's behind the same
authentik login as the rest of the site, and only one user may open it.

It reaches GitHub through a private GitHub App: the dashboard signs you in with it and then acts as you with tokens
GitHub expires after 8 hours (renewed on their own for 6 months), and its webhook makes the page live. Create it under
GitHub's Settings → Developer settings → GitHub Apps:

- **Callback URL** `https://auth.nikolaivorkinn.com/github/callback`, with *Expire user authorization tokens* ticked.
- **Webhook URL** `https://auth.nikolaivorkinn.com/api/github/webhook`, with a random secret, subscribed to *Pull
  request*, *Pull request review*, *Check run*, *Check suite*, *Status*, *Workflow run*, *Workflow job*, *Deployment
  status* and *Push*.
- Repository permissions **Actions: read**, **Checks: read**, **Commit statuses: read**, **Pull requests: read and
  write** (for auto-merge), **Deployments: read and write** (to approve) and **Contents: read and write** (for releases
  and merging). After changing them, accept them on the App's installation; GitHub doesn't apply them until you do.
- Then install it on your account, for the repositories the dashboard should show.

Enable it with `sops secrets/broker.env`, then release:

```dotenv
GITHUB_DASHBOARD_USER_ID=<your X-Authentik-Uid>
GITHUB_APP_CLIENT_ID=<the App's client ID>
GITHUB_APP_CLIENT_SECRET=<a client secret>
GITHUB_APP_WEBHOOK_SECRET=<the webhook secret>
```

The page shows **Connect GitHub** until you've signed in, and GitHub's tokens never reach the browser. The webhook is
the one dashboard path outside the authentik login (GitHub can't sign in), so its signature is its only check. It
doesn't call GitHub: it notes what changed in Redis (`v1:github:changes`), the page asks for those changes every 5
seconds and re-reads only them, and reads everything every 5 minutes in case a delivery was lost. Without
`GITHUB_APP_WEBHOOK_SECRET` the page reads everything every minute instead.

**Auto-merge** needs *Allow auto-merge* turned on in each repository's settings. GitHub only waits when the base
branch's protection requires checks; where nothing is required, the button merges at once if the PR's checks have all
passed, and refuses while any are still running or failed.

**New Release publishes immediately**, with no confirmation: it bumps the minor version of the highest semver release
(`1.19.0` becomes `1.20.0`, keeping any `v` prefix; `1.0.0` if there are none), generates notes from the previous tag
and creates the release on the default branch. It's never retried automatically, so after a timeout check GitHub before
trying again.

## Other endpoints

- **`GET /about`** is a public page about the display (no login), with pictures in `broker/static/display/`.
- **`GET /api/status/devices`** lists every device's `device_name`, `created_at`, `paired_at` and `last_seen_at`, for a
  Home Assistant REST sensor. It needs `Authorization: Bearer <STATUS_TOKEN>`, and is a 404 while `STATUS_TOKEN` is
  unset.

```yaml
rest:
  - resource: https://auth.nikolaivorkinn.com/api/status/devices
    headers:
      Authorization: !secret auth_broker_status_token   # "Bearer <STATUS_TOKEN>"
    scan_interval: 300
    sensor:
      - name: Countdown last seen
        value_template: "{{ value_json['<device id>'].last_seen_at }}"
        device_class: timestamp
```

## Development

Python 3.14 and [uv](https://docs.astral.sh/uv/). Clone with the submodule:

```bash
git clone --recurse-submodules https://github.com/nvorkinn/auth-broker.git   # or: git submodule update --init
uv sync && source .venv/bin/activate
cp secrets/broker.env.example .env   # fill in Spotify app credentials, FLASK_SECRET_KEY and CREDENTIAL_ENCRYPTION_KEY
export SPOTIFY_REDIRECT_URI=https://auth.nikolaivorkinn.com/auth/spotify/callback
export $(grep -v '^#' .env | xargs)
FLASK_DEBUG=1 python wsgi.py
```

The broker uses Redis at `REDIS_URL` (default `redis://127.0.0.1:6379/0`) with `REDIS_PASSWORD`. There's no login proxy
locally, so send `X-Authentik-Uid` yourself (e.g. with a header extension). To play a screen without hardware:

```bash
python scripts/simulate_device.py            # registers, and prints how to issue a pairing code
python scripts/simulate_device.py --token    # prints the /pair URL instead
# --secret <random string> to be the same screen across runs; pass a base URL to use the deployed broker
```

**Tests, lint and types** (CI runs all three on every PR, the tests split into 3 shards with coverage to Codecov):

```bash
uv run python -m pytest --cov=broker
ruff check . && ruff format --check .
uv run ty check
```

External APIs are mocked and Redis is fakeredis. The render queue's script tests also run against a real
`redis-server` when one is installed. `node --test tests/browser/*.test.cjs` tests the GitHub dashboard's JavaScript.

**Schema changes.** Edit `broker/models.py`, then generate a migration and read it before committing (autogenerate
misses renames and never backfills data):

```bash
flask --app wsgi db migrate -m "add nickname to devices"
```

The app runs pending migrations on startup, so deploying is all a migration needs.
`test_migrations_match_the_models` fails if a model changes without one.

**Code layout:** `broker/routes/` (Flask blueprints), `broker/services/` (the broker's own logic), `broker/clients/`
(outside APIs), `broker/models.py` and `broker/migrations/` (the schema), `broker/cli.py` (admin commands), and the
providers in `broker/providers/glow.py`, `broker/providers/music.py` and `broker/providers/developer.py`, which all
run on the loop in `broker/providers/loop.py`. `tests/` mirrors it.

## Admin commands

Run on the host with `docker exec auth-broker flask --app wsgi <command>`:

| Command | Does |
|---|---|
| `devices list` | List every device. |
| `devices code <device_id>` | Issue a pairing code (drawn on the screen if it has been paired before). |
| `devices owner <device_id> <user id>` | Pair a device with a user. |
| `devices unpair <device_id> [--config]` | Clear its owner and show a new code; `--config` also resets its settings and linked accounts. |
| `devices forget <device_id>` | Delete it; the screen registers again as a new device. |
| `config publish` | Rewrite the configs and subscriptions in Redis. |
| `weather backfill` | Give any config with a typed weather location but no picked place the place Open-Meteo finds first. |

Each change to a device's pairing also updates its config in Redis and queues a render.

## Deploying

Run the **Release** workflow from the Actions tab on `main`, picking `patch`, `minor` or `major` (or push a `v*` tag).
It builds five images for amd64 and arm64, tagged with the release and `latest`:

- `ghcr.io/nvorkinn/auth-broker` (the broker and the providers)
- `ghcr.io/nvorkinn/auth-broker-caddy` (with `caddy/Caddyfile`)
- `ghcr.io/nvorkinn/auth-broker-fluent-bit` (with `fluent-bit/`)
- `ghcr.io/nvorkinn/auth-broker-victoria-logs`
- `ghcr.io/nvorkinn/auth-broker-victoria-metrics` (with the vmui dashboards)

It also attaches a deploy bundle to the release (`docker-compose.yml`, `scripts/deploy.sh`, the encrypted secrets and
authentik's brand assets), then waits for approval of two sets of deploy jobs:

| Environment | Deploys |
|---|---|
| `deploy-infra` | Caddy, the broker and its providers, Fluent Bit, VictoriaLogs, VictoriaMetrics and Redis, a job each, two at a time |
| `deploy-authentik` | authentik, on the `AUTHENTIK_VERSION` pinned in the host's `.env` |

An approved job SSHes into the host and runs `scripts/fetch-release.sh`, which downloads the bundle into
`/opt/auth-broker/releases/<tag>/` (checking its SHA-256) and runs its `deploy.sh`. That decrypts the secrets, installs
the compose file, backs up the database (`data/broker.db.bak-<tag>`) when deploying the broker, pins the service's
`<SERVICE>_VERSION` in `/opt/auth-broker/.env` and recreates it. Redis and authentik are only recreated if their service
changed. After `deploy-authentik`, the `terraform` job applies this tag's authentik configuration, asking for approval
only when the plan changes something (see [terraform/README.md](terraform/README.md)).

To roll back, re-run the deploy jobs of an older release's workflow run. To deploy by hand, set the versions in
`/opt/auth-broker/.env` and run `docker compose up -d`. Make compose changes in the repo: each deploy replaces the
host's copy.

**One-time setup** in the repository's settings:

- **Environments** `deploy-infra`, `deploy-authentik` and `terraform`, each with yourself as a
  required reviewer (that's what stops them running on their own). `terraform` also needs a branch rule: see
  [terraform/README.md](terraform/README.md#applying).
- **Secrets:** `DEPLOY_HOST`, `DEPLOY_USER` (in the `docker` group, owning `/opt/auth-broker/`), `ORACLE_SSH_TOKEN` (a
  private key for that user) and `DEPLOY_SSH_KNOWN_HOSTS` (`ssh-keyscan <host>`), plus the Terraform ones in
  [terraform/README.md](terraform/README.md#secrets) and `CODECOV_TOKEN`.
- **The host** needs Docker Compose v2 (`sudo apt install docker-compose-v2`), `curl` and its age key (below).

### Secrets

Every secret is in `secrets/` (`broker.env`, `caddy.env`, `authentik.env`, `redis.env`), encrypted with
[SOPS](https://github.com/getsops/sops) and age; the `.example` files list what each holds. `.sops.yaml` lists who can
decrypt them: your laptop's age key and the host's (`/opt/auth-broker/age.key`). `deploy.sh` decrypts them all on every
deploy, and CI fails if any value isn't encrypted. Settings that aren't secret are in `docker-compose.yml`.

```bash
sops secrets/broker.env    # edit, re-encrypted on save; then commit, release and deploy
```

To set up: `age-keygen -o ~/.config/sops/age/keys.txt` on your laptop (and keep a copy somewhere safe: it's the only
key that can edit the secrets), `age-keygen -o /opt/auth-broker/age.key` on the host, put both public keys in
`.sops.yaml`, and run `sops updatekeys secrets/*.env` whenever a key changes.

## Operations

### Backups

`/opt/auth-broker/data/` (the SQLite database) and the `authentik_db` volume (every user and passkey) are
the only state worth backing up. Caddy's certificates are in the `caddy_data` volume: never remove it
(`docker compose down -v` would), or Caddy re-requests every certificate and hits Let's Encrypt's rate limits.

### Logs

Fluent Bit tails every container's log and ships it to VictoriaLogs, which keeps a week. The broker posts the
screens' logs there itself (`service=countdown-esp32`, `host=<device id>`). Our own services write JSON lines with
`level` and `_msg`; every record gets `service` (for containers, the container name) and `host`. Browse at
`https://logs.nikolaivorkinn.com/select/vmui` (authentik admins only), e.g. `{service="auth-broker"}`.

### Metrics

VictoriaMetrics keeps a month of pushed metrics: the screens' (via the broker), and the render pool's OTLP
metrics and the host's (via Fluent Bit). Browse at `https://metrics.nikolaivorkinn.com/vmui/` (admins only). Its
Dashboards tab shows `victoria-metrics/dashboards/` (one, **Screens**, per screen and for the render pool); try a query
in vmui before changing one, as CI checks every query parses. The ingest paths on both hosts take
`Authorization: Bearer <LOGS_INGEST_TOKEN>`.

### Redis

Capped at 128 MB, evicting only keys with a TTL. After a restart, the configs and screen list come back within a
minute and the broker's providers refetch on their own. pi-dashboard's providers need the subscriptions, which come back
on the next settings save or broker restart; run `flask config publish` to restore them at once.

```bash
docker exec -it redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli'
```

### Email

authentik sends sign-up confirmations and password-recovery links through [Resend](https://resend.com)'s
SMTP (settings in `docker-compose.yml`, the API key as `AUTHENTIK_EMAIL__PASSWORD` in `secrets/authentik.env`). The
domain's SPF and DKIM records must be verified in Resend, or mail lands in spam.

### authentik

authentik's configuration (flows, sign-up, passkey login, Google, the proxy providers) is Terraform in
[`terraform/`](terraform/README.md). Users and their devices are managed in authentik's UI. New users can only sign up
from a login that started at `/pair`, and must set up a passkey or authenticator app.

### Request tracing

Setting `LOG_REQUEST_HEADERS: "true"` in `docker-compose.yml` makes the broker and Caddy log every
request's headers (never `Authorization` or cookies), for debugging sign-in redirects. Off by default.

## Reusable PR workflows

[`all-checks.yml`](.github/workflows/all-checks.yml) (one required check that waits for every other check on the PR's
head commit) and [`auto_approve.yml`](.github/workflows/auto_approve.yml) (approves the PR once that passes) can be
called from other repositories:

```yaml
# .github/workflows/all-checks.yml
name: All checks
on: pull_request
permissions: {actions: read, checks: read}
jobs:
  all-checks:
    uses: nvorkinn/auth-broker/.github/workflows/all-checks.yml@main
```

```yaml
# .github/workflows/auto_approve.yml
name: Auto-approve PRs
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions: {contents: read, pull-requests: write, checks: read, actions: read}
jobs:
  auto-approve:
    uses: nvorkinn/auth-broker/.github/workflows/auto_approve.yml@main
```

Pin a commit SHA instead of `@main` for stability. Keep the approval workflow named `Auto-approve PRs` (or pass its name
as `auto-approve-workflow-name` to all-checks), as the gate ignores it. In branch protection, require the check name
GitHub actually shows (e.g. `all-checks / all-checks`). Auto-approval skips drafts, forks, Dependabot and
`github-actions[bot]`, and needs **Allow GitHub Actions to create and approve pull requests** enabled in the caller's
settings.
