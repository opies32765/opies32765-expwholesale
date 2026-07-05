"""EW Rep Want-List — INBOUND Twilio Media Streams → Bill (Haiku) + ElevenLabs.

REP_WANTLIST_2026_07_04. Cloned from outbound_stream_server.py (proven
Twilio mu-law <-> STT <-> Haiku <-> ElevenLabs machinery), flipped inbound:

  POST /rep-voice/answer   Twilio voice webhook -> TwiML <Connect><Stream>
  WS   /rep-voice/stream   the call itself

A field rep calls the main EW number, tells Bill what their customer is
hunting for, Bill registers it in bid_alerts (the bill_watcher daemon
already texts notify_phone when a matching bid lands). Caller ID = the
alert's notify_phone; Bill greets repeat callers with their open requests.

Tools are LOCAL (direct Postgres) — want-list only, no EW data exposure.
Isolated service (ew-rep-voice.service :5211); never touches the SMS
webhook, app.py, or the running voice services.
"""
from __future__ import annotations

import asyncio, base64, json, logging, os, time, audioop, io, wave
from contextlib import asynccontextmanager
from typing import Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, Response

from elevenlabs.client import AsyncElevenLabs

# ─── env ───────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVEN_API_KEY"]
ELEVEN_VOICE_ID    = os.environ.get("ELEVENLABS_VOICE_ID", "T5cu6IU92Krx4mh43osx")  # Bill
ELEVEN_MODEL       = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
# Brain = the operator's local 9B (vLLM, OpenAI-compatible) — NOT an external LLM.
EW_BRAIN_URL       = os.environ.get("EW_BRAIN_URL", "https://brain.experience-wholesale.net")
EW_BRAIN_KEY       = os.environ.get("EW_BRAIN_KEY", "")
EW_BRAIN_MODEL     = os.environ.get("EW_BRAIN_MODEL", "ew-brain")
DB_URL             = os.environ.get("DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
GOOGLE_CREDS       = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
    "/opt/expwholesale/google_vision_key.json")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDS
PUBLIC_WS_URL      = os.environ.get("REP_VOICE_WS_URL",
    "wss://experience-wholesale.net/rep-voice/stream")
PORT               = int(os.environ.get("REP_VOICE_PORT", "5211"))

log = logging.getLogger("ew-rep-voice")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")

SYSTEM_PROMPT = open("/opt/expwholesale/rep_wantlist_prompt.txt").read()

# ─── STT: Google phone_call model primary, ElevenLabs scribe fallback ──
_gstt = None
try:
    from google.cloud import speech_v1
    _gstt = speech_v1.SpeechClient()
    _gstt_config = speech_v1.RecognitionConfig(
        encoding=speech_v1.RecognitionConfig.AudioEncoding.MULAW,
        sample_rate_hertz=8000, language_code="en-US",
        model="phone_call", use_enhanced=True)
    log.info("STT: google speech_v1 ready")
except Exception as e:
    log.warning(f"STT: google init failed ({e}); will use ElevenLabs scribe")

_eleven = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)


async def brain_chat(system: str, history: list[dict], tag: str) -> dict:
    """One 9B turn. Returns the parsed {"say":..., "action":...} dict.
    temp 0, thinking OFF (ew-brain never emits JSON with thinking on),
    browser UA (Cloudflare 403s non-browser agents)."""
    payload = {
        "model": EW_BRAIN_MODEL,
        "messages": [{"role": "system", "content": system}] + history,
        "temperature": 0,
        "max_tokens": 300,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {
        "Authorization": f"Bearer {EW_BRAIN_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(EW_BRAIN_URL.rstrip("/") + "/v1/chat/completions",
                          json=payload, headers=headers) as r:
            data = await r.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    # Defensive JSON extraction: strip fences, take outermost {...}
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    def _prose_fallback():
        # QA-call fix 2026-07-04: the 9B sometimes answers in plain prose.
        # The prose is usually a perfectly good reply — SPEAK it instead of
        # a canned "say that again" (which read as Bill being touchy).
        clean = " ".join(t.split())[:400]
        log.warning(f"{tag} brain returned non-JSON, speaking prose: {text[:200]!r}")
        return {"say": clean or "Sorry, say that again?", "action": None}
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        return _prose_fallback()
    try:
        out = json.loads(t[i:j + 1])
        if not isinstance(out, dict):
            raise ValueError("not a dict")
        return {"say": str(out.get("say") or ""), "action": out.get("action")}
    except Exception:
        return _prose_fallback()


def _mulaw_to_wav16k(ulaw: bytes) -> bytes:
    pcm8 = audioop.ulaw2lin(ulaw, 2)
    pcm16, _ = audioop.ratecv(pcm8, 2, 1, 8000, 16000, None)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm16)
    return buf.getvalue()


