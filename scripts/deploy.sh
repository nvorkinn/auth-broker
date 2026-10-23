#!/usr/bin/env bash
# Ships in each release's deploy bundle, next to that release's
# docker-compose.yml; fetch-release.sh unpacks it into
# /opt/auth-broker/releases/<tag>/ and runs it. It installs the bundled
# docker-compose.yml, pins the service to the tag in .env, then pulls and
# recreates just that service.
# Usage: deploy.sh <auth-broker|caddy|fluent-bit|victoria-logs|victoria-metrics|redis|authentik> <tag>
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
  auth-broker | caddy | fluent-bit | victoria-logs | victoria-metrics)
    var="${service^^}"
    var="${var//-/_}_VERSION"
    # The providers run the broker's image (and so its AUTH_BROKER_VERSION), so they're recreated with it.
    if [ "$service" = auth-broker ]; then services=(auth-broker glow-provider spotify-provider developer-provider); fi
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
if [ "$service" = auth-broker ] && docker inspect -f '{{.State.Running}}' auth-broker 2>/dev/null | grep -q true; then
  # SQLite's backup API, so the copy is consistent even mid-write.
  docker exec auth-broker python -c "
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
  (umask 077 && grep -E '^[A-Z0-9_]+_VERSION=' .env > .env.tmp || true)
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

docker compose pull "${services[@]}"
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

if [ "$service" = auth-broker ]; then
  # A check that the database came through.
  docker exec auth-broker flask --app wsgi devices list
fi
