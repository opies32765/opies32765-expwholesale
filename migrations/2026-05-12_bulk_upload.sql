-- 2026-05-12: bulk-upload bid intake (xlsx/csv from dealer "needs to go" lists)
--
-- One bulk_uploads row per file; each created bid links back via
-- bids.bulk_upload_id so the operator can see "these 7 bids came from one
-- sheet" on a future admin recap page.

CREATE TABLE IF NOT EXISTS bulk_uploads (
    id           SERIAL PRIMARY KEY,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uploaded_by  TEXT,
    contact_id   INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    source_name  TEXT,
    filename     TEXT,
    row_count    INTEGER DEFAULT 0,
    created_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    delay_seconds INTEGER DEFAULT 5,
    notes        TEXT
);

ALTER TABLE bids
    ADD COLUMN IF NOT EXISTS bulk_upload_id INTEGER
        REFERENCES bulk_uploads(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_bids_bulk_upload_id
    ON bids(bulk_upload_id) WHERE bulk_upload_id IS NOT NULL;
