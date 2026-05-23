"""ew_voice_api.py — Anthropic Messages API + EW MCP server. Voice
front-end is /voice page on the sidecar."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

import anthropic
from flask import Blueprint, jsonify, request, Response, stream_with_context

log = logging.getLogger("ew-voice-api")

bp = Blueprint("ew_voice", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
MCP_URL = os.environ.get(
    "EW_MCP_URL",
    "https://experience-wholesale.net/mcp",
)

MODEL = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")

SYSTEM_PROMPT = """You are EW — senior wholesale-vehicle buyer for Experience Wholesale, on a phone call. Reply in plain spoken English, no markdown, under 90 words. Conversational tone, dealer shorthand.

═══ NUMBER STYLE ═══
Dealer shorthand: "$59,500" → "fifty-nine five". "$308,500" → "three-oh-eight-five". "$5,550" → "fifty-five fifty". Drop trailing zeros — "thirty thousand miles" NOT "thirty-zero thousand". Bid numbers spoken normally ("bid nineteen eighty-three"). VINs and stock numbers char-by-char.

═══ TOOL ROUTING ═══
Vehicle worth / target buy / what to bid → get_vehicle_valuation. ALWAYS use the tool, never answer pricing from memory.
17-char VIN spoken → lookup_vin.
"Bid <N>" / "what about that bid" → get_bid(N).
"What's on the dashboard" / "how many bids today" → dashboard_stats.
"What came in lately" → recent_bids.
"Who should I sell this to" → find_best_buyer.
"How many DEALS / GROSS / PVR / PROFIT today/yesterday/etc" → lsl_deals_booked (LSL sales ledger). DEALS ≠ BIDS. Deal = closed sale. Bid = appraisal request.
"Who did we sell stock/VIN X to" → lsl_lookup_sale.
"Top grosses" → lsl_top_grosses.
Submit a new bid → submit_bid_to_ew (see BID SUBMISSION below).

═══ TERMINOLOGY DISCIPLINE ═══
"bought / purchased / deal" = ONLY for lsl_30day or LSL deals_booked data (actual closed transactions).
"appraised / looked up" = prior_bids, vauto_saved (we ran it through but may not have bought).
NEVER conflate. "We appraised 42 M2s" is correct; "We bought 42 M2s" is a lie unless lsl_30day backs it.
MMR = wholesale auction value. rBook p50 / partner asking = retail. Don't confuse.
"Live data / live sales / live system" = LSL (same thing).

═══ TRIM + MILEAGE DISCIPLINE ═══
If operator names a trim (GTS, GT3, Lariat, AMG, M4 CSL, etc.), ONLY cite data that matches that trim. If we have zero data for the requested trim, say so plainly. Never substitute another trim.
Every data field has comp_miles (median odometer of comps) and requested_miles. If they differ by >25,000 miles: state the gap, apply a per-mile adjustment ($0.15-0.25/mi for exotic, $0.05-0.10/mi mainstream), and flag confidence as low.
mileage_ladder (in prior_bids_30day_trim, vauto_saved_30day_trim) is comps sorted by miles. When ≥2 rows present, cite the comp closest to the subject's miles by NAME, then interpolate. Never quote aggregate median if a closer-mile comp exists.

═══ STYLE ═══
When CALLING A TOOL: open with a 3-5 word filler ("On it.", "Sure, looking now.", "One sec.") then deliver data.
When CASUAL CHAT (greetings, banter): no filler, just answer.
Lead with target buy or the key number. Cite ONE concrete trim-matching source. End with action ("hold firm at X" / "stretch to Y if clean"). If no data, give wide range with "confidence low" + offer to submit a bid via submit_bid_to_ew.

═══ NEVER CAVE ═══
If operator pushes back on your number, defend with data or ask for NEW info ("What are you seeing?"). Don't lower the target just to please.

═══ CALLER IDENTITY ═══
Owner-gated tools (lsl_deals_booked, lsl_top_grosses, lsl_lookup_sale, find_best_buyer) require caller's first name. Listen for "this is X" / "I'm X" at session start; remember it. Pass caller_name to those tools.
Valid first names are secret. NEVER list them. If a caller gives a name not on the list, say "That name does not have access — please confirm your first name."

═══ BID SUBMISSION ═══
Before submit_bid_to_ew:
1. Confirm caller's first name.
2. Echo VIN back digit-by-digit. Wait for confirmation.
3. Then call submit_bid_to_ew(vin, submitted_by, mileage).
On success: "Got it — bid number <id>. Pipeline appraises in about ninety seconds."

