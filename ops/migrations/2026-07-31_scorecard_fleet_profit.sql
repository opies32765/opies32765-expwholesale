-- SCORECARD_FLEET_PROFIT_2026_07_31
-- The header KPI summed dp_dealer_scorecard.total_gross across dealers. That
-- DOUBLE COUNTS: EW buys a car from dealer A and sells it to dealer B, and the
-- front_value on that one deal is credited to BOTH relationships -- to A as
-- profit on a car we bought from them and resold, to B as profit on a car we
-- sold them. Both per-dealer rows are correct; the SUM of them is not a real
-- number. Measured: summed = $71.1M against a true $35.4M.
--
-- (commit 13c6583 flagged exactly this on the outreach tab: "Lifetime $ was
-- gross PROFIT all along, but summing it across dealers double-counts - one
-- deal credits both counterparties.")
--
-- So the fleet figure is computed ONCE from the deals ledger, deduped by VIN,
-- and stored per run. Per-dealer rows are never summed for a headline again.
ALTER TABLE dp_dealer_scorecard_run ADD COLUMN IF NOT EXISTS fleet_profit BIGINT;
ALTER TABLE dp_dealer_scorecard_run ADD COLUMN IF NOT EXISTS fleet_deals  INTEGER;
