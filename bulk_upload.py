"""bulk_upload.py — parse xlsx/csv "needs to go" lists from dealers.

Dealers send EW spreadsheets of vehicles they want off the lot. This module
turns one of those sheets into a normalized list of bid candidates that the
operator can preview, edit, then bulk-create.

The parser tolerates:
  - column reordering (header-based mapping, case + whitespace insensitive)
  - extra/blank columns and trailing junk rows
  - Unicode noise (replacement chars from cp1252 round-trips)
  - missing fields (any single column except VIN can be absent)

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
    'ymm': 'raw_vehicle',
    'stock': 'stock',
    'stock #': 'stock',
    'stock#': 'stock',
    'stocknumber': 'stock',
    'stock number': 'stock',
    'vin': 'vin',
    'vin#': 'vin',
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
    'odometer': 'mileage',
    'mileage': 'mileage',
    'miles': 'mileage',
    'vrank description': 'notes',
    'notes': 'notes',
    'comments': 'notes',
    'condition': 'notes',
    'condition notes': 'notes',
    'damage': 'notes',
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
_VIN_RE = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')


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
    try:
        n = int(float(s))
        if 0 <= n <= 9_999_999:
            return n
    except ValueError:
        pass
    return None


def _parse_money(value) -> int | None:
    s = _clean(value).lower().replace('$', '').replace(',', '').strip()
    if not s:
        return None
    # Allow shorthand like "889k"
    if s.endswith('k'):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            return None
    try:
        n = int(float(s))
        if 0 <= n <= 99_999_999:
            return n
    except ValueError:
        pass
    return None


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
    if not m:
        # No year prefix — give up and dump everything into model
        return None, '', raw, ''
    year = int(m.group(1))
    rest = m.group(2).strip()

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
    for h in headers:
        key = _clean(h).lower()
        out.append(_HEADER_MAP.get(key))
    return out


def _row_to_record(headers: list[str | None], row: tuple) -> dict | None:
    """Build a record dict from a single sheet row. Returns None if blank."""
    rec: dict = {}
    seen_any = False
    for i, col in enumerate(headers):
        if i >= len(row) or not col:
            continue
        val = row[i]
        if val not in (None, ''):
            seen_any = True
        rec.setdefault(col, val)  # keep first hit if dup header
    if not seen_any:
        return None
    return rec


def _finalize_record(rec: dict) -> dict:
    """Normalize a raw row dict into the canonical bid-candidate shape."""
    vin = _clean(rec.get('vin')).upper()
    raw_vehicle = _clean(rec.get('raw_vehicle'))
    year, make, model, trim = split_vehicle_string(raw_vehicle)

    asking = _parse_money(rec.get('asking_price'))
    cost   = _parse_money(rec.get('cost'))
    # If no asking price column, the "Cost" column is what the dealer paid —
    # use it as a hint for the operator's later asking-price decision but
    # don't put it in asking_price (it's an internal dealer number).

    out = {
        'vin': vin if _VIN_RE.match(vin) else '',
        'vin_check_digit_valid': bool(vin and _VIN_RE.match(vin)),
        'raw_vehicle': raw_vehicle,
        'year':  year,
        'make':  make,
        'model': model,
        'trim':  trim,
        'body':  _clean(rec.get('body')),
        'color': _clean(rec.get('color')),
        'mileage': _parse_mileage(rec.get('mileage')),
        'asking_price': asking,
        'dealer_cost': cost,
        'stock': _clean(rec.get('stock')),
        'notes': _clean(rec.get('notes')),
    }
    return out


def parse_xlsx(file_bytes: bytes) -> list[dict]:
    """Parse an .xlsx upload. Returns a list of candidate-row dicts."""
    import openpyxl  # lazy import — only loaded when bulk upload is used
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True,
                                read_only=True)
    out: list[dict] = []
    for sheet in wb.worksheets:
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            continue
        headers = _normalize_headers(list(header_row))
        # Sheet header detection: at least one column must be 'vin' or
        # 'raw_vehicle'. If not, this sheet is junk — skip.
        if not any(h in ('vin', 'raw_vehicle') for h in headers):
            continue
        for row in rows_iter:
            rec = _row_to_record(headers, row)
            if not rec:
                continue
            final = _finalize_record(rec)
            # Must have either a VIN or at least a vehicle string + something
            if not final['vin'] and not final['raw_vehicle']:
                continue
            out.append(final)
    return out


def parse_csv(file_bytes: bytes) -> list[dict]:
    """Parse a .csv upload. Returns a list of candidate-row dicts."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = _normalize_headers(rows[0])
    if not any(h in ('vin', 'raw_vehicle') for h in headers):
        return []
    out: list[dict] = []
    for row in rows[1:]:
        rec = _row_to_record(headers, row)
        if not rec:
            continue
        final = _finalize_record(rec)
        if not final['vin'] and not final['raw_vehicle']:
            continue
        out.append(final)
    return out


def parse_upload(filename: str, file_bytes: bytes) -> list[dict]:
    """Dispatch on extension. Returns [] for unrecognized types."""
    name = (filename or '').lower()
    if name.endswith('.xlsx') or name.endswith('.xlsm'):
        return parse_xlsx(file_bytes)
    if name.endswith('.csv') or name.endswith('.tsv') or name.endswith('.txt'):
        return parse_csv(file_bytes)
    # Try xlsx first then csv on the bytes if extension is missing
    try:
        return parse_xlsx(file_bytes)
    except Exception:
        pass
    try:
        return parse_csv(file_bytes)
    except Exception:
        return []
