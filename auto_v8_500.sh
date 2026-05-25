#!/bin/bash
TG_BOT="8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM"
TG_CHAT="7985611488"
tg() { curl -s --max-time 8 "https://api.telegram.org/bot${TG_BOT}/sendMessage" -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" > /dev/null 2>&1; }

LOGDIR=/opt/expwholesale/logs
mkdir -p $LOGDIR
T_START=$(date +%s)

tg "[EW Coach] v8 starting: 500 INBOUND sims, Hermes operator (GPU), 4 workers."

set -a
source /etc/default/expwholesale-mcp
ANTHROPIC_API_KEY=$(systemctl show expwholesale -p Environment --value | tr " " "\n" | grep "^ANTHROPIC_API_KEY=" | cut -d= -f2-)
ELEVEN_API_KEY=dummy
ELEVENLABS_API_KEY=dummy
GOOGLE_APPLICATION_CREDENTIALS=/dev/null
PARTNER_BACKEND=hermes
set +a

/opt/expwholesale/venv/bin/python3 /opt/expwholesale/sim_inbound.py \
    --runs 100 --workers 4 \
    --out $LOGDIR/sim_results_v8.json \
    > $LOGDIR/sim_run_v8.log 2>&1

T_END=$(date +%s)
DUR=$((T_END - T_START))
N_OK=$(/opt/expwholesale/venv/bin/python3 -c "import json; print(len(json.load(open('/opt/expwholesale/logs/sim_results_v8.json'))))" 2>/dev/null || echo "?")

tg "[EW Coach] v8 sims DONE in ${DUR}s. ${N_OK}/500 succeeded. Running single Opus Coach pass on 30 samples..."

if [ "$N_OK" = "0" ] || [ "$N_OK" = "?" ]; then
  tg "[EW Coach] v8 produced no transcripts. Check /opt/expwholesale/logs/sim_run_v8.log."
  exit 1
fi

echo "[$(date +%H:%M:%S)] Coach skipped — out of Opus budget. Unified Hermes filter will run after all batches finish."

T_FINAL=$(date +%s)
TOTAL=$((T_FINAL - T_START))
SCORES="Coach skipped (Opus out of budget; Hermes filter pending)"
COACH_BYTES=0
tg "[EW Coach] v8 FINAL: ${N_OK}/500 transcripts, total wall=${TOTAL}s. ${SCORES}. Coach: ${COACH_BYTES}B at /opt/expwholesale/logs/sim_results_v8_coach.md"
