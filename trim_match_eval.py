#!/usr/bin/env python3
"""trim_match_eval.py -- INDEPENDENT grader for any trims_match(a, b) -> bool.

NOT the matcher. This file never decides how to match; it decides whether a
candidate matcher is safe to put in front of the people setting buy prices.

WHY THE GRADING IS ASYMMETRIC
    OPERATOR DIRECTIVE 2026-09-02: "a big horn is not a sport and a sport is
    not a laramie."
    A FALSE MATCH shows a Sport as a comp for a Laramie -- a wrong number on
    the bid page. A MISSED MATCH shows fewer comps. Those are not equal costs.
    So the headline number is FALSE-MATCH RATE. Recall is reported, and it is
    reported honestly, but it is the secondary number.

WHERE THE PAIRS COME FROM
    Every string in CORPUS below was observed in the EW production database on
    C1 (62.146.226.100) as of 2026-09-02:
      * `b` values are real `auction_comps.style` values -- 27,918 rows from 13
        Florida EDGE Pipeline auctions, 2,508 distinct styles.
      * `a` values are real `bids.trim` / `bids.canon_trim` values (2,572
        distinct make/model/trim rows) OR, where the pair is auction-internal,
        a second real `auction_comps.style` on the same make/model.
    Nothing here is invented, and `--provenance` re-proves that against the live
    DB on demand. `n_a` / `n_b` are the row counts observed on 2026-09-02; they
    are a SNAPSHOT and the table keeps growing, so the row-weighted rates below
    are indicative, not exact.
    SIX pairs are bid x bid -- both strings come from `bids.trim`, so they are
    not the production argument shape (`--provenance` labels them `a=bid b=bid`).
    They are kept because each isolates a mechanism cleanly; every one of them
    has an auction x auction twin in the corpus, and the headline findings should
    be read off the auction-side pairs.
    `python3 trim_match_eval.py --mine` re-derives the CANDIDATE pool from the
    live DB (read-only) so the corpus can be refreshed -- it deliberately does
    NOT assign labels. Labels are frozen here, by hand.

ARGUMENT ORDER MATTERS -- it mirrors the live call site,
comps_lookup.py:172
        wide = [r for r in wide if trims_match(bid_trim, r.get('style') or '')]
    so `a` is ALWAYS the EW/bid side and `b` is ALWAYS the auction style.
    (The call site also short-circuits on a blank bid_trim, so a blank `a`
    never reaches the matcher in production. It is still graded here: a
    matcher must not depend on its caller to stay safe.)

LABELLING BASIS
    Labels come from manufacturer trim ladders (model-year press material and
    published trim listings), plus what the data itself proves.
    `--ambiguity` prints, straight from auction_comps, every style on that
    make/model that a fragment could expand to. Those expansion SETS are
    computed from the data; the CATEGORY is then assigned by hand from the set
    plus the manufacturer ladder, because no rule over strings can tell
    'BIG HORN' -> {BIG HORN LONE S, BIG HORN LONE STAR} (one trim, three
    spellings) from 'UNLIMITED R' -> {UNLIMITED RUBICON, UNLIMITED RUBICON 392}
    (two trims, $30,800 apart). Run it and check the labels against the sets.
    NO LLM WAS USED TO LABEL ANYTHING. The local 9B fabricates confidently
    (935 of 1,950 of its cached VIN decodes contradict the VIN's own WMI at
    0.95 confidence); it is not admissible as ground truth.
    Every row carries a `basis` string saying why it is labelled the way it is.

WHAT A GOOD SCORE HERE DOES NOT PROVE
    Passing this corpus is NECESSARY, NOT SUFFICIENT. 245 labelled pairs against
    852 pairs the current rule matches live (`--mine`). A candidate should be run
    through `--mine` and its top ~50 remaining matches read by eye before anyone
    calls it safe.
    The "without debatable" false-match rate is identical to the headline by
    construction, not by accident: every engine/drivetrain/body-variant row is
    labelled MATCH, so excluding them can only move RECALL. That split exists so
    the operator's unmade price-tier call is visible, not to move the metric.

PAIRS FROM THE BRIEF THAT DO NOT OCCUR IN THIS DATA
    Deliberately omitted, not overlooked -- no make/model in auction_comps
    carries both halves, so a pair would have had to be invented:
      SV/SVT, LS/LSX, SE/SE Premium, Base/Base Preferred, SR/SR Premium, EX/EXL.
    Their mechanism is covered by observed equivalents: XL/XLT (Ford, 165x289),
    LT/LTZ (Chevrolet, 193x49), SE/SEL (Hyundai, 114x80), S/SE (Ford, 27x141),
    LX/LXS (Kia, 47x61), SR/SR5 (Toyota, 26x41), EX/EX-L (Honda, 112x150),
    SEL/SEL Premium (Hyundai, Volkswagen), Limited/Limited Platinum (Chrysler).

WHY ymmt_catalog IS NOT USED
    It was considered as a trim-ladder oracle and rejected. Its build lives in
    scripts/ymmt_catalog/ and includes a `phase3_worker_prompt.txt` -- a
    model-authored phase. A ladder that may itself have been generated cannot
    adjudicate whether Latitude Lux is a tier; manufacturer press material and
    published trim listings were used instead, and each row records which.

READ-ONLY
    This module imports edge_canon for the baseline and never modifies it.
    --mine, --provenance and --ambiguity open read-only DB cursors. Nothing here
    writes to any table, and nothing here restarts any service.
"""

from __future__ import annotations

import argparse
import collections
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
MATCH = "match"            # a candidate SHOULD return True. Returning False = missed match.
NO_MATCH = "no_match"      # a candidate MUST return False. Returning True = FALSE MATCH.
ABSTAIN_OK = "abstain_ok"  # either answer is defensible; graded separately, never in the headline.

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
# truncation_exact        EDGE cut one trim short; the expansion is unambiguous.
# truncation_ambiguous    EDGE cut a trim short and the fragment could name >= 2
#                         DIFFERENT TRIMS on that same make/model (not merely two
#                         spellings of one trim -- see --ambiguity). It cannot be
#                         resolved, so it must match none of them.
# short_code              Bare short trim codes that prefix each other (XL/XLT).
# short_code_body_prefix  The same short codes, but with a body/engine phrase in
#                         front ("2500HD LT" / "2500HD LTZ"). This is the class
#                         that defeats a shared-prefix rule -- the prefix budget
#                         is spent on the body words before the codes diverge.
# hierarchy               Longer name is a HIGHER tier of the shorter one
#                         (Longhorn / Longhorn Limited, Latitude / Latitude Plus).
# package_suffix          A named package/edition that is its own price tier
#                         (Rubicon / Rubicon 392, Scat Pack / Scat Pack Widebody).
# trim_vs_body            A real trim on one side, a body descriptor on the other.
# body_descriptor         BOTH sides are body descriptors, no trim information.
# body_suffix             Same trim; the EW side carries cab/drive/body noise.
# model_prefix            Same trim; the auction side prepends a body/model word
#                         ("LX" vs "SEDAN LX").
# multi_trim_list         The EW side is an UNRESOLVED LIST of several trims.
# blank                   One or both sides empty.
# cross_make              Same token, different makes. A context-free
#                         trims_match(a, b) CANNOT get these right; scored apart.
# engine_variant          Same trim name, different engine (EX-L / EX-L V6).
# drivetrain_variant      Same trim name, different drive (Long Range / Long Range AWD).
# body_variant            Same trim name, different body (GT Premium / GT Premium Convertible).
# naming_era             The SAME slot on the ladder under two model-year badges
#                        (Longhorn / Limited Longhorn, Badlands / Badlands Advanced).
#                        Verified against manufacturer/press sources, not assumed.
# package_option         An option BOX, not a trim row (Stingray / Stingray Z51).
#                        Same trim by the ladder, real money on the sticker --
#                        so it is ABSTAIN-OK, not a label this file will invent.
# identity               Controls: same string modulo case/punctuation/space.

# The last three are PRICE-TIER JUDGEMENTS THE OPERATOR HAS NOT MADE. They are
# labelled the conservative way here (same trim => match) but the headline
# false-match rate is reported both WITH and WITHOUT them so no number quietly
# encodes a decision that is his to make.
DEBATABLE = {"engine_variant", "drivetrain_variant", "body_variant"}

# A context-free string function cannot possibly discriminate these. Excluded
# from the headline unless the candidate accepts make=/model= kwargs.
CONTEXT_ONLY = {"cross_make"}


def P(make, model, a, b, label, category, basis, n_a=1, n_b=1):
    return {"make": make, "model": model, "a": a, "b": b, "label": label,
            "category": category, "basis": basis, "n_a": n_a, "n_b": n_b}


