-- 2026-05-05 — Partner Portal v2 onboarding additions
-- Idempotent. Captures every dealer/partner schema change made between
-- 2026-04-23 (v1) and 2026-05-05. Run on any fresh restore so onboarding
-- a new dealer through /dealers/<id> works without manual SQL.

-- ── dealers — per-dealer config required to provision the partner portal
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS portal_slug         TEXT;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS salesperson         TEXT;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS salesperson_set_at  TIMESTAMPTZ;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS salesperson_phone   TEXT;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS dashboard_token     TEXT;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS mobile_token        TEXT;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS brand               JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dealers_portal_slug
    ON dealers(portal_slug) WHERE portal_slug IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dealers_dashboard_token
    ON dealers(dashboard_token) WHERE dashboard_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dealers_mobile_token
    ON dealers(mobile_token) WHERE mobile_token IS NOT NULL;

-- ── partner_users — added SMS opt-in + email-bid-alert toggles
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS phone              VARCHAR(20);
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_opt_in         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verify_code    VARCHAR(8);
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verify_expires TIMESTAMPTZ;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS sms_verified_at    TIMESTAMPTZ;
ALTER TABLE partner_users ADD COLUMN IF NOT EXISTS email_bid_alerts   BOOLEAN NOT NULL DEFAULT TRUE;

-- ── partner_bid_requests — relax NOT NULL constraints so Quick Drop
--    (no inventory_id) and anonymous mobile-token submissions
--    (no partner_user_id) can write.
ALTER TABLE partner_bid_requests ALTER COLUMN inventory_id    DROP NOT NULL;
ALTER TABLE partner_bid_requests ALTER COLUMN partner_user_id DROP NOT NULL;

-- ── bids — partner-dealer routing + salesperson snapshot + per-rep filter
ALTER TABLE bids ADD COLUMN IF NOT EXISTS partner_dealer_id INTEGER
    REFERENCES dealers(id) ON DELETE SET NULL;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS salesperson    TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS field_rep_name TEXT;
CREATE INDEX IF NOT EXISTS idx_bids_partner_dealer ON bids(partner_dealer_id)
    WHERE partner_dealer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bids_field_rep
    ON bids(LOWER(field_rep_name)) WHERE field_rep_name IS NOT NULL;

-- Backfill: where bids.phone follows the legacy 'field:<rep>' pattern,
-- materialize the rep name into the new field_rep_name column. Idempotent.
UPDATE bids
   SET field_rep_name = REPLACE(phone, 'field:', '')
 WHERE phone LIKE 'field:%' AND field_rep_name IS NULL;

-- ── dealer_stats view — surface salesperson + tokens to the dealer
--    detail / dealers list pages so the UI can render chips + links
--    without an extra query.
DROP VIEW IF EXISTS dealer_stats;
CREATE VIEW dealer_stats AS
SELECT
    d.id                                                     AS dealer_id,
    d.name, d.url, d.platform, d.scrape_method, d.active,
    d.last_scan_at, d.last_scan_status,
    COUNT(*) FILTER (WHERE i.status = 'active')              AS in_stock,
    COUNT(*) FILTER (WHERE i.status = 'sold')                AS sold_total,
    COUNT(*) FILTER (
        WHERE i.status = 'active'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) > NOW() - INTERVAL '30 days'
    )                                                        AS age_under_30,
    COUNT(*) FILTER (
        WHERE i.status = 'active'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '30 days'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) >  NOW() - INTERVAL '60 days'
    )                                                        AS age_30_60,
    COUNT(*) FILTER (
        WHERE i.status = 'active'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '60 days'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) >  NOW() - INTERVAL '90 days'
    )                                                        AS age_60_90,
    COUNT(*) FILTER (
        WHERE i.status = 'active'
          AND COALESCE(i.verified_at - ((i.verified_days_on_lot || ' days')::interval),
                       i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '90 days'
    )                                                        AS age_over_90,
    COUNT(*) FILTER (
        WHERE i.status = 'sold'
          AND i.sold_at > COALESCE(d.last_scan_at, NOW()) - INTERVAL '7 days'
    )                                                        AS sold_last_7d,
    -- Partner-portal columns surfaced for the /dealers/<id> chip + the
    -- shareable-links card. Salesperson appears on every partner bid.
    d.salesperson, d.salesperson_set_at, d.portal_slug, d.salesperson_phone
FROM dealers d
LEFT JOIN dealer_inventory i ON i.dealer_id = d.id
GROUP BY d.id;

GRANT SELECT ON dealer_stats TO expuser;
