"""Hybrid filter for fine-tuning corpus.

Step 1: Heuristic pre-filter (deterministic)
Step 2: Hermes-3-8B per-transcript scoring (0-10)
Step 3: Output summary + score distribution + top/bottom samples
"""
import json, os, time, sys
from pathlib import Path

INPUT_FILES = [
    "/opt/expwholesale/logs/sim_results_v5.json",
    "/opt/expwholesale/logs/sim_results_v6.json",
]
OUTPUT_FILTERED = "/opt/expwholesale/logs/finetune_corpus.json"
OUTPUT_SCORES   = "/opt/expwholesale/logs/finetune_scores.json"
OUTPUT_REPORT   = "/opt/expwholesale/logs/finetune_filter_report.txt"

JUNK_MARKERS = ["(no response)", "(tool loop exceeded)", "(silence)", "(content)"]


def load_corpus():
    sims = []
    for fp in INPUT_FILES:
        if not Path(fp).exists():
            print(f"  skipping missing: {fp}", flush=True)
            continue
        d = json.load(open(fp))
        for s in d:
            s["_src"] = Path(fp).stem
            sims.append(s)
    return sims


def heuristic_filter(sims):
    keep, drop = [], []
    for s in sims:
        reasons = []
        n = s.get("n_turns", 0)
        if n < 4: reasons.append(f"too_short({n})")
        if n > 25: reasons.append(f"too_long({n})")
        transcript = s.get("transcript", [])
        bill_texts = [t.get("text","") for t in transcript if t.get("speaker") == "BILL"]
        avg_bill = sum(len(b) for b in bill_texts) / max(1, len(bill_texts))
        if avg_bill < 50: reasons.append(f"bill_too_short(avg={avg_bill:.0f})")
        full_text = " ".join(t.get("text","") for t in transcript)
        for jm in JUNK_MARKERS:
            if jm in full_text:
                reasons.append(f"has_{jm}")
                break
        # Bill should mention vehicle within first 3 of his turns
        first_bill_block = " ".join(bill_texts[:3]).lower()
        ymm_hints = ["bmw","bimmer","seven-fifty","seven fifty","2021","twenty-twenty-one"]
        if not any(h in first_bill_block for h in ymm_hints):
            reasons.append("no_ymm_in_opener")
        if reasons:
            s["_drop_reasons"] = reasons
            drop.append(s)
        else:
            keep.append(s)
    return keep, drop


def hermes_score(transcript_text, timeout=60):
    """Score a transcript via Hermes via tunnel. Returns (score:int, reason:str)."""
    import requests
    sys_prompt = (
        "You evaluate sales-call transcripts for fine-tuning quality. "
        "Score 0-10. Return EXACTLY one line: 'SCORE: <0-10> | REASON: <one sentence>'. "
        "Score high (8-10) for: clean openers, smart objection handling, firm pricing, professional close. "
        "Score mid (5-7) for: generic but functional. "
        "Score low (0-4) for: tool errors, looping, sounding robotic, missing key disclosures, broken negotiation."
    )
    user = f"Transcript:\n{transcript_text[:3000]}\n\nScore now:"
    try:
        r = requests.post("http://localhost:11435/api/chat", timeout=timeout, json={
            "model":"hermes3:8b",
            "messages":[{"role":"system","content":sys_prompt},
                        {"role":"user","content":user}],
            "stream":False,
            "options":{"temperature":0.2,"num_predict":120,"num_ctx":8192},
        })
        r.raise_for_status()
        out = r.json()["message"]["content"].strip()
        # Parse "SCORE: X | REASON: Y"
        import re
        m = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)\s*\|\s*REASON:\s*(.+)", out, re.IGNORECASE)
        if m:
            return float(m.group(1)), m.group(2).strip()[:200]
        # Fallback: try to find any 0-10 number
        m = re.search(r"\b([0-9](?:\.\d+)?|10)\b", out)
        if m:
            return float(m.group(1)), out[:200]
        return None, f"unparseable: {out[:80]}"
    except Exception as e:
        return None, f"err: {type(e).__name__}: {e}"


