"""porsche_options.py — option-similarity enrichment for Porsche arb scanner.

Goal: extract a canonical option set per VIN so the arb pipeline can compute
LIKE-for-LIKE comp sets (filtering noise from stripper-vs-loaded comparisons).

Data sources tried (2026-05-26):
  1. vAuto priceGuides API (kbb / kbbOnline / blackBook / manheim blocks)
     — VERIFIED EMPTY for Porsches. Returns only Condition + Style fields
     (2 fields each, e.g. ["Excellent", "VeryGood", "Good", "Fair"] and
     ["Carrera 4 GTS Coupe 2D"]). The original Phase-1A audit claim of
     "36-48 named options per Porsche VIN" was INCORRECT. Documented in
     PORSCHE_ARB_PHASE1_DECISIONS.
  2. NHTSA vPIC — returns generic ABS/airbag flags only, no Porsche-
     specific equipment.
  3. Porsche.com — find-your-porsche/ URL returns 404. Configurator and
     finder.porsche.com require browser auth / are behind Vercel Security
     Checkpoint. NOT VIABLE.
  4. Dealer detail_uri scraping — most URLs are AutoTrader (Akamai-blocked
     to server-side curl) or dealer.com (403). Would require headless
     browser pool + proxies; out of scope for Phase 1.

What we DO have per comp VIN from vAuto competition response:
  - trim (e.g. "Carrera 4 GTS")
  - color, interior_description (free-text, sometimes mentions Race Tex,
    Bordeaux, package codes — useful as proxy signals)
  - engine string (e.g. "3.6L H6 Turbocharged DOHC 24V LEV3-ULEV70")
  - transmission (e.g. "8-Speed Porsche Doppelkupplung (PDK)" vs Manual)
  - drivetrain (RWD/AWD)
  - is_certified (CPO Y/N)
  - mileage band (effectively trim of a high-mileage example vs low)

Pragmatic Phase 1A implementation:
  - Try vAuto priceGuides first (kept in case Cox enables equipment data
    for our entity tier in future).
  - Always extract signal-based canonical options from the comp_row itself.
    This is the workhorse source. We can't tell sport_chrono from PCCB,
    but we CAN tell PDK vs Manual, AWD vs RWD, and that's where the
    biggest "Carrera vs Carrera 4 GTS" noise has been coming from.
  - Store both sources keyed by (subject_vin, source) — if Cox ever fixes
    the equipment endpoint, the kbb_equipment row supersedes comp_row_signals.

Canonical option set (high-impact Porsche options):
  pccb sport_chrono pdcc pasm burmester bose carbon_roof front_lift
  sport_exhaust pts_paint full_leather clubsport weissach lightweight_package
  premium_plus adaptive_cruise lane_keep heads_up_display sport_seats_plus
  bucket_seats cpo manual_transmission

For comp_row_signals we can only populate:
  cpo (from is_certified)
  manual_transmission (from transmission string)
  awd (additional — added; "Carrera 4 X" vs "Carrera X" is a real $$ delta)
  premium_interior_text (catch-all for fancy interior phrases)

The Jaccard similarity is then computed on whatever options are populated.
Candidates with EMPTY options on both sides match via trim+transmission
+drivetrain equality fallback in build_like_pool().
"""
from __future__ import annotations

import re
from typing import Optional


