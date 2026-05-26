"""/opt/ew_voice/agent.py — LiveKit Agents worker w/ minimal docstrings
so we can fit MORE tools under Anthropic's schema-complexity ceiling."""
from __future__ import annotations
import logging, os, sys

import aiohttp
from livekit.agents import (
    Agent, AgentSession, JobContext, JobProcess, RunContext,
    WorkerOptions, cli, function_tool,
)
from livekit.agents.voice.room_io import RoomOutputOptions
from livekit import rtc
from livekit.plugins import anthropic, elevenlabs, google, silero, openai as lkopenai

log = logging.getLogger("local-agent")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "T5cu6IU92Krx4mh43osx")
ELEVEN_MODEL    = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ANTHROPIC_MODEL = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")
INBOUND_LLM_PROVIDER = os.environ.get("INBOUND_LLM_PROVIDER", "anthropic").lower()
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
OLLAMA_BASE_URL  = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11435/v1")
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL", "hermes3:8b")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_PROJECT   = os.environ.get("GEMINI_PROJECT", "my-project-dia-492415")
GEMINI_LOCATION  = os.environ.get("GEMINI_LOCATION", "global")

def _build_llm():
    """Pick LLM plugin per INBOUND_LLM_PROVIDER."""
    if INBOUND_LLM_PROVIDER in ("gemini", "google", "vertex"):
        return google.LLM(
            model=GEMINI_MODEL,
            vertexai=True,
            project=GEMINI_PROJECT,
            location=GEMINI_LOCATION,
            max_output_tokens=600,  # was defaulting too low → mid-sentence cutoffs on multi-period responses
            temperature=0.4,
        )
    if INBOUND_LLM_PROVIDER == "ollama":
        return lkopenai.LLM(
            model=OLLAMA_MODEL,
            api_key="ollama-no-key",
            base_url=OLLAMA_BASE_URL,
        )
    if INBOUND_LLM_PROVIDER == "cerebras" and CEREBRAS_API_KEY:
        return lkopenai.LLM(
            model=CEREBRAS_MODEL,
            api_key=CEREBRAS_API_KEY,
            base_url="https://api.cerebras.ai/v1",
        )
    return anthropic.LLM(model=ANTHROPIC_MODEL, _strict_tool_schema=False)
TOOL_URL  = "https://experience-wholesale.net/api/ew-voice/tool"
BEARER    = os.environ.get("MCP_BEARER_TOKEN", "")

_RAW_SYSTEM_PROMPT = open("/opt/ew_voice/system_prompt.txt").read()

