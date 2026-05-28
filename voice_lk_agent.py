"""voice_lk_agent.py — LiveKit Agents worker, slim tool set to fit the
Anthropic 16-union-param schema limit. Keeps only the highest-value
tools; lookup_vin / lsl_top_grosses / lsl_lookup_sale / dashboard_stats
fall out (can be added back later via a 2nd agent or reduced params)."""
from __future__ import annotations
import logging, os, sys

from livekit.agents import (
    Agent, AgentSession, JobContext, JobProcess, RunContext,
    WorkerOptions, cli, function_tool,
)
from livekit.plugins import anthropic, elevenlabs, google, silero

sys.path.insert(0, "/opt/expwholesale")
import ew_mcp  # noqa: E402
from ew_voice_api import SYSTEM_PROMPT  # noqa: E402

log = logging.getLogger("voice-lk-agent")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "uC7JfYSX7CskaRcdTv72")
ELEVEN_MODEL    = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ANTHROPIC_MODEL = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")


def _unwrap(name):
    obj = getattr(ew_mcp, name, None)
    if obj is None: raise RuntimeError(f"ew_mcp has no {name}")
    return getattr(obj, "fn", obj)


def _z(v):  # zero-or-empty → None
    if v in (0, "", 0.0): return None
    return v


@function_tool
async def get_vehicle_valuation(
    context: RunContext,
    year: int, make: str, model: str, miles: int, trim: str,
) -> dict:
    """Wholesale valuation for year/make/model+trim+miles. Returns target buy,
    MMR, rBook, AccuTrade, LSL purchases, mileage ladder, buyer match.
    Use for ANY 'what is X worth' / 'what should I bid' question.
    Pass empty string for trim if unknown; pass 0 for miles if unknown."""
    return await _unwrap("get_vehicle_valuation")(
        year=year, make=make, model=model,
        miles=_z(miles), trim=_z(trim))


@function_tool
async def get_bid(context: RunContext, bid_id: int) -> dict:
    """Read a specific bid from the EW dashboard — vehicle, MMR + rBook,
    AccuTrade, AI assessment, partner offers, buy_profile_matches, photos,
    status. Use for any 'bid <number>' reference."""
    return await _unwrap("get_bid")(bid_id=bid_id)


@function_tool
async def recent_bids(context: RunContext, limit: int) -> dict:
    """Most recent N bids in the EW dashboard. Default limit: 5."""
    return await _unwrap("recent_bids")(limit=limit or 5)


@function_tool
async def lsl_deals_booked(
    context: RunContext, caller_name: str, period: str,
) -> dict:
    """OWNER-GATED. LSL deals booked over a period. Returns count, profit,
    PVR, top 3, full deals[] list with buyer + salesperson + sold_to +
    bought_from + profit per row. period: yesterday|today|last_7_days|
    last_30_days|this_month|last_month|this_quarter|last_quarter|ytd|
    this_year|last_year|all_time."""
    return await _unwrap("lsl_deals_booked")(
        caller_name=caller_name, period=period or "yesterday")


@function_tool
async def find_best_buyer(
    context: RunContext, caller_name: str,
    year: int, make: str, model: str, trim: str,
) -> dict:
    """OWNER-GATED. Ranked partner-dealer buyer matches for a year/make/model.
    Pass empty string for trim if unknown.
    Returns buy_profile_matches (onboarded T1/T2), top_pitch_buyers,
    recent_buyers, rolling_windows."""
    return await _unwrap("find_best_buyer")(
        caller_name=caller_name, year=year, make=make, model=model,
        trim=_z(trim))


@function_tool
async def submit_bid_to_ew(
    context: RunContext,
    vin: str, submitted_by: str, mileage: int,
) -> dict:
    """Drop a new bid into the EW dashboard. Requires VIN (17 chars),
    caller's first name, and mileage."""
    return await _unwrap("submit_bid_to_ew")(
        vin=vin, submitted_by=submitted_by, mileage=mileage)



# ─── BILL_INTEL_2026_05_27 — Phase A + ai_critique tools ────────────────

@function_tool
async def inventory_gaps_now(
    context: RunContext, caller_name: str, top_n_dealers: int,
) -> dict:
    """OWNER. Today's inventory holes + surpluses across the portal
    dealer network. Use when asked 'what holes do we have',
    'where are the surpluses', 'gaps', 'holes and surplus',
    'what is missing in inventory'. top_n_dealers: cap (default 10)."""
    return await _unwrap("inventory_gaps_now")(
        caller_name=caller_name, top_n_dealers=top_n_dealers or 10)


