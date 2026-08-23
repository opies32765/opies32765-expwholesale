"""bulk_upload.py — parse xlsx/csv "needs to go" lists from dealers.

Dealers send EW spreadsheets of vehicles they want off the lot. This module
turns one of those sheets into a normalized list of bid candidates that the
operator can preview, edit, then bulk-create.

The parser tolerates:
  - column reordering (header-based mapping, case + whitespace insensitive)
  - extra/blank columns and trailing junk rows
  - leading blank rows and blank separator rows between records
  - Unicode noise (replacement chars from cp1252 round-trips)
  - missing fields (any single column except VIN can be absent)
  - no-header sheets (heuristic content-based column inference)
  - shorthand money values ("235k" → 235000, bare 240 → 240000 when the
    column context shows it's in $thousands)

Output: list of dicts with keys
    vin, year, make, model, trim, body, color, mileage,
    asking_price, stock, notes, raw_vehicle, vin_check_digit_valid
The caller decides what to keep, edit, or insert.
"""
from __future__ import annotations
import csv
import io
import re
import unicodedata


# Header name → canonical field. All keys lowercased + stripped before lookup.
_HEADER_MAP = {
    'vehicle': 'raw_vehicle',
    'description': 'raw_vehicle',
    'year/make/model': 'raw_vehicle',
    # BULK_UPLOAD_HEADER_ALIASES_2026_05_18: additional YMM variants seen
    # on dealer printed-report exports.
    'year make model': 'raw_vehicle',
    'year, make, model': 'raw_vehicle',
    'year - make - model': 'raw_vehicle',
    'year-make-model': 'raw_vehicle',
    'yr/make/model': 'raw_vehicle',
    'yr make model': 'raw_vehicle',
    'yr/mk/md': 'raw_vehicle',
    'vehicle description': 'raw_vehicle',
    'ymm': 'raw_vehicle',
    'year': 'year_col',
    'make': 'make_col',
    'model': 'model_col',
    'trim': 'trim_col',
    'stock': 'stock',
    'stock #': 'stock',
    'stock#': 'stock',
    'stocknumber': 'stock',
    'stock number': 'stock',
    'stk': 'stock',
    'stk#': 'stock',
    'stk #': 'stock',
    'vin': 'vin',
    'vin#': 'vin',
    'vin number': 'vin',
    'vin no': 'vin',
    # combined columns: dealers often jam mileage + VIN into one cell
    'miles/vin': 'miles_vin',
    'miles / vin': 'miles_vin',
    'vin/miles': 'miles_vin',
    'vin / miles': 'miles_vin',
    'mileage/vin': 'miles_vin',
    'odometer/vin': 'miles_vin',
    'miles vin': 'miles_vin',
    'vin/odometer': 'miles_vin',
    'body': 'body',
    'body style': 'body',
    'color': 'color',
    'exterior color': 'color',
    'ext color': 'color',
    'cost': 'cost',
    'price': 'asking_price',
    'asking': 'asking_price',
    'asking price': 'asking_price',
    'ask': 'asking_price',
    'list': 'asking_price',
    'list price': 'asking_price',
    'wholesale': 'asking_price',
    'wholesale price': 'asking_price',
    'odometer': 'mileage',
    'mileage': 'mileage',
    'miles': 'mileage',
    'km': 'mileage',
    # ODO_ALIASES_2026_08_23: short forms seen on real dealer sheets. A
    # header we fail to recognise silently DISCARDS that column -- the Palm
    # Bay sheet said "ODO" and all ten odometers were dropped.
    'odo': 'mileage',
    'odo.': 'mileage',
    'odom': 'mileage',
    'odom.': 'mileage',
    'odometer reading': 'mileage',
    'mi': 'mileage',
    'mi.': 'mileage',
    'miles (k)': 'mileage',
    'mileage (mi)': 'mileage',
    'current miles': 'mileage',
    'actual miles': 'mileage',
    'kms': 'mileage',
    'kilometers': 'mileage',
    'vrank description': 'notes',
    'notes': 'notes',
    'comments': 'notes',
    'condition': 'notes',
    'condition notes': 'notes',
    'damage': 'notes',
    'status': 'notes',
    'buyer': 'notes',
}

# Two-word makes the heuristic split needs to keep together.
_TWO_WORD_MAKES = {
    'mercedes-benz', 'mercedes benz',
    'aston martin',
    'land rover', 'range rover',  # Range Rover often appears as make in lists
    'alfa romeo',
    'rolls-royce', 'rolls royce',
}

_YEAR_RE = re.compile(r'^\s*(19\d{2}|20\d{2})\b\s*(.+)$')
# TWO_DIGIT_YEAR_2026_05_30: dealer lists often lead with a 2-digit model year
# ("23 HONDA PILOT TOURING"). Require 2 digits + space + an alpha make so we
# never misfire on "1500 SILVERADO" (no \b inside the number) or "4RUNNER".
_YEAR2_RE = re.compile(r'^\s*(\d{2})\s+([A-Za-z].*)$')
_YEAR_ANY_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')
_VIN_RE = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')


# REAL_CHECKDIGIT_2026_08_23 — ISO 3779 check digit. Mirrors
# app.py:vin_check_digit_valid(); duplicated because this module must not
# import app.py. Used to RANK parse strategies, never to reject a VIN.
_VIN_TRANS = {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6, 'G': 7, 'H': 8,
              'J': 1, 'K': 2, 'L': 3, 'M': 4, 'N': 5, 'P': 7, 'R': 9,
              'S': 2, 'T': 3, 'U': 4, 'V': 5, 'W': 6, 'X': 7, 'Y': 8, 'Z': 9,
              '0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
              '7': 7, '8': 8, '9': 9}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_check_digit_ok(vin: str) -> bool:
    """True when the 9th character checks out against the other sixteen."""
    vin = (vin or '').upper()
    if len(vin) != 17:
        return False
    try:
        total = sum(_VIN_TRANS[c] * w
                    for c, w in zip(vin, _VIN_WEIGHTS))
    except KeyError:
        return False
    expected = total % 11
    return vin[8] == ('X' if expected == 10 else str(expected))


