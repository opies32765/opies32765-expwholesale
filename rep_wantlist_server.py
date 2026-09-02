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

import asyncio, base64, json, logging, os, random, re, time, audioop, io, wave, threading, queue
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
# REP_MODE=raw  -> the 9B words EVERYTHING (greeting + post-action replies);
#                  guards run log-only ("[raw-telemetry]") and never interfere.
# REP_MODE=guarded -> Python words action confirmations from DB results.
RAW_MODE           = os.environ.get("REP_MODE", "guarded").lower() == "raw"

log = logging.getLogger("ew-rep-voice")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")

SYSTEM_PROMPT = open("/opt/expwholesale/rep_wantlist_prompt.txt").read()
if RAW_MODE:
    SYSTEM_PROMPT += (
        "\nAfter you emit an action, the next message will be `[tool_result] {json}` — "
        "the actual outcome. Reply with a new JSON object whose \"say\" words that outcome "
        "naturally: brief, factual, strictly from the result. Never claim success before "
        "seeing the result.\n")
else:
    SYSTEM_PROMPT += (
        "\nWhen action is set, leave \"say\" empty — the system speaks the confirmation for you.\n")

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


def _first_balanced_obj(s: str):
    """Return the first balanced {...} JSON object in s, tolerant of trailing
    junk (e.g. the extra closing brace the 9B loves to add). None if no
    complete object is present."""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(s)):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:k + 1]
    return None


def _scan_json_string_value(s, key):
    """Pull a JSON string value for `key` out of possibly-malformed text
    without regex (avoids escaping traps). Returns the decoded str or None."""
    kpos = s.find(chr(34) + key + chr(34))
    if kpos == -1:
        return None
    q = s.find(chr(34), kpos + len(key) + 2)
    if q == -1:
        return None
    out = []
    esc = False
    for k in range(q + 1, len(s)):
        ch = s[k]
        if esc:
            out.append(ch); esc = False
        elif ch == chr(92):
            out.append(ch); esc = True
        elif ch == chr(34):
            raw = "".join(out)
            try:
                return json.loads(chr(34) + raw + chr(34))
            except Exception:
                return raw
    return None


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
    # The 9B often emits an extra trailing brace ({"say":..,"action":{..}}}).
    # The old rfind("}") swallowed the whole malformed string -> json.loads
    # failed -> we SPOKE the raw JSON and DROPPED the action (operator
    # 2026-07-07: rough call, the Porsche cancel never took). Scan for the
    # first BALANCED object so a trailing brace is simply ignored.
    obj = _first_balanced_obj(t)
    if obj is not None:
        try:
            out = json.loads(obj)
            if isinstance(out, dict):
                return {"say": str(out.get("say") or ""), "action": out.get("action")}
        except Exception:
            pass
    # Last resort: salvage the "say" (and any action) so we NEVER read JSON
    # scaffolding aloud and don't silently drop the caller's request.
    say = _scan_json_string_value(t, "say")
    if say is not None:
        act = None
        ap = t.find(chr(34) + "action" + chr(34))
        if ap != -1:
            ao = _first_balanced_obj(t[ap:])
            if ao is not None:
                try:
                    act = json.loads(ao)
                except Exception:
                    act = None
        log.warning(f"{tag} salvaged say/action from malformed JSON: {text[:160]!r}")
        return {"say": say, "action": act}
    return _prose_fallback()


def _mulaw_to_wav16k(ulaw: bytes) -> bytes:
    pcm8 = audioop.ulaw2lin(ulaw, 2)
    pcm16, _ = audioop.ratecv(pcm8, 2, 1, 8000, 16000, None)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm16)
    return buf.getvalue()