@function_tool
async def ai_assessment_for_bid(
    context: RunContext, caller_name: str, bid_id: int,
) -> dict:
    """OWNER. Return the LLM's existing assessment of a bid plus the
    market inputs (vAuto/MMR/rBook, AccuTrade, iPacket) it had. Use when
    asked 'what did the AI say about bid X', 'second-opinion bid X',
    'do you agree with the AI on bid X'."""
    return await _unwrap("ai_assessment_for_bid")(
        caller_name=caller_name, bid_id=bid_id)


@function_tool
async def ml_predict_price(
    context: RunContext,
    make: str, year: int, mileage: int, est_wholesale_price: float,
    model: str,
) -> dict:
    """Per-make ML model price prediction (xgboost). Second-opinion
    signal alongside MMR/rBook/AI. Use when asked 'what does the ML
    model say', 'ML predict', 'ML take on this car'. Pass empty string
    for model if unknown. est_wholesale_price is required (MMR / rBook
    median is the anchor input)."""
    return await _unwrap("ml_predict_price")(
        make=make, year=year, mileage=mileage,
        est_wholesale_price=est_wholesale_price, model=_z(model))


@function_tool
async def dealer_opportunities_now(
    context: RunContext, caller_name: str, top_n: int,
) -> dict:
    """OWNER. Today's top dealer-watch buy opportunities — vehicles in
    the portal dealer network priced under MMR. From the 09:30 daily AI
    scout. Use when asked 'best opportunities today', 'what should we
    buy', 'top dealer picks', 'opportunities'. top_n default 10."""
    return await _unwrap("dealer_opportunities_now")(
        caller_name=caller_name, top_n=top_n or 10)


@function_tool
async def ai_critique(
    context: RunContext, caller_name: str, bid_id: int, question: str,
) -> dict:
    """OWNER. Ask the AI assessor (Gemini) a follow-up question about a
    specific bid's existing assessment. The assessor sees its prior
    verdict + original market inputs + your question, then replies in
    2-4 sentences. Use when asked 'ask the assessor why...', 'second-
    guess the AI on bid X', 'what if mileage was lower', 'do you stand
    by that price'. Returns {answer: '...'} — speak the answer back."""
    return await _unwrap("ai_critique")(
        caller_name=caller_name, bid_id=bid_id, question=question)


@function_tool
async def dashboard_stats(context: RunContext) -> dict:
    """General EW dashboard health: bid counts, assessment progress,
    pipeline state. Use when asked 'how is the dashboard',
    'dashboard stats', 'what is going on today', 'state of EW'."""
    return await _unwrap("dashboard_stats")()


@function_tool
async def briefing_now(context: RunContext, caller_name: str) -> dict:
    """OWNER. Generate today's morning briefing on-demand — overnight
    bid count + top vehicle, dealer-watch top opportunity, stale bids,
    watchlist hits yesterday. Use when asked 'give me my briefing',
    'catch me up', 'morning brief', 'rundown', 'whats going on this
    morning'."""
    return await _unwrap("briefing_now")(caller_name=caller_name)


@function_tool
async def lsl_query(
    context: RunContext, caller_name: str, query: str,
) -> dict:
    """OWNER. Unified LSL deep-query dispatcher. Free-form natural
    language LSL questions: 'how much profit last week', 'what did Joe
    sell yesterday', 'inventory over 90 days', 'top buyer this month',
    'service queue'. Routes to the right LSL backend."""
    return await _unwrap("lsl_query")(
        caller_name=caller_name, query=query)


ALL_TOOLS = [
    # Original 6 (yesterday's Bill)
    get_vehicle_valuation, get_bid, recent_bids,
    lsl_deals_booked, find_best_buyer, submit_bid_to_ew,
    # Added 2026-05-27 — Phase A + ai_critique + 3 high-value extras
    inventory_gaps_now, ai_assessment_for_bid, ml_predict_price,
    dealer_opportunities_now, ai_critique,
    dashboard_stats, briefing_now, lsl_query,
]


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()
    log.info("VAD loaded in prewarm")


async def entrypoint(ctx: JobContext) -> None:
    log.info(f"agent joining room {ctx.room.name}")
    await ctx.connect()
    session = AgentSession(
        # CPU-LIGHT: skip Silero VAD entirely; rely on Google STT voice
        # activity events. Contabo CPU could not keep up with Silero
        # inference under streaming audio (3s+ behind realtime).
        stt=google.STT(spoken_punctuation=False, enable_voice_activity_events=True),
        llm=anthropic.LLM(model=ANTHROPIC_MODEL, _strict_tool_schema=False),
        tts=elevenlabs.TTS(voice_id=ELEVEN_VOICE_ID, model=ELEVEN_MODEL),
    )
    agent = Agent(instructions=SYSTEM_PROMPT, tools=ALL_TOOLS)
    await session.start(agent=agent, room=ctx.room)
    log.info("agent session live")
    await session.generate_reply(
        instructions="Briefly greet — say exactly: 'EW here, what can I look up?' Nothing else.",
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