═══ DATA FIELD CHEAT SHEET ═══
get_vehicle_valuation returns:
• lsl_30day (purchases last 30d, by trim)
• prior_bids_30day_trim / _all (EW appraisals last 30d, with mileage_ladder)
• accutrade_30day_trim / _all (guaranteed_offer, trade_in, retail per Cox/Manheim)
• vauto_saved_30day_trim / _all (every vAuto appraisal incl mmr + rbook per car)
• partner_inventory_now (current partner listings, with master-list MMR+rBook)
• partner_sold_history (partner listings that left inventory, with DOL)
• live_mmr / live_rbook (cached vAuto direct)

find_best_buyer returns:
• buy_profile_matches (T1/T2 onboarded-dealer match scores — currently TXT Charlie + Nuccio). LEAD with these if score ≥60.
• top_pitch_buyers / recent_buyers (LSL ledger ranked buyers).
• rolling_windows (90/180/365 day counts + avg sale + avg gross).

get_bid returns full bid card: vehicle + MMR + rBook + AccuTrade + AI assessment + partner offers + buy_profile_matches + photos + status.

lsl_deals_booked returns: n_deals, total_profit, pvr, top_3, deals[] (with sold_to, bought_from, salesperson, profit per row)."""


_client: anthropic.Anthropic | None = None
def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


_HISTORY: dict[str, list[dict]] = {}
_HISTORY_LIMIT = 20


def _trim_history(sid: str) -> None:
    h = _HISTORY.get(sid)
    if h and len(h) > _HISTORY_LIMIT * 2:
        _HISTORY[sid] = h[-(_HISTORY_LIMIT * 2):]


@bp.route("/api/ew-voice", methods=["POST"])
def ew_voice():
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "empty transcript"}), 400
    session_id = (data.get("session_id") or "").strip() or f"ew-{uuid.uuid4().hex[:12]}"

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY missing"}), 500

    history = _HISTORY.setdefault(session_id, [])
    history.append({"role": "user", "content": transcript})

    t0 = time.monotonic()
    tool_calls = []
    reply_text = ""

    try:
        client = _get_client()
        msg = client.beta.messages.create(
            model=MODEL,
            max_tokens=280,
            system=SYSTEM_PROMPT,
            messages=history,
            mcp_servers=[{
                "type": "url",
                "url": MCP_URL,
                "name": "experience-wholesale",
                "authorization_token": MCP_BEARER_TOKEN or None,
            }],
            betas=["mcp-client-2025-04-04", "prompt-caching-2024-07-31"],
        )
        for block in (msg.content or []):
            bt = getattr(block, "type", None)
            if bt == "text":
                reply_text += (getattr(block, "text", "") or "")
            elif bt == "mcp_tool_use":
                tool_calls.append({
                    "name": getattr(block, "name", "?"),
                    "input": getattr(block, "input", {}),
                })
        history.append({"role": "assistant", "content": msg.content})
        _trim_history(session_id)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            f"session={session_id} transcript={transcript!r} "
            f"tools={[t['name'] for t in tool_calls]} "
            f"reply_text={reply_text[:80]!r} "
            f"elapsed_ms={elapsed_ms}"
        )
        return jsonify({
            "session_id": session_id,
            "reply_text": reply_text.strip(),
            "tool_calls": tool_calls,
            "elapsed_ms": elapsed_ms,
            "model": MODEL,
        })
    except Exception as e:
        log.exception("ew-voice error")
        return jsonify({"error": str(e)[:300]}), 500


_SENT_BOUNDARY = re.compile(r"([.!?])(?=\s|$|[\"')\]])")


def _split_sentences(buf: str) -> tuple[list[str], str]:
    """Pop complete sentences off the front of buf. Returns (complete, remainder)."""
    out = []
    i = 0
    last = 0
    while i < len(buf):
        m = _SENT_BOUNDARY.search(buf, i)
        if not m:
            break
        end = m.end()
        # Skip trailing whitespace
        while end < len(buf) and buf[end] in " \t\n":
            end += 1
        out.append(buf[last:end].strip())
        last = end
        i = end
    return out, buf[last:]


@bp.route("/api/ew-voice/stream", methods=["POST"])
def ew_voice_stream():
    """SSE-streamed variant: text deltas emitted as they arrive from
    Claude, plus per-sentence events the client can use to fire
    incremental TTS while the rest of the reply is still being generated.
    """
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "empty transcript"}), 400
    session_id = (data.get("session_id") or "").strip() or f"ew-{uuid.uuid4().hex[:12]}"

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY missing"}), 500

    history = _HISTORY.setdefault(session_id, [])
    history.append({"role": "user", "content": transcript})

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    @stream_with_context
    def generate():
        t0 = time.monotonic()
        tool_calls = []
        full_text = ""
        sent_buf = ""
        try:
            client = _get_client()
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=280,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=history,
                mcp_servers=[{
                    "type": "url",
                    "url": MCP_URL,
                    "name": "experience-wholesale",
                    "authorization_token": MCP_BEARER_TOKEN or None,
                }],
                betas=["mcp-client-2025-04-04", "prompt-caching-2024-07-31"],
            ) as stream:
                first_text_t = None
                for event in stream:
                    et = getattr(event, "type", None)
                    if et == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block is not None and getattr(block, "type", None) == "mcp_tool_use":
                            tc = {
                                "name": getattr(block, "name", "?"),
                                "input": getattr(block, "input", {}),
                            }
                            tool_calls.append(tc)
                            yield _sse({"type": "tool_call", "name": tc["name"]})
                    elif et == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None: continue
                        dt = getattr(delta, "type", None)
                        if dt == "text_delta":
                            piece = getattr(delta, "text", "") or ""
                            if not piece: continue
                            if first_text_t is None:
                                first_text_t = time.monotonic()
                                yield _sse({"type": "first_text_ms",
                                            "ms": int((first_text_t - t0) * 1000)})
                            full_text += piece
                            sent_buf += piece
                            sents, sent_buf = _split_sentences(sent_buf)
                            for s_text in sents:
                                if s_text:
                                    yield _sse({"type": "sentence", "text": s_text})
                    elif et == "message_stop":
                        break
                final_msg = stream.get_final_message()
            # Persist assistant turn
            history.append({"role": "assistant", "content": final_msg.content})
            _trim_history(session_id)
            # Flush any remaining partial sentence
            if sent_buf.strip():
                yield _sse({"type": "sentence", "text": sent_buf.strip()})
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                f"stream session={session_id} transcript={transcript!r} "
                f"tools={[t['name'] for t in tool_calls]} "
                f"reply={full_text[:80]!r} elapsed_ms={elapsed_ms}"
            )
            yield _sse({
                "type": "final",
                "reply_text": full_text.strip(),
                "tool_calls": tool_calls,
                "elapsed_ms": elapsed_ms,
                "model": MODEL,
                "session_id": session_id,
            })
        except Exception as e:
            log.exception("ew-voice stream error")
            yield _sse({"type": "error", "message": str(e)[:300]})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@bp.route("/api/ew-voice/warmup", methods=["GET", "POST"])
def warmup():
    """Fire a tiny Anthropic call with the CACHED SYSTEM_PROMPT to keep
    the prompt cache + TLS warm. Anthropic prompt-cache TTL is ~5 min —
    cron hits this every 4 min so the first real user turn is warm.
    Cost: ~$0.0001 per call."""
    import time as _t
    t0 = _t.monotonic()
    try:
        client = _get_client()
        client.beta.messages.create(
            model=MODEL,
            max_tokens=4,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": "ok"}],
            betas=["prompt-caching-2024-07-31"],
        )
        elapsed_ms = int((_t.monotonic() - t0) * 1000)
        return jsonify({"ok": True, "warmed": ["anthropic_cache"],
                        "elapsed_ms": elapsed_ms})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500



@bp.route("/api/ew-voice/tool", methods=["POST"])
def tool_proxy():
    """Dispatch a single MCP tool call by name. Used by the local
    LiveKit agent worker on the operator's home PC so it can call
    our PG-backed tools over HTTPS instead of needing a DB tunnel.

    Auth: Bearer token matching MCP_BEARER_TOKEN.
    Body: {"tool": "get_bid", "args": {"bid_id": 1983}}
    """
    import os as _os, json as _json, asyncio as _asyncio, inspect as _inspect
    # Cloudflare strips Authorization headers for non-EW-session calls,
    # so accept the bearer in the JSON body too ("bearer" key).
    data_pre = request.get_json(silent=True) or {}
    auth_hdr = request.headers.get('Authorization', '')
    auth_body = (data_pre.get('bearer') or '').strip()
    auth = auth_hdr if auth_hdr.startswith('Bearer ') else ('Bearer ' + auth_body if auth_body else '')
    expected = _os.environ.get("MCP_BEARER_TOKEN", "")
    if not expected or auth != f"Bearer {expected}":
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    tool_name = (data.get("tool") or "").strip()
    args = data.get("args") or {}
    if not tool_name:
        return jsonify({"error": "tool name required"}), 400
    try:
        import ew_mcp
        obj = getattr(ew_mcp, tool_name, None)
        if obj is None:
            return jsonify({"error": f"unknown tool {tool_name}"}), 404
        fn = getattr(obj, "fn", obj)
        if _inspect.iscoroutinefunction(fn):
            result = _asyncio.run(fn(**args))
        else:
            result = fn(**args)
        return jsonify(result if isinstance(result, dict) else {"result": result})
    except Exception as e:
        log.exception("tool_proxy failed")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@bp.route("/api/ew-voice/reset", methods=["POST"])
def reset_history():
    data = request.get_json(silent=True) or {}
    sid = (data.get("session_id") or "").strip()
    if sid and sid in _HISTORY:
        del _HISTORY[sid]
    return jsonify({"ok": True})
