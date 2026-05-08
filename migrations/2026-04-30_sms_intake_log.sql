-- 2026-04-30 — SMS intake observability
--
-- Captures every inbound Twilio webhook hit with the outcome and (when
-- the text didn't produce a new bid) the reason it was routed elsewhere.
-- Lets ops answer "why didn't this text trigger a bid?" without grepping
-- gunicorn logs.
--
-- Also: persist MMS photo bytes locally so dealer-uploaded images survive
-- Twilio media rotation / outages, and so we can flag SMS-intake photos
-- distinct from manual dealer uploads.

CREATE TABLE IF NOT EXISTS sms_intake_log (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    from_phone VARCHAR(20),
    body TEXT,
    num_media INTEGER DEFAULT 0,
    media_urls JSONB,           -- list of Twilio MediaUrl strings
    parsed_vin VARCHAR(17),     -- regex+AI extraction result (NULL if none)
    parsed_miles INTEGER,
    outcome VARCHAR(32),        -- new_bid|stitched|partner_reply|share_reply|ignored|error
    bid_id INTEGER REFERENCES bids(id) ON DELETE SET NULL,
    reason TEXT,                -- human-readable: "Routed to bid #547 because phone matches partner_user; body had no VIN and no media"
    raw_form JSONB              -- entire request.form payload for replay/debug
);

CREATE INDEX IF NOT EXISTS idx_sms_intake_created  ON sms_intake_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_intake_phone    ON sms_intake_log(from_phone);
CREATE INDEX IF NOT EXISTS idx_sms_intake_bid      ON sms_intake_log(bid_id);
CREATE INDEX IF NOT EXISTS idx_sms_intake_outcome  ON sms_intake_log(outcome);

-- Persist MMS photo bytes locally on ingest. Templates prefer local_path
-- over the (auth-gated, rotation-prone) Twilio MediaUrl when present.
ALTER TABLE bid_photos ADD COLUMN IF NOT EXISTS local_path TEXT;
ALTER TABLE bid_photos ADD COLUMN IF NOT EXISTS is_sms_intake BOOLEAN DEFAULT FALSE;

-- Grant access to the application user. (Added 2026-04-30 after the original
-- migration shipped without grants, causing /webhook/twilio 500s.)
GRANT SELECT, INSERT, UPDATE, DELETE ON sms_intake_log TO expuser;
GRANT USAGE, SELECT ON SEQUENCE sms_intake_log_id_seq TO expuser;