def _clean(value) -> str:
    """Coerce a cell to a stripped str, replacing Unicode noise."""
    if value is None:
        return ''
    s = str(value)
    # Normalize + drop replacement char (often from cp1252 → utf-8 mismangle)
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('�', ' ')
    # Collapse weird trademark/registered symbols to nothing — dealers often
    # paste "AMG® 4MATIC®" and "®" survives normalization.
    s = re.sub(r'[®™]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _parse_mileage(value) -> int | None:
    s = _clean(value).lower().replace(',', '').replace('mi', '').strip()
    if not s:
        return None
    # "8786.0" / "8,786" / "8786 mi" / "" handled
    # Allow shorthand "235k" for mileage too (used in some dealer sheets)
    if s.endswith('k'):
        try:
            n = int(float(s[:-1]) * 1000)
            if 0 <= n <= 9_999_999:
                return n
        except ValueError:
            return None
    try:
        n = int(float(s))
        if 0 <= n <= 9_999_999:
            return n
    except ValueError:
        pass
    return None


def _parse_money(value, force_thousands: bool = False) -> int | None:
    """Parse a money cell. If force_thousands, bare integers <1000 are
    treated as $thousands (e.g., 240 → 240000)."""
    s = _clean(value).lower().replace('$', '').replace(',', '').strip()
    if not s:
        return None
    # Allow shorthand like "889k" / "235k" / "29.5k"
    if s.endswith('k'):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            return None
    try:
        n = float(s)
    except ValueError:
        return None
    # Decimal short form: 29.5 means $29,500
    if 0 < n < 1000 and (force_thousands or '.' in s):
        return int(round(n * 1000))
    n_int = int(n)
    if 0 <= n_int <= 99_999_999:
        return n_int
    return None


def _find_vin_in(value) -> str:
    """Find a 17-char VIN ANYWHERE inside a cell. Handles combined cells like a
    'MILES/VIN' column ('69,235 3TMCZ5AN5JM176XX' -> the VIN). Returns '' if none."""
    s = _clean(value).upper()
    if not s:
        return ''
    whole = s.replace(' ', '').replace('-', '')
    if _VIN_RE.match(whole):
        return whole
    for tok in re.split(r'[^A-HJ-NPR-Z0-9]+', s):
        if _VIN_RE.match(tok):
            return tok
    return ''


def _miles_beside_vin(value, vin: str) -> str:
    """From a combined 'miles vin' cell, return the leftover numeric (miles)."""
    s = _clean(value)
    if vin:
        s = re.sub(re.escape(vin), ' ', s, flags=re.I)
    m = re.search(r'\d[\d,]*', s)
    return m.group(0) if m else ''


def split_vehicle_string(s: str) -> tuple[int | None, str, str, str]:
    """Heuristic split of a free-form "2023 BMW M8 Competition" cell.

    Returns (year, make, model, trim). Any/all may be None/'' — the canon
    pipeline (NHTSA + VIN-prefix) is the source of truth downstream; this
    is just for display and as a hint to the assessment prompt.
    """
    raw = _clean(s)
    if not raw:
        return None, '', '', ''
    m = _YEAR_RE.match(raw)
    if m:
        year = int(m.group(1))
        rest = m.group(2).strip()
    else:
        # TWO_DIGIT_YEAR_2026_05_30: 2-digit model year fallback.
        m2 = _YEAR2_RE.match(raw)
        if not m2:
            # No year prefix — give up and dump everything into model
            return None, '', raw, ''
        _yy = int(m2.group(1))
        year = 2000 + _yy if _yy <= 49 else 1900 + _yy
        rest = m2.group(2).strip()

    # Try 2-word make first
    lo = rest.lower()
    matched_make = None
    for tw in _TWO_WORD_MAKES:
        if lo.startswith(tw + ' '):
            matched_make = rest[:len(tw)]
            rest = rest[len(tw):].strip()
            break
    if not matched_make:
        # First word is make
        parts = rest.split(' ', 1)
        matched_make = parts[0]
        rest = parts[1] if len(parts) > 1 else ''

    # Next word is model; everything else is trim
    if rest:
        parts = rest.split(' ', 1)
        model = parts[0]
        trim = parts[1] if len(parts) > 1 else ''
    else:
        model = ''
        trim = ''
    return year, matched_make, model, trim


def _normalize_headers(headers: list) -> list[str | None]:
    """Map a header row to canonical field names. Unrecognized cols → None."""
    out: list[str | None] = []
    # BULK_UPLOAD_DEMOTE_DUP_2026_05_18: when a sheet has TWO columns that
    # both map to 'raw_vehicle' (most common pattern: "YEAR MAKE MODEL" +
    # "DESCRIPTION"), keep the first as the YMM source and demote the
    # second to 'notes' so dealer comments are preserved instead of
    # silently dropped by setdefault.
    seen_raw_vehicle = False
    for h in headers:
        key = _clean(h).lower()
        canon = _HEADER_MAP.get(key)
        if canon == 'raw_vehicle':
            if seen_raw_vehicle:
                canon = 'notes'
            else:
                seen_raw_vehicle = True
        out.append(canon)
    return out


def _header_score(headers: list[str | None]) -> int:
    """Count how many strong header columns were recognized."""
    strong = {h for h in headers
              if h in ('vin', 'miles_vin', 'raw_vehicle', 'stock', 'mileage',
                       'asking_price', 'cost', 'model_col')}
    return len(strong)


def _row_to_record(headers: list[str | None], row: tuple) -> dict | None:
    """Build a record dict from a single sheet row. Returns None if blank."""
    rec: dict = {}
    seen_any = False
    # BULK_UPLOAD_TRUNCATION_FIX_2026_05_18 (C): track any unmapped
    # column whose value is a 2-digit integer 0..99 — likely a year
    # shorthand column with a blank header (common in printed dealer
    # report exports). Used by _finalize_record as a year fallback.
    two_digit_year_hint = None
    for i, col in enumerate(headers):
        if i >= len(row):
            continue
        val = row[i]
        if val not in (None, ''):
            seen_any = True
        if col:
            rec.setdefault(col, val)  # keep first hit if dup header
        else:
            # Unmapped column — check for 2-digit year hint
            if two_digit_year_hint is None:
                try:
                    n = int(val) if val is not None else None
                    if n is not None and 0 <= n <= 99:
                        two_digit_year_hint = n
                except (ValueError, TypeError):
                    pass
    if not seen_any:
        return None
    if two_digit_year_hint is not None:
        rec.setdefault('_year_hint_2digit', two_digit_year_hint)
    return rec


def _finalize_record(rec: dict) -> dict:
    """Normalize a raw row dict into the canonical bid-candidate shape."""
    vin = _find_vin_in(rec.get('vin'))
    if not vin and rec.get('miles_vin'):                  # combined MILES/VIN column
        vin = _find_vin_in(rec.get('miles_vin'))
    if not vin:                                           # last resort: VIN hiding in any cell
        for _v in rec.values():
            vin = _find_vin_in(_v)
            if vin:
                break
    raw_vehicle = _clean(rec.get('raw_vehicle'))
    year, make, model, trim = split_vehicle_string(raw_vehicle)

    # BULK_UPLOAD_TRUNCATION_FIX_2026_05_18 (A): detect notes-leak.
    # If raw_vehicle came from a DESCRIPTION column that's actually
    # free-text notes (no year prefix, contains noise tokens, or just
    # too long for a real YMM string), reroute it to notes BEFORE the
    # downstream model field gets stuffed with 50+ chars of options
    # text. Sample offender: "COGNITO LIFT FOX SHOCKS COLOR MATCH
    # BUMPERS WOW FACTO!!!! ASK FOR PICS" (70 chars).
    if raw_vehicle and not year:
        rv_upper = raw_vehicle.upper()
        looks_like_notes = (
            '!' in raw_vehicle
            or '$' in raw_vehicle
            or ' PKG' in rv_upper
            or ' WPKG' in rv_upper
            or ' PACKAGE' in rv_upper
            or ' PKG.' in rv_upper
            or ' LIFTED' in rv_upper
            or ' SHOCKS' in rv_upper
            or ' LEATHER' in rv_upper
            or len(raw_vehicle) > 40
            or len(raw_vehicle.split()) > 6
        )
        if looks_like_notes:
            existing_notes = _clean(rec.get('notes'))
            rec['notes'] = (existing_notes + ' | ' if existing_notes
                            else '') + raw_vehicle
            raw_vehicle = ''
            year = None
            make = ''
            model = ''
            trim = ''

    # Split-column overrides: if year/make/model came as separate columns,
    # prefer those and rebuild raw_vehicle for display.
    if not year and rec.get('year_col'):
        try:
            year = int(_clean(rec['year_col']).split('.')[0])
        except (ValueError, AttributeError):
            year = None
    # BULK_UPLOAD_TRUNCATION_FIX_2026_05_18 (C): 2-digit year shorthand
    # from a blank-header sibling column. 0..49 -> 2000+, 50..99 -> 1900+.
    if not year and rec.get('_year_hint_2digit') is not None:
        try:
            yy = int(rec['_year_hint_2digit'])
            if 0 <= yy <= 49:
                year = 2000 + yy
            elif 50 <= yy <= 99:
                year = 1900 + yy
        except (ValueError, TypeError):
            pass
    if not make and rec.get('make_col'):
        make = _clean(rec['make_col'])
    if not model and rec.get('model_col'):
        model = _clean(rec['model_col'])
    if not trim and rec.get('trim_col'):
        trim = _clean(rec['trim_col'])
    if not raw_vehicle and (year or make or model):
        raw_vehicle = ' '.join(
            p for p in (str(year) if year else '', make, model, trim) if p
        ).strip()

    asking = _parse_money(rec.get('asking_price'))
    cost   = _parse_money(rec.get('cost'))
    # If no asking price column, the "Cost" column is what the dealer paid —
    # use it as a hint for the operator's later asking-price decision but
    # don't put it in asking_price (it's an internal dealer number).

    out = {
        'vin': vin if _VIN_RE.match(vin) else '',
        # REAL_CHECKDIGIT_2026_08_23: was bool(_VIN_RE.match(vin)),
        # which only checked the SHAPE and made the key's name a lie.
        'vin_check_digit_valid': vin_check_digit_ok(vin),
        'raw_vehicle': raw_vehicle,
        'year':  year,
        'make':  make,
        'model': model,
        'trim':  trim,
        'body':  _clean(rec.get('body')),
        'color': _clean(rec.get('color')),
        'mileage': _parse_mileage(
            rec.get('mileage') if rec.get('mileage') not in (None, '')
            else _miles_beside_vin(rec.get('miles_vin'), vin) if rec.get('miles_vin')
            else None),
        'asking_price': asking,
        'dealer_cost': cost,
        'stock': _clean(rec.get('stock')),
        'notes': _clean(rec.get('notes')),
    }
    return out


# ──────────────────────────────────────────────────────────────────────────
# No-header heuristic mode — for sheets with no header row at all, or with
# blank leading rows + a partial label row that doesn't match our map.
# ──────────────────────────────────────────────────────────────────────────

def _is_blank_row(row: tuple) -> bool:
    return not any(_clean(v) for v in row)


def _scan_for_header(rows: list[tuple], max_scan: int = 15):
    """Look at the first `max_scan` non-blank rows and return the first one
    that scores as a real header. Returns (row_index, mapped_headers) or
    None if no header row was found."""
    scanned = 0
    for i, row in enumerate(rows):
        if _is_blank_row(row):
            continue
        mapped = _normalize_headers(list(row))
        if _header_score(mapped) >= 2:
            return i, mapped
        scanned += 1
        if scanned >= max_scan:
            break
    return None


def _classify_cell(s: str) -> str:
    """Classify a single non-empty cell for heuristic column inference.

    Returns one of: 'vin', 'vehicle', 'money_k', 'money_dollar', 'numeric',
    'short_alnum', 'text'."""
    raw = s.strip()
    if _find_vin_in(raw):                 # VIN anywhere, incl. combined 'miles vin' cells
        return 'vin'
    if _YEAR_RE.match(raw):
        return 'vehicle'
    lo = raw.lower().replace(',', '').replace('$', '').strip()
    if lo.endswith('k'):
        try:
            float(lo[:-1])
            return 'money_k'
        except ValueError:
            pass
    if '$' in raw:
        return 'money_dollar'
    # pure number?
    try:
        n = float(lo)
        if '.' in lo:
            return 'money_dollar'  # decimal almost always means $thousands
        if 0 < n < 1_000_000_000:
            return 'numeric'
    except ValueError:
        pass
    # short alphanumeric token with both letters + digits → likely stock #
    if 2 <= len(raw) <= 12 and re.search(r'[A-Za-z]', raw) and re.search(r'\d', raw):
        return 'short_alnum'
    return 'text'


def _infer_column_roles(rows: list[tuple]) -> dict[int, str]:
    """Scan all rows and assign a role to each column index. Roles:
        'vin', 'vehicle', 'stock', 'mileage', 'price_a', 'price_b',
        'price_c', 'notes'.
    Returns dict col_index → role. Columns with no clear role are omitted.
    """
    if not rows:
        return {}
    ncols = max(len(r) for r in rows)
    col_kinds: list[dict[str, int]] = [{} for _ in range(ncols)]
    col_max_int: list[int] = [0] * ncols
    col_has_k: list[bool] = [False] * ncols
    for row in rows:
        for ci in range(ncols):
            val = row[ci] if ci < len(row) else None
            s = _clean(val)
            if not s:
                continue
            kind = _classify_cell(s)
            col_kinds[ci][kind] = col_kinds[ci].get(kind, 0) + 1
            if kind == 'money_k':
                col_has_k[ci] = True
            if kind == 'numeric':
                try:
                    n = int(float(s.replace(',', '').replace('$', '')))
                    if n > col_max_int[ci]:
                        col_max_int[ci] = n
                except ValueError:
                    pass

    roles: dict[int, str] = {}
    # 1) VIN column = column with the most 'vin' classifications
    vin_col = max(range(ncols),
                  key=lambda c: col_kinds[c].get('vin', 0))
    if col_kinds[vin_col].get('vin', 0) == 0:
        return {}  # no VIN anywhere → no anchor
    roles[vin_col] = 'vin'

    # 2) Vehicle column = column with the most 'vehicle' (year-prefixed) hits
    veh_candidates = [c for c in range(ncols)
                      if c not in roles and col_kinds[c].get('vehicle', 0) > 0]
    if veh_candidates:
        veh_col = max(veh_candidates,
                      key=lambda c: col_kinds[c].get('vehicle', 0))
        roles[veh_col] = 'vehicle'

    # 3) Stock column = column with the most short_alnum (letters+digits)
    stock_candidates = [c for c in range(ncols)
                        if c not in roles
                        and col_kinds[c].get('short_alnum', 0) > 0]
    if stock_candidates:
        stock_col = max(stock_candidates,
                        key=lambda c: col_kinds[c].get('short_alnum', 0))
        roles[stock_col] = 'stock'

    # 4) Money columns — two passes so we don't steal the mileage column.
    #    Pass A: columns with at least one EXPLICIT money marker ('235k',
    #    '$25,000', or a decimal like 29.5). Bare-int columns adjacent to
    #    these (and clearly in $thousands shorthand) also qualify.
    explicit_money: list[int] = []
    for c in range(ncols):
        if c in roles:
            continue
        k = col_kinds[c]
        if (k.get('money_k', 0) + k.get('money_dollar', 0)) > 0:
            explicit_money.append(c)
    # Bare-int columns whose entries pair with k-markers (typical pattern in
    # dealer sheets: one column has "235k", the next has "240" meaning $240k)
    if explicit_money:
        for c in range(ncols):
            if c in roles or c in explicit_money:
                continue
            k = col_kinds[c]
            if (k.get('numeric', 0) > 0
                    and col_max_int[c] < 1000
                    and col_has_k[c]):
                explicit_money.append(c)
    money_cols = sorted(explicit_money)

    # 5) Mileage column: prefer a pure-numeric column not yet claimed and
    #    NOT in the money set. If none, fall back to the largest leftover
    #    numeric column.
    mileage_candidates = []
    for c in range(ncols):
        if c in roles or c in money_cols:
            continue
        k = col_kinds[c]
        if k.get('numeric', 0) > 0 and 0 < col_max_int[c] <= 500_000:
            mileage_candidates.append((c, k.get('numeric', 0)))
    if mileage_candidates:
        mileage_candidates.sort(key=lambda x: -x[1])
        roles[mileage_candidates[0][0]] = 'mileage'

    # 6) Money column fallback: if NO explicit money column was found, the
    #    largest bare-int column not used for mileage becomes the price.
    if not money_cols:
        fallback = []
        for c in range(ncols):
            if c in roles:
                continue
            k = col_kinds[c]
            if k.get('numeric', 0) > 0 and col_max_int[c] >= 1000:
                fallback.append((c, col_max_int[c]))
        if fallback:
            fallback.sort(key=lambda x: -x[1])
            money_cols = sorted([fallback[0][0]])

    for i, c in enumerate(money_cols[:3]):
        roles[c] = f'price_{["a","b","c"][i]}'

    # 6) Whatever's left → notes
    for c in range(ncols):
        if c in roles:
            continue
        if col_kinds[c].get('text', 0) + col_kinds[c].get('short_alnum', 0) > 0:
            roles[c] = 'notes'

    return roles


def _row_to_record_heuristic(roles: dict[int, str], row: tuple,
                             k_cols: set) -> dict | None:
    """Build a record from one row using inferred column roles."""
    rec: dict = {}
    notes_bits: list[str] = []
    seen_any = False
    for ci, role in roles.items():
        val = row[ci] if ci < len(row) else None
        s = _clean(val)
        if not s:
            continue
        seen_any = True
        if role == 'vin':
            rec['vin'] = s
            v = _find_vin_in(s)            # combined 'miles vin' cell -> also capture miles
            if v and not rec.get('mileage'):
                mm = _miles_beside_vin(s, v)
                if mm:
                    rec['mileage'] = mm
        elif role == 'vehicle':
            rec['raw_vehicle'] = s
        elif role == 'stock':
            rec['stock'] = s
        elif role == 'mileage':
            rec['mileage'] = s
        elif role.startswith('price_'):
            # Use k-column context to upgrade bare ints to thousands
            force = ci in k_cols
            m = _parse_money(s, force_thousands=force)
            if m is None:
                continue
            if 'asking_price' not in rec:
                rec['asking_price'] = m
            else:
                notes_bits.append(f'alt_price=${m:,}')
        elif role == 'notes':
            notes_bits.append(s)
    if not seen_any:
        return None
    if notes_bits:
        existing = rec.get('notes', '')
        rec['notes'] = ' | '.join(p for p in (existing, *notes_bits) if p)
    return rec


def _parse_no_header(rows: list[tuple]) -> list[dict]:
    """Heuristic parse when no usable header row exists. Walks every row,
    skipping blanks, and emits a record whenever a VIN is present."""
    data_rows = [r for r in rows if not _is_blank_row(r)]
    if not data_rows:
        return []
    roles = _infer_column_roles(data_rows)
    if not roles:
        return []
    # Identify columns containing "k"-suffix money so bare ints there are
    # interpreted as $thousands.
    k_cols: set = set()
    ncols = max(len(r) for r in data_rows)
    for ci in range(ncols):
        for row in data_rows:
            if ci >= len(row):
                continue
            s = _clean(row[ci]).lower().replace(',', '').replace('$', '')
            if s.endswith('k'):
                try:
                    float(s[:-1])
                    k_cols.add(ci)
                    break
                except ValueError:
                    pass
    out: list[dict] = []
    for row in data_rows:
        rec = _row_to_record_heuristic(roles, row, k_cols)
        if not rec:
            continue
        final = _finalize_record(rec)
        # In heuristic mode a real VIN is required — otherwise the row is
        # ambiguous and the operator can't act on it.
        if not final['vin']:
            continue
        out.append(final)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Public entrypoints
# ──────────────────────────────────────────────────────────────────────────

def _parse_with_header(rows: list[tuple], header_idx: int,
                       headers: list) -> list[dict]:
    """Use a detected header row at index `header_idx`; parse rows below."""
    out: list[dict] = []
    for row in rows[header_idx + 1:]:
        rec = _row_to_record(headers, row)
        if not rec:
            continue
        final = _finalize_record(rec)
        if not final['vin'] and not final['raw_vehicle']:
            continue
        out.append(final)
    return out


def parse_xlsx(file_bytes: bytes) -> list[dict]:
    """Parse an .xlsx upload. Returns a list of candidate-row dicts."""
    import openpyxl  # lazy import — only loaded when bulk upload is used
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True,
                                read_only=True)
    out: list[dict] = []
    for sheet in wb.worksheets:
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        if not rows:
            continue
        hdr = _scan_for_header(rows)
        if hdr is not None:
            idx, headers = hdr
            out.extend(_parse_with_header(rows, idx, headers))
            continue
        out.extend(_parse_no_header(rows))
    return out


