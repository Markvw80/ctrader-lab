#!/bin/sh
# Run on the NAS inside /volume1/docker/ctrader-lab
set -eu
git pull --ff-only
COMMIT=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then COMMIT="${COMMIT}-dirty"; fi
GIT_COMMIT=$COMMIT docker compose up -d --build
echo "Deployed ctrader-lab @ $COMMIT"
