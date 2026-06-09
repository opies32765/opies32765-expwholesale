#!/usr/bin/env bash
# EW Gemini/Vertex cost canary — Telegram alert if Gemini Vision WASTE spikes.
# Catches a recurrence of the dealer_completion ext_color re-bill loop that drove
# the Vertex bill +62% (fixed 2026-06-08 via the color_attempts<4 cap). Counts the
# "no color (gemini returned ...)" failure lines (= wasted PAID Vision calls) in a
# trailing window and Telegrams if they exceed a threshold.
#
# Install: cron every 3h (see ew_gemini_canary cron). Creds are SOURCED from
# /etc/ew_failover.env — never hardcoded here.  Canonical copy in ops/scripts/.
set -uo pipefail

WINDOW_MIN="${EW_CANARY_WINDOW_MIN:-360}"             # trailing window (6h)
WASTE_THRESHOLD="${EW_CANARY_WASTE_THRESHOLD:-1200}"  # wasted calls in window -> alert
COOLDOWN_MIN="${EW_CANARY_COOLDOWN_MIN:-720}"         # 12h min between alerts
STATEFILE="/var/run/ew_gemini_canary.last"
LOG="/var/log/ew_gemini_canary.log"
ENVFILE="${EW_TELEGRAM_ENVFILE:-/etc/ew_failover.env}"

ts() { date '+%Y-%m-%dT%H:%M:%S'; }

# Telegram creds — sourced from the env file, never echoed.
if [ -f "$ENVFILE" ]; then set -a; . "$ENVFILE" 2>/dev/null || true; set +a; fi
BOT="${TELEGRAM_BOT_TOKEN:-${TG_BOT:-${TG_TOKEN:-}}}"
CHAT="${TELEGRAM_CHAT_ID:-${TG_CHAT:-}}"

tg() {
  if [ -z "$BOT" ] || [ -z "$CHAT" ]; then
    echo "$(ts) NO_TELEGRAM_CREDS (set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in $ENVFILE)" >>"$LOG"
    return 1
  fi
  curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT}/sendMessage" \
       --data-urlencode chat_id="${CHAT}" \
       --data-urlencode parse_mode=HTML \
       --data-urlencode "text=$1" >/dev/null 2>>"$LOG" \
    && echo "$(ts) telegram_sent" >>"$LOG" \
    || echo "$(ts) telegram_SEND_FAIL" >>"$LOG"
}

# --- test mode: prove delivery + show the active thresholds ---
if [ "${1:-}" = "--test" ]; then
  tg "🧪 <b>EW Gemini cost canary armed</b>
Watching Vertex/Gemini Vision waste on C1.
Alerts if &gt; ${WASTE_THRESHOLD} failed color calls / ${WINDOW_MIN} min (a runaway like the ext_color loop).
$(ts)"
  echo "$(ts) test mode — telegram attempted" >>"$LOG"
  exit 0
fi

# --- count WASTED vision calls (failed color extractions) in the window ---
WASTE=$(journalctl -u dealer-completion.service --since "${WINDOW_MIN} min ago" --no-pager 2>/dev/null \
        | grep -cE "no color \(gemini returned"); WASTE="${WASTE:-0}"
TOTAL=$(journalctl -u dealer-completion.service --since "${WINDOW_MIN} min ago" --no-pager 2>/dev/null \
        | grep -cE "no color \(gemini returned|-> [A-Z]|gemini call timed"); TOTAL="${TOTAL:-0}"
echo "$(ts) window=${WINDOW_MIN}m wasted=${WASTE} total=${TOTAL} thr=${WASTE_THRESHOLD}" >>"$LOG"

[ "$WASTE" -lt "$WASTE_THRESHOLD" ] && exit 0

# --- cooldown so we don't spam ---
now=$(date +%s); last=0
[ -f "$STATEFILE" ] && last="$(cat "$STATEFILE" 2>/dev/null || echo 0)"
if [ $(( (now - last) / 60 )) -lt "$COOLDOWN_MIN" ]; then
  echo "$(ts) over_threshold wasted=${WASTE} but within cooldown" >>"$LOG"
  exit 0
fi
echo "$now" >"$STATEFILE"

EST=$(awk "BEGIN{printf \"%.2f\", ${WASTE}*0.0003}")
tg "⚠️ <b>EW Vertex/Gemini cost canary</b>
${WASTE} <b>wasted</b> Gemini Vision calls in ${WINDOW_MIN} min (of ${TOTAL} total) — threshold ${WASTE_THRESHOLD}.
The ext_color re-bill loop may be back (cap removed, or a new dealer's photos can't be fetched).
~\$${EST} burned this window. Check: <code>journalctl -u dealer-completion -n 60</code>"
echo "$(ts) ALERT sent wasted=${WASTE}" >>"$LOG"