# DELIMITED_VS_WHOLELINE_2026_08_23 — see parse_csv.
_DELIMS = set(',\t;|"\'')


def _looks_delimited(text: str) -> bool:
    """Is this text really a delimited file, or one vehicle per line?

    Decided by what fences the VIN: a comma/tab/semicolon/pipe/quote means a
    real cell boundary, whitespace means free text. Sampling several lines
    keeps one odd row from flipping the whole file.
    """
    votes = []
    for line in (text or '').splitlines():
        vin = _find_vin_in(line)
        if not vin:
            continue
        i = line.upper().find(vin)
        if i < 0:
            continue
        before = line[i - 1] if i > 0 else ''
        j = i + len(vin)
        after = line[j] if j < len(line) else ''
        votes.append(before in _DELIMS or after in _DELIMS)
        if len(votes) >= 5:
            break
    if not votes:
        return True          # no VIN lines: let the csv reader try
    return sum(votes) * 2 >= len(votes)


def parse_csv(file_bytes: bytes) -> list[dict]:
    """Parse a .csv (or .tsv) upload. Returns a list of candidate-row dicts."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    # Sniff delimiter — tab, comma, semicolon, or pipe
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
    except csv.Error:
        dialect = csv.excel
    # DELIMITED_VS_WHOLELINE_2026_08_23: a space-separated .txt list must NOT
    # go through the csv reader -- the comma in "41,200" gets taken for a
    # delimiter and the odometer ends up as 200.
    if not _looks_delimited(text):
        return _rows_to_records(_text_lines_to_rows(text))
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [tuple(r) for r in reader]
    if not rows:
        return []
    # TXT_WHOLELINE_2026_08_23: go through the shared record builder rather
    # than straight to the header scan, so single-cell whole-record rows are
    # parsed VIN-first instead of by the column heuristic.
    return _rows_to_records(rows)


def parse_upload(filename: str, file_bytes: bytes,
                 require_price: bool = False) -> list[dict]:
    """Dispatch on extension. Returns [] for unrecognized types.

    The dispatcher is forgiving: if the extension is wrong or missing it
    tries xlsx first, then csv on the raw bytes.

    require_price: if True, drop any row whose asking_price isn't a
    positive number. Used for price-list sheets where unsold/no-price
    rows should be skipped."""
    name = (filename or '').lower()
    rows: list[dict] = []
    if name.endswith('.xlsx') or name.endswith('.xlsm'):
        try:
            rows = parse_xlsx(file_bytes)
        except Exception:
            pass
    elif name.endswith('.csv') or name.endswith('.tsv') or name.endswith('.txt'):
        try:
            rows = parse_csv(file_bytes)
        except Exception:
            pass
    if not rows:
        # Unknown extension or first attempt failed: try xlsx then csv
        try:
            rows = parse_xlsx(file_bytes)
        except Exception:
            pass
    if not rows:
        try:
            rows = parse_csv(file_bytes)
        except Exception:
            rows = []
    if require_price and rows:
        rows = [r for r in rows
                if isinstance(r.get('asking_price'), (int, float))
                and r['asking_price'] > 0]
    return rows


# -- PASTE_INTAKE_2026_06_08 -------------------------------------------------
# Parse a pasted dealer email / auction run-list / freeform vehicle list into
# the same candidate-row shape as parse_upload(), using an LLM to read any
# layout (cell-per-line, tab tables, prose emails). The caller passes its
# gemini_call function so this module stays import-free of app.py.
import json as _json


def _json_array_salvage(raw):
    """Pull a JSON array of objects out of an LLM response that may include
    prose, ```fences```, or a truncated tail."""
    s = (raw or '').strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\s*', '', s)
        s = re.sub(r'\s*```$', '', s).strip()
    try:
        v = _json.loads(s)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for val in v.values():
                if isinstance(val, list):
                    return val
            return [v]
    except Exception:
        pass
    a = s.find('[')
    b = s.rfind(']')
    if a != -1 and b > a:
        try:
            v = _json.loads(s[a:b + 1])
            if isinstance(v, list):
                return v
        except Exception:
            pass
    objs = []
    for m in re.finditer(r'\{[^{}]*\}', s):
        try:
            objs.append(_json.loads(m.group(0)))
        except Exception:
            pass
    return objs


_PASTE_PROMPT = (
    "You are extracting vehicles from a wholesale/auction vehicle list or a "
    "used-car dealer's email that was pasted in. Return ONLY a JSON array "
    "(no prose, no markdown fences). One object per VEHICLE with keys: "
    "desc (verbatim year make model trim/body text as written), "
    "vin (17-char VIN string or null), "
    "mileage (odometer integer, or null if not shown), "
    "price (per-car asking/floor/buy price as an integer, $ and commas "
    "stripped, or null), "
    "stock (stock number string or null), "
    "extra (any other per-car detail: auction grade, MMR/book value, color, "
    "location, condition).\n"
    "Rules: one object per distinct vehicle; never invent or merge vehicles. "
    "Ignore column headers, titles, totals, greetings, signatures, "
    "disclaimers, and non-vehicle lines. VIN must be exactly 17 chars "
    "[A-HJ-NPR-Z0-9]; if partial or missing use null. If a row shows both a "
    "book/MMR value AND a separate asking/floor price, use the asking/floor "
    "as price and put the MMR in extra. Only report mileage when an actual "
    "odometer reading is shown; never guess. Return [] if no vehicles.\n\n"
    "LIST:\n"
)


def parse_pasted_text(text, gemini_fn):
    """Parse a pasted vehicle list/email into parse_upload()-shaped rows.

    gemini_fn(prompt, max_tokens=..., temperature=...) -> str | None
    Rows are run through _finalize_record() so they are identical in shape to
    a spreadsheet upload.
    """
    text = (text or '').strip()
    if not text:
        return []
    snippet = text[:24000]
    raw = gemini_fn(_PASTE_PROMPT + snippet, max_tokens=8192, temperature=0.1)
    if not raw:
        raise RuntimeError('AI extraction returned nothing (Gemini unavailable)')
    items = _json_array_salvage(raw)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        vin = it.get('vin')
        vin = '' if (vin is None or str(vin).strip().lower() in ('', 'null', 'none')) else str(vin)
        rec = {
            'raw_vehicle': _clean(it.get('desc')),
            'vin': vin,
            'mileage': it.get('mileage'),
            'asking_price': it.get('price'),
            'stock': it.get('stock'),
            'notes': _clean(it.get('extra')),
        }
        row = _finalize_record(rec)
        if not row.get('vin') and not row.get('raw_vehicle'):
            continue
        out.append(row)
    return out


# -- IMAGE_INTAKE_2026_06_26 -------------------------------------------------
# Parse a SCREENSHOT / PHOTO of a vehicle list (auction run sheet, vAuto
# "N Selected", a SmartAuction listing grab, a pasted spreadsheet image) into
# the same candidate-row shape as parse_upload(), using a MULTIMODAL model to
# read the visual table layout. Flat OCR scrambles multi-column tables and
# mis-pairs odometer-vs-price; a vision model reads the columns as a human
# would. The caller passes its multimodal vision callable so this module stays
# import-free of app.py (same contract as parse_pasted_text).

_IMAGE_PROMPT = (
    "You are reading a SCREENSHOT or PHOTO of a wholesale/auction vehicle "
    "list (e.g. a SmartAuction / vAuto / dealer inventory table, or a pasted "
    "spreadsheet image). It usually has one ROW per vehicle across several "
    "COLUMNS. Read it column-by-column like a human; do NOT flatten the table. "
    "Return ONLY a JSON array (no prose, no markdown fences). One object per "
    "VEHICLE with keys: "
    "vin (the 17-character VIN exactly as shown, or null), "
    "year (4-digit model year integer or null), "
    "make (string or null), "
    "model (string or null), "
    "trim (trim/series text or null), "
    "drivetrain (RWD/FWD/AWD/4WD or null), "
    "mileage (odometer integer; ONLY the column labeled Odometer/Odo/Miles - "
    "NEVER the price, stock number, or model year; null if not shown), "
    "color (exterior color or null), "
    "location (city/state or null), "
    "price (the per-car asking / Buy-Now price as an integer, $ and commas "
    "stripped, or null), "
    "floor (the Floor / minimum price as an integer or null), "
    "stock (stock/unit number or null).\n"
    "Rules: one object per distinct vehicle; never invent or merge vehicles. "
    "Ignore titles, headlines, column headers, totals, and decorative text. A "
    "VIN is exactly 17 chars [A-HJ-NPR-Z0-9] (no I/O/Q); if a cell is "
    "unreadable or partial use null - do not guess digits. Only report "
    "mileage when an actual odometer number is visible. Return [] if you see "
    "no vehicles."
)


def parse_image(file_bytes, mime, vision_fn):
    """IMAGE_INTAKE_2026_06_26: parse a screenshot/photo of a vehicle list
    into parse_upload()-shaped rows using a multimodal model.

    vision_fn(prompt, image_bytes, mime) -> str | None
        runs ONE multimodal vision request and returns the model's text (the
        caller wires this to its Gemini/9B vision helper).
    Rows go through _finalize_record() so they match a spreadsheet upload.
    """
    if not file_bytes:
        return []
    raw = vision_fn(_IMAGE_PROMPT, file_bytes, mime)
    if not raw:
        raise RuntimeError('AI image read returned nothing (vision model unavailable)')
    items = _json_array_salvage(raw)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        vin = it.get('vin')
        vin = '' if (vin is None or str(vin).strip().lower() in ('', 'null', 'none')) else str(vin)
        # Build the verbatim YMM string from the discrete columns so the
        # existing splitter/labels render the same as a spreadsheet row.
        ymm = ' '.join(
            str(p).strip() for p in (
                it.get('year'), it.get('make'), it.get('model'), it.get('trim'))
            if p not in (None, '', 'null', 'None')).strip()
        # Fold wholesale-only extras (drivetrain, location, floor) into notes —
        # they aren't bid columns but the operator wants to see them.
        extras = []
        for label, key in (('Drivetrain', 'drivetrain'),
                           ('Location', 'location'), ('Floor', 'floor')):
            v = it.get(key)
            if v not in (None, '', 'null', 'None'):
                extras.append(f'{label}: {v}')
        rec = {
            'raw_vehicle': ymm,
            'vin': vin,
            'year_col': it.get('year'),
            'make_col': it.get('make'),
            'model_col': it.get('model'),
            'trim_col': it.get('trim'),
            'color': it.get('color'),
            'mileage': it.get('mileage'),
            'asking_price': it.get('price'),
            'stock': it.get('stock'),
            'notes': ' | '.join(extras),
        }
        row = _finalize_record(rec)
        if not row.get('vin') and not row.get('raw_vehicle'):
            continue
        out.append(row)
    return out


# -- PDF_HEIC_INTAKE_2026_08_23 ---------------------------------------------
# Dealer-facing intake accepts "whatever you've got": a PDF export from the
# DMS, a HEIC straight off an iPhone, a screenshot, a spreadsheet. All of it
# funnels into the SAME row shape parse_upload() produces, so there is exactly
# one set of VIN/mileage rules to maintain.
#
# PDF strategy, in order:
#   1. pdfplumber extract_tables() — a real text-layer table. Best accuracy:
#      column boundaries are known, so miles can never be read as price.
#   2. pdfplumber extract_text() split on 2+ spaces — text layer, no ruled
#      table (most DMS "print to PDF" reports land here).
#   3. pdftoppm -> PNG per page -> parse_image(). Only for SCANNED pdfs with
#      no text layer at all. Costs one vision call per page, so it is the
#      last resort, never the first try.

_PDF_RASTER_DPI = 150
_PDF_MAX_RASTER_PAGES = 12   # a vision call per page — cap the burn


def _heic_to_jpeg(file_bytes: bytes) -> tuple[bytes, str]:
    """Convert HEIC/HEIF bytes to JPEG so the vision path can read them.
    Returns (bytes, mime). Falls through unchanged if conversion fails —
    some models accept HEIC directly, and an unconverted try beats none."""
    try:
        import pillow_heif
        from PIL import Image
        pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(file_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=88)
        return buf.getvalue(), 'image/jpeg'
    except Exception:
        return file_bytes, 'image/heic'


def _text_lines_to_rows(text: str) -> list[tuple]:
    """Turn a page of extracted PDF text into row tuples. Columns in a
    text-layer PDF are separated by RUNS of spaces (a single space is a word
    break inside a cell), so split on 2+ spaces and keep the cells in order —
    that preserves the column positions _infer_column_roles() relies on."""
    rows: list[tuple] = []
    for line in (text or '').splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        cells = [c.strip() for c in re.split(r'\s{2,}', line.strip())]
        if len(cells) == 1:
            # No column runs on this line. It may still be one whole record
            # ("2021 HONDA PILOT EX-L 5FNYF6H55MB012345 48,221"), which the
            # single-column heuristic path handles fine.
            rows.append((cells[0],))
        else:
            rows.append(tuple(cells))
    return rows


# WHOLE_LINE_RECORD_2026_08_23 — see module note at parse_pdf.
_MONEY_TOKEN_RE = re.compile(r'\$\s*\d')


def _record_from_line(line: str) -> dict | None:
    """Parse one vehicle written as a single free-text line.

    The VIN is the anchor: it is the only token whose SHAPE is unambiguous
    (17 chars, no I/O/Q). Everything before it is the vehicle description
    (which carries the model year); the odometer is the first bare number
    AFTER it. That ordering is why the year can never be read as mileage
    here — a year sits before the VIN, an odometer after it.

    Returns None when the line has no VIN; the caller falls back to the
    column heuristic.
    """
    s = _clean(line)
    vin = _find_vin_in(s)
    if not vin:
        return None
    up = s.upper()
    idx = up.find(vin)
    before = s[:idx].strip(' ,|\t-')
    after = s[idx + len(vin):]

    def _first_number(text: str) -> str:
        """First integer that is not a price. Skips $-prefixed amounts and
        anything with a cents decimal."""
        for m in re.finditer(r'(\$?)\s*(\d[\d,]*)(\.\d+)?', text or ''):
            if m.group(1) or m.group(3):
                continue                      # $24,995 or 24995.00 -> price
            tail = text[m.end():m.end() + 12].lower()
            if tail.strip().startswith(('k mi', 'k miles')):
                continue
            return m.group(2)
        return ''

    miles = _parse_mileage(_first_number(after))
    if miles is None:
        # ODO_STRICT_2026_08_23: nothing usable after the VIN. We may look
        # BEFORE it, but that half of the line is where model designations
        # live -- F-150, 1500, Q50, 300, X5 -- and "F-150" happily yields
        # 150 miles. So a number before the VIN only counts as an odometer
        # if it is WRITTEN like one: a thousands comma, or an explicit
        # k/mi/miles suffix. A model number never carries either.
        head = _YEAR_RE.sub(r'\2', before) if _YEAR_RE.match(before) else before
        m = re.search(
            r'(?<![-A-Za-z0-9])'
            r'(\d{1,3}(?:,\d{3})+|\d{2,7}\s*(?:k\b|mi\b|miles\b))',
            head, flags=re.I)
        miles = _parse_mileage(m.group(1)) if m else None

    return _finalize_record({
        'raw_vehicle': before,
        'vin': vin,
        'mileage': miles,
    })


def _is_whole_line_grid(rows: list[tuple]) -> bool:
    """True when every non-blank row is one cell AND at least one holds a
    VIN — i.e. records were never split into columns at all."""
    cells = [r for r in rows if not _is_blank_row(r)]
    if not cells or any(len(r) > 1 for r in cells):
        return False
    return any(_find_vin_in(r[0]) for r in cells)


def _scrub_mileage(rows: list[dict]) -> list[dict]:
    """Drop a mileage that is exactly this row's model year. See Fix 2."""
    for r in rows:
        try:
            yr = int(r.get('year') or 0)
            mi = int(r.get('mileage') or 0)
        except (TypeError, ValueError):
            continue
        if yr and mi and yr == mi:
            r['mileage'] = None
            r['mileage_dropped'] = 'looked like the model year'
    return rows


