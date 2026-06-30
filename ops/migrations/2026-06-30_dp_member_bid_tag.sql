-- DEALERPRICE_NETWORK member->bid tag (2026-06-30)
ALTER TABLE bids ADD COLUMN IF NOT EXISTS dp_member_id INTEGER;
CREATE INDEX IF NOT EXISTS bids_dp_member_idx ON bids (dp_member_id) WHERE dp_member_id IS NOT NULL;
