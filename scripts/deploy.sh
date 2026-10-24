#!/usr/bin/env bash
# Ships in each release's deploy bundle, next to that release's
# docker-compose.yml; fetch-release.sh unpacks it into
# /opt/auth-broker/releases/<tag>/ and runs it. It installs the bundled
# docker-compose.yml, pins the service to the tag in .env, then pulls and
# recreates just that service.
# Usage: deploy.sh <countdown-server|caddy|fluent-bit|victoria-logs|victoria-metrics|redis|authentik> <tag>
#
# Every secrets/<name>.env in the bundle is decrypted on every deploy, with the
# host's age key, into <name>.env next to the compose file (compose won't load
# the file if any service's env_file is missing). Nobody edits those files.
#
# authentik (its three services) and redis run upstream's images, pinned by
# hand (AUTHENTIK_VERSION in .env, redis's tag in docker-compose.yml), so
# they aren't pinned to the release tag. They're brought up rather than
# force-recreated, so a release that doesn't change them leaves them running.
set -euo pipefail

service="$1"
tag="$2"
bundle="$(cd "$(dirname "$0")" && pwd)"
cd "${DEPLOY_DIR:-/opt/auth-broker}"

# sops itself, downloaded once per version into bin/ and checked against
# upstream's published checksums.
SOPS_VERSION=v3.13.3
case "$(uname -m)" in
  aarch64) sops_arch=arm64 sops_sha256=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77 ;;
  x86_64) sops_arch=amd64 sops_sha256=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b ;;
  *) echo "no sops build for $(uname -m)" >&2; exit 1 ;;
esac

services=("$service")
var=
case "$service" in
  # This repo's images, each pinned by <SERVICE>_VERSION in .env, e.g.
  # fluent-bit -> FLUENT_BIT_VERSION.
  countdown-server | caddy | fluent-bit | victoria-logs | victoria-metrics)
    var="${service^^}"
    var="${var//-/_}_VERSION"
    # The providers run the broker's image (and so its COUNTDOWN_SERVER_VERSION), so they're recreated with it.
    if [ "$service" = countdown-server ]; then
      services=(countdown-server glow-provider spotify-provider developer-provider)
    fi
    ;;
  authentik) services=(authentik-postgresql authentik-server authentik-worker) ;;
  redis) ;;
  *) echo "unknown service: $service" >&2; exit 1 ;;
esac
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$PWD/age.key}"
if [ ! -r "$SOPS_AGE_KEY_FILE" ]; then
  echo "No age key at $SOPS_AGE_KEY_FILE to decrypt the secrets; see Secrets in the README" >&2
  exit 1
fi
sops="$PWD/bin/sops-$SOPS_VERSION"
if [ ! -x "$sops" ]; then
  mkdir -p bin
  # Unique temp name, then mv, so two deploy jobs downloading at once can't
  # leave a half-written binary.
  tmp="$(mktemp bin/.sops-XXXXXX)"
  curl -fsSL --retry 3 -o "$tmp" \
    "https://github.com/getsops/sops/releases/download/$SOPS_VERSION/sops-$SOPS_VERSION.linux.$sops_arch"
  echo "$sops_sha256  $tmp" | sha256sum -c --quiet -
  chmod 755 "$tmp"
  mv "$tmp" "$sops"
