#!/bin/bash
# Wait for v5+v6 to fully complete, then launch v7 (Haiku) and v8 (Hermes)
# in parallel. Each runs 500 inbound sims, single Coach pass at end.

TG_BOT="8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM"
TG_CHAT="7985611488"
tg() { curl -s --max-time 8 "https://api.telegram.org/bot${TG_BOT}/sendMessage" -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" > /dev/null 2>&1; }

LOGDIR=/opt/expwholesale/logs

# Wait for both v5 and v6 to finish
echo "[$(date +%H:%M:%S)] waiting for v5 and v6 to finish..."
while pgrep -f "sim_outbound.py" > /dev/null 2>&1; do sleep 30; done
echo "[$(date +%H:%M:%S)] v5 and v6 done; launching v7 + v8 in parallel"

tg "[EW Coach] v5+v6 complete. Launching v7 (Haiku op, 5 workers) and v8 (Hermes op, 4 workers) inbound sims in parallel — 1000 more transcripts."

# v7 — Haiku operator (uses Anthropic API)
nohup /opt/expwholesale/auto_v7_500.sh > /opt/expwholesale/logs/auto_v7_500.out 2>&1 &
V7_PID=$!
echo "v7 PID=$V7_PID"

# v8 — Hermes operator (uses local GPU via tunnel)
nohup /opt/expwholesale/auto_v8_500.sh > /opt/expwholesale/logs/auto_v8_500.out 2>&1 &
V8_PID=$!
echo "v8 PID=$V8_PID"

tg "[EW Coach] v7 PID=$V7_PID v8 PID=$V8_PID — running. Will Telegram when each completes."

wait $V7_PID
echo "v7 finished"
wait $V8_PID
echo "v8 finished"

tg "[EW Coach] v7 AND v8 inbound batches both complete. Check /opt/expwholesale/logs/sim_results_v7*.* and v8*.* for transcripts + Coach reviews."