def _rows_to_records(rows: list[tuple]) -> list[dict]:
    """Header-scan then parse, else fall back to content inference. Same
    two-step every other parser in this module uses."""
    if not rows:
        return []
    if _is_whole_line_grid(rows):
        out = []
        for r in rows:
            if _is_blank_row(r):
                continue
            rec = _record_from_line(r[0])
            if rec:
                out.append(rec)
        if out:
            return out
    hdr = _scan_for_header(rows)
    if hdr is not None:
        idx, headers = hdr
        recs = _parse_with_header(rows, idx, headers)
        if recs:
            return recs
    return _parse_no_header(rows)


# Column-detection strategies, best structure first. "ruled" wins when the
# PDF has real table borders; "text-aligned" clusters words by x-position and
# is what most DMS "print to PDF" reports need.
_PDF_TEXT_TABLE = {'vertical_strategy': 'text', 'horizontal_strategy': 'text'}


# VIN_ANCHORED_GRID_2026_08_23 — see the note on _pdf_vin_grid below.
_LINE_TOL = 2.0      # pts: words within this vertical distance are one line
_COL_TOL = 3.0       # pts: word starts within this distance are one column


def _pdf_lines(page):
    """Group a page's words into visual lines. Returns [(top, [word,...])]
    with words left-to-right."""
    try:
        words = page.extract_words(use_text_flow=False,
                                   keep_blank_chars=False) or []
    except Exception:
        return []
    lines = []
    for w in sorted(words, key=lambda w: (round(w['top'], 1), w['x0'])):
        if lines and abs(w['top'] - lines[-1][0]) <= _LINE_TOL:
            lines[-1][1].append(w)
        else:
            lines.append((w['top'], [w]))
    for _t, ws in lines:
        ws.sort(key=lambda w: w['x0'])
    return lines


