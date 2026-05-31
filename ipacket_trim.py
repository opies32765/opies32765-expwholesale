"""ipacket_trim.py — extract canonical trim from iPacket sticker OCR text.

Window stickers have the model+trim in a header line that follows the
year+make. Algorithm:
  1. Find a "YYYY  <MAKE>  <model+trim>" sequence near the top
  2. Strip the bid's known model from the front of the capture
  3. What remains = canonical trim (e.g. "SRT HELLCAT JAILBREAK")

Persists the result to bids.canon_trim + canon_source so downstream
matchers (dealer_match v2) prefer it over bid.trim.

Caches the OCR text in ipacket_lookups.raw_json._ocr_text so we don't
re-OCR. ~3KB per row at 6-7K chars trimmed.
"""
from __future__ import annotations
import os
import re
import json
from typing import Optional


def _get_ocr_text(ipacket: dict) -> Optional[str]:
    """Return cached OCR text from raw_json, or None if not cached."""
    raw = ipacket.get('raw_json') or {}
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception: return None
    if not isinstance(raw, dict):
        return None
    return raw.get('_ocr_text')


# -- IPACKET_OCR_DASHBOARD_GUARD_2026_05_31 -------------------------------
# iPacket screenshots are full_page captures: dashboard-chrome HEADER ->
# inline sticker MIDDLE -> "Recent Sticker Pulls" FOOTER. Vision reads
# top-down so OCR leads with chrome even when the sticker is below it in the
# SAME image. Slice to the sticker region; None for pure-dashboard captures.
_IPKT_CHROME_MARKERS = (
    'DASHBOARD INVENTORY CONTACTS', 'WINDOW STICKER/BUILDSHEET LOOKUP',
    'ENTER YOUR 17-CHARACTER', 'ENTER YOUR 17 CHARACTER',
)
_IPKT_STICKER_ANCHORS = (
    'TOTAL SUGGESTED', 'SUGGESTED RETAIL', 'AS DELIVERED', 'TOTAL PRICE',
    'M.S.R.P', 'MSRP', 'STANDARD EQUIPMENT', 'STANDARD OPTIONS',
    'BASE PRICE', 'DESTINATION CHARGE', 'MONRONEY',
)
_IPKT_FOOTER_MARKERS = ('RECENT STICKER PULLS', 'STICKER PULL ARCHIVED')


def _clean_sticker_ocr(txt):
    """Slice the iPacket dashboard chrome HEADER off OCR text, KEEPING the
    sticker header (year/make/model/trim) + body. Return None when there is no
    real sticker (recent-pulls list / pure dashboard). Pure string ops."""
    if not txt:
        return txt
    up = txt.upper()
    if not any(m in up for m in _IPKT_CHROME_MARKERS):
        return txt  # no chrome -> real sticker, untouched
    # Start right AFTER the lookup-prompt chrome so the sticker header (which
    # carries the trim) survives. Chrome ends "...Vehicle Identification
    # Number"; fall back to the latest chrome marker end.
    start = 0
    j = up.find('IDENTIFICATION NUMBER')
    if j != -1:
        start = j + len('IDENTIFICATION NUMBER')
    else:
        for mk in _IPKT_CHROME_MARKERS:
            i = up.find(mk)
            if i != -1:
                start = max(start, i + len(mk))
    sliced = txt[start:].strip()
    su = sliced.upper()
    # Require a real Monroney price anchor; else it's recent-pulls / pure
    # dashboard -> nothing usable (better blank than chrome).
    if not any(a in su for a in _IPKT_STICKER_ANCHORS):
        return None
    fcut = len(sliced)
    for fm in _IPKT_FOOTER_MARKERS:
        k = su.find(fm)
        if k != -1 and k < fcut:
            fcut = k
    sliced = sliced[:fcut].strip()
    return sliced or None


def _persist_ocr_text(conn, bid_id, text):
    try:
        cur = conn.cursor()
        cur.execute("UPDATE ipacket_lookups SET raw_json = COALESCE(raw_json,'{}'::jsonb) || %s::jsonb WHERE bid_id=%s",
                    (json.dumps({'_ocr_text': text[:8000]}), bid_id))
        conn.commit()
    except Exception as e:
        print('[ipacket_trim] _persist_ocr_text err bid=%s: %s' % (bid_id, e), flush=True)
        try: conn.rollback()
        except Exception: pass


