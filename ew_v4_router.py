"""
v4-router: per-user routing of EW vision OCR calls to the home 5070 Ti.

Drop this file into /opt/expwholesale/ on Contabo 1 alongside app.py.

How it works:
  - Reads two env vars set in the systemd service file:
      EW_V4_URL=http://100.x.x.x:8001/v1/extract   (Tailscale IP of home machine)
      EW_TEST_USER_PHONE=+1XXXXXXXXXX              (only this caller routes to v4)
  - In a Flask request context, it inspects the inbound `From` (Twilio) or
    `from_phone` (web form) field. If it matches EW_TEST_USER_PHONE, calls v4.
  - On any other call (no env vars, no match, v4 unreachable, v4 returns None
    or skipped), the caller falls back to the existing Gemini path.

This file is fail-safe by design — if anything in v4 land breaks, prod EW
keeps using Gemini for everyone, including the test user, with a single
log line ("v4 ... falling back").
"""
import base64
import os

import requests as _requests

EW_V4_URL = os.environ.get("EW_V4_URL", "")
EW_TEST_USER_PHONE = os.environ.get("EW_TEST_USER_PHONE", "")
EW_V4_TIMEOUT = float(os.environ.get("EW_V4_TIMEOUT", "60"))
# Set EW_V4_SKIP_PRECHECK=0 to re-enable serve_v4's YES/NO precheck.
# Default: skip precheck — too conservative on dense documents like Monroney
# stickers. Hallucinations are filtered by check-digit + logprob below.
EW_V4_SKIP_PRECHECK = os.environ.get("EW_V4_SKIP_PRECHECK", "1") == "1"
# avg log-prob threshold for VIN OCR. Hallucinated VINs typically have lower
# confidence than reads of real VINs. -0.5 is a reasonable default; tune via env.
EW_V4_VIN_LOGPROB_MIN = float(os.environ.get("EW_V4_VIN_LOGPROB_MIN", "-0.5"))


_VIN_CHARS = set("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")
_VIN_TRANSLITERATE = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    **{str(d): d for d in range(10)},
}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_check_digit_valid(vin: str) -> bool:
    """Standard VIN check-digit (NHTSA Title 49 CFR § 565.15)."""
    if not vin or len(vin) != 17 or any(c not in _VIN_CHARS for c in vin.upper()):
        return False
    vin = vin.upper()
    total = sum(_VIN_TRANSLITERATE[c] * _VIN_WEIGHTS[i] for i, c in enumerate(vin))
    expect = total % 11
    expect_char = "X" if expect == 10 else str(expect)
    return vin[8] == expect_char


def should_use_v4() -> bool:
    """True only inside a Flask request from the test user's phone."""
    if not EW_V4_URL or not EW_TEST_USER_PHONE:
        return False
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return False
        from_phone = request.form.get("From") or request.form.get("from_phone") or ""
        return from_phone.strip() == EW_TEST_USER_PHONE.strip()
    except Exception:
        return False


def v4_extract(image_bytes: bytes, task: str = "vin"):
    """Call the home machine. Returns the extracted value or None on failure/skip."""
    if not EW_V4_URL:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        r = _requests.post(EW_V4_URL,
                           json={"image_b64": b64, "task": task,
                                 "skip_precheck": EW_V4_SKIP_PRECHECK},
                           timeout=EW_V4_TIMEOUT)
        if r.status_code != 200:
            print(f"[v4] HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        d = r.json()
        if d.get("skipped"):
            print(f"[v4] skipped (precheck): {d.get('reason')}", flush=True)
            return None
        val = d.get("value")
        logprob = d.get("logprob")
        if not val:
            return None
        # VIN-specific guards: check digit + logprob threshold.
        # These catch hallucinations on non-VIN photos.
        if task == "vin":
            if not vin_check_digit_valid(val):
                print(f"[v4] vin={val} REJECTED (bad check digit, lp={logprob})", flush=True)
                return None
            if logprob is not None and logprob < EW_V4_VIN_LOGPROB_MIN:
                print(f"[v4] vin={val} REJECTED (low confidence lp={logprob} < {EW_V4_VIN_LOGPROB_MIN})", flush=True)
                return None
        print(f"[v4] {task}={val} (lp={logprob})", flush=True)
        return val
    except _requests.Timeout:
        print(f"[v4] timeout calling {EW_V4_URL}", flush=True)
        return None
    except Exception as e:
        print(f"[v4] error: {e}", flush=True)
        return None
