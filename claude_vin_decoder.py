"""Claude Sonnet 4.6 VIN trim decoder.

Replaces the deterministic VDS-table cascade in vin_precise.py with an
LLM-driven decoder that handles generation overlaps + premium-brand
conventions. Caches forever per VIN.

Cache flow:
    decode_vin_smart(vin)
        ├─ vin_decode_cache HIT      → return cached row
        └─ MISS
             ├─ Claude Sonnet 4.6 call (10s timeout)
             │     ├─ confidence ≥ 0.7  → cache + return
             │     └─ < 0.7 or error    → NHTSA fallback + cache as low-confidence
             └─ NHTSA fallback if Claude fully fails

Shadow mode entry point:
    shadow_log_comparison(vin, old_result, bid_id=None)
    Calls decode_vin_smart, compares to old_result, logs to vin_decode_shadow_log.
    Never affects bids.trim.
"""
import os
import json
import re
import time
import threading

ANTHROPIC_MODEL = "claude-sonnet-4-6"
CLAUDE_TIMEOUT_SEC = 12

# Lazy global client + lock for thread-safe init
_client = None
_client_lock = threading.Lock()

# Regex for first balanced JSON object in a string
_JSON_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

# Module-level system prompt: force JSON-only output (Claude 4.6 chains-of-thought without this)
_SYSTEM = ("You output ONLY a single JSON object. No preamble, no explanation, "
           "no markdown, no commentary. The very first character of your response "
           "is '{' and the very last character is '}'.")

_USER_PROMPT_TEMPLATE = """Decode VIN. Position-10 letter maps to model year (2010-2024 range):
A=2010 B=2011 C=2012 D=2013 E=2014 F=2015 G=2016 H=2017
J=2018 K=2019 L=2020 M=2021 N=2022 P=2023 R=2024.

Brand conventions to apply BEFORE answering:

Porsche WP0 (911/Boxster/Cayman):
- 997.2 generation: model years 2009-2012. Position 4: A=Coupe body, C=Cabriolet, B=Targa.
- 991.1 generation: 2012-2016. Position 4: A=Coupe, B=Cabriolet.
- 991.2 generation: 2017-2019.
- 992 generation: 2020+. WP0AC on a 992 = GT3 (not Carrera 4).
- Use position-10 model year FIRST to identify which generation, THEN body/trim.

Porsche WP0AD on 2014 Panamera (970 gen) = Panamera S Hybrid sedan (not Turbo).
Porsche WP1 = Cayenne, WP2 = Panamera (some years).

Mercedes-Benz WDD/W1K/4JG: generation overlaps exist for C-Class (W205 → W206 at ~2022),
E-Class, S-Class. Use position-10 year to disambiguate.

For ANY make where the VIN does not encode the trim, RETURN trim=null and
confidence ≤ 0.6. Do NOT guess. Specifically: Ford Bronco (Base/Big Bend/
Black Diamond/Outer Banks/Badlands/Wildtrak/First Edition/Heritage/Raptor —
option-pack only, not VIN-encoded), Ford F-150 (XL/XLT/Lariat/King Ranch/
Platinum/Limited/Raptor — same), Ford Super Duty, Ford Explorer/Expedition,
Ford Mustang trims (V6/EcoBoost/GT-not-named-by-VIN), Jeep Wrangler (Sport/
Sahara/Rubicon/Rubicon 392/4xe), Jeep Grand Cherokee (Laredo/Limited/
Overland/Summit/Trailhawk/SRT/Trackhawk), Land Rover Range Rover (HSE/
Autobiography/SV variants), Toyota Tundra/Tacoma/4Runner trim hierarchy,
Chevrolet Silverado/Tahoe/Suburban/Colorado, GMC Sierra/Yukon/Canyon,
RAM 1500/2500/3500 trim hierarchy, BMW M-cars in some years, AMG line
packages (E63/C63/S63 base vs S variant). For ALL of these, populate
year/make/model/body/drive but leave trim=null with confidence 0.4-0.6.
The downstream pipeline reads the real trim from iPacket window-sticker
OCR or Carfax/AutoCheck reports.

For makes/models WHERE the trim IS reliably VIN-encoded (Porsche WP0AA=
Carrera vs WP0AB=Carrera S vs WP0AD=Turbo/Turbo S etc., Ferrari position
4-5 VDS, Lamborghini, McLaren), you may report a confident trim.

(Background: bid 1782 was a Ford Bronco. The model returned trim="Base"
at confidence 0.8. The actual sticker said "Outer Banks." This rule
prevents that hallucination by forcing trim=null instead.)

VIN: {vin}

Return ONLY this JSON object (no markdown fences, no extra text):
{{"year":<int>,"make":"<UPPERCASE>","model":"<canonical>","trim":"<specific>","body_style":"<Coupe|Cabriolet|Convertible|Sedan|Wagon|SUV|Hatchback|Targa>","drive_type":"<RWD|AWD|FWD|4WD>","generation":"<e.g. 997.2, 991.1, 992, 970, W205>","confidence":<float 0-1>,"reasoning":"<one short sentence>"}}
"""


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    from anthropic import Anthropic
                    _client = Anthropic()  # picks up ANTHROPIC_API_KEY from env
                except Exception as e:
                    print(f"[claude_vin] init failed: {e}", flush=True)
                    _client = False  # poison so we don't retry init every call
    return _client if _client else None