fi
for encrypted in "$bundle"/secrets/*.env; do
  name="$(basename "$encrypted")"
  # mktemp's file is readable by the deploy user only, and unique per job, as
  # the deploy jobs can run at once; mv replaces the old one in one step.
  tmp="$(mktemp "$name.XXXXXX")"
  "$sops" decrypt --output-type dotenv "$encrypted" > "$tmp"
  mv "$tmp" "$name"
  echo "Decrypted $name"
done

DB_BACKUPS_KEPT=5
# The broker's running container: auth-broker is its name from before the repo was renamed, still running on the
# first deploy under the new one.
broker=
if [ "$service" = countdown-server ]; then
  for name in countdown-server auth-broker; do
    if docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null | grep -q true; then broker="$name"; break; fi
  done
fi
if [ -n "$broker" ]; then
  # SQLite's backup API, so the copy is consistent even mid-write.
  docker exec "$broker" python -c "
import sqlite3
src = sqlite3.connect('/app/data/broker.db')
src.backup(sqlite3.connect('/app/data/broker.db.bak-$tag'))
# Keep the newest $DB_BACKUPS_KEPT copies; deleted in here as the container
# owns them.
from pathlib import Path
backups = sorted(Path('/app/data').glob('broker.db.bak-*'), key=lambda p: p.stat().st_mtime)
for old in backups[:-$DB_BACKUPS_KEPT]:
    old.unlink()
    print(f'Removed old backup data/{old.name}')
"
  echo "Backed up the database to data/broker.db.bak-$tag"
fi

# The deploy jobs can run at once, and each edits .env and the compose file.
(
  flock 9
  # Copy then mv, so compose never reads a half-written file.
  cp "$bundle/docker-compose.yml" docker-compose.yml.tmp
  mv docker-compose.yml.tmp docker-compose.yml
  # authentik-assets/ is the server's public static mount (brand assets
  # only); made here so Docker doesn't create it root-owned. The authentik
  # deploy ships the bundle's whole folder into it, each file copied then
  # mv'd so the server never serves a half-written one.
  mkdir -p authentik-assets
  if [ "$service" = authentik ]; then
    for asset in "$bundle"/authentik-assets/*; do
      name="$(basename "$asset")"
      cp "$asset" "authentik-assets/.$name.tmp"
      mv "authentik-assets/.$name.tmp" "authentik-assets/$name"
    done
  fi
  # Keep only the version pins: secrets are in the *.env files and settings
  # in the compose file.
  # AUTH_BROKER_VERSION is the broker's pin from before the rename (now COUNTDOWN_SERVER_VERSION).
  (umask 077 && grep -E '^[A-Z0-9_]+_VERSION=' .env | grep -v '^AUTH_BROKER_VERSION=' > .env.tmp || true)
  mv .env.tmp .env
  if [ -n "$var" ]; then
    if grep -q "^$var=" .env; then
      sed -i "s/^$var=.*/$var=$tag/" .env
    else
      echo "$var=$tag" >> .env
    fi
  fi
) 9>.env.lock
echo "Installed $tag's docker-compose.yml${var:+ and pinned $var=$tag in .env}"

# Another deploy job's cleanup (below) can delete layers this one is still pulling, so pulls hold image.lock shared
# (they can run at once) and the cleanup holds it alone.
(
  flock -s 9
  docker compose pull "${services[@]}"
) 9>image.lock
if [ "$service" = countdown-server ]; then
  # The broker's container from before the rename, which compose no longer knows: it holds 127.0.0.1:5000, so the
  # new one couldn't start beside it.
  docker rm --force auth-broker >/dev/null 2>&1 && echo "Removed the old auth-broker container" || true
fi
if [ -n "$var" ]; then
  docker compose up -d --no-deps --force-recreate "${services[@]}"
else
  # Recreates only what changed. Jobs for these can run at once, so
  # they take turns.
  (
    flock 9
    docker compose up -d --wait --wait-timeout 600 "${services[@]}"
  ) 9>up.lock
fi
docker compose ps "${services[@]}"

# Every release pulls new tags, so drop the older ones of what was just deployed: keep the running tag and the
# newest other one (to roll back to), remove the rest. `docker image rm` refuses an image a container still uses,
# and only these repositories are touched, so a deploy job running at once never loses an image it just pulled.
IMAGES_KEPT=2
(
  flock 9
  for image in $(docker compose config --images "${services[@]}" | sort -u); do
    repo="${image%:*}"
    docker images "$repo" --format '{{.Repository}}:{{.Tag}}' | { grep -vxF "$image" || true; } |
      tail -n +"$IMAGES_KEPT" | while read -r old; do
        docker image rm "$old" >/dev/null 2>&1 && echo "Removed old image $old" || true
      done
    # And every tag of this repo's image from before the rename (ghcr.io/nvorkinn/auth-broker*), now that nothing
    # runs it.
    old_repo="${repo/\/countdown-server/\/auth-broker}"
    if [ "$old_repo" != "$repo" ]; then
      docker images "$old_repo" --format '{{.Repository}}:{{.Tag}}' | while read -r old; do
        docker image rm "$old" >/dev/null 2>&1 && echo "Removed old image $old" || true
      done
    fi
  done
  # Layers left untagged by the above or by re-pulled tags.
  docker image prune -f >/dev/null
) 9>image.lock

if [ "$service" = countdown-server ]; then
  # A check that the database came through.
  docker exec countdown-server flask --app wsgi devices list
fi
