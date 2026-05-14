#!/bin/bash
# Daily dealer opportunity pipeline runner.
# Installed via cron at /etc/cron.d/dealer_opportunities (09:30 EDT).
#
# Hardened 2026-05-14:
#   - Pre-flight pg_is_in_recovery() check: if THIS host is a standby,
#     skip (the actual primary's cron handles the run).
#   - Retry on transient failure: up to 3 attempts, 30 min apart.
#     Covers the case where 5433 is briefly unreachable at cron-fire time
#     (the 2026-05-13 PG wipe at 09:06 killed the 09:30 run with no retry).
#   - Single Telegram failure alert only after all attempts exhausted.
#
# Steps per attempt:
#   1. Verify local 5433 is a writable primary
#   2. Run the opportunity pipeline (MMR sweep + rBook + score + write)
#   3. Send Telegram summary on first success (top 5 by score)
#
# Logs:  /var/log/dealer_opportunities.log (rotates via system logrotate)

set -uo pipefail   # NOTE: not -e — we want to handle non-zero exits ourselves

LOG=/var/log/dealer_opportunities.log
PIPELINE=/opt/expwholesale/dealer_opportunity_pipeline.py
PYBIN=/opt/expwholesale/venv/bin/python3
DB_URL="postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"
DB_HOST=localhost
DB_PORT=5433
DB_NAME=expwholesale
DB_USER=expuser
DB_PASS='ExpWholesale2026!'

MAX_ATTEMPTS=3
RETRY_SLEEP_SEC=1800   # 30 min

# Telegram creds: shared with the auto-failover watchdog.
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

# ── Run one attempt of the pipeline. Returns:
#      0 = success
#      1 = transient (DB unreachable) — retry
#      2 = standby — skip permanently (NOT a failure)
#     >0 = hard failure — retry
run_pipeline_once() {
    local attempt=$1

    # 1. Pre-flight: is the DB reachable and is this the primary?
    local is_primary
    is_primary=$(PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" \
                  -d "$DB_NAME" -tAc "SELECT NOT pg_is_in_recovery()" 2>/dev/null)

    if [ "$is_primary" = "f" ]; then
        echo "$(ts) attempt $attempt: this host is a standby — skipping run (primary should handle)" >> "$LOG"
        return 2
    fi

    if [ "$is_primary" != "t" ]; then
        echo "$(ts) attempt $attempt: DB unreachable (pg_is_in_recovery returned empty)" >> "$LOG"
        return 1
    fi

    # 2. Run pipeline
    echo "$(ts) attempt $attempt: starting pipeline run" >> "$LOG"
    cd /opt/expwholesale
    DATABASE_URL="$DB_URL" "$PYBIN" -u "$PIPELINE" 2>&1 | tee -a "$LOG"
    local pipe_exit=${PIPESTATUS[0]}
    echo "$(ts) attempt $attempt: pipeline exit=$pipe_exit" >> "$LOG"
    return $pipe_exit
}

# ── Top-level: retry loop ────────────────────────────────────────────────
echo "$(ts) ─── opportunity run start ───" >> "$LOG"

attempt=1
last_exit=99
while [ $attempt -le $MAX_ATTEMPTS ]; do
    run_pipeline_once $attempt
    last_exit=$?

    if [ $last_exit -eq 0 ]; then
        break
    fi
    if [ $last_exit -eq 2 ]; then
        # standby — quiet exit, no Telegram
        echo "$(ts) ─── opportunity run end (skipped — standby) ───" >> "$LOG"
        exit 0
    fi

    # Transient or hard failure — retry if we have attempts left
    if [ $attempt -lt $MAX_ATTEMPTS ]; then
        echo "$(ts) sleeping ${RETRY_SLEEP_SEC}s before retry $((attempt+1))/${MAX_ATTEMPTS}" >> "$LOG"
        sleep $RETRY_SLEEP_SEC
        attempt=$((attempt+1))
    else
        break
    fi
done

if [ $last_exit -ne 0 ]; then
    echo "$(ts) all $MAX_ATTEMPTS attempts failed (last exit=$last_exit) — sending failure alert" >> "$LOG"
    send_telegram "⚠️ EW Opportunity pipeline FAILED after ${MAX_ATTEMPTS} attempts (last exit=${last_exit}). Check /var/log/dealer_opportunities.log"
    exit "$last_exit"
fi

# ── Telegram summary (only on success) ───────────────────────────────────
SUMMARY=$(PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -t -A -F '|' <<'SQL'
SELECT
  (SELECT COUNT(*) FROM dealer_opportunities WHERE snapshot_date=CURRENT_DATE) AS total_today,
  (SELECT COUNT(*) FROM dealer_opportunities WHERE snapshot_date=CURRENT_DATE AND status='new') AS new_today,
  (SELECT COUNT(*) FROM dealer_opportunities WHERE snapshot_date=CURRENT_DATE AND status='pursuing') AS pursuing_today,
  (SELECT MAX(score) FROM dealer_opportunities WHERE snapshot_date=CURRENT_DATE) AS top_score,
  (SELECT COALESCE(SUM(dollars_under_mmr),0) FROM dealer_opportunities WHERE snapshot_date=CURRENT_DATE) AS total_under;
SQL
)
TOTAL=$(echo "$SUMMARY" | cut -d'|' -f1)
NEW=$(echo "$SUMMARY" | cut -d'|' -f2)
PURSUING=$(echo "$SUMMARY" | cut -d'|' -f3)
TOP=$(echo "$SUMMARY" | cut -d'|' -f4)
TOTAL_UNDER=$(echo "$SUMMARY" | cut -d'|' -f5)

TOP5=$(PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -t -A -F '|' <<'SQL'
SELECT
  o.score,
  o.year || ' ' || o.make || ' ' || o.model AS ymm,
  '$' || trim(to_char(o.asking_price, 'FM999,999,999')) AS ask,
  '$' || trim(to_char(o.dollars_under_mmr, 'FM999,999,999')) AS delta,
  d.name AS dealer
FROM dealer_opportunities o
JOIN dealers d ON d.id = o.dealer_id
WHERE o.snapshot_date = CURRENT_DATE
ORDER BY o.score DESC
LIMIT 5;
SQL
)

TOP5_TEXT=""
while IFS='|' read -r score ymm ask delta dealer; do
    [ -z "$score" ] && continue
    TOP5_TEXT="${TOP5_TEXT}
• [${score}] ${ymm} — ${ask} (${delta} under MMR) @ ${dealer}"
done <<< "$TOP5"

MSG="🎯 <b>EW Daily Opportunities — $(date '+%a %b %d')</b>
${TOTAL} candidates · ${NEW} new · ${PURSUING} pursuing
Top score: ${TOP} · ${TOTAL_UNDER} aggregate under MMR
${TOP5_TEXT}

View: https://experience-wholesale.net/opportunities"

send_telegram "$MSG"

echo "$(ts) telegram sent" >> "$LOG"
echo "$(ts) ─── opportunity run end (total=$TOTAL top_score=$TOP attempts=$attempt) ───" >> "$LOG"
