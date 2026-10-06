#!/usr/bin/env bash
# Runs on the deploy host (fed over ssh by release.yml): pins one compose
# service to a release tag in .env, then pulls and restarts just that service.
# Usage: deploy.sh <auth-broker|caddy> <tag>
set -euo pipefail

service="$1"
tag="$2"
cd "${DEPLOY_DIR:-/opt/auth-broker}"

case "$service" in
  auth-broker) var=AUTH_BROKER_VERSION ;;
  caddy) var=CADDY_VERSION ;;
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

# The two deploy jobs can run at once and both edit .env.
(
  flock 9
  if grep -q "^$var=" .env; then
    sed -i "s/^$var=.*/$var=$tag/" .env
  else
    echo "$var=$tag" >> .env
  fi
) 9>.env.lock
echo "Pinned $var=$tag in .env"

docker compose pull "$service"
docker compose up -d --no-deps --force-recreate "$service"
docker compose ps "$service"