# ── Canonical option map ──────────────────────────────────────────────
# Maps a string token (case-insensitive substring) to its canonical key.
# Used by _classify_option_text to walk KBB/equipment lists if/when Cox
# ever returns one for Porsches. Kept short and high-precision.
CANONICAL_OPTION_MAP: list[tuple[str, str]] = [
    # (substring_match, canonical_key)
    ('porsche ceramic composite brake', 'pccb'),
    ('pccb', 'pccb'),
    ('ceramic composite', 'pccb'),

    ('sport chrono', 'sport_chrono'),
    ('chrono package', 'sport_chrono'),

    ('porsche dynamic chassis control', 'pdcc'),
    ('pdcc', 'pdcc'),

    ('porsche active suspension management', 'pasm'),
    ('active suspension management', 'pasm'),
    ('pasm', 'pasm'),

    ('burmester', 'burmester'),
    ('bose', 'bose'),

    ('carbon fiber roof', 'carbon_roof'),
    ('carbon roof', 'carbon_roof'),

    ('front axle lift', 'front_lift'),
    ('nose lift', 'front_lift'),
    ('lift system', 'front_lift'),

    ('sport exhaust', 'sport_exhaust'),
    ('sports exhaust', 'sport_exhaust'),

    ('paint to sample', 'pts_paint'),
    ('paint-to-sample', 'pts_paint'),
    ('pts paint', 'pts_paint'),

    ('full leather', 'full_leather'),
    ('extended leather', 'full_leather'),
    ('club leather', 'full_leather'),

    ('clubsport package', 'clubsport'),
    ('club sport package', 'clubsport'),
    ('weissach package', 'weissach'),
    ('lightweight sport package', 'lightweight_package'),
    ('lightweight package', 'lightweight_package'),
    ('premium package plus', 'premium_plus'),

    ('adaptive cruise', 'adaptive_cruise'),
    ('lane keep', 'lane_keep'),
    ('active lane keep', 'lane_keep'),

    ('head-up display', 'heads_up_display'),
    ('head up display', 'heads_up_display'),
    ('hud', 'heads_up_display'),

    ('sport seats plus', 'sport_seats_plus'),
    ('18-way', 'sport_seats_plus'),

    ('carbon bucket seat', 'bucket_seats'),
    ('full bucket seat', 'bucket_seats'),
    ('carbon-bucket', 'bucket_seats'),
]

# Options considered "high-impact" for the option-similarity Jaccard.
# Adaptive cruise etc. is a $400 option and shouldn't dominate the
# similarity score the way PCCB ($9K) or PTS ($14K) should.
HIGH_IMPACT_OPTIONS = {
    'pccb', 'sport_chrono', 'pdcc', 'pasm', 'burmester',
    'carbon_roof', 'front_lift', 'sport_exhaust', 'pts_paint',
    'full_leather', 'clubsport', 'weissach', 'lightweight_package',
    'bucket_seats', 'manual_transmission',
}


def _classify_option_text(text: str) -> Optional[str]:
    """Map a single option description into a canonical key. Returns None
    when no high-precision match is found."""
    if not text:
        return None
    lo = text.lower()
    for needle, key in CANONICAL_OPTION_MAP:
        if needle in lo:
            return key
    return None


def canonicalize_kbb_fields(kbb_fields: list[dict]) -> tuple[dict, list[dict]]:
    """Walk a vAuto priceGuides->kbb->fields[] structure and pull out
    canonical options. Returns (canonical_dict, raw_selected_options).

    In practice for Porsches the kbb block only has 2 fields (Condition +
    Style) and NO equipment options come back. This is a no-op for
    Porsches today, kept as scaffolding in case Cox enables equipment
    data on our entity tier.
    """
    canonical: dict = {}
    raw: list[dict] = []
    for f in kbb_fields or []:
        for opt in (f.get('fieldOptions') or []):
            if not opt.get('isSelected') or opt.get('isInverse'):
                continue
            desc = opt.get('optionTranslatedDescription') or ''
            key = _classify_option_text(desc)
            if key:
                canonical[key] = True
                raw.append({'desc': desc, 'optionId': opt.get('optionId'),
                            'canonical': key})
    return canonical, raw


# ── Comp-row signal extraction (the actual workhorse) ─────────────────

_MANUAL_TRANS_RE = re.compile(r'\b(manual|6-?speed manual|7-?speed manual|stick|3-?pedal)\b', re.I)
_PDK_RE = re.compile(r'\b(pdk|doppelkupplung|dual.?clutch|tiptronic)\b', re.I)

# Substrings in interior_description that signal premium upgrades. We
# can't tell WHICH premium option, so we lump them under
# premium_interior_text rather than guessing pccb/etc.
_PREMIUM_INTERIOR_HINTS = (
    'race-tex', 'race tex', 'racetex',  # Race-Tex Alcantara — Sport Chrono/Sport interior bundle
    'bordeaux', 'guards red',            # Premium Heritage interior or PTS-adjacent
    'cognac', 'basalt',
    'club leather', 'extended leather', 'full leather',
    'tartan',                            # Heritage Design Edition only
    'leather stitching', 'contrast stitching',
)


