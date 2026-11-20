#!/usr/bin/env bash
# Build Hindsight's control-plane UI with api-key-mode.patch, from upstream's own Dockerfile.
#   control-plane/build.sh <hindsight version, e.g. 1.2.1> <image tag> [docker buildx build args...]
# Fails, building nothing, if the patch doesn't apply to that version.
set -euo pipefail
version=$1 image=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT

git clone --quiet --depth 1 --branch "v$version" https://github.com/vectorize-io/hindsight.git "$src"
git -C "$src" apply --verbose "$here/api-key-mode.patch"
docker buildx build "$src" -f "$src/docker/standalone/Dockerfile" --target cp-only -t "$image" \
  --label org.opencontainers.image.title=hindsight-ui \
  --label org.opencontainers.image.description="Hindsight's control-plane UI with per-user API-key sign-in" \
  --label org.opencontainers.image.version="$version" \
  --label org.opencontainers.image.source=https://github.com/omar2535/hindsight-gateway \
  --label org.opencontainers.image.licenses=MIT \
  "$@"
