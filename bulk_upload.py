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
_YEAR_ANY_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')
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
              if h in ('vin', 'raw_vehicle', 'stock', 'mileage',
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
    vin = _clean(rec.get('vin')).upper().replace(' ', '').replace('-', '')
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
    up = raw.upper().replace(' ', '').replace('-', '')
    if _VIN_RE.match(up):
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


def parse_csv(file_bytes: bytes) -> list[dict]:
    """Parse a .csv (or .tsv) upload. Returns a list of candidate-row dicts."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    # Sniff delimiter — tab, comma, semicolon, or pipe
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',\t;|')
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [tuple(r) for r in reader]
    if not rows:
        return []
    hdr = _scan_for_header(rows)
    if hdr is not None:
        idx, headers = hdr
        return _parse_with_header(rows, idx, headers)
    return _parse_no_header(rows)


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
