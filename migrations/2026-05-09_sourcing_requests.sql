-- 2026-05-09 sourcing_requests
--
-- AI-driven SMS sourcing flow. When a wholesaler/dealer texts the EW
-- 754 number to look for a vehicle (vs reply to a bid), the inbound
-- router classifies, gathers specs via Gemini, searches EW dealer
-- inventory, and either presents matches or saves a 30-day wishlist.
--
-- See project_sourcing_bot.md for full design.

CREATE TABLE IF NOT EXISTS sourcing_requests (
    id              SERIAL PRIMARY KEY,
    phone           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'gathering',
        -- gathering | searching | presented | matched | wishlist | archived

    -- hard spec
    year_min        INTEGER,
    year_max        INTEGER,
    make            TEXT,
    model           TEXT,
    trim            TEXT,

    -- soft spec (filters when set, ignored when NULL)
    ext_color       TEXT[],
    int_color       TEXT[],
    miles_max       INTEGER,
    options         TEXT[],
    must_clean_title BOOLEAN DEFAULT TRUE,

    -- soft hint (sort only, never filter)
    price_hint      INTEGER,

    -- conversation
    conversation    JSONB NOT NULL DEFAULT '[]'::jsonb,
        -- [{role:'user'|'bot', ts:iso, text:str, raw:obj}]
    last_msg_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_inbound_at TIMESTAMPTZ,

    -- wishlist
    wishlist_until  TIMESTAMPTZ,
    last_scan_at    TIMESTAMPTZ,
    notified_inventory_ids INTEGER[] NOT NULL DEFAULT '{}',

    -- handoff
    handoff_summary TEXT,
    handoff_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at     TIMESTAMPTZ,
    archive_reason  TEXT,

    CONSTRAINT sourcing_status_chk CHECK (status IN
        ('gathering','searching','presented','matched','wishlist','archived'))
);

-- Find the active conversation for an incoming phone number fast.
CREATE INDEX IF NOT EXISTS idx_sourcing_active_phone
    ON sourcing_requests (phone, last_msg_at DESC)
    WHERE status <> 'archived';

-- Wishlist sweep — only scan rows still in monitoring window.
CREATE INDEX IF NOT EXISTS idx_sourcing_wishlist_active
    ON sourcing_requests (wishlist_until)
    WHERE status = 'wishlist';

-- updated_at trigger
CREATE OR REPLACE FUNCTION sourcing_requests_touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sourcing_requests_touch ON sourcing_requests;
CREATE TRIGGER sourcing_requests_touch
    BEFORE UPDATE ON sourcing_requests
    FOR EACH ROW EXECUTE FUNCTION sourcing_requests_touch_updated_at();
