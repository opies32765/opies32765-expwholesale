#!/bin/bash
# Hourly sync of EW code from Contabo 1 (primary) -> Contabo 2 (warm standby).
# DB syncs via PG streaming replication. This handles the code.
#
# 2026-07-31: added failure alerting. This ran clean for 500+ consecutive
# hours, but if it ever started failing — key rotation, C2 disk full, network —
# it would have logged a nonzero exit to a file nobody reads, and C2 would
# quietly drift while PG replication kept reporting perfect health. The
# failover watchdog on C3 validates the DATABASE standby; nothing validated
# the code. Now the mirror reports its own failure, which is the only place
# that signal is authoritative.
set -uo pipefail
LOG=/var/log/c2_sync.log
STATE=/var/lib/ew-c2-sync
mkdir -p "$STATE"
exec >> $LOG 2>&1

ALERT_FROM='EW Ops Alerts <alerts@experience-wholesale.net>'
ALERT_TO="${EW_ALERT_TO:-opies32765@gmail.com}"
RESEND_API_KEY=$(systemctl show expwholesale -p Environment --no-pager \
                 | tr ' ' '\n' | grep '^RESEND_API_KEY=' | cut -d= -f2-)
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

# at most one alert per 6h so a persistent fault warns without spamming
alert() {
  local subject="$1" body="$2" f="$STATE/last_alert" now
  now=$(date +%s)
  if [[ -f "$f" ]] && (( now - $(cat "$f" 2>/dev/null || echo 0) < 21600 )); then return; fi
  echo "$now" > "$f"
  curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
       --data-urlencode "chat_id=${TG_CHAT}" \
       --data-urlencode "text=🔴 EW C2 CODE MIRROR: $subject — $body" --max-time 10 >/dev/null 2>&1 || true
  [[ -z "$RESEND_API_KEY" ]] && return 0
  local payload
  payload=$(SUBJ="$subject" BODY="$body" FROM="$ALERT_FROM" TO="$ALERT_TO" python3 - <<'PY' 2>/dev/null
import json, os
html = ('<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a;max-width:560px">'
        '<div style="border-left:4px solid #b3261e;padding:2px 0 2px 14px">'
        '<div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#b3261e;'
        'font-weight:600">EW C2 code mirror</div>'
        '<div style="font-size:19px;font-weight:600;margin-top:2px">%s</div></div>'
        '<p>%s</p>'
        '<p style="font-size:12.5px;color:#666;margin-top:20px">C1 mirrors application code to C2 hourly at :17 '
        '(/usr/local/bin/sync_expwholesale_to_c2.sh). The database replicates separately by PG streaming and is '
        'unaffected by this — but a failover while this is broken would bring up stale code against a current '
        'database. Log: /var/log/c2_sync.log</p></div>') % (os.environ["SUBJ"], os.environ["BODY"])
print(json.dumps({"from": os.environ["FROM"],
                  "to": [a.strip() for a in os.environ["TO"].split(",") if a.strip()],
                  "subject": os.environ["SUBJ"], "html": html}))
PY
)
  [[ -z "$payload" ]] && return 0
  curl -fsS -X POST "https://api.resend.com/emails" \
       -H "Authorization: Bearer ${RESEND_API_KEY}" \
       -H "Content-Type: application/json" \
       -d "$payload" --max-time 15 >/dev/null 2>&1 || echo "  alert email failed"
}

echo "[$(date -Iseconds)] sync start"
rsync -a --delete \
  --exclude="*.log" --exclude="*.bak*" --exclude="__pycache__" \
  --exclude="venv/" --exclude=".git/" --exclude="*.pyc" \
  --exclude="vauto_reports/" --exclude="accutrade_reports/" --exclude="ipacket_reports/" \
  --exclude="thumb_cache/" --exclude="static/uploads/" --exclude="_README_*.md" \
  /opt/expwholesale/ root@84.46.244.0:/opt/expwholesale/
rc=$?
echo "[$(date -Iseconds)] sync done (exit $rc)"

if (( rc != 0 )); then
  alert "rsync to C2 failed (exit $rc)" \
        "The hourly code mirror C1 -> C2 exited $rc. C2 is no longer receiving application code and will drift further every hour until this is fixed. Database replication is separate and is NOT affected."
  exit $rc
fi

# Success: confirm C2 actually holds the same app.py we just pushed. Catches a
# "successful" rsync that wrote nothing (wrong path, read-only remote).
here=$(md5sum /opt/expwholesale/app.py 2>/dev/null | cut -d' ' -f1)
there=$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@84.46.244.0 \
        "md5sum /opt/expwholesale/app.py 2>/dev/null | cut -d' ' -f1" 2>/dev/null)
if [[ -n "$here" && -n "$there" && "$here" != "$there" ]]; then
  alert "C2 app.py does not match C1 after a successful sync" \
        "rsync exited 0 but C2's app.py md5 ($there) still differs from C1's ($here). The mirror is reporting success without landing the code."
else
  rm -f "$STATE/last_alert" 2>/dev/null || true
fi
echo "[$(date -Iseconds)] verify: c1=$here c2=$there"