# ===========================================================================
# THE CORPUS
# a = EW / bids side.  b = auction_comps.style side.
# ===========================================================================
CORPUS = [

    # -- truncation_exact -----------------------------------------------------
    # EDGE cuts styles at a source-dependent width (observed max style length
    # per auction_slug: aaayam 6, aaapensacola/daxtampafl2/jacksonvilleaa 13,
    # aaayasa/aaayatb 14, aaayafm 15, vemoaag 16, orlandoaa/speedwayaa 17,
    # orlandolongwoodaafl/southfloridaaa 20, anaaorlando 27). These fragments
    # each expand to exactly ONE trim on their make/model.
    P("RAM", "1500", "Big Horn/Lone Star", "BIG HORN/LONE S", MATCH, "truncation_exact",
      "Same Ram trim, cut at 15 chars. Ram sells it as the single trim 'Big Horn/Lone Star' (Autoblog).", 7, 17),
    P("RAM", "1500", "BIG Horn/ Lone Star", "Big Horn", MATCH, "truncation_exact",
      "Ram sells this as ONE trim, 'Big Horn/Lone Star'; Lone Star is the Texas-market badge with "
      "identical equipment groups and pricing (Autoblog; dealer trim pages, MY2024-26).", 7, 59),
    P("RAM", "1500", "Longhorn Limited", "LONGHORN LIMITE", MATCH, "truncation_exact",
      "15-char cut of Longhorn Limited; no other Ram trim starts 'LONGHORN LIMITE'.", 1, 1),
    P("RAM", "1500", "Laramie Longhorn", "LARAMIE LONGHOR", MATCH, "truncation_exact",
      "15-char cut of Laramie Longhorn; unambiguous on Ram 1500.", 2, 5),
    P("LAND ROVER", "RANGE ROVER", "Autobiography", "AUTOBIOG", MATCH, "truncation_exact",
      "8-char cut of Autobiography. Threshold-edge TRUE match: raising min_prefix past 8 loses it.", 16, 3),
    P("CADILLAC", "SRX", "Luxury Collection", "LUXURY COLLECTIO", MATCH, "truncation_exact",
      "16-char cut. Cadillac SRX ladder: Base / Luxury / Performance / Premium Collection.", 13, 26),
    P("CADILLAC", "SRX", "Performance Collection", "PERFORMANCE COLL", MATCH, "truncation_exact",
      "16-char cut of Performance Collection.", 5, 7),
    P("CADILLAC", "SRX", "Premium Collection", "PREMIUM COLLECTI", MATCH, "truncation_exact",
      "16-char cut of Premium Collection.", 1, 2),
    P("CADILLAC", "XTS", "Luxury Collection", "LUXURY COLLECTIO", MATCH, "truncation_exact",
      "16-char cut of Luxury Collection.", 1, 3),
    P("CADILLAC", "CTS", "Sedan Luxury Collection", "SEDAN LUXURY COL", MATCH, "truncation_exact",
      "16-char cut; body word 'Sedan' plus Luxury Collection.", 1, 1),
    P("CADILLAC", "DTS", "Premium Collection", "PREMIUM COLLECTI", MATCH, "truncation_exact",
      "16-char cut of Premium Collection.", 1, 1),
    P("CADILLAC", "XT6", "Premium Luxury", "PREMIUM LUXU", MATCH, "truncation_exact",
      "12-char cut. XT6 ladder Luxury / Premium Luxury / Sport; only Premium Luxury starts thus.", 3, 11),
    P("CADILLAC", "XT4", "Premium Luxury", "PREMIUM LUXU", MATCH, "truncation_exact",
      "12-char cut of Premium Luxury.", 1, 13),
    P("CADILLAC", "XT5", "Premium Luxury", "PREMIUM LUXU", MATCH, "truncation_exact",
      "12-char cut of Premium Luxury.", 3, 5),
    P("CADILLAC", "XT5", "Premium Luxury", "PREMIUM LUXURY F", MATCH, "truncation_exact",
      "16-char cut of 'Premium Luxury FWD' -- trim plus drivetrain letter.", 3, 3),
    P("GMC", "SIERRA 1500", "Elevation", "ELEVATIO", MATCH, "truncation_exact",
      "8-char cut of Elevation. Threshold-edge TRUE match at exactly min_prefix=8.", 3, 18),
    P("GMC", "TERRAIN", "Elevation", "ELEVATIO", MATCH, "truncation_exact",
      "8-char cut of Elevation.", 4, 3),
    P("TOYOTA", "HIGHLANDER", "Limited V6", "LIMITED V", MATCH, "truncation_exact",
      "9-char cut of Limited V6. Threshold-edge TRUE match.", 3, 26),
    P("TOYOTA", "SIENNA", "XLE 8 Passenger", "XLE 8 PASSENG", MATCH, "truncation_exact",
      "13-char cut of XLE 8 Passenger.", 1, 16),
    P("HONDA", "CR-V", "Hybrid Sport Touring", "HYBRID SPORT TO", MATCH, "truncation_exact",
      "15-char cut of Hybrid Sport Touring, the top CR-V hybrid trim.", 1, 13),
    P("TOYOTA", "RAV4", "Hybrid XLE Premium", "HYBRID XLE PREM", MATCH, "truncation_exact",
      "15-char cut of Hybrid XLE Premium.", 1, 9),
    P("VOLKSWAGEN", "ATLAS", "3.6L V6 SE w/Technology", "3.6L V6 SE W/T", MATCH, "truncation_exact",
      "14-char cut of 3.6L V6 SE w/Technology.", 4, 17),
    P("HONDA", "CIVIC", "Hatchback Sport", "HATCHBACK SPOR", MATCH, "truncation_exact",
      "14-char cut of Hatchback Sport.", 2, 29),
    P("HONDA", "CIVIC", "Si Coupe Manual", "SI COUPE MANUA", MATCH, "truncation_exact",
      "14-char cut of Si Coupe Manual.", 1, 2),
    P("ACURA", "MDX", "Type S w/Advance Package", "TYPE S W/ADVANCE", MATCH, "truncation_exact",
      "16-char cut of Type S w/Advance Package.", 1, 1),
    P("ACURA", "MDX", "Tech/Entertainment Pkg", "TECH/ENTERTAINME", MATCH, "truncation_exact",
      "16-char cut of Tech/Entertainment Pkg.", 1, 2),
    P("ACURA", "ILX", "w/Premium/A-SPEC Pkg", "W/PREMIUM/A-SPEC", MATCH, "truncation_exact",
      "16-char cut of w/Premium/A-SPEC Pkg.", 1, 3),
    P("MAZDA", "CX-5", "2.5 S Premium Package", "2.5 S PREMIUM P", MATCH, "truncation_exact",
      "15-char cut of 2.5 S Premium Package.", 3, 9),
    P("MAZDA", "CX-5", "2.5 S CARBON EDITION", "2.5 S CARBON ED", MATCH, "truncation_exact",
      "15-char cut of 2.5 S Carbon Edition.", 1, 8),
    P("TOYOTA", "VENZA", "NIGHTSHADE EDITION", "NIGHTSHADE EDI", MATCH, "truncation_exact",
      "14-char cut of Nightshade Edition.", 1, 1),
    P("VOLKSWAGEN", "TIGUAN", "SE R-LINE BLACK", "SE R-LINE BLA", MATCH, "truncation_exact",
      "13-char cut of SE R-Line Black.", 3, 15),
    P("BMW", "M4", "COMPETITION XDRIVE", "COMPETITION XDRIV", MATCH, "truncation_exact",
      "17-char cut of Competition xDrive.", 9, 1),
    P("AUDI", "A4", "Sedan Premium Plus", "SEDAN PREMIUM PLU", MATCH, "truncation_exact",
      "17-char cut of Sedan Premium Plus.", 1, 3),
    P("AUDI", "A3", "Sedan Premium Plus", "SEDAN PREMIUM PLU", MATCH, "truncation_exact",
      "17-char cut of Sedan Premium Plus.", 1, 4),
    P("CHEVROLET", "SILVERADO 1500", "Work Truck", "WORK", MATCH, "truncation_exact",
      "4-char cut of Work Truck (WT). Only Silverado trim starting 'WORK'. Current rule cannot reach it.", 9, 34),
    P("FORD", "MUSTANG", "EcoBoost Premium", "ECOBOOST PRE", MATCH, "truncation_exact",
      "12-char cut of EcoBoost Premium.", 1, 22),
    P("HONDA", "ACCORD", "Sedan Sport 1.5T", "SEDAN SPORT 1", MATCH, "truncation_exact",
      "13-char cut of Sedan Sport 1.5T.", 7, 17),
    P("CADILLAC", "ESCALADE", "Premium Luxury", "PREMIUM LUXU", MATCH, "truncation_exact",
      "12-char cut; at 12 chars only Premium Luxury / Premium Luxury Platinum survive and "
      "'PREMIUM LUXU' + nothing is the base of both -- see the ambiguous row for 'PREMIUM LUX'.", 14, 1),

    # -- truncation_ambiguous -------------------------------------------------
    # Computed mechanically: the fragment is a leading substring of >= 2
    # DIFFERENT trims observed on the same make/model in auction_comps. It must
    # therefore match NONE of them -- resolving it to the most common expansion
    # is exactly the "showed a Sahara as a Sport" failure.
    P("JEEP", "WRANGLER", "Unlimited Sport", "UNLIMITED S", NO_MATCH, "truncation_ambiguous",
      "'UNLIMITED S' (11 chars, 95 rows) is a prefix of Unlimited Sport, Unlimited Sport S, "
      "Unlimited Sport Altitude, Unlimited Sahara and Unlimited Sahara Altitude -- all present on "
      "Wrangler in this table. Sold prices in the bucket run $2,800-$26,600 (mean $11,257) against "
      "Unlimited Sport $8,589 and Unlimited Sahara $10,750: a mixed population, proven by price.", 25, 95),
    P("JEEP", "WRANGLER", "Unlimited Sahara", "UNLIMITED S", NO_MATCH, "truncation_ambiguous",
      "Same fragment. Sahara is a distinct, higher Wrangler tier than Sport; the fragment cannot "
      "be resolved to either.", 5, 95),
    P("JEEP", "WRANGLER", "Unlimited Sport S", "UNLIMITED S", NO_MATCH, "truncation_ambiguous",
      "Same fragment; Sport S is its own tier above Sport.", 1, 95),
    P("JEEP", "WRANGLER", "Unlimited Sahara Open Body", "UNLIMITED S", NO_MATCH, "truncation_ambiguous",
      "Same fragment.", 1, 95),
    P("JEEP", "WRANGLER", "Unlimited Rubicon", "UNLIMITED R", NO_MATCH, "truncation_ambiguous",
      "'UNLIMITED R' is a prefix of Unlimited Rubicon, Unlimited Rubicon 392 and Unlimited Rubicon "
      "4xe -- Rubicon 392 is roughly double the money.", 6, 18),
    P("JEEP", "WRANGLER", "Unlimited Rubicon 392", "UNLIMITED R", NO_MATCH, "truncation_ambiguous",
      "Same fragment; 392 is a separate top tier.", 4, 18),
    P("JEEP", "WRANGLER", "Unlimited Rubicon 4xe", "UNLIMITED R", NO_MATCH, "truncation_ambiguous",
      "Same fragment; 4xe is a separate plug-in tier.", 1, 18),
    P("CADILLAC", "ESCALADE", "Premium Luxury", "PREMIUM LUX", MATCH, "truncation_exact",
      "CORRECTED BY --ambiguity: among Escalade STYLES 'PREMIUM LUX' expands only to Premium Luxury "
      "(Premium Luxury Platinum occurs on the bid side only). One expansion, so it resolves.", 14, 5),
    P("CADILLAC", "ESCALADE", "PREMIUM LUXURY PLATINUM", "PREMIUM LUX", NO_MATCH, "hierarchy",
      "'PREMIUM LUX' resolves to Premium Luxury ($83,195, MY2021 Cars.com); the bid car is Premium "
      "Luxury Platinum ($100,595). One expansion, wrong trim.", 3, 5),
    P("DODGE", "CHALLENGER", "R/T Scat Pack Widebody", "R/T SCAT", NO_MATCH, "package_suffix",
      "CORRECTED BY --ambiguity: among Challenger STYLES 'R/T SCAT' expands only to R/T Scat Pack. "
      "The bid car is the Widebody, ~$6,000 above it (KBB, MY2022 Charger figures).", 1, 14),
    P("DODGE", "CHALLENGER", "R/T Scat Pack", "R/T SCAT", MATCH, "truncation_exact",
      "CORRECTED BY --ambiguity: 'R/T SCAT' expands to exactly one Challenger style, R/T Scat Pack.", 7, 14),
    P("ACURA", "MDX", "SH-AWD Technology", "SH-", NO_MATCH, "truncation_ambiguous",
      "'SH-' is a 3-char fragment prefixing SH-AWD, SH-AWD Technology and SH-AWD A-Spec.", 15, 9),
    P("CHEVROLET", "CORVETTE", "Stingray", "STINGRAY Z5", ABSTAIN_OK, "package_option",
      "VERIFIED AS GENUINELY AMBIGUOUS: Z51 is an OPTION BOX on Stingray ($5,995 in 2021, $6,345 in "
      "2022), not a trim row -- the C8's priced ladder is 1LT/2LT/3LT (GM Authority, Jalopnik). Same "
      "trim by the ladder, ~$6k of content by the window sticker. Either answer is defensible; that "
      "is a price-tier call for the operator, not for this file.", 30, 2),
    P("JEEP", "CHEROKEE", "Latitude", "LATITUDE PL", NO_MATCH, "hierarchy",
      "'LATITUDE PL' cuts Latitude Plus, a separate Cherokee tier from Latitude.", 39, 7),
    P("JEEP", "CHEROKEE", "Latitude", "LATITUDE LU", NO_MATCH, "hierarchy",
      "'LATITUDE LU' cuts Latitude Lux, a separate Cherokee tier from Latitude.", 39, 5),
    P("JEEP", "COMPASS", "Latitude", "LATITUDE ALT", NO_MATCH, "hierarchy",
      "'LATITUDE ALT' cuts Latitude Altitude, a separate Compass appearance tier.", 36, 1),
    P("FORD", "ESCAPE", "Titanium", "TITANIUM HYBR", MATCH, "engine_variant",
      "VERIFIED CORRECTION: Titanium is ONE tier with several powertrains. MY2022 Titanium (2.0T AWD) "
      "~$37,145 vs Titanium Hybrid (2.5 FWD) ~$35,095 -- the same band (KBB/Edmunds).", 50, 2),
    P("FORD", "ESCAPE", "Titanium", "TITANIUM PLUG", NO_MATCH, "hierarchy",
      "Titanium Plug-In Hybrid is ~$40,435-$42,195 against a ~$37,145 gas Titanium, and Edmunds "
      "carries the PHEV as a SEPARATE MODEL. Same badge, different money.", 50, 1),
    P("MAZDA", "CX-5", "Grand Touring", "GRAND TOURING R", NO_MATCH, "hierarchy",
      "'GRAND TOURING R' cuts Grand Touring Reserve. MY2021: GT $30,460 vs GT Reserve $35,285 -- "
      "Reserve adds the 2.5T and standard AWD (TrueCar/Autotrader).", 34, 2),
    P("HONDA", "CR-V", "Hybrid Sport", "HYBRID SPORT-L", NO_MATCH, "hierarchy",
      "Sport-L was ADDED for MY2024 at $36,350, between EX-L and the range-topping Sport Touring (Honda "
      "press release, hondanews.com). MY2023 had no Sport-L at all.", 9, 26),
    P("HONDA", "CR-V", "Hybrid Sport", "HYBRID SPORT TO", NO_MATCH, "hierarchy",
      "'HYBRID SPORT TO' cuts Hybrid Sport Touring, the top hybrid tier.", 9, 13),
    P("RAM", "1500", "Longhorn", "LONGHORN LIMITE", MATCH, "naming_era",
      "VERIFIED CORRECTION: for 2019-20 the DT trim was Laramie Longhorn (shortened to 'Longhorn'); "
      "for MY2021 Ram dropped 'Laramie' and renamed the SAME slot Limited Longhorn (Motor1, "
      "MoparInsiders). Same truck, different model-year badge. NOTE plain 'Limited' is a DIFFERENT, "
      "higher trim.", 4, 1),
    P("DODGE", "CHARGER", "Scat Pack", "SCAT PACK WI", NO_MATCH, "package_suffix",
      "'SCAT PACK WI' cuts Scat Pack Widebody, its own configuration and price.", 7, 3),
    P("JEEP", "COMPASS", "Latitude", "LATITUDE W/S", NO_MATCH, "package_suffix",
      "'LATITUDE W/S' cuts a Latitude w/Sun & Sound package car, not plain Latitude.", 36, 7),
    P("VOLKSWAGEN", "JETTA", "SE", "SEDAN SE W/CON", NO_MATCH, "package_suffix",
      "'SEDAN SE W/CON' cuts Sedan SE w/Convenience & Sunroof, a packaged car above plain SE.", 19, 2),

    # -- short_code -----------------------------------------------------------
    # The class the current docstring says it protects: bare codes that prefix
    # each other and are genuinely different cars.
    P("FORD", "F150", "XL", "XLT", NO_MATCH, "short_code",
      "F-150 ladder XL < XLT < Lariat < King Ranch < Platinum < Limited. XL is the fleet trim and the "
      "single highest-volume short-code confusion in this table (165 x 289 rows).", 165, 289),
    P("FORD", "F250SD", "XL", "XLT", NO_MATCH, "short_code",
      "Super Duty ladder: XL < XLT < Lariat < King Ranch < Platinum.", 7, 7),
    P("FORD", "RANGER", "XL", "XLT", NO_MATCH, "short_code", "Ranger ladder XL < XLT < Lariat.", 21, 22),
    P("FORD", "EXPEDITION", "XL", "XLT", NO_MATCH, "short_code", "Expedition XL < XLT.", 18, 39),
    P("CHEVROLET", "SILVERADO 1500", "LT", "LTZ", NO_MATCH, "short_code",
      "Silverado ladder: WT < Custom < LT < RST < LT Trail Boss < LTZ < High Country.", 193, 49),
    P("CHEVROLET", "EQUINOX", "LT", "LTZ", NO_MATCH, "short_code", "Equinox LS < LT < LTZ/Premier.", 161, 11),
    P("CHEVROLET", "TAHOE", "LT", "LTZ", NO_MATCH, "short_code", "Tahoe LS < LT < LTZ/Premier.", 69, 19),
    P("CHEVROLET", "MALIBU", "LT", "LTZ", NO_MATCH, "short_code", "Malibu LS < LT < LTZ/Premier.", 108, 5),
    P("HYUNDAI", "ELANTRA", "SE", "SEL", NO_MATCH, "short_code",
      "Elantra ladder SE < SEL < Limited. SEL is a full tier above SE.", 114, 80),
    P("HYUNDAI", "TUCSON", "SE", "SEL", NO_MATCH, "short_code", "Tucson SE < SEL < Limited.", 78, 66),
    P("HYUNDAI", "SANTA FE", "SE", "SEL", NO_MATCH, "short_code", "Santa Fe SE < SEL < Limited/Calligraphy.", 41, 71),
    P("FORD", "ESCAPE", "SE", "SEL", NO_MATCH, "short_code", "Escape S < SE < SEL < Titanium.", 141, 37),
    P("FORD", "EDGE", "SE", "SEL", NO_MATCH, "short_code", "Edge SE < SEL < ST-Line < Titanium.", 35, 71),
    P("FORD", "ESCAPE", "S", "SE", NO_MATCH, "short_code", "Escape S is the base trim; SE is a step up.", 27, 141),
    P("TOYOTA", "COROLLA", "S", "SE", NO_MATCH, "short_code", "Corolla L/LE/S/SE are distinct tiers.", 48, 91),
    P("VOLKSWAGEN", "TIGUAN", "S", "SE", NO_MATCH, "short_code", "MY2022 FWD: S $25,995 vs SE $29,495 (Cars.com/Edmunds). NOTE for 2022+ there is no plain SEL -- a "
      "bare SEL implies a 2018-21 Tiguan, where SE and SEL were separate trims.", 56, 64),
    P("FORD", "FUSION", "S", "SE", NO_MATCH, "short_code", "Fusion S < SE < SEL/Titanium.", 11, 94),
    P("KIA", "FORTE", "LX", "LXS", NO_MATCH, "short_code", "Forte ladder FE/LX < LXS < GT-Line < GT.", 47, 61),
    P("KIA", "K5", "LX", "LXS", NO_MATCH, "short_code", "K5 LX < LXS < GT-Line < GT.", 2, 11),
    P("KIA", "CARNIVAL", "LX", "LXS", NO_MATCH, "short_code", "Carnival LX < LXS(+) < EX < SX.", 12, 3),
    P("TOYOTA", "TACOMA", "SR", "SR5", NO_MATCH, "short_code", "Tacoma SR is the base; SR5 is the volume trim above it.", 26, 41),
    P("TOYOTA", "TUNDRA", "SR", "SR5", NO_MATCH, "short_code", "Tundra SR < SR5 < Limited < Platinum < 1794.", 6, 36),
    P("HONDA", "CR-V", "EX", "EX-L", NO_MATCH, "short_code", "CR-V EX < EX-L (leather); separate tiers.", 112, 150),
    P("HONDA", "PILOT", "EX", "EX-L", NO_MATCH, "short_code", "Pilot EX < EX-L.", 26, 85),
    P("HONDA", "ODYSSEY", "EX-L", "EX", NO_MATCH, "short_code", "Odyssey EX < EX-L; reversed argument order.", 11, 24),
    P("HONDA", "HR-V", "EX", "EX-L", NO_MATCH, "short_code", "HR-V EX < EX-L.", 17, 20),
    P("CHEVROLET", "SILVERADO 1500 LTD", "L", "LT", NO_MATCH, "short_code",
      "Silverado LTD carry-over: L/LS base vs LT. One-letter prefix, different trims.", 3, 1),
    P("MASERATI", "LEVANTE", "GT", "GTS", NO_MATCH, "short_code", "Levante GT < Modena < GTS < Trofeo.", 1, 1),
    P("MITSUBISHI", "LANCER", "GT", "GTS", NO_MATCH, "short_code", "Lancer ES/SE/GT vs GTS are distinct.", 1, 1),
    P("TOYOTA", "CAMRY", "XLE", "XSE", NO_MATCH, "short_code",
      "Camry XLE (comfort) and XSE (sport) are parallel, differently priced trims -- and share only 'X'.", 29, 37),
    P("FORD", "MAVERICK", "XL", "XLT", NO_MATCH, "short_code",
      "Maverick XL is the base trim, XLT the step up.", 1, 6),
    P("HONDA", "CIVIC", "SDN EX", "SDN EX-L", NO_MATCH, "short_code_body_prefix",
      "6 shared chars behind the 'SDN ' body word; EX vs EX-L are separate Civic trims.", 18, 6),
    P("NISSAN", "ALTIMA", "2.5 S", "2.5 SV", NO_MATCH, "short_code", "Altima S < SV < SR < SL.", 2, 63),
    P("NISSAN", "ALTIMA", "2.5 S", "2.5 SR", NO_MATCH, "short_code", "Altima S < SR (sport).", 2, 59),
    P("NISSAN", "ALTIMA", "2.5 SV", "2.5 SR", NO_MATCH, "short_code", "Altima SV and SR are different trims.", 1, 59),
    P("HYUNDAI", "KONA", "SEL", "SEL Premium", NO_MATCH, "short_code", "Kona SEL < SEL Premium.", 31, 1),
    P("VOLKSWAGEN", "JETTA", "SEL", "SEL Premium", NO_MATCH, "short_code", "Jetta SEL < SEL Premium.", 6, 3),
    P("CHRYSLER", "200", "Limited", "Limited Platinum", NO_MATCH, "hierarchy",
      "UNRESOLVED-SO-FAIL-CLOSED: Cars.com lists both 2016 trims at $24,610, but that same page "
      "equates LX with Touring and C with C Platinum -- a family-base-price artifact, not evidence -- "
      "and CarsDirect omits Limited Platinum entirely. No MSRP delta could be sourced. Labelled "
      "NO-MATCH because under an asymmetric metric an unresolved pair takes the SAFE label. The same "
      "X / X Platinum shape IS verified on Escalade: Premium Luxury $83,195 vs Premium Luxury "
      "Platinum $100,595 (MY2021, Cars.com).", 24, 3),

    # -- short_code_body_prefix ----------------------------------------------
    # *** THE HIGHEST-VALUE CLASS. ***
    # These are the same short-code pairs -- XL/XLT, LT/LTZ, SE/SEL, EX/EX-L,
    # S/SE, S/SV, Premium/Premium Plus -- but with a body, cab or engine phrase
    # in FRONT. The shared-prefix budget is spent before the codes diverge, so
    # the min_prefix guard that stops the bare pairs cannot see these at all.
    P("FORD", "F250SD", "F-250 Super Duty XL", "F-250 Super Duty XLT", NO_MATCH, "short_code_body_prefix",
      "19 SHARED LEADING CHARACTERS and the shorter is a full prefix of the longer -- yet this is "
      "exactly XL vs XLT, the first pair the min_prefix rule claims to reject.", 4, 5),
    P("VOLKSWAGEN", "GOLF", "SportWagen TDI S", "SportWagen TDI SE", NO_MATCH, "short_code_body_prefix",
      "16 shared chars, full prefix. Golf S vs SE.", 1, 1),
    P("NISSAN", "NV200", "Compact Cargo S", "Compact Cargo SV", NO_MATCH, "short_code_body_prefix",
      "15 shared chars, full prefix. NV200 S vs SV.", 1, 1),
    P("AUDI", "A5", "Sportback Premium Plus", "SPORTBACK PREMIUM", NO_MATCH, "short_code_body_prefix",
      "17 shared chars, full prefix. Audi Premium vs Premium Plus are consecutive tiers.", 1, 9),
    P("AUDI", "S5", "Cabriolet Premium Plus", "CABRIOLET PREMIUM", NO_MATCH, "short_code_body_prefix",
      "17 shared chars, full prefix. Premium vs Premium Plus.", 1, 1),
    P("AUDI", "Q3 S LINE", "S line Premium Plus", "S line Premium", NO_MATCH, "short_code_body_prefix",
      "14 shared chars, full prefix. Premium vs Premium Plus.", 3, 1),
    P("CHEVROLET", "SILVERADO 2500", "2500HD LT", "2500HD LTZ", NO_MATCH, "short_code_body_prefix",
      "9 shared chars, full prefix. LT vs LTZ, hidden behind the '2500HD ' body word.", 15, 17),
    P("VOLKSWAGEN", "JETTA", "SEDAN SE", "SEDAN SEL", NO_MATCH, "short_code_body_prefix",
      "8 shared chars, full prefix. SE vs SEL behind 'SEDAN '.", 19, 2),
    P("VOLKSWAGEN", "ATLAS", "3.6L V6 SE", "3.6L V6 SEL", NO_MATCH, "short_code_body_prefix",
      "9 shared chars after normalization, full prefix. SE vs SEL behind the engine phrase.", 9, 15),
    P("HONDA", "ACCORD", "SEDAN EX", "SEDAN EX-L", NO_MATCH, "short_code_body_prefix",
      "8 shared chars, full prefix. EX vs EX-L behind 'SEDAN '.", 31, 22),
    P("HONDA", "CIVIC", "SEDAN EX", "SEDAN EX-L", NO_MATCH, "short_code_body_prefix",
      "8 shared chars, full prefix. EX vs EX-L.", 30, 18),
    P("HONDA", "CIVIC", "SEDAN EX", "SEDAN EX-T", NO_MATCH, "short_code_body_prefix",
      "8 shared chars, full prefix. EX vs EX-T.", 30, 2),
    P("HONDA", "ACCORD", "SEDAN SPORT", "SEDAN SPORT S", NO_MATCH, "short_code_body_prefix",
      "11 shared chars, full prefix. Accord Sport vs Sport SE.", 42, 9),
    P("TOYOTA", "RAV4", "Hybrid XLE", "Hybrid XLE Premium", NO_MATCH, "short_code_body_prefix",
      "10 shared chars, full prefix. XLE vs XLE Premium behind 'Hybrid '.", 17, 1),
    P("MERCEDES", "S CLASS", "S 580 4MATIC", "S 550", NO_MATCH, "short_code_body_prefix",
      "S 550 and S 580 are different engines and generations; correctly separated only because the "
      "third character differs.", 14, 14),

    # -- hierarchy / package_suffix ------------------------------------------
    P("RAM", "1500", "Longhorn", "Longhorn Limited", MATCH, "naming_era",
      "VERIFIED CORRECTION: Longhorn (2019-20 badge) and Limited Longhorn (MY2021+ badge) are the "
      "same slot on the Ram 1500 DT ladder, renamed. Sources: Motor1 'Ram Unceremoniously Ditches "
      "Laramie from Longhorn Name'; MoparInsiders.", 4, 1),
    P("RAM", "1500", "Laramie", "LARAMIE LONGHOR", NO_MATCH, "hierarchy",
      "MY2022: Laramie $47,350 vs Limited Longhorn (ex-Laramie Longhorn) $55,195 -- a ~$8,000 gap "
      "(Cars.com). The operator's own example class.", 50, 5),
    P("RAM", "1500", "Laramie", "LARAMIE LIMITED", NO_MATCH, "hierarchy",
      "Laramie Limited is above Laramie.", 50, 1),
    P("RAM", "1500", "Big Horn", "Laramie", NO_MATCH, "hierarchy",
      "The operator's literal directive: a Big Horn is not a Laramie.", 59, 50),
    P("RAM", "1500", "Sport", "Big Horn", NO_MATCH, "hierarchy",
      "The operator's literal directive: a Big Horn is not a Sport.", 14, 59),
    P("CADILLAC", "ESCALADE", "PREMIUM LUXURY PLATINUM", "Premium Luxury", NO_MATCH, "hierarchy",
      "14 SHARED LEADING CHARACTERS, full prefix. MY2021 Cars.com: Premium Luxury 2WD $83,195 vs "
      "Premium Luxury Platinum 2WD $100,595 -- a $17,400 gap.", 14, 3),
    P("CADILLAC", "ESCALADE", "Platinum Sport Cab/Utility Body Style", "Platinum", NO_MATCH, "hierarchy",
      "8 shared chars, full prefix. Sport Platinum and Platinum are separate Escalade configurations.", 4, 4),
    P("CADILLAC", "ESCALADE", "Premium Luxury", "Luxury", NO_MATCH, "hierarchy",
      "Escalade Luxury is the base tier; Premium Luxury 2WD is $83,195 (MY2021, Cars.com). Correctly "
      "rejected today.", 14, 18),
    P("CADILLAC", "ESCALADE", "Premium Luxury", "Sport", NO_MATCH, "hierarchy",
      "MY2021 Cars.com: Premium Luxury 2WD $83,195 vs Sport 2WD $85,895 -- separate priced rows and "
      "different equipment. No shared prefix; a control.", 14, 3),
    P("JEEP", "WRANGLER", "Unlimited Rubicon 392", "Unlimited Rubicon", NO_MATCH, "package_suffix",
      "17 SHARED LEADING CHARACTERS, full prefix. MY2022: Rubicon $45,595 vs Rubicon 392 $76,395 -- a "
      "$30,800 gap (KBB/Cars.com). The single worst false match a prefix rule can make on this model.", 4, 6),
    P("JEEP", "WRANGLER", "Unlimited Rubicon 4xe", "Unlimited Rubicon", NO_MATCH, "package_suffix",
      "17 shared chars, full prefix. 4xe is the plug-in hybrid, separately priced.", 1, 6),
    P("JEEP", "WRANGLER", "Unlimited Sport S", "Unlimited Sport", NO_MATCH, "package_suffix",
      "15 shared chars, full prefix. MY2022 Unlimited: Sport $34,820 vs Sport S $38,020 (KBB/Cars.com).", 1, 25),
    P("JEEP", "WRANGLER", "Unlimited Sport Altitude", "Unlimited Sport", NO_MATCH, "package_suffix",
      "15 shared chars, full prefix. Altitude is a priced appearance edition.", 1, 25),
    P("JEEP", "WRANGLER", "Unlimited Sahara", "Unlimited Sport", NO_MATCH, "hierarchy",
      "MY2022 Unlimited: Sport $34,820 vs Sahara $42,045 (KBB/Cars.com). Correctly rejected today only "
      "because the shared run 'UNLIMITED S' is not a FULL prefix of either string.", 5, 25),
    P("JEEP", "WRANGLER", "Sport", "Unlimited Sport", NO_MATCH, "hierarchy",
      "2-door Sport vs 4-door Unlimited Sport are different vehicles at different money.", 6, 25),
    P("DODGE", "DURANGO", "SRT Hellcat Redeye", "SRT Hellcat Redeye Jailbreak", NO_MATCH, "package_suffix",
      "18 SHARED LEADING CHARACTERS, full prefix. Jailbreak is a separately priced package.", 2, 3),
    P("HONDA", "PASSPORT", "TRAILSPORT ELITE BLACKOUT", "TrailSport Elite", NO_MATCH, "package_suffix",
      "16 shared chars, full prefix. Blackout is a priced edition on top of TrailSport Elite.", 1, 1),
    P("RAM", "PROMASTER CITY", "Cargo Van Tradesman", "Cargo Van Tradesman SLT", NO_MATCH, "package_suffix",
      "19 shared chars, full prefix. Tradesman SLT is the upgraded ProMaster City.", 1, 9),
    P("JEEP", "CHEROKEE", "Latitude Plus", "Latitude", NO_MATCH, "hierarchy",
      "8 shared chars, full prefix. MY2021 FWD: Latitude $27,890 / Latitude Plus $30,200 / Latitude Lux "
      "$32,110 (TrueCar/KBB).", 1, 39),
    P("JEEP", "COMPASS", "Latitude Altitude", "Latitude", NO_MATCH, "hierarchy",
      "8 shared chars, full prefix. Altitude is a priced appearance package.", 2, 36),
    P("JEEP", "COMPASS", "Latitude LUX", "Latitude", NO_MATCH, "hierarchy",
      "8 shared chars, full prefix. Latitude Lux is above Latitude.", 1, 36),
    P("BUICK", "ENCORE", "Preferred", "Preferred II", NO_MATCH, "hierarchy",
      "9 shared chars, full prefix. FIRST-GEN Encore MY2018: FWD Preferred $24,400 vs Preferred II "
      "$26,900 (JD Power/Cars.com). Encore GX has no Preferred II; the names coexist only on the old car.", 30, 4),
    P("CHRYSLER", "PACIFICA", "Touring L", "Touring L Plus", NO_MATCH, "hierarchy",
      "9 shared chars, full prefix. MY2018: Touring L $35,945 vs Touring L Plus $39,045 (Cars.com/Edmunds).", 20, 3),
    P("CHRYSLER", "PACIFICA", "Touring L", "TOURING-L P", NO_MATCH, "hierarchy",
      "9 shared chars after normalization. 'TOURING-L P' cuts Touring L Plus.", 20, 7),
    P("FORD", "MUSTANG", "EcoBoost", "EcoBoost Premium", NO_MATCH, "hierarchy",
      "8 shared chars, full prefix. Mustang EcoBoost vs EcoBoost Premium are separate trims.", 7, 9),
    P("DODGE", "CHARGER", "Scat Pack", "Scat Pack Widebody", NO_MATCH, "package_suffix",
      "9 shared chars, full prefix. MY2022 Charger: Scat Pack $47,385 vs Scat Pack Widebody $53,380; KBB "
      "and JD Power both carry Widebody as its own priced configuration.", 7, 3),
    P("GMC", "YUKON", "Denali Ultimate", "Denali", NO_MATCH, "hierarchy",
      "Denali Ultimate was new for MY2023: KBB Denali $76,200 vs Denali Ultimate $96,450; MotorAuthority "
      "puts the like-for-like gap at ~$9,000. Correctly rejected today (shared prefix 6).", 7, 36),
    P("HYUNDAI", "PALISADE", "Calligraphy Night", "Calligraphy", NO_MATCH, "package_suffix",
      "11 shared chars, full prefix. New for MY2024: Night Edition $54,935 (AWD standard) vs Calligraphy "
      "FWD $51,435 / AWD $53,435 (KBB, MotorWeek).", 1, 9),
    P("RAM", "2500", "Tradesman Power Wagon", "Tradesman", NO_MATCH, "package_suffix",
      "9 shared chars, full prefix. Power Wagon is a distinct, far more expensive 2500.", 1, 21),
    P("TOYOTA", "RAV4", "XLE Premium", "XLE", NO_MATCH, "hierarchy",
      "Separate rows on Toyota's gas ladder LE / XLE / XLE Premium / Adventure / TRD Off-Road / Limited; "
      "MY2022 XLE Premium $31,335 against LE $26,975 (KBB/Edmunds). Correctly rejected today (prefix 3).", 14, 164),
    P("CHEVROLET", "SILVERADO 1500", "LT TRAIL BOSS", "LT", NO_MATCH, "package_suffix",
      "MY2022: LT $44,295 vs LT Trail Boss $53,695, both with destination -- a $9,400 gap; Trail Boss is "
      "4WD-only with the factory 2in lift (autoevolution, quoting GM pricing). Correctly rejected today.", 7, 193),
    P("FORD", "BRONCO", "Badlands Advanced", "Badlands", MATCH, "naming_era",
      "VERIFIED CORRECTION: Advanced 4x4 is STANDARD on Badlands -- Cars.com's 2023 Bronco trim list "
      "carries Badlands only as 'Badlands 2/4 Door Advanced 4x4' ($48,145/$49,435), with no plain-4x4 "
      "Badlands row, while Base and Big Bend list both forms. Same vehicle.", 6, 2),
    P("PORSCHE", "911", "Carrera 4S", "CARRERA S", NO_MATCH, "hierarchy",
      "Carrera 4S is the all-wheel-drive S; a different car and price from Carrera S. Correctly "
      "rejected today, and the biggest single bid-side population in this bucket.", 69, 3),
    P("HONDA", "ODYSSEY", "EX-L", "Touring", NO_MATCH, "hierarchy",
      "Odyssey EX-L < Touring < Elite. No shared prefix; a control that any matcher must get right.", 11, 8),
    P("VOLKSWAGEN", "TIGUAN", "SE R-LINE BLACK", "SE", NO_MATCH, "hierarchy",
      "MY2022 FWD: SE $29,495 vs SE R-Line Black $32,295 (Cars.com/Edmunds). Correctly rejected today.", 3, 64),
    P("HYUNDAI", "ELANTRA", "SEL Convenience", "SEL", NO_MATCH, "hierarchy",
      "SEL with the Convenience package is priced above plain SEL. Correctly rejected today.", 2, 80),
    P("MERCEDES", "GLE", "350", "450 4MATIC", NO_MATCH, "hierarchy",
      "GLE 350 and GLE 450 are different engines and price tiers.", 7, 20),
    P("CADILLAC", "ATS", "Sedan Standard", "SEDAN STANDARD R", MATCH, "truncation_exact",
      "16-char cut of 'Sedan Standard RWD' -- trim plus drivetrain letter, same Standard tier.", 1, 6),

    # -- trim_vs_body ---------------------------------------------------------
    # A real trim on one side, a pure body/drivetrain descriptor on the other.
    # There is no trim information in a body descriptor, so it can never be
    # asserted equal to a named trim.
    P("TOYOTA", "COROLLA", "LE", "4DR SDN AUTO", NO_MATCH, "trim_vs_body",
      "'4DR SDN AUTO' carries no trim information; asserting it equals LE is the failure that matters.", 91, 3),
    P("ACURA", "MDX", "W/Tech", "4WD 4DR", NO_MATCH, "trim_vs_body",
      "Body/drive descriptor vs a named package tier.", 8, 2),
    P("ACURA", "MDX", "Advance", "4DR SUV", NO_MATCH, "trim_vs_body",
      "Body descriptor vs the Advance package tier.", 2, 1),
    P("ACURA", "ILX", "Tech Pkg", "4DR SDN", NO_MATCH, "trim_vs_body",
      "Body descriptor vs the Tech package tier.", 6, 1),
    P("LEXUS", "IS 250", "F Sport", "4DR SPORT SDN", NO_MATCH, "trim_vs_body",
      "'4dr Sport Sdn' is Lexus body nomenclature, not the F Sport package.", 1, 13),
    P("TOYOTA", "CAMRY", "SE", "4DR SDN I4 AUT", NO_MATCH, "trim_vs_body",
      "Body/engine/transmission descriptor vs the SE trim.", 267, 6),
    P("TOYOTA", "RAV4", "XLE", "4DR 4-CYL 4", NO_MATCH, "trim_vs_body",
      "Body/engine descriptor vs the XLE trim.", 164, 13),
    P("HONDA", "CIVIC", "Sport", "HYBRID 4DR SDN", NO_MATCH, "trim_vs_body",
      "Powertrain plus body descriptor vs the Sport trim.", 6, 4),
    P("PORSCHE", "PANAMERA", "GTS", "4DR HB", NO_MATCH, "trim_vs_body",
      "'4DR HB' is a hatchback body descriptor, not the GTS trim.", 2, 7),
    P("ACURA", "MDX", "Type S Advance", "7-PASSENGER", NO_MATCH, "trim_vs_body",
      "Seating count is not a trim.", 1, 5),

    # -- body_descriptor (ABSTAIN-OK) -----------------------------------------
    # Both sides are body/engine descriptors. Neither carries a price tier, so
    # neither a match nor a non-match is wrong; a matcher is free to abstain.
    P("ACURA", "ILX", "Sedan", "4DR SDN", ABSTAIN_OK, "body_descriptor",
      "Both sides are the same body with no trim content.", 3, 1),
    P("LEXUS", "IS 250", "4dr Sport Sdn Auto", "4DR SPORT SDN", ABSTAIN_OK, "body_descriptor",
      "Both sides body nomenclature; the truncation is real but there is no trim to get wrong.", 6, 13),
    P("TOYOTA", "RAV4", "4dr 4-cyl 4-Spd AT (Natl)", "4DR 4-CYL 4", ABSTAIN_OK, "body_descriptor",
      "Both sides are body/engine/transmission descriptors.", 4, 13),
    P("SCION", "XB", "5dr Wgn Auto (Natl)", "5DR WGN AUTO (NAT", ABSTAIN_OK, "body_descriptor",
      "Both sides body descriptors; truncation of the same string.", 3, 4),
    P("ACURA", "MDX", "4WD 4dr AWD", "4WD 4DR", ABSTAIN_OK, "body_descriptor",
      "Both sides drive/body descriptors.", 1, 2),
    P("CADILLAC", "CTS", "Sedan 4dr Sdn 3.0L", "SEDAN 4DR SDN 3.", ABSTAIN_OK, "body_descriptor",
      "Both sides body/engine descriptors; truncation of the same string.", 2, 2),

    # -- multi_trim_list ------------------------------------------------------
    # bids.trim sometimes holds an UNRESOLVED LIST of every trim the model
    # offers. It names no single price tier, so it must not resolve to one.
    P("PORSCHE", "911", "Carrera S / 4S / GTS / 4 GTS", "CARRERA S", NO_MATCH, "multi_trim_list",
      "The bid trim is a list spanning Carrera S through GTS -- a >$60k spread. Currently MATCHED.", 3, 3),
    P("PORSCHE", "911", "Carrera S / Carrera 4S", "CARRERA S", NO_MATCH, "multi_trim_list",
      "Unresolved list; 4S is a different car from S.", 3, 3),
    P("TOYOTA", "COROLLA", "L, LE, LE w/LE Conveneince Tech pkg, LE - US Source", "LE", NO_MATCH, "multi_trim_list",
      "Unresolved list spanning L and LE and packaged LE.", 5, 424),
    P("VOLKSWAGEN", "TIGUAN", "SE, SEL, SEL R-Line, SEL R-Line Jet-Black", "SE", NO_MATCH, "multi_trim_list",
      "Unresolved list spanning SE to SEL R-Line Jet-Black.", 13, 64),
    P("FORD", "BRONCO", "Base, Big Bend, Black Diamond, Outer Banks", "Badlands", NO_MATCH, "multi_trim_list",
      "Unresolved list that does not even contain the auction trim.", 2, 3),
    P("PORSCHE", "PANAMERA",
      "Panamera / Panamera 4 / Panamera Platinum Edition / Panamera 4 Platinum Edition",
      "4 Platinum Edition", NO_MATCH, "multi_trim_list",
      "Unresolved list spanning base Panamera to Platinum Edition against a specific Platinum "
      "Edition car. This is also the literal 'Platinum Edition' string the brief asked after -- it "
      "is real, on a Porsche, and it shares NO prefix with plain 'Panamera'.", 1, 1),

    # -- blank ----------------------------------------------------------------
    # Fail closed. A blank side is an absence of evidence, never evidence of
    # sameness. (auction_comps holds 1,386 one-character and many empty styles.)
    P("RAM", "1500", "Laramie", "", NO_MATCH, "blank", "Empty auction style proves nothing about trim.", 50, 6),
    P("RAM", "1500", "", "Laramie", NO_MATCH, "blank", "Empty bid trim proves nothing about trim.", 1, 50),
    P("RAM", "1500", "", "", NO_MATCH, "blank", "Two blanks are not a match.", 1, 6),
    P("RAM", "1500", "Big Horn", "   ", NO_MATCH, "blank", "Whitespace-only style.", 59, 1),
    P("FORD", "F150", "XLT", None, NO_MATCH, "blank", "NULL style must not raise and must not match.", 289, 1),
    P("FORD", "F150", None, "XLT", NO_MATCH, "blank", "NULL bid trim must not raise and must not match.", 1, 289),
    P("JEEP", "WRANGLER", "Sport", "-", NO_MATCH, "blank",
      "Punctuation-only style normalizes to empty; must not match.", 6, 1),

    # -- model_prefix (MATCH) -------------------------------------------------
    # Same trim; the auction side prepends the body word. These are the biggest
    # recall holes in the current implementation.
    P("HONDA", "ACCORD", "LX", "SEDAN LX", MATCH, "model_prefix",
      "Accord LX in a sedan body. Same trim; the auction prepends the body word. 86 comp rows unreachable.", 4, 86),
    P("HONDA", "CIVIC", "LX", "SEDAN LX", MATCH, "model_prefix", "Civic LX, sedan body. 76 comp rows unreachable.", 6, 76),
    P("HONDA", "CIVIC", "LX", "SDN LX", MATCH, "model_prefix", "Civic LX; 'SDN' is the same body word abbreviated.", 6, 44),
    P("HONDA", "CIVIC", "Sport", "SEDAN SPORT", MATCH, "model_prefix", "Civic Sport, sedan body. 74 comp rows unreachable.", 6, 74),
    P("HONDA", "CIVIC", "EX", "SEDAN EX", MATCH, "model_prefix", "Civic EX, sedan body.", 5, 30),
    P("HONDA", "ACCORD", "EX-L", "SDN EX-L", MATCH, "model_prefix", "Accord EX-L, sedan body.", 2, 36),
    P("MERCEDES", "GLE", "350", "GLE 350", MATCH, "model_prefix", "The auction repeats the model name before the engine trim.", 7, 27),
    P("MERCEDES", "GLC", "300", "GLC 300", MATCH, "model_prefix", "Model name repeated before the trim.", 2, 25),
    P("HONDA", "CIVIC", "EX", "SDN EX", MATCH, "model_prefix",
      "Civic EX with the abbreviated body word in front; 18 comp rows unreachable today.", 5, 18),

    # -- body_suffix (MATCH) --------------------------------------------------
    # Same trim; the EW side carries cab / drive / body noise from the OCR or
    # the book. Dropping these only costs comps, but there are a lot of them.
    P("FORD", "F150", "KING RANCH 4WD", "KING RANCH", MATCH, "body_suffix",
      "Same King Ranch trim; 4WD is drivetrain, not a tier.", 1, 11),
    P("FORD", "EXPEDITION", "Platinum 4WD", "Platinum", MATCH, "body_suffix", "Same Platinum trim.", 2, 5),
    P("JEEP", "COMPASS", "Latitude 4WD", "Latitude", MATCH, "body_suffix", "Same Latitude trim.", 1, 36),
    P("JEEP", "COMPASS", "Trailhawk 4WD", "Trailhawk", MATCH, "body_suffix", "Same Trailhawk trim.", 1, 13),
    P("FORD", "EXPLORER", "XLT 4WD", "XLT", MATCH, "body_suffix", "Same XLT trim; 117 comp rows unreachable today.", 1, 117),
    P("FORD", "ESCAPE", "SE 4WD", "SE", MATCH, "body_suffix", "Same SE trim; 141 comp rows unreachable today.", 1, 141),
    P("NISSAN", "ROGUE", "SV FWD", "SV", MATCH, "body_suffix", "Same SV trim; 171 comp rows unreachable today.", 2, 171),
    P("NISSAN", "ROGUE", "SV Wagon Body Style", "SV", MATCH, "body_suffix", "'Wagon Body Style' is book boilerplate.", 2, 171),
    P("KIA", "SORENTO", "S Wagon Body Style", "S", MATCH, "body_suffix", "Same S trim plus boilerplate.", 15, 24),
    P("CHEVROLET", "MALIBU", "LT (1LT) Notchback Body Style", "LT", MATCH, "body_suffix",
      "Same LT trim; 1LT is the order code for LT.", 2, 108),
    P("NISSAN", "ARMADA", "PLATINUM-CP Wagon Body Style", "PLATINUM", MATCH, "body_suffix",
      "Same Platinum trim plus boilerplate.", 1, 15),
    P("CADILLAC", "XT6", "Premium Luxury Cab/Utility Body Style", "Premium Luxury", MATCH, "body_suffix",
      "Same Premium Luxury trim plus boilerplate.", 1, 6),
    P("RAM", "1500", "Tradesman Regular Cab 2WD", "Tradesman", MATCH, "body_suffix",
      "Same Tradesman trim; cab and drive are not tiers.", 1, 24),
    P("RAM", "2500", "Big Horn Crew Cab 4x4", "Big Horn", MATCH, "body_suffix", "Same Big Horn trim.", 1, 20),
    P("RAM", "1500", "Big Horn/Lonestar Crew Cab", "Big Horn", MATCH, "body_suffix", "Same Big Horn/Lone Star trim.", 2, 59),
    P("FORD", "MUSTANG", "GT Coupe", "GT", MATCH, "body_suffix", "Same GT trim; Coupe is the body.", 9, 17),
    P("JEEP", "WRANGLER", "Sport Open Body", "Sport", MATCH, "body_suffix", "Same Sport trim; 'Open Body' is book boilerplate.", 3, 38),
    P("GMC", "SIERRA HD", "Denali Ultimate Crew Cab", "Denali Ultimate", MATCH, "body_suffix",
      "Same Denali Ultimate trim; Crew Cab is the body.", 1, 3),
    P("FORD", "F150", "Lariat SuperCrew 4WD", "Lariat SuperCrew", MATCH, "body_suffix",
      "Same Lariat trim; 4WD is drivetrain.", 1, 3),
    P("RAM", "1500", "Limited Crew Cab 4x4", "Limited Crew Cab", MATCH, "body_suffix", "Same Limited trim.", 1, 1),

    # -- engine_variant (DEBATABLE) -------------------------------------------
    # Same trim NAME, different engine. Labelled MATCH because the trim ladder
    # is identical, but reported separately -- whether a V6 Accord EX-L is a
    # comp for a 4-cyl Accord EX-L is the operator's call, not this file's.
    P("HONDA", "ACCORD", "Sedan EX-L", "SEDAN EX-L V6", MATCH, "engine_variant",
      "Same EX-L trim; V6 vs I4 engine. Price differs but the tier does not.", 1, 2),
    P("TOYOTA", "TACOMA", "PreRunner", "PRERUNNER V6", MATCH, "engine_variant",
      "On 1998-2015 Tacomas PreRunner is a 2WD drivetrain designation, not a grade (a truck could be a "
      "PreRunner SR5), so PreRunner and PreRunner V6 are one designation plus an engine call-out. "
      "Threshold-edge (prefix 9). NOTE on the 2024+ truck TRD PreRunner IS a real trim row.", 4, 26),
    P("AUDI", "Q7", "Premium Plus 55", "Premium Plus", MATCH, "engine_variant",
      "Premium Plus is the trim; 45 (2.0T, 248hp) and 55 (3.0T V6, 329hp) are engine designations that "
      "KBB, US News and JD Power all carry as separate configurations of the SAME trim.", 1, 18),
    P("BMW", "X3", "xDrive30", "XDrive30i", MATCH, "engine_variant",
      "Same xDrive30i car; the trailing 'i' is dropped on the bid side.", 2, 26),
    P("LAND ROVER", "DEFENDER", "130 X-Dynamic SE", "130 X-Dynamic SE P400", MATCH, "engine_variant",
      "Same X-Dynamic SE trim; P400 is the engine.", 2, 1),
    P("HONDA", "ACCORD", "Sedan LX", "SEDAN LX 1.5T", MATCH, "engine_variant",
      "Same LX trim; 1.5T is the engine call-out.", 14, 13),

    # -- drivetrain_variant (DEBATABLE) ---------------------------------------
    P("TESLA", "MODEL Y", "Long Range AWD", "Long Range", MATCH, "drivetrain_variant",
      "Same Long Range trim; AWD vs RWD. Note the SAME auction bucket also holds the RWD car.", 1, 15),
    P("TESLA", "MODEL Y", "Long Range RWD", "Long Range", MATCH, "drivetrain_variant",
      "Same Long Range trim, other drivetrain -- which is why this category is flagged debatable: "
      "the auction bucket cannot be both.", 1, 15),
    P("BMW", "4 SERIES", "430I XDRIVE", "430i", MATCH, "drivetrain_variant", "Same 430i; xDrive is AWD.", 5, 23),
    P("MERCEDES", "GLC", "300 4MATIC", "GLC 300", MATCH, "drivetrain_variant", "Same GLC 300; 4MATIC is AWD.", 5, 25),
    P("MERCEDES", "GLC", "300 4MATIC", "300 4MATIC SUV", MATCH, "drivetrain_variant",
      "Same car; 'SUV' is the body word.", 5, 5),

    # -- body_variant (DEBATABLE) ---------------------------------------------
    P("FORD", "MUSTANG", "GT Premium", "GT Premium Convertible", MATCH, "body_variant",
      "Same GT Premium trim, convertible body. Convertibles carry a premium -- flagged.", 12, 2),
    P("FORD", "MUSTANG", "GT Premium", "GT PREMIUM F", MATCH, "body_variant",
      "'GT PREMIUM F' cuts GT Premium Fastback; same trim, coupe body.", 12, 5),
    P("LAND ROVER", "RANGE ROVER", "Autobiography LWB", "Autobiography", MATCH, "body_variant",
      "MY2021: Autobiography SWB $144,500 vs Autobiography LWB $151,000 -- same trim, ~$6,500 for the "
      "wheelbase (KBB/Cars.com). NOT to be confused with SVAutobiography LWB ($212,350), a real tier above.", 2, 7),
    P("LAND ROVER", "RANGE ROVER", "P530 Autobiography LWB", "P530 Autobiography", MATCH, "body_variant",
      "18 shared chars; same trim and engine, longer wheelbase.", 2, 3),
    P("JEEP", "WRANGLER", "Unlimited Sport Open Body", "Unlimited Sport", MATCH, "body_variant",
      "'Open Body' is book boilerplate on an Unlimited Sport.", 3, 25),

    # -- cross_make (CONTEXT-ONLY) --------------------------------------------
    # Identical token, different make, different car. A trims_match(a, b) with
    # no make/model argument CANNOT get these right -- it is the model gate's
    # job. Scored in its own bucket and never folded into the headline for a
    # context-free candidate.
    P("CROSS", "SPORT", "Sport", "Sport", NO_MATCH, "cross_make",
      "Jeep Wrangler Sport (132 rows) vs Mazda CX-5 Sport (51) vs Honda Sport (91): same token, "
      "three unrelated ladders.", 132, 51),
    P("CROSS", "LX", "LX", "LX", NO_MATCH, "cross_make",
      "Kia LX (413 rows) is a base trim; Honda LX (173) is a base trim; Chrysler LX (15) is a "
      "different ladder again.", 413, 173),
    P("CROSS", "EX", "EX", "EX", NO_MATCH, "cross_make", "Kia EX (212) vs Honda EX (205): unrelated ladders.", 212, 205),
    P("CROSS", "LIMITED", "Limited", "Limited", NO_MATCH, "cross_make",
      "Ford (191) / Hyundai (179) / Toyota (119) / Subaru (116) / Jeep (103) all use 'Limited' at "
      "different heights on their ladders.", 191, 179),
    P("CROSS", "GT", "GT", "GT", NO_MATCH, "cross_make", "Dodge GT (55) vs Kia GT (22) vs Ford GT (17).", 55, 22),
    P("CROSS", "PREMIUM", "Premium", "Premium", NO_MATCH, "cross_make",
      "Subaru Premium (121) is near-base; Audi Premium (76) is the entry of three; Lexus Premium (44) differs again.", 121, 76),
    P("CROSS", "SLT", "SLT", "SLT", NO_MATCH, "cross_make", "GMC SLT (191) vs Dodge/Ram SLT (36/23).", 191, 36),
    P("CROSS", "LS", "LS", "LS", NO_MATCH, "cross_make", "Chevrolet LS (444) vs Mitsubishi LS (12) vs Mercury LS (6).", 444, 12),
    P("CROSS", "PLATINUM", "Platinum", "Platinum", NO_MATCH, "cross_make",
      "Nissan Platinum (85) / Ford Platinum (51) / Toyota Platinum (23) / Cadillac Platinum (10).", 85, 51),
    P("CROSS", "SELECT", "Select", "Select", NO_MATCH, "cross_make", "Lincoln Select (26) vs Buick/Mazda Select.", 26, 10),

    # -- identity controls ----------------------------------------------------
    # A matcher that fails these is broken outright.
    P("RAM", "1500", "Laramie", "Laramie", MATCH, "identity", "Identical strings.", 50, 50),
    P("RAM", "1500", "Big Horn", "BIG HORN", MATCH, "identity", "Case-only difference.", 59, 27),
    P("RAM", "1500", "Big Horn/Lone Star", "BIG HORN/LONE STAR", MATCH, "identity", "Case-only difference.", 4, 4),
    P("HONDA", "CR-V", "EX-L", "EX L", MATCH, "identity", "Punctuation-only difference.", 150, 150),
    P("JEEP", "WRANGLER", "Rubicon", "RUBICON", MATCH, "identity", "Case-only difference.", 1, 1),
    P("TOYOTA", "TACOMA", "SR5", "SR5", MATCH, "identity", "Identical strings.", 41, 41),
    P("FORD", "F150", "XLT", "XLT ", MATCH, "identity", "Trailing whitespace only.", 289, 289),
]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Result(dict):
    pass


