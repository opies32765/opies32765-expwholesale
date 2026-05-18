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
]

# Lazy-loaded WMI -> (module, decode_fn) lookup.
_WMI_INDEX: dict | None = None


def _build_index() -> dict:
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
            idx[wmi.upper()] = (mod_name, decode_fn)
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
    """
    if not vin or not isinstance(vin, str) or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    idx = _build_index()
    entry = idx.get(vin[:3])
    if not entry:
        return None
    _mod_name, fn = entry
    try:
        return fn(vin)
    except Exception:
        return None


def supported_makes() -> list[str]:
    """Returns the list of make names currently dispatchable."""
    idx = _build_index()
    return sorted({fn(None) or '' for _, fn in idx.values() if False}) or \
           sorted({mn.replace('vds_', '') for mn, _ in idx.values()})


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