class _StreamingSTT:
    """Eager per-utterance Google streaming recognizer: audio is transcribed
    WHILE the caller is still talking, so the transcript is ready at
    end-of-utterance instead of a batch pass afterward (SPEED 2026-07-07,
    option 1). Any failure -> ok()=False and the caller uses batch STT."""

    def __init__(self, tag: str):
        self.tag = tag
        self._q = queue.Queue()
        self._final = []
        self._closed = False
        self._ok = _gstt is not None
        self._thread = None
        if self._ok:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _requests(self):
        while True:
            chunk = self._q.get()
            if chunk is None:
                return
            yield speech_v1.StreamingRecognizeRequest(audio_content=chunk)

    def _run(self):
        try:
            scfg = speech_v1.StreamingRecognitionConfig(
                config=_gstt_config, interim_results=False, single_utterance=False)
            for resp in _gstt.streaming_recognize(scfg, self._requests()):
                for result in resp.results:
                    if result.is_final and result.alternatives:
                        self._final.append(result.alternatives[0].transcript)
        except Exception as e:
            self._ok = False
            log.warning(f"{self.tag} streaming STT err: {e}")

    def feed(self, ulaw_chunk: bytes):
        if self._ok and not self._closed:
            try:
                self._q.put_nowait(bytes(ulaw_chunk))
            except Exception:
                pass

    def abort(self):
        if not self._closed:
            self._closed = True
            try:
                self._q.put_nowait(None)
            except Exception:
                pass

    def finish(self, timeout: float = 1.5) -> str:
        self.abort()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return " ".join(self._final).strip()

    def ok(self) -> bool:
        return self._ok


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



# Manufacturer tokens — make de-dup robust to the 9B storing the same car
# with the make on the model ("corvette"/"Grand Sport") one call and properly
# ("chevrolet"/"Corvette"/"Grand Sport") the next (operator 2026-07-07).
_MFR_TOKENS = {
    "chevrolet", "chevy", "ford", "toyota", "honda", "nissan", "mercedes",
    "benz", "bmw", "audi", "porsche", "ferrari", "lamborghini", "cadillac",
    "gmc", "dodge", "ram", "jeep", "chrysler", "lexus", "acura", "infiniti",
    "mazda", "subaru", "volkswagen", "vw", "volvo", "tesla", "land", "rover",
    "jaguar", "bentley", "rolls", "royce", "aston", "martin", "maserati",
    "alfa", "romeo", "mini", "buick", "lincoln", "hyundai", "kia", "genesis",
    "mitsubishi", "fiat", "mclaren", "bugatti", "hummer", "pontiac", "saturn",
    "mercury", "scion", "smart", "polestar", "rivian", "lucid",
}


def _dedup_vehicle_key(w: dict):
    """Token bag of make+model+trim, lowercased, MINUS manufacturer tokens, so
    the same car survives inconsistent make/model extraction. Empty = not
    enough to key on (never de-duped)."""
    toks = set()
    for p in (w.get("make"), w.get("model"), w.get("trim_contains")):
        for t in re.findall(r"[a-z0-9]+", str(p or "").lower()):
            toks.add(t)
    return frozenset(toks - _MFR_TOKENS)


def _has_mfr_make(w: dict) -> bool:
    m = str(w.get("make") or "").lower()
    return any(tok in _MFR_TOKENS for tok in re.findall(r"[a-z0-9]+", m))


def db_dedupe_wants(phone_digits: str) -> list:
    """DETERMINISTIC de-dup: collapse ACTIVE wants describing the same car
    (same year range + same manufacturer-stripped token bag) down to ONE,
    keeping the best-formed row (real manufacturer make, else lowest id) and
    soft-cancelling the rest. The 9B never decides this (operator 2026-07-07:
    9B cancelled BOTH Corvette rows when asked to drop a duplicate). Returns
    cancelled ids."""
    import psycopg2.extras
    with _db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, make, model, trim_contains, year_min, year_max"
            " FROM bid_alerts WHERE phone_digits=%s AND active=TRUE ORDER BY id",
            (phone_digits,))
        rows = [dict(r) for r in cur.fetchall()]
    groups = {}
    for r in rows:
        key = _dedup_vehicle_key(r)
        if not key:
            continue
        groups.setdefault(((r.get("year_min"), r.get("year_max")), key), []).append(r)
    cancel = []
    for grp in groups.values():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda r: (0 if _has_mfr_make(r) else 1, r["id"]))
        cancel.extend(r["id"] for r in grp[1:])  # keep grp[0], drop the rest
    if cancel:
        with _db() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE bid_alerts SET active=FALSE, updated_at=NOW()"
                " WHERE phone_digits=%s AND id = ANY(%s) AND active=TRUE",
                (phone_digits, cancel))
        log.info(f"[dedupe] phone={phone_digits} cancelled dup ids={cancel}")
    return sorted(cancel)


