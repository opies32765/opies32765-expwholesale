-- 2026-05-12: DealerClub live auction integration.
--
-- Each row in dealerclub_lots is one live auction. The scraper polls
-- POST /auctions/marketplace/active/ on a 30s cadence (5min idle) and
-- upserts by external_id. On first-seen we create an EW bid that runs
-- the standard canon + vAuto + AccuTrade + iPacket + Gemini pipeline.
-- Each subsequent poll just bumps current_price + end_time so the live
-- dashboard tiles can show the latest state without re-firing the
-- expensive enrichment pipeline.

CREATE TABLE IF NOT EXISTS dealerclub_lots (
    id              SERIAL PRIMARY KEY,
    external_id     TEXT NOT NULL UNIQUE,    -- 'AUC-HRP-RNR-MIZ'
    vin             VARCHAR(17),
    year            INTEGER,
    make            TEXT,
    model           TEXT,
    trim            TEXT,
    odometer        INTEGER,
    drivetrain      TEXT,

    -- Live bid state (updated every poll)
    current_price          INTEGER,
    high_bid               INTEGER,
    bid_count              INTEGER DEFAULT 0,
    unique_bidder_count    INTEGER DEFAULT 0,
    end_time               TIMESTAMPTZ,
    duration_in_minutes    INTEGER,

    -- Reserve / status flags
    reserve_met            BOOLEAN DEFAULT FALSE,
    is_no_reserve          BOOLEAN DEFAULT FALSE,
    reserve_price          INTEGER,
    reserve_progress_color TEXT,
    status                 TEXT,

    -- Media + deep link
    featured_image_url     TEXT,
    detail_url             TEXT NOT NULL,

    -- Optional fee/transport cache (populated by separate enrichment)
    estimated_buy_fee      INTEGER,
    estimated_transport    INTEGER,

    -- Linkage + bookkeeping
    bid_id                 INTEGER REFERENCES bids(id) ON DELETE SET NULL,
    raw_payload            JSONB,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_polled_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Was this lot deleted/closed? Stamped when status != 'active'
    closed_at              TIMESTAMPTZ,
    close_reason           TEXT
);

CREATE INDEX IF NOT EXISTS idx_dealerclub_lots_vin
    ON dealerclub_lots(vin) WHERE vin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dealerclub_lots_status_endtime
    ON dealerclub_lots(status, end_time DESC);

CREATE INDEX IF NOT EXISTS idx_dealerclub_lots_active
    ON dealerclub_lots(end_time)
    WHERE status = 'active' AND closed_at IS NULL;
