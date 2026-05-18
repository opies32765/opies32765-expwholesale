#!/bin/bash
# Daily encore-comps pipeline runner.
# Installed via cron at /etc/cron.d/encore_comps (10:30 EDT).
#
# Patterned after /usr/local/bin/dealer_opportunities_cron.sh.
#
# Steps per attempt:
#   1. pg_is_in_recovery() preflight — standbys skip silently.
#   2. Run encore_comps_pipeline.py (MMR + ManheimTransactions + rBook + trends).
#   3. Telegram digest on first success (count of units refreshed, today's
#      trend verdict mix).
# Retries up to 3 times 30 min apart on transient failures.
#
# Logs: /var/log/encore_comps.log

set -uo pipefail

LOG=/var/log/encore_comps.log
PIPELINE=/opt/expwholesale/encore_comps_pipeline.py
PYBIN=/opt/expwholesale/venv/bin/python3
DB_URL="postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"
DB_HOST=localhost
DB_PORT=5433
DB_NAME=expwholesale
DB_USER=expuser
DB_PASS='ExpWholesale2026!'
DEALER_SLUG="${ENCORE_COMPS_SLUG:-encore}"

MAX_ATTEMPTS=3
RETRY_SLEEP_SEC=1800

[ -r /etc/ew_failover.env ] && set -a && . /etc/ew_failover.env && set +a
TG_BOT="${TELEGRAM_BOT_TOKEN:-8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM}"
TG_CHAT="${TELEGRAM_CHAT_ID:-7985611488}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot$TG_BOT/sendMessage" \
         --data-urlencode "chat_id=$TG_CHAT" \
         --data-urlencode "text=$1" \
         --data-urlencode "parse_mode=HTML" \
         > /dev/null
}

run_once() {
    local attempt=$1
    local is_primary
    is_primary=$(PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" \
                  -d "$DB_NAME" -tAc "SELECT NOT pg_is_in_recovery()" 2>/dev/null)
    if [ "$is_primary" = "f" ]; then
        echo "$(ts) attempt $attempt: standby — skipping" >> "$LOG"
        return 2
    fi
    if [ "$is_primary" != "t" ]; then
        echo "$(ts) attempt $attempt: DB unreachable" >> "$LOG"
        return 1
    fi

    echo "$(ts) attempt $attempt: starting" >> "$LOG"
    cd /opt/expwholesale
    DATABASE_URL="$DB_URL" "$PYBIN" -u "$PIPELINE" --dealer-slug "$DEALER_SLUG" 2>&1 | tee -a "$LOG"
    local pipe_exit=${PIPESTATUS[0]}
    echo "$(ts) attempt $attempt: exit=$pipe_exit" >> "$LOG"
    return $pipe_exit
}

echo "$(ts) === encore_comps run start ===" >> "$LOG"

attempt=1
last_exit=99
while [ $attempt -le $MAX_ATTEMPTS ]; do
    run_once $attempt
    last_exit=$?
    [ $last_exit -eq 0 ] && break
    if [ $last_exit -eq 2 ]; then
        echo "$(ts) === run end (standby skip) ===" >> "$LOG"
        exit 0
    fi
    if [ $attempt -lt $MAX_ATTEMPTS ]; then
        echo "$(ts) sleeping ${RETRY_SLEEP_SEC}s before retry $((attempt+1))" >> "$LOG"
        sleep $RETRY_SLEEP_SEC
        attempt=$((attempt+1))
    else
        break
    fi
done

if [ $last_exit -ne 0 ]; then
    echo "$(ts) all $MAX_ATTEMPTS attempts failed (exit=$last_exit)" >> "$LOG"
    send_telegram "ENCORE COMPS pipeline FAILED after ${MAX_ATTEMPTS} attempts (exit=${last_exit}). Check /var/log/encore_comps.log"
    exit "$last_exit"
fi

# Telegram digest
SUMMARY=$(PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -t -A -F '|' <<SQL
SELECT
  (SELECT COUNT(*) FROM dealer_inventory_comps dic
     JOIN dealer_inventory di ON di.id = dic.dealer_inventory_id
     JOIN dealers d ON d.id = di.dealer_id
    WHERE dic.snapshot_date = CURRENT_DATE AND d.portal_slug='${DEALER_SLUG}') AS units,
  (SELECT COUNT(*) FROM dealer_inventory_comps dic
     JOIN dealer_inventory di ON di.id = dic.dealer_inventory_id
     JOIN dealers d ON d.id = di.dealer_id
    WHERE dic.snapshot_date = CURRENT_DATE AND d.portal_slug='${DEALER_SLUG}'
      AND dic.mmr_comp_value IS NOT NULL) AS with_mmr,
  (SELECT COUNT(*) FROM dealer_inventory_comps dic
     JOIN dealer_inventory di ON di.id = dic.dealer_inventory_id
     JOIN dealers d ON d.id = di.dealer_id
    WHERE dic.snapshot_date = CURRENT_DATE AND d.portal_slug='${DEALER_SLUG}'
      AND dic.rbook_p50 IS NOT NULL) AS with_rbook;
SQL
)
UNITS=$(echo "$SUMMARY" | cut -d'|' -f1)
W_MMR=$(echo "$SUMMARY" | cut -d'|' -f2)
W_RB=$(echo "$SUMMARY" | cut -d'|' -f3)

MSG="<b>Encore Comps — $(date '+%a %b %d')</b>
${UNITS} units refreshed
${W_MMR} with MMR · ${W_RB} with rBook
Portal: https://experience-wholesale.net/partner/${DEALER_SLUG}"

send_telegram "$MSG"
echo "$(ts) telegram sent" >> "$LOG"
echo "$(ts) === encore_comps run end (units=${UNITS}) ===" >> "$LOG"
