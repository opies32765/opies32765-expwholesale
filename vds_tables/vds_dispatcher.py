"""Deterministic VIN VDS dispatcher.

Routes a VIN to the right per-manufacturer decoder by WMI prefix. Returns
the decoded {year, make, model, trim, body, engine, confidence, source}
dict or None if no module owns the WMI or the WMI module returns None.

Confidence and source pass through unchanged so callers can decide whether
to trust the result or fall back to vAuto BFF / Claude.

To add a new manufacturer:
  1. Drop the module file (e.g. vds_audi.py) into the same directory
     with a top-level `WMI` list and a `decode(vin)` function.
  2. Add it to MODULES below.
"""
from __future__ import annotations
import importlib

# Per-manufacturer modules, by import name.
MODULES = [
    'vds_ferrari',
    'vds_porsche',
    'vds_lamborghini',
    'vds_mclaren',
    'vds_astonmartin',
    'vds_bentley',
    'vds_rollsroyce',
    'vds_bugatti',
    'vds_bmw',
    'vds_mercedes',
    'vds_audi',
    'vds_landrover',
    'vds_jaguar',
    'vds_maserati',
    'vds_lexus',
    'vds_lotus',
    # EV-native brands (2026-05-18)
    'vds_tesla',
    'vds_rivian',
    'vds_lucid',
    'vds_polestar',
    # Ford Motor Company brands (2026-05-18)
    'vds_ford',
    'vds_lincoln',
    'vds_mercury',
    # Stellantis brands (2026-05-18). Multiple brands share WMIs 1C3/2C3/3C3/
    # 1C4/1C6/3C6/ZAR. Dispatcher tries each in module order until one returns
    # a non-None decode (see decode() logic with shared-WMI fallback).
    'vds_chrysler',
    'vds_dodge',
    'vds_jeep',
    'vds_ram',
    'vds_fiat',
    'vds_alfaromeo',
    # Mainstream Japanese / Korean (deployed 2026-05-18 afternoon)
    'vds_toyota',
    'vds_honda',
    'vds_acura',
    'vds_nissan',
    'vds_hyundai',
    'vds_kia',
    'vds_genesis',
    'vds_mazda',
    # vds_infiniti held back: 7 cases return wrong model (G37↔Q40/Q60/M56 rebrand)
    # GM domestic (deployed 2026-05-18 afternoon)
    'vds_chevrolet',
    'vds_gmc',
    'vds_cadillac',
    'vds_buick',
    'vds_pontiac',   # legacy — brand ended 2010
    'vds_saturn',    # legacy — brand ended 2010
    'vds_hummer',    # legacy H2/H3; modern Hummer EV is under vds_gmc
    'vds_saab',      # legacy — GM-era ended 2011
    # European mainstream (deployed 2026-05-18 afternoon)
    'vds_volkswagen',
    'vds_volvo',
    'vds_mini',
    # Japanese mainstream additions (deployed 2026-05-18 afternoon)
    'vds_subaru',
    'vds_mitsubishi',
]

# Lazy-loaded WMI -> (module, decode_fn) lookup.
_WMI_INDEX: dict | None = None


def _build_index() -> dict:
    """Builds a WMI -> list of (mod_name, decode_fn) lookup. Multiple modules
    can claim the same WMI (e.g., Stellantis 1C3/2C3/3C3 shared by Chrysler+
    Dodge); the decode() function will try each in order and return the
    first non-None result.
    """
    global _WMI_INDEX
    if _WMI_INDEX is not None:
        return _WMI_INDEX
    idx: dict = {}
    for mod_name in MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue  # module not yet built — skip silently
        wmi_list = getattr(mod, 'WMI', None) or []
        decode_fn = getattr(mod, 'decode', None)
        if not decode_fn:
            continue
        for wmi in wmi_list:
            key = wmi.upper()
            existing = idx.get(key)
            if existing is None:
                idx[key] = [(mod_name, decode_fn)]
            else:
                existing.append((mod_name, decode_fn))
    _WMI_INDEX = idx
    return idx


def decode(vin: str) -> dict | None:
    """Returns decoded dict or None.

    Output shape:
        {
          'year': int | None,
          'make': str,
          'model': str,
          'trim': str | None,
          'body': str | None,
          'engine': str | None,
          'confidence': float,
          'source': 'vds_table:<make>',
          # plus any extra keys the per-make module returns (chassis, drive, etc)
        }

    For WMIs shared by multiple manufacturers (e.g., Stellantis 1C3 shared
    by Chrysler + Dodge), tries each module in turn until one returns a
    non-None result. Order in MODULES determines priority.
    """
    if not vin or not isinstance(vin, str) or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    idx = _build_index()
    candidates = idx.get(vin[:3])
    if not candidates:
        return None
    for _mod_name, fn in candidates:
        try:
            result = fn(vin)
            if result is not None:
                return result
        except Exception:
            continue
    return None


def supported_makes() -> list[str]:
    """Returns the list of make names currently dispatchable."""
    idx = _build_index()
    makes = set()
    for candidates in idx.values():
        for mn, _ in candidates:
            makes.add(mn.replace('vds_', ''))
    return sorted(makes)


if __name__ == '__main__':
    # Quick smoke test
    cases = [
        ('ZFF98RNA1N0276279', 'Ferrari', 'Roma'),
        ('WP0AB2A92SS207018', 'Porsche', '911'),
    ]
    for vin, exp_make, exp_model in cases:
        r = decode(vin)
        ok = r and r['make'] == exp_make
        print(f'{"OK " if ok else "FAIL "} {vin} -> {r}')