def _parse_json(txt):
    """Tolerant JSON extraction from Claude response."""
    if not txt:
        return None
    txt = txt.strip()
    if "```" in txt:
        txt = re.sub(r"```(?:json)?\s*|\s*```", "", txt)
    m = _JSON_RE.search(txt)
    if m:
        txt = m.group(0)
    try:
        return json.loads(txt)
    except Exception:
        return None


def decode_vin_via_claude(vin, model=ANTHROPIC_MODEL, timeout=CLAUDE_TIMEOUT_SEC):
    """One-shot Claude VIN decode. Returns dict or None on failure."""
    c = _get_client()
    if not c:
        return None
    try:
        t0 = time.time()
        resp = c.messages.create(
            model=model,
            max_tokens=600,
            timeout=timeout,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _USER_PROMPT_TEMPLATE.format(vin=vin)}],
        )
        latency_ms = int((time.time() - t0) * 1000)
        txt = resp.content[0].text if resp.content else ""
        parsed = _parse_json(txt)
        if not parsed:
            print(f"[claude_vin] {vin}: parse failed | raw={txt[:200]}", flush=True)
            return None
        parsed["_latency_ms"] = latency_ms
        parsed["_raw"] = txt[:1000]
        return parsed
    except Exception as e:
        print(f"[claude_vin] {vin}: API error: {e}", flush=True)
        return None


def _cache_get(vin, db_conn):
    cur = db_conn.cursor()
    cur.execute("""SELECT vin, year, make, model, trim, body_style, drive_type,
                          generation, confidence, source, reasoning
                     FROM vin_decode_cache WHERE vin=%s""", (vin,))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def _cache_put(vin, decoded, source, db_conn):
    cur = db_conn.cursor()
    cur.execute("""
        INSERT INTO vin_decode_cache
          (vin, year, make, model, trim, body_style, drive_type, generation,
           confidence, source, reasoning, raw_response)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (vin) DO UPDATE SET
          year         = COALESCE(EXCLUDED.year, vin_decode_cache.year),
          make         = COALESCE(EXCLUDED.make, vin_decode_cache.make),
          model        = COALESCE(EXCLUDED.model, vin_decode_cache.model),
          trim         = COALESCE(EXCLUDED.trim, vin_decode_cache.trim),
          body_style   = COALESCE(EXCLUDED.body_style, vin_decode_cache.body_style),
          drive_type   = COALESCE(EXCLUDED.drive_type, vin_decode_cache.drive_type),
          generation   = COALESCE(EXCLUDED.generation, vin_decode_cache.generation),
          confidence   = EXCLUDED.confidence,
          source       = EXCLUDED.source,
          reasoning    = EXCLUDED.reasoning,
          raw_response = EXCLUDED.raw_response,
          decoded_at   = NOW()
    """, (
        vin,
        decoded.get("year"),
        (decoded.get("make") or "").upper() or None,
        decoded.get("model"),
        decoded.get("trim"),
        decoded.get("body_style"),
        decoded.get("drive_type"),
        decoded.get("generation"),
        float(decoded.get("confidence", 0.5)),
        source,
        decoded.get("reasoning"),
        json.dumps({"raw": decoded.get("_raw")}) if decoded.get("_raw") else None,
    ))
    db_conn.commit()
    cur.close()


