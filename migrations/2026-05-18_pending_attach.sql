-- 2026-05-18 — pending_attach table for Layer 2 "ask first" customer-reply
-- confirmation flow.
--
-- When a customer sends an inbound with VIN/miles/photos but NO #bid hint
-- AND has at least one verify-pending bid in the last 24h, we stage the
-- inbound here instead of creating a new bid or silently stitching. The
-- bot sends a YMM-based confirmation SMS. The customer replies YES/NO/A/B
-- and we resolve by attaching to the chosen bid (or creating a new bid on
-- NO/NEW).
--
-- Operator can also force-resolve via /admin/api/pending-attach/<id>/resolve.

CREATE TABLE IF NOT EXISTS pending_attach (
    id              BIGSERIAL PRIMARY KEY,
    phone           TEXT NOT NULL,
    inbound_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The staged inbound data
    body            TEXT,
    num_media       INTEGER NOT NULL DEFAULT 0,
    media_urls      JSONB,   -- list of Twilio MediaUrl{i} values
    media_types     JSONB,   -- list of MIME types parallel to media_urls
    parsed_vin      TEXT,    -- post-extraction (incl. IOQ salvage)
    parsed_miles    INTEGER,

    -- The candidate bids we offered (denormalized for atomic resolution)
    candidates      JSONB NOT NULL,  -- [{bid_id, year, make, model, letter, status, reason}, ...]

    -- The question we asked (for audit)
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asked_body      TEXT,
    asked_sms_sid   TEXT,    -- Twilio SID of the question we sent

    -- Resolution outcome
    resolved_at     TIMESTAMPTZ,
    resolved_bid_id INTEGER REFERENCES bids(id) ON DELETE SET NULL,
    resolved_by     TEXT,    -- 'user_choice' | 'user_new' | 'op_force' | 'expired'
    resolved_note   TEXT,

    -- Soft expiry (24h)
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours')
);

CREATE INDEX IF NOT EXISTS idx_pa_phone_open
    ON pending_attach (phone, asked_at DESC)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pa_expires_open
    ON pending_attach (expires_at)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pa_resolved_bid
    ON pending_attach (resolved_bid_id)
    WHERE resolved_bid_id IS NOT NULL;
