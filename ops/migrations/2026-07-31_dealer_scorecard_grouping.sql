-- DEALER_SCORECARD_GROUPING_2026_07_31
-- Follow-up to 2026-07-31_dealer_scorecard.sql, same day, before anything
-- consumed the board.
--
-- Two corrections the operator caught on review:
--
-- 1. "2300 is not all dealers." Correct. suppliers holds 2,645 rows and only
--    1,967 have an uploaded licence or tax cert. The rest are largely private
--    individuals who sold one car to the desk, plus marketplaces (Backlotcars,
--    TradeRev, Manheim). Measured on the board: 1,180 licenced dealers carry
--    $67.9M of the $71.1M gross; the 1,168 unlicenced carry $3.2M.
--    The licence is the discriminator -- 83% of the outreach targets have one
--    against 0% of known retail (project_dealerprice_verified_sendlist_20260730).
--    Kept as a FLAG, not a filter: 53 real-looking franchise stores have no
--    uploaded licence, so hard-deleting them would lose real dealers. The board
--    defaults to dealers and can show everything.
--
-- 2. Fragmented supplier ids. One rooftop can hold several suppliers.id, which
--    is what hid Scott Ales from the outreach list (commit 4599b0c).
--    Merged on a CONSERVATIVE key: case, punctuation and invisible-character
--    folding only. Trade words are KEPT.
--    classify.py normalize_name stops "auto/motors/group/sales" and would merge
--    "Wholesale Auto Group" + "Wholesale Inc" + "Dealer Wholesale Group LLC"
--    into one dealer, and Enterprise/Enterprise Holdings/Enterprise Car Sales
--    likewise. The conservative key folds 56 ids into 26 groups, every one of
--    them a genuine duplicate (Signature Auto Group x4, F.c. Kerbeck & Sons x3,
--    BMW FINANCIAL SERVICES vs Bmw Financial Services).
--    Operator decisions in dealerprice_person_links are unioned on top and win:
--    3 confirmed merges are applied, and the 1 rejected pair is never merged.

ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS dealer_key    TEXT;
ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS supplier_ids  INTEGER[];
ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS rooftops      INTEGER DEFAULT 1;
-- licence evidence: the dealer-vs-retail discriminator
ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS is_dealer     BOOLEAN DEFAULT FALSE;
ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS has_license   BOOLEAN DEFAULT FALSE;
ALTER TABLE dp_dealer_scorecard ADD COLUMN IF NOT EXISTS has_tax_cert  BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS dp_scorecard_isdealer_idx ON dp_dealer_scorecard (is_dealer, total_gross DESC);
CREATE INDEX IF NOT EXISTS dp_scorecard_key_idx      ON dp_dealer_scorecard (dealer_key);
