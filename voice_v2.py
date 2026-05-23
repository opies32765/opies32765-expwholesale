"""voice_v2.py — sidecar Flask app for the next-gen voice pipeline.

Listens on port 9002. Owns only the persistent-WS STT path:
    GET  /api/voice/stt/ws/v2     — one WebSocket per CONVERSATION (not per
                                    utterance). PCM streams continuously.
                                    Server-side Silero VAD segments speech
                                    into turns. Each turn runs a fresh
                                    Google Cloud Speech streaming session
                                    inside the same WS.
    GET  /healthz/v2              — liveness

Everything else (/api/voice/query/stream, /api/voice/tts, /mobile/ewbot,
LSL lookup, bid pipeline, etc.) stays on the main gunicorn on 9001.

WS protocol (v2):
  Client → server:
    text frame {"type":"start","sample_rate":16000}    once at open
    binary PCM (16-bit LE)                              continuously
    text frame {"type":"end_conversation"}              when user ends

  Server → client:
    {"type":"speech_start"}                             Silero detected
    {"type":"interim","transcript":"..."}               Google interim
    {"type":"final","transcript":"...","confidence":N}  Google final on
                                                        utterance close
    {"type":"speech_end"}                               Silero confirms
    {"type":"error","message":"..."}
"""
from __future__ import annotations
import json as _json
import os
import queue as _q
import threading as _th
import time
from typing import Optional

import numpy as np
import onnxruntime as ort
import secrets as _secrets
import os as _os
from flask import Flask, jsonify, render_template, request as _request
from livekit.api import AccessToken, VideoGrants
from flask_sock import Sock

# ── App + WS ────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="/opt/expwholesale/templates")
# Sidecar serves /mobile/ewbot too — auto_reload picks up template edits
# without a service restart. Cheap because only this one template here.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
sock = Sock(app)


@app.route("/mobile/ewbot")
def mobile_ewbot():
    return render_template("mobile_ewbot.html")


# SIDECAR_TTS_2026_05_22 / FLASH_2026_05_22 — ElevenLabs Flash v2.5
# (~75ms first-byte) replaces Google Neural2 for faster client demo.
from flask import Response as _Resp, request as _req
_ELEVEN_API_KEY = _os.environ.get("ELEVENLABS_API_KEY", "")
_ELEVEN_VOICE_ID = _os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")  # Adam
_ELEVEN_MODEL = _os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
_eleven_client = None
def _get_eleven_client():
    global _eleven_client
    if _eleven_client is None:
        from elevenlabs.client import ElevenLabs
        _eleven_client = ElevenLabs(api_key=_ELEVEN_API_KEY)
    return _eleven_client

@app.route("/api/voice/tts/warmup", methods=["GET"])
def sidecar_tts_warmup():
    try:
        client = _get_eleven_client()
        # Force an actual TTS roundtrip so TLS + voice-id load are hot
        # for the FIRST real reply. Tiny payload — half a cent.
        _gen = client.text_to_speech.convert(
            voice_id=_ELEVEN_VOICE_ID,
            text="ok",
            model_id=_ELEVEN_MODEL,
            output_format="mp3_44100_128",
        )
        for _chunk in _gen:
            pass
        return jsonify({"ok": True, "warmed": ["elevenlabs_audio"]})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/voice/tts", methods=["POST"])