def _is_list_question(text: str) -> bool:
    """True when the caller is just ASKING what's on their list (not adding or
    dropping) — those get answered deterministically from the DB, never via
    the 9B (which stalls with "let me check" / under-reads the list)."""
    t = (text or "").lower()
    if any(w in t for w in ("add ", "drop", "cancel", "remove", "take off",
                            "get rid")):
        return False
    phrases = (
        "on my list", "my list", "what else do i have", "what else is on",
        "what else you", "what do i have", "what have i", "what am i watching",
        "what are you watching", "watching for me", "how many",
        "read back", "read me", "read my", "what's on", "whats on",
        "what is on", "what cars", "which cars", "anything else on",
    )
    return any(p in t for p in phrases)


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
        self._stt = None            # per-utterance streaming recognizer
        self._stt_fed_trigger = False
        self.silence_ms = 0
        self.last_voiced_ts = time.monotonic()
        self.utterance_active = False
        # QA-call tuning 2026-07-04: 300ms chopped sentences in half and Bill
        # felt "touchy" — a natural mid-sentence pause is ~400-600ms on PSTN.
        self.SILENCE_END_MS = 550   # SPEED 2026-07-07: 700->550 (snappier reply; still > the 300ms that chopped sentences)
        self.MIN_UTTER_MS = 350
        self.utter_start_ts = 0.0
        self._tag = "?"
        # QA-call fix 2026-07-04 ("keeps repeating"): serialize brain turns,
        # coalesce fragmented utterances, suppress repeated identical actions.
        self._turn_lock = asyncio.Lock()
        self._debounce_task: Optional[asyncio.Task] = None
        self._done_actions: set = set()
        # QA 2026-07-05: anti-robotic variety + anti-fabrication guards
        self._last_lines: dict = {}      # phrase-pool key -> last used line
        self._asked_tail = False         # "Anything else?" at most once per call
        self._heard_numbers: set = set() # every number the CALLER actually said

    def _pick(self, key: str, options: list[str]) -> str:
        """Pseudo-random line from a pool, never the same one twice in a row."""
        last = self._last_lines.get(key)
        pool = [o for o in options if o != last] or options
        line = random.choice(pool)
        self._last_lines[key] = line
        return line

    def _tail(self, line: str) -> str:
        if not self._asked_tail:
            self._asked_tail = True
            return line + " Anything else?"
        return line

    def _note_heard_numbers(self, text: str):
        import re as _re
        for m in _re.finditer(r"\b(\d[\d,]*)\s*([kK]|grand)?\b", text):
            try:
                n = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            self._heard_numbers.add(n)
            if m.group(2):
                self._heard_numbers.add(n * 1000)
            self._heard_numbers.add(n * 1000)  # "ninety" -> STT often "90"

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
            if self.utterance_active and self._stt is not None:
                if self._stt_fed_trigger:
                    self._stt_fed_trigger = False  # pre-roll already had this frame
                else:
                    self._stt.feed(ulaw)
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
                self._stt = _StreamingSTT(self._tag)
                self._stt.feed(bytes(self.pcm_buffer))  # pre-roll
                self._stt_fed_trigger = True
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
                        if self._stt is not None:
                            self._stt.abort()
                            self._stt = None
                            self._stt_fed_trigger = False

    async def _finalize_utterance(self):
        stt = self._stt
        self._stt = None
        self._stt_fed_trigger = False
        audio = bytes(self.pcm_buffer)
        self.pcm_buffer.clear()
        if not audio:
            if stt is not None:
                stt.abort()
            return
        text = ""
        if stt is not None and stt.ok():
            text = await asyncio.to_thread(stt.finish)
        if not text:  # streaming empty/failed -> batch fallback (never breaks)
            text = await stt_transcribe(audio, self._tag)
        if not text:
            return
        now = time.monotonic()
        if getattr(self, "_last_text", None) == text and \
           now - getattr(self, "_last_ts", 0) < 1.5:
            return
        self._last_text, self._last_ts = text, now
        log.info(f"{self._tag} USER: {text!r}")
        self._note_heard_numbers(text)
        self.messages.append({"role": "user", "content": text})
        # Debounce: a follow-on fragment within 700ms joins this turn instead
        # of spawning its own — then turns run one at a time under the lock.
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_turn())

    async def _debounced_turn(self):
        try:
            await asyncio.sleep(0.3)   # SPEED 2026-07-07: 0.7->0.3s per-turn debounce
        except asyncio.CancelledError:
            return
        async with self._turn_lock:
            # If a queued earlier turn already answered everything (last word
            # is Bill's and no new user text since), don't answer again.
            if self.messages and self.messages[-1]["role"] == "assistant":
                return
            # Deterministic list-back: answer "what's on my list" instantly
            # from the DB. Never let the 9B stall ("let me check") or narrate
            # (operator 2026-07-07 call: Bill stalled + under-read the list).
            last_user = next((m["content"] for m in reversed(self.messages)
                              if m["role"] == "user"), "")
            if self.caller_digits and _is_list_question(last_user):
                line = await asyncio.to_thread(
                    lambda: _speak_wants(db_open_wants(self.caller_digits)))
                log.info(f"{self._tag} BOT(list/shortcircuit): {line!r}")
                self.messages.append({"role": "assistant", "content": line})
                await self.speak(line)
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
        if self.caller_digits:
            await asyncio.to_thread(db_dedupe_wants, self.caller_digits)
        wants = await asyncio.to_thread(self._refresh_ctx)
        # Deterministic opener — Python picks it, not the LLM. Varied per
        # call so repeat callers don't hear the same script every time.
        def _desc(w):
            return (w.get("label") or f"{(w.get('make') or '').title()} {w.get('model') or ''}").strip()
        SMALL = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
        if not wants:
            opener = self._pick("open0", [
                "This is Bill at Experience Wholesale. What's your customer looking for?",
                "Bill at Experience Wholesale — what are we hunting for?",
                "Hey, Bill here at Experience Wholesale. Who's looking for what?",
            ])
        elif len(wants) == 1:
            d = _desc(wants[0])
            opener = self._pick("open1", [
                f"Hey, it's Bill. Still watching for that {d} for you. What's up?",
                f"Bill here — that {d} is still on my radar. What can I do for you?",
                f"Hey, it's Bill at Experience Wholesale. Haven't forgotten the {d}. What do you need?",
            ])
        else:
            # Enumerate EVERY want deterministically — the small 9B drops
            # cars when it narrates a list (operator 2026-07-07: "had to
            # press him for the second car"). Never let the model word this.
            descs = [_desc(w) for w in wants]
            if len(descs) == 2:
                listed = f"the {descs[0]} and the {descs[1]}"
            else:
                listed = ", ".join(f"the {d}" for d in descs[:-1]) + f", and the {descs[-1]}"
            opener = f"Hey, it's Bill. Still watching {listed} for you. What else you got?"
        if RAW_MODE and not wants:
            # raw mode: let the 9B word its own greeting ONLY when there are no
            # open wants. With wants present, KEEP the deterministic
            # enumerating opener above so no car is ever dropped.
            self.messages.append({"role": "user",
                                  "content": "[call_connected] The caller just dialed in. Greet them."})
            try:
                out = await brain_chat(self.full_prompt, self.messages, self._tag)
                if (out.get("say") or "").strip():
                    opener = out["say"].strip()
            except Exception as e:
                log.warning(f"{self._tag} raw opener failed ({e}); using pool opener")
        self.messages.append({"role": "assistant", "content": opener})
        log.info(f"{self._tag} BOT (opener{'/raw' if RAW_MODE else ''}): {opener!r}")
        await self.speak(opener)

    # ─── 9B turn ─────────────────────────────────────────────────────────
    # guarded: Python words action confirmations from DB results.
    # raw:     the 9B sees [tool_result] and words everything itself.
    async def run_llm_turn(self):
        for _cycle in range(3):
            try:
                out = await brain_chat(self.full_prompt, self.messages, self._tag)
            except Exception as e:
                log.exception(f"brain err: {e}")
                await self.speak("Sorry, having a connection issue. Text this same number and we'll take care of you.")
                return
            say, action = out.get("say", ""), out.get("action")
            actions = action if isinstance(action, list) else ([action] if isinstance(action, dict) else [])
            if actions and RAW_MODE:
                # Reading the caller's list back must be DETERMINISTIC — the
                # 9B drops wants when it narrates them. Bypass the model.
                if any(isinstance(a, dict) and str(a.get("name")) == "list_wants" for a in actions):
                    line = _speak_wants(db_open_wants(self.caller_digits))
                    log.info(f"{self._tag} BOT(list/deterministic): {line!r}")
                    self.messages.append({"role": "assistant", "content": line})
                    await self.speak(line)
                    return
                results = []
                for act in actions[:4]:
                    if not isinstance(act, dict):
                        continue
                    name = str(act.get("name") or "")
                    args = act.get("args") or {}
                    log.info(f"{self._tag} ACTION(raw): {name}({json.dumps(args, default=str)[:150]})")
                    try:
                        results.append({name: await asyncio.to_thread(self._run_action_raw, name, args)})
                        await asyncio.to_thread(self._refresh_ctx)
                    except Exception as e:
                        log.exception(f"{self._tag} raw action failed: {e}")
                        results.append({name: {"error": str(e)}})
                self.messages.append({"role": "assistant",
                                      "content": json.dumps({"say": say, "action": actions}, default=str)})
                self.messages.append({"role": "user",
                                      "content": "[tool_result] " + json.dumps(results, default=str)[:2000]})
                continue  # model now words the outcome itself
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
            return
        # raw mode only: 3 cycles and the model never produced a spoken reply
        await self.speak("All set. Anything else?")

    def _run_action_raw(self, name: str, args: dict) -> dict:
        """RAW mode executor: run the action, return the raw result for the
        9B to word itself. Guards run LOG-ONLY ([raw-telemetry]) so we can
        score the model without interfering."""
        if name == "add_want":
            pm = args.get("price_max")
            try:
                pm_i = int(pm) if pm else None
            except (TypeError, ValueError):
                pm_i = None
            if pm_i and pm_i not in self._heard_numbers and pm_i // 1000 not in self._heard_numbers:
                log.warning(f"{self._tag} [raw-telemetry] price_max={pm_i} was never said by caller")
            sig = json.dumps(["add_want", args], sort_keys=True, default=str)
            if sig in self._done_actions:
                log.warning(f"{self._tag} [raw-telemetry] duplicate add_want re-emitted")
            self._done_actions.add(sig)
            r = db_add_want(self.caller_digits,
                            str(args.get("make") or ""), str(args.get("model") or ""),
                            args.get("year_min"), args.get("year_max"),
                            args.get("trim_contains"), pm_i, args.get("label"))
            if r.get("ok"):
                db_dedupe_wants(self.caller_digits)
            return r
        if name == "list_wants":
            return {"open_requests": db_open_wants(self.caller_digits)}
        if name == "cancel_want":
            raw = args.get("alert_id")
            ids = [int(x) for x in (raw if isinstance(raw, list) else [raw]) if x]
            results = [db_cancel_want(self.caller_digits, i) for i in ids]
            return {"cancelled": sum(1 for r in results if r.get("ok")),
                    "requested": len(ids)}
        return {"error": f"unknown tool {name}"}

    def _run_action(self, name: str, args: dict) -> str:
        """Execute a want-list action; return the DETERMINISTIC line Bill
        speaks (Python words the outcome — the 9B never voices data)."""
        if name == "add_want":
            # ANTI-FABRICATION 2026-07-05: the 9B invented price caps in
            # read-backs ("under 170k" the caller never said). A price_max the
            # caller never uttered on this call gets stripped, not stored.
            pm = args.get("price_max")
            if pm:
                try:
                    pm_i = int(pm)
                    if pm_i not in self._heard_numbers and pm_i // 1000 not in self._heard_numbers:
                        log.warning(f"{self._tag} stripping fabricated price_max={pm_i} "
                                    f"(heard: {sorted(self._heard_numbers)[:12]})")
                        pm = None
                except (TypeError, ValueError):
                    pm = None
            r = db_add_want(self.caller_digits,
                            str(args.get("make") or ""), str(args.get("model") or ""),
                            args.get("year_min"), args.get("year_max"),
                            args.get("trim_contains"), pm,
                            args.get("label"))
            if r.get("ok"):
                db_dedupe_wants(self.caller_digits)
            if r.get("ok") and r.get("note"):
                return self._pick("dup", [
                    "You're already covered on that one — still watching.",
                    "Already got you down for that one.",
                ])
            if r.get("ok"):
                return self._tail(self._pick("add", [
                    "You're on the list. The second one hits our board, you'll get a text.",
                    "Got you down. Soon as one lands, I'll text you at this number.",
                    "On the list. You'll hear from me the minute one shows up.",
                    "Locked in. I'll text you when one comes through.",
                ]))
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
                return self._tail(self._pick("cancel", [
                    "Done — dropped it.",
                    "That one's off the list.",
                    "Gone. Off the list.",
                ]))
            return self._tail(f"Done — cleared all {done} of them.")
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
                       lambda m: three(int(m.group(1))) + " thousand", text)
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
    finally:
        try:
            if getattr(session, "_stt", None) is not None:
                session._stt.abort()
        except Exception:
            pass


