#!/usr/bin/env bash
# Fed over ssh by release.yml's deploy jobs. Downloads a release's deploy bundle
# from GitHub Releases into /opt/auth-broker/releases/<tag>/ (skipped when that
# release is already there with the same checksum), then runs the bundle's own
# deploy.sh, so the host only ever deploys what the release published.
# Usage: fetch-release.sh <service> <tag> <bundle url> <bundle sha256>
set -euo pipefail

service="$1"
tag="$2"
url="$3"
sha256="$4"
releases="${DEPLOY_DIR:-/opt/auth-broker}/releases"
mkdir -p "$releases"

# The two deploy jobs of a release can run at once; only one downloads.
(
  flock 9
  if [ "$(cat "$releases/$tag/.sha256" 2>/dev/null)" = "$sha256" ]; then
    echo "Release $tag is already on the host"
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
) 9>"$releases/.lock"

exec bash "$releases/$tag/deploy.sh" "$service" "$tag"
