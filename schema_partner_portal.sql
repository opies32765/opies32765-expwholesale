-- Partner Dealer Portal schema.
--
-- One partner user per dealer (TXT Charlie, Marino, Napletons, …). Dealer logs
-- into their curated dashboard, sees cars that hit configurable age buckets,
-- checks the ones they want us to bid + sets target price + message. Submit
-- creates EW bids. Responses stream back to the dashboard. Daily 5pm ET digest
-- emails them new cars that crossed a threshold.

-- ── partner_users ─────────────────────────────────────────────────────────
-- Credentials. One row per partner login. Dealer sets own password via
-- emailed invite link. Forgot-password emits a reset_token.
CREATE TABLE IF NOT EXISTS partner_users (
    id              SERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    email           TEXT NOT NULL UNIQUE,
    full_name       TEXT,
    password_hash   TEXT,                -- NULL until they complete invite
    invite_token    TEXT UNIQUE,         -- one-time set-password token
    invite_sent_at  TIMESTAMPTZ,
    invite_used_at  TIMESTAMPTZ,
    reset_token     TEXT UNIQUE,         -- forgot-password token
    reset_expires   TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_partner_users_dealer ON partner_users(dealer_id);

-- Notification preferences (added 2026-04-13 / 2026-05-04). Per spec
-- 2026-05-04, SMS opt-in is single-step (no 6-digit verify code anymore)
-- but the columns are kept for the legacy verification code path that
-- can be re-enabled per-dealer if Twilio compliance posture changes.
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS phone              VARCHAR(20);
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_opt_in         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verify_code    VARCHAR(8);
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verify_expires TIMESTAMPTZ;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verified_at    TIMESTAMPTZ;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS email_bid_alerts   BOOLEAN NOT NULL DEFAULT TRUE;

-- ── partner_alert_config ──────────────────────────────────────────────────
-- Per-dealer toggle for which alerts to emit. Rows pre-seeded on invite.
-- Bucket keys: '30_60', '60_90', '90_plus', 'price_drop'.
CREATE TABLE IF NOT EXISTS partner_alert_config (
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    bucket          TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dealer_id, bucket)
);

-- ── partner_alerts_sent ───────────────────────────────────────────────────
-- Dedupe: once a VIN has been alerted in a given bucket, never alert again.
-- Insert on digest send; query on next digest to skip already-alerted.
CREATE TABLE IF NOT EXISTS partner_alerts_sent (
    id              SERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    inventory_id    INTEGER NOT NULL REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    vin             TEXT NOT NULL,
    bucket          TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dealer_id, vin, bucket)
);
CREATE INDEX IF NOT EXISTS idx_partner_alerts_sent_dealer_at
    ON partner_alerts_sent(dealer_id, sent_at DESC);

-- ── partner_bid_requests ──────────────────────────────────────────────────
-- When the partner checks a car + submits with target_price + message, we
-- write this row AND create a corresponding entry in bids. Link back via
-- bid_id so we can 2-way sync message thread + status.
-- inventory_id is nullable so Quick Drop bids (cars not in scraped
-- inventory) can write a pbr row. partner_user_id is nullable so the
-- mobile-token submission path (no logged-in partner_user) can write.
CREATE TABLE IF NOT EXISTS partner_bid_requests (
    id              SERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    partner_user_id INTEGER REFERENCES partner_users(id) ON DELETE CASCADE,
    inventory_id    INTEGER REFERENCES dealer_inventory(id),
    vin             TEXT NOT NULL,
    target_price    NUMERIC(10,2),
    partner_message TEXT,
    bid_id          INTEGER REFERENCES bids(id) ON DELETE SET NULL,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Existing installs may still have NOT NULL on these — relax idempotently.
ALTER TABLE partner_bid_requests ALTER COLUMN partner_user_id DROP NOT NULL;
ALTER TABLE partner_bid_requests ALTER COLUMN inventory_id    DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_partner_bid_requests_dealer
    ON partner_bid_requests(dealer_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_bid_requests_bid
    ON partner_bid_requests(bid_id);

-- Mark bids originating from partner portal so the EW bid listing can
-- render a partner badge + route responses back to the portal.
-- partner_dealer_id is set DIRECTLY on the bid (in addition to the
-- transitive link via partner_request_id) so the EW dashboard can
-- query/filter by partner without joining through pbr.
-- salesperson is a snapshot of dealers.salesperson at create time —
-- frozen on the bid even if the dealer's assigned salesperson changes
-- later (preserves audit trail).
-- field_rep_name is the rep identity for mobile-tagged submissions
-- (decoupled from `phone` so partner-mobile bids that use phone=
-- 'PARTNER<id>' for visual parity still belong to a specific rep for
-- the My Bids tab).
ALTER TABLE bids ADD COLUMN IF NOT EXISTS partner_request_id INTEGER
    REFERENCES partner_bid_requests(id) ON DELETE SET NULL;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS partner_dealer_id  INTEGER
    REFERENCES dealers(id) ON DELETE SET NULL;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS salesperson    TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS field_rep_name TEXT;
CREATE INDEX IF NOT EXISTS idx_bids_partner_request ON bids(partner_request_id)
    WHERE partner_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bids_partner_dealer ON bids(partner_dealer_id)
    WHERE partner_dealer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bids_field_rep
    ON bids(LOWER(field_rep_name)) WHERE field_rep_name IS NOT NULL;
