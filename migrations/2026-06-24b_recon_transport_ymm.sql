-- RECON_TRANSPORT_YMM_2026_06_24 — add the display YMM columns to recon_transport
-- so the In-Transit lane can show "2022 Bentley Bentayga" for every sheet row
-- (ours and not-ours alike). Run once, by hand, C1. Idempotent. Cold table.
BEGIN;
ALTER TABLE recon_transport ADD COLUMN IF NOT EXISTS year  TEXT;
ALTER TABLE recon_transport ADD COLUMN IF NOT EXISTS make  TEXT;
ALTER TABLE recon_transport ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE recon_transport ADD COLUMN IF NOT EXISTS ymm   TEXT;
COMMIT;
