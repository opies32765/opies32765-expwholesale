"""EW_LOCAL_BRAIN_SHIM_2026_06_11: route EVERY google.genai generate_content call
through the local Qwen brain, with REAL Gemini as fallback on ANY error.

Import this module once per process; install() monkey-patches the class method so all
genai clients in the process are covered, regardless of how the call site is LABELED
(gemini_call, gemini_text, sonnet_vision_call, claude_vin_decoder, the direct
assessment call, dealer_intel, discover_dealer, voice ...). They all bottom out at
google.genai.models.Models.generate_content, which is what we patch.

Config (env var first, then /etc/ew-brain.env, then default) so it works the same in
systemd services, cron jobs, and manual runs without per-launcher env plumbing:
  EW_LOCAL_BRAIN   "1" to force-enable (else governed by the flag file)
  EW_BRAIN_URL     default https://brain.experience-wholesale.net
  EW_BRAIN_KEY     bearer token for the vLLM --api-key
  EW_BRAIN_TIMEOUT seconds, default 45

Enabled when env EW_LOCAL_BRAIN=1 OR the flag file /opt/expwholesale/LOCAL_BRAIN_ON
exists. It can never break EW: any error in the local path -> the original Gemini call.
Kill switch (no restart): rm /opt/expwholesale/LOCAL_BRAIN_ON."""
import os, json, base64, urllib.request, urllib.error

_FLAG = "/opt/expwholesale/LOCAL_BRAIN_ON"
_ENVFILE = "/etc/ew-brain.env"
_DEFAULT_URL = "https://brain.experience-wholesale.net"

