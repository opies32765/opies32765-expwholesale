#!/bin/bash
# ew_pg_backup.sh — nightly EW PostgreSQL backup
# Runs on Contabo 1; rsyncs to Contabo 2 for off-site copy.
#
# Cron: 0 3 * * * /usr/local/bin/ew_pg_backup.sh

set -e
set -o pipefail

DB_NAME="expwholesale"
DB_USER="expuser"
DB_PORT="5433"
DB_HOST="localhost"
PGPASSWORD="ExpWholesale2026!"
export PGPASSWORD

LOCAL_DIR="/var/backups/expwholesale"
REMOTE_HOST="root@62.146.226.100"
REMOTE_DIR="/var/backups/expwholesale"

LOCAL_RETAIN_DAYS=14
REMOTE_RETAIN_DAYS=30

LOG="/var/log/ew_pg_backup.log"
TS=$(date '+%Y-%m-%d_%H%M')
DUMPFILE="${LOCAL_DIR}/expwholesale_${TS}.dump"

mkdir -p "$LOCAL_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

log "=== START backup ${TS} ==="

# 1) Dump (custom format — supports --jobs parallel restore + selective restore)
log "pg_dump → ${DUMPFILE}"
if ! pg_dump --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
        --format=custom --compress=6 \
        --no-owner --no-privileges \
        --file="$DUMPFILE" \
        "$DB_NAME" 2>>"$LOG"; then
    log "ERROR: pg_dump failed"
    exit 1
fi

SIZE=$(du -h "$DUMPFILE" | cut -f1)
log "pg_dump complete (${SIZE})"

# 2) Smoke-check the dump (pg_restore -l lists archive contents)
if ! pg_restore -l "$DUMPFILE" >/dev/null 2>>"$LOG"; then
    log "ERROR: dump file failed integrity check — keeping for inspection"
    exit 1
fi
log "dump integrity OK"

# 3) Rsync to Contabo 2 (off-site)
log "rsync → ${REMOTE_HOST}:${REMOTE_DIR}/"
if rsync -az --timeout=300 -e "ssh -o ConnectTimeout=15 -o BatchMode=yes -o StrictHostKeyChecking=no" \
        "$DUMPFILE" "${REMOTE_HOST}:${REMOTE_DIR}/" 2>>"$LOG"; then
    log "rsync complete"
else
    log "WARN: rsync to C2 failed — local copy still good; will retry tomorrow"
fi

# 4) Retention — local
log "purging local dumps older than ${LOCAL_RETAIN_DAYS} days"
find "$LOCAL_DIR" -name 'expwholesale_*.dump' -type f -mtime +${LOCAL_RETAIN_DAYS} -print -delete 2>>"$LOG" | while read f; do log "  deleted local: $(basename "$f")"; done

# 5) Retention — remote (best-effort)
ssh -o ConnectTimeout=15 -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE_HOST" \
    "mkdir -p ${REMOTE_DIR} && find ${REMOTE_DIR} -name 'expwholesale_*.dump' -type f -mtime +${REMOTE_RETAIN_DAYS} -delete" \
    2>>"$LOG" || log "WARN: remote retention purge failed"

# 6) Summary
LOCAL_COUNT=$(find "$LOCAL_DIR" -name 'expwholesale_*.dump' -type f | wc -l)
REMOTE_COUNT=$(ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE_HOST" \
    "ls ${REMOTE_DIR}/expwholesale_*.dump 2>/dev/null | wc -l" 2>/dev/null || echo "?")
log "DONE — local=${LOCAL_COUNT} dumps, remote=${REMOTE_COUNT} dumps"
log ""
