"""EW Outbound — conversation simulator + coach.

Runs simulated calls between Bill (the EW outbound bot using the SAME
system prompt + tools as live calls) and a partner-archetype agent.
No Twilio, no TTS, no STT — pure text turn-taking. Fast: ~30s per call.

Then Coach (Opus 4.7) reviews all transcripts and outputs proposed
edits to /opt/expwholesale/outbound_system_prompt.txt.

Usage:
  /opt/ew_outbound/venv/bin/python3 /opt/ew_outbound/simulate.py \\
      --bid 2009 --archetypes busy,skeptical,chatty,negotiator,eager \\
      --runs 5 --coach"""
from __future__ import annotations
import argparse, asyncio, json, os, time, sys, importlib.util
from pathlib import Path

import aiohttp
import anthropic
import psycopg2, psycopg2.extras

# ─── env ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MCP_BEARER_TOKEN  = os.environ["MCP_BEARER_TOKEN"]
MCP_TOOL_URL      = "https://experience-wholesale.net/api/ew-voice/tool"
BILL_MODEL        = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")
PARTNER_MODEL     = "claude-sonnet-4-6"
COACH_MODEL       = "claude-opus-4-7"
DB_URL            = "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=10)

# ─── load Bill's exact runtime config ──────────────────────────────────
BILL_PROMPT = open("/opt/expwholesale/outbound_system_prompt.txt").read()
spec = importlib.util.spec_from_file_location(
    "stream_server", "/opt/expwholesale/outbound_stream_server.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
TOOLS = ss.TOOLS  # reuse the EXACT tool schema Bill sees on live calls

# ─── partner archetypes ────────────────────────────────────────────────
ARCHETYPES = {
"busy": """You are Tom, a 50yo dealer manager at a Florida used-car lot. You're in the middle of three things when the phone rings. You answer reluctantly, you're impatient, you cut to the chase. You only stay on the line if the deal is genuinely interesting in the first 30 seconds. You say things like 'quick — what is it', 'I'm busy, just numbers', 'gotta go, send it to me'. Keep responses SHORT (1-2 sentences max). Hang up after 4-6 turns if you don't see value.""",

"skeptical": """You are Mike, a 60yo dealer who has been burned before. You question every claim. You ask 'how do you know it's clean?', 'who's your source?', 'why so much above MMR?'. You don't trust unsolicited calls. You're not rude but you make Bill defend everything. You'll engage if he handles your questions well. Keep responses focused on poking holes.""",

"chatty": """You are Dave, a friendly outgoing dealer who likes small talk. You ask 'how's the weather over in Pompano', 'you been with EW long?', 'how's the market treating you?'. Bill needs to engage with you a little before pitching. Once he does, you're receptive. Match the human-warmth energy.""",

"negotiator": """You are Sarah, a hard-nosed dealer who NEVER takes the first price. You always counter 10-15% below ask. You say things like 'I can do twenty-eight K', 'that's high, what's your bottom line', 'I want it but you gotta come down'. You'll close if Bill holds firm but offers something (faster delivery, future credit, etc).""",

"eager": """You are Alex, a young aggressive dealer trying to fill your lot with luxury inventory. You're ready to pull the trigger fast IF the specs check out. You ask sharp questions: miles, Carfax, options, ETA, deposit terms. If Bill answers cleanly, you'll commit verbally on the call. Keep momentum high.""",
}


# ─── tool dispatch — exact same as live call ───────────────────────────
async def call_tool(tool_name: str, args: dict) -> dict:
    payload = {"bearer": MCP_BEARER_TOKEN, "tool": tool_name, "args": args}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(MCP_TOOL_URL, json=payload) as r:
            return await r.json()


# ─── Bill's turn (uses the SAME prompt + tools as live) ────────────────
def bill_turn(bill_messages, full_prompt):
    """Single LLM turn for Bill, including tool loop."""
    for _ in range(5):
        resp = client.messages.create(
            model=BILL_MODEL, max_tokens=250,
            system=[{"type":"text","text":full_prompt,"cache_control":{"type":"ephemeral"}}],
            tools=TOOLS, messages=bill_messages,
        )
        if resp.stop_reason == "tool_use":
            assistant_blocks = []
            tool_results = []
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    assistant_blocks.append({"type":"text","text":block.text})
                elif btype == "tool_use":
                    assistant_blocks.append({
                        "type":"tool_use","id":block.id,
                        "name":block.name,"input":block.input,
                    })
                    # Skip side-effecty tools in sim: send_partner_bid_card and schedule_callback
                    if block.name in ("send_partner_bid_card", "schedule_callback"):
                        result = {"ok": True, "_simulated": True,
                                  "note": "tool would have fired in production"}
                    else:
                        try:
                            result = asyncio.run(call_tool(block.name, block.input))
                        except Exception as e:
                            result = {"error": f"{type(e).__name__}: {e}"}
                    tool_results.append({
                        "type":"tool_result","tool_use_id":block.id,
                        "content":json.dumps(result, default=str)[:6000],
                    })
            bill_messages.append({"role":"assistant","content":assistant_blocks})
            bill_messages.append({"role":"user","content":tool_results})
            continue
        text = ""
        for block in resp.content:
            if getattr(block,"type",None) == "text":
                text += block.text
        return text.strip() or "(no response)"
    return "(tool loop exceeded)"


# ─── partner's turn ────────────────────────────────────────────────────
def partner_turn(partner_messages, partner_system):
    """Anthropic Haiku 4.5 partner — ~$0.001/turn, fast + reliable."""
    msgs = []
    for m in partner_messages:
        content = m["content"] if isinstance(m["content"], str) else "[tool result]"
        msgs.append({"role": m["role"], "content": content})
    if not msgs:
        msgs = [{"role": "user", "content": "(call begins)"}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        temperature=0.9,
        system=[{"type":"text","text":partner_system,"cache_control":{"type":"ephemeral"}}],
        messages=msgs,
    )
    parts = [b.text for b in resp.content if hasattr(b, "text")]
    return ("".join(parts)).strip() or "(silence)"



# Alternative partner using local Hermes-3 with proper context size (FREE, 152 tok/s)
def partner_turn_hermes(partner_messages, partner_system):
    import requests
    msgs = [{"role":"system","content":partner_system}]
    for m in partner_messages:
        content = m["content"] if isinstance(m["content"],str) else "[content]"
        msgs.append({"role":m["role"],"content":content})
    r = requests.post("http://localhost:11435/api/chat", json={
        "model":"hermes3:8b","messages":msgs,"stream":False,
        "options":{"temperature":0.85,"num_predict":250,"num_ctx":8192},
    }, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"].strip() or "(silence)"

# Switch the active partner via PARTNER_BACKEND env var
import os as _os
if _os.environ.get("PARTNER_BACKEND","").lower() == "hermes":
    partner_turn = partner_turn_hermes

# ─── build the call briefing same way live does ────────────────────────
def build_briefing(bid_id, partner_name):
    bid = asyncio.run(call_tool("get_bid", {"bid_id": int(bid_id)}))
    yr = bid.get("year") or ""
    mk = bid.get("make") or ""
    md = bid.get("model") or ""
    tr = bid.get("trim") or ""
    mi = bid.get("mileage") or 0
    ip = bid.get("ipacket") or {}
    ext = ip.get("exterior_color") or bid.get("color") or ""
    interior = ip.get("interior_color") or ""
    msrp = ip.get("total_msrp")
    sticker = ip.get("sticker_text") or ""
    mmr = bid.get("vauto_mmr")
    rbook = bid.get("vauto_rbook")
    target = bid.get("ai_price") or bid.get("asking_price") or rbook
    damage = bid.get("damage_audit") or {}
    flags = damage.get("flags") or []
    options = []
    for line in sticker.split("\n"):
        line = line.strip()
        if not line or len(line) < 5: continue
        if line.lstrip().upper().startswith("STD"): continue
        parts = line.split("-", 1)
        if len(parts) == 2 and len(parts[0].strip()) <= 6:
            line = parts[1].strip()
        options.append(line)
        if len(options) >= 6: break
    positioning = []
    if target and rbook:
        d = float(rbook) - float(target)
        if d > 500: positioning.append(f"BELOW rBook by ${int(d):,}")
        elif d < -500: positioning.append(f"ABOVE rBook by ${int(abs(d)):,}")
    if target and mmr:
        d = float(target) - float(mmr)
        if d > 500: positioning.append(f"${int(d):,} above MMR — typical spread")
    briefing = {
        "vehicle": f"{yr} {mk} {md} {tr}".strip(),
        "miles": int(mi) if mi else None,
        "exterior": ext, "interior": interior,
        "msrp_new": int(msrp) if msrp else None,
        "options": options, "damage": "clean" if not flags else flags,
        "mmr": int(mmr) if mmr else None,
        "rbook": int(rbook) if rbook else None,
        "our_target": int(target) if target else None,
        "positioning": positioning,
    }
    ctx = (f"═══ THIS CALL ═══\nbid_id: {bid_id}\npartner_name: {partner_name}\n"
           f"partner_phone: +14074309675\nmatch_score: 88\n")
    full = ctx + BILL_PROMPT + (
        f"\n\n═══ CALL BRIEFING ═══\n" + json.dumps(briefing, default=str, indent=2)
    )
    return full, briefing


# ─── one full simulated call ───────────────────────────────────────────
def simulate_call(bid_id, archetype, partner_name="Tom", max_turns=10):
    full_prompt, briefing = build_briefing(bid_id, partner_name)
    partner_system = ARCHETYPES[archetype]

    # Bill opens with the canonical intro
    bill_first = f"Hi {partner_name}, this is Bill from Experience Wholesale. Do you have a moment?"
    bill_messages = [{"role":"assistant","content":bill_first}]
    partner_messages = []
    transcript = [{"turn":0,"speaker":"BILL","text":bill_first}]

    t0 = time.monotonic()
    for turn in range(1, max_turns+1):
        # Partner responds
        _last_bill = bill_messages[-1]["content"] if isinstance(bill_messages[-1]["content"],str) else "[Bill response]"
        if not _last_bill.strip(): _last_bill = "(no response)"
        partner_messages.append({"role":"user","content":_last_bill})
        partner_text = partner_turn(partner_messages, partner_system)
        partner_messages.append({"role":"assistant","content":partner_text})
        transcript.append({"turn":turn,"speaker":"PARTNER","text":partner_text})
        # End signals
        lo = partner_text.lower()
        if any(s in lo for s in ["*hangs up*","goodbye now","gotta go now","bye now"]):
            break
        # Bill responds (with tool loop)
        bill_messages.append({"role":"user","content":(partner_text.strip() if partner_text else "") or "(no response)"})
        bill_text = bill_turn(bill_messages, full_prompt)
        bill_messages.append({"role":"assistant","content":bill_text})
        transcript.append({"turn":turn,"speaker":"BILL","text":bill_text})
        lo = bill_text.lower()
        if any(s in lo for s in ["take care","talk soon","catch you later","have a good one"]):
            break

    elapsed = time.monotonic() - t0
    return {"archetype": archetype, "partner_name": partner_name,
            "bid_id": bid_id, "briefing": briefing,
            "transcript": transcript, "n_turns": len(transcript),
            "elapsed_s": elapsed}


# ─── persist sim run to outbound_call_log w/ tag ───────────────────────
def save_sim(sim):
    try:
        with psycopg2.connect(DB_URL, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO outbound_call_log
                  (call_sid, partner_name, partner_phone, bid_id, n_user_turns,
                   n_bot_turns, transcript)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                f"SIM-{sim['archetype']}-{int(time.time())}",
                sim["partner_name"], "SIMULATED",
                int(sim["bid_id"]),
                sum(1 for t in sim["transcript"] if t["speaker"]=="PARTNER"),
                sum(1 for t in sim["transcript"] if t["speaker"]=="BILL"),
                json.dumps(sim["transcript"], default=str),
            ))
            row_id = cur.fetchone()[0]
            c.commit()
            return row_id
    except Exception as e:
        print(f"  save_sim err: {e}", flush=True)
        return None


# ─── coach: review N transcripts, propose prompt edits ─────────────────
COACH_SYSTEM = """You are reviewing transcripts of an AI sales bot named Bill making outbound wholesale-car pitches to dealers. Bill's job: pitch a specific bid, handle objections, and either close via SMS or schedule a callback.

Score EACH transcript on a 1-10 scale on these dimensions:
1. NATURALNESS: Did Bill sound human? Varied openers, fillers, fragments, energy-matching, NO stage directions or stock phrases.
2. ACCURACY: Were the specs/numbers correct per the briefing? Did Bill use the right tools?
3. PERSUASION: Did Bill move the partner toward yes? Used positioning vs market correctly?
4. RECOVERY: Did Bill handle objections, misunderstandings, redirects? Avoided loops?
5. CLOSING: Did the call end cleanly with a clear outcome (SMS sent, callback scheduled, pass)?

Then identify PATTERNS across all transcripts:
- Phrases Bill repeats verbatim across calls (robotic signal)
- Tool calls Bill SHOULD have made but didn't
- Tone mismatches with partner archetype
- Stall/silence points
- Specific lines that felt fake

Finally, output PROPOSED EDITS to system_prompt.txt as a JSON list:
[
  {
    "section": "name of prompt section to edit",
    "issue": "concrete observed problem with quoted example from a transcript",
    "edit_type": "ADD | REPLACE | DELETE",
    "old_text": "if replacing, the exact old text (or empty for ADD)",
    "new_text": "the new text",
    "confidence": 0.0-1.0,
    "expected_improvement": "what dimension this helps"
  },
  ...
]

Be ruthless. Only propose edits backed by EVIDENCE in the transcripts. Confidence 0.85+ means auto-apply-worthy."""


def coach_review(transcripts):
    body = f"# {len(transcripts)} SIMULATED CALL TRANSCRIPTS\n\n"
    for i, sim in enumerate(transcripts, 1):
        body += f"\n\n## CALL {i} — archetype: {sim['archetype']}, partner: {sim['partner_name']}\n"
        body += f"vehicle: {sim['briefing']['vehicle']} @ ${sim['briefing']['our_target']}\n"
        body += f"turns: {sim['n_turns']}\n\n"
        for t in sim["transcript"]:
            body += f"[T{t['turn']:02d} {t['speaker']:7s}] {t['text']}\n"
    body += "\n\nNow score each and propose prompt edits."
    print(f"\n[coach] sending {len(body):,} chars to Opus...", flush=True)
    resp = client.messages.create(
        model=COACH_MODEL, max_tokens=8000,
        system=COACH_SYSTEM,
        messages=[{"role":"user","content":body}],
    )
    text = ""
    for block in resp.content:
        if getattr(block,"type",None) == "text":
            text += block.text
    return text


# ─── orchestrator ──────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bid", type=int, required=True)
    p.add_argument("--archetypes", default="busy,skeptical,chatty,negotiator,eager")
    p.add_argument("--runs", type=int, default=5, help="runs per archetype")
    p.add_argument("--workers", type=int, default=6, help="parallel sims")
    p.add_argument("--coach", action="store_true", help="run Coach after sims")
    p.add_argument("--out", default="/opt/expwholesale/logs/sim_results.json")
    args = p.parse_args()

    archetypes = [a.strip() for a in args.archetypes.split(",") if a.strip()]
    partner_names = {"busy":"Tom","skeptical":"Mike","chatty":"Dave",
                     "negotiator":"Sarah","eager":"Alex"}

    # Build all jobs upfront
    jobs = []
    for arch in archetypes:
        for r in range(args.runs):
            jobs.append((arch, r+1))
    total = len(jobs)
    print(f"\nlaunching {total} sims with {args.workers} parallel workers", flush=True)
    t_start = time.monotonic()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    sims = []
    completed = 0

    def _run_one(arch, run_n):
        try:
            sim = simulate_call(args.bid, arch, partner_names.get(arch, "Partner"))
            return arch, run_n, sim, None
        except Exception as e:
            import traceback
            return arch, run_n, None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[:500]}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run_one, arch, r) for arch, r in jobs]
        for fut in as_completed(futures):
            arch, run_n, sim, err = fut.result()
            completed += 1
            if err:
                print(f"  [{completed}/{total}] {arch} #{run_n} ERR: {err[:200]}", flush=True)
                continue
            elapsed = time.monotonic() - t_start
            print(f"  [{completed}/{total}] {arch} #{run_n}: {sim['n_turns']} turns "
                  f"({sim['elapsed_s']:.1f}s | wall {elapsed:.0f}s)", flush=True)
            save_sim(sim)
            sims.append(sim)
    print(f"\nall sims done in {time.monotonic()-t_start:.0f}s wall clock", flush=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(sims, f, default=str, indent=2)
    print(f"\nsims saved to {args.out}", flush=True)

    if args.coach:
        review = coach_review(sims)
        coach_path = args.out.replace(".json","_coach.md")
        Path(coach_path).write_text(review)
        print(f"coach review saved to {coach_path}", flush=True)
        print("\n" + "="*70 + "\n" + review[:3000])


if __name__ == "__main__":
    main()