async def stt_transcribe(ulaw: bytes, tag: str) -> str:
    if _gstt is not None:
        try:
            resp = await asyncio.to_thread(
                _gstt.recognize, config=_gstt_config,
                audio=speech_v1.RecognitionAudio(content=ulaw))
            return " ".join(r.alternatives[0].transcript for r in resp.results
                            if r.alternatives).strip()
        except Exception as e:
            log.warning(f"{tag} google STT failed ({e}); falling back to scribe")
    try:
        r = await _eleven.speech_to_text.convert(
            file=io.BytesIO(_mulaw_to_wav16k(ulaw)), model_id="scribe_v1")
        return (getattr(r, "text", "") or "").strip()
    except Exception as e:
        log.exception(f"{tag} scribe STT failed: {e}")
        return ""


# ─── want-list DB tools (local, want-only — no EW data exposure) ───────
def _db():
    import psycopg2
    return psycopg2.connect(DB_URL)


def _digits(s: str) -> str:
    d = "".join(ch for ch in (s or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def db_open_wants(phone_digits: str) -> list[dict]:
    import psycopg2.extras
    with _db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, make, model, trim_contains, year_min, year_max,"
            " price_max, label, match_count, created_at::date::text AS created"
            " FROM bid_alerts WHERE phone_digits=%s AND active=TRUE"
            " ORDER BY created_at DESC LIMIT 10", (phone_digits,))
        return [dict(r) for r in cur.fetchall()]


def db_add_want(phone_digits: str, make: str, model: str,
                year_min=None, year_max=None, trim_contains=None,
                price_max=None, label=None) -> dict:
    if not (make or "").strip() or not (model or "").strip():
        return {"ok": False, "error": "make and model are both required"}
    if len(phone_digits) != 10:
        return {"ok": False, "error": "no valid caller number on this call"}
    # bill_watcher matches lower(make) equality + loose model contains —
    # store the same shapes create_bid_alert does.
    mk = make.strip().lower()
    md = model.strip()
    with _db() as c, c.cursor() as cur:
        # dedupe: identical active want for this phone -> return existing
        cur.execute(
            "SELECT id FROM bid_alerts WHERE phone_digits=%s AND active=TRUE"
            " AND make=%s AND lower(model)=lower(%s)"
            " AND COALESCE(year_min,-1)=COALESCE(%s,-1)"
            " AND COALESCE(year_max,-1)=COALESCE(%s,-1)",
            (phone_digits, mk, md, year_min, year_max))
        row = cur.fetchone()
        if row:
            return {"ok": True, "alert_id": row[0], "note": "already on the list"}
        cur.execute(
            "INSERT INTO bid_alerts (created_by, notify_phone, phone_digits,"
            " make, model, trim_contains, year_min, year_max, price_max,"
            " label, active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)"
            " RETURNING id",
            ("rep:" + phone_digits, "+1" + phone_digits, phone_digits, mk, md,
             (trim_contains or None), year_min, year_max, price_max,
             (label or None)))
        aid = cur.fetchone()[0]
    return {"ok": True, "alert_id": aid}


def db_cancel_want(phone_digits: str, alert_id: int) -> dict:
    with _db() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE bid_alerts SET active=FALSE, updated_at=NOW()"
            " WHERE id=%s AND phone_digits=%s AND active=TRUE RETURNING id",
            (alert_id, phone_digits))
        row = cur.fetchone()
    return {"ok": bool(row), "cancelled": alert_id if row else None,
            **({} if row else {"error": "no active request with that id for this caller"})}