def decode_vin_smart(vin, db_conn, nhtsa_fallback=None):
    """Main entry point. Returns dict with year/make/model/trim/body_style/drive_type/
    generation/confidence/source. Tries cache → Claude → NHTSA fallback."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()

    # 1. Cache hit
    cached = _cache_get(vin, db_conn)
    if cached:
        cached["source"] = cached.get("source") or "cache"
        cached["_cache_hit"] = True
        return cached

    # 2. Claude call
    decoded = decode_vin_via_claude(vin)
    if decoded and float(decoded.get("confidence", 0)) >= 0.7:
        _cache_put(vin, decoded, "claude_sonnet_4_6", db_conn)
        decoded["source"] = "claude_sonnet_4_6"
        return decoded

    # 3. Low-confidence Claude OR Claude failed → NHTSA fallback
    if nhtsa_fallback:
        try:
            nh = nhtsa_fallback(vin) or {}
            if nh:
                merged = {
                    "year": nh.get("year"),
                    "make": (nh.get("make") or "").upper() or None,
                    "model": nh.get("model"),
                    "trim": nh.get("trim"),
                    "body_style": nh.get("body_style") or nh.get("body_class"),
                    "drive_type": nh.get("drive_type"),
                    "generation": None,
                    "confidence": 0.5,
                    "reasoning": "Claude unavailable or low-confidence — used NHTSA fallback",
                }
                _cache_put(vin, merged, "nhtsa_fallback", db_conn)
                merged["source"] = "nhtsa_fallback"
                return merged
        except Exception as e:
            print(f"[claude_vin] NHTSA fallback failed for {vin}: {e}", flush=True)

    # 4. Claude returned something low-conf, cache it anyway with low-conf source
    if decoded:
        _cache_put(vin, decoded, "claude_low_conf", db_conn)
        decoded["source"] = "claude_low_conf"
        return decoded

    return None


def shadow_log_comparison(vin, bid_id, old_trim, old_source, old_confidence, db_conn):
    """Shadow-mode call: log a comparison to vin_decode_shadow_log without
    modifying any production data. Called from decode_vin_precise_wrapper
    after the legacy decoder has already run + returned its answer.

    Runs in a thread so the intake request doesn't wait on Claude's 7s.
    """
    def _run():
        try:
            t0 = time.time()
            new = decode_vin_smart(vin, db_conn)
            latency_ms = int((time.time() - t0) * 1000)
            if not new:
                return
            agrees = False
            if old_trim and new.get("trim"):
                old_norm = (old_trim or "").lower()
                new_norm = (new.get("trim") or "").lower()
                # Loose match: shared body/trim keyword
                for tok in ["coupe", "cabriolet", "convertible", "sedan",
                            "carrera", "panamera", "gt3", "turbo", "4s", "gts"]:
                    if tok in old_norm and tok in new_norm:
                        agrees = True
                        break

            cur = db_conn.cursor()
            cur.execute("""
                INSERT INTO vin_decode_shadow_log
                    (vin, bid_id, old_trim, old_source, old_confidence,
                     new_trim, new_body, new_drive, new_generation, new_confidence,
                     new_source, agrees, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                vin, bid_id, old_trim, old_source, str(old_confidence) if old_confidence else None,
                new.get("trim"), new.get("body_style"), new.get("drive_type"),
                new.get("generation"),
                float(new.get("confidence", 0)) if new.get("confidence") is not None else None,
                new.get("source"),
                agrees,
                latency_ms,
            ))
            db_conn.commit()
            cur.close()
        except Exception as e:
            print(f"[claude_vin shadow] {vin}: {e}", flush=True)

    # Fire-and-forget thread so the intake request doesn't block
    t = threading.Thread(target=_run, daemon=True)
    t.start()