def _clear_ocr_text(conn, bid_id):
    try:
        cur = conn.cursor()
        cur.execute("UPDATE ipacket_lookups SET raw_json = COALESCE(raw_json,'{}'::jsonb) - '_ocr_text' WHERE bid_id=%s", (bid_id,))
        conn.commit()
    except Exception as e:
        print('[ipacket_trim] _clear_ocr_text err bid=%s: %s' % (bid_id, e), flush=True)
        try: conn.rollback()
        except Exception: pass


def ensure_ipacket_ocr_cached(bid_id: int, ipacket: dict, conn) -> Optional[str]:
    """Idempotently ensure OCR text is cached for this iPacket sticker.

    Returns the OCR text (cached or freshly extracted) or None when no
    sticker exists. Always persists to ipacket_lookups.raw_json._ocr_text
    when freshly OCR'd. Safe to call from any path (ipacket submit,
    accutrade overseer, assessment, etc) — only does work when needed.

    Added 2026-05-18 to support evidence-first AccuTrade trim selection.
    """
    if not ipacket:
        return None
    text = _get_ocr_text(ipacket)
    if text:
        # IPACKET_OCR_DASHBOARD_GUARD_2026_05_31: clean + self-heal stored chrome
        cleaned = _clean_sticker_ocr(text)
        if cleaned == text:
            return text
        if cleaned:
            _persist_ocr_text(conn, bid_id, cleaned)
            return cleaned
        _clear_ocr_text(conn, bid_id)  # pure dashboard -> drop, re-OCR below
        text = None
    ss = ipacket.get('screenshot')
    if not ss:
        return None
    path = ss
    if path.startswith('/ipacket_reports/'):
        path = '/opt/expwholesale' + path
    text = _ocr_screenshot(path)
    if not text:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE ipacket_lookups
               SET raw_json = COALESCE(raw_json, '{}'::jsonb) || %s::jsonb
             WHERE bid_id = %s
        """, (json.dumps({'_ocr_text': text[:8000]}), bid_id))
        conn.commit()
    except Exception as e:
        print(f'[ensure_ipacket_ocr_cached] persist err bid={bid_id}: {e}', flush=True)
        try: conn.rollback()
        except Exception: pass
    return text


def _ocr_screenshot(screenshot_path: str) -> Optional[str]:
    """Force-OCR an iPacket screenshot via Google Vision."""
    if not screenshot_path or not os.path.exists(screenshot_path):
        return None
    try:
        from app import _google_vision_ocr
    except Exception:
        return None
    try:
        with open(screenshot_path, 'rb') as f:
            img = f.read()
        return _clean_sticker_ocr(_google_vision_ocr(img))
    except Exception as e:
        print(f'[ipacket_trim] OCR err on {screenshot_path}: {e}', flush=True)
        return None


def _extract_trim_from_text(text: str, make: str, model: str) -> Optional[str]:
    """Find the trim string from a sticker's OCR text.

    Pattern: a line containing the make followed by the model and additional
    trim tokens. We anchor on make (always present in caps on stickers) and
    take everything after model up to the end of the line / first sentence.
    """
    if not text or not make or not model:
        return None
    text = text.replace('\r', '\n')
    mk = re.escape(make.upper().strip())
    md = re.escape(model.upper().strip())
    # Allow a year-line above and the make-line below (sometimes split):
    #   2023
    #   CHALLENGER SRT HELLCAT JAILBREAK
    # or single-line "DODGE CHALLENGER SRT HELLCAT JAILBREAK"
    patterns = [
        # "<MAKE> <MODEL> <trim...>" on one line
        rf'\b{mk}\s+{md}\s+([A-Z][A-Z0-9\s/&-]{{2,80}})(?=\s*$|\s*\n)',
        # year-line followed by "<MODEL> <trim...>" (Dodge sticker pattern)
        rf'\b\d{{4}}\s*\n\s*{md}\s+([A-Z][A-Z0-9\s/&-]{{2,80}})(?=\s*$|\s*\n)',
        # Stand-alone "<MODEL> <trim...>" on a header-like line
        rf'\n\s*{md}\s+([A-Z][A-Z0-9\s/&-]{{2,80}})(?=\s*$|\s*\n)',
    ]
    candidates = []
    for pat in patterns:
        for m in re.finditer(pat, text.upper()):
            cap = m.group(1).strip()
            # Strip noise body words and obvious labels
            cap = re.sub(r'\s+(SPORT UTILITY VEHICLE|PICKUP TRUCK|SEDAN|COUPE|'
                         r'CONVERTIBLE|HATCHBACK|WAGON|SUV|HYBRID|PHEV|EV)\s*$',
                         '', cap, flags=re.I)
            cap = cap.strip()
            # Minimum 2 chars, max 60 (avoid grabbing legalese paragraphs)
            if 2 <= len(cap) <= 60:
                candidates.append(cap)
    if not candidates:
        return None
    # Choose the first candidate (usually the cleanest header line) but tie-
    # break by length — we want the most specific (longest) reasonable match.
    candidates.sort(key=lambda s: (len(s.split()), -len(s)))  # fewest tokens, longest str
    return candidates[0]


def extract_and_persist(bid_id: int, make: str, model: str, ipacket: dict,
                        conn, force_ocr: bool = False) -> Optional[str]:
    """Main entry: get sticker text → extract trim → write to bids.canon_trim.

    Returns the extracted trim (or None if not found).
    Side effects: UPDATEs bids.canon_trim + canon_source if a trim was found,
    and caches the OCR text in ipacket_lookups.raw_json._ocr_text.
    """
    if not (bid_id and make and model and ipacket):
        return None

    # 1) Try cached OCR text
    text = _get_ocr_text(ipacket)
    cached_in_db = bool(text)

    # 2) If no cached text, force-OCR (only if force_ocr=True, to control cost)
    if not text and force_ocr:
        ss = ipacket.get('screenshot')
        if ss:
            # screenshot column is a URL like /ipacket_reports/foo.png — translate
            path = ss
            if path.startswith('/ipacket_reports/'):
                path = '/opt/expwholesale' + path
            text = _ocr_screenshot(path)

    if not text:
        return None

    # 3) Extract trim
    trim = _extract_trim_from_text(text, make, model)
    if not trim:
        return None

    # Normalize: collapse whitespace, title-case
    trim = re.sub(r'\s+', ' ', trim.strip())
    trim_titled = ' '.join(w.capitalize() for w in trim.split())

    # 4) Persist
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE bids
               SET canon_trim = %s,
                   canon_source = COALESCE(canon_source, '') ||
                                  CASE WHEN canon_source IS NULL OR canon_source=''
                                       THEN 'ipacket'
                                       ELSE ',ipacket' END,
                   canon_decoded_at = NOW()
             WHERE id = %s
               AND (canon_trim IS NULL OR canon_trim <> %s)
        """, (trim_titled, bid_id, trim_titled))

        # Cache OCR text if we just freshly OCR'd
        if not cached_in_db:
            cur.execute("""
                UPDATE ipacket_lookups
                   SET raw_json = COALESCE(raw_json, '{}'::jsonb) || %s::jsonb
                 WHERE bid_id = %s
            """, (json.dumps({'_ocr_text': text[:8000]}), bid_id))
        conn.commit()
    except Exception as e:
        print(f'[ipacket_trim] persist err bid={bid_id}: {e}', flush=True)
        try: conn.rollback()
        except Exception: pass
        return None

    return trim_titled


# Self-test
if __name__ == '__main__':
    sample = """2 DODGE
2023
CHALLENGER SRT HELLCAT JAILBREAK
THIS VEHICLE IS MANUFACTURED TO MEET..."""
    print('test 1 (year split):',
          _extract_trim_from_text(sample, 'DODGE', 'Challenger'))

    sample2 = "DODGE CHALLENGER SRT HELLCAT REDEYE WIDEBODY\nMSRP..."
    print('test 2 (single line):',
          _extract_trim_from_text(sample2, 'DODGE', 'Challenger'))

    sample3 = "2024 PORSCHE 911 GT3 RS\n..."
    print('test 3 (porsche):',
          _extract_trim_from_text(sample3, 'PORSCHE', '911'))
