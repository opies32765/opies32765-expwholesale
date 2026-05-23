"""ew_voice_cerebras.py — alternate voice backend using Cerebras Llama 3.3 70B
with OpenAI-compatible function calling. Mounts at /api/ew-voice-fast/* on
the voice_v2 sidecar. Reuses the same MCP tool implementations in-process
(no HTTP roundtrip) — just a different LLM up front.

Tools dispatched in-process (no HTTP) so per-call overhead is minimal.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid

from flask import Blueprint, jsonify, request, Response, stream_with_context

log = logging.getLogger("ew-voice-cerebras")

bp = Blueprint("ew_voice_cerebras", __name__)

CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")

# Per-session history
_HISTORY: dict[str, list[dict]] = {}
_HISTORY_LIMIT = 20


def _trim_history(sid: str) -> None:
    h = _HISTORY.get(sid)
    if h and len(h) > _HISTORY_LIMIT * 2:
        _HISTORY[sid] = h[-(_HISTORY_LIMIT * 2):]


# Lazy client
_client = None
def _get_client():
    global _client
    if _client is None:
        from cerebras.cloud.sdk import Cerebras
        _client = Cerebras(api_key=CEREBRAS_API_KEY)
    return _client


# ── Tool registry — bridges Cerebras OpenAI-style tool calls to our existing
#    MCP functions in ew_mcp.py. We import each tool, extract its signature
#    to build the OpenAI tool schema, and dispatch by name at call time.

def _build_tool_specs():
    """Build OpenAI tools[] schema from the @mcp.tool functions in ew_mcp.
    We hand-curate descriptions since the docstrings are voice-targeted."""
    import ew_mcp  # noqa: F401 — used dynamically via getattr in _call_tool

    # FastMCP wraps the original function. The underlying callable is on .fn
    def _unwrap(name):
        obj = getattr(ew_mcp, name, None)
        if obj is None: return None
        return getattr(obj, "fn", obj)

    specs = []

    def _add(name, description, params):
        fn = _unwrap(name)
        if fn is None:
            log.warning(f"tool {name} not exported by ew_mcp")
            return
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": [k for k, v in params.items() if v.get("__required__")],
                },
            },
        })
        # strip the synthetic __required__ flag from the OpenAI payload
        for k, v in params.items():
            v.pop("__required__", None)

    _add("get_vehicle_valuation",
         "Wholesale valuation for a year/make/model. Returns target buy, MMR, rBook, "
         "AccuTrade, LSL purchase history, partner inventory + buyer match data, "
         "mileage ladder. Use for ANY 'what is X worth' / 'what should I bid' question.",
         {
             "year":  {"type": "integer", "description": "4-digit model year", "__required__": True},
             "make":  {"type": "string",  "description": "Canonical brand", "__required__": True},
             "model": {"type": "string",  "description": "Base model", "__required__": True},
             "miles": {"type": "integer", "description": "Odometer reading"},
             "trim":  {"type": "string",  "description": "Trim level if known"},
             "msrp":  {"type": "integer", "description": "Original sticker price"},
             "notes": {"type": "string",  "description": "Condition / damage / options"},
         })
    _add("lookup_vin",
         "Look up a specific VIN to find its bid + assessment in the EW dashboard.",
         {"vin": {"type": "string", "description": "17-character VIN", "__required__": True}})
    _add("get_bid",
         "Read a specific bid number from the EW dashboard. Returns vehicle, MMR + rBook, "
         "AccuTrade, AI assessment + reasoning, partner offers, buy_profile_matches, "
         "photo count, status. Use for ANY 'bid <number>' reference.",
         {"bid_id": {"type": "integer", "description": "EW bid id", "__required__": True}})
    _add("submit_bid_to_ew",
         "Drop a new bid into the EW dashboard. Requires caller's first name.",
         {
             "vin":           {"type": "string",  "__required__": True},
             "submitted_by":  {"type": "string",  "description": "Caller first name", "__required__": True},
             "mileage":       {"type": "integer"},
             "year":          {"type": "integer"},
             "make":          {"type": "string"},
             "model":         {"type": "string"},
             "trim":          {"type": "string"},
             "asking_price":  {"type": "number"},
             "notes":         {"type": "string"},
         })
    _add("lsl_deals_booked",
         "OWNER-GATED. LSL deals booked over a period. Returns count, total profit, "
         "PVR (avg profit per car), top 3 by gross, full deals[] list with buyer + "
         "salesperson + bought_from + sold_to + profit per row.",
         {
             "caller_name": {"type": "string", "description": "Caller first name (Oscar/Gregg/Joe/Todd)", "__required__": True},
             "period":      {"type": "string", "description": "yesterday | today | last_7_days | last_30_days | this_month | last_month | this_quarter | last_quarter | ytd | this_year | last_year | all_time"},
             "caller_pin":  {"type": "string", "description": "Optional"},
         })
    _add("lsl_top_grosses",
         "OWNER-GATED. Top-N highest-gross deals in a period.",
         {
             "caller_name": {"type": "string", "__required__": True},
             "period":      {"type": "string"},
             "limit":       {"type": "integer"},
             "caller_pin":  {"type": "string"},
         })
    _add("lsl_lookup_sale",
         "OWNER-GATED. Look up a specific deal by stock# or VIN. Returns customer, "
         "salesperson, supplier, sale price, purchase cost, profit, sold date.",
         {
             "caller_name":  {"type": "string", "__required__": True},
             "stock_or_vin": {"type": "string", "__required__": True},
             "caller_pin":   {"type": "string"},
         })
    _add("find_best_buyer",
         "OWNER-GATED. For a year/make/model, return ranked partner-dealer buyer matches: "
         "buy_profile_matches (onboarded dealers Nuccio + TXT Charlie with T1/T2 scores), "
         "top_pitch_buyers (LSL ledger ranked buyers), recent_buyers, rolling windows.",
         {
             "caller_name": {"type": "string", "__required__": True},
             "year":        {"type": "integer", "__required__": True},
             "make":        {"type": "string",  "__required__": True},
             "model":       {"type": "string",  "__required__": True},
             "trim":        {"type": "string"},
             "mileage":     {"type": "integer"},
             "caller_pin":  {"type": "string"},
         })
    _add("dashboard_stats",
         "EW dashboard health: today's bids by status, total open, pending AI assessments, "
         "unseen partner offers, most recent offers to review.",
         {})
    _add("recent_bids",
         "Most recent N bids in the EW dashboard.",
         {"limit": {"type": "integer", "description": "default 5"}})
    return specs


_TOOL_SPECS = None
def get_tool_specs():
    global _TOOL_SPECS
    if _TOOL_SPECS is None:
        _TOOL_SPECS = _build_tool_specs()
    return _TOOL_SPECS


async def _call_tool(name: str, args: dict):
    """Dispatch a tool call to the underlying ew_mcp function in-process."""
    import ew_mcp
    obj = getattr(ew_mcp, name, None)
    if obj is None:
        return {"error": f"unknown tool {name}"}
    fn = getattr(obj, "fn", obj)
    try:
        if inspect.iscoroutinefunction(fn):
            return await fn(**(args or {}))
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad args for {name}: {e}"}
    except Exception as e:
        log.exception(f"tool {name} failed")
        return {"error": f"{type(e).__name__}: {e}"}


def _system_prompt():
    """Cerebras path: prepend an aggressive tool-use directive because
    open-source LLMs (gpt-oss, qwen) sometimes skip tool calls and
    hallucinate numbers from training. Force tool use on every data query.
    """
    PREFIX = (
        "CRITICAL: For ANY question involving specific numbers, deals, bids, "
        "vehicles, prices, dealers, customers, salespeople, profit, gross, PVR, "
        "or activity — you MUST call a tool to fetch the data. NEVER make up "
        "numbers from training. NEVER skip the tool call. If you do not know "
        "which tool fits, default to lsl_deals_booked for deal/profit questions "
        "or recent_bids/get_bid for dashboard questions or get_vehicle_valuation "
        "for pricing.\n\n"
    )
    try:
        from ew_voice_api import SYSTEM_PROMPT
        return PREFIX + SYSTEM_PROMPT
    except Exception:
        return PREFIX + "You are EW."


@bp.route("/api/ew-voice-fast/warmup", methods=["GET", "POST"])
def warmup():
    """Fire a 1-token Cerebras call to warm the connection."""
    if not CEREBRAS_API_KEY:
        return jsonify({"error": "CEREBRAS_API_KEY missing"}), 500
    try:
        client = _get_client()
        t0 = time.monotonic()
        # Use the REAL system prompt so the connection + model state is hot
        # for the next real query (Cerebras keeps recent context warm).
        client.chat.completions.create(
            model=CEREBRAS_MODEL,
            max_tokens=4,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user",   "content": "ok"},
            ],
        )
        return jsonify({"ok": True, "warmed": ["cerebras"],
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@bp.route("/api/ew-voice-fast/stream", methods=["POST"])
def ew_voice_fast_stream():
    """SSE-streamed variant. Same event shape as /api/ew-voice/stream so the
    client UI is interchangeable. Events: tool_call, first_text_ms, sentence,
    final, error."""
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "empty transcript"}), 400
    session_id = (data.get("session_id") or "").strip() or f"ewf-{uuid.uuid4().hex[:12]}"

    if not CEREBRAS_API_KEY:
        return jsonify({"error": "CEREBRAS_API_KEY missing — paste key into env"}), 500

    history = _HISTORY.setdefault(session_id, [])
    history.append({"role": "user", "content": transcript})

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    @stream_with_context
    def generate():
        import re as _re
        t0 = time.monotonic()
        first_text_t = None
        full_text = ""
        sent_buf = ""
        tool_calls_log = []
        SENT_BOUNDARY = _re.compile(r"([.!?])(?=\s|$|[\"\')\]])")

        def _split_sentences(buf):
            out = []
            i = 0
            last = 0
            while i < len(buf):
                m = SENT_BOUNDARY.search(buf, i)
                if not m: break
                end = m.end()
                while end < len(buf) and buf[end] in " \t\n":
                    end += 1
                out.append(buf[last:end].strip())
                last = end
                i = end
            return out, buf[last:]

        client = _get_client()
        messages = [{"role": "system", "content": _system_prompt()}] + history
        tools = get_tool_specs()

        # Cerebras requires multi-step tool-call loop manually
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            for _step in range(3):  # up to 2 tool rounds + 1 final
                if time.monotonic() - t0 > 20.0:
                    yield _sse({"type": "error", "message": "timeout — try again"})
                    break
                # Force a final spoken reply on the last iteration so the model
                # cant just go silent after a tool call (known gpt-oss-120b quirk).
                _is_last = (_step == 2)
                resp = client.chat.completions.create(
                    model=CEREBRAS_MODEL,
                    max_tokens=350,
                    messages=messages,
                    tools=(None if _is_last else tools),
                    tool_choice=("none" if _is_last else "auto"),
                    stream=False,
                    timeout=12.0,
                )
                msg = resp.choices[0].message
                if msg.tool_calls:
                    # Append the assistant message + each tool result, then loop
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name,
                                          "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ],
                    })
                    for tc in msg.tool_calls:
                        tname = tc.function.name
                        try:
                            targs = json.loads(tc.function.arguments or "{}")
                        except Exception:
                            targs = {}
                        tool_calls_log.append({"name": tname, "input": targs})
                        yield _sse({"type": "tool_call", "name": tname})
                        result = loop.run_until_complete(_call_tool(tname, targs))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tname,
                            "content": json.dumps(result, default=str)[:8000],
                        })
                    continue  # next round — let the model see tool results
                # No tool calls → this is the final reply
                final_text = msg.content or ""
                # JSON_GUARD_2026_05_22 — sometimes gpt-oss outputs raw JSON
                # like '{"tool":"..."}{"error":"..."}' instead of speech.
                # Detect + re-prompt for plain English.
                stripped = final_text.lstrip()
                if (stripped.startswith("{\"tool\"") or stripped.startswith("{\"error\"")
                    or stripped.startswith("{\"name\"") or stripped.count("{\"") >= 2):
                    if not _is_last:
                        messages.append({"role": "user",
                                          "content": "Your last reply contained JSON. Respond with PLAIN SPOKEN ENGLISH only — no curly braces, no tool names, no JSON. Summarize the data from the previous tool result."})
                        continue
                    final_text = ""  # force fallback path below
                if not final_text and not _is_last:
                    # Model went silent after a tool call — force a summary
                    messages.append({"role": "user", "content": "Now give the operator the spoken summary based on the tool data above."})
                    continue
                if final_text:
                    if first_text_t is None:
                        first_text_t = time.monotonic()
                        yield _sse({"type": "first_text_ms",
                                    "ms": int((first_text_t - t0) * 1000)})
                    full_text += final_text
                    sent_buf += final_text
                    sents, sent_buf = _split_sentences(sent_buf)
                    for s_text in sents:
                        if s_text:
                            yield _sse({"type": "sentence", "text": s_text})
                if sent_buf.strip():
                    yield _sse({"type": "sentence", "text": sent_buf.strip()})
                break
            else:
                yield _sse({"type": "error", "message": "tool loop exceeded 3 rounds"})

            # FALLBACK_2026_05_22 — if Cerebras went silent across all
            # iterations but did call tools, emit a brief recovery sentence
            # so the operator hears something instead of silence.
            if not full_text.strip() and tool_calls_log:
                fallback = ("I pulled the data but had trouble summarizing. "
                            "Could you rephrase the question?")
                yield _sse({"type": "sentence", "text": fallback})
                full_text = fallback

            history.append({"role": "assistant", "content": full_text})
            _trim_history(session_id)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.info(f"cerebras session={session_id} transcript={transcript!r} "
                     f"tools={[t['name'] for t in tool_calls_log]} "
                     f"reply={full_text[:80]!r} elapsed_ms={elapsed_ms}")
            yield _sse({
                "type": "final",
                "reply_text": full_text.strip(),
                "tool_calls": tool_calls_log,
                "elapsed_ms": elapsed_ms,
                "model": CEREBRAS_MODEL,
                "session_id": session_id,
            })
        except Exception as e:
            log.exception("cerebras stream error")
            yield _sse({"type": "error", "message": str(e)[:300]})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@bp.route("/api/ew-voice-fast/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    sid = (data.get("session_id") or "").strip()
    if sid and sid in _HISTORY:
        del _HISTORY[sid]
    return jsonify({"ok": True})
