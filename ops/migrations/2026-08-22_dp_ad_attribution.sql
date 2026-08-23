-- AD_ATTRIBUTION_2026_08_22
-- Where a dealer application came from. Additive and idempotent: every column is
-- nullable and nothing existing is touched, so an application that arrives with
-- no attribution (which is most of them) behaves exactly as it did before.
--
-- source_channel is DERIVED on insert, not sent by the browser, so it cannot be
-- spoofed into 'google_ads' by anyone posting to the bridge.
ALTER TABLE dealer_applications
  ADD COLUMN IF NOT EXISTS source_channel  text,
  ADD COLUMN IF NOT EXISTS gclid           text,
  ADD COLUMN IF NOT EXISTS utm_source      text,
  ADD COLUMN IF NOT EXISTS utm_medium      text,
  ADD COLUMN IF NOT EXISTS utm_campaign    text,
  ADD COLUMN IF NOT EXISTS utm_content     text,
  ADD COLUMN IF NOT EXISTS utm_term        text,
  ADD COLUMN IF NOT EXISTS utm_matchtype   text,
  ADD COLUMN IF NOT EXISTS landing_page    text,
  ADD COLUMN IF NOT EXISTS ad_referrer     text,
  ADD COLUMN IF NOT EXISTS first_touch_at  timestamptz;

CREATE INDEX IF NOT EXISTS dealer_applications_source_channel_idx
  ON dealer_applications (source_channel, created_at DESC);

-- Carry the channel onto the member when an application is approved, so a
-- dealer's future bids stay traceable to the ad that produced them.
ALTER TABLE dealerprice_members
  ADD COLUMN IF NOT EXISTS source_channel text;