def sidecar_tts():
    """Synthesize via ElevenLabs Flash v2.5. Streams MP3 chunks back to
    the client as soon as ElevenLabs starts emitting them. First audio
    typically lands within 150-300ms from the request."""
    body = _req.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text) > 4000:
        text = text[:4000]
    voice_id = body.get("voice") or _ELEVEN_VOICE_ID
    model_id = body.get("model") or _ELEVEN_MODEL
    if not _ELEVEN_API_KEY:
        return jsonify({"error": "ELEVENLABS_API_KEY missing"}), 500
    try:
        client = _get_eleven_client()
        def _gen():
            for chunk in client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id=model_id,
                output_format="mp3_44100_128",
            ):
                if chunk:
                    yield chunk
        return _Resp(_gen(), mimetype="audio/mpeg", headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # disable nginx buffering for true streaming
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# LIVEKIT_TOKEN_2026_05_22 — mint a short-lived room access token
@app.route("/api/livekit/token", methods=["POST"])
def lk_token():
    data = _request.get_json(silent=True) or {}
    identity = (data.get("identity") or f"user-{_secrets.token_urlsafe(8)}")[:64]
    room = (data.get("room") or f"ew-{_secrets.token_urlsafe(8)}")[:64]
    api_key = _os.environ.get("LIVEKIT_API_KEY")
    api_secret = _os.environ.get("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        return jsonify({"error": "LIVEKIT_API_KEY/SECRET missing"}), 500
    grant = VideoGrants(
        room_join=True, room=room,
        can_publish=True, can_publish_data=True, can_subscribe=True,
    )
    tok = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grant)
        .with_ttl(__import__("datetime").timedelta(hours=1))
    ).to_jwt()
    return jsonify({"token": tok, "room": room, "identity": identity,
                    "url": "wss://experience-wholesale.net/livekit"})


@app.route("/mobile/ewbot/lk")
def mobile_ewbot_lk():
    return render_template("mobile_ewbot_lk.html")


# MOBILE_EWBOT_PC_2026_05_22 — Pipecat-based bot page
@app.route("/mobile/ewbot/pc")
def mobile_ewbot_pc():
    return render_template("mobile_ewbot_pc.html")


# EW_VOICE_API_2026_05_22 — Option D: Anthropic API + our MCP server.
# Deterministic tool calling, EW-branded front-end.
try:
    from ew_voice_api import bp as _ew_voice_bp
    app.register_blueprint(_ew_voice_bp)
    print("[voice_v2] ew_voice_api blueprint registered", flush=True)
except Exception as _e:
    print(f"[voice_v2] ew_voice_api blueprint failed: {_e}", flush=True)

try:
    from ew_voice_cerebras import bp as _ew_voice_cbr_bp
    app.register_blueprint(_ew_voice_cbr_bp)
    print("[voice_v2] ew_voice_cerebras blueprint registered", flush=True)
except Exception as _e:
    print(f"[voice_v2] ew_voice_cerebras blueprint failed: {_e}", flush=True)


@app.route("/voice")
@app.route("/v")
def ew_voice_page():
    return render_template("ew_voice.html")

@app.route("/v-fast")
def ew_voice_fast_page():
    # Same UI; client-side overrides the stream endpoint via query flag
    return render_template("ew_voice.html")


# ── Silero VAD (loaded once at boot, shared across requests) ───────────
_SILERO_PATH = '/opt/expwholesale/models/silero_vad.onnx'
print(f'[voice_v2] loading Silero VAD from {_SILERO_PATH}', flush=True)
_silero_session = ort.InferenceSession(
    _SILERO_PATH, providers=['CPUExecutionProvider'])
print('[voice_v2] Silero VAD ready', flush=True)


def _silero_step(audio_f32: np.ndarray, state: np.ndarray,
                 ctx: np.ndarray, sr: int = 16000):
    """Run one Silero v5 inference on a 512-sample frame at 16kHz.

    SILERO_CTX_FIX_2026_05_22 — v5 ONNX requires the input tensor to be
    the 512-sample frame prefixed by 64 samples of context carried over
    from the previous frame. Without it the first conv layer has no
    left-history and output is pinned at ~0.001 regardless of input.

    Returns (prob, new_state, new_ctx).
    """
    x = np.concatenate([ctx, audio_f32.reshape(1, -1).astype(np.float32)], axis=1)
    out = _silero_session.run(None, {
        'input': x,
        'state': state,
        'sr': np.array(sr, dtype=np.int64),
    })
    new_ctx = x[:, -64:]
    return float(out[0][0][0]), out[1], new_ctx


# ── Google Cloud Speech (lazy import — only when actually streaming) ────