# ─── REP_WANTLIST_SMS_2026_09_02 — the BILL keyword over text ──────────
# app.py's /webhook/twilio matches ^\s*bill\b and forwards {from, body} here
# instead of opening a bid. Bill's want-list logic stays in THIS service; app.py
# only forwards. Nothing here touches bids, valuations or enrichment — a reply
# carries the rep's OWN request back to them and nothing else.
#
# Confirmations are worded by PYTHON from the DB result, never by the model:
# the model decides INTENT, the data grounds the words. The model only writes
# the drill question when it can't name a make + model.

_SMS_SUFFIX = (
    "\n═══ THIS IS A TEXT MESSAGE, NOT A CALL ═══\n"
    "Same job, same JSON. One line, no greeting, no sign-off. You get ONE reply — "
    "there is no back-and-forth, so if you can name a make and model, ADD IT NOW "
    "rather than asking. Only ask a question when you genuinely cannot name both.\n")


def _sms_ctx(digits: str) -> str:
    """THIS TEXT block — same shape as _refresh_ctx builds for a call."""
    wants = []
    try:
        wants = db_open_wants(digits)
    except Exception as e:
        log.warning(f"[sms] open-wants lookup failed: {e}")
    ctx = [f"caller_phone_digits: {digits}"]
    if wants:
        ctx.append("OPEN REQUESTS for this caller (id | want | since):")
        for w in wants:
            yr = ""
            if w.get("year_min") and w.get("year_max"):
                yr = (f"{w['year_min']}-{w['year_max']} "
                      if w["year_min"] != w["year_max"] else f"{w['year_min']} ")
            elif w.get("year_min"):
                yr = f"{w['year_min']}+ "
            desc = w.get("label") or f"{yr}{(w['make'] or '').title()} {w['model'] or ''}".strip()
            ctx.append(f"  #{w['id']} | {desc} | {w['created']}")
    else:
        ctx.append("OPEN REQUESTS: none")
    return "═══ THIS TEXT ═══\n" + "\n".join(ctx) + "\n\n"


