"""EW Outbound — Twilio Media Streams → Anthropic Haiku + ElevenLabs + MCP tools.

Receives Twilio mu-law 8kHz audio over WebSocket, runs Google STT,
sends transcripts to Haiku (with EW MCP tools), streams ElevenLabs
TTS audio back to Twilio as ulaw_8000 frames.

Bill from Experience Wholesale persona via system_prompt.txt.
Outbound only — Twilio dials the partner, we ride in on the answered call."""
from __future__ import annotations

import asyncio, base64, json, logging, os, time, audioop
from contextlib import asynccontextmanager
from typing import Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

import anthropic
from elevenlabs.client import AsyncElevenLabs
from google.cloud import speech_v1

# ─── env ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
ELEVENLABS_API_KEY   = os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVEN_API_KEY"]
ELEVEN_VOICE_ID      = os.environ.get("ELEVENLABS_VOICE_ID", "T5cu6IU92Krx4mh43osx")
ELEVEN_MODEL         = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ANTHROPIC_MODEL      = os.environ.get("EW_VOICE_MODEL", "claude-haiku-4-5")
MCP_BEARER_TOKEN     = os.environ["MCP_BEARER_TOKEN"]
MCP_TOOL_URL         = "https://experience-wholesale.net/api/ew-voice/tool"
GOOGLE_CREDS         = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                                      "/opt/ew_voice/google_key.json")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDS

# ─── logging ───────────────────────────────────────────────────────────
log = logging.getLogger("ew-outbound")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")

SYSTEM_PROMPT_PATH = "/opt/ew_outbound/system_prompt.txt"
SYSTEM_PROMPT = open(SYSTEM_PROMPT_PATH).read()

# ─── tools ─────────────────────────────────────────────────────────────
# Mirror /opt/ew_voice/agent.py's 10-tool surface, called via the same
# /api/ew-voice/tool proxy with bearer-in-body auth.
TOOLS = [
    {
        "name": "get_vehicle_valuation",
        "description": "Wholesale valuation by YMM. miles=0 or trim='' if unknown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"}, "make": {"type": "string"},
                "model": {"type": "string"}, "miles": {"type": "integer"},
                "trim": {"type": "string"},
            },
            "required": ["year", "make", "model"],
        },
    },
    {
        "name": "get_bid",
        "description": "Full bid card — vAuto, AccuTrade, iPacket MSRP + sticker_text + options, damage_audit, rbook_comps, manheim_transactions, photos. Use for ANY 'bid N' reference.",
        "input_schema": {
            "type": "object",
            "properties": {"bid_id": {"type": "integer"}},
            "required": ["bid_id"],
        },
    },
    {
        "name": "recent_bids",
        "description": "Last N bids on the dashboard. limit=5 default.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "name": "find_best_buyer",
        "description": "Ranked partner-dealer buyer matches for a YMM. trim='' if unknown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "year": {"type": "integer"}, "make": {"type": "string"},
                "model": {"type": "string"}, "trim": {"type": "string"},
            },
            "required": ["caller_name", "year", "make", "model"],
        },
    },
    {
        "name": "lsl_deals_booked",
        "description": "OWNER. LSL deals/gross/PVR by period (yesterday|today|last_7_days|last_30_days|this_month|last_month|this_quarter|last_quarter|ytd|this_year|last_year|all_time).",
        "input_schema": {
            "type": "object",
            "properties": {"caller_name": {"type": "string"}, "period": {"type": "string"}},
            "required": ["caller_name", "period"],
        },
    },
    {
        "name": "lsl_salesperson_stats",
        "description": "OWNER. Salesperson/manager performance by period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "salesperson_name": {"type": "string"},
                "period": {"type": "string"},
            },
            "required": ["caller_name", "salesperson_name", "period"],
        },
    },
    {
        "name": "lsl_inventory_now",
        "description": "OWNER. Cars currently on the EW lot. Empty/0 = all.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "make": {"type": "string"}, "model": {"type": "string"},
                "year": {"type": "integer"},
            },
            "required": ["caller_name"],
        },
    },
    {
        "name": "lsl_customer_history",
        "description": "OWNER. Deal history with a customer/dealer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "customer_name": {"type": "string"},
            },
            "required": ["caller_name", "customer_name"],
        },
    },
    {
        "name": "lsl_query",
        "description": "OWNER. LSL deep query. query_type values: customer_history|dealer_intel|service_requests|payments|appraisal_history|customer_lookup|top_grosses|lookup_sale|recent_bids. target = name/vin/stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "query_type": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["caller_name", "query_type"],
        },
    },
    {
        "name": "send_partner_bid_card",
        "description": "Fire an SMS to the partner with the bid card summary. Call this when the partner expresses ANY interest in the car ('yes I want it', 'looks good', 'send me the details', 'text me'). Records the verbal offer in EW.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bid_id": {"type": "integer"},
                "partner_phone": {"type": "string"},
                "partner_name": {"type": "string"},
                "offer_amount": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["bid_id", "partner_phone"],
        },
    },
    {
        "name": "schedule_callback",
        "description": "Schedule a callback to the same partner after N minutes. Use when the partner says 'call me back in X minutes' or 'try me later'. Dials the SAME bid + partner.",
        "input_schema": {
            "type": "object",
            "properties": {"minutes": {"type": "integer"}},
            "required": ["minutes"],
        },
    },
]