_filecfg = None
def _file():
    """Parse /etc/ew-brain.env (KEY=VALUE lines) once; cache. Never raises."""
    global _filecfg
    if _filecfg is None:
        _filecfg = {}
        try:
            with open(_ENVFILE) as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, v = ln.split("=", 1)
                    _filecfg[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            _filecfg = {}
    return _filecfg

def _conf(name, default=None):
    v = os.environ.get(name)
    if v is not None and v != "":
        return v
    v = _file().get(name)
    return v if (v is not None and v != "") else default

def _enabled():
    try:
        if os.environ.get("EW_LOCAL_BRAIN") == "1":
            return True
        if os.path.exists(_FLAG):
            return True
        return _file().get("EW_LOCAL_BRAIN") == "1"
    except Exception:
        return False

def _url():
    return _conf("EW_BRAIN_URL", _DEFAULT_URL).rstrip("/") + "/v1/chat/completions"

class _Part:
    def __init__(self, t): self.text = t
class _Content:
    def __init__(self, t): self.parts = [_Part(t)]; self.role = "model"
class _Cand:
    def __init__(self, t): self.content = _Content(t); self.finish_reason = "STOP"
class _Resp:
    """Mimics genai GenerateContentResponse for the accessors EW uses
    (resp.text and resp.candidates[0].content.parts[0].text)."""
    def __init__(self, t): self._t = t; self.candidates = [_Cand(t)]
    @property
    def text(self): return self._t

def _cfg(config, name, default):
    if config is None: return default
    try:
        v = config.get(name) if isinstance(config, dict) else getattr(config, name, None)
        return v if v is not None else default
    except Exception:
        return default

def _flatten(contents):
    """Pull plain text + (mime, bytes) images out of whatever genai `contents`
    shape the call site used: str, a Part, a Content(.parts), or a list of those."""
    text, imgs = [], []
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    for it in items:
        if it is None: continue
        if isinstance(it, str): text.append(it); continue
        inl = getattr(it, "inline_data", None)
        if inl is not None and getattr(inl, "data", None) is not None:
            imgs.append((getattr(inl, "mime_type", "image/jpeg") or "image/jpeg", inl.data)); continue
        t = getattr(it, "text", None)
        if t: text.append(t); continue
        for p in (getattr(it, "parts", None) or []):
            pinl = getattr(p, "inline_data", None)
            if pinl is not None and getattr(pinl, "data", None) is not None:
                imgs.append((getattr(pinl, "mime_type", "image/jpeg") or "image/jpeg", pinl.data))
            elif getattr(p, "text", None):
                text.append(p.text)
    return "\n".join(text), imgs

# BRAIN_CTX_RETRY_2026_07_29: vLLM rejects (HTTP 400) when
# prompt_tokens + max_tokens > max_model_len. Call sites request
# max_output_tokens=8000 into a 32,768 window, so the real prompt ceiling was
# 24,768 — and the assessment model only ever emits ~130 tokens (measured max 154
# over 191 July assessments). Every breach used to land in the `except` below and
# silently leave the local brain for Gemini: ~9.9% of July assessments.
# Reserving less output is free, so try a descending ladder before giving up.
# Only ever runs on a request that has ALREADY failed outright.
_CTX_RETRY_LADDER = (2048, 512)


def _call_brain(model, contents, config):
    prompt, imgs = _flatten(contents)
    if imgs:
        content = [{"type": "text", "text": prompt}]
        for mime, data in imgs:
            content.append({"type": "image_url", "image_url": {
                "url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}})
    else:
        content = prompt or ""
    want = int(_cfg(config, "max_output_tokens", 1024) or 1024)
    # Cloudflare Bot-Fight blocks the default "Python-urllib/x" UA with a fast 403
    # (verified: browser UA -> 200, urllib UA -> 403). Send a browser UA so the
    # named tunnel lets server-to-server POSTs through. Same lesson as AccuTrade CDN.
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "application/json"}
    k = _conf("EW_BRAIN_KEY")
    if k: headers["Authorization"] = "Bearer " + k

    def _attempt(maxtok):
        body = {"model": "ew-brain",
                "messages": [{"role": "user", "content": content}],
                "max_tokens": maxtok,
                "temperature": float(_cfg(config, "temperature", 0.4) or 0.0),
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(_url(), data=json.dumps(body).encode(),
                                     headers=headers)
        with urllib.request.urlopen(
                req, timeout=float(_conf("EW_BRAIN_TIMEOUT", "45"))) as r:
            return json.loads(r.read().decode())

    try:
        j = _attempt(want)
    except urllib.error.HTTPError as e:
        detail = ""
        try: detail = e.read().decode()[:400]
        except Exception: pass
        # Only squeeze on a genuine context overflow — never mask a real 400.
        if e.code != 400 or "maximum context length" not in detail:
            raise
        j = None
        for _mt in _CTX_RETRY_LADDER:
            if _mt >= want:
                continue
            try:
                j = _attempt(_mt)
                try: print("[local-brain-shim] ctx-squeeze: max_tokens %d->%d, stayed local"
                           % (want, _mt), flush=True)
                except Exception: pass
                break
            except Exception:
                continue
        if j is None:
            raise

    txt = (j.get("choices") or [{}])[0].get("message", {}).get("content")
    if not txt or not txt.strip():
        raise RuntimeError("empty brain response")
    return _Resp(txt.strip())

def install():
    try:
        from google.genai import models as _gm
    except Exception as e:
        try: print("[local-brain-shim] genai not importable: %s" % e, flush=True)
        except Exception: pass
        return False
    if getattr(getattr(_gm.Models, "generate_content", None), "_ew_shim", False):
        return True
    _orig = _gm.Models.generate_content
    def generate_content(self, *args, **kwargs):
        if _enabled():
            try:
                model = kwargs.get("model") if "model" in kwargs else (args[0] if args else None)
                contents = kwargs.get("contents") if "contents" in kwargs else (args[1] if len(args) > 1 else None)
                config = kwargs.get("config") if "config" in kwargs else (args[2] if len(args) > 2 else None)
                return _call_brain(model, contents, config)
            except Exception as e:
                try: print("[local-brain-shim] fallback->gemini: %s: %s" % (type(e).__name__, e), flush=True)
                except Exception: pass
        return _orig(self, *args, **kwargs)
    generate_content._ew_shim = True
    _gm.Models.generate_content = generate_content
    try: print("[local-brain-shim] installed (all generate_content -> 9B, Gemini fallback)", flush=True)
    except Exception: pass
    return True

install()
