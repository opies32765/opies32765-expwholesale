BEGIN;
CREATE TABLE IF NOT EXISTS bid_alerts (
    id              SERIAL PRIMARY KEY,
    created_by      TEXT,
    notify_phone    TEXT        NOT NULL,
    phone_digits    VARCHAR(10) NOT NULL,
    make            TEXT,
    model           TEXT,
    trim_contains   TEXT,
    year_min        INTEGER,
    year_max        INTEGER,
    price_max       INTEGER,
    label           TEXT,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    match_count     INTEGER     NOT NULL DEFAULT 0,
    last_matched_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bid_alerts_active ON bid_alerts(active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_bid_alerts_created_by ON bid_alerts(LOWER(created_by));
CREATE TABLE IF NOT EXISTS bid_alert_hits (
    id           SERIAL PRIMARY KEY,
    alert_id     INTEGER NOT NULL REFERENCES bid_alerts(id) ON DELETE CASCADE,
    bid_id       INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    matched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at  TIMESTAMPTZ,
    notify_via   VARCHAR(16) DEFAULT 'sms',
    message      TEXT,
    skip_reason  TEXT,
    UNIQUE (alert_id, bid_id)
);
CREATE INDEX IF NOT EXISTS idx_bid_alert_hits_alert ON bid_alert_hits(alert_id);
CREATE INDEX IF NOT EXISTS idx_bid_alert_hits_bid ON bid_alert_hits(bid_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON bid_alerts     TO expuser;
GRANT SELECT, INSERT, UPDATE, DELETE ON bid_alert_hits TO expuser;
GRANT USAGE, SELECT ON SEQUENCE bid_alerts_id_seq      TO expuser;
GRANT USAGE, SELECT ON SEQUENCE bid_alert_hits_id_seq  TO expuser;
COMMIT;
