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

ssh "$HOST" "mkdir -p '$DEST/data/raw' '$DEST/data/parquet' '$DEST/results'"
# Replace the code only: data/, results/ and .env on the NAS are never touched or deleted.
# (UGOS restricts rsync, so: remove old code inside $DEST, then unpack the commit with tar.)
git archive --format=tar HEAD | ssh "$HOST" "set -e; cd '$DEST'
  case \"\$(pwd)\" in */ctrader-lab) ;; *) echo 'refusing: unexpected directory' >&2; exit 1;; esac
  find . -mindepth 1 -maxdepth 1 ! -name data ! -name results ! -name .env -exec rm -rf {} +
  tar -x"

ssh "$HOST" "cd '$DEST' && if [ ! -f .env ]; then
    cp .env.example .env && chmod 600 .env &&
    sed -i \"s/^PUID=.*/PUID=\$(id -u)/; s/^PGID=.*/PGID=\$(id -g)/\" .env &&
    echo 'created .env from .env.example (fill in cTrader credentials later)'; fi
  # /volume1/docker hands out inherited ACLs (rwx for everyone): strip them, secrets stay 0600
  setfacl -b .env 2>/dev/null || true; chmod 600 .env"
ssh "$HOST" "cd '$DEST' && GIT_COMMIT=$COMMIT docker compose up -d --build && docker compose ps"
echo "Deployed ctrader-lab @ $COMMIT to $HOST:$DEST"
