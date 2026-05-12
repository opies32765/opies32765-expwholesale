-- 2026-05-12 (later): track which thalist posts spawned junk bids.
--
-- Rule: when a thalist-sourced bid gets through AccuTrade and AccuTrade
-- returns no values (no guaranteed_offer, trade_in, or market_avg), the
-- VIN the seller typed is invalid/junk. We delete the bid and stamp the
-- ledger row so re-scrapes know to skip re-creating it.

ALTER TABLE thalist_posts
    ADD COLUMN IF NOT EXISTS invalidated_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidate_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_thalist_posts_invalidated
    ON thalist_posts(invalidated_at) WHERE invalidated_at IS NOT NULL;
