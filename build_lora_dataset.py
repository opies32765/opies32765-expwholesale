"""build_lora_dataset.py (2026-06-11) — assemble a pricing fine-tune set from the
517 vehicles that were assessed on the dash AND later found bought in LSL.

Each training example:
  user      = EW's REAL assessment prompt (ai_assessment_v2.build_prompt) built from
              that bid's stored point-in-time market context (no images / text-only).
  assistant = the JSON buy verdict with target_buy = the ACTUAL LSL purchase_cost
              (confidence band ~±4%, prior reasoning kept for coherence).

So the 9B learns: this market workup -> the price this kind of car actually cleared at.
Time-based split: newest ~100 bids -> test (held out), the rest -> train.
Outputs: /tmp/lora_pricing_{train,test}.jsonl + a manifest.
"""
import os, sys, json, re, time
import psycopg2, psycopg2.extras
from ai_assessment_v2 import build_prompt

TEST_N = int(os.environ.get("TEST_N", "100"))
SYS = ("You are the Experience Wholesale wholesale-vehicle pricing AI. Given the market "
       "workup below, output ONLY a JSON object: "
       '{"target_buy": <int>, "confidence_low": <int>, "confidence_high": <int>, '
       '"reasoning": "<text>", "flags": []}. Set target_buy purely from the market data.')

DSN = os.environ.get("DATABASE_URL")
if not DSN:
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")

def db(): return psycopg2.connect(DSN, connect_timeout=10)

conn = db(); conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT a.bid_id, a.actual_purchase_cost, l.gemini_raw_target, l.llm_reasoning
                 FROM ai_accuracy a JOIN ai_assessment_log l ON l.id = a.ai_assessment_log_id
                WHERE a.actual_purchase_cost > 0 AND a.ai_recommendation > 0
                ORDER BY a.bid_id DESC""")
rows = cur.fetchall(); conn.close()
print(f"source vehicles: {len(rows)}", flush=True)

def load_ctx(bid_id):
    c = db(); c.autocommit = True; cu = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def one(q):
        cu.execute(q, (bid_id,)); r = cu.fetchone(); return dict(r) if r else None
    bid = one("SELECT * FROM bids WHERE id=%s")
    vauto = one("SELECT * FROM vauto_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    accu = one("SELECT * FROM accutrade_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    ipkt = one("SELECT * FROM ipacket_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    cu.execute("SELECT market_intel,dealer_intel,buyer_intel FROM ai_assessment_log WHERE bid_id=%s ORDER BY created_at DESC LIMIT 1", (bid_id,))
    log = cu.fetchone() or {}
    cu.execute("SELECT * FROM bid_photos WHERE bid_id=%s ORDER BY id", (bid_id,))
    photos = [dict(r) for r in cu.fetchall()]
    c.close()
    return bid, vauto, accu, ipkt, dict(log), photos

examples = []
skipped = 0
t0 = time.time()
for i, row in enumerate(rows):
    bid_id = row["bid_id"]; actual = int(round(float(row["actual_purchase_cost"])))
    try:
        bid, vauto, accu, ipkt, log, photos = load_ctx(bid_id)
        if not bid:
            skipped += 1; continue
        prompt = build_prompt(bid, vauto=vauto, accutrade=accu, ipacket=ipkt, photos=photos,
                              market_intel=log.get("market_intel"),
                              dealer_intel=log.get("dealer_intel"),
                              buyer_intel=log.get("buyer_intel"))
        reasoning = (row.get("llm_reasoning") or "Anchored to the MMR / rBook / LSL market stack for this year-make-model and mileage.")
        reasoning = re.sub(r"\s+", " ", str(reasoning)).strip()[:400]
        completion = {
            "target_buy": actual,
            "confidence_low": int(round(actual * 0.96)),
            "confidence_high": int(round(actual * 1.04)),
            "reasoning": reasoning,
            "flags": [],
        }
        examples.append({"bid_id": bid_id, "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps(completion)},
        ]})
    except Exception as e:
        skipped += 1
        if skipped <= 5: print(f"skip bid {bid_id}: {str(e)[:100]}", flush=True)
    if (i + 1) % 50 == 0:
        print(f"...{i+1}/{len(rows)}  built={len(examples)}  {time.time()-t0:.0f}s", flush=True)

# Time-based split: rows are newest-first, so newest TEST_N -> test, rest -> train.
test = examples[:TEST_N]
train = examples[TEST_N:]

def write(path, items):
    with open(path, "w") as f:
        for it in items:
            f.write(json.dumps({"messages": it["messages"]}) + "\n")

write("/tmp/lora_pricing_train.jsonl", train)
write("/tmp/lora_pricing_test.jsonl", test)
# also keep bid_id mapping for the test set so we can score the LoRA later
json.dump([{"bid_id": x["bid_id"]} for x in test], open("/tmp/lora_pricing_test_bids.json", "w"))

import statistics as st
tok = [len(x["messages"][1]["content"]) // 4 for x in examples]  # ~chars/4 token estimate
print("\n==== LoRA PRICING DATASET ====", flush=True)
print(f"built {len(examples)} examples (skipped {skipped})")
print(f"  train: {len(train)}  ->  /tmp/lora_pricing_train.jsonl")
print(f"  test : {len(test)}   ->  /tmp/lora_pricing_test.jsonl (newest {TEST_N}, held out)")
if tok:
    print(f"  prompt length (approx tokens): median={st.median(tok)}  p90={sorted(tok)[int(len(tok)*0.9)]}  max={max(tok)}")
print("  format: chat JSONL {messages:[system,user,assistant]}; assistant = JSON verdict, target_buy = ACTUAL clearing price")
print("done", flush=True)