def _call(fn, row, pass_context):
    kw = {}
    if pass_context:
        kw = {"make": row["make"], "model": row["model"]}
    try:
        return fn(row["a"], row["b"], **kw), None
    except Exception as exc:                                   # noqa: BLE001
        return "ERROR", exc


def _accepts_context(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "make" in params and "model" in params


def score(fn, corpus=None, include_debatable=True, include_context_only=None,
          weighted=True):
    """Grade a candidate trims_match(a, b) -> bool | None.

    True  -> asserts the pair is the same trim
    False -> asserts it is not
    None  -> abstains (safe on a NO_MATCH pair, counts as missed on a MATCH pair)

    Returns a dict of counts and rates. FALSE MATCHES ARE THE METRIC.
    """
    corpus = CORPUS if corpus is None else corpus
    pass_context = _accepts_context(fn)
    if include_context_only is None:
        include_context_only = pass_context

    rows = []
    for r in corpus:
        if r["category"] in CONTEXT_ONLY and not include_context_only:
            continue
        if r["category"] in DEBATABLE and not include_debatable:
            continue
        rows.append(r)

    out = {
        "n": len(rows), "pass_context": pass_context,
        "false_match": 0, "missed_match": 0, "correct_reject": 0,
        "correct_match": 0, "abstain_on_no_match": 0, "abstain_on_match": 0,
        "abstain_ok_true": 0, "abstain_ok_false": 0, "abstain_ok_none": 0,
        "errors": 0,
        "w_false_match": 0, "w_no_match_total": 0,
        "w_missed_match": 0, "w_match_total": 0,
        "false_match_rows": [], "missed_match_rows": [], "error_rows": [],
        "by_make": collections.defaultdict(lambda: {"fm": 0, "no": 0, "mm": 0, "ma": 0}),
        "by_category": collections.defaultdict(lambda: {"fm": 0, "no": 0, "mm": 0, "ma": 0}),
    }

    for r in rows:
        w = max(1, r["n_a"]) * max(1, r["n_b"])
        got, exc = _call(fn, r, pass_context)
        if got == "ERROR":
            out["errors"] += 1
            out["error_rows"].append((r, repr(exc)))
            # An exception on a live bid page is a hard failure. On a NO_MATCH
            # pair count it as a FALSE MATCH so it can never be scored "safe";
            # on a MATCH pair count it as a missed match. Either way it is
            # never silently dropped from the denominator.
            if r["label"] == NO_MATCH:
                out["false_match"] += 1
                out["w_false_match"] += w
                out["w_no_match_total"] += w
                out["by_make"][r["make"]]["fm"] += 1
                out["by_make"][r["make"]]["no"] += 1
                out["by_category"][r["category"]]["fm"] += 1
                out["by_category"][r["category"]]["no"] += 1
                out["false_match_rows"].append(r)
            elif r["label"] == MATCH:
                out["missed_match"] += 1
                out["w_missed_match"] += w
                out["w_match_total"] += w
                out["by_make"][r["make"]]["mm"] += 1
                out["by_make"][r["make"]]["ma"] += 1
                out["by_category"][r["category"]]["mm"] += 1
                out["by_category"][r["category"]]["ma"] += 1
                out["missed_match_rows"].append(r)
            continue

        if r["label"] == NO_MATCH:
            out["w_no_match_total"] += w
            out["by_make"][r["make"]]["no"] += 1
            out["by_category"][r["category"]]["no"] += 1
            if got is True:
                out["false_match"] += 1
                out["w_false_match"] += w
                out["by_make"][r["make"]]["fm"] += 1
                out["by_category"][r["category"]]["fm"] += 1
                out["false_match_rows"].append(r)
            elif got is None:
                out["abstain_on_no_match"] += 1
                out["correct_reject"] += 1
            else:
                out["correct_reject"] += 1

        elif r["label"] == MATCH:
            out["w_match_total"] += w
            out["by_make"][r["make"]]["ma"] += 1
            out["by_category"][r["category"]]["ma"] += 1
            if got is True:
                out["correct_match"] += 1
            else:
                out["missed_match"] += 1
                out["w_missed_match"] += w
                out["by_make"][r["make"]]["mm"] += 1
                out["by_category"][r["category"]]["mm"] += 1
                out["missed_match_rows"].append(r)
                if got is None:
                    out["abstain_on_match"] += 1

        else:  # ABSTAIN_OK
            if got is True:
                out["abstain_ok_true"] += 1
            elif got is False:
                out["abstain_ok_false"] += 1
            else:
                out["abstain_ok_none"] += 1

    n_no = out["correct_reject"] + out["false_match"]
    n_ma = out["correct_match"] + out["missed_match"]
    n_ab = out["abstain_ok_true"] + out["abstain_ok_false"] + out["abstain_ok_none"]
    out["n_no_match"] = n_no
    out["n_match"] = n_ma
    out["n_abstain_ok"] = n_ab
    out["false_match_rate"] = out["false_match"] / n_no if n_no else 0.0
    out["missed_match_rate"] = out["missed_match"] / n_ma if n_ma else 0.0
    out["abstain_rate"] = ((out["abstain_on_no_match"] + out["abstain_on_match"] +
                            out["abstain_ok_none"]) / len(rows)) if rows else 0.0
    out["w_false_match_rate"] = (out["w_false_match"] / out["w_no_match_total"]
                                 if out["w_no_match_total"] else 0.0)
    out["w_missed_match_rate"] = (out["w_missed_match"] / out["w_match_total"]
                                  if out["w_match_total"] else 0.0)
    out["by_make"] = dict(out["by_make"])
    out["by_category"] = dict(out["by_category"])
    return out


def _pct(x):
    return f"{100.0 * x:5.1f}%"


def _stamp(fn):
    """Identify the code actually being graded. edge_canon.py is under active
    development by another agent; a score without a version stamp is noise."""
    import hashlib
    try:
        path = inspect.getsourcefile(fn) or "<unknown>"
        with open(path, "rb") as fh:
            digest = hashlib.md5(fh.read()).hexdigest()
        return f"{path}  md5 {digest}  mtime {os.path.getmtime(path):.0f}"
    except Exception:                                          # noqa: BLE001
        return "<source unavailable>"


def report(res, name="candidate", show_rows=25, stream=sys.stdout, fn=None):
    w = stream.write
    w("=" * 78 + "\n")
    w(f"  {name}\n")
    if fn is not None:
        w(f"  graded implementation: {_stamp(fn)}\n")
    w("=" * 78 + "\n")
    w(f"  pairs graded            {res['n']}   "
      f"(no-match {res['n_no_match']}, match {res['n_match']}, abstain-ok {res['n_abstain_ok']})\n")
    w(f"  accepts make/model      {res['pass_context']}\n\n")
    w("  >>> FALSE-MATCH RATE    "
      f"{_pct(res['false_match_rate'])}  ({res['false_match']}/{res['n_no_match']})"
      "   <-- THE NUMBER THAT MATTERS\n")
    w(f"      row-weighted        {_pct(res['w_false_match_rate'])}  "
      f"({res['w_false_match']:,}/{res['w_no_match_total']:,} bid x comp exposures)\n")
    w(f"      missed-match rate   {_pct(res['missed_match_rate'])}  "
      f"({res['missed_match']}/{res['n_match']})\n")
    w(f"      row-weighted        {_pct(res['w_missed_match_rate'])}\n")
    w(f"      abstain rate        {_pct(res['abstain_rate'])}\n")
    w(f"      errors              {res['errors']}\n\n")

    w("  BY CATEGORY (false / no-match  |  missed / match)\n")
    for cat in sorted(res["by_category"]):
        c = res["by_category"][cat]
        flag = ""
        if cat in DEBATABLE:
            flag = "  [debatable]"
        elif cat in CONTEXT_ONLY:
            flag = "  [context-only]"
        w(f"    {cat:<24} {c['fm']:>3}/{c['no']:<4}   {c['mm']:>3}/{c['ma']:<4}{flag}\n")

    w("\n  BY MAKE (false / no-match  |  missed / match)\n")
    for mk in sorted(res["by_make"], key=lambda m: (-res["by_make"][m]["fm"], m)):
        c = res["by_make"][mk]
        w(f"    {mk:<16} {c['fm']:>3}/{c['no']:<4}   {c['mm']:>3}/{c['ma']:<4}\n")

    if res["false_match_rows"]:
        w(f"\n  FALSE MATCHES ({len(res['false_match_rows'])}) -- each one is a wrong number on the bid page\n")
        rows = sorted(res["false_match_rows"],
                      key=lambda r: -(max(1, r["n_a"]) * max(1, r["n_b"])))
        for r in rows[:show_rows]:
            w(f"    [{r['category']}] {r['make']} {r['model']}: "
              f"{r['a']!r} ~ {r['b']!r}  ({r['n_a']}x{r['n_b']} rows)\n")
            w(f"        {r['basis']}\n")
        if len(rows) > show_rows:
            w(f"    ... {len(rows) - show_rows} more\n")

    if res["missed_match_rows"]:
        w(f"\n  MISSED MATCHES ({len(res['missed_match_rows'])}) -- acceptable, but this is the recall cost\n")
        rows = sorted(res["missed_match_rows"],
                      key=lambda r: -(max(1, r["n_a"]) * max(1, r["n_b"])))
        for r in rows[:show_rows]:
            w(f"    [{r['category']}] {r['make']} {r['model']}: "
              f"{r['a']!r} !~ {r['b']!r}  ({r['n_a']}x{r['n_b']} rows)\n")
        if len(rows) > show_rows:
            w(f"    ... {len(rows) - show_rows} more\n")

    if res["error_rows"]:
        w(f"\n  ERRORS ({len(res['error_rows'])})\n")
        for r, e in res["error_rows"][:show_rows]:
            w(f"    {r['make']} {r['model']}: {r['a']!r} / {r['b']!r} -> {e}\n")
    w("\n")
    return res


def grade(fn, name="candidate", show_rows=25, **kw):
    """Convenience: score + print + return. This is the importable entry point.

        from trim_match_eval import grade
        grade(my_trims_match, "my matcher v3")
    """
    return report(score(fn, **kw), name=name, show_rows=show_rows, fn=fn)


def baseline_fn():
    """The CURRENT production implementation, imported read-only."""
    from edge_canon import trims_match
    return trims_match


# ---------------------------------------------------------------------------
# --mine : re-derive CANDIDATE pairs from the live DB. NEVER assigns labels.
# ---------------------------------------------------------------------------
_MINE_SQL_STYLES = """
SELECT canon_make, canon_model, COALESCE(style, ''), COUNT(*)
  FROM auction_comps
 GROUP BY 1, 2, 3
"""
_MINE_SQL_BIDS = """
SELECT COALESCE(canon_make, UPPER(make)), COALESCE(canon_model, UPPER(model)),
       COALESCE(canon_trim, trim, ''), COUNT(*)
  FROM bids
 WHERE COALESCE(canon_trim, trim, '') <> ''
 GROUP BY 1, 2, 3
"""


def _dsn():
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            return ln.strip().split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("no DATABASE_URL")


def mine(min_prefix=8, limit=400):
    """Print candidate pairs the CURRENT rule would match but that differ.

    Read-only. Output is a WORKLIST for a human labeller, not a corpus.
    """
    import itertools
    import psycopg2
    from edge_canon import canon_make, norm_text, trims_match

    conn = psycopg2.connect(_dsn(), connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    pool = collections.defaultdict(dict)
    try:
        with conn.cursor() as cur:
            for sql, side in ((_MINE_SQL_STYLES, "auc"), (_MINE_SQL_BIDS, "bid")):
                cur.execute(sql)
                for mk, md, s, c in cur.fetchall():
                    if not (s or "").strip():
                        continue
                    k = (canon_make(mk or ""), norm_text(md or ""))
                    ent = pool[k].setdefault(norm_text(s), {"auc": 0, "bid": 0, "raw": s})
                    ent[side] += c
    finally:
        conn.close()

    rows = []
    for k, ent in pool.items():
        for x, y in itertools.combinations(sorted(ent), 2):
            if trims_match(x, y, min_prefix=min_prefix):
                n = 0
                for p, q in zip(x, y):
                    if p != q:
                        break
                    n += 1
                rows.append((n, k[0], k[1], ent[x], ent[y]))
    rows.sort(key=lambda r: -(max(1, r[3]["auc"] + r[3]["bid"]) *
                              max(1, r[4]["auc"] + r[4]["bid"])))
    print(f"# {len(rows)} candidate pairs the current rule MATCHES but that are not identical.")
    print("# prefix | make | model | A (auc/bid) | B (auc/bid)   -- LABEL THESE BY HAND")
    for n, mk, md, x, y in rows[:limit]:
        print(f"{n:>3} | {mk} | {md} | {x['raw']!r} {x['auc']}/{x['bid']} | "
              f"{y['raw']!r} {y['auc']}/{y['bid']}")


def ambiguity():
    """Machine-check the truncation categories against auction_comps.

    For every prefix-shaped row, take the SHORTER normalized string and print
    every style observed on that make/model that it is a leading substring of.
    This is EVIDENCE FOR THE LABEL, not the label: only the trim ladder can say
    whether two expansions are two trims or one trim spelled twice.

    Read-only.
    """
    import psycopg2
    from edge_canon import canon_make, norm_text

    conn = psycopg2.connect(_dsn(), connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    styles = collections.defaultdict(collections.Counter)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT canon_make, canon_model, style, COUNT(*) "
                        "FROM auction_comps WHERE style IS NOT NULL "
                        "GROUP BY 1, 2, 3")
            for mk, md, v, c in cur.fetchall():
                n = norm_text(v)
                if n:
                    styles[(canon_make(mk or ""), norm_text(md or ""))][n] += c
    finally:
        conn.close()

    want = ("truncation_ambiguous", "truncation_exact", "hierarchy",
            "package_suffix", "naming_era")
    print("Observed expansions of each fragment, from auction_comps.")
    print("EVIDENCE for the hand-assigned label -- not the label itself.\n")
    seen = set()
    for r in CORPUS:
        if r["category"] not in want:
            continue
        mk, md = canon_make(r["make"]), norm_text(r["model"])
        na, nb = norm_text(r["a"]), norm_text(r["b"])
        frag = na if len(na) <= len(nb) else nb
        if (mk, md, frag) in seen:
            continue
        seen.add((mk, md, frag))
        exp = sorted(x for x in styles.get((mk, md), ()) if x.startswith(frag))
        print(f"  {r['make']} {r['model']:<16} {frag!r:<22} -> {len(exp)}: {exp}")
    print(f"\n{len(seen)} distinct fragments checked.")
    return 0


def selftest():
    """The corpus must not contradict itself.

    A grader that carries two different labels for the same normalized pair
    cannot be trusted to grade anything. Run this before believing any score.
    """
    from edge_canon import canon_make, norm_text

    groups = collections.defaultdict(list)
    for r in CORPUS:
        ab = tuple(sorted((norm_text(r["a"]), norm_text(r["b"]))))
        groups[(canon_make(r["make"]), norm_text(r["model"])) + ab].append(r)

    conflicts = [(k, v) for k, v in groups.items()
                 if len({x["label"] for x in v}) > 1]
    bad_label = [r for r in CORPUS if r["label"] not in (MATCH, NO_MATCH, ABSTAIN_OK)]
    bad_basis = [r for r in CORPUS if not r.get("basis")]

    print(f"corpus {len(CORPUS)} pairs")
    print(f"  distinct normalized pairs   {len(groups)}")
    print(f"  label conflicts             {len(conflicts)}")
    print(f"  rows with a bad label       {len(bad_label)}")
    print(f"  rows with no stated basis   {len(bad_basis)}")
    for k, v in conflicts:
        print("  CONFLICT", k, [(x["label"], x["category"]) for x in v])
    for r in bad_label + bad_basis:
        print("  BAD ROW", r["make"], r["model"], r["a"], r["b"])
    ok = not (conflicts or bad_label or bad_basis)
    print("  SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def provenance():
    """Re-check, against the LIVE DB, that every corpus string was really observed.

    Read-only. The corpus makes no frozen provenance claim -- this proves it on
    demand. Prints, per side, whether the string occurs as an auction_comps.style
    or a bids.trim/canon_trim under that make.
    """
    import psycopg2
    from edge_canon import canon_make, norm_text

    conn = psycopg2.connect(_dsn(), connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    styles, trims = collections.defaultdict(set), collections.defaultdict(set)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT canon_make, style FROM auction_comps WHERE style IS NOT NULL")
            for mk, v in cur.fetchall():
                n = norm_text(v)
                if n:
                    styles[canon_make(mk or "")].add(n)
            cur.execute("SELECT COALESCE(canon_make, make), COALESCE(canon_trim, trim) FROM bids")
            for mk, v in cur.fetchall():
                n = norm_text(v)
                if n:
                    trims[canon_make(mk or "")].add(n)
    finally:
        conn.close()

    synthetic = {"blank", "identity", "cross_make"}

    def where(mk, v):
        n = norm_text(v)
        if not n:
            return "empty"
        tags = []
        if n in styles.get(mk, ()):
            tags.append("auction")
        if n in trims.get(mk, ()):
            tags.append("bid")
        return "+".join(tags) if tags else "NOT-FOUND"

    counts, missing = collections.Counter(), []
    for r in CORPUS:
        if r["category"] in synthetic:
            counts["synthetic"] += 1
            continue
        mk = canon_make(r["make"])
        wa, wb = where(mk, r["a"]), where(mk, r["b"])
        counts[f"a={wa} b={wb}"] += 1
        if "NOT-FOUND" in (wa, wb):
            missing.append((r["make"], r["model"], r["a"], wa, r["b"], wb))

    print(f"corpus {len(CORPUS)} pairs; provenance against the live DB:")
    for k, v in counts.most_common():
        print(f"  {v:>4}  {k}")
    if missing:
        print(f"\n  {len(missing)} STRING(S) NOT OBSERVED -- fix or remove:")
        for m in missing:
            print("   ", m)
    else:
        print("\n  every non-synthetic string was observed in the live DB.")
    return 1 if missing else 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mine", action="store_true",
                    help="re-derive candidate pairs from the live DB (read-only, unlabelled)")
    ap.add_argument("--min-prefix", type=int, default=8, help="for --mine")
    ap.add_argument("--rows", type=int, default=25, help="how many failing rows to print")
    ap.add_argument("--no-debatable", action="store_true",
                    help="exclude engine/drivetrain/body variants from the headline")
    ap.add_argument("--stats", action="store_true", help="print corpus composition and exit")
    ap.add_argument("--provenance", action="store_true",
                    help="prove every corpus string still occurs in the live DB (read-only)")
    ap.add_argument("--selftest", action="store_true",
                    help="check the corpus does not contradict itself")
    ap.add_argument("--ambiguity", action="store_true",
                    help="machine-check the truncation categories against auction_comps")
    args = ap.parse_args(argv)

    if args.mine:
        mine(min_prefix=args.min_prefix)
        return 0

    if args.selftest:
        return selftest()

    if args.ambiguity:
        return ambiguity()

    if args.provenance:
        return provenance()

    if args.stats:
        by_lab = collections.Counter(r["label"] for r in CORPUS)
        by_cat = collections.Counter(r["category"] for r in CORPUS)
        by_mk = collections.Counter(r["make"] for r in CORPUS)
        print(f"corpus: {len(CORPUS)} labelled pairs")
        print("  by label:    " + ", ".join(f"{k}={v}" for k, v in sorted(by_lab.items())))
        print("  by make:     " + ", ".join(f"{k}={v}" for k, v in by_mk.most_common()))
        print("  by category:")
        for k, v in sorted(by_cat.items()):
            print(f"    {k:<24} {v}")
        return 0

    fn = baseline_fn()
    res = report(score(fn, include_debatable=not args.no_debatable),
                 name="BASELINE  edge_canon.trims_match  (min_prefix=8)",
                 show_rows=args.rows, fn=fn)

    if not args.no_debatable:
        alt = score(fn, include_debatable=False)
        print("  Headline WITHOUT the debatable engine/drivetrain/body categories:")
        print(f"    false-match rate  {_pct(alt['false_match_rate'])} "
              f"({alt['false_match']}/{alt['n_no_match']})")
        print(f"    missed-match rate {_pct(alt['missed_match_rate'])} "
              f"({alt['missed_match']}/{alt['n_match']})\n")

    return 0 if res["false_match"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
