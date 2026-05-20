-- 2026-05-19 — Dealer DB Graph System: Layer 1 (network-wide segment perf)
--
-- Per-segment performance across the full peer-dealer fleet (currently 14
-- luxury/exotic dealers EW scans daily). Dealer-agnostic — the same row is
-- used to inform every dealer's acquisition recommendations.
--
-- Computed nightly by dealer_intel_network.py BEFORE any per-dealer
-- intel run, so dealer-side queries can join against fresh network data.
--
-- Per-dealer Layer 3 (acquisition blind spots) is derived from this:
--   "Hot at peers but you have zero" = network row with dealers_selling >= 3
--   and sold_30d >= some threshold, joined LEFT to dealer's own segments.

CREATE TABLE IF NOT EXISTS network_segment_performance (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_date     DATE        NOT NULL,
    window_days       INTEGER     NOT NULL,   -- e.g. 30

    -- Segment dimensions (match dealer_intel_segments grain)
    segment_key       TEXT        NOT NULL,   -- 'porsche|2020-2023|0-40k'
    make              TEXT        NOT NULL,
    year_band         TEXT,
    mileage_band      TEXT,

    -- Network-wide metrics
    dealers_selling   INTEGER     NOT NULL DEFAULT 0,  -- distinct dealers with a sale in window
    sold_volume       INTEGER     NOT NULL DEFAULT 0,  -- total units sold in window
    avg_dol_days      NUMERIC(6,1),                    -- avg DOL at sale across the network
    median_dol_days   NUMERIC(6,1),
    active_count      INTEGER     NOT NULL DEFAULT 0,  -- total active across network
    dealers_with_active INTEGER   NOT NULL DEFAULT 0,  -- distinct dealers with >=1 active

    -- "Heat" — a derived score used to rank segments. Higher = more
    -- interesting (multiple dealers turning multiple units fast).
    heat_score        NUMERIC(8,2),

    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (snapshot_date, segment_key)
);

CREATE INDEX IF NOT EXISTS idx_network_segment_perf_date
    ON network_segment_performance (snapshot_date DESC, heat_score DESC);
CREATE INDEX IF NOT EXISTS idx_network_segment_perf_make
    ON network_segment_performance (make, snapshot_date DESC);

-- ─────────────────────────────────────────────────────────────────────
-- Layer 4: daily summary narrative (per dealer, per day)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dealer_intel_summary (
    id              BIGSERIAL PRIMARY KEY,
    dealer_id       INTEGER     NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    snapshot_date   DATE        NOT NULL,

    -- Gemini-written sections (rendered server-side from the model's
    -- structured-output response). Each section is a list of bullet
    -- objects {text, citations[]} so the portal can render them cleanly.
    headline        TEXT,                       -- the one-line lead
    what_you_move_best  JSONB NOT NULL DEFAULT '[]'::jsonb,
    watch_list          JSONB NOT NULL DEFAULT '[]'::jsonb,
    acquisition_blind_spots JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Source-of-truth counters for the credibility footer
    sample_sizes    JSONB       NOT NULL DEFAULT '{}'::jsonb,
        -- e.g. {"ew_bids_30d": 1311, "ew_sales_180d": 28386,
        --       "peer_dealers": 14, "peer_sold_30d": 781}

    -- Audit trail
    model_name      TEXT,                       -- 'gemini-2.5-flash'
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    raw_response    JSONB,                      -- full model JSON for debugging
    generation_ms   INTEGER,

    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (dealer_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_dealer_intel_summary_dealer_date
    ON dealer_intel_summary (dealer_id, snapshot_date DESC);
