-- ============================================================
-- Inbound bid push to subscribed partners + partner offer capture
-- 2026-05-11
-- ============================================================

-- Toggle: which dealers receive inbound bid pushes. Default FALSE.
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS receive_inbound_pushes BOOLEAN DEFAULT FALSE;

-- Audit trail: which bid got pushed to which dealer + viewed state.
CREATE TABLE IF NOT EXISTS bid_pushes (
    id         SERIAL PRIMARY KEY,
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    dealer_id  INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    pushed_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    viewed_at  TIMESTAMP,
    UNIQUE (bid_id, dealer_id)
);
CREATE INDEX IF NOT EXISTS idx_bid_pushes_dealer ON bid_pushes(dealer_id, pushed_at DESC);

-- Partner offer on an inbound bid. NOT a new partner_bid_requests row —
-- attaches to the original bids.id so the EW operator sees all offers
-- side-by-side on the bid detail page.
CREATE TABLE IF NOT EXISTS bid_partner_offers (
    id              SERIAL PRIMARY KEY,
    bid_id          INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    partner_user_id INTEGER REFERENCES partner_users(id) ON DELETE SET NULL,
    offer_amount    NUMERIC(10,2),
    message         TEXT,
    submitted_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    ew_seen_at      TIMESTAMP,
    ew_action       TEXT
);
CREATE INDEX IF NOT EXISTS idx_bid_partner_offers_bid ON bid_partner_offers(bid_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_bid_partner_offers_dealer ON bid_partner_offers(dealer_id, submitted_at DESC);

-- Enable Nuccio (dealer 16) for v1.
UPDATE dealers SET receive_inbound_pushes = TRUE WHERE id = 16;

-- Fix Mark Palangio's full_name (was the auto-generated placeholder).
UPDATE partner_users
   SET full_name = 'Mark Palangio'
 WHERE id = 22 AND full_name = 'Nuccio Auto Group (placeholder)';

-- Show final state for verification
SELECT id, name, receive_inbound_pushes FROM dealers WHERE id = 16;
SELECT id, dealer_id, email, full_name, phone, sms_opt_in, email_bid_alerts FROM partner_users WHERE dealer_id = 16 ORDER BY id;

-- 2026-05-11 followup: chown new tables to expuser (gunicorn user).
-- Discovered when /partner/<slug>/d/<token> hit InsufficientPrivilege
-- on SELECT from bid_pushes. The CREATE TABLE statements above ran
-- as postgres superuser, leaving expuser with no rights on the tables.
ALTER TABLE bid_pushes OWNER TO expuser;
ALTER TABLE bid_partner_offers OWNER TO expuser;
ALTER SEQUENCE bid_pushes_id_seq OWNER TO expuser;
ALTER SEQUENCE bid_partner_offers_id_seq OWNER TO expuser;
GRANT ALL ON bid_pushes TO expuser;
GRANT ALL ON bid_partner_offers TO expuser;
