-- DEALER_SCORECARD_2026_07_31
-- Permanent, self-refreshing dealer profitability board + batting average.
--
-- Origin: management liked the historical-profit figures surfaced for the
-- DealerPrice outreach email. Those live in dp_outreach_targets and are a
-- FROZEN SNAPSHOT built for that one send -- only the clocks refresh, the
-- profit columns never do. This makes the numbers permanent and current, and
-- adds the batting average (cars a dealer set in vs cars we actually bought).
--
-- HARD RULES honored:
--   HR8 no import-time DDL -- schema ships here, never from app code.
--   HR1 no FK from these objects to bids -- nothing here can gate or delay
--       enrichment. The scorecard is a pure read-side consumer.
--   HR6 LSL (crm.db) stays read-only; this only ever SELECTs from it.
--
-- Reversible: every statement is IF NOT EXISTS / ADD COLUMN, no data is moved
-- or dropped. dp_outreach_targets is left completely untouched so the 08-04
-- send is not disturbed.

-- ── 1. who SET THE CAR IN ────────────────────────────────────────────────────
-- Deliberately NOT the existing bids.lsl_supplier_id: that one means "the
-- supplier on the deal we pushed to LSL", a different fact. A car set in by
-- Marino and sold to Germain would corrupt one of the two if they shared a
-- column.
--
-- source_supplier_id is an LSL suppliers.id, never a typed name. Names collide
-- (43 dealer names map to multiple rooftops; one switchboard phone covers 13
-- stores) -- audit rule 3 from _lsl_history. The typeahead resolves to an id
-- before anything is stored.
ALTER TABLE bids ADD COLUMN IF NOT EXISTS source_supplier_id   INTEGER;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS source_supplier_name TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS source_tagged_by     TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS source_tagged_at     TIMESTAMPTZ;
-- manual = a human typed it on the bid page; dealerprice = stamped automatically
-- from the submitting member. Kept apart so an inferred number can never be
-- mistaken for a human-confirmed one.
ALTER TABLE bids ADD COLUMN IF NOT EXISTS source_tag_origin    TEXT;

CREATE INDEX IF NOT EXISTS bids_source_supplier_idx
    ON bids (source_supplier_id) WHERE source_supplier_id IS NOT NULL;

-- ── 2. member -> LSL supplier ────────────────────────────────────────────────
-- dealerprice_members had no route to LSL at all: lsl_match is jsonb and is
-- JSON-null on 11 of 14 rows, and scanner_dealer_id points at the scan roster
-- (dealers), not at LSL. Without this, a bid stamped with dp_member_id still
-- cannot reach the profit history.
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS lsl_supplier_id INTEGER;

-- ── 3. the board ─────────────────────────────────────────────────────────────
-- Keyed on LSL suppliers.id. One row per rooftop, rebuilt in full by
-- dealer_scorecard.py -- it is a derived cache, so a rebuild is always safe.
CREATE TABLE IF NOT EXISTS dp_dealer_scorecard (
  supplier_id        INTEGER PRIMARY KEY,
  supplier_name      TEXT,
  -- EW BOUGHT FROM them
  bought_cars        INTEGER DEFAULT 0,
  bought_paid        BIGINT  DEFAULT 0,
  buy_first          DATE,
  buy_last           DATE,
  -- EW SOLD TO them
  sold_cars          INTEGER DEFAULT 0,
  sold_revenue       BIGINT  DEFAULT 0,
  sold_gross         BIGINT  DEFAULT 0,
  sell_first         DATE,
  sell_last          DATE,
  -- what EW made on the whole relationship
  buy_resale_cars    INTEGER DEFAULT 0,
  buy_resale_gross   BIGINT  DEFAULT 0,
  total_gross        BIGINT  DEFAULT 0,
  tx_count           INTEGER DEFAULT 0,
  first_activity     DATE,
  last_activity      DATE,
  days_since         INTEGER,
  -- batting average
  set_in_cars        INTEGER DEFAULT 0,   -- distinct VINs tagged to this dealer
  set_in_first       DATE,
  set_in_last        DATE,
  acquired_cars      INTEGER DEFAULT 0,   -- of those, bought FROM THIS dealer
  acquired_any       INTEGER DEFAULT 0,   -- of those, acquired by EW from anyone
  batting            NUMERIC(5,2),        -- acquired_cars / set_in_cars, NULL if none set in
  refreshed_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dp_scorecard_gross_idx  ON dp_dealer_scorecard (total_gross DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS dp_scorecard_batting_idx ON dp_dealer_scorecard (batting) WHERE set_in_cars > 0;
CREATE INDEX IF NOT EXISTS dp_scorecard_name_idx   ON dp_dealer_scorecard (lower(supplier_name));

-- run bookkeeping, so the page can show how fresh it is and a failed refresh
-- is visible rather than silently serving stale numbers
CREATE TABLE IF NOT EXISTS dp_dealer_scorecard_run (
  id          SERIAL PRIMARY KEY,
  started_at  TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ,
  dealers     INTEGER,
  ok          BOOLEAN,
  error       TEXT,
  secs        NUMERIC(8,2)
);
