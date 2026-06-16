"""wmi_guard.py — WMI_GUARD_2026_06_16

A VIN's WMI (first 3 chars) is assigned by SAE/the manufacturer and CANNOT
contradict the make. Decoders (esp. the cached Claude VIN decoder) sometimes
hallucinate the wrong marque entirely — bid 3403: a W1N Mercedes-Benz G-wagon
was decoded as "Porsche 911" at high confidence and the label stuck through
the dashboard, canon_* columns, and buyer matching.

This module is a TRIGGER, not the source of truth: `make_conflict` only tells
the caller "the decoded make disagrees with the WMI — go re-decode with the
deterministic NHTSA path." The corrected value always comes from NHTSA, so an
imperfect entry here can at worst cause a harmless no-op re-decode, never a
wrong overwrite. Unknown WMI -> no conflict -> no action (current behavior).
"""

# Curated, high-confidence WMI -> canonical make. Covers EW's luxury/exotic
# focus plus common mainstream marques. Keep entries UNAMBIGUOUS.
WMI_MAKE = {
    # Mercedes-Benz
    'W1N': 'MERCEDES-BENZ', 'W1K': 'MERCEDES-BENZ', 'WDB': 'MERCEDES-BENZ',
    'WDC': 'MERCEDES-BENZ', 'WDD': 'MERCEDES-BENZ', 'WDF': 'MERCEDES-BENZ',
    '4JG': 'MERCEDES-BENZ', '55S': 'MERCEDES-BENZ',
    # Porsche
    'WP0': 'PORSCHE', 'WP1': 'PORSCHE',
    # BMW
    'WBA': 'BMW', 'WBS': 'BMW', 'WBX': 'BMW', 'WBY': 'BMW',
    '5UX': 'BMW', '5YM': 'BMW', '4US': 'BMW',
    # Audi
    'WAU': 'AUDI', 'WA1': 'AUDI', 'WUA': 'AUDI', 'TRU': 'AUDI',
    # Exotics
    'ZFF': 'FERRARI', 'ZHW': 'LAMBORGHINI', 'ZPB': 'LAMBORGHINI',
    'ZAM': 'MASERATI', 'SCB': 'BENTLEY', 'SCA': 'ROLLS-ROYCE',
    'SCF': 'ASTON MARTIN', 'SBM': 'MCLAREN',
    # British
    'SAL': 'LAND ROVER', 'SAJ': 'JAGUAR',
    # Tesla
    '5YJ': 'TESLA', '7SA': 'TESLA', 'LRW': 'TESLA', 'XP7': 'TESLA',
}

# Normalize decoder make spellings to the canonical form above.
_ALIAS = {
    'MERCEDES': 'MERCEDES-BENZ', 'MERCEDES BENZ': 'MERCEDES-BENZ',
    'MERCEDESBENZ': 'MERCEDES-BENZ', 'MB': 'MERCEDES-BENZ',
    'MERCEDES-BENZ AG': 'MERCEDES-BENZ',
    'LANDROVER': 'LAND ROVER', 'LAND-ROVER': 'LAND ROVER',
    'RANGE ROVER': 'LAND ROVER', 'ROVER': 'LAND ROVER',
    'ROLLS ROYCE': 'ROLLS-ROYCE', 'ROLLSROYCE': 'ROLLS-ROYCE',
    'ASTONMARTIN': 'ASTON MARTIN', 'ASTON-MARTIN': 'ASTON MARTIN',
    'ASTON MARTIN LAGONDA': 'ASTON MARTIN',
}


def _norm(make):
    if not make:
        return None
    m = ' '.join(str(make).strip().upper().split())
    return _ALIAS.get(m, m)


def wmi_make(vin):
    """Canonical make for a VIN's WMI, or None when the WMI is unknown."""
    if not vin or len(vin) < 3:
        return None
    return WMI_MAKE.get(vin[:3].upper())


def make_conflict(vin, decoded_make):
    """True iff the VIN's WMI maps to a KNOWN make that the decoded make
    contradicts. Unknown WMI or empty decoded make -> False (never block)."""
    w = wmi_make(vin)
    dm = _norm(decoded_make)
    if not w or not dm:
        return False
    return dm != w