def _speak_wants(wants: list[dict]) -> str:
    """Deterministic natural-language summary of open requests."""
    if not wants:
        return "You've got nothing open right now. What's your customer looking for?"
    parts = []
    for w in wants:
        yr = ""
        ymin, ymax = w.get("year_min"), w.get("year_max")
        if ymin and ymax and ymin != ymax:
            yr = f"{ymin} to {ymax} "
        elif ymin and ymax:
            yr = f"{ymin} "
        elif ymin:
            yr = f"{ymin} and newer "
        desc = w.get("label") or f"{yr}{(w.get('make') or '').title()} {w.get('model') or ''}".strip()
        parts.append(desc)
    lead = f"You've got {len(parts)} open: " if len(parts) > 1 else "You've got one open: "
    return lead + "; ".join(parts) + ". Want to add or drop anything?"


# ─── one call session (machinery cloned from outbound_stream_server) ───
class RepCall:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.caller_digits = ""
        self.messages: list[dict] = []
        self.full_prompt = SYSTEM_PROMPT
        self.bot_speaking = False
        self.cancel_speech = asyncio.Event()
        self.pcm_buffer = bytearray()
        self.silence_ms = 0
        self.last_voiced_ts = time.monotonic()
        self.utterance_active = False
        # QA-call tuning 2026-07-04: 300ms chopped sentences in half and Bill
        # felt "touchy" — a natural mid-sentence pause is ~400-600ms on PSTN.
        self.SILENCE_END_MS = 700
        self.MIN_UTTER_MS = 350
        self.utter_start_ts = 0.0
        self._tag = "?"
        # QA-call fix 2026-07-04 ("keeps repeating"): serialize brain turns,
        # coalesce fragmented utterances, suppress repeated identical actions.
        self._turn_lock = asyncio.Lock()
        self._debounce_task: Optional[asyncio.Task] = None
        self._done_actions: set = set()

    async def send_event(self, evt: dict):
        await self.ws.send_text(json.dumps(evt))

    async def send_media(self, b64: str):
        await self.send_event({"event": "media", "streamSid": self.stream_sid,
                               "media": {"payload": b64}})

    async def clear_audio(self):
        await self.send_event({"event": "clear", "streamSid": self.stream_sid})

    async def handle_twilio_frame(self, msg: dict):
        evt = msg.get("event")
        if evt == "start":
            start = msg.get("start") or {}
            self.stream_sid = start.get("streamSid")
            self.call_sid = start.get("callSid")
            cp = start.get("customParameters") or {}
            self.caller_digits = _digits(cp.get("caller", ""))
            self._tag = f"[rep:{self.caller_digits or '?'}/{(self.call_sid or '')[-6:]}]"
            log.info(f"{self._tag} start streamSid={self.stream_sid}")
            await self.start_call()
        elif evt == "media":
            ulaw = base64.b64decode(msg["media"]["payload"])
            self.pcm_buffer.extend(ulaw)
            self._vad_step(audioop.ulaw2lin(ulaw, 2))
        elif evt == "stop":
            log.info(f"{self._tag} twilio stop")
            try:
                self._save_call_log()
            except Exception as e:
                log.warning(f"call log save failed: {e}")
            raise WebSocketDisconnect(code=1000)

    def _vad_step(self, pcm: bytes):
        try:
            rms = audioop.rms(pcm, 2)
        except Exception:
            return
        threshold = 550 if self.bot_speaking else 300
        now = time.monotonic()
        if rms > threshold:
            if not self.utterance_active:
                self.utterance_active = True
                self.utter_start_ts = now
                if self.bot_speaking:
                    log.info(f"{self._tag} barge-in (rms={rms})")
                    self.cancel_speech.set()
                if len(self.pcm_buffer) > 1600:
                    del self.pcm_buffer[:-1600]
            self.last_voiced_ts = now
        else:
            if self.utterance_active:
                if (now - self.last_voiced_ts) * 1000 >= self.SILENCE_END_MS:
                    dur = (now - self.utter_start_ts) * 1000
                    self.utterance_active = False
                    if dur >= self.MIN_UTTER_MS:
                        asyncio.create_task(self._finalize_utterance())
                    else:
                        self.pcm_buffer.clear()

    async def _finalize_utterance(self):
        if not self.pcm_buffer:
            return
        audio = bytes(self.pcm_buffer)
        self.pcm_buffer.clear()
        text = await stt_transcribe(audio, self._tag)
        if not text:
            return
        now = time.monotonic()
        if getattr(self, "_last_text", None) == text and \
           now - getattr(self, "_last_ts", 0) < 1.5:
            return
        self._last_text, self._last_ts = text, now
        log.info(f"{self._tag} USER: {text!r}")
        self.messages.append({"role": "user", "content": text})
        # Debounce: a follow-on fragment within 700ms joins this turn instead
        # of spawning its own — then turns run one at a time under the lock.
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_turn())

    async def _debounced_turn(self):
        try:
            await asyncio.sleep(0.7)
        except asyncio.CancelledError:
            return
        async with self._turn_lock:
            # If a queued earlier turn already answered everything (last word
            # is Bill's and no new user text since), don't answer again.
            if self.messages and self.messages[-1]["role"] == "assistant":
                return
            await self.run_llm_turn()

    # ─── start of call: load open wants, Bill speaks FIRST ─────────────
    def _refresh_ctx(self) -> list[dict]:
        """(Re)build the THIS CALL context block from the DB. Returns wants."""
        wants = []
        if self.caller_digits:
            try:
                wants = db_open_wants(self.caller_digits)
            except Exception as e:
                log.warning(f"{self._tag} open-wants lookup failed: {e}")
        ctx = [f"caller_phone_digits: {self.caller_digits or 'UNKNOWN'}"]
        if wants:
            ctx.append("OPEN REQUESTS for this caller (id | want | since):")
            for w in wants:
                yr = ""
                if w.get("year_min") and w.get("year_max"):
                    yr = f"{w['year_min']}-{w['year_max']} " if w["year_min"] != w["year_max"] else f"{w['year_min']} "
                elif w.get("year_min"):
                    yr = f"{w['year_min']}+ "
                desc = w.get("label") or f"{yr}{(w['make'] or '').title()} {w['model'] or ''}".strip()
                ctx.append(f"  #{w['id']} | {desc} | {w['created']}")
        else:
            ctx.append("OPEN REQUESTS: none")
        self.full_prompt = "═══ THIS CALL ═══\n" + "\n".join(ctx) + "\n\n" + SYSTEM_PROMPT
        return wants

    async def start_call(self):
        wants = await asyncio.to_thread(self._refresh_ctx)
        # Deterministic opener — Python picks it, not the LLM.
        if wants:
            w = wants[0]
            first = (w.get("label") or f"{(w['make'] or '').title()} {w['model'] or ''}").strip()
            more = f" and {len(wants)-1} more" if len(wants) > 1 else ""
            opener = (f"Hey, it's Bill at Experience Wholesale. Still watching for that "
                      f"{first}{more} for you. What can I do for you?")
        else:
            opener = ("This is Bill at Experience Wholesale. What's your customer looking for?")
        self.messages.append({"role": "assistant", "content": opener})
        log.info(f"{self._tag} BOT (opener): {opener!r}")
        await self.speak(opener)

    # ─── 9B turn: strict JSON in/out, deterministic confirmations ──────
    async def run_llm_turn(self):
        try:
            out = await brain_chat(self.full_prompt, self.messages, self._tag)
        except Exception as e:
            log.exception(f"brain err: {e}")
            await self.speak("Sorry, having a connection issue. Text this same number and we'll take care of you.")
            return
        say, action = out.get("say", ""), out.get("action")
        actions = action if isinstance(action, list) else ([action] if isinstance(action, dict) else [])
        if actions:
            says = []
            for act in actions[:4]:
                if not isinstance(act, dict):
                    continue
                name = str(act.get("name") or "")
                args = act.get("args") or {}
                sig = json.dumps([name, args], sort_keys=True, default=str)
                if name == "add_want" and sig in self._done_actions:
                    # sticky-model guard: same add re-emitted after it was
                    # already confirmed this call — don't repeat the pitch.
                    says.append("You're all set on that one.")
                    continue
                self._done_actions.add(sig)
                log.info(f"{self._tag} ACTION: {name}({json.dumps(args, default=str)[:150]})")
                try:
                    says.append(await asyncio.to_thread(self._run_action, name, args))
                    await asyncio.to_thread(self._refresh_ctx)  # keep OPEN REQUESTS (ids) current
                except Exception as e:
                    log.exception(f"{self._tag} action failed: {e}")
                    says.append("Hit a snag logging that — give me the make and model one more time?")
            # Combined confirmations: drop mid-sentence "Anything else?" tails
            says = [s.replace(" Anything else?", "") for s in says[:-1]] + says[-1:] if says else says
            say = " ".join(s for s in says if s)
        if say:
            log.info(f"{self._tag} BOT: {say!r}")
            self.messages.append({"role": "assistant", "content": say})
            await self.speak(say)

    def _run_action(self, name: str, args: dict) -> str:
        """Execute a want-list action; return the DETERMINISTIC line Bill
        speaks (Python words the outcome — the 9B never voices data)."""
        if name == "add_want":
            r = db_add_want(self.caller_digits,
                            str(args.get("make") or ""), str(args.get("model") or ""),
                            args.get("year_min"), args.get("year_max"),
                            args.get("trim_contains"), args.get("price_max"),
                            args.get("label"))
            if r.get("ok") and r.get("note"):
                return "You're already on the list for that one — still watching. Anything else?"
            if r.get("ok"):
                return ("You're on the list. The second a matching car hits our board, "
                        "you'll get a text at this number. Anything else?")
            return "I didn't catch enough to log that — give me the make and model again?"
        if name == "list_wants":
            return _speak_wants(db_open_wants(self.caller_digits))
        if name == "cancel_want":
            # QA-call fix 2026-07-04: "cancel all those" must cancel ALL —
            # accept a single id or a list of ids.
            raw = args.get("alert_id")
            ids = [int(x) for x in (raw if isinstance(raw, list) else [raw]) if x]
            done = sum(1 for i in ids
                       if db_cancel_want(self.caller_digits, i).get("ok"))
            if done == 0:
                return "I don't see that on your list. Want me to read back what you've got open?"
            if done == 1:
                return "Done — dropped it. Anything else?"
            return f"Done — dropped all {done} of them. Anything else?"
        return "Sorry, say that again?"

    # ─── TTS ────────────────────────────────────────────────────────────
    @staticmethod
    def _tts_normalize(text: str) -> str:
        """NUMSPEECH_2026_07_04: convert digits to natural car-talk words so
        ElevenLabs doesn't read '2024-2025 Ferrari 296' like a serial number."""
        import re as _re
        ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
                "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
                "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
        TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty",
                "seventy", "eighty", "ninety"]

        def two(n):  # 0-99
            if n < 20:
                return ONES[n]
            t, r = divmod(n, 10)
            return TENS[t] + ("-" + ONES[r] if r else "")

        def three(n):  # 0-999 formal
            h, r = divmod(n, 100)
            if h and r:
                return ONES[h] + " hundred " + two(r)
            if h:
                return ONES[h] + " hundred"
            return two(n)

        def words(n):  # 0-999,999 formal
            th, r = divmod(n, 1000)
            out = []
            if th:
                out.append(three(th) + " thousand")
            if r:
                out.append(three(r))
            return " ".join(out) or "zero"

        def car3(n):  # 100-999 car-style: 296 -> two ninety-six, 911 -> nine eleven
            h, r = divmod(n, 100)
            if r == 0:
                return ONES[h] + " hundred"
            if r < 10:
                return ONES[h] + " oh " + ONES[r]
            return ONES[h] + " " + two(r)

        def year(n):  # 1900-2099
            a, b = divmod(n, 100)
            if b == 0:
                return two(a) + " hundred"
            if b < 10:
                return two(a) + " oh " + ONES[b]
            return two(a) + " " + two(b)

        # $90,000 / $90000 -> ninety thousand dollars
        text = _re.sub(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,6})",
                       lambda m: words(int(m.group(1).replace(",", ""))) + " dollars", text)
        # 90k / 90K -> ninety thousand
        text = _re.sub(r"\b(\d{1,3})[kK]\b",
                       lambda m: two(int(m.group(1))) + " thousand", text)
        # year range 2024-2025 / 2024 to 2025
        text = _re.sub(r"\b(19|20)(\d{2})\s*[-–]\s*(19|20)(\d{2})\b",
                       lambda m: year(int(m.group(1) + m.group(2))) + " to "
                       + year(int(m.group(3) + m.group(4))), text)
        # 2022+ -> twenty twenty-two and newer
        text = _re.sub(r"\b(19|20)(\d{2})\s*\+",
                       lambda m: year(int(m.group(1) + m.group(2))) + " and newer", text)
        # bare years
        text = _re.sub(r"\b(19|20)(\d{2})\b",
                       lambda m: year(int(m.group(1) + m.group(2))), text)
        # 31,000 / 31000 (miles, money) -> thirty-one thousand
        text = _re.sub(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,6}\b",
                       lambda m: words(int(m.group(0).replace(",", ""))), text)
        # remaining 3-digit numbers = model numbers -> car-style (two ninety-six)
        text = _re.sub(r"\b(\d{3})\b", lambda m: car3(int(m.group(1))), text)
        # em-dashes read better as commas
        return text.replace("—", ",").replace("–", ",")

    @staticmethod
    def _strip_stage_directions(text: str) -> str:
        import re as _re
        text = _re.sub(r"\([^)]*\)", "", text)
        text = _re.sub(r"\*[^*]*\*", "", text)
        text = _re.sub(r"\[[^\]]*\]", "", text)
        return _re.sub(r"\s+", " ", text).strip()

    async def speak(self, text: str):
        text = self._strip_stage_directions(text)
        text = self._tts_normalize(text)
        if not text:
            return
        self.bot_speaking = True
        self.cancel_speech.clear()
        try:
            stream = _eleven.text_to_speech.stream(
                voice_id=ELEVEN_VOICE_ID, text=text, model_id=ELEVEN_MODEL,
                output_format="ulaw_8000", optimize_streaming_latency=4)
            buf = bytearray()
            async for chunk in stream:
                if self.cancel_speech.is_set():
                    await self.clear_audio()
                    break
                if not chunk:
                    continue
                buf.extend(chunk)
                while len(buf) >= 160:
                    frame = bytes(buf[:160]); del buf[:160]
                    await self.send_media(base64.b64encode(frame).decode())
            if buf and not self.cancel_speech.is_set():
                await self.send_media(base64.b64encode(bytes(buf)).decode())
        except Exception as e:
            log.exception(f"speak err: {e}")
        finally:
            self.bot_speaking = False

    def _save_call_log(self):
        import psycopg2, json as _json
        turns = []
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, str):
                turns.append({"role": m.get("role"), "text": c})
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        turns.append({"role": m.get("role"), "text": blk.get("text")})
                    elif isinstance(blk, dict) and blk.get("type") == "tool_use":
                        turns.append({"role": m.get("role"), "tool_call": blk.get("name"),
                                      "args": blk.get("input")})
        with _db() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rep_call_log (
                  id SERIAL PRIMARY KEY, call_sid TEXT, caller_digits TEXT,
                  n_turns INTEGER, transcript JSONB,
                  created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute(
                "INSERT INTO rep_call_log (call_sid, caller_digits, n_turns, transcript)"
                " VALUES (%s,%s,%s,%s) RETURNING id",
                (self.call_sid, self.caller_digits, len(turns),
                 _json.dumps(turns, default=str)))
            log.info(f"{self._tag} rep_call_log saved id={cur.fetchone()[0]}")


# ─── FastAPI app ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("ew-rep-voice startup")
    yield

app = FastAPI(lifespan=lifespan)


@app.get("/rep-voice/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.post("/rep-voice/answer")
async def answer(request: Request):
    form = await request.form()
    frm = str(form.get("From", ""))
    log.info(f"inbound call from {frm}")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{PUBLIC_WS_URL}">'
        f'<Parameter name="caller" value="{frm}"/>'
        "</Stream></Connect></Response>")
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/rep-voice/stream")
async def rep_stream(ws: WebSocket):
    await ws.accept()
    session = RepCall(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            await session.handle_twilio_frame(msg)
    except WebSocketDisconnect:
        log.info("websocket disconnected")
    except Exception as e:
        log.exception(f"stream loop err: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
