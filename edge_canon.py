"""edge_canon.py - normalize auction-feed vehicle strings toward EW's canon vocabulary.

PERMANENT MODULE, not test scaffolding. EW's bids table already carries canon_make /
canon_model / canon_trim; this maps an external auction feed's strings into that space so
comps can be joined. Written source-agnostic on purpose - Edge Pipeline today, ACV or any
other feed tomorrow.

Why it exists: a raw Year+Make+Model join scored BMW 4.3%, Audi 0%, Lexus 0% against the
Longwood backfill while those exact makes were present in volume (BMW 46, Mercedes 51,
Lexus 38 in a SINGLE sale). The misses were punctuation and granularity:

    EW canon        Edge Pipeline
    ----------      -------------
    4 Series        4-Series          <- hyphen
    C-CLASS         C Class           <- hyphen + case (Edge is itself inconsistent:
                                         "C Class" 37x AND "C-Class" 26x)
    ES              ES 350            <- EW keeps the family, Edge appends the engine code
    Defender 110    Defender          <- reverse of the above
    PANAMERA        Panamera          <- case

Deliberately NOT matched: "Range Rover" vs "Range Rover Sport". Those are different
vehicles at different prices. The rule below only tolerates an extra token when that token
contains a DIGIT (350, 460, 200t, 110) - an engine/size designation - never a word
("Sport", "Coupe", "Cabriolet"), which denotes a distinct model.
"""
import re

_NONWORD = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")

MAKE_ALIASES = {
    "MERCEDES BENZ": "MERCEDES",
    "MERCEDESBENZ": "MERCEDES",
    "MERCEDES-BENZ": "MERCEDES",
    "LANDROVER": "LAND ROVER",
    "CHEVY": "CHEVROLET",
    "VW": "VOLKSWAGEN",
    "MINI COOPER": "MINI",
    "ROLLS ROYCE": "ROLLS-ROYCE",
    "ALFA": "ALFA ROMEO",
}


def norm_text(s):
    """Uppercase, strip punctuation (hyphens -> space), collapse whitespace."""
    if not s:
        return ""
    s = str(s).upper().replace("-", " ").replace("/", " ")
    s = _NONWORD.sub("", s)
    return _WS.sub(" ", s).strip()


def canon_make(s):
    m = norm_text(s)
    return MAKE_ALIASES.get(m, m)


def canon_model(s):
    """Normalized model. 'SERIES' is kept - '3 SERIES' must not collapse to '3'."""
    return norm_text(s)


def _has_digit(tok):
    return any(ch.isdigit() for ch in tok)


def models_match(a, b):
    """True if two normalized model strings denote the same vehicle family.

    Exact match, or one is a token-prefix of the other AND every extra token carries a
    digit (engine/size code). 'ES' ~ 'ES 350'. 'DEFENDER' ~ 'DEFENDER 110'.
    'RANGE ROVER' !~ 'RANGE ROVER SPORT'.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if long_[: len(short)] != short:
        return False
    return all(_has_digit(t) for t in long_[len(short):])


def key(year, make, model):
    """Coarse bucket key - (year, canonical make). Model compared via models_match."""
    return (year, canon_make(make))


def trims_match(a, b, min_prefix=None):
    """True only if two trim/style strings are the SAME trim.

    EXACT MATCH ONLY after normalization. 2026-09-02: the previous rule
    (shared leading run >= 8 chars) was graded by an independent corpus of 245
    real labelled pairs and scored a **40.5% FALSE-MATCH RATE**. It guarded bare
    short codes and nothing else -- one body word in front spent the prefix
    budget before the codes diverged:

        "2500HD LT"  ~ "2500HD LTZ"          (Silverado 2500, 15x17 rows)
        "SEDAN EX"   ~ "SEDAN EX-L"          (Accord 31x22, Civic 30x18)
        "F-250 Super Duty XL" ~ "... XLT"    (19 shared characters)

    and long trims collided outright:

        "Unlimited Rubicon" ~ "Unlimited Rubicon 392"   5,595 vs 6,395
        "Premium Luxury"    ~ "Premium Luxury Platinum" 3,195 vs 00,595

    A 0,800 error on a card the buyers price from. Showing nothing beats
    showing a Rubicon 392 as a comp for a Rubicon, so: exact or abstain.
    Truncated auction strings ("BIG HORN/LONE S") simply no longer match until
    a real canonicalization table replaces this -- that is the correct failure.

    min_prefix is accepted and IGNORED so existing callers keep working.
    """
    a, b = norm_text(a), norm_text(b)
    return bool(a) and bool(b) and a == b