async def call_tool(tool_name: str, args: dict) -> dict:
    payload = {"bearer": MCP_BEARER_TOKEN, "tool": tool_name, "args": args}
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(MCP_TOOL_URL, json=payload) as r:
            return await r.json()


# ─── audio helpers ─────────────────────────────────────────────────────
def ulaw_to_pcm16(ulaw_bytes: bytes) -> bytes:
    """Twilio mu-law 8kHz → LINEAR16 8kHz."""
    return audioop.ulaw2lin(ulaw_bytes, 2)


def pcm16_8k_to_16k(pcm: bytes, state=None) -> tuple[bytes, object]:
    """Google STT wants 16kHz. Upsample 8kHz → 16kHz."""
    return audioop.ratecv(pcm, 2, 1, 8000, 16000, state)


# ─── one call session ──────────────────────────────────────────────────
class CallSession:
    def __init__(self, ws: WebSocket, anthropic_client, eleven_client):
        self.ws = ws
        self.anthropic = anthropic_client
        self.eleven = eleven_client
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.custom_params: dict = {}
        self.messages: list[dict] = []
        self.bot_speaking = False
        self.cancel_speech = asyncio.Event()
        # Audio buffer for incoming user speech (PCM 16kHz)
        self.pcm_buffer = bytearray()
        self.upsample_state = None
        # Simple energy VAD
        self.silence_ms = 0
        self.last_voiced_ts: float = time.monotonic()
        self.utterance_active = False
        self.SILENCE_END_MS = 300   # 800ms of silence ends utterance
        self.MIN_UTTER_MS = 250     # ignore <250ms blips
        self.utter_start_ts: float = 0
        # Google STT short_recognize on each utterance (simpler than streaming)
        self._tag = "?"  # set when start event arrives w/ callSid + partner_name
        self.stt = speech_v1.SpeechClient()
        self.stt_config = speech_v1.RecognitionConfig(
            encoding=speech_v1.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="en-US",
            model="phone_call",
            use_enhanced=True,
        )

    async def send_event(self, evt: dict):
        await self.ws.send_text(json.dumps(evt))

    async def send_media(self, ulaw_payload_b64: str):
        """Send a mu-law audio chunk to Twilio."""
        await self.send_event({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": ulaw_payload_b64},
        })

    async def send_mark(self, name: str):
        await self.send_event({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": name},
        })

    async def clear_audio(self):
        """Tell Twilio to drop any queued audio (barge-in)."""
        await self.send_event({"event": "clear", "streamSid": self.stream_sid})

    # ─── inbound from Twilio ───────────────────────────────────────────
    async def handle_twilio_frame(self, msg: dict):
        evt = msg.get("event")
        if evt == "connected":
            log.info("twilio connected")
        elif evt == "start":
            start = msg.get("start") or {}
            self.stream_sid = start.get("streamSid")
            self.call_sid = start.get("callSid")
            self.custom_params = start.get("customParameters") or {}
            # Short tag: last 6 chars of callSid + partner name (e.g. "[Oscar/77f9d1]")
            self._tag = f"[{self.custom_params.get('partner_name','?')}/{(self.call_sid or '')[-6:]}]"
            log.info(f"{self._tag} start streamSid={self.stream_sid} callSid={self.call_sid} "
                     f"customParams={self.custom_params}")
            # Inject per-call context into the system prompt
            await self.start_call()
        elif evt == "media":
            payload_b64 = msg["media"]["payload"]
            ulaw = base64.b64decode(payload_b64)
            # Buffer the RAW mu-law for STT (no upsampling — Google STT
            # handles mu-law @ 8kHz natively with 'phone_call' model)
            self.pcm_buffer.extend(ulaw)
            # Compute PCM for VAD energy only
            pcm8k = ulaw_to_pcm16(ulaw)
            self._vad_step(pcm8k)
        elif evt == "stop":
            log.info(f"{self._tag} twilio stop")
            try:
                self._save_call_log()
            except Exception as e:
                log.warning(f"call log save failed: {e}")
            raise WebSocketDisconnect(code=1000)
        elif evt == "mark":
            pass  # we don't act on marks coming back

    _rms_sample_count = 0
    _rms_last_log = 0.0

    def _vad_step(self, pcm16k: bytes):
        # Periodic RMS sampling: log max RMS every ~5 seconds when listening
        # so we can diagnose VAD threshold issues post-mortem.
        try:
            rms_now = audioop.rms(pcm16k, 2)
        except Exception:
            rms_now = 0
        CallSession._rms_sample_count = CallSession._rms_sample_count + 1
        if CallSession._rms_sample_count % 50 == 0:  # ~once per second (20ms frames)
            now = time.monotonic()
            if now - CallSession._rms_last_log > 5.0:
                CallSession._rms_last_log = now
                log.info(f"{self._tag} VAD rms_sample={rms_now} (threshold={'250' if self.bot_speaking else '150'}, bot_speaking={self.bot_speaking})")
        """Adaptive-threshold VAD:
           - bot_speaking: high threshold (1800) — real voice triggers,
             echo of bot's own voice does NOT.
           - bot silent: normal threshold (600) — sensitive utterance detection."""
        try:
            rms = audioop.rms(pcm16k, 2)
        except Exception:
            return
        threshold = 250 if self.bot_speaking else 150
        VOICED = rms > threshold
        now = time.monotonic()
        if VOICED:
            if not self.utterance_active:
                self.utterance_active = True
                self.utter_start_ts = now
                log.info(f"{self._tag} VAD utterance START (rms={rms}, threshold={threshold})")
                if self.bot_speaking:
                    log.info(f"{self._tag} barge-in detected (rms={rms}) — cancelling bot speech")
                    self.cancel_speech.set()
                # Trim buffer to last ~200ms of pre-roll so STT gets clean audio
                # 200ms × 16kHz × 2 bytes/sample = 6400 bytes
                if len(self.pcm_buffer) > 1600:
                    del self.pcm_buffer[:-1600]
            self.last_voiced_ts = now
        else:
            if self.utterance_active:
                silence_for = (now - self.last_voiced_ts) * 1000
                if silence_for >= self.SILENCE_END_MS:
                    dur_ms = (now - self.utter_start_ts) * 1000
                    self.utterance_active = False
                    if dur_ms >= self.MIN_UTTER_MS:
                        asyncio.create_task(self._finalize_utterance())
                    else:
                        self.pcm_buffer.clear()

    async def _finalize_utterance(self):
        if not self.pcm_buffer:
            log.info(f"{self._tag} _finalize: empty buffer, skipping")
            return
        audio = bytes(self.pcm_buffer)
        self.pcm_buffer.clear()
        log.info(f"{self._tag} _finalize: sending {len(audio)} bytes to STT")
        try:
            resp = self.stt.recognize(
                config=self.stt_config,
                audio=speech_v1.RecognitionAudio(content=audio),
            )
            text = " ".join(r.alternatives[0].transcript for r in resp.results
                             if r.alternatives).strip()
            if not text:
                log.info(f"{self._tag} STT returned empty result for {len(audio)} byte buffer")
                return
            # DUPLICATE FILTER: ignore the same transcription twice within 1.5s
            now = time.monotonic()
            last_text = getattr(self, '_last_utterance_text', None)
            last_ts   = getattr(self, '_last_utterance_ts', 0)
            if last_text == text and (now - last_ts) < 1.5:
                log.info(f"{self._tag} USER (dup, suppressed): {text!r}")
                return
            self._last_utterance_text = text
            self._last_utterance_ts = now
            log.info(f"{self._tag} USER: {text!r}")
            # CANONICAL INTRO: if Bill hasn't introduced himself yet,
            # do that FIRST — don't let the LLM riff on the user's "hello".
            if not getattr(self, "_intro_done", False):
                self._intro_done = True
                # Record the user's first utterance in history so the
                # subsequent LLM call sees it, but speak the canonical
                # intro NOW.
                self.messages.append({"role": "user", "content": text})
                await self.bot_speak_initial()
                # Don't run the LLM on this turn — Bill just introduced.
                # Next user response will trigger normal LLM loop.
                return
            await self.on_user_message(text)
        except Exception as e:
            log.exception(f"STT failed: {e}")

    # ─── start of call ─────────────────────────────────────────────────
    async def start_call(self):
        cp = self.custom_params
        ctx_lines = []
        if cp.get("bid_id"):       ctx_lines.append(f"bid_id: {cp['bid_id']}")
        if cp.get("partner_name"): ctx_lines.append(f"partner_name: {cp['partner_name']}")
        if cp.get("partner_phone"):ctx_lines.append(f"partner_phone: {cp['partner_phone']}")
        if cp.get("match_score"):  ctx_lines.append(f"match_score: {cp['match_score']}")
        if ctx_lines:
            ctx = "═══ THIS CALL ═══\n" + "\n".join(ctx_lines) + "\n"
            self.full_prompt = ctx + SYSTEM_PROMPT
        else:
            self.full_prompt = SYSTEM_PROMPT
        self.messages = []
        # PREFETCH: kick off get_bid the moment the call connects so the
        # data is warm by the time the partner confirms "yes". Cuts
        # ~4s off the first response after confirmation.
        bid_id = cp.get("bid_id")
        if bid_id:
            asyncio.create_task(self._prefetch_bid(bid_id))
        # Hello-first flow: wait up to 2.5s for the callee to say
        # something. If they do, Bill replies to it with the intro.
        # If they don't (no hello), Bill speaks first anyway.
        asyncio.create_task(self._wait_for_hello_or_timeout())

    async def _wait_for_hello_or_timeout(self):
        # Watch for the first user utterance. If none within 2.5s, Bill
        # speaks proactively.
        intro_sent = False
        elapsed = 0.0
        TICK = 0.1
        TIMEOUT_S = 0.0
        while elapsed < TIMEOUT_S:
            if self.messages:
                # _finalize_utterance has appended a user turn — let the
                # normal LLM loop handle it (Bill will introduce in reply)
                return
            await asyncio.sleep(TICK)
            elapsed += TICK
        # Timeout — no hello detected, speak proactively
        if not intro_sent and not self.messages:
            await self.bot_speak_initial()

    async def _prefetch_bid(self, bid_id):
        """Call get_bid in the background, stash result in system prompt
        so the LLM has it ready for the pitch without a tool round-trip."""
        try:
            t0 = time.monotonic()
            result = await call_tool("get_bid", {"bid_id": int(bid_id)})
            elapsed = (time.monotonic() - t0) * 1000
            log.info(f"{self._tag} prefetch get_bid({bid_id}) in {elapsed:.0f}ms")
            # Slim the result for prompt injection — keep only the
            # voice-relevant fields, drop screenshots/AI bookkeeping.
            # Build a structured WHOLESALE PITCH BRIEFING from the bid data.
            yr = result.get("year") or ""
            mk = result.get("make") or ""
            md = result.get("model") or ""
            tr = result.get("trim") or ""
            mi = result.get("mileage") or 0
            ip = result.get("ipacket") or {}
            ext = ip.get("exterior_color") or result.get("color") or ""
            interior = ip.get("interior_color") or ""
            msrp = ip.get("total_msrp")
            sticker = ip.get("sticker_text") or ""
            mmr = result.get("vauto_mmr")
            rbook = result.get("vauto_rbook")
            our_target = result.get("ai_price") or result.get("asking_price") or rbook
            guaranteed = result.get("guaranteed_offer")
            damage = result.get("damage_audit") or {}
            damage_flags = damage.get("flags") or []

            # Extract 3-5 headline options from sticker_text (lines that
            # name an actual package/feature, skip STD/standard items)
            options = []
            if sticker:
                for line in sticker.split("\n"):
                    line = line.strip()
                    if not line or len(line) < 5:
                        continue
                    # Skip "STD..." standard-equipment codes
                    if line.lstrip().upper().startswith("STD"):
                        continue
                    # Strip code prefix like "C2U-" or "PA1 -"
                    parts = line.split("-", 1)
                    if len(parts) == 2 and len(parts[0].strip()) <= 6:
                        line = parts[1].strip()
                    options.append(line)
                    if len(options) >= 6: break

            # Compute positioning vs market — explicit deltas
            positioning = []
            if our_target and rbook:
                delta = float(rbook) - float(our_target)
                if delta > 500:
                    positioning.append(
                        f"BELOW rBook by ${int(abs(delta)):,} "
                        f"(rBook ${int(rbook):,} vs target ${int(our_target):,}) — strong spot for partner")
                elif delta < -500:
                    positioning.append(
                        f"ABOVE rBook by ${int(abs(delta)):,} — tight, will need to justify")
                else:
                    positioning.append(f"AT rBook (${int(rbook):,})")
            if our_target and mmr:
                delta = float(our_target) - float(mmr)
                if delta > 500:
                    positioning.append(
                        f"${int(delta):,} above MMR — typical wholesale spread")
                elif delta < -500:
                    positioning.append(
                        f"BELOW MMR by ${int(abs(delta)):,} — unusual, double-check")
                else:
                    positioning.append("at MMR")

            import json as _json
            briefing = {
                "vehicle":   f"{yr} {mk} {md} {tr}".strip(),
                "miles":     int(mi) if mi else None,
                "exterior":  ext,
                "interior":  interior,
                "msrp_new":  int(msrp) if msrp else None,
                "options":   options,
                "damage":    "clean" if not damage_flags else damage_flags,
                "guaranteed_offer": int(guaranteed) if guaranteed else None,
                "mmr":       int(mmr) if mmr else None,
                "rbook":     int(rbook) if rbook else None,
                "our_target":int(our_target) if our_target else None,
                "positioning": positioning,
            }
            self.prefetched_bid = briefing

            self.full_prompt += (
                f"\n\n═══ CALL BRIEFING — USE THESE EXACT FIELDS ═══\n"
                f"This is your script. Speak from this. Do not call get_bid "
                f"unless asked about something missing here.\n"
                + _json.dumps(briefing, default=str, indent=2)
                + "\n\n═══ HOW TO PITCH IT — IN ORDER ═══\n"
                "1. Identify the car: '[year] [make] [model] [trim]'\n"
                "2. Miles + colors: '[N] miles, [exterior] over [interior]'\n"
                "3. New MSRP context (if msrp_new is set): 'New MSRP was around $[X]'\n"
                "4. Headline options (pick 3-4 from options[], say naturally): 'Loaded — exec package, pano roof, ventilated seats'\n"
                "5. Damage (if not clean, mention; if clean, skip)\n"
                "6. THE NUMBER: 'We're at $[our_target]'\n"
                "7. **POSITIONING — say this verbatim** if positioning[] contains a BELOW: 'That's $X below rBook, so it's a solid spot for your lot.'\n"
                "   If positioning[] contains BELOW MMR (unusual): mention that too.\n"
                "   If ABOVE rBook: be honest, don't pretend it's cheap.\n"
                "8. Ask: 'Could this work for your lot?' or 'Where you at on this?'\n"
                "\nDO NOT lead with 'first look before it hits the floor' — that's retail-speak. You're calling DEALERS who buy wholesale. Lead with the SPECS + POSITIONING."
            )
        except Exception as e:
            log.warning(f"{self._tag} prefetch get_bid failed: {e}")

    async def bot_speak_initial(self):
        self._intro_done = True
        partner = self.custom_params.get("partner_name", "")
        first_name = partner.split()[0] if partner else "there"
        # Hardcoded opener — Bill always says this same line first.
        opener = f"Hi {first_name}, this is Bill from Experience Wholesale. Do you have a moment?"
        log.info(f"{self._tag} BOT (canonical intro): {opener!r}")
        self.messages.append({"role": "assistant", "content": opener})
        await self.speak(opener)

    # ─── conversation loop ─────────────────────────────────────────────
    async def on_user_message(self, text: str):
        self.messages.append({"role": "user", "content": text})
        await self.run_llm_turn()

    async def run_llm_turn(self):
        for _ in range(5):  # max 5 tool-use cycles per turn
            try:
                resp = await asyncio.to_thread(
                    self.anthropic.messages.create,
                    model=ANTHROPIC_MODEL,
                    max_tokens=250,
                    system=self.full_prompt,
                    tools=TOOLS,
                    messages=self.messages,
                )
            except Exception as e:
                log.exception(f"anthropic err: {e}")
                await self.speak("Sorry, I'm having a connection issue. Let me text you and follow up.")
                return
            if resp.stop_reason == "tool_use":
                # Collect all tool_use blocks
                assistant_blocks = []
                tool_results = []
                for block in resp.content:
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        assistant_blocks.append({"type": "text", "text": block.text})
                    elif btype == "tool_use":
                        assistant_blocks.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input,
                        })
                        log.info(f"{self._tag} TOOL_CALL: {block.name}({json.dumps(block.input)[:120]})")
                        try:
                            if block.name == "schedule_callback":
                                result = await self._handle_schedule_callback(
                                    int(block.input.get("minutes", 5)))
                            else:
                                result = await call_tool(block.name, block.input)
                        except Exception as e:
                            result = {"error": f"{type(e).__name__}: {e}"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str)[:8000],
                        })
                self.messages.append({"role": "assistant", "content": assistant_blocks})
                self.messages.append({"role": "user", "content": tool_results})
                continue  # loop back to LLM
            # Final text response
            reply = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    reply += block.text
            reply = reply.strip()
            log.info(f"{self._tag} BOT: {reply!r}")
            if reply:
                self.messages.append({"role": "assistant", "content": reply})
                await self.speak(reply)
            return

    # ─── speak via ElevenLabs ──────────────────────────────────────────
    @staticmethod
    def _strip_stage_directions(text: str) -> str:
        """Remove parenthetical / asterisk / bracket stage directions
        before passing text to TTS. The LLM sometimes writes them; we
        absolutely never want ElevenLabs to voice 'pause' out loud."""
        import re as _re
        # Remove (anything in parens)
        text = _re.sub(r"\([^)]*\)", "", text)
        # Remove *anything in asterisks*
        text = _re.sub(r"\*[^*]*\*", "", text)
        # Remove [anything in brackets]
        text = _re.sub(r"\[[^\]]*\]", "", text)
        # Collapse double whitespace and leading/trailing
        text = _re.sub(r"\s+", " ", text).strip()
        return text

    async def _handle_schedule_callback(self, minutes: int) -> dict:
        """Schedule a Twilio redial after N minutes via SSH to C1 dialer."""
        cp = self.custom_params
        bid_id = cp.get("bid_id") or ""
        partner_name = cp.get("partner_name") or "there"
        partner_phone = cp.get("partner_phone") or ""
        score = cp.get("match_score") or "90"
        if not partner_phone:
            return {"ok": False, "error": "no partner_phone in session"}
        minutes = max(1, min(60, int(minutes)))
        async def _do_callback():
            await asyncio.sleep(minutes * 60)
            import subprocess
            log.info(f"firing scheduled callback to {partner_phone} for bid {bid_id}")
            cmd = (
                f"eval $(systemctl show expwholesale -p Environment --value | "
                f"tr \' \' \'\\n\' | grep -E \'^TWILIO_\' | sed \'s/^/export /\') && "
                f"/opt/expwholesale/venv/bin/python /opt/expwholesale/ew_dialer.py "
                f"--bid {bid_id} --to {partner_phone} "
                f"--partner '{partner_name}' --score {score}"
            )
            try:
                subprocess.Popen(["ssh", "root@62.146.226.100", cmd])
            except Exception as e:
                log.exception(f"callback dial failed: {e}")
        asyncio.create_task(_do_callback())
        log.info(f"scheduled callback in {minutes} min to {partner_phone}")
        return {"ok": True, "minutes": minutes, "partner_phone": partner_phone}


    def _save_call_log(self):
        """Persist this call's transcript + outcome to Postgres for the
        Coach agent to review later."""
        import psycopg2, psycopg2.extras, json as _json, os as _os
        db_url = _os.environ.get(
            "EW_DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@62.146.226.100:5433/expwholesale",
        )
        # Compose transcript from self.messages (user + assistant turns)
        turns = []
        for m in self.messages:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, str):
                turns.append({"role": role, "text": content})
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        if blk.get("type") == "text":
                            turns.append({"role": role, "text": blk.get("text")})
                        elif blk.get("type") == "tool_use":
                            turns.append({"role": role, "tool_call": blk.get("name"),
                                          "args": blk.get("input")})
                        elif blk.get("type") == "tool_result":
                            turns.append({"role": role, "tool_result": (blk.get("content") or "")[:500]})
        try:
            with psycopg2.connect(db_url, connect_timeout=5) as c, c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS outbound_call_log (
                      id              SERIAL PRIMARY KEY,
                      call_sid        TEXT,
                      stream_sid      TEXT,
                      partner_name    TEXT,
                      partner_phone   TEXT,
                      bid_id          INTEGER,
                      match_score     INTEGER,
                      started_at      TIMESTAMP,
                      ended_at        TIMESTAMP DEFAULT NOW(),
                      n_user_turns    INTEGER,
                      n_bot_turns     INTEGER,
                      transcript      JSONB,
                      coach_reviewed  BOOLEAN DEFAULT FALSE,
                      created_at      TIMESTAMP DEFAULT NOW()
                    )
                """)
                cp = self.custom_params
                n_user = sum(1 for t in turns if t.get("role") == "user" and t.get("text"))
                n_bot  = sum(1 for t in turns if t.get("role") == "assistant" and t.get("text"))
                cur.execute("""
                    INSERT INTO outbound_call_log
                      (call_sid, stream_sid, partner_name, partner_phone, bid_id,
                       match_score, n_user_turns, n_bot_turns, transcript)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    self.call_sid, self.stream_sid,
                    cp.get("partner_name"), cp.get("partner_phone"),
                    int(cp.get("bid_id") or 0) or None,
                    int(cp.get("match_score") or 0) or None,
                    n_user, n_bot, _json.dumps(turns, default=str),
                ))
                row_id = cur.fetchone()[0]
                c.commit()
                log.info(f"{self._tag} call_log saved id={row_id} turns={len(turns)}")
        except Exception as e:
            log.warning(f"{self._tag} call_log insert err: {e}")

    async def speak(self, text: str):
        # Strip any stage directions so TTS only voices real dialogue
        text = self._strip_stage_directions(text)
        if not text:
            log.info(f"{self._tag} speak: nothing to say after strip — silent")
            return
        self.bot_speaking = True
        self.cancel_speech.clear()
        try:
            audio_stream = self.eleven.text_to_speech.stream(
                voice_id=ELEVEN_VOICE_ID,
                text=text,
                model_id=ELEVEN_MODEL,
                output_format="ulaw_8000",
                optimize_streaming_latency=4,
            )
            CHUNK = 320   # 20ms of mu-law 8kHz = 160 bytes per frame, send 2 per outer chunk
            buf = bytearray()
            async for chunk in audio_stream:
                if self.cancel_speech.is_set():
                    log.info(f"{self._tag} speech cancelled mid-stream")
                    await self.clear_audio()
                    break
                if not chunk:
                    continue
                buf.extend(chunk)
                # Emit in 160-byte (20ms) frames to keep Twilio happy
                while len(buf) >= 160:
                    frame = bytes(buf[:160])
                    del buf[:160]
                    await self.send_media(base64.b64encode(frame).decode())
            if buf and not self.cancel_speech.is_set():
                # Flush remainder
                await self.send_media(base64.b64encode(bytes(buf)).decode())
        except Exception as e:
            log.exception(f"speak err: {e}")
        finally:
            self.bot_speaking = False


# ─── FastAPI app ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("ew-outbound-stream startup")
    yield
    log.info("ew-outbound-stream shutdown")

app = FastAPI(lifespan=lifespan)
_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_eleven = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket):
    await ws.accept()
    session = CallSession(ws, _anthropic, _eleven)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            await session.handle_twilio_frame(msg)
    except WebSocketDisconnect:
        log.info("websocket disconnected")
    except Exception as e:
        log.exception(f"stream loop err: {e}")
