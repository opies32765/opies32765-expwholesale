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


ALL_TOOLS = [
    get_vehicle_valuation, get_bid, recent_bids,
    lsl_deals_booked, find_best_buyer, submit_bid_to_ew,
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
        llm=anthropic.LLM(model=ANTHROPIC_MODEL),
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
