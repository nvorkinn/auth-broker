#!/usr/bin/env bash
# Ships inside each release's deploy bundle (deploy-<tag>.tar.gz) next to that
# release's docker-compose.yml. release.yml unpacks the bundle on the host into
# /opt/auth-broker/releases/<tag>/ and runs this copy, which installs the
# bundled docker-compose.yml, pins one compose service to the tag in .env, then
# pulls and restarts just that service.
# Usage: deploy.sh <auth-broker|caddy|fluent-bit|victoria-logs|victoria-metrics|authentik> <tag>
#
# Secrets come from the bundle's SOPS-encrypted secrets/<name>.env, all of
# them decrypted on every deploy with the host's age key into <name>.env next
# to the compose file (compose won't load the file if any service's env_file
# is missing). Nobody edits those files.
#
# authentik is the three Authentik services. Their image is upstream's, pinned
# by hand with AUTHENTIK_VERSION in .env (one minor version at a time), so
# they aren't pinned to the release tag: this installs the compose file and
# brings them up on whatever is pinned.
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
    ;;
  authentik) services=(authentik-postgresql authentik-server authentik-worker) ;;
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
  # mktemp's file is readable by the deploy user only; a unique name per job,
  # as the deploy jobs can run at once, then mv to replace it in one step.
  tmp="$(mktemp "$name.XXXXXX")"
  "$sops" decrypt --output-type dotenv "$encrypted" > "$tmp"
  mv "$tmp" "$name"
  echo "Decrypted $name"
done

if [ "$service" = auth-broker ] && docker inspect -f '{{.State.Running}}' auth-broker 2>/dev/null | grep -q true; then
  # SQLite's backup API, so the copy is consistent even mid-write.
  docker exec auth-broker python -c "
import sqlite3
src = sqlite3.connect('/app/data/broker.db')
src.backup(sqlite3.connect('/app/data/broker.db.bak-$tag'))
"
  echo "Backed up the database to data/broker.db.bak-$tag"
fi

# The two deploy jobs can run at once and both edit .env and the compose file.
(
  flock 9
  # Copy then mv, so compose never reads a half-written file.
  cp "$bundle/docker-compose.yml" docker-compose.yml.tmp
  mv docker-compose.yml.tmp docker-compose.yml
  # The broker's secrets and settings used to live in .env; now they come
  # from broker.env and the compose file, so keep only the version pins.
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
  # Recreates only what changed, so a release that doesn't touch Authentik
  # leaves its database running.
  docker compose up -d --wait --wait-timeout 600 "${services[@]}"
fi
docker compose ps "${services[@]}"

if [ "$service" = auth-broker ]; then
  # A check that the database came through.
  docker exec auth-broker flask --app wsgi devices list
fi