def _want_desc(w: dict) -> str:
    yr = ""
    ymin, ymax = w.get("year_min"), w.get("year_max")
    if ymin and ymax and ymin != ymax:
        yr = f"{ymin}-{ymax} "
    elif ymin:
        yr = f"{ymin}+ "
    return (w.get("label") or f"{yr}{(w.get('make') or '').title()} {w.get('model') or ''}").strip()


def _sms_wants_line(digits: str) -> str:
    """Open list WITH ids, so 'BILL DROP 41' is always available to them."""
    wants = db_open_wants(digits)
    if not wants:
        return ("Nothing on your list right now. Text BILL then the year, make "
                "and model to start watching one.")
    return "Watching: " + "; ".join("#%s %s" % (w["id"], _want_desc(w)) for w in wants)


async def bill_sms_reply(digits: str, body: str) -> str:
    """One inbound text -> one reply string. Never raises."""
    text = (body or "").strip()
    # strip the leading keyword; app.py matched it, we own the rest
    rest = re.sub(r"^\s*bill\b[\s,:;-]*", "", text, flags=re.I).strip()

    # ---- deterministic fast paths (small models obey code, not prompts) ----
    if not rest:
        return ("It's Bill. Text BILL then the year, make and model and I'll watch "
                "for it — e.g. BILL 2022+ Escalade under 90. BILL LIST to see yours.")
    if re.match(r"^(list|what|status)\b", rest, re.I) and len(rest.split()) <= 3:
        return _sms_wants_line(digits)
    m = re.match(r"^(?:drop|stop|cancel|done|remove)\s*#?\s*(\d{1,7})\s*$", rest, re.I)
    if m:
        r = db_cancel_want(digits, int(m.group(1)))
        if not r.get("ok"):
            return "No open request with that number. " + _sms_wants_line(digits)
        return "Dropped #%s. %s" % (m.group(1), _sms_wants_line(digits))

    # ---- everything else: the brain decides intent, Python words the facts ----
    system = _sms_ctx(digits) + SYSTEM_PROMPT + _SMS_SUFFIX
    out = await brain_chat(system, [{"role": "user", "content": rest}], "[sms]")
    action = out.get("action")
    actions = action if isinstance(action, list) else ([action] if isinstance(action, dict) else [])

    added, cancelled = [], 0
    for act in actions[:4]:
        if not isinstance(act, dict):
            continue
        name = str(act.get("name") or "")
        args = act.get("args") or {}
        log.info("[sms] ACTION %s(%s)" % (name, json.dumps(args, default=str)[:150]))
        try:
            if name == "add_want":
                pm = args.get("price_max")
                try:
                    pm = int(pm) if pm else None
                except (TypeError, ValueError):
                    pm = None
                r = db_add_want(digits, str(args.get("make") or ""),
                                str(args.get("model") or ""), args.get("year_min"),
                                args.get("year_max"), args.get("trim_contains"),
                                pm, args.get("label"))
                if r.get("ok"):
                    added.append(r.get("alert_id"))
                    db_dedupe_wants(digits)
            elif name == "cancel_want":
                raw = args.get("alert_id")
                ids = [int(x) for x in (raw if isinstance(raw, list) else [raw]) if x]
                cancelled += sum(1 for i in ids if db_cancel_want(digits, i).get("ok"))
            elif name == "list_wants":
                return _sms_wants_line(digits)
        except Exception as e:
            log.exception("[sms] action %s failed: %s" % (name, e))
            return "Hit a snag logging that — call me at 754-247-1123 and I'll set it up."

    if added:
        cur = {w["id"]: w for w in db_open_wants(digits)}
        # dedupe may have folded the new row into an existing one
        desc = next((_want_desc(cur[i]) for i in added if i in cur), None)
        if not desc:
            return _sms_wants_line(digits)
        tail = " Also dropped %d." % cancelled if cancelled else ""
        return ("Watching for %s — I'll text you the second one hits our board.%s "
                "Reply BILL DROP %s to stop." % (desc, tail, added[0]))
    if cancelled:
        return "Dropped %d. %s" % (cancelled, _sms_wants_line(digits))

    say = (out.get("say") or "").strip()
    if say:
        return say[:300]
    return ("Didn't catch a car in that — text BILL then the year, make and model, "
            "like BILL 2022+ Escalade under 90.")


@app.post("/rep-voice/sms")
async def rep_sms(request: Request):
    """Called ONLY by app.py's /webhook/twilio BILL branch (loopback)."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "reply": ""}
    digits = _digits(str(data.get("from") or ""))[-10:]
    body = str(data.get("body") or "")
    if len(digits) != 10:
        return {"ok": False, "reply": ""}
    try:
        reply = await bill_sms_reply(digits, body)
    except Exception as e:
        log.exception("[sms] reply failed: %s" % e)
        return {"ok": False, "reply": ""}
    log.info("[sms] %s -> %r" % (digits, reply[:160]))
    return {"ok": True, "reply": reply}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
