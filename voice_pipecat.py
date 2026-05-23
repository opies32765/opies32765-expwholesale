"""voice_pipecat.py — Pipecat WebSocket bot for EW Voice.

Matches Anthropic's published cookbook stack:
  - Transport: WebSocket
  - STT: ElevenLabs Scribe v1
  - VAD: Silero v5
  - LLM: route to EW /api/voice/query (which runs Gemini parse + comps + Claude)
  - TTS: ElevenLabs Turbo v2.5

Listens on 0.0.0.0:9003. nginx will reverse-proxy /pipecat/ws → here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

log = logging.getLogger("voice-pipecat")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

EW_QUERY_URL = os.environ.get("EW_QUERY_URL",
                              "http://127.0.0.1:9001/api/voice/query")
ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]

# Default ElevenLabs voice — Adam (deep, calm, professional male)
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")


class EWRouterLLM(FrameProcessor):
    """Custom 'LLM' that routes user transcripts to the existing EW
    backend (/api/voice/query) and emits the reply as TextFrames so the
    TTS service speaks it."""

    def __init__(self) -> None:
        super().__init__()
        self._session_id = f"pc-{uuid.uuid4().hex[:12]}"
        self._turn_index = 0
        self._phone: Optional[str] = None
        self._http: Optional[aiohttp.ClientSession] = None
        log.info(f"EWRouterLLM ready (session={self._session_id})")

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))
        return self._http

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            transcript = (frame.text or "").strip()
            if not transcript:
                return
            self._turn_index += 1
            t0 = time.monotonic()
            log.info(f"EW query: {transcript!r}")
            payload = {
                "transcript": transcript,
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "phone": self._phone,
            }
            reply = ""
            try:
                http = await self._ensure_http()
                async with http.post(EW_QUERY_URL, json=payload) as r:
                    if r.status == 200:
                        data = await r.json()
                        reply = (data.get("reply_text") or "").strip()
                        if data.get("phone_captured"):
                            self._phone = data["phone_captured"]
                    else:
                        body = await r.text()
                        log.error(f"EW {r.status}: {body[:200]}")
                        reply = "Sorry — backend hiccup. Try that again?"
            except Exception:
                log.exception("EW query failed")
                reply = "Sorry — network glitch. Say that again?"

            if not reply:
                reply = "I didn't catch a clean answer — could you rephrase?"
            log.info(f"EW reply {(time.monotonic()-t0)*1000:.0f}ms: {reply[:80]!r}")

            # Frame the reply as an LLM response so TTS picks it up
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame(text=reply))
            await self.push_frame(LLMFullResponseEndFrame())
            return

        # Pass-through everything else
        await self.push_frame(frame, direction)


app = FastAPI()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "voice-pipecat"}


@app.websocket("/pipecat/ws")
async def pipecat_ws(ws: WebSocket):
    await ws.accept()
    log.info("client connected")

    transport = FastAPIWebsocketTransport(
        websocket=ws,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=ProtobufFrameSerializer(),
        ),
    )

    stt = ElevenLabsSTTService(
        api_key=ELEVEN_API_KEY,
        model="scribe_v1",
        language="en",
    )

    llm = EWRouterLLM()

    tts = ElevenLabsTTSService(
        api_key=ELEVEN_API_KEY,
        voice_id=ELEVEN_VOICE_ID,
        model=ELEVEN_MODEL,
        sample_rate=24000,
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        llm,
        tts,
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connect(t, client):
        # Greet briefly so the user hears something within 500ms
        await task.queue_frames([
            TextFrame("EW voice ready. What car are you looking at?"),
        ])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnect(t, client):
        log.info("client disconnected")
        await task.cancel()

    runner = PipelineRunner()
    try:
        await runner.run(task)
    except Exception:
        log.exception("pipeline run error")
    finally:
        log.info("session ended")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9003, log_level="info")
