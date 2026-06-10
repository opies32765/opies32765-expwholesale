"""Standalone Gemini (Vertex AI) text helper.

Added 2026-05-29 when EW's trim/VIN layer was migrated off the Anthropic/Claude
API onto Google/Gemini. Decoupled from app.py on purpose so the trim/VIN modules
(claude_vin_decoder, claude_trim_match, ymmt_match) work both inside the gunicorn
process AND standalone (e.g. canonicalize_bid run from a cron/CLI) without
triggering a full app import or a circular import.

Uses the same Vertex AI service account already configured for EW
(GOOGLE_APPLICATION_CREDENTIALS -> google_vision_key.json,
project my-project-dia-492415, location global).
"""
import threading
import time

_client = None
_lock = threading.Lock()


def _client_get():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    from google import genai
                    _client = genai.Client(
                        vertexai=True,
                        project='my-project-dia-492415',
                        location='global',
                    )
                except Exception as e:
                    print(f'[gemini_helper] init failed: {e}', flush=True)
                    _client = False  # poison so we don't retry init every call
    return _client if _client else None


def gemini_text(prompt, model='gemini-2.5-flash', max_tokens=2000, temperature=0.0,
                thinking_budget=None):
    """One-shot Gemini text completion. Returns stripped text or None.

    Defaults: gemini-2.5-flash, temperature 0.0 (deterministic structured output —
    these callers all want strict JSON). max_tokens defaults high so reasoning
    ('thinking') tokens don't crowd out the JSON answer.

    thinking_budget: pass 0 (with a flash model) to disable thinking entirely —
    use for short strict-JSON judgments where thinking can eat the token budget
    and truncate the answer (seen on trim-match: pro+thinking truncated the JSON
    mid-reason). Leave None to keep model-default thinking (good for the VIN
    decoder's generation-overlap reasoning). Retries up to 2x on 429."""
    start_keepalive(warm_now=False)  # keep this process Gemini client hot
    client = _client_get()
    if not client:
        return None
    from google.genai import types
    _cfg = dict(max_output_tokens=max_tokens, temperature=temperature)
    # GEMINI_35_FLASH_NOTHINK_2026_05_31: flash models default to extended
    # thinking which eats max_output_tokens -> small-budget calls return empty.
    # Default flash to thinking_budget=0. Non-flash keeps THINKING_CLAMP behavior.
    _tb = thinking_budget
    if _tb is None and 'flash' in (model or '').lower():
        _tb = 0
    if _tb is not None:
        if _tb == 0 and 'flash' not in (model or '').lower():
            _tb = 128
        _cfg['thinking_config'] = types.ThinkingConfig(thinking_budget=_tb)
    cfg = types.GenerateContentConfig(**_cfg)
    last = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=cfg,
            )
            return resp.text.strip() if resp.text else None
        except Exception as e:
            last = e
            m = str(e)
            if '429' in m or 'RESOURCE_EXHAUSTED' in m or 'rate' in m.lower():
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            break
    print(f'[gemini_helper] call failed ({model}): {last}', flush=True)
    return None


# ---- WARM / KEEPALIVE (added 2026-06-09) --------------------------------
# Keep each process Gemini client + OAuth token + TLS connection hot so the
# first real call is not a 5-6s cold start. gunicorn runs N worker processes;
# each forked worker re-warms via the os.register_at_fork hook below. Other
# long-running processes warm lazily on their first gemini_text() call.
import os as _os

_keepalive_started = False
_keepalive_lock = threading.Lock()
_KEEPALIVE_INTERVAL_SEC = 240
_WARM_MODEL = "gemini-2.5-flash"


def _warm_ping():
    try:
        gemini_text("ping", model=_WARM_MODEL, max_tokens=4, thinking_budget=0)
    except Exception:
        pass


def start_keepalive(warm_now=True):
    """Idempotent per process: one daemon thread that optionally warm-pings now,
    then re-pings every _KEEPALIVE_INTERVAL_SEC to keep the client / token /
    connection from going cold."""
    global _keepalive_started
    with _keepalive_lock:
        if _keepalive_started:
            return
        _keepalive_started = True

    def _run():
        if warm_now:
            _warm_ping()
        while True:
            time.sleep(_KEEPALIVE_INTERVAL_SEC)
            _warm_ping()

    threading.Thread(target=_run, name="gemini-keepalive", daemon=True).start()


def _after_fork_child():
    # Parent client connection + keepalive thread do NOT survive fork; reset
    # and re-warm this child (gunicorn worker) process.
    global _client, _keepalive_started
    _client = None
    _keepalive_started = False
    start_keepalive(warm_now=True)


try:
    _os.register_at_fork(after_in_child=_after_fork_child)
except Exception:
    pass
# ------------------------------------------------------------------------
