-- 2026-07-29_dp_intent_autoscan.sql
-- DP_INTENT_2026_07_29 — Joe's ask: capture WHAT a dealer wants from EW at
-- application time, so intent can be categorised and (for buyers) can kick off
-- the existing dealer-scanner onboarding automatically after vetting.
--
-- intent values: 'sell' | 'buy' | 'both'   (NULL = applied before this shipped)
--   sell  — submit & sell cars to EW  (no scanning)
--   buy   — wants inventory FROM EW   (scan their lot if they have a website)
--   both  — one relationship for everything
--
-- website is deliberately NOT required: many applicants are wholesalers with no
-- website at all. A buyer with no site is a perfectly good dealer — we just skip
-- the scanner registration and flag it on the packet.

ALTER TABLE dealer_applications ADD COLUMN IF NOT EXISTS intent TEXT;

-- Set once the approve step registers them into dealers(); lets the packet show
-- "already in the scanner as dealer #N" and stops a re-approve double-registering.
ALTER TABLE dealer_applications ADD COLUMN IF NOT EXISTS scanner_dealer_id INTEGER;
ALTER TABLE dealer_applications ADD COLUMN IF NOT EXISTS scanner_registered_at TIMESTAMPTZ;
ALTER TABLE dealer_applications ADD COLUMN IF NOT EXISTS scanner_note TEXT;

CREATE INDEX IF NOT EXISTS dealer_applications_intent_idx ON dealer_applications (intent);

-- Same three on the member row, so a bid can resolve straight to the scanner
-- dealer without walking back through the application.
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS intent TEXT;
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS scanner_dealer_id INTEGER;

-- Phone lookup for the bid dashboard: a vetted dealer who TEXTS a car in has no
-- member token on the bid, so we match on the last 10 digits of their mobile.
CREATE INDEX IF NOT EXISTS dealerprice_members_phone10_idx
  ON dealerprice_members (right(regexp_replace(contact_phone, '[^0-9]', '', 'g'), 10));
