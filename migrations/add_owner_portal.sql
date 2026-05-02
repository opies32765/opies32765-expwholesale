-- EW Owner Portal — separate user table for the 3 EW owners.
-- Owners get a mobile-first read-mostly view of bid activity + push
-- notifications when new bids arrive. Push routing piggybacks on the
-- existing push_subscriptions table by using rep_phone = 'owner:<id>'.
CREATE TABLE IF NOT EXISTS owner_users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT NOT NULL,
    name            TEXT,
    phone           TEXT,
    password_hash   TEXT,
    -- notification preferences
    notify_new_bid     BOOLEAN DEFAULT TRUE,
    notify_bid_reply   BOOLEAN DEFAULT FALSE,
    -- invite + reset flow
    invite_token    TEXT,
    invite_sent_at  TIMESTAMPTZ,
    invite_used_at  TIMESTAMPTZ,
    reset_token     TEXT,
    reset_sent_at   TIMESTAMPTZ,
    -- presence
    last_login_at   TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,
    last_ip         TEXT,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_owner_email ON owner_users (LOWER(email));
CREATE INDEX IF NOT EXISTS idx_owner_invite_token ON owner_users (invite_token)
    WHERE invite_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_owner_reset_token ON owner_users (reset_token)
    WHERE reset_token IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON owner_users TO expuser;
GRANT USAGE, SELECT ON SEQUENCE owner_users_id_seq TO expuser;
