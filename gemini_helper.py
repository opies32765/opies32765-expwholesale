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


def gemini_text(prompt, model='gemini-2.5-pro', max_tokens=2000, temperature=0.0,
                thinking_budget=None):
    """One-shot Gemini text completion. Returns stripped text or None.

    Defaults: gemini-2.5-pro, temperature 0.0 (deterministic structured output —
    these callers all want strict JSON). max_tokens defaults high so reasoning
    ('thinking') tokens don't crowd out the JSON answer.

    thinking_budget: pass 0 (with a flash model) to disable thinking entirely —
    use for short strict-JSON judgments where thinking can eat the token budget
    and truncate the answer (seen on trim-match: pro+thinking truncated the JSON
    mid-reason). Leave None to keep model-default thinking (good for the VIN
    decoder's generation-overlap reasoning). Retries up to 2x on 429."""
    client = _client_get()
    if not client:
        return None
    from google.genai import types
    _cfg = dict(max_output_tokens=max_tokens, temperature=temperature)
    if thinking_budget is not None:
        # THINKING_CLAMP_2026_05_30: pro/non-flash models 400 on thinking_budget=0
        # ("model does not support setting thinking_budget to 0"). Clamp 0->128
        # (pro minimum) unless flash, which does support a true 0.
        _tb = thinking_budget
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