def _build_system_prompt():
    """Prepend current date/time so Bill can resolve relative day references."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now()
    header = (
        f"\n═══ CURRENT TIME (ET) ═══\n"
        f"Today is {now.strftime('%A, %B %d, %Y')} (ISO {now.strftime('%Y-%m-%d')}). "
        f"Current time: {now.strftime('%I:%M %p %Z')}.\n"
        f"Yesterday was {(now.replace(hour=12) - __import__('datetime').timedelta(days=1)).strftime('%A, %B %d')}. "
        f"Last Monday was {(now - __import__('datetime').timedelta(days=(now.weekday() or 7))).strftime('%Y-%m-%d')}. "
        f"Last Friday was {(now - __import__('datetime').timedelta(days=((now.weekday() - 4) % 7) or 7)).strftime('%Y-%m-%d')}.\n"
        f"When the operator says 'Friday' / 'yesterday' / 'last week', resolve relative to TODAY ({now.strftime('%A %Y-%m-%d')}) — not your training cutoff.\n"
        f"For tool calls needing dates, translate weekday references into ISO dates.\n"
    )
    return header + _RAW_SYSTEM_PROMPT

SYSTEM_PROMPT = _build_system_prompt()  # initial load — rebuilt per entrypoint


async def _call_remote(tool: str, args: dict) -> dict:
    payload = {"bearer": BEARER, "tool": tool, "args": args}
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(TOOL_URL, json=payload) as r:
            return await r.json()


def _z(v):
    if v in (0, "", 0.0): return None
    return v


@function_tool
async def carvana_offer(
    context: RunContext, vin: str, miles: int,
) -> dict:
    """Carvana instant-offer for a SPECIFIC VIN. Uses our verifier-VM
    worker pool to scrape value.carvana.com past Cloudflare. Returns
    within ~8s (cache hit instant, fresh ~3-6s, pending if no worker).
    USE in parallel with live_valuation_by_vin once you have a VIN —
    Carvana's offer is a great 3rd data point alongside MMR and AccuTrade.
    Response: {vin, offer_amount, offer_expires, status: cached|fresh|pending|failed|no_offer}"""
    return await _call_remote("carvana_offer", {
        "vin": vin or "", "miles": int(miles) if miles else 0,
    })


@function_tool
async def find_vin_for_ymm(
    context: RunContext, year: int, make: str, model: str,
    trim: str, miles: int,
) -> dict:
    """Discover a VIN for a YMM we do not have in our system. USE WHEN
    valuation_precheck returned confidence='none'. Tries our DB first
    (closest-mile match in bids/deals), falls back to AutoTrader scrape.
    Returns {vin, source, found} within ~2-3s. If found=true, immediately
    call live_valuation_by_vin with that VIN."""
    return await _call_remote("find_vin_for_ymm", {
        "year": int(year), "make": make or "", "model": model or "",
        "trim": trim or "", "miles": int(miles) if miles else 0,
    })


@function_tool
async def live_valuation_by_vin(
    context: RunContext, vin: str, miles: int,
    year: int, make: str, model: str, trim: str,
) -> dict:
    """LIVE vAuto/AccuTrade lookup anchored on a SPECIFIC VIN. Use AFTER
    find_vin_for_ymm gave you a VIN. Returns within ~5-10s with live MMR,
    rBook, AccuTrade comps centered on this VIN. miles is the target
    odometer (use the caller's number, not the VIN's actual miles)."""
    return await _call_remote("live_valuation_by_vin", {
        "vin": vin or "", "miles": int(miles) if miles else 0,
        "year": int(year) if year else 0,
        "make": make or "", "model": model or "", "trim": trim or "",
    })


@function_tool
async def valuation_precheck(
    context: RunContext, year: int, make: str, model: str, trim: str,
) -> dict:
    """FAST (~200-500ms) DB-only precheck. Call BEFORE get_vehicle_valuation.
    Returns counts of prior bids, prior deals, current dealer inventory so
    you can craft an appropriate filler line about whether we've seen this
    YMM before. THEN immediately call get_vehicle_valuation for the slow
    live comps."""
    return await _call_remote("valuation_precheck", {
        "year": int(year), "make": make or "", "model": model or "",
        "trim": trim or "",
    })


@function_tool
async def get_vehicle_valuation(
    context: RunContext, year: int, make: str, model: str,
    miles: int, trim: str,
) -> dict:
    """Wholesale valuation by YMM. miles=0 or trim='' if unknown."""
    return await _call_remote("get_vehicle_valuation", {
        "year": year, "make": make, "model": model,
        "miles": _z(miles), "trim": _z(trim),
    })


@function_tool
async def get_bid(context: RunContext, bid_id: int) -> dict:
    """Full bid card by id — vehicle, vAuto, AccuTrade, iPacket MSRP,
    Carfax/AutoCheck damage_audit, AI assessment, partner offers."""
    return await _call_remote("get_bid", {"bid_id": bid_id})


@function_tool
async def lsl_deals_booked(
    context: RunContext, caller_name: str, period: str,
) -> dict:
    """OWNER. LSL deals/gross/PVR by period."""
    return await _call_remote("lsl_deals_booked", {
        "caller_name": caller_name, "period": period or "yesterday",
    })


@function_tool
async def find_best_buyer(
    context: RunContext, caller_name: str,
    year: int, make: str, model: str, trim: str,
) -> dict:
    """Ranked partner-dealer buyer matches for YMM. trim='' if unknown."""
    return await _call_remote("find_best_buyer", {
        "caller_name": caller_name, "year": year, "make": make,
        "model": model, "trim": _z(trim),
    })


@function_tool
async def lsl_salesperson_stats(
    context: RunContext, caller_name: str,
    salesperson_name: str, period: str,
) -> dict:
    """OWNER. Salesperson/manager performance by period."""
    return await _call_remote("lsl_salesperson_stats", {
        "caller_name": caller_name,
        "salesperson_name": salesperson_name,
        "period": period or "this_month",
    })


@function_tool
async def lsl_inventory_now(
    context: RunContext, caller_name: str,
    make: str, model: str, year: int,
) -> dict:
    """OWNER. Cars currently on the EW lot. Empty/0 = all."""
    return await _call_remote("lsl_inventory_now", {
        "caller_name": caller_name,
        "make": make, "model": model, "year": year,
    })


@function_tool
async def lsl_customer_history(
    context: RunContext, caller_name: str, customer_name: str,
) -> dict:
    """OWNER. Full deal history with a customer or dealer (both directions)."""
    return await _call_remote("lsl_customer_history", {
        "caller_name": caller_name,
        "customer_name": customer_name,
        "limit": 10,
    })


@function_tool
async def recent_bids(context: RunContext, limit: int) -> dict:
    """Last N bids on the dashboard (max 20, newest first, no filters). For
    a date window WIDER than the last 20, or for filtering by make/model/
    submitter, use search_bids instead."""
    return await _call_remote("recent_bids", {"limit": limit or 5})


@function_tool
async def search_bids(
    context: RunContext,
    make: str,
    model: str,
    year: int,
    since_days: int,
    submitter: str,
    limit: int,
) -> dict:
    """Search bids across a date window with filters. USE THIS when operator
    asks 'did we bid any [make/model] in the last N days', 'who submitted
    the bid for X', or wants more than the last 20 bids.
    Args:
      make: vehicle make (substring match, '' for any)
      model: vehicle model (substring match, '' for any)
      year: model year (0 for any)
      since_days: how many days back (1-365, default 30)
      submitter: filter by contact name or company substring ('' for any)
      limit: max bids to return (1-100, default 25)
    Returns: {filters, n, bids[]} with each bid having submitter_name and
    submitter_company so you can tell the operator WHO submitted it."""
    return await _call_remote("search_bids", {
        "make": make or "",
        "model": model or "",
        "year": int(year) if year else 0,
        "since_days": int(since_days) if since_days else 30,
        "submitter": submitter or "",
        "limit": int(limit) if limit else 25,
    })


@function_tool
async def lsl_query(
    context: RunContext, caller_name: str, query_type: str, target: str,
) -> dict:
    """OWNER. LSL deep query. query_type values: dealer_intel,
    service_requests, payments, appraisal_history, customer_lookup,
    top_grosses, lookup_sale. target = name/vin/stock (or '')."""
    return await _call_remote("lsl_query", {
        "caller_name": caller_name,
        "query_type": query_type,
        "target": target or "",
        "status_filter": "",
        "days_back": 30,
    })


@function_tool
async def submit_bid_to_ew(
    context: RunContext, vin: str, submitted_by: str, mileage: int,
) -> dict:
    """Submit a new bid. VIN + caller first name + mileage."""
    return await _call_remote("submit_bid_to_ew", {
        "vin": vin, "submitted_by": submitted_by, "mileage": mileage,
    })


@function_tool
async def lsl_make_volume(
    context: RunContext, caller_name: str, make: str, period: str,
) -> dict:
    """OWNER. Count + summarize deals of a specific MAKE in a period.
    USE for "how many BMWs/Mercedes/Fords did we buy this month/year/etc".
    Returns n_deals, total_profit, avg_pvr, top_models for the filtered make.
    period: ytd, this_month, last_month, last_year, last_30_days, 'april',
    'april_mtd', or ISO range '2026-04-01:2026-04-25'."""
    return await _call_remote("lsl_make_volume", {
        "caller_name": caller_name,
        "make": make or "",
        "period": period or "ytd",
    })


@function_tool
async def lsl_top_makes(
    context: RunContext, caller_name: str, period: str, limit: int,
) -> dict:
    """OWNER. Top N makes by deal count in a period. USE for "what did we
    buy most of", "top makes this year". limit defaults to 10 if unspecified."""
    return await _call_remote("lsl_top_makes", {
        "caller_name": caller_name,
        "period": period or "ytd",
        "limit": int(limit) if limit else 10,
    })


