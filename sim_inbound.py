"""EW Inbound sim — same machinery as sim_outbound.py but flipped roles.

Bill uses /opt/expwholesale/inbound_system_prompt.txt (LiveKit prompt).
Operator INITIATES with a query, Bill responds with tool calls.
Conversation is task-driven, 3-8 turns typical.

Partner backend: PARTNER_BACKEND=hermes (free GPU) or default Haiku.
"""
from __future__ import annotations
import argparse, asyncio, json, os, time, sys, importlib.util, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import aiohttp
import anthropic

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MCP_BEARER_TOKEN  = os.environ["MCP_BEARER_TOKEN"]
MCP_TOOL_URL      = "https://experience-wholesale.net/api/ew-voice/tool"
BILL_MODEL        = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=10)

BILL_PROMPT = open("/opt/expwholesale/inbound_system_prompt.txt").read()
spec = importlib.util.spec_from_file_location(
    "stream_server", "/opt/expwholesale/outbound_stream_server.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
_OUTBOUND_ONLY = {"send_partner_bid_card", "schedule_callback"}
TOOLS = [t for t in ss.TOOLS if t.get("name") not in _OUTBOUND_ONLY]

OPERATOR_ARCHETYPES = {
"quick_lookup": {
    "system": "You are Oscar, the EW owner. Short direct queries via voice. No small talk. After Bill answers, ONE follow-up then end with 'thanks' or 'got it'. 1 sentence per response.",
    "queries": [
        "Hey EW, what's bid two thousand nine going for?",
        "EW pull bid one ninety-eight five",
        "What's the MMR on a twenty twenty-one BMW seven fifty?",
        "How many deals today?",
        "What's the gross today?",
        "Who's our top salesperson this month?",
        "What came in lately?",
        "What's on the dashboard right now?",
        "Top three grosses this month?",
    ],
},
"exploratory": {
    "system": "You are Oscar exploring data. Ask Bill questions, follow up logically (who would buy that, pull history, etc). 2-3 turn dialogue. Curious not chatty. 1-2 sentences.",
    "queries": [
        "What's on the lot in BMW seven series right now?",
        "Pull bid two thousand four. Then tell me who would buy it.",
        "How is Carlos doing this month?",
        "History with Encore Motorcars?",
        "Find any Porsche on the lot. Who's the best buyer for one?",
        "Recent bids — anything Lambo or Ferrari?",
    ],
},
"technical": {
    "system": "You are Oscar asking deep technical questions. Probe damage, Carfax, vAuto sourcing, market spread, days-on-lot. Expect specific numbers. Push back if Bill is vague. 1-2 sentences.",
    "queries": [
        "Run a valuation on a twenty twenty Porsche nine eleven Carrera S, twenty-five thousand miles.",
        "What does vAuto have on a twenty twenty-three Mercedes G wagon at fifteen thousand miles?",
        "MSRP on bid two thousand nine, what's the window sticker say?",
        "Pull Carfax flags on bid two thousand four.",
        "Average days on lot for our BMW inventory?",
    ],
},
"conversational": {
    "system": "You are Oscar — greet Bill warmly first ('hey EW how we doing'), then ask a real query. You appreciate when Bill is human-sounding. 1-sentence responses.",
    "queries": [
        "Hey EW how we looking today?",
        "Morning EW — what's hot on the dashboard?",
        "Yo EW give me a quick rundown on recent bids",
        "Hey EW, anything good in the wholesale pipeline?",
    ],
},
"multistep": {
    "system": "You are Oscar with a 2-3 step task. After Bill answers query 1, ask query 2 that depends on it. Drive Bill through a chained task. 1 sentence per response.",
    "queries": [
        "Pull bid two thousand nine. Then find who would buy it. Then tell me how we should pitch it.",
        "Get a valuation on a twenty twenty-two F one fifty Raptor with thirty thousand miles. Then who should we sell it to?",
        "What's our top grosser today, and who did they sell to?",
        "Find a Porsche on the lot. What's the buyer profile? Should we run a stocking proposal?",
    ],
},
}


async def call_tool(tool_name, args):
    payload = {"bearer": MCP_BEARER_TOKEN, "tool": tool_name, "args": args}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(MCP_TOOL_URL, json=payload) as r:
            return await r.json()


def bill_turn(bill_messages, full_prompt):
    for _ in range(5):
        resp = client.messages.create(
            model=BILL_MODEL, max_tokens=250,
            system=[{"type":"text","text":full_prompt,"cache_control":{"type":"ephemeral"}}],
            tools=TOOLS, messages=bill_messages,
        )
        if resp.stop_reason == "tool_use":
            assistant_blocks = []; tool_results = []
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    assistant_blocks.append({"type":"text","text":block.text})
                elif btype == "tool_use":
                    assistant_blocks.append({"type":"tool_use","id":block.id,
                                             "name":block.name,"input":block.input})
                    try:
                        result = asyncio.run(call_tool(block.name, block.input))
                    except Exception as e:
                        result = {"error": f"{type(e).__name__}: {e}"}
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,
                                         "content":json.dumps(result, default=str)[:6000]})
            bill_messages.append({"role":"assistant","content":assistant_blocks})
            bill_messages.append({"role":"user","content":tool_results})
            continue
        text = ""
        for block in resp.content:
            if getattr(block,"type",None) == "text":
                text += block.text
        return text.strip() or "(no response)"
    return "(tool loop exceeded)"


