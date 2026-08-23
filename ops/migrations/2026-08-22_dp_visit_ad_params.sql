-- AD_ATTRIBUTION_2026_08_22 (visits)
-- The visit beacon only ever stored location.pathname, so an ad click was
-- indistinguishable from a direct visit. Store the ad parameters too, so
-- /network/visitors can show paid arrivals as they land -- not only the ones
-- that go on to apply. Additive and nullable.
ALTER TABLE dp_site_visits
  ADD COLUMN IF NOT EXISTS query        text,
  ADD COLUMN IF NOT EXISTS gclid        text,
  ADD COLUMN IF NOT EXISTS utm_source   text,
  ADD COLUMN IF NOT EXISTS utm_medium   text,
  ADD COLUMN IF NOT EXISTS utm_campaign text,
  ADD COLUMN IF NOT EXISTS utm_term     text;

CREATE INDEX IF NOT EXISTS dp_site_visits_gclid
  ON dp_site_visits (visited_at DESC) WHERE gclid IS NOT NULL;
