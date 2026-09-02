"""trim_schema.py — resolve a vendor trim string to a canonical trim, or ABSTAIN.

OPERATOR DIRECTIVE 2026-09-02: "a big horn is not a sport and a sport is not a
laramie."  A like car must match YEAR + MODEL + TRIM.  Showing a Sport as a comp
for a Laramie is a serious failure.

This module REPLACES the heuristic in edge_canon.trims_match (normalize, then
match on a shared >=8-char leading run).  That heuristic had no notion of what a
trim IS; it only measured how two strings look.  This module resolves each side
INTO A CLOSED VOCABULARY and compares the resolved keys.  Two strings match only
when both land on the same vocabulary entry.  Everything else ABSTAINS.

    ABSTAIN IS A CORRECT ANSWER.  A card that shows nothing beats a card that
    shows a Sport under a Laramie.  Every rule below is tuned for ZERO false
    matches; misses are the accepted cost and are counted, not hidden.

-------------------------------------------------------------------------------
WHY THIS SHAPE — the five things the survey established (see __main__ --survey)
-------------------------------------------------------------------------------

1.  EW ALREADY OWNS A TRIM VOCABULARY.  `ymmt_catalog` holds 28,372 rows of
    (year, make, model, trim) over 2010-2026 / 44 makes / 944 distinct trim
    strings.  Nothing here invents a new vocabulary; the catalog is the
    authority and this module extends it with reviewed observations.

2.  THE AUCTION FEED IS WIDTH-CAPPED, AND THE CAP IS FINDABLE.  EDGE's vehicle
    description is one string of the form "<MODEL> <TRIM/BODY DETAIL>" cut to a
    fixed width, then split into the Model and Style columns.  Reconstructing
    MODEL + ' ' + STYLE (removing the overlap EDGE leaves behind, e.g.
    model="Silverado 2500" style="2500HD LTZ" -> "SILVERADO 2500HD LTZ") gives a
    length histogram with a wall:

            len 18:  806     len 19:  965     len 20: 2842     len 21: 138

    and for FIVE of the THIRTEEN auctions the 99.9th percentile of reconstructed
    length is exactly 20, with 8-14% of every one of those feeds' rows sitting
    on it.  The other eight run out to 28-51 with no mass anywhere.  That is not
    a heuristic, it is the export's column width -- and it is MEASURED, not
    assumed: `measure_feed_widths` re-derives it, which is how speedwayaa (2,988
    rows, capped at 20) was found without anyone hand-listing it.  So:

        TRUNCATION IS DETECTED BY ARITHMETIC ON THE ROW, NOT BY LOOKING AT THE
        STRING.  A style is truncated iff its reconstructed description reaches
        the width of the feed it came from.

    "BIG HORN/LONE S" is flagged because "1500 BIG HORN/LONE S" is 20.
    "AUTOBIOG" is flagged because "RANGE ROVER AUTOBIOG" is 20.
    "XL" on an F-150 is NOT flagged ("F 150 XL" is 8) and can therefore never be
    prefix-extended into "XLT".  The 13 known-distinct short-code pairs are safe
    STRUCTURALLY, not because 8 characters happened to work.

2b. ONE FEED CARRIES TWO UPSTREAM SOURCES, AND CASE TELLS THEM APART.  The raw
    EDGE CSVs (/opt/expwholesale/edge_comps_in/) settle this.  In ONE file, on
    ONE sale date, Cadillac XT4 appears as both:

        2023,Cadillac,XT4,PREMIUM LUXU      <- ALL CAPS, cut
        2021,Cadillac,XT6,Premium Luxury    <- Mixed Case, whole

    "XT4 PREMIUM LUXU" is only 16 characters, so the width rule cannot see it.
    Hence a second, independent trigger: an ALL-UPPERCASE style that is NOT a
    trim in its own right, and that some COMPLETE entry extends, is a cut of
    that entry.  "Not a trim in its own right" is the guard that keeps XL, LT,
    SE, S, LX, SR, LS, GT, EX and SV safe — every one of them is in
    ymmt_catalog, so none is ever read as a cut of its longer sibling.
    Over-flagging costs misses, never false matches, because rule 3 then
    abstains on any residue with more than one completion.

3.  A FLAGGED RESIDUE IS RESOLVED BY UNIQUE PREFIX, AND AMBIGUITY ABSTAINS.
    That is the whole safety argument, and it does NOT rest on "short codes are
    never truncated" — a 17-character model plus "LT" also reaches 20.  It rests
    on this: a flagged residue resolves only when EXACTLY ONE vocabulary entry
    for that make+model extends it.  "LIMIT" on a model whose only Limited-ish
    trim is "Limited" resolves; "LIMIT" on a Highlander (Limited AND Limited
    Platinum) abstains.  "UNLIMITED S" on a Wrangler abstains — Unlimited Sport,
    Unlimited Sahara and Unlimited Sport S all extend it (112 rows correctly
    abstained rather than guessed).  A residue that is itself a complete entry
    but also has longer extensions abstains too: you cannot tell a complete "LT"
    from a cut "LTZ".

3b. A CUT THAT LANDED ON A TOKEN BOUNDARY CANNOT BE RESOLVED AT ALL.
    "S5 CABRIOLET PREMIUM" is exactly 20 characters, so the row is flagged -- but
    the original was equally plausibly "Premium" or "Premium Plus" and the row
    carries nothing that separates them.  So a flagged residue resolves only when
    the completion CONTINUES ITS LAST TOKEN: "LIMIT"->"LIMITED", "AUTOBIOG"->
    "AUTOBIOGRAPHY", "LONGHOR"->"LONGHORN", "BIG HORN/LONE S"->"BIG HORN/LONE
    STAR".  Those fragments are not words any trim ladder contains, so they are
    provably incomplete.  A boundary cut abstains.  Both of these rules were
    written after an INDEPENDENT 245-pair labelled corpus (trim_match_eval.py,
    authored by another session, labels from manufacturer ladders) caught them:
    'Cabriolet Premium Plus' ~ 'CABRIOLET PREMIUM' and 'Unlimited Rubicon' ~
    'UNLIMITED R'.

4.  STYLE IS OFTEN NOT A TRIM AT ALL.  27.8% of joinable auction rows carry a
    style that matches no catalog trim: body ("4WD 4DR", "SEDAN LX", "Double Cab
    Truck", "7-PASSENGER"), engine ("2.5 S" — note norm strips the point, so it
    arrives as "25 S"), packages ("W/A-SPEC PAC"), or nothing at all (6.7% of
    all 37,831 rows carry no style whatsoever).  The distinction is made by STRIPPING, not by classifying: body /
    door / drivetrain / transmission / package tokens are removed, and

        IF NOTHING SURVIVES, THE VENDOR STRING CARRIED NO TRIM -> ABSTAIN.

    "4WD 4DR" strips to empty.  "W/TECHNOLOGY PAC" strips to empty.  Neither can
    ever match anything, which is the correct outcome.

5.  bids.ymmt_id IS NOT A TRIM KEY.  Of the 3,802 bids carrying a ymmt_id, only
    45.2% have a catalog trim that agrees with bids.trim.  The disagreements are
    nearest-neighbour guesses, and some are dangerous: "AMG C63" -> "AMG GLS 63",
    "SE" -> "P530 SE", "R" -> "R/T", "Stingray" -> "Stingray 1LT".  1,565 of the
    2,085 disagreements carry trim_confidence='low', so unlike the VIN decoder's
    flat 0.95 that flag does mean something — but a 45% key is not a key.
    THIS MODULE NEVER READS ymmt_id.  It resolves bids.trim (falling back to
    canon_trim) through exactly the same path as the auction style, so both
    sides are normalized by one set of rules.

-------------------------------------------------------------------------------
THE GOVERNING ASYMMETRY (read this before editing the stoplists)
-------------------------------------------------------------------------------
Removing a token can create a FALSE MATCH ("Unlimited Sport" -> "Sport" would put
2-door Wranglers under a 4-door).  Keeping a token can only create a MISS.  False
matches are the failure the operator named; misses are free.  THEREFORE, WHEN IN
DOUBT, KEEP THE TOKEN.  A token belongs in a stoplist below only if it can never
by itself separate two price tiers.  UNLIMITED, SPORT, TURBO, HYBRID, PREMIUM,
TOURING, LIMITED, GRAND and every model-line word are deliberately NOT stripped.

-------------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT DONE
-------------------------------------------------------------------------------
* No LLM in the match path.  The local 9B has been fabricating VIN decodes since
  2026-06-11 (935 of 1,950 cached decodes contradict the VIN's own WMI, 1,655 sit
  at exactly 0.95 confidence).  Its self-reported confidence carries zero
  information, so it can never be a runtime authority here.  See the 9B section
  in the report and `propose_with_9b()` below: offline, once, proposals only,
  every one checked against the catalog before it may enter the vocabulary.
* No fuzzy distance, no shared-prefix threshold, no edit distance.  Those are the
  instruments that merged XL with XLT.
* No engine/displacement stripping.  A first cut rewrote "SE V6" -> "SE" and
  "25 SL" -> "SL" when the result landed in the vocabulary.  Those tokens carry
  price, so the rule could only ever create false matches; it was doing 22 of
  30,061 resolutions and every one was the harmful case.  Deleted -- see the
  tombstone above canon_trim_text().
* T2 (below) is gated to CAPPED FEEDS ONLY.  Ungated it also ran on bid trims
  and on uncapped feeds, where nothing can have been cut, and it rewrote 61
  strings into a longer sibling: LT->LTZ, GL->GLS, XR->XRS, CX->CXL, X->XE,
  S->SPORT, L->LIMITED, SEL->SEL PREMIUM, GT->GTS, R->R/T, EX->EX-L.  That is
  the operator's failure exactly, produced by a rule meant to prevent it.  The
  gate is one condition and it closes all 61.
* No body gate.  Body/cab/drivetrain tokens are stripped symmetrically from BOTH
  the catalog and the vendor string, so a SuperCrew XLT and a SuperCab XLT both
  canonicalize to XLT.  That is a deliberate, documented limitation: this module
  gates the TRIM TIER the operator named, not the cab configuration.  The tokens
  removed are returned in Resolution.body_terms so a future body gate can be
  built on top without redesigning this one.

-------------------------------------------------------------------------------
FAILURE MODES THAT REMAIN — do not claim "zero false matches by construction"
-------------------------------------------------------------------------------
The resolver compares KEYS, so it cannot merge two trims it resolved correctly.
It can still resolve one of them WRONG.  Three paths, in order of exposure:

1.  T2 + a catalog gap + a lone sibling.  On a CAPPED feed, an uppercase surface
    that (a) is missing from ymmt_catalog, (b) was never seen on any of the eight
    uncapped feeds or in Mixed Case, and (c) has exactly ONE longer completion,
    resolves to that completion.  If the surface was a real trim, that is a false
    match.  The Chevrolet Equinox "L" trim is genuinely absent from the catalog
    and is the live example — it survives only because Equinox has several L*
    trims, so the ambiguity rule catches it.  A model with exactly one would not
    be caught.  MITIGATION: fill the catalog gap; the review queue surfaces them.

2.  The hand aliases.  Two rows, both Ram badge facts.  If one is wrong, every
    comp it touches is wrong.  They are in a table so they can be deleted.

3.  Body-tier merging, by design.  "SuperCrew XLT" and "SuperCab XLT" both
    canonicalize to XLT; a BMW "Competition Coupe" and "Competition Convertible"
    both to COMPETITION.  Same trim tier, different body — the operator's rule is
    about tier, but a buyer would price these differently.

Measured honestly: on 1,200 recent bids the live heuristic accepts 11 pairs whose
two sides both resolve to KNOWN and DIFFERENT trims; this module accepts 0 under
the same test.  That is "0 provable", not "0 possible".
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from edge_canon import canon_make, canon_model, models_match  # noqa: E402

# ===========================================================================
# 1. NORMALIZATION
# ===========================================================================
# edge_canon.norm_text is not reused directly: it deletes '/' and '.' before we
# get to look at them, and both carry signal.  "W/TECHNOLOGY" marks a package;
# "BIG HORN/LONE STAR" is one trim with two badges; "2.5 S" is a displacement
# plus a trim and must not silently become the token "25".

_PKG_SLASH = re.compile(r"\bW\s*/", re.I)          # "w/Nav", "W/A-SPEC PAC"
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")

# Tokens that describe the BODY.  None of these can separate two price tiers on
# its own.  Verified against ymmt_catalog: where a catalog trim contains one
# (Ford "SuperCrew XLT", BMW "Competition Coupe") it is body contamination in the
# catalog, and the same strip is applied there — symmetry is what makes it safe.
BODY_TOKENS = {
    "SEDAN", "SDN", "SD", "COUPE", "CPE", "CONVERTIBLE", "CONV", "CABRIOLET",
    "CABRIO", "ROADSTER", "HATCHBACK", "HATCH", "HB", "LIFTBACK", "WAGON", "WGN",
    "SPORTWAGEN", "ESTATE", "SUV", "MINIVAN", "VAN", "PICKUP", "TRUCK",
    "UTILITY", "FASTBACK", "NOTCHBACK", "HARDTOP",
    # cab configurations
    "CAB", "CREW", "CREWMAX", "SUPERCREW", "SUPERCAB", "EXTENDED", "EXT",
    "REGULAR", "REG", "DOUBLE", "QUAD", "MEGA", "KINGCAB", "XTRACAB", "ACCESS",
    "CLUB", "DOUBLECAB",
    # door / seat counts
    "DOOR", "DR", "2DR", "3DR", "4DR", "5DR", "2D", "4D", "5D",
    "PASSENGER", "PASS", "SEATER",
    # wheelbase
    "SWB", "LWB", "WHEELBASE", "BODY", "STYLE",
}
# "KING" is NOT here — Ford "King Ranch" is a trim.  "CLUB" is borderline (Mazda
# "MX-5 Club" is a real trim); it is vetoed per-make below by CATALOG ATTESTATION.

DRIVE_TOKENS = {
    "2WD", "4WD", "AWD", "FWD", "RWD", "4X4", "4X2", "2X4", "4WD/AWD",
    "4MATIC", "4MATIC+", "QUATTRO", "XDRIVE", "SDRIVE", "ZDRIVE",
    "4A", "4X4/AWD", "ALL4",
}
# Bare XDRIVE/SDRIVE only.  "XDRIVE40I" arrives as ONE token and is a real BMW
# trim, so it is untouched.

TRANS_TOKENS = {"AUTO", "AUTOMATIC", "AT", "MANUAL", "MAN", "MT", "CVT", "6MT",
                "5MT", "6AT", "8AT", "SPEED", "SPD"}

PKG_TOKENS = {"PKG", "PKGE", "PACKAGE", "PAC", "PACK", "GROUP", "GRP", "EQUIP",
              "EQUIPMENT", "PREF", "PREFERRED EQUIPMENT", "OPTION", "OPT"}
# NOTE: "PREFERRED" alone is a real trim (Kia, Buick) and is NOT here.

NOISE_TOKENS = BODY_TOKENS | DRIVE_TOKENS | TRANS_TOKENS

# Engine-displacement shapes.  EDGE writes "2.5 S"; punctuation stripping makes
# it "25 S".  These are RECOGNISED but NEVER stripped — see the tombstone above
# canon_trim_text().  They are kept so a human working the review queue can tell
# an engine code from a trim code at a glance, and because two-digit tokens are
# otherwise meaningful ("63" is AMG 63, "350" is an ES 350).
_DISPL = re.compile(r"^\d{2}[LT]?$")
_DISPL_DOTTED = re.compile(r"^\d\.\d[LT]?$")

# A bare single digit is a seat or door count left behind after its noun was
# stripped ("7-PASSENGER" -> "7", "5DR" -> "5").  It is noise EXCEPT where the
# catalog attests it for the make: Porsche's "4" (Carrera 4, Panamera 4, Macan 4)
# is a real trim, and the per-make `protect` set keeps it.
_BARE_COUNT = re.compile(r"^[1-9]$")


class Surface:
    """A vendor string decomposed into trim tokens and discarded noise."""

    __slots__ = ("raw", "tokens", "body_terms", "pkg_terms", "had_package")

    def __init__(self, raw, tokens, body_terms, pkg_terms, had_package):
        self.raw = raw
        self.tokens = tokens
        self.body_terms = body_terms
        self.pkg_terms = pkg_terms
        self.had_package = had_package

    @property
    def text(self):
        return " ".join(self.tokens)

    def __repr__(self):
        return "Surface(%r -> %r, body=%r, pkg=%r)" % (
            self.raw, self.text, self.body_terms, self.pkg_terms)


def decompose(s, protect=()):
    """Split a vendor trim string into surviving trim tokens and discarded noise.

    `protect` is the set of tokens attested as trim-bearing for this make in the
    canonicalized catalog.  A protected token is NEVER stripped — that is how
    "Club" survives on an MX-5 while being stripped on a truck, without a
    per-make hand-written exception list.
    """
    if s is None:
        return Surface("", [], [], [], False)
    raw = str(s)
    txt = raw.upper()

    # 1. "W/" marks the start of a package descriptor.  Everything after it, to
    #    the end of the string, is package text.  Detected BEFORE punctuation is
    #    destroyed so that a truncated Willys ("UNLIMITED W") is not mistaken for
    #    a package marker.
    had_package = False
    m = _PKG_SLASH.search(txt)
    if m:
        had_package = True
        pkg_tail = txt[m.start():]
        txt = txt[:m.start()]
    else:
        pkg_tail = ""

    # 2. '/' and '-' are token separators, not deletions.  "BIG HORN/LONE STAR"
    #    must become two badges, not "BIG HORNLONE STAR".
    txt = txt.replace("/", " ").replace("-", " ").replace(".", ".")
    txt = _PUNCT.sub(" ", txt.replace(".", " "))
    toks = _WS.sub(" ", txt).strip().split()

    pkg_terms = _WS.sub(" ", _PUNCT.sub(" ", pkg_tail.replace("/", " "))).strip().split()

    keep, body = [], []
    for t in toks:
        if t in protect:
            keep.append(t)
        elif t in NOISE_TOKENS or _BARE_COUNT.match(t):
            body.append(t)
        elif t in PKG_TOKENS:
            had_package = True
            pkg_terms.append(t)
        else:
            keep.append(t)
    return Surface(raw, keep, body, pkg_terms, had_package)


# ---------------------------------------------------------------------------
# REMOVED 2026-09-02: the conditional engine/displacement strip.
#
# It rewrote "25 SL" -> "SL", "SE V6" -> "SE", "25T PRESTIGE" -> "PRESTIGE"
# whenever the stripped form landed in the vocabulary.  Every token it dropped
# CARRIES PRICE: a Camry SE V6 is not a Camry SE, an Altima 2.5 SL is not a
# 3.5 SL, a Tacoma SR5 V6 is not a 4-cylinder SR5.  That is exactly what the
# governing asymmetry at the top of this file forbids -- the rule could only
# ever create a false match.
#
# Measured before deleting: load-bearing for 22 of 30,061 resolutions (0.07%),
# and all 22 were the harmful case ("SE V6"->"SE", "LIMITED V6"->"LIMITED",
# "25T PRESTIGE"->"PRESTIGE").  The decorated forms are already admitted as
# observed vocabulary entries, so they exact-match each other.  What is lost is
# only a bid saying "S" matching a comp saying "2.5 S" -- and that SHOULD miss,
# because the bid never named the engine.
#
# Do not reintroduce it.  If a decorated surface must reach a plainer trim, that
# is a hand-verified trim_alias row with a written justification.
# ---------------------------------------------------------------------------


def canon_trim_text(s, protect=()):
    """The canonical form of one trim string: uppercase, punctuation-normalized,
    body/door/drivetrain/transmission/package tokens removed, tokens joined by a
    single space.  Empty string means 'this string carried no trim'."""
    return decompose(s, protect).text


# ===========================================================================
# 2. TRUNCATION — the feed width
# ===========================================================================

def reconstruct(model, style):
    """Rebuild EDGE's pre-split description from the Model and Style columns.

    EDGE normalizes the model ("Silverado 2500") but the Style column keeps the
    tail of the ORIGINAL description, which re-states part of the model in its
    original form ("2500HD LTZ").  Merging on that overlap recovers
    "SILVERADO 2500HD LTZ" — 20 characters, the export's column width.
    """
    m = str(model or "").upper().replace("-", " ").split()
    s = str(style or "").upper().replace("-", " ").split()
    k = 0
    for j in range(min(len(m), len(s)), 0, -1):
        if all(s[i].startswith(m[len(m) - j + i]) for i in range(j)):
            k = j
            break
    return " ".join(m[:len(m) - k] + s)


# Widths measured 2026-09-02 over 25,634 styled auction_comps rows.  These are
# DEFAULTS ONLY — `TrimVocab.load` overwrites them from trim_feed_width, which
# `--build` recomputes from the data.  An auction that is not listed is treated
# as UNCAPPED: nothing is ever flagged, so nothing is ever prefix-extended.
DEFAULT_FEED_WIDTH = {
    "orlandolongwoodaafl": 20,   # n=10486  max reconstructed 21 (1 row)  13.2% at 20
    "southfloridaaa": 20,        # n= 8263  max reconstructed 20         14.3% at 20
    "speedwayaa": 20,            # n= 2988  12.8% at 20  <- found by measurement
    "orlandoaa": 20,             # n=  967  13.1% at 20
    "vemoaag": 20,               # n=  209   8.1% at 20
    # NOT capped — reconstructed lengths run to 50/51 with no mass at any width:
    #   daxtampafl2 (n=4031, max 50), anaaorlando (3454, 51),
    #   jacksonvilleaa (2477, 50), aaapensacola (1010, 50), aaayatb (812, 50),
    #   aaayasa (382, 36), aaayafm (175, 36), aaayam (59, 28)
}

# A feed is declared capped only if this share of its rows sits exactly at the
# maximum reconstructed length.  A genuine cap produces a wall; an uncapped feed
# produces a tail.  Measured: capped feeds 8.1%-14.3%, uncapped feeds 0.0%-4.6%.
WIDTH_MASS_THRESHOLD = 0.06
WIDTH_MIN_ROWS = 150


def measure_feed_widths(rows, mass_threshold=WIDTH_MASS_THRESHOLD,
                        min_rows=WIDTH_MIN_ROWS):
    """rows: iterable of (auction_slug, model, style).  Returns
    {slug: (width, n, pct_at_width)} for feeds that show a hard cap.

    A width is claimed only when the 99.9th percentile of reconstructed lengths
    carries at least `mass_threshold` of the feed's rows.  The percentile rather
    than the max absorbs merge artifacts (one Kia Spectra5 row reconstructs to 21
    because EDGE renamed the model to 'Spectra5' after the cut)."""
    per = defaultdict(Counter)
    for slug, model, style in rows:
        if not style or not str(style).strip():
            continue
        per[slug][len(reconstruct(model, style))] += 1
    out = {}
    for slug, h in per.items():
        n = sum(h.values())
        if n < min_rows:
            continue
        lengths = sorted(h)
        cum = 0
        w = lengths[-1]
        for L in lengths:                      # 99.9th percentile
            cum += h[L]
            if cum >= 0.999 * n:
                w = L
                break
        pct = h[w] / float(n)
        if pct >= mass_threshold:
            out[slug] = (w, n, pct)
    return out


# ===========================================================================
# 3. THE VOCABULARY
# ===========================================================================

# An observed (non-catalog) surface must be seen this many times, UNTRUNCATED,
# before it is admitted.  Rare surfaces are usually one-off vendor noise, and an
# admitted entry is a prefix-resolution TARGET — a bad one would be load-bearing.
OBSERVED_MIN_SUPPORT = 3

VOCAB_DDL = """
-- ===================================================================
-- trim_schema.py backing tables.  NEW TABLES.  Nothing here writes to
-- bids, auction_comps, ymmt_catalog or any table the live card reads.
--
-- MAINTENANCE NOTE: these are CREATE TABLE IF NOT EXISTS, which will NOT
-- add a column to a table that already exists.  If a column is added
-- here later, ship the matching ALTER TABLE by hand -- `--build --apply`
-- will otherwise fail on the missing column rather than migrate it.
-- ===================================================================

