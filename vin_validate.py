"""VIN structural validator — ISO 3779 check digit + char set + length.

Used at intake (canonicalize_bid_vin) to flag bids whose VIN can never be
enriched, so workers don't spin on them. Returns a short machine-readable
`reason` string so the operator can see WHY on the dashboard.

Public API:
    validate(vin) -> {'valid': bool, 'reason': str|None,
                      'check_digit_expected': str|None}

Reason codes (stable, used as bids.vin_invalid_reason values):
    'missing'             — empty/None
    'length:<actual>'     — not 17 chars (e.g. 'length:15')
    'illegal_chars:<XYZ>' — contains I/O/Q (forbidden) or non-alnum
    'check_digit'         — fails ISO 3779 mod-11 check
"""
from __future__ import annotations

# ISO 3779 transliteration table
_TRANSLIT = {
    '0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6, 'G': 7, 'H': 8,
    'J': 1, 'K': 2, 'L': 3, 'M': 4, 'N': 5, 'P': 7, 'R': 9,
    'S': 2, 'T': 3, 'U': 4, 'V': 5, 'W': 6, 'X': 7, 'Y': 8, 'Z': 9,
}
# Position weights (1-indexed). Position 9 (check digit) has weight 0.
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_ALLOWED_CHARS = set(_TRANSLIT.keys())


def validate(vin):
    """Return {'valid': bool, 'reason': str|None, 'check_digit_expected': str|None}.

    `valid=True` means: 17 chars, only allowed letters/digits (no I/O/Q),
    and the ISO 3779 check digit at position 9 matches.

    Some real-world VINs (mostly pre-1981 or aftermarket-issued plates)
    fail check digit but are still valid in the wild. EW's wholesale flow
    targets modern vehicles only, so check-digit-strict is the right
    default. If you need to ingest a known-bad-but-real VIN, set
    vin_invalid_reason=NULL manually after creation.
    """
    if not vin:
        return {'valid': False, 'reason': 'missing', 'check_digit_expected': None}

    v = vin.strip().upper()
    if len(v) != 17:
        return {'valid': False, 'reason': f'length:{len(v)}',
                'check_digit_expected': None}

    illegal = sorted({c for c in v if c not in _ALLOWED_CHARS})
    if illegal:
        return {'valid': False,
                'reason': f'illegal_chars:{"".join(illegal)}',
                'check_digit_expected': None}

    total = 0
    for i, c in enumerate(v):
        total += _TRANSLIT[c] * _WEIGHTS[i]
    rem = total % 11
    expected = 'X' if rem == 10 else str(rem)
    actual = v[8]
    if actual != expected:
        return {'valid': False, 'reason': 'check_digit',
                'check_digit_expected': expected}

    return {'valid': True, 'reason': None, 'check_digit_expected': expected}


if __name__ == '__main__':
    cases = [
        ('1C6RRFFG2SJW60011', False, 'check_digit'),   # bid 1438 — typo VIN
        ('2C3CDXGJ0PH516474', True,  None),            # bid 1432 — real Scat Pack
        ('W1K6G7GB1SA125341', True,  None),            # bid 1436 — real E 350
        ('SHORTVIN', False, 'length:8'),
        ('1HGCM82633A123456', True,  None),            # textbook honda
        ('1HGCM8I633A123456', False, 'illegal_chars:I'),
        ('', False, 'missing'),
        (None, False, 'missing'),
    ]
    fail = 0
    for vin, want_valid, want_reason in cases:
        r = validate(vin)
        ok = (r['valid'] == want_valid and r['reason'] == want_reason)
        fail += 0 if ok else 1
        mark = 'OK' if ok else 'FAIL'
        print(f'{mark:4} vin={vin!r:25}  → valid={r["valid"]} reason={r["reason"]!r}')
    print(f'\n{fail} failures')
