#!/usr/bin/env bash
#
# Seeds a Redis/Valkey target with a large, realistically shaped keyspace so the
# key browser can be exercised against something like production data.
#
#   scripts/seed-keys.sh                    # 1,000,000 keys into localhost:6479
#   scripts/seed-keys.sh -n 50000 -p 6480   # smaller set into the Valkey target
#   scripts/seed-keys.sh --flush            # empty the database first
#
# Keys are written through redis-cli --pipe, which streams raw protocol instead
# of waiting for a reply per command. One round trip per command would make a
# million keys take hours.
set -euo pipefail

HOST=localhost
PORT=6479
COUNT=1000000
FLUSH=false

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--host)  HOST="$2"; shift 2 ;;
        -p|--port)  PORT="$2"; shift 2 ;;
        -n|--count) COUNT="$2"; shift 2 ;;
        --flush)    FLUSH=true; shift ;;
        --help)     usage 0 ;;
        *)          echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

# The port the CLI actually dials. Normally the one given, but running inside the
# pod bypasses the published ports: there each server still listens on its
# canonical port, and only the host side is shifted out of the way (see
# deploy/keydra-dev.yaml), so the shift has to be undone.
DIAL_PORT=$PORT

if ! command -v redis-cli >/dev/null 2>&1; then
    # The dev pod ships one, so fall back to it rather than demanding a local install.
    if command -v podman >/dev/null 2>&1 && podman container exists keydra-dev-redis 2>/dev/null; then
        case "$PORT" in
            6479) DIAL_PORT=6379 ;;
            6480) DIAL_PORT=6380 ;;
        esac
        redis_cli() { podman exec -i keydra-dev-redis redis-cli "$@"; }
    else
        echo "redis-cli not found, and the keydra-dev-redis container is not running." >&2
        exit 1
    fi
else
    redis_cli() { command redis-cli "$@"; }
fi

echo "Seeding $COUNT keys into $HOST:$PORT"

if [[ "$FLUSH" == true ]]; then
    echo "Flushing the database first"
    redis_cli -h "$HOST" -p "$DIAL_PORT" FLUSHDB >/dev/null
fi

# Generates RESP protocol on stdout. Types and namespaces are mixed so the
# namespace tree has several levels and every value editor has something to open.
python3 - "$COUNT" <<'PY' | redis_cli -h "$HOST" -p "$DIAL_PORT" --pipe
import sys

total = int(sys.argv[1])
out = sys.stdout.write

def cmd(*args):
    out(f"*{len(args)}\r\n")
    for a in args:
        a = str(a)
        out(f"${len(a.encode())}\r\n{a}\r\n")

# Rough shape of a real application's keyspace: mostly cache and session strings,
# a long tail of structured records.
for i in range(total):
    bucket = i % 100
    if bucket < 40:
        cmd("SET", f"cache:page:{i // 1000}:{i}", f"payload-{i}")
    elif bucket < 65:
        # Sessions expire, so the browser has TTLs to display.
        cmd("SETEX", f"session:{i}", 3600 + (i % 3600), f"token-{i}")
    elif bucket < 80:
        cmd("HSET", f"user:{i}:profile", "name", f"user{i}", "email", f"user{i}@example.com", "age", 20 + (i % 50))
    elif bucket < 88:
        cmd("RPUSH", f"cart:{i}:items", f"sku-{i}-1", f"sku-{i}-2", f"sku-{i}-3")
    elif bucket < 94:
        cmd("SADD", f"tags:post:{i}", "redis", "valkey", f"tag-{i % 50}")
    elif bucket < 98:
        cmd("ZADD", f"leaderboard:{i % 20}", i % 1000, f"player-{i}")
    else:
        cmd("XADD", f"events:orders:{i % 10}", "*", "order", str(i), "status", "created")
PY

echo
echo "Done. Keyspace now holds:"
redis_cli -h "$HOST" -p "$DIAL_PORT" DBSIZE