-- The closed vocabulary: every canonical trim EW is willing to name, per
-- make+model.  Regenerated by `python3 trim_schema.py --build`.
CREATE TABLE IF NOT EXISTS trim_vocab (
    make        text    NOT NULL,          -- edge_canon.canon_make
    model       text    NOT NULL,          -- edge_canon.canon_model
    canon_trim  text    NOT NULL,          -- canon_trim_text() output
    source      text    NOT NULL,          -- 'catalog' | 'observed'
    support     integer NOT NULL DEFAULT 0,-- untruncated sightings (observed)
    mixed_seen  boolean NOT NULL DEFAULT false, -- PROVEN COMPLETE: seen on an
                                           -- uncapped feed, or in Mixed Case.
                                           -- Powers the T2 truncation trigger.
    example     text,                      -- a raw surface that produced it
    built_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (make, model, canon_trim)
);
CREATE INDEX IF NOT EXISTS idx_trim_vocab_mm ON trim_vocab (make, model);

-- HAND-VERIFIED equivalences only.  This is the ONLY place where two different
-- strings are declared to be the same trim.  Every row needs a note saying why.
-- Nothing may be inserted here by a script, an LLM, or a similarity score.
CREATE TABLE IF NOT EXISTS trim_alias (
    make        text    NOT NULL,
    model       text    NOT NULL,          -- '*' = every model of this make
    surface     text    NOT NULL,          -- canon_trim_text() of the alias
    canon_trim  text    NOT NULL,          -- the vocabulary entry it means
    note        text    NOT NULL,          -- WHY.  required.
    approved_by text    NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (make, model, surface)
);

