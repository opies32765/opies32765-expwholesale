#!/bin/bash
TG_BOT="8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM"
TG_CHAT="7985611488"
tg() { curl -s --max-time 8 "https://api.telegram.org/bot${TG_BOT}/sendMessage" -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" > /dev/null 2>&1; }

LOGDIR=/opt/expwholesale/logs
mkdir -p $LOGDIR
T_START=$(date +%s)

tg "[EW Coach] v6 starting (Hermes partner, GPU): 500 sims, Hermes partner, 4 workers, NO Coach per batch."

set -a
source /etc/default/expwholesale-mcp
ANTHROPIC_API_KEY=$(systemctl show expwholesale -p Environment --value | tr " " "\n" | grep "^ANTHROPIC_API_KEY=" | cut -d= -f2-)
ELEVEN_API_KEY=dummy
ELEVENLABS_API_KEY=dummy
GOOGLE_APPLICATION_CREDENTIALS=/dev/null
export PARTNER_BACKEND=hermes
set +a

/opt/expwholesale/venv/bin/python3 /opt/expwholesale/sim_outbound.py \
    --bid 2009 \
    --runs 100 \
    --workers 4 \
    --out $LOGDIR/sim_results_v6.json \
    > $LOGDIR/sim_run_v6.log 2>&1

V5_EXIT=$?
T_V5=$(date +%s)
DUR=$((T_V5 - T_START))
N_OK=$(/opt/expwholesale/venv/bin/python3 -c "import json; print(len(json.load(open(\"/opt/expwholesale/logs/sim_results_v6.json\"))))" 2>/dev/null || echo "?")

tg "[EW Coach] v6 sims DONE in ${DUR}s. ${N_OK}/500 succeeded. Now running single Opus Coach pass on 30-transcript sample..."

if [ "$N_OK" = "0" ] || [ "$N_OK" = "?" ]; then
  tg "[EW Coach] v6 produced no transcripts. Check /opt/expwholesale/logs/sim_run_v6.log."
  exit 1
fi

# Single Opus Coach pass on a 30-transcript sample (avg, plus 5 worst, plus 5 best by avg turn count)
/opt/expwholesale/venv/bin/python3 - << "PYEOF"
import json, os, random, sys
sys.path.insert(0, "/opt/expwholesale")
import anthropic
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

sims = json.load(open("/opt/expwholesale/logs/sim_results_v6.json"))
print(f"loaded {len(sims)} sims", flush=True)
# Sample: 5 shortest, 5 longest, 20 random — 30 total
sims_sorted = sorted(sims, key=lambda x: x.get("n_turns", 0))
sample = sims_sorted[:5] + sims_sorted[-5:] + random.sample(sims_sorted[5:-5], min(20, max(0, len(sims_sorted)-10)))
print(f"sampled {len(sample)} for Coach", flush=True)

sp = open("/opt/expwholesale/outbound_system_prompt.txt").read()

transcripts = ""
for i, sim in enumerate(sample, 1):
    transcripts += f"\n\n=== TRANSCRIPT {i}/{len(sample)} archetype={sim.get(\"archetype\")} partner={sim.get(\"partner_name\")} turns={sim.get(\"n_turns\")} ===\n"
    for m in sim.get("transcript", []):
        role = m.get("role", "?")
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in c)
        transcripts += f"\n[{role}] {c[:600]}"

prompt = f"""You are reviewing {len(sample)} simulated outbound sales calls from \"Bill from Experience Wholesale\" - a voice bot pitching used cars to dealer partners.

CURRENT SYSTEM PROMPT (truncated):
{sp[:8000]}

TRANSCRIPTS:
{transcripts}

Score each transcript on 5 dimensions 0-10:
1. Naturalness — does Bill sound human?
2. Accuracy — facts/numbers correct?
3. Persistence — push back vs. fold?
4. Recovery — handle objections?
5. Closing — drive to SMS bid card + callback?

Then identify:
- Top 3 patterns of failure with quoted examples
- Top 3 patterns of success
- 5 specific edit suggestions (with confidence 0-1) for the system prompt

Return aggregate scores (average across all transcripts) and proposed edits as JSON in a code block."""

resp = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8000,
    messages=[{"role":"user","content":prompt}],
)
out = "".join(b.text for b in resp.content if hasattr(b,"text"))
open("/opt/expwholesale/logs/sim_results_v6_coach.md","w").write(out)
print(f"Coach review saved ({len(out)} bytes)")

# Extract scores
import re
scores_match = re.search(r"Naturalness[^\d]*([\d.]+)[^\d]*Accuracy[^\d]*([\d.]+)[^\d]*Persistence[^\d]*([\d.]+)[^\d]*Recovery[^\d]*([\d.]+)[^\d]*Closing[^\d]*([\d.]+)", out)
if scores_match:
    nat, acc, per, rec, clo = scores_match.groups()
    summary = f"v6 scores: Nat={nat} Acc={acc} Per={per} Rec={rec} Close={clo}"
    print(summary)
    open("/tmp/v6_scores.txt","w").write(summary)
PYEOF

T_END=$(date +%s)
TOTAL=$((T_END - T_START))
SCORES=$(cat /tmp/v6_scores.txt 2>/dev/null || echo "scores not parsed - see /opt/expwholesale/logs/sim_results_v6_coach.md")
COACH_BYTES=$(wc -c < /opt/expwholesale/logs/sim_results_v6_coach.md 2>/dev/null || echo "?")

tg "[EW Coach] FINAL: ${N_OK}/500 transcripts, total wall=${TOTAL}s. ${SCORES}. Coach report: ${COACH_BYTES} bytes. Path: /opt/expwholesale/logs/sim_results_v6_coach.md"