def _gcs_streaming(audio_q: _q.Queue, sample_rate: int, on_msg):
    """Run Google Cloud Speech streaming_recognize against an audio queue.
    Stops cleanly when queue gets a None sentinel.
    on_msg: callback({"type":"interim"|"final","transcript":...})
    """
    from google.cloud import speech
    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate,
        language_code='en-US',
        model='latest_short',
        enable_automatic_punctuation=True,
        use_enhanced=True,
    )
    # We use Silero VAD for turn detection now — let Google emit finals
    # too but we trust Silero for end-of-utterance.
    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        interim_results=True,
        single_utterance=False,
    )

    def _req_gen():
        while True:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    try:
        for resp in client.streaming_recognize(streaming_config, _req_gen()):
            for r in resp.results:
                if not r.alternatives:
                    continue
                alt = r.alternatives[0]
                txt = (alt.transcript or '').strip()
                if not txt:
                    continue
                on_msg({
                    'type': 'final' if r.is_final else 'interim',
                    'transcript': txt,
                    'confidence': float(alt.confidence or 0),
                })
    except Exception as e:
        on_msg({'type': 'error', 'message': f'gcs: {str(e)[:200]}'})


# ── VAD state machine constants ─────────────────────────────────────────
# Silero is calibrated at 16kHz. Frames must be exactly 512 samples (32 ms).
VAD_SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * 2  # int16 LE

# Speech-start: prob > 0.50 for 2 consecutive frames (~64 ms)
SPEECH_START_PROB = 0.25   # 2026-05-22 — iOS Chrome quiet again
SPEECH_START_FRAMES = 2
# Speech-end: prob < 0.15 for ~600 ms (19 frames)
SPEECH_END_PROB = 0.15
SPEECH_END_FRAMES = 30
# Pre-roll: include ~250 ms of audio before speech_start in STT (so we
# don't lose the first phoneme). 8 frames buffered always.
PRE_ROLL_FRAMES = 8


