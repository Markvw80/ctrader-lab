#!/bin/sh
# Deploy the current commit from this machine to the NAS over SSH.
# No git or GitHub access needed on the NAS. Usage: ./scripts/deploy-nas.sh [ssh-host]
set -eu
HOST=${1:-nas}
DEST=${CTLAB_NAS_DIR:-/volume1/docker/ctrader-lab}
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Uncommitted changes: commit first, so the deployed code matches a commit." >&2
  exit 1
fi
COMMIT=$(git rev-parse --short HEAD)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git archive --format=tar HEAD | tar -x -C "$TMP"

ssh "$HOST" "mkdir -p '$DEST/data/raw' '$DEST/data/parquet' '$DEST/results'"
# Code only: data/, results/ and .env on the NAS are never touched or deleted.
rsync -a --delete --exclude '/data/' --exclude '/results/' --exclude '/.env' "$TMP/" "$HOST:$DEST/"

ssh "$HOST" "cd '$DEST' && if [ ! -f .env ]; then
    cp .env.example .env && chmod 600 .env &&
    sed -i \"s/^PUID=.*/PUID=\$(id -u)/; s/^PGID=.*/PGID=\$(id -g)/\" .env &&
    echo 'created .env from .env.example (fill in cTrader credentials later)'; fi"
ssh "$HOST" "cd '$DEST' && GIT_COMMIT=$COMMIT docker compose up -d --build && docker compose ps"
echo "Deployed ctrader-lab @ $COMMIT to $HOST:$DEST"
