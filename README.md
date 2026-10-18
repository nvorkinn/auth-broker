# auth-broker

Cloud-hosted companion service for [countdown](https://github.com/nvorkinn/countdown),
a gifted Raspberry Pi e-paper display. Each recipient's Pi sits on their own
home WiFi with no shared network or stable address, and each has their own
Spotify account — so the one-time OAuth handshake (which needs a public HTTPS
redirect URI) can't happen on the device itself. This service is the one
public HTTPS endpoint that handles that handshake, hosts the recipient-facing
config site, and brokers ongoing Spotify calls so no device ever has to hold
a Spotify token.

## How it fits together

[docs/architecture.md](docs/architecture.md) shows who calls whom (screen, Caddy, this broker, authentik, the
orchestrator and the renderers), which token or secret exists when, and what each call changes;
[docs/database.md](docs/database.md) shows the tables and how they fit in. The sections below say how each part works
and how to run and deploy it.

## Personal GitHub dashboard

The dashboard is served at **https://auth.nikolaivorkinn.com/github**, in the broker's existing image.
Caddy already puts `/github`, its JSON endpoints and its static assets behind authentik forward auth.
It requires no additional hostname, container or authentik provider, and does not change the screen
configuration site's landing page.

Enable it by adding these settings with `sops secrets/broker.env`, then deploying/recreating the broker:

```dotenv
GITHUB_DASHBOARD_USER_ID=<your authentik X-Authentik-Uid>
GITHUB_DASHBOARD_TOKEN=<your fine-grained GitHub personal access token>
```

Use the authentik UID forwarded by the broker's login proxy, not the username, email or numeric database
primary key. This is the same identity stored as a screen's owner. The dashboard refuses other signed-in
users; with either setting absent, it stays disabled. Keep the PAT in the encrypted secrets file: it is
used server-side only, never sent to the browser. As with the broker's other pages, only the trusted
proxy may reach the broker's loopback listener; do not expose its port directly.

Create a fine-grained PAT for your GitHub account with access to the desired repositories. Repository
permissions: **Actions: read**, **Checks: read**, **Commit statuses: read**, **Pull requests: read** and
**Contents: read and write** (for workflow YAML, generated notes, tags and releases); Metadata read is
implicit. Organization-owned repositories are intentionally excluded: discovery includes every
non-archived repository **owned by the token's user**, not just recently active projects. All result
lists are paginated. Repository permission or PAT expiry failures are displayed explicitly.

All open PRs appear in one compact, cross-project list, with repository labels and conflicts or
failed checks first. Pending and failed checks stay visible; completed checks expand inline.
The Checks API and legacy commit statuses are both included. Unknown mergeability is labelled
as such, not as clear. On wide screens the combined workflow diagrams and compact per-project
release controls sit alongside the PR list, using the full viewport width. Diagrams fit their
panel without horizontal scrolling; job and step details expand inline. Narrow screens reflow
the panels without hiding PRs or workflows. Refresh failures retain the previous results with
explicit stale-data labels instead of silently dropping a project's activity.
Projects with pending checks, unknown mergeability or unfinished workflows refresh automatically every
10 seconds after the previous fetch; idle projects and repository discovery refresh every minute.
Requests are limited to three projects at once, stop in hidden tabs, and respect GitHub rate-limit
backoff. ETags are shared through Redis and revalidated on every read: authenticated unchanged
responses do not consume GitHub's primary rate limit. Cached data is scoped to the PAT and expires
after an hour; it is never returned without revalidation. With many active repositories, fetching
can take longer than the refresh interval.

The right-hand panel includes only **release workflows** (a case-insensitive `release` match in the
workflow name), including queued, requested, pending, running and **waiting-for-approval** runs,
with no age cutoff. Other CI workflows still keep active projects on the fast refresh cadence,
but are not drawn in this panel. The dependency diagram uses the workflow YAML **at the run's commit** so jobs that have
not started are present too. Nodes distinguish done, running, waiting for approval, not started,
failed and skipped. Matrix and reusable-workflow jobs are grouped into their parent stage, with
completion counts; expand **All jobs and steps** for every runtime job and step from the current
attempt. Dynamic matrices and reusable-workflow internals cannot be expanded before GitHub returns
their jobs. A workflow that cannot be read is an explicit error, not a misleading edgeless graph.

**New Release publishes immediately**, with no confirmation dialog. It rechecks repository ownership,
reads all published releases **including prereleases** (excluding drafts), and bumps the highest
semantic version's minor number, resetting patch to zero: `1.19.0` becomes `1.20.0`,
`v1.20.0-rc.1` becomes `v1.21.0`. The optional `v` prefix is preserved. With no releases the first tag
is `1.0.0`; if releases exist but none has a semantic version tag, publication is refused.
The broker calls `POST /repos/{owner}/{repo}/releases/generate-notes` with the previous tag and
default branch, then creates a non-draft, non-prerelease release with those notes on that branch.
An already-existing target tag is refused rather than reused. Release actions require a session
CSRF token, are never automatically retried, and disable the button once requested. If a write
times out, check GitHub's releases before reloading to try again: GitHub might have accepted it.

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
   - **Code (fallback).** A short-lived `pairing_code` (10 minutes, one use) is made on
     request: by `POST /api/devices/pairing-code` (either secret), by `flask devices code <device_id>`
     (below), or, for an unpaired device, by its renderer's `GET /api/config`. The user enters it at
     `/pair`. A code also hands a device to a different user, since it proves they're at the device.
     The ESP32 firmware doesn't ask for one.

   Once paired, `pairing_code` is `null`. A new device's weather location,
   postcode and stop ids are empty (`""`, `null`, `[]`), which is valid; the
   renderer's panels report for themselves that they need configuring.
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
   The weather location is a town or city picked from a search (`GET /api/places/search?q=`, at least 2
   characters, for a signed-in paired user like the stop search), which asks
   [Open-Meteo's geocoding](https://open-meteo.com/en/docs/geocoding-api) and lists up to 8 matches as
   `{id, name, admin1, country, country_code, latitude, longitude, label}` (`[]` if Open-Meteo is down). The
   page posts the picked place's Open-Meteo id, and the broker looks that id up again rather than trusting
   the browser, storing its id, name, label, country code and coordinates (`device_config.weather_place_*`).
   Text that wasn't picked (a suggestion, or a browser without JavaScript) is saved as Open-Meteo's first
   match for it, or refused if there's none. `weather_key()` in `broker/services/weather_place.py` names a
   place readably and uniquely (`london-gb-2643743`: its name, country and id), and the weather is subscribed
   (`v1:sub:weather`) and kept in Redis (`v1:weather:<key>`) under that key, so screens that picked the same
   place share one forecast however they typed it. Renderers are sent it as `weather.key`, beside the place's
   name as `weather.location`. Configs saved before the picker have only their text until
   `flask weather backfill` (below) gives them a place; until then they're subscribed under the text's slug
   (`london`, `st-albans`), which the weather provider geocodes by name, so their screens keep their weather.
3. **Spotify:** from `/device`, "Connect Spotify" kicks off the OAuth flow.
   The server exchanges the code for tokens and keeps them — the Pi never
   sees a Spotify token, only its own device secret.
4. **Ongoing:** the renderer reads its config from Redis (see
   [Configs in the store](#configs-in-the-store)), or polls `GET /api/config` (the device's
   settings, and no API key: the weather needs none, and TfL's is only for the broker's stop
   lookups, as pi-dashboard's providers have their own) without it, and reads its panels'
   data from Redis. With Spotify or Apple Music on, the music provider fetches
   what's playing and the top lists with the tokens only the broker holds
   (refreshing Spotify's itself) and writes them there (see "Music in the
   store"). With the developer panel on, the developer provider fetches the owner's GitHub at a
   glance (open PRs, reviews waiting, repos failing on their default branch,
   and a Release or Deploy workflow running or just finished, with its jobs as
   GitHub's graph: `broker/clients/workflow_graph.py`) and their AI plans,
   with the GitHub token saved on `/device`, and writes them to Redis for the
   renderers (see "Developer in the store"). The
   Claude and Gemini plans are placeholders until there's an API to ask
   (`broker/clients/ai_plans.py`).

Every browser page shows the signed-in user's username at the top, from the
`X-Authentik-Username` header the login proxy sets next to `X-Authentik-Uid` (set
`AUTH_USERNAME_HEADER` to change the name). It is only shown and used to name their
screens; the user's identity stays the uid.

On each screen's settings page, **General → Clock format** selects **24-hour (13:00)** or
**12-hour (1:00 PM)** for all clock times on that screen. Existing and new screens default to 24-hour
time, and each screen keeps its own choice. The setting does not change time zones, durations,
machine timestamps or logs. The renderer receives it as the top-level `clock_format` field from
`GET /api/config`; older responses without the field default to `"24h"`.

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
   - 200: draw the frame. `Retry-After` is the refresh interval: 60 s, or 20-59 s while a provider's data is bursty
     (a release is running). Nobody sets it; see [Pace](docs/architecture.md#pace).
   - 304: unchanged. Same `Retry-After`.
   - 404: no frame yet (its renderer is still being set up). `Retry-After: 20`.
   - 401: unknown secret (an unpaired screen is forgotten after 24 hours, or the
     screen was unlinked), so register again.

`/register` answers:

| Code | Meaning | Client should |
|---|---|---|
| 201 / 200 | `{device_id}`: a new device, or a repeat of a registered secret | start polling |
| 400 | bad secret | fix the request; a firmware bug |
| 409 | secret already registered as a renderer | generate a new secret |
| 429 | over 5 calls a minute from this IP | wait `Retry-After` (lockouts double, up to 10 min) |
| 503 | too many unpaired screens | wait `Retry-After` (60s) |

`/api/renderers/claim` answers 201 / 200 `{device_id}`, 400 for a bad secret or no `claim_token`,
403 for an unknown or spent token, and 409 for a secret already registered as a screen.

A renderer reads its config from Redis, where the broker keeps a copy of each paired screen's (see
[Configs in the store](#configs-in-the-store)), and polls `GET /api/config`, which carries its `device_id` and a
`pairing_code` to draw, only when there's none there. From
then on it can use the ID-less routes for everything (`PUT /api/frame`), so it
never has to handle its `device_id`. The broker still logs each request with
the `device_id` it found from the secret. A screen can also ask for a pairing code
(`POST /api/devices/pairing-code`), which a screen with no renderer yet has nothing
else to show.

What anyone can add is bounded:
- `/register` allows 5 calls a minute per IP, every call counting (429 with
  `Retry-After`, lockouts doubling like `/pair` but capped at 10 min).
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
| `POST /api/devices/pairing-code` | renderer, display |
| `PUT /api/frame` (raw 800x480 1-bit, 48000 bytes) | renderer |
| `GET /api/frame` (ETag / `If-None-Match` → 304) | renderer, display |
| `POST /api/frame` (JSON `{"metrics": "<InfluxDB line protocol>", "logs": [...]}`; answers like the GET) | renderer, display |

`POST /api/frame` is the screen's poll with its report attached. The broker forwards
`metrics` to VictoriaMetrics' `/write` after replacing every literal `{device_id}` in it with the
device's id, and `logs` to VictoriaLogs' JSON lines endpoint (`127.0.0.1:9428`), both in the background and
best-effort: a destination being down is logged and never fails the poll.

A wrong or missing secret gets 401. A valid secret for the wrong role gets
403.

`/device/<id>/screen` shows the owner the screen's latest frame in the browser, behind the same Authentik
login as the rest of the site, and checks for a new one at the screen's refresh interval. The picture comes from
`/device/<id>/screen/frame.png`: the 1-bit frame as a PNG, turned upright for a screen hung vertically, with the
frame's ETag so an unchanged frame is a 304.

"Unlink screen" on `/device` revokes the screen's secret. Bringing a
replacement screen back to an existing device isn't built yet: registering
it creates a new device.

## The page about the display

`GET /about` is a public page about the display, for showing people what it does. It needs no login: Caddy leaves
`/about` and its pictures (`/static/display/*`) out of forward auth, and the view reads nothing about the visitor. It's
`broker/templates/about.html`, with its pictures in `broker/static/display/`: frames from pi-dashboard's renderer, drawn
with its snapshot-test helpers (`tests/test_display_snapshots.py`) from sample data in English and Norwegian, and
screenshots of the settings page. The page's EN / NO switch swaps the pictures; its own text is English.

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
    "last_seen_at": "2026-10-05T18:01:00+00:00"
  }
}
```

`last_seen_at` is when the device last made any authenticated request (config,
Spotify, frame). It's updated at most once a minute, and is `null` until the
device's first request after this was added.

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
  api.py      the routes of the countdown API contract (vendor/countdown-api): the registry they are served from
  auth.py     the decorators that guard routes
  config.py   environment variables → app.config
  cli.py      `flask devices ...` admin commands
  providers/  long-running processes that fill the shared Redis store (the glow-provider)
  spotify_provider.py  the music provider, writing each screen's Spotify and Apple Music to Redis
  developer_provider.py  the developer provider, writing each screen's GitHub and AI plans to Redis, and the pace
  provider_loop.py  the loop it runs in (the music and glow providers have their own, older copies), and the read-only
                    app it and the glow-provider read the database in
```

`tests/` mirrors this layout.

## Running locally

Python 3.14 is needed. `vendor/countdown-api` is a git submodule (the API contract and the code generated from it),
installed by `requirements.txt` in editable mode, so clone with it and update it with the repository:

```bash
git clone --recurse-submodules https://github.com/nvorkinn/auth-broker.git   # or, in a clone: git submodule update --init
python3.14 -m venv .venv && source .venv/bin/activate
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
docker exec auth-broker flask --app wsgi config publish                         # rewrite the store's configs and subscriptions
```

Each of these that changes a device's pairing also updates its config in the store and nudges its renderer
(see [Configs in the store](#configs-in-the-store)), so `unpair` and `code` show on its screen at once.

The device's data in the store (`v1:energy:`, `v1:spotify:`, `v1:spotify-top:`, `v1:developer:` and
`v1:pace:developer:`) is deleted, after the database's commit, whenever it changes hands: `unpair`, `forget`,
`owner` with someone other than its owner, and pairing with a code or token by anyone but its owner
(`broker/services/device_keys.py`). So is the data made from an account the settings page removes or replaces: the
energy readings when the Glowmarkt account is cleared or changed, the developer panel when the GitHub token is
replaced or disconnected, and the music when Spotify or Apple Music is disconnected. A Redis that can't be asked is
logged and the change saved all the same; the keys then lapse by their TTLs. The frame record follows the frames
table's row, as before (`forget` and `unpair --config`). Linked accounts that a hand-over keeps (`code`, `owner`,
`unpair` without `--config`) are still the device's, so the providers write their keys again from them: use
`unpair --config` to hand a device over without them.

`docker exec auth-broker flask --app wsgi weather backfill` gives every config with a weather location
but no picked place the place Open-Meteo finds first for its text (the one a renderer finds too), leaving
the text itself alone. It reports each one, skips any it can't resolve, and is safe to run again; run it
once after deploying the migration that added the place (0021), as a migration doesn't call the network.
Then it rewrites the subscriptions and the configs in the store, as a config save does, so the weather is
fetched and read under the new keys straight away: run it again after deploying a broker that changes how they're keyed (as the switch to
`weather.key` did), or if Redis has lost them.

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

## The device API contract

The contract between the broker and the renderer ([pi-dashboard](https://github.com/nvorkinn/pi-dashboard)) is
one OpenAPI file, in [nvorkinn/countdown-api](https://github.com/nvorkinn/countdown-api), which both sides use as
the submodule `vendor/countdown-api`. [openapi-codegen](https://github.com/nvorkinn/openapi-codegen) turns it into
the Pydantic models, the renderer's `requests` client and the server side below, and the result is committed
there, so a contract change is a change to the code.

So far only `GET /api/config` is in it. The broker serves it through `broker/api.py`: `api` is the registry the
generated code provides, and `routes/devices.py` registers what answers each operation:

```python
@api.get_config()
def get_config(*, device_id, x_device_name=None) -> ConfigResponse: ...
```

- **Who may call it** is in the contract: the operation's `security` names the `deviceSecret` scheme with the role
  (`renderer`) as its scope. `broker/api.py` registers the one authenticator for that scheme, which is
  `auth.authenticate_device`, the same check as `require_device_auth`, and the `device_id` it provides is the
  handler's argument.
- **Nothing can be forgotten.** `create_app()` ends with `api.register(app)`, which refuses to start the app if the
  contract has an operation nothing here registers, a security scheme without an authenticator, or a handler
  that doesn't fit its operation. So when the submodule moves to a contract that adds a route, the build fails
  (every test uses `create_app()`) until the broker implements it.
- **Changing the contract** is a PR in countdown-api, which regenerates its code and keeps it current. Then here:
  `git -C vendor/countdown-api fetch && git -C vendor/countdown-api checkout <commit>`, commit the new pointer
  (Dependabot opens that PR weekly), and implement what the build says is missing. Change the format by adding
  fields: a device on an older renderer ignores fields it doesn't know.

The broker's other routes are plain Flask blueprints and not in the contract.

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

The same workflow builds each image. The broker's is then started the way
`docker-compose.yml` runs it, broker and providers, which have to start and
stop cleanly (`scripts/check_image_starts.py`, which also runs on any Linux
Docker host after `docker build -t auth-broker-broker .`: the services use
host networking, as on the server).

## Deploying

To release, run the Release workflow from the Actions tab (Run workflow, on
`main`) and pick `patch` (the default), `minor` or `major`: it tags the head
of `main` with the next version after the latest `v*` tag. Pushing a tag by
hand (`git tag v1.2.3 && git push --tags`) still works too. Either way
`.github/workflows/release.yml` builds five Docker images, side by
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
own env file next to them (`broker.env`, `caddy.env`, `authentik.env`, `redis.env`), which
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
`scripts/deploy.sh`, Authentik's brand assets and encrypted secrets),
and attaches it to the GitHub Release. Then it has three deploy jobs, one per
environment: `deploy-caddy`, `deploy-config` and `deploy-authentik`. Each one
waits, paused, until someone approves it on the workflow run page, and each is
approved on its own. The Authentik job runs upstream's image, so it only waits
for the bundle, not this repo's images. Once it has run, the `terraform` job
applies this tag's Authentik configuration, asking for approval in the
`terraform` environment only when its plan changes something (see
[terraform/README.md](terraform/README.md#applying)).
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
VictoriaLogs and VictoriaMetrics the same way, then Redis, which is brought up
like Authentik (see [Redis](#redis)). The Authentik job skips steps
4–5: they bring the three Authentik services up on the pinned
`AUTHENTIK_VERSION` (recreating only what changed). It also installs
`authentik-assets/countdown-pill.svg` (the brand logo) into
`/opt/auth-broker/authentik-assets/`, which the server mounts read-only at
`/web/dist/assets/countdown`, so it is public at
`/static/dist/assets/countdown/`. Only brand assets belong there.

What Authentik is *configured* with is not in the deploy bundle: it lives in
`terraform/` (see its [README](terraform/README.md)), is planned on every PR and
is applied by the release, from the release's tag, never from a branch. It leaves users alone: no users, tokens, TOTP or
passkey devices, or group members are in it, so manage those in Authentik's UI. It holds only what
this repo owns (the recovery flow, the sign-up flow, passkey login, the brands, the Broker, Logs and
Metrics proxy providers and apps, the embedded outpost's providers, the Google source), and
the Google client and the Logs and Metrics client secrets are GitHub secrets
(`TF_VAR_GOOGLE_CLIENT_ID`, `TF_VAR_GOOGLE_CLIENT_SECRET`, `TF_VAR_LOGS_CLIENT_SECRET`,
`TF_VAR_METRICS_CLIENT_SECRET`), never committed. The Google client's redirect URI is
`https://sso.nikolaivorkinn.com/source/oauth/callback/google/`.
Google login and sign-up use their own flows (`broker-source-authentication`, `broker-source-enrollment`)
that run the same mandatory second-factor stage as the sign-up before logging the user in, so a Google
sign-in no longer skips the passkey or authenticator app; a user with neither sets one up first.
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
policies, mappings and groups; Terraform imports the few this repo customises and sets only the
attributes it overrides, so it leaves the rest of those objects as Authentik has them. Re-running
a job redeploys the same tag; to roll back, re-run the deploy jobs of the older
release's workflow run, which deploys that release's own bundle. Old releases
stay under `releases/`; they're a few KB each.

The login screen's recovery link uses `default-recovery-flow`: identify by
email or username, follow the emailed link (valid for 30 minutes), verify an
already-enrolled TOTP authenticator or WebAuthn device, then enter and confirm
a new password. Recovery uses the existing password-change policy and denies
users without either supported second factor; it does not enroll new factors
or reactivate disabled accounts. Both recovery and the sign-up's email
confirmation need working email: see [Email](#email-resend).

Passkey login: the login screen's identification stage offers a passkey
(Face ID / Touch ID) button, backed by the `default-authentication-passkey-validation`
stage (WebAuthn only, user verification required). A passkey identifies the user
and authenticates them in one step, with no username, password or second factor;
the default flow's bindings skip those for `auth_webauthn_pwl` logins. Enroll a
passkey from the user settings (`default-authenticator-webauthn-setup`, now
requiring a discoverable credential and user verification). Passkeys enrolled
before this change that aren't discoverable still work as a second factor only.

One-time setup in the repo's **Settings**:

- **Terraform** (Authentik's configuration): the `AUTHENTIK_TOKEN` and `TF_VAR_*` secrets and the
  `terraform` environment, described in [terraform/README.md](terraform/README.md#secrets).
- **Environments**: create `deploy-caddy`, `deploy-config` and `deploy-authentik`,
  and on each tick
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
`broker.env`, `caddy.env`, `authentik.env` and `redis.env`. The `.example` files next to
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

Fluent Bit only transports: it adds no fields and rewrites none. Each source writes the standard
fields itself, as one JSON object per line (stream fields are `service,host`):

| Field | Meaning |
|---|---|
| `service` | The source app. For containers it is the Docker log `tag`, which lands in each record's `attrs.tag` and which Fluent Bit lifts to `service`: the compose service name (the shared `x-logging` anchor sets `tag: "{{.Name}}"`), `orchestrator`, and `renderer-<slug>` for each renderer (the orchestrator sets the same tag when it starts one). Screens are `countdown-esp32`; OTLP senders that name none are `otlp` |
| `host` | The machine's host name; for a screen, its device id |
| `level` | `debug`/`info`/`warn`/`error`/`fatal`. Written by the broker, renderers and orchestrator; Caddy and the Victoria services have it natively; Authentik uses `warning` for `warn`; Postgres and Fluent Bit write plain text with no `level` |
| `_msg` | The message: `_msg` from our apps, `msg` from Caddy and the Victoria services, `event` from Authentik, else the raw line (`log`) |
| `_time` | When Docker saw the line. A screen has no clock, so the broker dates its lines by their uptime relative to the newest in the report |

The broker's JSON comes from `gunicorn.conf.py` and `broker/log_format.py` (the access log has the
method, path without query string, status, bytes and duration as fields); a `flask` command writes the
same lines to stderr, as its stdout is its output. The renderers and orchestrator
use `countdown_core.core.log_format` and `countdown_orchestrator.log_format` in the pi-dashboard repo; a
renderer's lines also carry `device` (its name). Screens' logs are posted by the broker straight to
VictoriaLogs, tagged `service=countdown-esp32` and `host=<device id>`; `VICTORIA_LOGS_URL` overrides the
destination.

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

### Redis

The `redis` compose service is upstream's `redis:8.10-alpine`, pinned in
`docker-compose.yml` (bump it there). It is the store in the middle of the
renderers: the broker records which stops and locations screens want, one
provider per API fetches them and writes the latest data under a key per stop
or location, and the renderers read it. The broker writes the subscriptions and
each screen's config ([Configs in the store](#configs-in-the-store)), and
[the energy provider](#the-energy-provider) is the first provider here.

It is a cache only, so it keeps nothing on disk: provider data comes back
within minutes, and the subscriptions and configs are rebuilt from the broker's
database (as it starts, on every config save, and by `flask config publish`).
It listens on `127.0.0.1:6379` only (host networking, like the Victoria
services), needs `REDIS_PASSWORD` from `secrets/redis.env`, and is capped at
128 MB; when full it evicts only keys with a TTL. The `deploy-config` job
brings it up with `docker compose up -d --wait`, so a release that doesn't
change its service leaves it running. To look inside, on the host:

```bash
docker exec -it redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli'
```

### The energy provider

`broker/providers/glow.py`, the `glow-provider` compose service, fetches every
Glow device's electricity readings from Glowmarkt and writes them to Redis, so
the renderers on the host draw the energy panel from there instead of signing
in to Glowmarkt themselves. It's the broker's own image with another command
(`python -m broker.providers.glow`), because the Glowmarkt passwords are
encrypted with the broker's `CREDENTIAL_ENCRYPTION_KEY`: it reads the devices
with an account straight from the database (opened read-only, `./data` mounted
`:ro`; the same accounts as the broker's `v1:sub:glow`, less any without a
username), so the passwords never leave the broker and it needs no token of its
own. `GET /api/config` carries only `glowmarkt.enabled`, true when the device
has an account the provider signs in with (`GlowmarktCredentials.sign_in`, the
rule both use), and nothing of the account itself.

Every 15 minutes (on the epoch-aligned tick, or at once for an account that's
new or changed when the broker publishes on `v1:sub:changed`) it asks Glowmarkt,
per device, for the three pages the renderer's energy panel draws, and writes
`v1:energy:<device id>` with a TTL of about a day (`TTL_S`). Glowmarkt has a new
reading at most every half-hour, and DCC data is often hours late, so energy
changes slowly: Glowmarkt failing for hours changes nothing on the screens,
while after a day without a fetch the key is gone and the energy panel says it's
not available rather than draw readings that old. The TTL is the only expiry
(pi-dashboard's `docs/stateless-renderers.md`, "Expiry"): renderers keep no copy
of their own. A device
whose sign-in fails is logged and skipped (its key expires); its token is kept
between cycles and renewed when Glowmarkt refuses it. The value is Glowmarkt's
answers as they came, in a thin wrapper, so the renderer reads them with the
models it already has (pi-dashboard's `countdown_core/store/energy.py`):

```json
{
  "fetched_at": "2026-09-25T05:10:00+00:00",
  "device_id": "a1b2c3d4e5f6",
  "offset": "-60",
  "resource_id": "<the electricity consumption resource>",
  "hourly":  {"data": [[1790229600, 0.21], "..."], "...": "Glowmarkt's PT1H readings, the last 24 hours"},
  "daily":   {"data": [[1787616000, 6.4], "..."],  "...": "P1D, the last 31 days"},
  "monthly": {"data": [[1767225600, 190.0], "..."], "...": "P1M, since 1st January"}
}
```

Each page is asked for from the start of its window's first bucket (the hour
24 hours ago, midnight 31 days ago, 1st January) to now, in the screen's time
zone (the `timezone` `GET /api/config` sends: Europe/London or Europe/Oslo) at
today's UTC offset. `offset` is Glowmarkt's parameter for it, minutes from local
time to UTC (BST is `-60`): Glowmarkt labels each bucket with its local
wall-clock start as if it were UTC, and the renderer relabels them with it. The
renderer cuts each page to its window as of when it draws, so a value up to an
hour old still draws the right 24 hours, 31 days and year, missing at most the
readings since `fetched_at`. `tests/fixtures/glow_energy.json` is one fetch,
from Glowmarkt's answers to the stored value; pi-dashboard's tests read the
same file, so change both together.

It runs the broker's image, so `deploy.sh` recreates it with the broker on
every `deploy-config`, pinned by the same `AUTH_BROKER_VERSION`. To see it on
the host:

```bash
docker logs -f glow-provider
docker exec -it redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --scan --pattern "v1:energy:*"'
```

The renderers read it once pi-dashboard's orchestrator is given
`RENDERER_REDIS_URL` (see its providers README).
## Configs in the store

SQLite stays the source of truth for each screen's settings, and Redis holds a copy of what `GET /api/config` answers
its renderer, `v1:config:<device id>`, so the renderers read their config there (pi-dashboard's README, "The config")
and don't need the broker to be up, and a save reaches the screen at once
(`broker/services/config_store.py`; `services/renderer_config.py` makes it, and the route's answer, the same way):

- **The value** is exactly `GET /api/config`'s JSON for the device (the contract's `ConfigResponse`), less its
  pairing code (`pairing_code` null). It carries no API key: neither the weather's nor TfL's is in the config at
  all any more.
  `tests/fixtures/config_store.json` is an example; pi-dashboard's tests read the same file, so change both together.
- **Written** for every device on every config save, as the broker starts (gunicorn's `post_worker_init`, in
  `gunicorn.conf.py`, which writes the subscriptions too), by `flask weather backfill` and by
  `flask config publish`; and for one device when its pairing changes. Only what changed is written, and after each
  change the broker publishes the device id on `v1:config:changed`, which its renderer listens for and redraws on.
- **No key** while a device isn't paired, or has a live pairing code: only `GET /api/config` gives the code (and
  issues it, while the device is unpaired), so its renderer asks there. Pairing writes the key back.
- **No TTL**, since it's rebuilt from the database, so `volatile-lru` never evicts it: about 1 KB a screen in Redis.
- If Redis can't be written during a save, the save still works and the log says "Couldn't write the screens'
  configs to Redis"; the screen keeps its old config until the next save or `flask config publish`.

[The config in the store](docs/architecture.md#the-config-in-the-store) has the details: the events, the failure
modes, and how to roll back past this (delete the keys first, or the renderers keep reading them). Deploy this before
the renderers that read it (they ask `GET /api/config` until the keys are there); to check, on the host:

```sh
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 --scan --pattern "v1:config:*"'
```

lists one key per paired screen.

## Frames in the store

pi-dashboard's renderers hand each screen's frame over through Redis as well as `PUT /api/frame`: each new picture
goes to the screen's record, `v1:frame:<device id>` (pi-dashboard's `store/frame_record.py`), and the broker serves
the screen from it (`broker/services/frame_record.py`). It's one latest value per screen, like the providers' keys:

- **The record** is a hash: `frame`, the 48,000 raw bytes (read with `app.extensions["redis_bytes"]`, a client that
  doesn't decode values); `etag`, the frame's BLAKE2b in hex, the same ETag as the database's copy; `rendered_at`,
  when the renderer first wrote the picture. `tests/fixtures/frame_record.json` (with `frame_record.bin`) pins it;
  pi-dashboard's tests read the same files, so change both together.
- **Served** to the screen, and on the config page's preview and its "Drawn" time, when it's there and newer than the
  `frames` row, or holds the same picture; the row otherwise (no record, one that can't be read, or a newer row with
  another picture: a renderer that only PUTs). A screen with the picture already gets its 304 from `etag` without the
  48 KB being read. The ETag, `Retry-After` and 304s are as before, whichever is served, so the firmware sees nothing
  new.
- **TTL a day** from the picture's last change, set by the renderer, so `volatile-lru` can evict it; the row is
  served meanwhile, and the renderer writes it again on its next cycle. The broker deletes a device's record with its
  row (`flask devices forget`, `unpair --config`).
- If Redis can't be asked, the screen is served the row and the log says "Couldn't read a screen's frame from Redis",
  with the error's type.

Either side can be deployed first: renderers that don't write records leave the row served, and a broker that
doesn't read them never sees them. To check, on the host, once both are deployed:

```sh
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 --scan --pattern "v1:frame:*"'
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 HMGET v1:frame:<device id> etag rendered_at'
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 TTL v1:frame:<device id>'
```

lists one key per screen with a running renderer, each with a TTL under a day; a record's `rendered_at` is the
"Drawn" time on its screen's page.

## Developer in the store

`broker/developer_provider.py` is the developer provider, run from the broker's image like the music one. For as long
as a screen's developer panel is on (`v1:sub:developer` holds its device id) it polls everything the panel shows on its
own, whether or not a renderer is looking: the owner's GitHub, with the token only the broker holds, and their AI plans
(`broker/services/developer.py`), and writes it to `v1:developer:<device id>` with `fetched_at` (TTL 15 minutes). That
key is the only way the renderers get the panel: the broker has no route for it. A poll costs GitHub about 25 calls, so
it polls every 2 minutes (`IDLE_INTERVAL_S`).

It also sets the screen's pace. While one of a device's releases is running its stages change by the minute, so it polls
every 20 seconds (`BURST_INTERVAL_S`) and writes `v1:pace:developer:<device id>` = 20 with a one-minute TTL, renewed each
cycle the release still runs. Nothing says a release has ended: the key lapses and the screen goes back to polling
twice a minute. The broker reads the key on each of the screen's polls and has it poll every 10 seconds meanwhile
(`broker/services/pace.py`, and "Pace" in [docs/architecture.md](docs/architecture.md#pace)); today's renderers also
read it, draw that often, and send it with their frames as `X-Refresh-Hint`, which the broker counts too. A burst costs
about 750 calls over a ten-minute release, against the 5,000 an hour a token gets.

If GitHub rate-limits a device's token anyway (a 429, or a 403 that is a rate limit rather than a missing permission), the
provider leaves that device alone for as long as GitHub says (`Retry-After`, or until `X-RateLimit-Reset` when
`X-RateLimit-Remaining` is 0; a minute if it says neither), and polls the other devices as usual. The device's keys are
left as they were meanwhile, so a long wait lets them lapse and the panel says it has no data rather than showing it
stale. A rate limit part-way through a poll fails the whole poll (`developer.snapshot()` raises and nothing is
written) instead of leaving out the releases or the Copilot usage it couldn't read.

It only reads the database, like the glow-provider: it runs in `provider_loop.read_only_app()`, not the broker's
`create_app()`, so it runs no migrations, needs only `CREDENTIAL_ENCRYPTION_KEY` of the broker's secrets (to decrypt the
tokens), and opens the database with SQLite's `mode=ro`, where any write fails; compose mounts `./data` `:ro` as well.
A GitHub token is never refreshed, so there's nothing to write back. A read-only mount works because the database is in
SQLite's default rollback-journal mode, where a reader only takes shared locks on the file (`tests/test_db.py` pins
it): in WAL mode every reader has to create a `-shm` file beside it, and can't. As it doesn't migrate, a cycle that runs
while the broker is still migrating after a deploy can fail on the new schema; the next tick tries again.

`tests/fixtures/developer.json` is the stored shape, copied into pi-dashboard's tests too, so a change to either side
that breaks the other shows up in both. To see it on the host: `docker logs -f developer-provider`. Locally,
`python -m broker.developer_provider`. `deploy.sh` recreates it with the broker.

## Music in the store

`broker/spotify_provider.py` is the music provider: a long-running process that fetches what each screen's Spotify
and Apple Music are playing, and their top lists, and writes them to Redis, which is the only place the renderers get
them from. It runs from the broker's image with the broker's settings and `data/`, and calls the services with the
broker's own clients (`broker/clients/spotify.py`, `broker/services/music.py`), so the tokens never leave the broker,
and a Spotify token it refreshes is stored back in the database.

Apple Music has no top lists of its own, so the broker counts them from the plays it has seen (`apple_music_plays`).
The provider is what logs them: each minute's read of the recently played list logs the songs ahead of where the
last read picks up (`apple_music.record_plays`), so a list read again logs nothing.

Its loop follows pi-dashboard's providers (`countdown/providers/README.md` there): a cycle on start, then on every
epoch-aligned minute, or at once on a `v1:sub:changed` message (a config was saved). Each cycle reads the devices
from the database: Spotify switched on and linked, or Apple Music switched on (connected or not; the latter stored as
nothing playing). A device whose fetch fails (a revoked token, Spotify down) is logged and left out, so its keys
expire; an unreachable Redis is logged and tried again next minute. SIGTERM stops it between devices.

| Key | TTL | Fetched | Holds |
|---|---|---|---|
| `v1:spotify:<device id>` | 3 min | every minute | `{"fetched_at", "spotify", "apple_music"}`: each service's `/queue` body |
| `v1:spotify-top:<device id>` | 2 h | every 30 min, and at once for a device new to it | `{"fetched_at", "spotify", "apple_music"}`: each service's top lists |
| `v1:art:<sha1 of the image URL>` | 5 min, renewed every minute while a player key shows it | once, when a player key first shows it (before that key is written) | the playing track's cover: the image file's raw bytes, as its host served it (see [Covers](#covers)) |

Each service's part is in Spotify's shapes: Spotify's own answers, and Apple Music's made to look like them, so the
renderer reads both with the same models; the only definition of its own is the wrapper (pi-dashboard's
`countdown_core/store/music.py`):

```json
{
  "fetched_at": "2026-10-17T12:00:00+00:00",
  "spotify": {"currently_playing": {...}, "queue": [...]},
  "apple_music": null
}
```

```json
{
  "fetched_at": "2026-10-17T12:00:00+00:00",
  "spotify": {
    "long_term": {"tracks": {"items": [...]}, "artists": {"items": [...]}},
    "short_term": {"tracks": {"items": [...]}, "artists": {"items": [...]}}
  },
  "apple_music": null
}
```

- A part is `null` when the service is switched off or its fetch failed; the renderer's panel then says it's not
  available.
  Top lists short of a service are written only if there's no copy yet: a full older one is kept, and the fetch is
  tried again next minute.
- Nothing playing is a queue with `"currently_playing": null` (and `"queue": []` for Spotify's 204), not a missing
  key: a missing key means the provider is down.
- The top lists are Spotify's `/me/top/{tracks,artists}?time_range=<long_term|short_term>&limit=5`, the two time
  ranges the renderer shows, and Apple Music's counted equivalents.
- `tests/fixtures/music_player.json` and `music_top.json` are an example of each; pi-dashboard's tests read copies
  of the same files, so keep them in step.

A minute is as often as a renderer moves its music panel on today, and it shows the player only every other page, so
the provider makes no more Spotify calls than the renderers it stands in for: one a minute per device, plus four
every half hour, and an image host call only when a cover is new to the store.

To see it on the host: `docker logs -f spotify-provider`, and
`docker exec -it redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli GET v1:spotify:<device id>'`. Locally, with
Redis running and the broker's environment loaded, `python -m broker.spotify_provider` (`LOG_LEVEL=debug` shows each
cycle).

It runs the broker's image, so `deploy.sh` recreates it with the broker on every `deploy-config`, pinned by the same
`AUTH_BROKER_VERSION`. The renderers read it once pi-dashboard's orchestrator is given `RENDERER_REDIS_URL` (its
providers README, "Enabling in production").

### Covers

The renderer's now playing page shows the playing track's cover, which it used to download from Spotify's or Apple's
image host itself. The provider stores it instead (`broker/album_art.py`), so the renderers never call those hosts:

- **Which.** Each service's playing track's (Apple Music's last played), so the page has a cover whichever service it
  shows. Of the album's `images`, the smallest at least 120 px tall (the renderer's Spotify area is 120 px high in
  every layout), else the tallest: Spotify's 300 px of its 640, 300 and 64, and the 300 px Apple Music image the broker
  asks for (`ARTWORK_SIZE`). The renderer scales it down to the style's size. pi-dashboard's `store/music.py`
  (`art_image`) picks the same way to find the key, so change both together.
- **Keyed by image, not device.** `v1:art:<sha1 of the URL, exactly as the service wrote it>`, so every screen
  showing the same album reads one copy, fetched once a cycle at most however many screens show it.
- **Raw bytes, not base64.** The value is the file itself: base64 would take a third more of Redis's 128 MB for
  nothing. Writing bytes through the provider's client needs nothing special (its `decode_responses=True` only decodes
  replies, and it never reads a cover back, only renews its TTL); the renderer reads covers with a second client that
  doesn't decode (pi-dashboard's `store.reader.binary_store()`), as its usual one decodes every value as text.
- **Renewed, not fetched again.** Each cycle the provider `EXPIRE`s the covers the player keys still show, and fetches
  only the ones that aren't there (new, lapsed or evicted), before it writes the player key. The 5 min TTL outlives
  the player key's 3 min, so a cover never goes before the key that shows it, and one nothing shows lapses soon after.
- **Failures cost only the cover.** One that can't be fetched (an error, not an image, over 256 KB, not `https`) is
  logged and tried again next minute; a host that doesn't answer in 5 s isn't tried again until the next cycle. The
  player key is written regardless, and the renderer draws the page without a cover, as for a local file.

Size: a 300 px cover is a JPEG of about 30 KB (nothing over 256 KB is stored). A device keeps one or two alive
(Spotify's and Apple Music's), about 60 KB; a listener skipping a track a minute on both keeps about six per service
alive within the 5 minutes, about 400 KB. For 50 devices that's 3 MB as a rule and 20 MB if every one of them skips
like that, less where screens share albums: small beside `maxmemory 128mb`. Every cover has a TTL, so under
`volatile-lru` it can be evicted like the providers' data, and is then just fetched again.

`tests/fixtures/music_art.json` is the keys stored for `music_player.json` (each holding `music_art.jpg` in the tests);
pi-dashboard's tests read copies of all three. To see them on the host (a cover still shown has a TTL of 240 to 300):

```sh
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 --scan --pattern "v1:art:*" | head'
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 TTL v1:art:<sha1>'
docker exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 STRLEN v1:art:<sha1>'   # its bytes
```

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
