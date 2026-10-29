#!/usr/bin/env bash
# Try changes locally before pushing: the image built from this working tree, optionally on a copy of production data.
#
#   scripts/test-deploy.sh up                 run the tests, build and start on port 8001 (all interfaces)
#   POT_SERVER=host scripts/test-deploy.sh snapshot
#                                             copy the live database from the home server into data/snapshots/
#   scripts/test-deploy.sh load [file]        load a snapshot (default: newest) into the test stack and compare
#                                             totals before and after the upgrade
#   scripts/test-deploy.sh down | logs | reset
#
# Snapshot settings: POT_SERVER (ssh host, required), POT_CONTAINER (default: the running the-pot container),
# POT_DOCKER (default: docker; e.g. "sudo docker").
set -euo pipefail
cd "$(dirname "$0")/.."

compose() { docker compose -f compose.test.yaml "$@"; }
URL=http://127.0.0.1:8001
SNAPSHOTS=data/snapshots

# Money totals and counts that an upgrade must not change. Missing tables (older schemas) count as 0.
TOTALS='
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
def q(sql):
    try:
        return c.execute(sql).fetchone()[0] or 0
    except sqlite3.OperationalError:
        return 0
for label, sql in [
    ("series", "SELECT COUNT(*) FROM series"),
    ("tournaments", "SELECT COUNT(*) FROM tournament"),
    ("players", "SELECT COUNT(*) FROM player"),
    ("payouts", "SELECT COUNT(*) FROM payout"),
    ("players owed (c)", "SELECT SUM(amount_cents) FROM payout WHERE status=\"owed\""),
    ("players paid (c)", "SELECT SUM(amount_cents) FROM payout WHERE status=\"paid\""),
    ("venues owed (c)", "SELECT SUM(amount_cents) FROM venue_payment WHERE status=\"owed\""),
    ("venues paid (c)", "SELECT SUM(amount_cents) FROM venue_payment WHERE status=\"paid\""),
]:
    print(f"{label:<18} {q(sql)}")
'

wait_up() {
    for _ in $(seq 1 60); do
        curl -fs -o /dev/null "$URL/healthz" && { echo "Test deployment running at $URL"; return; }
        sleep 1
    done
    echo "Not responding on $URL; recent logs:" >&2
    compose logs --tail 40 >&2
    exit 1
}

cmd_up() {
    .venv/bin/pytest -q
    compose up -d --build
    wait_up
}

cmd_snapshot() {
    : "${POT_SERVER:?set POT_SERVER to the home server ssh host}"
    local docker="${POT_DOCKER:-docker}" container="${POT_CONTAINER:-}"
    if [[ -z "$container" ]]; then
        container=$(ssh "$POT_SERVER" "$docker ps --filter ancestor=ghcr.io/monday-j/the-pot --format '{{.Names}}'" | head -1)
        [[ -n "$container" ]] || { echo "No running the-pot container found on $POT_SERVER (set POT_CONTAINER)" >&2; exit 1; }
    fi
    mkdir -p -m 700 "$SNAPSHOTS"
    local out="$SNAPSHOTS/pot-$(date +%Y-%m-%d-%H%M).db"
    # SQLite online backup (safe while the app runs in WAL mode) into the container's /tmp, never /data.
    ssh "$POT_SERVER" "$docker exec $container python -c \"import sqlite3; s=sqlite3.connect('/data/pot.db'); d=sqlite3.connect('/tmp/pot-snapshot.db'); s.backup(d); d.close()\""
    (umask 077; ssh "$POT_SERVER" "$docker exec $container cat /tmp/pot-snapshot.db" > "$out")
    ssh "$POT_SERVER" "$docker exec $container rm -f /tmp/pot-snapshot.db"
    echo "Saved $out ($(du -h "$out" | cut -f1)) from $container on $POT_SERVER"
}

cmd_load() {
    local file="${1:-$(ls -t "$SNAPSHOTS"/*.db 2>/dev/null | head -1)}"
    [[ -n "$file" && -f "$file" ]] || { echo "No snapshot to load (run snapshot first, or pass a file)" >&2; exit 1; }
    local before after
    before=$(python3 -I -c "$TOTALS" "$file")
    compose build
    compose stop pot-test >/dev/null 2>&1 || true
    # On stdin, not a mount: the snapshot is private to you (0600), so the container's user couldn't read a mount.
    compose run --rm -T --no-deps --entrypoint sh pot-test \
        -c 'cat > /data/pot.db && rm -f /data/pot.db-wal /data/pot.db-shm' < "$file"
    compose up -d
    wait_up  # startup runs the migrations
    after=$(compose exec -T pot-test python -c "$TOTALS" /data/pot.db)
    echo "Loaded $file"
    paste <(echo "$before") <(echo "$after" | awk '{print $NF}') | awk -F'\t' '
        BEGIN { printf "%-25s %12s %12s\n", "", "snapshot", "upgraded" }
        { n = split($1, a, " "); v = a[n]; label = substr($1, 1, length($1) - length(v)); bad = (v != $2)
          printf "%-25s %12s %12s%s\n", label, v, $2, bad ? "   <-- changed" : ""; if (bad) diff = 1 }
        END { if (diff) { print "Totals changed during the upgrade: check before pushing."; exit 1 }
              print "Totals unchanged by the upgrade." }'
}

case "${1:-}" in
    up) cmd_up ;;
    snapshot) cmd_snapshot ;;
    load) shift; cmd_load "$@" ;;
    down) compose down ;;
    logs) compose logs -f ;;
    reset) compose down -v ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