ALL_TOOLS = [
    valuation_precheck, find_vin_for_ymm, live_valuation_by_vin,
    get_vehicle_valuation,
    get_bid, recent_bids, search_bids,
    lsl_deals_booked, lsl_make_volume, lsl_top_makes,
    find_best_buyer, submit_bid_to_ew,
    lsl_salesperson_stats, lsl_inventory_now,
    lsl_customer_history, lsl_query,
]


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()
    log.info("VAD loaded in prewarm")


async def entrypoint(ctx: JobContext) -> None:
    log.info(f"agent joining room {ctx.room.name}")
    await ctx.connect()
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=google.STT(spoken_punctuation=False),
        llm=_build_llm(),
        tts=elevenlabs.TTS(voice_id=ELEVEN_VOICE_ID, model=ELEVEN_MODEL, streaming_latency=4),
        # Give the operator room to pause mid-sentence (default 0.5s was too aggressive,
        # cutting off utterances on natural "uh...so..." pauses).
        min_endpointing_delay=2.0,
        max_endpointing_delay=6.0,
    )
    # GLM-4.7 sometimes emits 'None' as filler text alongside tool calls.
    # This TTS filter intercepts and substitutes a real acknowledgment phrase.
    class _FilteredBill(Agent):
        _BAD = {'none','null','nothing','n/a','undefined','empty',''}
        _ACK = ['Let me check that.','One sec.','Pulling that now.','Hold on, looking.']
        _ack_idx = 0
        async def tts_node(self, text, model_settings):
            import re
            async def filtered():
                buf = ''
                decided = False
                async for chunk in text:
                    if chunk is None:
                        continue
                    if not decided:
                        buf += chunk
                        stripped = re.sub(r'[\.\,\!\?\s]+$', '', buf.strip().lower())
                        if stripped in _FilteredBill._BAD:
                            phrase = _FilteredBill._ACK[_FilteredBill._ack_idx % len(_FilteredBill._ACK)]
                            _FilteredBill._ack_idx += 1
                            log.info(f'BILL_TTS_FILTER: dropped {buf!r}, sub {phrase!r}')
                            yield phrase
                            buf = ''
                            decided = True
                        elif len(buf) >= 6 or any(c in buf for c in ' .,!?'):
                            yield buf
                            buf = ''
                            decided = True
                    else:
                        yield chunk
                if buf and not decided:
                    yield buf
            async for audio in Agent.default.tts_node(self, filtered(), model_settings):
                yield audio
    agent = _FilteredBill(instructions=_build_system_prompt(), tools=ALL_TOOLS)

    # iOS Safari WebRTC does NOT support audio/red mime (RFC 2198 RED wrapper).
    # Publishing plain Opus instead, with DTX off to keep iOS audio path alive.
    # Default would yield mime=audio/red which silently drops on iPhone Safari.
    publish_opts = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_MICROPHONE,
        red=False,
        dtx=False,
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        room_output_options=RoomOutputOptions(
            audio_publish_options=publish_opts,
        ),
    )
    log.info("agent session live (LOCAL with tools, red=False)")

    # ── Diagnostic logging: STT inputs, tool calls/results, LLM replies ──
    # CALLER_NAME_FROM_URL: if the client sent ?name=Oscar, the participant
    # arrives with attributes["caller_name"]. Capture it and tell Bill.
    def _on_participant_connected(part):
        try:
            attrs = getattr(part, "attributes", {}) or {}
            cn = attrs.get("caller_name") or getattr(part, "name", "") or ""
            if cn:
                log.info(f"caller_name from URL: {cn!r}")
                # Inject as a system note via session chat ctx
                try:
                    extra = (
                        f"\n\n═══ CALLER IDENTITY (from URL) ═══\n"
                        f"The current caller is {cn}. Use this for owner-gated tools "
                        f"(lsl_*) without asking the user to repeat their name. Pass caller_name={cn!r}."
                    )
                    agent.instructions = (agent.instructions or "") + extra
                except Exception as _e:
                    log.warning(f"caller_name injection failed: {_e}")
        except Exception as _e:
            log.warning(f"participant_connected handler err: {_e}")

    @ctx.room.on("participant_connected")
    def _pc(part):
        _on_participant_connected(part)

    # Also check participants already in the room (race with first connect)
    for _p in (ctx.room.remote_participants or {}).values():
        _on_participant_connected(_p)

    @session.on("user_input_transcribed")
    def _log_user_input(ev):
        try:
            text = getattr(ev, "transcript", None) or getattr(ev, "text", "")
            is_final = getattr(ev, "is_final", True)
            if is_final and text:
                log.info(f"USER: {text!r}")
        except Exception as _e:
            log.warning(f"user_input log err: {_e}")

    @session.on("function_tools_executed")
    def _log_tools(ev):
        try:
            for fc in getattr(ev, "function_calls", []) or []:
                name = getattr(fc, "name", "?")
                args = getattr(fc, "arguments", "") or getattr(fc, "args", "")
                if isinstance(args, dict):
                    import json as _j
                    args = _j.dumps(args)[:300]
                log.info(f"TOOL_CALL: {name}({args!s:.300})")
            for out in getattr(ev, "function_call_outputs", []) or []:
                name = getattr(out, "name", "?")
                output = getattr(out, "output", "") or getattr(out, "result", "")
                if isinstance(output, dict):
                    import json as _j
                    output = _j.dumps(output, default=str)[:500]
                log.info(f"TOOL_RESULT: {name} -> {output!s:.500}")
        except Exception as _e:
            log.warning(f"tool log err: {_e}")

    @session.on("agent_state_changed")
    def _log_state(ev):
        try:
            log.info(f"AGENT_STATE: {getattr(ev, 'old_state', '?')} -> {getattr(ev, 'new_state', '?')}")
        except Exception:
            pass

    @session.on("conversation_item_added")
    def _log_conv(ev):
        try:
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = getattr(item, "role", "?")
            content = getattr(item, "content", "")
            if role == "assistant" and content:
                if isinstance(content, list):
                    content = " ".join(str(c) for c in content)
                log.info(f"BOT: {str(content)[:300]!r}")
        except Exception as _e:
            log.warning(f"conv log err: {_e}")

    # ── Listen for client page-visibility messages.
    # When the user's phone shows a notification, the page goes hidden,
    # client publishes {type: "page_hidden"} — we interrupt TTS so the
    # bot stops talking into a void. On {type: "page_visible"} we just
    # log; the user can re-prompt themselves.
    import json as _json
    from livekit import rtc as _rtc

    @ctx.room.on("data_received")
    def _on_data(packet: _rtc.DataPacket):
        try:
            msg = _json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        kind = msg.get("type")
        if kind == "page_hidden":
            log.info("client page_hidden — interrupting TTS")
            # Try every interrupt path the LiveKit Agents API exposes
            try:
                cs = getattr(session, "current_speech", None)
                if cs is not None:
                    cs.interrupt()
                    log.info("  current_speech.interrupt() ok")
            except Exception as e:
                log.warning(f"  current_speech.interrupt failed: {e}")
            try:
                session.interrupt()
                log.info("  session.interrupt() ok")
            except Exception as e:
                log.warning(f"  session.interrupt failed: {e}")
        elif kind == "page_visible":
            log.info("client page_visible — ready")
    import asyncio as _asyncio
    # Wait for the participant's audio track to be published. This is the real
    # signal that the bidirectional audio path is open (when the client's mic
    # indicator turns green on Twilio/web, the track has been published).
    # Without this, Bill's greeting fires into a half-open channel and the
    # first syllable gets clipped.
    mic_ready = _asyncio.Event()
    def _on_track_pub(pub, participant):
        try:
            if pub.kind == rtc.TrackKind.KIND_AUDIO and participant.identity != ctx.room.local_participant.identity:
                log.info(f"participant mic track published - audio path open")
                mic_ready.set()
        except Exception:
            pass
    ctx.room.on("track_published", _on_track_pub)
    # Also check if a remote mic is already published (race-condition safety)
    for rp in ctx.room.remote_participants.values():
        for pub in rp.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO:
                mic_ready.set()
                break
    try:
        await _asyncio.wait_for(mic_ready.wait(), timeout=8.0)
        log.info("mic ready - waiting 600ms buffer")
    except _asyncio.TimeoutError:
        log.warning("no mic track in 8s - firing greeting anyway")
    await _asyncio.sleep(0.6)
    log.info("firing greeting now")
    try:
        # Hardcoded greeting via session.say() bypasses LLM (saves ~150ms) and
        # guarantees identical wording. Time-of-day prefix uses operator's EDT.
        from datetime import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            _hr = _dt.now(ZoneInfo("America/New_York")).hour
        except Exception:
            _hr = _dt.now().hour
        if _hr < 12:
            _tod = "Good morning"
        elif _hr < 18:
            _tod = "Good afternoon"
        else:
            _tod = "Good evening"
        await session.say(
            f"{_tod}, this is Bill from Experience Wholesale. How can I help you?",
            allow_interruptions=True,
        )
        log.info(f"greeting dispatched ({_tod})")
    except Exception as e:
        log.exception(f"greeting failed: {e}")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, num_idle_processes=3))
