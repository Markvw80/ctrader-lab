#!/bin/sh
# Put cTrader credentials into the NAS .env without showing them on screen, in shell history
# or on a command line. Run in your own terminal: ./scripts/set-nas-secrets.sh [ssh-host]
set -eu
HOST=${1:-nas}
DEST=${CTLAB_NAS_DIR:-/volume1/docker/ctrader-lab}
cd "$(dirname "$0")/.."

local_val() {
  [ -f .env ] || return 0
  sed -nE "s/^$1[[:space:]]*=[[:space:]]*[\"']?([^\"']*)[\"']?[[:space:]]*\$/\1/p" .env | head -1
}

ask_secret() {  # $1 = name, $2 = default (may be empty)
  hint=""; [ -n "$2" ] && hint=" [Enter = use value from local .env]"
  printf '%s%s: ' "$1" "$hint" >&2
  stty -echo; read -r v; stty echo; echo >&2
  [ -z "$v" ] && v=$2
  printf '%s' "$v"
}

trap 'stty echo 2>/dev/null || true' EXIT INT
CID=$(ask_secret CTRADER_CLIENT_ID "$(local_val CTRADER_CLIENT_ID)")
CSEC=$(ask_secret CTRADER_CLIENT_SECRET "$(local_val CTRADER_CLIENT_SECRET)")
ATOK=$(ask_secret CTRADER_ACCESS_TOKEN "")
RTOK=$(ask_secret CTRADER_REFRESH_TOKEN "")
printf 'CTRADER_ACCOUNT_ID (optional, Enter = skip): ' >&2; read -r ACC

for pair in "CLIENT_ID:$CID" "CLIENT_SECRET:$CSEC" "ACCESS_TOKEN:$ATOK" "REFRESH_TOKEN:$RTOK"; do
  [ -n "${pair#*:}" ] || { echo "CTRADER_${pair%%:*} is empty, aborting." >&2; exit 1; }
done

# Values travel over the SSH channel's stdin, never as arguments.
{
  printf 'CTRADER_CLIENT_ID=%s\nCTRADER_CLIENT_SECRET=%s\n' "$CID" "$CSEC"
  printf 'CTRADER_ACCESS_TOKEN=%s\nCTRADER_REFRESH_TOKEN=%s\n' "$ATOK" "$RTOK"
  [ -n "$ACC" ] && printf 'CTRADER_ACCOUNT_ID=%s\n' "$ACC"
} | ssh "$HOST" "set -e; umask 077; cd '$DEST'; cat > .env.vals
  awk 'NR==FNR { k=\$0; sub(/=.*/, \"\", k); v=\$0; sub(/^[^=]*=/, \"\", v); val[k]=v; next }
       { k=\$0; sub(/[ \t]*=.*/, \"\", k)
         if (k in val) { print k \"=\\\"\" val[k] \"\\\"\"; seen[k]=1 } else print }
       END { for (k in val) if (!(k in seen)) print k \"=\\\"\" val[k] \"\\\"\" }' .env.vals .env > .env.tmp
  mv .env.tmp .env; rm -f .env.vals; setfacl -b .env 2>/dev/null || true; chmod 600 .env
  docker compose up -d >/dev/null 2>&1
  echo 'NAS .env updated (0600), container restarted.'
  docker compose exec -T lab ctlab api check || true"
