-- 2026-05-12: thalist.com Wholesale Inventory scraper -> bid intake
--
-- Each scraped post lands as a thalist_posts ledger row. Posts with new
-- VINs also spawn a bid (creation_source='thalist'). Posts whose VIN
-- matches an existing open bid are recorded in the ledger but do not
-- create a new bid (dedupe).

CREATE TABLE IF NOT EXISTS thalist_posts (
    id              SERIAL PRIMARY KEY,
    post_id         BIGINT NOT NULL,         -- thalist's post id (often negative)
    vin             VARCHAR(17),
    title           TEXT,
    year            INTEGER,
    make_id         INTEGER,
    model           TEXT,
    asking_price    INTEGER,
    mileage         INTEGER,
    location_zip    VARCHAR(16),
    description     TEXT,
    teaser          TEXT,
    title_holder    TEXT,
    poster_name     TEXT,
    poster_company  TEXT,
    poster_company_id INTEGER,
    photos          JSONB,                   -- array of image URLs
    detail_url      TEXT NOT NULL,
    raw_payload     JSONB,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bid_id          INTEGER REFERENCES bids(id) ON DELETE SET NULL,
    dedupe_target_bid_id INTEGER REFERENCES bids(id) ON DELETE SET NULL
);

-- A given thalist post_id is unique across time; we upsert on it so
-- repeat scrapes just bump last_seen_at.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thalist_posts_post_id
    ON thalist_posts(post_id);

CREATE INDEX IF NOT EXISTS idx_thalist_posts_vin
    ON thalist_posts(vin) WHERE vin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_thalist_posts_first_seen
    ON thalist_posts(first_seen_at DESC);
