#!/usr/bin/env bash
# ======================================================================
# partdb_auto_sync.sh
#
# Watches the Part-DB database and automatically runs partdb_sync.py
# the moment anything changes (new part added, stock quantity changed)
# so the production dashboard quantities refresh on their own.
#
# Usage
# -----
#   bash partdb_auto_sync.sh            # watch forever (foreground / systemd)
#   bash partdb_auto_sync.sh --once     # run one sync (good for cron)
#
# Config (environment variables, with defaults for the pnp-db server)
# --------------------------------------------------------------------
#   PARTDB_SOURCE_DB   Part-DB database file   (default: /home/server/parts-db-setup/db/app.db)
#   PRODUCTION_DB      production database     (default: <script dir>/production.db)
#   SYNC_SCRIPT        partdb_sync.py          (default: <script dir>/partdb_sync.py)
#   POLL_SECONDS       change check interval   (default: 5)
#   SETTLE_SECONDS     wait after a change     (default: 3)
#   FULL_INTERVAL      safety full sync period (default: 300)
#   LOG_FILE           log file                (default: <script dir>/partdb_auto_sync.log)
#
# Example
# --------
#   PARTDB_SOURCE_DB=/home/server/parts-db-setup/db/app.db \
#   PRODUCTION_DB=/home/server/Production/production-db/production.db \
#   bash partdb_auto_sync.sh
#
# Install as a service (runs at boot, restarts on crash):
#   sudo cp partdb-auto-sync.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now partdb-auto-sync
#   journalctl -u partdb-auto-sync -f        # watch it live
#
# Or with cron (one sync per minute):
#   crontab -e
#   * * * * * /usr/bin/flock -n /tmp/partdb-auto-sync.lock /bin/bash /home/server/Production/production-db/partdb_auto_sync.sh --once >> /home/server/Production/production-db/partdb_auto_sync.log 2>&1
# ======================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PARTDB_SOURCE_DB="${PARTDB_SOURCE_DB:-/home/server/parts-db-setup/db/app.db}"
PRODUCTION_DB="${PRODUCTION_DB:-$SCRIPT_DIR/production.db}"
SYNC_SCRIPT="${SYNC_SCRIPT:-$SCRIPT_DIR/partdb_sync.py}"
POLL_SECONDS="${POLL_SECONDS:-5}"
SETTLE_SECONDS="${SETTLE_SECONDS:-3}"
FULL_INTERVAL="${FULL_INTERVAL:-300}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/partdb_auto_sync.log}"
LOCK_FILE="/tmp/partdb-auto-sync.lock"

log() {
    printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"
}

die() {
    log "ERROR: $*"
    exit 1
}

source_signature() {
    stat -c '%Y:%s' "$PARTDB_SOURCE_DB" 2>/dev/null || echo "missing"
}

run_sync() {
    # one sync at a time
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "Skipped: another sync is already running"
        return 1
    fi

    log "Sync start ($PARTDB_SOURCE_DB -> $PRODUCTION_DB)"

    if python3 "$SYNC_SCRIPT" "$PARTDB_SOURCE_DB" "$PRODUCTION_DB" >>"$LOG_FILE" 2>&1; then
        log "Sync OK"
        return 0
    fi

    log "Sync FAILED - retrying from a read-only snapshot copy..."
    SNAPSHOT="$(mktemp /tmp/partdb-snapshot-XXXXXX.db)"
    if cp "$PARTDB_SOURCE_DB" "$SNAPSHOT" 2>>"$LOG_FILE" \
        && python3 "$SYNC_SCRIPT" "$SNAPSHOT" "$PRODUCTION_DB" >>"$LOG_FILE" 2>&1; then
        log "Sync OK (from snapshot)"
        rm -f "$SNAPSHOT"
        return 0
    fi
    rm -f "$SNAPSHOT"
    log "Sync FAILED (also from snapshot) - will retry on next change"
    return 1
}

# ----------------------------------------------------------------------
# Pre-flight checks
# ----------------------------------------------------------------------

[ -f "$PARTDB_SOURCE_DB" ] || die "Part-DB source not found: $PARTDB_SOURCE_DB"
[ -f "$PRODUCTION_DB" ] || die "Production DB not found: $PRODUCTION_DB"
[ -f "$SYNC_SCRIPT" ] || die "Sync script not found: $SYNC_SCRIPT"

# ----------------------------------------------------------------------
# --once mode
# ----------------------------------------------------------------------

if [ "${1:-}" = "--once" ]; then
    run_sync
    exit $?
fi

# ----------------------------------------------------------------------
# Watch loop
# ----------------------------------------------------------------------

log "Watching Part-DB: $PARTDB_SOURCE_DB (poll ${POLL_SECONDS}s, settle ${SETTLE_SECONDS}s, full-sync ${FULL_INTERVAL}s)"
log "Production DB:   $PRODUCTION_DB"

run_sync

last_signature="$(source_signature)"
last_full="$(date +%s)"

trap 'log "Stopped by user"; exit 0' INT TERM

while true; do
    sleep "$POLL_SECONDS"

    signature="$(source_signature)"

    if [ "$signature" != "$last_signature" ]; then
        log "Change detected in Part-DB database"
        # let the writer finish before reading
        sleep "$SETTLE_SECONDS"
        last_signature="$(source_signature)"
        run_sync
        last_full="$(date +%s)"
    elif [ $(( $(date +%s) - last_full )) -ge "$FULL_INTERVAL" ]; then
        last_full="$(date +%s)"
        run_sync
    fi
done