@sock.route('/api/voice/stt/ws/v2')
def stt_ws_v2(ws):
    """Persistent voice-conversation WebSocket. Lives for the whole
    conversation. Silero VAD segments speech into turns."""
    print('[voice_v2] WS opened', flush=True)
    ws_lock = _th.Lock()

    def _send(payload: dict):
        try:
            with ws_lock:
                ws.send(_json.dumps(payload))
        except Exception:
            pass

    # Receive the start frame
    try:
        first = ws.receive(timeout=10)
    except Exception as e:
        _send({'type': 'error', 'message': f'no start: {e}'})
        return
    sample_rate = VAD_SAMPLE_RATE
    if first and isinstance(first, str):
        try:
            f = _json.loads(first)
            if f.get('sample_rate'):
                sample_rate = int(f['sample_rate'])
        except Exception:
            pass
    if sample_rate != VAD_SAMPLE_RATE:
        _send({'type': 'error',
               'message': f'sample_rate must be {VAD_SAMPLE_RATE}, got {sample_rate}'})
        return

    _send({'type': 'ready'})

    # Per-conversation Silero state
    silero_state = np.zeros((2, 1, 128), dtype=np.float32)
    silero_ctx = np.zeros((1, 64), dtype=np.float32)
    consec_speech = 0
    consec_silence = 0
    in_speech = False

    # Pre-roll ring buffer of recent frames (always populated, even in silence)
    pre_roll: list[bytes] = []

    # Per-utterance GCS thread + audio queue
    gcs_thread: Optional[_th.Thread] = None
    gcs_queue: Optional[_q.Queue] = None

    # Frame assembly buffer — incoming PCM may arrive in chunks of arbitrary size
    pcm_buf = bytearray()

    def _start_utterance():
        """Spin up a fresh Google streaming session for this utterance."""
        nonlocal gcs_thread, gcs_queue
        gcs_queue = _q.Queue()
        # Flush pre-roll into GCS so first phoneme isn't lost
        for f in pre_roll:
            gcs_queue.put(f)

        def _run():
            _gcs_streaming(gcs_queue, sample_rate, _send)
        gcs_thread = _th.Thread(target=_run, daemon=True)
        gcs_thread.start()

    def _end_utterance():
        """Close the GCS stream. Google emits any remaining final."""
        nonlocal gcs_thread, gcs_queue
        if gcs_queue is not None:
            gcs_queue.put(None)
        if gcs_thread is not None:
            gcs_thread.join(timeout=2)
        gcs_thread = None
        gcs_queue = None

    # DEBUG_VAD_2026_05_22 — log VAD prob + audio level every 30 frames (~1s)
    _frame_counter = [0]
    def _process_frame(frame_bytes: bytes):
        """One 512-sample frame. Update VAD, feed GCS when in speech."""
        nonlocal consec_speech, consec_silence, in_speech, silero_state, silero_ctx
        # int16 → float32 in [-1, 1]
        audio = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size != VAD_FRAME_SAMPLES:
            return
        prob, silero_state, silero_ctx = _silero_step(audio, silero_state, silero_ctx, sample_rate)
        _frame_counter[0] += 1
        if _frame_counter[0] % 30 == 0:
            rms = float(np.sqrt(np.mean(audio * audio)))
            peak = float(np.max(np.abs(audio)))
            print(f"[voice_v2 vad] f={_frame_counter[0]} prob={prob:.3f} "
                  f"rms={rms:.4f} peak={peak:.4f} in_speech={in_speech}", flush=True)

        # Maintain pre-roll ring (always)
        pre_roll.append(frame_bytes)
        if len(pre_roll) > PRE_ROLL_FRAMES:
            pre_roll.pop(0)

        # iOS_WARMUP_2026_05_22 — first ~1.5s, use a lower threshold
        # so the first utterance (iOS AGC hasn't ramped) still registers.
        _warmup_thresh = 0.12 if _frame_counter[0] < 45 else SPEECH_START_PROB
        if prob >= _warmup_thresh:
            consec_speech += 1
            consec_silence = 0
        else:
            consec_silence += 1
            if prob < SPEECH_END_PROB:
                # only count as "true silence" when low-confidence
                pass
            else:
                # ambiguous — reset speech-start counter so we don't latch
                consec_speech = 0

        if not in_speech:
            if consec_speech >= SPEECH_START_FRAMES:
                in_speech = True
                consec_silence = 0
                _send({'type': 'speech_start'})
                _start_utterance()
                # The pre_roll frames are ALREADY in the GCS queue (above),
                # but we still want to feed THIS frame too.
                if gcs_queue is not None:
                    gcs_queue.put(frame_bytes)
        else:
            # In-speech: forward every frame to GCS
            if gcs_queue is not None:
                gcs_queue.put(frame_bytes)
            if consec_silence >= SPEECH_END_FRAMES:
                in_speech = False
                consec_speech = 0
                _end_utterance()
                _send({'type': 'speech_end'})

    # Main receive loop — runs for the whole conversation
    print('[voice_v2] entering recv loop', flush=True)
    try:
        while True:
            msg = ws.receive(timeout=120)  # 2-min idle tolerance
            if msg is None:
                print('[voice_v2] WS closed by client', flush=True)
                break
            if isinstance(msg, str):
                # Control frame
                try:
                    j = _json.loads(msg)
                except Exception:
                    continue
                t = j.get('type')
                if t == 'end_conversation':
                    print('[voice_v2] end_conversation', flush=True)
                    break
                # Could add 'barge_in_ack' etc later
                continue
            if not isinstance(msg, (bytes, bytearray)):
                continue
            pcm_buf.extend(msg)
            # Process all complete 512-sample frames
            while len(pcm_buf) >= VAD_FRAME_BYTES:
                frame = bytes(pcm_buf[:VAD_FRAME_BYTES])
                del pcm_buf[:VAD_FRAME_BYTES]
                _process_frame(frame)
    except Exception as e:
        print(f'[voice_v2] recv err: {e}', flush=True)
        _send({'type': 'error', 'message': f'recv: {str(e)[:200]}'})
    finally:
        # Make sure any in-flight utterance is closed
        if in_speech:
            _end_utterance()
        print('[voice_v2] WS handler exiting', flush=True)


@app.route('/healthz/v2')
def healthz():
    return jsonify({'ok': True, 'service': 'voice_v2', 'silero': 'loaded'})
