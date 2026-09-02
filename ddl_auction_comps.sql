-- auction_comps : EW's own permanent store of auction outcomes.
-- Edge Pipeline retains ~6 weeks; this table never deletes. That is the point.
-- AUCTION_COMPS_2026_08_29
--
-- vin is NULL for sold cars (Edge masks it on post-sale) and populated for
-- no-sales and for anything captured from a pre-sale run list. Only rows with
-- a vin can be enriched A-to-Z.

CREATE TABLE IF NOT EXISTS auction_comps (
    id             bigserial PRIMARY KEY,
    auction_slug   text        NOT NULL,
    auction_name   text,
    sale_date      date        NOT NULL,
    stock_no       text        NOT NULL,
    run_number     text,
    lane           text,
    lot            text,

    vin            text,                 -- full 17; only pre-sale / no-sale
    vin_partial    text,                 -- 10-char squish, when harvested

    year           int,
    make           text,
    model          text,
    style          text,
    color          text,
    odometer       int,
    has_cr         boolean,
    grade          numeric(3,1),
    lights         text,
    announcements  text,

    outcome        text        NOT NULL, -- 'sold' | 'no_sale' | 'pre_sale'
    price          int,                  -- hammer price; NULL unless sold

    picture_count  int,
    canon_make     text,                 -- normalized, see edge_canon.py
    canon_model    text,

    source_file    text,
    ingested_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT auction_comps_uniq UNIQUE (auction_slug, sale_date, stock_no)
);

-- the hot path: same car, nearest miles
CREATE INDEX IF NOT EXISTS idx_ac_ymm      ON auction_comps (canon_make, year, canon_model);
CREATE INDEX IF NOT EXISTS idx_ac_odo      ON auction_comps (odometer);
CREATE INDEX IF NOT EXISTS idx_ac_sale     ON auction_comps (sale_date DESC);
CREATE INDEX IF NOT EXISTS idx_ac_outcome  ON auction_comps (outcome);
CREATE INDEX IF NOT EXISTS idx_ac_vin      ON auction_comps (vin) WHERE vin IS NOT NULL;
