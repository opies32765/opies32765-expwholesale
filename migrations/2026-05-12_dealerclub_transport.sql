-- 2026-05-12 (later): real transport-quote data from DealerClub.
-- Scraper fetches POST /transportation/quote/ per lot on first-seen and
-- sends the price/mileage/ETA along with the lot payload. Replaces the
-- $700 flat transport estimate in opportunity scoring.

ALTER TABLE dealerclub_lots
    ADD COLUMN IF NOT EXISTS transport_mileage   INTEGER,
    ADD COLUMN IF NOT EXISTS transport_eta_min   INTEGER,
    ADD COLUMN IF NOT EXISTS transport_eta_max   INTEGER,
    ADD COLUMN IF NOT EXISTS transport_enclosed  BOOLEAN;
