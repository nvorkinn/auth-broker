#!/usr/bin/env bash
# Ships inside each release's deploy bundle (deploy-<tag>.tar.gz) next to that
# release's docker-compose.yml. release.yml unpacks the bundle on the host into
# /opt/auth-broker/releases/<tag>/ and runs this copy, which installs the
# bundled docker-compose.yml, pins one compose service to the tag in .env, then
# pulls and restarts just that service.
# Usage: deploy.sh <auth-broker|caddy|fluent-bit> <tag>
set -euo pipefail

service="$1"
tag="$2"
bundle="$(cd "$(dirname "$0")" && pwd)"
cd "${DEPLOY_DIR:-/opt/auth-broker}"

case "$service" in
  auth-broker) var=AUTH_BROKER_VERSION ;;
  caddy) var=CADDY_VERSION ;;
  fluent-bit) var=FLUENT_BIT_VERSION ;;
  *) echo "unknown service: $service" >&2; exit 1 ;;
esac

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
  if grep -q "^$var=" .env; then
    sed -i "s/^$var=.*/$var=$tag/" .env
  else
    echo "$var=$tag" >> .env
  fi
) 9>.env.lock
echo "Installed $tag's docker-compose.yml and pinned $var=$tag in .env"

docker compose pull "$service"
docker compose up -d --no-deps --force-recreate "$service"
docker compose ps "$service"