-- Per-feed truncation width, recomputed by --build from the data itself.
CREATE TABLE IF NOT EXISTS trim_feed_width (
    auction_slug text PRIMARY KEY,
    width        integer NOT NULL,
    n_rows       integer NOT NULL,
    pct_at_width numeric(5,4) NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now()
);

-- Surfaces that could not be resolved.  THIS IS THE MAINTENANCE STORY: a new
-- trim appears in the feed, fails to resolve, lands here, and abstains from the
-- card until a human admits it to trim_vocab or trim_alias.  Growth in this
-- table is the signal that the vocabulary needs a refresh.
CREATE TABLE IF NOT EXISTS trim_review_queue (
    make        text NOT NULL,
    model       text NOT NULL,
    surface     text NOT NULL,
    status      text NOT NULL,             -- the abstain reason
    n           integer NOT NULL DEFAULT 1,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (make, model, surface)
);
"""

# Seed aliases.  DELIBERATELY TINY.  Each one is a manufacturer fact, not a
# similarity judgement, and each is the kind of claim the operator can check in
# ten seconds.  Anything less certain than these belongs in the review queue.
SEED_ALIASES = [
    ("RAM", "*", "BIG HORN LONE STAR", "BIG HORN",
     "Lone Star is Ram's Texas-market badge for the Big Horn; same truck, same "
     "content. The window sticker literally reads 'Big Horn/Lone Star'."),
    ("RAM", "*", "LONE STAR", "BIG HORN",
     "Same as above, badge name alone."),
]


class Resolution:
    """The outcome of resolving one vendor string."""

    __slots__ = ("status", "canon", "reason", "candidates", "body_terms",
                 "pkg_terms", "truncated", "surface")

    RESOLVED = "resolved"
    NO_TRIM = "abstain_no_trim"                 # string carried only body/pkg
    EMPTY = "abstain_empty"                     # nothing there at all
    NO_VOCAB = "abstain_no_vocabulary"          # we know nothing about this YMM
    UNKNOWN = "abstain_unknown_surface"         # not in vocab, not truncated
    AMBIGUOUS = "abstain_ambiguous_truncation"  # >1 completion
    UNRESOLVABLE = "abstain_truncated_no_match"  # truncated, 0 completions

    def __init__(self, status, canon=None, reason="", candidates=(),
                 body_terms=(), pkg_terms=(), truncated=False, surface=""):
        self.status = status
        self.canon = canon
        self.reason = reason
        self.candidates = list(candidates)
        self.body_terms = list(body_terms)
        self.pkg_terms = list(pkg_terms)
        self.truncated = truncated
        self.surface = surface

    @property
    def ok(self):
        return self.status == Resolution.RESOLVED

    def __repr__(self):
        return "Resolution(%s, canon=%r, trunc=%s, %s)" % (
            self.status, self.canon, self.truncated, self.reason)


class TrimVocab:
    """The closed per-(make, model) trim vocabulary, plus the feed widths."""

    def __init__(self):
        self.entries = defaultdict(set)     # (make, model) -> {canon_trim}
        self.catalog_entries = defaultdict(set)   # (make, model) -> {catalog canon_trim}
        self.complete_attested = defaultdict(set)  # (make,model) -> {canon PROVEN complete}
        self.sources = {}                   # (make, model, canon) -> 'catalog'|'observed'
        self.protect = defaultdict(set)     # make -> {token attested as trim}
        self.aliases = {}                   # (make, model, surface) -> canon
        self.widths = dict(DEFAULT_FEED_WIDTH)
        self._model_index = defaultdict(list)   # make -> [model, ...]

    # -- construction ------------------------------------------------------
    def add_catalog(self, make, model, trim_raw):
        mk, md = canon_make(make), canon_model(model)
        # Two passes: the FIRST pass over the whole catalog fills self.protect,
        # so callers must run add_catalog_protect() over everything first.
        c = canon_trim_text(trim_raw, self.protect.get(mk, ()))
        if not c:
            return
        self.entries[(mk, md)].add(c)
        self.catalog_entries[(mk, md)].add(c)
        self.sources.setdefault((mk, md, c), "catalog")

    def add_catalog_protect(self, make, trim_raw):
        """Pass 1: record which tokens survive body-stripping in THIS make's
        catalog trims.  A token that survives here is trim-bearing for the make
        and is protected from stripping in pass 2 and at resolve time.

        Bootstrapped with an empty protect set so the global stoplist applies to
        the catalog too — that is what turns Ford's "SuperCrew XLT" into "XLT"
        instead of protecting SUPERCREW forever.
        """
        mk = canon_make(make)
        for t in decompose(trim_raw).tokens:
            self.protect[mk].add(t)

    def add_observed(self, make, model, canon, support, example=None, mixed=False):
        """Admit an observed surface into the vocabulary.

        VOCABULARY HYGIENE: an admitted entry becomes a prefix-resolution TARGET,
        so a truncated string must never get in.  Two guards:
          * only UNTRUNCATED sightings are counted (caller's job), and
          * a surface that is a STRICT PREFIX of a catalog trim for this
            make+model, and is not itself a catalog trim, is refused.
            "PREMIUM LUXU" is a strict prefix of the catalog's "Premium Luxury"
            and is not a trim in its own right -> refused.  "LT" is a strict
            prefix of "LTZ" but IS a catalog trim -> admitted.
        """
        mk, md = canon_make(make), canon_model(model)
        if not canon or support < OBSERVED_MIN_SUPPORT:
            return False
        if mixed:
            self.complete_attested[(mk, md)].add(canon)
        if canon in self.entries[(mk, md)]:
            return False
        cat = self.catalog_entries.get((mk, md), ())
        if canon not in cat and any(t.startswith(canon) and t != canon for t in cat):
            return False
        self.entries[(mk, md)].add(canon)
        self.sources[(mk, md, canon)] = "observed"
        return True

    def add_alias(self, make, model, surface, canon):
        self.aliases[(canon_make(make), canon_model(model) if model != "*" else "*",
                      surface)] = canon

    def finalize(self):
        self._model_index.clear()
        for (mk, md) in self.entries:
            self._model_index[mk].append(md)

    # -- lookup ------------------------------------------------------------
    def _model_key(self, mk, md):
        """Exact model key, else the UNIQUE model family that EW's own
        models_match links to it.  If two catalog models both match, we cannot
        tell which lineup the trim belongs to -> no vocabulary -> abstain."""
        if (mk, md) in self.entries:
            return md
        cands = [m for m in self._model_index.get(mk, ()) if models_match(md, m)]
        return cands[0] if len(cands) == 1 else None

    def alias(self, mk, md, text):
        """Map a surface OR a resolved vocabulary entry onto its canonical
        representative.  Applied in BOTH directions: on the raw surface before
        resolution (so a badge string short-circuits) and on the resolved canon
        afterwards (so a truncation that completes to 'BIG HORN LONE STAR' still
        lands on 'BIG HORN', the same key the bid side produces).  Idempotent."""
        for key in ((mk, md, text), (mk, "*", text)):
            if key in self.aliases:
                return self.aliases[key]
        return text

    def vocab_for(self, make, model):
        mk = canon_make(make)
        md = self._model_key(mk, canon_model(model))
        if md is None:
            return mk, None, frozenset()
        return mk, md, frozenset(self.entries[(mk, md)])

    # -- the decision ------------------------------------------------------
    def resolve(self, make, model, style, *, auction_slug=None, feed_model=None):
        """Resolve one vendor trim string to a canonical trim, or ABSTAIN.

        `auction_slug` + `feed_model` are supplied for AUCTION rows so truncation
        can be detected.  A bid has no feed width, so a bid string is never
        treated as truncated — bids come from a decoder or a dealer, not from a
        width-capped export.
        """
        mk, md, vocab = self.vocab_for(make, model)
        surf = decompose(style, self.protect.get(mk, ()))
        raw_present = bool(str(style or "").strip())

        if not raw_present:
            return Resolution(Resolution.EMPTY, reason="no style/trim on the row",
                              surface="")
        if not surf.tokens:
            return Resolution(
                Resolution.NO_TRIM,
                reason="all tokens were body/drivetrain/package: body=%s pkg=%s"
                       % (surf.body_terms, surf.pkg_terms),
                body_terms=surf.body_terms, pkg_terms=surf.pkg_terms,
                surface="")

        text = surf.text

        # ---- TRUNCATION: two independent triggers, either sufficient --------
        #
        # T1 (ARITHMETIC).  The row's feed has a measured column width and the
        # reconstructed description reaches it.  A property of the ROW, not of
        # the string.  Catches "BIG HORN/LONE S", "AUTOBIOG", "LIMIT", "LARED".
        #
        # T2 (CROSS-SOURCE EVIDENCE).  The raw CSVs show that a single EDGE feed
        # carries TWO upstream sources: one ALL-UPPERCASE and width-capped, one
        # Mixed-Case and complete.  The same Cadillac XT4 appears on the same
        # feed as "PREMIUM LUXU" (uppercase, cut) and "Premium Luxury" (mixed,
        # whole) — and "XT4 PREMIUM LUXU" is only 16, so T1 cannot see it.  So:
        # an ALL-UPPERCASE surface that is NOT a trim in its own right, and that
        # some longer entry extends, is a cut of that entry.
        #
        # The guard that makes T2 safe is "not a trim in its own right": XL is
        # in ymmt_catalog for Ford, so an uppercase "XL" is never treated as a
        # cut "XLT".  Over-flagging costs MISSES (the ambiguity rule below then
        # abstains), never false matches.
        truncated = False
        trunc_why = ""
        if auction_slug is not None:
            w = self.widths.get(auction_slug)
            if w is not None and len(reconstruct(
                    feed_model if feed_model is not None else model, style)) >= w:
                truncated, trunc_why = True, "T1 feed width %d" % w
        # T2 is gated on the row coming from a CAPPED FEED, exactly like T1.
        # Its entire justification is "this feed carries an uppercase, cut
        # upstream source".  Neither of the other two cases has that property:
        #   * a BID trim comes from NHTSA / a dealer / AccuTrade — no column
        #     limit anywhere in that path, so nothing there is ever a cut;
        #   * an UNCAPPED feed is complete by construction — that is the same
        #     argument _derive_observed uses to prove entries whole.
        # Letting T2 run in those two places would rewrite a genuine short trim
        # into a longer sibling whenever the catalog had a gap and exactly one
        # sibling existed — the Equinox-"L" case without the ambiguity rescue.
        if not truncated and md is not None and auction_slug in self.widths:
            complete = (self.complete_attested.get((mk, md), set())
                        | self.catalog_entries.get((mk, md), set()))
            if text not in complete and not any(ch.islower() for ch in str(style)):
                longer = [v for v in self.entries[(mk, md)]
                          if v.startswith(text) and v != text and v in complete]
                if longer:
                    truncated = True
                    trunc_why = "T2 cut of %s" % sorted(longer)[:3]

        if md is None:
            return Resolution(Resolution.NO_VOCAB,
                              reason="no catalog/observed vocabulary for %s %s"
                                     % (mk, canon_model(model)),
                              body_terms=surf.body_terms, pkg_terms=surf.pkg_terms,
                              truncated=truncated, surface=text)

        # Hand-verified alias on the raw surface (model-specific, then make-wide).
        aliased = self.alias(mk, md, text)
        if aliased != text:
            return Resolution(Resolution.RESOLVED, canon=aliased,
                              reason="hand-verified alias %r -> %r" % (text, aliased),
                              body_terms=surf.body_terms, pkg_terms=surf.pkg_terms,
                              truncated=truncated, surface=text)

        def _res(canon, reason, **kw):
            return Resolution(Resolution.RESOLVED, canon=self.alias(mk, md, canon),
                              reason=reason, body_terms=surf.body_terms,
                              pkg_terms=surf.pkg_terms, surface=text, **kw)

        if not truncated:
            # A complete string matches its vocabulary entry EXACTLY.  No prefix
            # logic ever runs here.  This is what makes XL/XLT, LT/LTZ, SE/SEL,
            # S/SE, LX/LXS, SR/SR5, LS/LSX, GT/GTS, EX/EXL, SV/SVT, L/LT,
            # Base/Base Preferred and Limited/Limited Platinum structurally safe.
            if text in vocab:
                return _res(text, "exact vocabulary entry")
            return Resolution(Resolution.UNKNOWN,
                              reason="%r not in the %s %s vocabulary" % (text, mk, md),
                              body_terms=surf.body_terms, pkg_terms=surf.pkg_terms,
                              surface=text)

        # ---- truncated: resolve by UNIQUE prefix, else abstain ----
        # Completions are deduped by their ALIASED form first: 'BIG HORN LONE
        # STAR' and 'LONE STAR' are two surfaces for one trim, so a residue that
        # both extend is not actually ambiguous.  Ambiguity is counted in
        # CANONICAL TRIMS, which is the thing the operator cares about.
        # A cut that landed on a TOKEN BOUNDARY is not recoverable.  "S5
        # CABRIOLET PREMIUM" is 20 characters; the original could equally have
        # been "Premium" or "Premium Plus", and nothing in the row says which.
        # A cut that landed MID-TOKEN is different: "LIMIT", "AUTOBIOG",
        # "LONGHOR", "BIG HORN/LONE S" are not words any ladder contains, so the
        # string is provably incomplete.  Only mid-token cuts may resolve.
        cands = sorted(v for v in vocab
                       if v.startswith(text)
                       and len(v) > len(text) and v[len(text)] != " ")
        canons = sorted({self.alias(mk, md, v) for v in cands})
        if len(canons) == 1 and cands:
            return Resolution(Resolution.RESOLVED, canon=canons[0],
                              reason="truncated (%s); unique completion %s"
                                     % (trunc_why, cands),
                              candidates=cands, body_terms=surf.body_terms,
                              pkg_terms=surf.pkg_terms, truncated=True, surface=text)
        if len(canons) > 1:
            # Includes the case where `text` is itself a complete entry AND has
            # longer extensions: a complete "LT" is indistinguishable from a cut
            # "LTZ" once the row has reached the width.  Abstain.
            return Resolution(Resolution.AMBIGUOUS,
                              reason="truncated; %d distinct trims complete it: %s"
                                     % (len(canons), canons[:6]),
                              candidates=cands, body_terms=surf.body_terms,
                              pkg_terms=surf.pkg_terms, truncated=True, surface=text)
        boundary = sorted(v for v in vocab if v.startswith(text + " "))
        if boundary:
            return Resolution(
                Resolution.AMBIGUOUS,
                reason="truncated (%s) but the cut fell on a token boundary; %r is "
                       "indistinguishable from a complete trim. Boundary "
                       "candidates: %s" % (trunc_why, text, boundary[:5]),
                candidates=boundary, body_terms=surf.body_terms,
                pkg_terms=surf.pkg_terms, truncated=True, surface=text)
        return Resolution(Resolution.UNRESOLVABLE,
                          reason="truncated; no vocabulary entry continues %r" % text,
                          body_terms=surf.body_terms, pkg_terms=surf.pkg_terms,
                          truncated=True, surface=text)


# ===========================================================================
# 4. THE MATCH DECISION
# ===========================================================================

def trims_match(bid_res, comp_res):
    """The only comparison in this module.

    Both sides must have RESOLVED to the SAME canonical trim.  Any abstain on
    either side is NOT a match.  There is no 'unknown means probably fine' path,
    because that path is how a Sport ends up under a Laramie.
    """
    return bool(bid_res.ok and comp_res.ok and bid_res.canon == comp_res.canon)


def match_bid_to_comp(vocab, bid, comp):
    """Convenience wrapper over one bid dict and one auction_comps row dict.

    Returns (matched: bool, bid_res, comp_res) so a caller can log WHY a comp was
    dropped instead of it silently vanishing.

    Bid trim precedence: bids.trim (what the dealer / sticker said) first,
    canon_trim second.  bids.ymmt_id is NEVER consulted — it agrees with
    bids.trim only 45.2% of the time (see the module docstring, point 5).
    """
    bid_year = bid.get("year") or bid.get("canon_year")
    comp_year = comp.get("year")
    bid_trim = bid.get("trim") or bid.get("canon_trim") or ""
    bmk = (bid.get("make") or bid.get("canon_make") or "").strip()
    bmd = (bid.get("model") or bid.get("canon_model") or "").strip()

    bid_res = vocab.resolve(bmk, bmd, bid_trim)
    comp_res = vocab.resolve(comp.get("canon_make") or comp.get("make"),
                             comp.get("canon_model") or comp.get("model"),
                             comp.get("style"),
                             auction_slug=comp.get("auction_slug"),
                             feed_model=comp.get("model"))
    if bid_year and comp_year and int(bid_year) != int(comp_year):
        return False, bid_res, comp_res
    return trims_match(bid_res, comp_res), bid_res, comp_res


# ===========================================================================
# 5. LOADING / BUILDING
# ===========================================================================

def _dsn():
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            return ln.strip().split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("no DATABASE_URL")


def _connect():
    import psycopg2
    return psycopg2.connect(_dsn(), connect_timeout=15)


def _table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + name,))
    return bool(cur.fetchone()[0])


def load(conn=None, use_tables=True):
    """Build the in-memory vocabulary.

    Reads ymmt_catalog always.  Reads trim_vocab / trim_alias / trim_feed_width
    if those tables exist; otherwise derives the observed layer live from
    auction_comps and uses DEFAULT_FEED_WIDTH.  Either way the result is the same
    object, so the module works before its tables are created.
    """
    own = conn is None
    c = conn or _connect()
    v = TrimVocab()
    try:
        cur = c.cursor()
        cur.execute("SELECT make, model, trim FROM ymmt_catalog")
        cat = cur.fetchall()
        for mk, _md, tr in cat:                       # pass 1: protected tokens
            v.add_catalog_protect(mk, tr)
        for mk, md, tr in cat:                        # pass 2: canonical entries
            v.add_catalog(mk, md, tr)

        have = use_tables and _table_exists(cur, "trim_vocab")
        if have:
            cur.execute("SELECT make, model, canon_trim, source, support, mixed_seen "
                        "FROM trim_vocab")
            for mk, md, ct, src, sup, mixed in cur.fetchall():
                if mixed:
                    v.complete_attested[(canon_make(mk), canon_model(md))].add(ct)
                if src == "observed":
                    v.add_observed(mk, md, ct, max(sup, OBSERVED_MIN_SUPPORT),
                                   mixed=bool(mixed))
        else:
            _derive_observed(cur, v)   # (counts, measured) discarded at load time

        if use_tables and _table_exists(cur, "trim_feed_width"):
            cur.execute("SELECT auction_slug, width FROM trim_feed_width")
            for slug, w in cur.fetchall():
                v.widths[slug] = w

        if use_tables and _table_exists(cur, "trim_alias"):
            cur.execute("SELECT make, model, surface, canon_trim FROM trim_alias")
            for mk, md, sf, ct in cur.fetchall():
                v.add_alias(mk, md, sf, ct)
        else:
            for mk, md, sf, ct, _n in SEED_ALIASES:
                v.add_alias(mk, md, sf, ct)
    finally:
        if own:
            c.close()
    v.finalize()
    return v


def _derive_observed(cur, v):
    """Admit observed surfaces: UNTRUNCATED only, support >= OBSERVED_MIN_SUPPORT.

    Truncated surfaces are never admitted — admitting "BIG HORN/LONE S" as a
    vocabulary entry would make it a prefix-resolution TARGET, which is exactly
    the error this module exists to prevent.
    """
    cur.execute("""SELECT auction_slug, canon_make, canon_model, model, style
                     FROM auction_comps WHERE coalesce(style,'') <> ''""")
    rows = cur.fetchall()
    widths = measure_feed_widths([(r[0], r[3], r[4]) for r in rows])
    measured = {}
    for slug, (w, n, p) in widths.items():
        v.widths[slug] = w
        measured[slug] = (n, p)
    counts = Counter()
    complete_counts = Counter()
    example = {}
    for slug, cmk, cmd, mdl, st in rows:
        w = v.widths.get(slug)
        capped_feed = w is not None
        if capped_feed and len(reconstruct(mdl, st)) >= w:
            continue                                   # T1-truncated -> never admit
        ct = canon_trim_text(st, v.protect.get(canon_make(cmk or ""), ()))
        if not ct:
            continue
        counts[(cmk or "", cmd or "", ct)] += 1
        # PROOF OF COMPLETENESS.  Two independent ways to know a surface is whole:
        #   * it came off an UNCAPPED feed -- that feed has no column limit, so it
        #     cannot have cut anything.  This is the strong signal.
        #   * it was written in Mixed Case, the signature of the uncapped upstream
        #     source even when it arrives via a capped feed.
        # The uncapped-feed test is what rescues INITIALISMS: "EX", "LT", "SE" are
        # uppercase by nature, so case alone would read every one of them as a cut
        # and abstain on the most common trims in the book.
        if (not capped_feed) or any(ch.islower() for ch in str(st)):
            complete_counts[(cmk or "", cmd or "", ct)] += 1
        example.setdefault((cmk or "", cmd or "", ct), st)
    # Completeness must be registered BEFORE admission decisions so a surface
    # refused by the hygiene guard still counts as "seen complete".
    for (cmk, cmd, ct), n in complete_counts.items():
        if n >= OBSERVED_MIN_SUPPORT:
            v.complete_attested[(canon_make(cmk), canon_model(cmd))].add(ct)
    for (cmk, cmd, ct), n in counts.items():
        v.add_observed(cmk, cmd, ct, n, example.get((cmk, cmd, ct)),
                       mixed=complete_counts.get((cmk, cmd, ct), 0) >= OBSERVED_MIN_SUPPORT)

    # SECOND PASS -- admit RARE surfaces that strictly EXTEND an entry already in
    # the vocabulary, at support 1.
    #
    # Adding an extension can only ever ADD a completion to some truncated
    # residue, which can only turn a resolve into an ABSTAIN.  It is monotonically
    # safe, so the support floor that protects prefix TARGETS is not needed here.
    # Without this, a rare-but-real trim silently makes a common one look
    # unambiguous: the corpus caught "UNLIMITED R" resolving to Unlimited Rubicon
    # because Unlimited Rubicon 392 and Unlimited Rubicon 4xe were too rare to be
    # admitted -- and a Rubicon 392 is about double the money.
    for (cmk, cmd, ct), n in counts.items():
        if n >= OBSERVED_MIN_SUPPORT:
            continue
        key = (canon_make(cmk), canon_model(cmd))
        cur_set = v.entries.get(key)
        if not cur_set or ct in cur_set:
            continue
        if any(ct.startswith(e) and len(ct) > len(e) for e in cur_set):
            cur_set.add(ct)
            v.sources[(key[0], key[1], ct)] = "observed"
    return counts, measured


# ===========================================================================
# 6. THE 9B — PROPOSALS ONLY, OFFLINE, VERIFIED, NEVER IN THE MATCH PATH
# ===========================================================================

def propose_with_9b(unresolved_tokens, limit=200):
    """Ask the local brain to classify unresolved TOKENS as TRIM/BODY/DRIVE/
    ENGINE/PACKAGE.  Offline tool only.  NEVER called from resolve().

    The brain has been fabricating VIN decodes since 2026-06-11 (935/1,950
    decodes contradict the VIN's own WMI; 1,655/1,950 sit at exactly 0.95
    confidence, fabrications included).  Its confidence field is therefore
    DISCARDED here, and every proposal is checked by verify_proposals() against
    the catalog before it may be used.  Returns raw proposals; the caller must
    verify.
    """
    import json
    import urllib.request

    key = os.environ.get("EW_BRAIN_KEY")
    if not key:
        for ln in open("/etc/ew-brain.env"):
            if ln.strip().startswith("EW_BRAIN_KEY="):
                key = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")
    toks = list(unresolved_tokens)[:limit]
    prompt = (
        "Classify each automotive descriptor token as exactly one of: "
        "TRIM, BODY, DRIVETRAIN, ENGINE, PACKAGE.\n"
        "TRIM = a factory trim level or its name (Laramie, XLT, Denali).\n"
        "BODY = body/cab/door/seat descriptor (Sedan, Crew, 4DR).\n"
        "DRIVETRAIN = 4WD, AWD, quattro, xDrive.\n"
        "ENGINE = displacement or cylinder code (2.5L, V6).\n"
        "PACKAGE = an option package word (Package, Group, Equipment).\n"
        "Return ONLY a JSON object mapping token -> class. No prose.\n\n"
        + json.dumps(toks))
    # Call shape copied from assess_backtest_v2.ask9b — the User-Agent is load
    # bearing, the endpoint's WAF answers 403 to a bare urllib request.
    req = urllib.request.Request(
        "https://brain.experience-wholesale.net/v1/chat/completions",
        data=json.dumps({
            "model": "ew-brain",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % key,
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.load(r)
    txt = body["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else {}


def verify_proposals(vocab, proposals):
    """Check every 9B proposal against observed data.  Returns
    (accepted, rejected) where rejected carries the reason.

    A proposal is REJECTED when:
      * the token survives body-stripping in some make's catalog trim (the
        catalog says it is trim-bearing) but the 9B called it BODY/DRIVE/
        ENGINE/PACKAGE — the catalog wins, always;
      * the class is not one of the five;
      * the token was not in the set we asked about (fabricated key).
    An ACCEPTED non-TRIM proposal is still only a CANDIDATE for the stoplists; it
    is written into this file by a human, never applied at runtime.
    """
    attested = set()
    for toks in vocab.protect.values():
        attested |= toks
    valid = {"TRIM", "BODY", "DRIVETRAIN", "ENGINE", "PACKAGE"}
    accepted, rejected = {}, {}
    for tok, cls in proposals.items():
        t = str(tok).upper().strip()
        c = str(cls).upper().strip()
        if c not in valid:
            rejected[t] = "invalid class %r" % cls
        elif t in attested and c != "TRIM":
            rejected[t] = ("catalog attests %s as trim-bearing; 9B said %s" % (t, c))
        else:
            accepted[t] = c
    return accepted, rejected


# ===========================================================================
# 7. SELF-TEST + SURVEY
# ===========================================================================

# The 13 pairs a naive startswith merged, all 13 wrongly.  Every one must be a
# NON-match here.  These are asserts, not comments.
KNOWN_DISTINCT = [
    ("FORD", "F 150", "XL", "XLT"),
    ("CHEVROLET", "SILVERADO 1500", "LT", "LTZ"),
    ("NISSAN", "ALTIMA", "SE", "SEL"),
    ("NISSAN", "ROGUE", "S", "SE"),
    ("KIA", "FORTE", "LX", "LXS"),
    ("TOYOTA", "TACOMA", "SR", "SR5"),
    ("CHEVROLET", "MALIBU", "LS", "LSX"),
    ("KIA", "STINGER", "GT", "GTS"),
    ("HONDA", "CR V", "EX", "EX L"),
    ("NISSAN", "FRONTIER", "SV", "SVT"),
    ("CHEVROLET", "EQUINOX", "L", "LT"),
    ("KIA", "SOUL", "BASE", "BASE PREFERRED"),
    ("TOYOTA", "HIGHLANDER", "LIMITED", "LIMITED PLATINUM"),
]

# The operator's own sentence, as a test.
OPERATOR_TRIPLE = [("RAM", "1500", "BIG HORN", "SPORT"),
                   ("RAM", "1500", "SPORT", "LARAMIE"),
                   ("RAM", "1500", "BIG HORN", "LARAMIE")]


def selftest(vocab, verbose=True):
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)
        elif verbose:
            print("   ok   %s" % msg)

    print("-- decomposition --")
    check(canon_trim_text("4WD 4DR") == "", "'4WD 4DR' strips to empty (body only)")
    check(canon_trim_text("Double Cab Truck") == "",
          "'Double Cab Truck' strips to empty (body only)")
    check(canon_trim_text("7-PASSENGER") == "", "'7-PASSENGER' strips to empty")
    check(canon_trim_text("W/A-SPEC PAC") == "", "'W/A-SPEC PAC' strips to empty (package)")
    check(canon_trim_text("SEDAN LX") == "LX", "'SEDAN LX' -> 'LX'")
    check(canon_trim_text("Big Horn/Lone Star") == "BIG HORN LONE STAR",
          "'/' is a separator, not a deletion")
    check(canon_trim_text("Unlimited Sport") == "UNLIMITED SPORT",
          "UNLIMITED is NOT stripped (2dr vs 4dr Wrangler is real money)")
    check(canon_trim_text("Turbo") == "TURBO", "TURBO is NOT stripped (911 Turbo)")

    print("-- truncation arithmetic --")
    check(len(reconstruct("1500", "BIG HORN/LONE S")) == 20,
          "'1500'+'BIG HORN/LONE S' reconstructs to exactly 20")
    check(len(reconstruct("Range Rover", "AUTOBIOG")) == 20,
          "'Range Rover'+'AUTOBIOG' reconstructs to exactly 20")
    check(len(reconstruct("Silverado 2500", "2500HD LTZ")) == 20,
          "model/style overlap is merged: 'SILVERADO 2500HD LTZ' = 20")
    check(len(reconstruct("F 150", "XL")) < 20,
          "'F 150 XL' is 8 chars -> can never be flagged truncated")

    print("-- the 13 known-distinct pairs (naive startswith merged all 13) --")
    for mk, md, a, b in KNOWN_DISTINCT:
        ra = vocab.resolve(mk, md, a)
        rb = vocab.resolve(mk, md, b)
        check(not trims_match(ra, rb),
              "%s %s: %r !~ %r  (%s / %s)" % (mk, md, a, b, ra.status, rb.status))

    print("-- the operator's sentence --")
    for mk, md, a, b in OPERATOR_TRIPLE:
        ra, rb = vocab.resolve(mk, md, a), vocab.resolve(mk, md, b)
        check(not trims_match(ra, rb), "%s %s: %r !~ %r" % (mk, md, a, b))
    ra = vocab.resolve("RAM", "1500", "BIG HORN")
    rb = vocab.resolve("RAM", "1500", "Big Horn/Lone Star")
    check(trims_match(ra, rb),
          "RAM 1500: 'BIG HORN' ~ 'Big Horn/Lone Star' via hand-verified alias")

    print("-- truncation resolution --")
    r = vocab.resolve("RAM", "1500", "BIG HORN/LONE S",
                      auction_slug="orlandolongwoodaafl", feed_model="1500")
    check(r.truncated, "'BIG HORN/LONE S' is flagged truncated")
    check(r.ok and r.canon == "BIG HORN",
          "'BIG HORN/LONE S' resolves to BIG HORN (unique completion + alias)")
    r = vocab.resolve("JEEP", "WRANGLER", "UNLIMITED S",
                      auction_slug="orlandolongwoodaafl", feed_model="Wrangler")
    check(r.status == Resolution.AMBIGUOUS,
          "'UNLIMITED S' on a Wrangler ABSTAINS (Sport/Sahara/Sport S all extend it)")
    r = vocab.resolve("CHEVROLET", "SILVERADO 1500", "LT",
                      auction_slug="anaaorlando", feed_model="Silverado 1500")
    check(not r.truncated,
          "an UNCAPPED feed does not flag 'LT' (T1 off, and T2 is vetoed because "
          "LT is a catalog trim in its own right)")

    print("-- T2: the uppercase/mixed-case cut, which the width rule cannot see --")
    r = vocab.resolve("CADILLAC", "XT4", "PREMIUM LUXU",
                      auction_slug="orlandolongwoodaafl", feed_model="XT4")
    check(r.truncated, "'PREMIUM LUXU' is flagged by T2 (16 chars — T1 cannot see it)")
    check(r.ok and r.canon == "PREMIUM LUXURY",
          "'PREMIUM LUXU' resolves to the catalog's PREMIUM LUXURY")
    rb = vocab.resolve("CADILLAC", "XT4", "Premium Luxury")
    check(trims_match(rb, r), "the cut and the whole string now MATCH each other")
    check("PREMIUM LUXU" not in vocab.vocab_for("CADILLAC", "XT4")[2],
          "vocabulary hygiene: a strict prefix of a catalog trim is never admitted")
    # T2 must never turn a short code INTO its longer sibling.  Where the code is
    # in ymmt_catalog, T2 is vetoed outright.  Where the catalog has a GAP — the
    # Chevrolet Equinox "L" trim is genuinely missing — T2 fires and the
    # ambiguity rule abstains.  Either outcome is acceptable; RESOLVING TO THE
    # LONGER SIBLING IS NOT, and that is what this asserts.
    gaps = []
    for mk, md, short, long_ in KNOWN_DISTINCT:
        # a CAPPED feed, so T2 is actually live and its veto is what is tested
        rr = vocab.resolve(mk, md, short, auction_slug="orlandolongwoodaafl",
                           feed_model=md)
        canon_long = canon_trim_text(long_, vocab.protect.get(canon_make(mk), ()))
        check(not (rr.ok and rr.canon == canon_long),
              "%r (%s %s) never resolves to %r  [%s]"
              % (short, mk, md, long_, "veto" if not rr.truncated else rr.status))
        if rr.truncated:
            gaps.append("%s %s %r" % (mk, md, short))
    if gaps:
        print("   note: T2 fired (-> abstain) on %d short codes missing from "
              "ymmt_catalog: %s" % (len(gaps), gaps))
    # T2 must be inert everywhere its premise does not hold.
    for mk, md, short, _long in KNOWN_DISTINCT:
        check(not vocab.resolve(mk, md, short).truncated,
              "T2 is inert on BIDS: %r (%s %s) is never read as a cut" % (short, mk, md))
        check(not vocab.resolve(mk, md, short, auction_slug="anaaorlando",
                                feed_model=md).truncated,
              "T2 is inert on the UNCAPPED feed: %r (%s %s)" % (short, mk, md))
    check(not vocab.resolve("CADILLAC", "XT4", "PREMIUM LUXU").truncated,
          "even 'PREMIUM LUXU' is not treated as a cut when it arrives as a BID trim")

    print("-- match decision --")
    b = {"year": 2022, "make": "Ram", "model": "1500", "trim": "Laramie"}
    c1 = {"year": 2022, "canon_make": "RAM", "canon_model": "1500", "model": "1500",
          "style": "Big Horn", "auction_slug": "orlandolongwoodaafl"}
    c2 = dict(c1, style="Laramie")
    c3 = dict(c1, style="4WD 4DR")
    check(not match_bid_to_comp(vocab, b, c1)[0], "Laramie bid does NOT match Big Horn comp")
    check(match_bid_to_comp(vocab, b, c2)[0], "Laramie bid matches Laramie comp")
    check(not match_bid_to_comp(vocab, b, c3)[0],
          "Laramie bid does NOT match a body-only style (abstain)")
    check(not match_bid_to_comp(vocab, dict(b, year=2021), c2)[0],
          "year band is 0: a 2021 bid does not match a 2022 comp")

    print()
    if fails:
        print("FAILED %d:" % len(fails))
        for f in fails:
            print("   XX  %s" % f)
    else:
        print("ALL SELF-TESTS PASS")
    return not fails


def survey(conn=None):
    """Reproduce every number quoted in the module docstring and the report."""
    own = conn is None
    c = conn or _connect()
    cur = c.cursor()

    cur.execute("""SELECT count(*), count(DISTINCT make||'|'||model),
                          count(DISTINCT trim), min(year), max(year)
                     FROM ymmt_catalog""")
    print("== ymmt_catalog: %d rows, %d make|model, %d distinct trims, %s-%s" % cur.fetchone())

    cur.execute("""SELECT count(*), count(*) FILTER (WHERE coalesce(style,'')=''),
                          count(DISTINCT style), count(DISTINCT auction_slug)
                     FROM auction_comps""")
    n, empt, nst, naa = cur.fetchone()
    print("== auction_comps: %d rows, %d empty style (%.1f%%), %d distinct styles, %d auctions"
          % (n, empt, 100.0 * empt / n, nst, naa))

    cur.execute("""SELECT auction_slug, model, style FROM auction_comps
                    WHERE coalesce(style,'') <> ''""")
    rows = cur.fetchall()
    widths = measure_feed_widths(rows)
    print("\n== feed widths (a wall in the reconstructed-length histogram) ==")
    per = defaultdict(Counter)
    for slug, mdl, st in rows:
        per[slug][len(reconstruct(mdl, st))] += 1
    for slug in sorted(per, key=lambda s: -sum(per[s].values())):
        h = per[slug]
        tot = sum(h.values())
        if slug in widths:
            w, _n, p = widths[slug]
            print("   %-22s n=%6d  CAPPED at %2d  %5.1f%% of rows sit at the cap"
                  % (slug, tot, w, 100 * p))
        else:
            print("   %-22s n=%6d  uncapped (max reconstructed %d)"
                  % (slug, tot, max(h)))

    v = load(conn=c, use_tables=True)
    print("\n== vocabulary ==")
    ncat = sum(1 for k in v.sources.values() if k == "catalog")
    nobs = sum(1 for k in v.sources.values() if k == "observed")
    print("   %d make|model keys, %d entries (%d catalog, %d observed), %d aliases"
          % (len(v.entries), len(v.sources), ncat, nobs, len(v.aliases)))

    print("\n== resolution of auction_comps.style ==")
    cur.execute("""SELECT auction_slug, canon_make, canon_model, model, style, year,
                          count(*) FROM auction_comps GROUP BY 1,2,3,4,5,6""")
    tally = Counter()
    unres = Counter()
    for slug, cmk, cmd, mdl, st, _yr, k in cur.fetchall():
        r = v.resolve(cmk, cmd, st, auction_slug=slug, feed_model=mdl)
        tally[r.status] += k
        if r.status in (Resolution.UNKNOWN, Resolution.AMBIGUOUS,
                        Resolution.UNRESOLVABLE):
            unres[(cmk, cmd, r.surface, r.status)] += k
    tot = sum(tally.values())
    for s, k in tally.most_common():
        print("   %-32s %6d  %5.1f%%" % (s, k, 100.0 * k / tot))
    print("   RESOLVED share of NON-EMPTY styles: %.1f%%"
          % (100.0 * tally[Resolution.RESOLVED]
             / max(1, tot - tally[Resolution.EMPTY])))

    print("\n== top 25 unresolved auction surfaces (the review queue) ==")
    for (cmk, cmd, sf, stt), k in unres.most_common(25):
        print("   %5d  %-12s %-18s %-22r %s" % (k, cmk, cmd, sf, stt))

    print("\n== resolution of bids.trim ==")
    cur.execute("""SELECT coalesce(year,canon_year), coalesce(make,canon_make),
                          coalesce(model,canon_model), trim, canon_trim
                     FROM bids""")
    bt = Counter()
    for _y, mk, md, tr, ctr in cur.fetchall():
        bt[v.resolve(mk or "", md or "", tr or ctr or "").status] += 1
    btot = sum(bt.values())
    for s, k in bt.most_common():
        print("   %-32s %6d  %5.1f%%" % (s, k, 100.0 * k / btot))

    print("\n== end-to-end: what the card would show ==")
    cur.execute("""SELECT id, coalesce(year,canon_year) y, coalesce(make,canon_make) mk,
                          coalesce(model,canon_model) md, trim, canon_trim
                     FROM bids WHERE coalesce(year,canon_year) IS NOT NULL
                       AND coalesce(make,canon_make) IS NOT NULL
                  ORDER BY id DESC LIMIT 400""")
    bids = cur.fetchall()
    with_comp = 0
    shown = 0
    for bid_id, y, mk, md, tr, ctr in bids:
        cur.execute("""SELECT auction_slug, canon_make, canon_model, model, style
                         FROM auction_comps
                        WHERE canon_make=%s AND year=%s AND outcome='sold'
                          AND price IS NOT NULL LIMIT 500""", (canon_make(mk), y))
        b = {"year": y, "make": mk, "model": md, "trim": tr, "canon_trim": ctr}
        hits = 0
        for slug, cmk, cmd, mdl, st in cur.fetchall():
            if not models_match(canon_model(md or ""), cmd or ""):
                continue
            comp = {"year": y, "canon_make": cmk, "canon_model": cmd, "model": mdl,
                    "style": st, "auction_slug": slug}
            if match_bid_to_comp(v, b, comp)[0]:
                hits += 1
        if hits:
            with_comp += 1
        shown += hits
    print("   of the last %d bids, %d (%.1f%%) get at least one trim-matched comp"
          % (len(bids), with_comp, 100.0 * with_comp / max(1, len(bids))))
    if own:
        c.close()
    return v


def build(conn=None, apply=False):
    """Recompute trim_feed_width + trim_vocab and report NEW surfaces.

    Read-only unless apply=True.  Never touches bids / auction_comps / ymmt_catalog.
    """
    own = conn is None
    c = conn or _connect()
    cur = c.cursor()
    if apply:
        cur.execute(VOCAB_DDL)
        c.commit()
    v = TrimVocab()
    cur.execute("SELECT make, model, trim FROM ymmt_catalog")
    cat = cur.fetchall()
    for mk, _md, tr in cat:
        v.add_catalog_protect(mk, tr)
    for mk, md, tr in cat:
        v.add_catalog(mk, md, tr)
    counts, measured = _derive_observed(cur, v)
    v.finalize()
    print("built: %d entries over %d make|model keys" % (len(v.sources), len(v.entries)))
    if not apply:
        print("(dry run — pass --apply to write trim_vocab / trim_feed_width / aliases)")
        if own:
            c.close()
        return v
    cur.execute("DELETE FROM trim_feed_width")
    for slug, w in v.widths.items():
        n, pct = measured.get(slug, (0, 0.0))
        cur.execute("""INSERT INTO trim_feed_width (auction_slug,width,n_rows,pct_at_width)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (auction_slug) DO UPDATE
                         SET width=EXCLUDED.width, n_rows=EXCLUDED.n_rows,
                             pct_at_width=EXCLUDED.pct_at_width, computed_at=now()""",
                    (slug, w, n, round(pct, 4)))
    cur.execute("DELETE FROM trim_vocab")
    for (mk, md), trims in v.entries.items():
        for t in trims:
            src = v.sources.get((mk, md, t), "catalog")
            cur.execute("""INSERT INTO trim_vocab
                             (make,model,canon_trim,source,support,mixed_seen)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (make,model,canon_trim) DO UPDATE
                             SET source=EXCLUDED.source, support=EXCLUDED.support,
                                 mixed_seen=EXCLUDED.mixed_seen, built_at=now()""",
                        (mk, md, t, src, counts.get((mk, md, t), 0),
                         t in v.complete_attested.get((mk, md), ())))
    for mk, md, sf, ct, note in SEED_ALIASES:
        cur.execute("""INSERT INTO trim_alias (make,model,surface,canon_trim,note,approved_by)
                       VALUES (%s,%s,%s,%s,%s,'trim_schema.py SEED_ALIASES')
                       ON CONFLICT (make,model,surface) DO NOTHING""",
                    (mk, md, sf, ct, note))

    # THE MAINTENANCE STORY.  Re-resolve the whole feed with the vocabulary we
    # just built and file everything that abstained.  A new trim shows up here,
    # abstaining from the card, until a human admits it to trim_vocab or writes
    # a trim_alias.  Growth in this table is the signal to refresh the catalog.
    for mk, md, sf, ct, note in SEED_ALIASES:
        v.add_alias(mk, md, sf, ct)
    v.finalize()
    cur.execute("SELECT auction_slug, canon_make, canon_model, model, style, count(*) "
                "FROM auction_comps WHERE coalesce(style,'')<>'' GROUP BY 1,2,3,4,5")
    queue = Counter()
    for slug, cmk, cmd, mdl, st, n in cur.fetchall():
        r = v.resolve(cmk, cmd, st, auction_slug=slug, feed_model=mdl)
        if r.status in (Resolution.UNKNOWN, Resolution.AMBIGUOUS,
                        Resolution.UNRESOLVABLE, Resolution.NO_VOCAB):
            queue[(cmk or "", cmd or "", r.surface or str(st), r.status)] += n
    cur.execute("TRUNCATE trim_review_queue")
    for (mk2, md2, sf, stt), n in queue.items():
        cur.execute("""INSERT INTO trim_review_queue (make,model,surface,status,n)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (make,model,surface) DO UPDATE
                         SET n=EXCLUDED.n, status=EXCLUDED.status, last_seen=now()""",
                    (mk2, md2, sf, stt, n))
    c.commit()
    print("written. review queue: %d distinct surfaces, %d rows of feed data"
          % (len(queue), sum(queue.values())))
    if own:
        c.close()
    return v


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--ddl" in args:
        print(VOCAB_DDL)
    elif "--build" in args:
        build(apply="--apply" in args)
    elif "--survey" in args:
        v = survey()
        print()
        selftest(v, verbose=False)
    else:
        selftest(load(), verbose=True)
