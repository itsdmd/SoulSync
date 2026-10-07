#!/usr/bin/env bash
# Build the fork's image on this machine for testing before publishing.
#   ./deploy/build-local.sh            -> soulsync-fork:local
#   ./deploy/build-local.sh mytag      -> soulsync-fork:mytag
# Then deploy deploy/portainer-stack.local.yml (without re-pulling the image).
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:-local}"
sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git diff --quiet 2>/dev/null; then sha="${sha}-dirty"; fi
docker build --build-arg COMMIT_SHA="${sha}" -t "soulsync-fork:${tag}" .
echo "Built soulsync-fork:${tag} (${sha})"
