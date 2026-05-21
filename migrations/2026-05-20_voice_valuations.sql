-- voice_valuations — single new table for the voice agent. No alters to
-- any existing table. Drop safely to roll back.
CREATE TABLE IF NOT EXISTS voice_valuations (
    id                BIGSERIAL PRIMARY KEY,
    short_id          TEXT UNIQUE NOT NULL,
    partner_user_id   INTEGER,                                -- logical FK to partner_users(id); no DB constraint (cross-owner)
    partner_dealer_id INTEGER,                                -- logical FK to dealers(id); no DB constraint (cross-owner)

    raw_transcript    TEXT,
    parsed_year       INTEGER,
    parsed_make       TEXT,
    parsed_model      TEXT,
    parsed_trim       TEXT,
    parsed_miles      INTEGER,
    parsed_msrp       INTEGER,
    parsed_notes      TEXT,

    target_buy        INTEGER,
    range_low         INTEGER,
    range_high        INTEGER,
    confidence        TEXT,
    reasoning         TEXT,
    comps_snapshot    JSONB,

    session_id        TEXT,
    turn_index        INTEGER NOT NULL DEFAULT 0,

    sms_sent_at       TIMESTAMPTZ,
    sms_sent_to       TEXT,
    bid_id            INTEGER,                                -- FK added in next migration when bids is confirmed in same schema

    llm_router        TEXT,
    llm_ms            INTEGER,
    llm_tokens_in     INTEGER,
    llm_tokens_out    INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_val_user    ON voice_valuations(partner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_val_session ON voice_valuations(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_voice_val_short   ON voice_valuations(short_id);