def _column_starts(data_lines) -> list[float]:
    """Cluster the x0 of every word on the VIN-bearing lines into column
    start positions. A real column start recurs on most data rows; a word
    that happens to begin mid-cell (the second word of "GRAND CHEROKEE")
    does not, and is dropped by the frequency floor."""
    xs = sorted(w['x0'] for _t, ws in data_lines for w in ws)
    if not xs:
        return []
    clusters = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= _COL_TOL:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    floor = max(2, int(len(data_lines) * 0.6))
    starts = [sum(c) / len(c) for c in clusters if len(c) >= floor]
    return sorted(starts)


def _assign_to_columns(words, starts) -> tuple:
    """Place each word in the last column whose start is at or before it.
    Words sharing a column are joined with a space, so a two-word MODEL cell
    stays one cell."""
    cells = [[] for _ in starts]
    for w in words:
        idx = 0
        for i, s in enumerate(starts):
            if w['x0'] >= s - _COL_TOL:
                idx = i
            else:
                break
        cells[idx].append(w['text'])
    return tuple(' '.join(c).strip() for c in cells)


def _pdf_vin_grid(page):
    """Build a rectangular grid for a fixed-width report, using the rows that
    contain VINs to define the columns. Returns [] when the page has no
    VIN-bearing line (a cover page, a terms page), so the caller falls
    through to the other strategies."""
    lines = _pdf_lines(page)
    if not lines:
        return []
    data = [(t, ws) for t, ws in lines
            if _find_vin_in(' '.join(w['text'] for w in ws))]
    if not data:
        return []
    starts = _column_starts(data)
    if len(starts) < 2:
        return []

    grid = []
    # The header is the nearest line ABOVE the first data row that names a
    # column we understand. Without it the content heuristic still runs, but
    # with it MILES/ASK/FLOOR are unambiguous.
    first_top = data[0][0]
    header_ws = None
    for t, ws in lines:
        if t >= first_top:
            break
        txt = ' '.join(w['text'] for w in ws).lower()
        if 'vin' in txt.split() or 'vin#' in txt or (
                'miles' in txt and ('make' in txt or 'model' in txt
                                    or 'year' in txt or 'yr' in txt)):
            header_ws = ws
    # VIN_GRID_NEEDS_HEADER_2026_08_23: no header means no mapping to make,
    # and a word-per-column grid handed to the content heuristic is WORSE
    # than the whole-line parser -- it reads the model year as the odometer.
    if not header_ws:
        return []
    header_cells = _assign_to_columns(header_ws, starts)
    if 'vin' not in _normalize_headers(list(header_cells)):
        return []
    grid.append(header_cells)
    for _t, ws in data:
        grid.append(_assign_to_columns(ws, starts))
    return grid


