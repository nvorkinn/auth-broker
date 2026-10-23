#!/usr/bin/env bash
# Fed over ssh by release.yml's deploy jobs. Downloads a release's deploy bundle
# into /opt/auth-broker/releases/<tag>/ (unless it's already there with the same
# checksum), then runs the bundle's own deploy.sh, so the host only ever deploys
# what the release published.
# Usage: fetch-release.sh <service> <tag> <bundle url> <bundle sha256>
set -euo pipefail

service="$1"
tag="$2"
url="$3"
sha256="$4"
releases="${DEPLOY_DIR:-/opt/auth-broker}/releases"
RELEASES_KEPT=5
mkdir -p "$releases"

# A release's deploy jobs can run at once; only one downloads.
(
  flock 9
  if [ "$(cat "$releases/$tag/.sha256" 2>/dev/null)" = "$sha256" ]; then
    echo "Release $tag is already on the host"
    # Counts as the newest for the cleanup below, e.g. when rolling back to it.
    touch "$releases/$tag"
  else
    tmp="$(mktemp -d "$releases/.incoming-XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL --retry 3 -o "$tmp/bundle.tar.gz" "$url"
    echo "$sha256  $tmp/bundle.tar.gz" | sha256sum -c --quiet -
    tar -xzf "$tmp/bundle.tar.gz" -C "$tmp"
    echo "$sha256" > "$tmp/deploy-$tag/.sha256"
    rm -rf "${releases:?}/$tag"
    mv "$tmp/deploy-$tag" "$releases/$tag"
    echo "Downloaded release $tag to $releases/$tag"
  fi
  # Keep the newest $RELEASES_KEPT bundles. The one being deployed was just written or reused, so it's never
  # among those removed.
  find "$releases" -mindepth 1 -maxdepth 1 -type d -name 'v*' -printf '%T@ %p\n' | sort -rn |
    tail -n +"$((RELEASES_KEPT + 1))" | cut -d' ' -f2- | while read -r old; do
      [ "$old" = "$releases/$tag" ] && continue
      rm -rf "$old"
      echo "Removed old release $(basename "$old")"
    done
) 9>"$releases/.lock"

exec bash "$releases/$tag/deploy.sh" "$service" "$tag"