def extract_comp_row_options(comp_row: dict) -> dict:
    """Read a vAuto comp_row and emit canonical option flags from the
    structured fields we DO have. This is the realistic Phase 1A source.

    Returns a dict like:
      {'cpo': True, 'awd': True, 'manual_transmission': False,
       'pdk_transmission': True, 'premium_interior_text': True}
    """
    out: dict = {}

    if comp_row.get('is_certified') or comp_row.get('isCertified'):
        out['cpo'] = True

    drivetrain = (comp_row.get('drivetrain') or comp_row.get('driveTrain') or '').strip().upper()
    if drivetrain == 'AWD':
        out['awd'] = True
    elif drivetrain == 'RWD':
        out['rwd'] = True

    trans = (comp_row.get('transmission') or '').strip()
    if trans:
        if _MANUAL_TRANS_RE.search(trans):
            out['manual_transmission'] = True
        elif _PDK_RE.search(trans):
            out['pdk_transmission'] = True

    interior = (comp_row.get('interior') or comp_row.get('interiorDescription') or '').strip()
    if interior:
        lo = interior.lower()
        if any(h in lo for h in _PREMIUM_INTERIOR_HINTS):
            out['premium_interior_text'] = True

    # color → flag PTS-suggestive colors only. Stock colors are noisy
    # enough that they're not a signal.
    color = (comp_row.get('color') or '').strip().lower()
    if color and color not in ('black', 'white', 'silver', 'gray', 'grey',
                                'red', 'blue', 'yellow', 'green', 'orange'):
        # Non-standard color name — likely a PTS or special color
        out['nonstandard_color'] = True

    return out


def extract_vehicleinfo_options(vehicle: dict, anchor_meta: dict | None = None) -> dict:
    """Extract canonical option flags from a vAuto vehicleInfo decode
    response (the dict returned by /api/appraisal/vehicleInfo).

    vehicleInfo has: transmissionType (Automatic/Manual), engineFuelType
    (Gasoline/Hybrid/Electric), bodyType, etc. We can't tell drivetrain
    from here for Porsches because vAuto doesn't decode it; instead infer
    from anchor_meta['trim'] when it contains 'Carrera 4' (= AWD).
    """
    out: dict = {}
    if not vehicle:
        return out

    trans_type = (vehicle.get('transmissionType') or '').strip().lower()
    if 'manual' in trans_type:
        out['manual_transmission'] = True
    elif 'automatic' in trans_type or 'pdk' in trans_type:
        out['pdk_transmission'] = True

    # Drivetrain inference from trim: '... 4 ...' or 'Carrera 4 ...' means AWD.
    # 'Turbo' / 'Turbo S' is also AWD by default. 'GT3', '911 R', plain
    # 'Carrera' (no 4) is RWD.
    trim = (anchor_meta or {}).get('trim') or vehicle.get('trim') or ''
    trim_lo = trim.lower()
    if 'carrera 4' in trim_lo or 'turbo' in trim_lo or 'targa 4' in trim_lo or 'gt2' in trim_lo:
        out['awd'] = True
    elif 'gt3' in trim_lo or 'gt4' in trim_lo or trim_lo.startswith('carrera ') or trim_lo == 'carrera':
        out['rwd'] = True

    return out


# ── Jaccard / similarity ─────────────────────────────────────────────

def option_jaccard(a: dict, b: dict, high_impact_only: bool = True) -> float:
    """Compute Jaccard similarity over option sets.

    If high_impact_only=True (default), restricts to HIGH_IMPACT_OPTIONS
    + the comp-row signals (cpo / awd / manual_transmission etc.) that
    materially affect Porsche value. Otherwise uses all keys.

    Returns 1.0 when both sets are empty (a 'we have no data either way'
    match — better than returning 0 which would penalize EVERY comp).
    """
    keys_a = {k for k, v in (a or {}).items() if v}
    keys_b = {k for k, v in (b or {}).items() if v}
    if high_impact_only:
        # Always include the comp-row signal keys — they're the actual
        # data we have today; HIGH_IMPACT_OPTIONS is the audit's ideal.
        allowed = HIGH_IMPACT_OPTIONS | {
            'cpo', 'awd', 'rwd', 'manual_transmission', 'pdk_transmission',
            'premium_interior_text', 'nonstandard_color',
        }
        keys_a &= allowed
        keys_b &= allowed
    union = keys_a | keys_b
    if not union:
        # Both empty — treat as a perfect match (no info to discriminate).
        # This makes Jaccard a non-issue when neither side has option data.
        return 1.0
    intersection = keys_a & keys_b
    return round(len(intersection) / len(union), 3)


def merge_option_sets(*sources: dict) -> dict:
    """OR-merge multiple option dicts. Later sources don't overwrite True
    values from earlier ones — once True, always True."""
    out: dict = {}
    for s in sources:
        if not s:
            continue
        for k, v in s.items():
            if v:
                out[k] = True
    return out