def _pdf_page_grids(page):
    """Yield (label, rows) candidate grids for one page, best-first.

    NOTE: page.extract_text() is deliberately LAST and is only ever a
    whole-record-per-line fallback. It collapses inter-column space runs to a
    single space, so it cannot be used to recover columns — see
    PDF_TEXT_STRATEGY_2026_08_23.
    """
    vin_grid = _pdf_vin_grid(page)
    if vin_grid:
        yield 'vin-anchored', vin_grid
    for label, settings in (('ruled', None), ('text-aligned', _PDF_TEXT_TABLE)):
        try:
            tables = (page.extract_tables(settings) if settings
                      else page.extract_tables())
        except Exception:
            continue
        grid = []
        for tbl in tables or []:
            for r in tbl or []:
                if r:
                    grid.append(tuple(_clean(c) for c in r))
        if grid:
            yield label, grid
    try:
        txt = page.extract_text() or ''
    except Exception:
        txt = ''
    if txt.strip():
        yield 'text-lines', _text_lines_to_rows(txt)


def _vin_count(records: list[dict]) -> int:
    return sum(1 for r in records
               if _VIN_RE.match((r.get('vin') or '').upper()))


def _vin_valid_count(records: list[dict]) -> int:
    """CHECKDIGIT_GATE_2026_08_23: VINs whose 9th character actually checks
    out against the other sixteen. A VIN split across two table cells is
    still 17 legal characters and still matches _VIN_RE -- only the check
    digit tells the difference."""
    return sum(1 for r in records if r.get('vin_check_digit_valid'))