def operator_turn(operator_messages, operator_system):
    """Haiku 4.5 operator role-play (default)."""
    msgs = []
    for m in operator_messages:
        content = m["content"] if isinstance(m["content"], str) else "[tool result]"
        msgs.append({"role": m["role"], "content": content})
    if not msgs:
        msgs = [{"role": "user", "content": "(call begins)"}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=200, temperature=0.9,
        system=[{"type":"text","text":operator_system,"cache_control":{"type":"ephemeral"}}],
        messages=msgs,
    )
    parts = [b.text for b in resp.content if hasattr(b,"text")]
    return ("".join(parts)).strip() or "(silence)"


def operator_turn_hermes(operator_messages, operator_system):
    """Free local Hermes-3 (num_ctx=8192 fits 5070 Ti)."""
    import requests
    msgs = [{"role":"system","content":operator_system}]
    for m in operator_messages:
        content = m["content"] if isinstance(m["content"],str) else "[content]"
        msgs.append({"role":m["role"],"content":content})
    r = requests.post("http://localhost:11435/api/chat", json={
        "model":"hermes3:8b","messages":msgs,"stream":False,
        "options":{"temperature":0.85,"num_predict":200,"num_ctx":8192},
    }, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"].strip() or "(silence)"


if os.environ.get("PARTNER_BACKEND","").lower() == "hermes":
    operator_turn = operator_turn_hermes


def simulate_call(archetype, max_turns=6):
    arch = OPERATOR_ARCHETYPES[archetype]
    operator_system = arch["system"]
    initial_query = random.choice(arch["queries"])

    full_prompt = BILL_PROMPT
    transcript = [{"turn":0,"speaker":"OPERATOR","text":initial_query}]
    bill_messages = [{"role":"user","content":initial_query}]

    t0 = time.monotonic()
    for turn in range(1, max_turns+1):
        bill_text = bill_turn(bill_messages, full_prompt)
        bill_messages.append({"role":"assistant","content":bill_text})
        transcript.append({"turn":turn,"speaker":"BILL","text":bill_text})
        # Operator follow-up from full transcript context
        operator_messages = []
        for t in transcript:
            role = "assistant" if t["speaker"] == "OPERATOR" else "user"
            operator_messages.append({"role": role, "content": t["text"]})
        op_text = operator_turn(operator_messages, operator_system)
        transcript.append({"turn":turn,"speaker":"OPERATOR","text":op_text})
        bill_messages.append({"role":"user","content":op_text})
        lo = op_text.lower()
        if any(s in lo for s in ["thanks","thank you","got it","perfect","later ew","talk soon"]):
            break

    elapsed = time.monotonic() - t0
    return {"archetype": archetype, "transcript": transcript,
            "n_turns": len(transcript), "elapsed_s": elapsed,
            "initial_query": initial_query}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--archetypes", default="quick_lookup,exploratory,technical,conversational,multistep")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", default="/opt/expwholesale/logs/sim_inbound.json")
    args = p.parse_args()

    archetypes = [a.strip() for a in args.archetypes.split(",") if a.strip()]
    jobs = [(arch, r+1) for arch in archetypes for r in range(args.runs)]
    total = len(jobs)
    print(f"launching {total} inbound sims with {args.workers} workers", flush=True)
    t_start = time.monotonic()

    sims = []; completed = 0
    def _run_one(arch, run_n):
        try:
            sim = simulate_call(arch)
            return arch, run_n, sim, None
        except Exception as e:
            return arch, run_n, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run_one, arch, r) for arch, r in jobs]
        for fut in as_completed(futures):
            arch, run_n, sim, err = fut.result()
            completed += 1
            if err:
                print(f"  [{completed}/{total}] {arch} #{run_n} ERR: {err[:150]}", flush=True)
                continue
            elapsed = time.monotonic() - t_start
            print(f"  [{completed}/{total}] {arch} #{run_n}: {sim['n_turns']} turns ({sim['elapsed_s']:.1f}s | wall {elapsed:.0f}s)", flush=True)
            sims.append(sim)

    json.dump(sims, open(args.out, "w"), default=str, indent=1)
    print(f"saved {len(sims)} sims to {args.out}", flush=True)


if __name__ == "__main__":
    main()