def transcript_to_text(sim):
    lines = []
    for t in sim.get("transcript", []):
        speaker = t.get("speaker", "?")
        text = t.get("text", "")
        if isinstance(text, list):
            text = " ".join(str(p) for p in text)
        lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def main():
    t0 = time.monotonic()
    print(f"=== Hybrid Filter Pipeline ===", flush=True)
    print(f"Loading corpus from {len(INPUT_FILES)} files...", flush=True)
    sims = load_corpus()
    print(f"  loaded {len(sims)} total raw sims", flush=True)

    print(f"\nStep 1: Heuristic filter", flush=True)
    keep, drop = heuristic_filter(sims)
    print(f"  KEPT:    {len(keep)}", flush=True)
    print(f"  DROPPED: {len(drop)}", flush=True)
    # Drop reason histogram
    from collections import Counter
    drop_reasons = Counter()
    for s in drop:
        for r in s.get("_drop_reasons", []):
            drop_reasons[r.split("(")[0]] += 1
    print(f"  drop reasons: {dict(drop_reasons.most_common(10), flush=True)}")

    print(f"\nStep 2: Hermes scoring ({len(keep)} sims, flush=True)")
    scored = []
    failed = 0
    for i, s in enumerate(keep, 1):
        ttext = transcript_to_text(s)
        score, reason = hermes_score(ttext)
        if score is None:
            failed += 1
            if failed <= 5:
                print(f"  [{i}/{len(keep)}] FAIL: {reason[:80]}", flush=True)
        else:
            s["_score"] = score
            s["_score_reason"] = reason
            scored.append(s)
        if True:
            elapsed = time.monotonic() - t0
            rate = i / max(1, elapsed - 0)  # sims/sec
            eta = (len(keep) - i) / max(0.01, rate)
            print(f"  [{i}/{len(keep)}] scored, elapsed={elapsed:.0f}s ETA={eta:.0f}s failed={failed}", flush=True)

    # Distribution
    if scored:
        from statistics import median, mean
        scores_only = [s["_score"] for s in scored]
        buckets = Counter()
        for sc in scores_only:
            buckets[int(sc)] += 1
        print(f"\nStep 3: Score distribution (N={len(scored)}, flush=True)")
        print(f"  mean={mean(scores_only):.2f}  median={median(scores_only):.2f}  "
              f"min={min(scores_only)}  max={max(scores_only)}", flush=True)
        for b in sorted(buckets):
            bar = "#" * (buckets[b] // 5 + 1)
            print(f"  {b}: {buckets[b]:4d}  {bar}", flush=True)

        # Cutoffs
        at_7 = [s for s in scored if s["_score"] >= 7]
        at_8 = [s for s in scored if s["_score"] >= 8]
        at_9 = [s for s in scored if s["_score"] >= 9]
        print(f"\nCutoffs (for fine-tune corpus):", flush=True)
        print(f"  >=7/10: {len(at_7)}  (recommended for SFT)", flush=True)
        print(f"  >=8/10: {len(at_8)}  (tighter quality, may underfit on diversity)", flush=True)
        print(f"  >=9/10: {len(at_9)}  (very tight, probably too few)", flush=True)

        # Save outputs
        json.dump([{k:v for k,v in s.items() if not k.startswith("_") or k in ("_score","_score_reason","_src")} for s in at_7],
                  open(OUTPUT_FILTERED, "w"), default=str, indent=1)
        json.dump([{"src":s["_src"],"archetype":s.get("archetype"),"n_turns":s.get("n_turns"),
                    "score":s["_score"],"reason":s["_score_reason"]} for s in scored],
                  open(OUTPUT_SCORES, "w"), default=str, indent=1)
        print(f"\nSaved {len(at_7)} transcripts to {OUTPUT_FILTERED}", flush=True)
        print(f"Saved {len(scored)} scores to {OUTPUT_SCORES}", flush=True)

    total = time.monotonic() - t0
    print(f"\nTotal wall: {total:.0f}s", flush=True)

    # Write report
    with open(OUTPUT_REPORT, "w") as f:
        f.write(f"Hybrid Filter Report\n====================\n\n")
        f.write(f"Raw sims loaded: {len(sims)}\n")
        f.write(f"After heuristic filter: {len(keep)} ({100*len(keep)/max(1,len(sims)):.0f}%)\n")
        f.write(f"After Hermes scoring (success): {len(scored)} ({100*len(scored)/max(1,len(keep)):.0f}%)\n")
        if scored:
            f.write(f"Hermes scoring failed on: {failed}\n\n")
            f.write(f"Score distribution:\n")
            for b in sorted(buckets):
                f.write(f"  {b}: {buckets[b]}\n")
            f.write(f"\n>=7/10: {len(at_7)} (recommended for SFT)\n")
            f.write(f">=8/10: {len(at_8)}\n")
            f.write(f">=9/10: {len(at_9)}\n")
            f.write(f"\nDrop reasons:\n")
            for r, c in drop_reasons.most_common():
                f.write(f"  {r}: {c}\n")
            f.write(f"\nWall time: {total:.0f}s\n")


if __name__ == "__main__":
    main()