def parse_pdf(file_bytes: bytes, vision_fn=None) -> list[dict]:
    """Parse a PDF vehicle list into parse_upload()-shaped rows.

    Per page, try each column strategy in order and keep the FIRST one that
    actually yields a valid VIN. Gating on "found a real VIN" rather than
    "returned something" is what stops a mis-clustered grid from winning and
    filing the asking price as the odometer.

    vision_fn(prompt, image_bytes, mime) -> str | None
        Optional. Only used when the PDF has NO text layer (a scan, or a
        photo saved as a PDF). Without it, a scanned PDF returns [].
    """
    out: list[dict] = []
    text_layer_found = False
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                # Score EVERY strategy for this page and keep the best, rather
                # than taking the first that produced something VIN-shaped.
                # Score is (check-digit-valid VINs, VIN-shaped, any rows).
                best: list[dict] = []
                best_score = (-1, -1, -1)
                for _label, grid in _pdf_page_grids(page):
                    text_layer_found = True
                    recs = _rows_to_records(grid)
                    n_ok = _vin_valid_count(recs)
                    n_vin = _vin_count(recs)
                    score = (n_ok, n_vin, len(recs))
                    if score > best_score:
                        best, best_score = recs, score
                    if n_ok and n_ok == n_vin:
                        break   # every VIN checks out; nothing can beat this
                out.extend(best)
    except Exception:
        pass

    if out or (text_layer_found and not vision_fn):
        return _scrub_mileage(_dedupe_rows(out))

    # Scanned PDF — rasterize and read each page with the vision model.
    if vision_fn is None:
        return []
    for png in _pdf_to_pngs(file_bytes):
        try:
            out.extend(parse_image(png, 'image/png', vision_fn))
        except Exception:
            continue
    return _scrub_mileage(_dedupe_rows(out))


