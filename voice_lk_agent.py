"""voice_lk_agent.py — LiveKit Agents worker, rewritten per the canonical
agent-starter-python reference (livekit-examples/agent-starter-python).

Key fixes from the reference vs prior versions:
  - VAD loaded in prewarm() — fixes "inference is slower than realtime"
  - Google TTS uses Chirp3-HD voice + use_streaming=True + enable_ssml=False
    (NO gender= param — Google API rejects it for non-NEUTRAL voices)
  - Google STT model="chirp_2" (chirp_3 doesn't stream)
  - LLM = our custom EWLLM adapter (routes to /api/voice/query on main app)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

import aiohttp
from livekit.agents import (
    Agent,
    AgentSession,
    APIConnectOptions,
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.plugins import google, silero

log = logging.getLogger("voice-lk-agent")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

EW_QUERY_URL = os.environ.get(
    "EW_QUERY_URL",
    "http://127.0.0.1:9001/api/voice/query",
)


class EWLLM(llm.LLM):
    """Routes user transcripts to the existing /api/voice/query endpoint
    (which runs Gemini parse + LSL/MMR comp lookup + Claude reply)."""

    def __init__(self) -> None:
        super().__init__()
        self._session_id = f"lk-{uuid.uuid4().hex[:12]}"
        self._turn_index = 0
        self._phone: str | None = None
        self._http: aiohttp.ClientSession | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))
        return self._http

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        return _EWLLMStream(self, chat_ctx=chat_ctx,
                            tools=tools or [],
                            conn_options=conn_options or APIConnectOptions())

    async def aclose(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
        await super().aclose()


class _EWLLMStream(llm.LLMStream):
    def __init__(self, ew: EWLLM, *, chat_ctx, tools, conn_options):
        super().__init__(llm=ew, chat_ctx=chat_ctx,
                         tools=tools, conn_options=conn_options)
        self._ew = ew

    async def _run(self) -> None:
        transcript = ""
        items = (getattr(self.chat_ctx, "items", None)
                 or getattr(self.chat_ctx, "messages", []))
        for msg in reversed(items):
            role = getattr(msg, "role", None)
            if role and str(role).lower() == "user":
                content = (getattr(msg, "content", None)
                           or getattr(msg, "text_content", ""))
                if isinstance(content, list):
                    content = " ".join(str(c) for c in content)
                transcript = (content or "").strip()
                break
        if not transcript:
            log.warning(f"no user transcript; items={len(items)}")
            return

        self._ew._turn_index += 1
        payload = {
            "transcript": transcript,
            "session_id": self._ew._session_id,
            "turn_index": self._ew._turn_index,
            "phone": self._ew._phone,
        }
        t0 = time.monotonic()
        log.info(f"EW query: {transcript!r}")
        http = await self._ew._ensure_http()
        reply = ""
        try:
            async with http.post(EW_QUERY_URL, json=payload) as r:
                if r.status == 200:
                    data = await r.json()
                    reply = (data.get("reply_text") or "").strip()
                    if data.get("phone_captured"):
                        self._ew._phone = data["phone_captured"]
                else:
                    body = await r.text()
                    log.error(f"EW {r.status}: {body[:200]}")
                    reply = "Sorry — hit a backend error. Try that again?"
        except Exception:
            log.exception("EW query failed")
            reply = "Sorry — network glitch. Say that again?"

        if not reply:
            reply = "I didn't catch a clean answer — could you rephrase?"
        log.info(f"EW reply in {(time.monotonic()-t0)*1000:.0f}ms: {reply[:80]!r}")

        chunk = llm.ChatChunk(
            id=str(uuid.uuid4()),
            delta=llm.ChoiceDelta(role="assistant", content=reply),
        )
        self._event_ch.send_nowait(chunk)


def prewarm(proc: JobProcess) -> None:
    """Load Silero VAD ONCE per process before any session starts.
    Reference repo says this eliminates the slower-than-realtime warnings.
    """
    proc.userdata["vad"] = silero.VAD.load()
    log.info("VAD prewarmed")


async def entrypoint(ctx: JobContext) -> None:
    log.info(f"entrypoint: room={ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    participant = await ctx.wait_for_participant()
    log.info(f"participant joined: {participant.identity}")

    # Canonical config from livekit-examples/agent-starter-python:
    #   STT: Google chirp_2 (chirp_3 is batch-only — won't stream)
    #   TTS: Google Chirp3-HD voice + use_streaming=True + enable_ssml=False
    #        NO gender= param (Google API rejects it for non-NEUTRAL voices)
    #   VAD: Silero from prewarmed proc.userdata
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=google.STT(
            languages="en-US",
            model="chirp_2",
            spoken_punctuation=False,
        ),
        llm=EWLLM(),
        tts=google.TTS(
            language="en-US",
            voice_name="en-US-Chirp3-HD-Aoede",
            use_streaming=True,
            enable_ssml=False,
        ),
    )

    agent = Agent(
        instructions=(
            "You are EW, a senior wholesale-vehicle buyer. Reply in plain "
            "spoken text — no markdown, no bullet points. The EW backend "
            "generates the actual reply; you are the voice."
        ),
    )

    await session.start(agent=agent, room=ctx.room)

    await session.say(
        "EW voice ready. What car are you looking at?",
        allow_interruptions=True,
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        num_idle_processes=1,
    ))