def _pdf_to_pngs(file_bytes: bytes) -> list[bytes]:
    """Rasterize a PDF to one PNG per page via poppler's pdftoppm.
    Returns [] if poppler is unavailable — the caller degrades to no rows
    rather than raising."""
    import os
    import subprocess
    import tempfile
    pages: list[bytes] = []
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 'in.pdf')
        with open(src, 'wb') as fh:
            fh.write(file_bytes)
        try:
            subprocess.run(
                ['pdftoppm', '-png', '-r', str(_PDF_RASTER_DPI),
                 '-l', str(_PDF_MAX_RASTER_PAGES), src,
                 os.path.join(td, 'pg')],
                check=True, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return []
        for name in sorted(os.listdir(td)):
            if name.startswith('pg') and name.endswith('.png'):
                try:
                    with open(os.path.join(td, name), 'rb') as fh:
                        pages.append(fh.read())
                except Exception:
                    continue
    return pages[:_PDF_MAX_RASTER_PAGES]


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """Collapse rows that describe the same vehicle. A multi-page PDF repeats
    its header/footer, and a dealer who sends 3 screenshots of one scrolling
    table will overlap them. Keyed on VIN when present; rows with no VIN are
    kept as-is (we cannot prove they are the same car).

    When two rows share a VIN, MERGE rather than drop: page 1 may carry the
    mileage and page 2 the price."""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        vin = (r.get('vin') or '').strip().upper()
        if not vin:
            out.append(r)
            continue
        if vin not in seen:
            seen[vin] = r
            out.append(r)
            continue
        keep = seen[vin]
        for k, v in r.items():
            if v not in (None, '', 0) and keep.get(k) in (None, '', 0):
                keep[k] = v
    return out


# Extensions we will even attempt. Anything else is rejected before it
# reaches a parser — a public endpoint should never hand arbitrary bytes to
# openpyxl or a subprocess.
SHEET_EXTS = ('xlsx', 'xlsm', 'xls', 'csv', 'tsv', 'txt')
IMAGE_EXTS = ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'heic', 'heif')
PDF_EXTS = ('pdf',)
ALLOWED_EXTS = SHEET_EXTS + IMAGE_EXTS + PDF_EXTS


def parse_any(filename: str, file_bytes: bytes, mime: str = '',
              vision_fn=None) -> list[dict]:
    """One door for every supported format. Dispatches on extension first,
    then on content-type, then falls back to sniffing the magic bytes —
    dealers rename files and phone browsers send bad content-types.

    vision_fn(prompt, image_bytes, mime) -> str | None  (image + scanned PDF)
    Returns parse_upload()-shaped rows. Raises nothing on a bad file; an
    unreadable upload comes back as [].
    """
    name = (filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    ctype = (mime or '').lower()
    head = (file_bytes or b'')[:8]

    is_pdf = ext in PDF_EXTS or ctype == 'application/pdf' or head[:4] == b'%PDF'
    is_img = (ext in IMAGE_EXTS or ctype.startswith('image/')
              or head[:3] == b'\xff\xd8\xff'          # jpeg
              or head[:8] == b'\x89PNG\r\n\x1a\n'     # png
              or (len(file_bytes or b'') > 12
                  and file_bytes[4:12] in (b'ftypheic', b'ftypheix',
                                           b'ftyphevc', b'ftypmif1')))

    if is_pdf:
        return parse_pdf(file_bytes, vision_fn)

    if is_img:
        if vision_fn is None:
            return []
        img, imime = file_bytes, (ctype if ctype.startswith('image/') else '')
        # HEIC_MAGIC_FIX_2026_08_23: slice file_bytes, not head (head is only
        # 8 bytes, so head[4:12] could never match an 8-byte brand literal).
        if ext in ('heic', 'heif') or file_bytes[4:12] in (
                b'ftypheic', b'ftypheix', b'ftypmif1', b'ftyphevc'):
            img, imime = _heic_to_jpeg(file_bytes)
        if not imime:
            imime = 'image/png' if head[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
        return _scrub_mileage(_dedupe_rows(parse_image(img, imime, vision_fn)))

    # Spreadsheet / delimited text. parse_upload() already tries xlsx then
    # csv on the raw bytes when the extension lies, so a mislabeled sheet
    # still parses.
    rows = parse_upload(filename, file_bytes)
    if not rows and ext == 'xls':
        # Excel 97 (BIFF). openpyxl cannot read it; xlrd can.
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=file_bytes)
            grid: list[tuple] = []
            for sh in book.sheets():
                for rx in range(sh.nrows):
                    grid.append(tuple(sh.row_values(rx)))
            rows = _rows_to_records(grid)
        except Exception:
            rows = []
    return _scrub_mileage(_dedupe_rows(rows))
