-- DP_OUTREACH_TRACKING_2026_07_30
-- Per-send tracking for the DealerPrice launch outreach.
--
-- One row per EMAIL SENT (not per dealership) — dp_outreach_targets is already
-- keyed by address because 1,022 dealerships collapse to 809 inboxes and one
-- address covers 35 stores. Sending per-dealership would put 35 near-identical
-- emails in one person's morning.
--
-- Tracking is SELF-HOSTED (pixel + wrapped links on our own domain) because the
-- Resend API key is send-only: it 403s on /domains, so open/click tracking and
-- webhooks cannot be configured programmatically. Bounces and complaints are the
-- one thing self-hosting cannot see — those arrive via the Resend webhook.

CREATE TABLE IF NOT EXISTS dp_outreach_email (
  id             serial PRIMARY KEY,
  target_id      int REFERENCES dp_outreach_targets(id) ON DELETE SET NULL,
  email          text NOT NULL,
  -- opaque per-send token; it is what the pixel and every wrapped link carry.
  -- Never the email address in the URL — that would leak the address into logs,
  -- referrers and anyone's browser history.
  token          text NOT NULL UNIQUE,
  subject        text,
  provider_id    text,                    -- Resend message id, for webhook match
  campaign       text NOT NULL DEFAULT 'launch',
  status         text NOT NULL DEFAULT 'queued',  -- queued|sent|delivered|bounced|complained|failed
  error          text,
  sent_at        timestamptz,
  delivered_at   timestamptz,
  -- opens are split on purpose. Apple Mail Privacy Protection PRE-FETCHES images
  -- for recipients who never opened anything, and Gmail proxies them. Reporting
  -- one blended number would overstate engagement, so machine fetches are counted
  -- separately and the UI leads with clicks.
  opens          int NOT NULL DEFAULT 0,
  proxy_opens    int NOT NULL DEFAULT 0,
  first_open_at  timestamptz,
  last_open_at   timestamptz,
  clicks         int NOT NULL DEFAULT 0,
  first_click_at timestamptz,
  last_click_at  timestamptz,
  bounced_at     timestamptz,
  bounce_type    text,                    -- hard|soft|blocked
  complained_at  timestamptz,
  unsubscribed_at timestamptz,
  -- did this contact actually apply? matched on address, and on the tagged link
  applied_at     timestamptz,
  application_id int,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dp_oe_email_idx    ON dp_outreach_email (lower(email));
CREATE INDEX IF NOT EXISTS dp_oe_status_idx   ON dp_outreach_email (status);
CREATE INDEX IF NOT EXISTS dp_oe_provider_idx ON dp_outreach_email (provider_id);

-- Every raw event, kept so a number on the dashboard can always be explained.
CREATE TABLE IF NOT EXISTS dp_outreach_event (
  id         bigserial PRIMARY KEY,
  email_id   int REFERENCES dp_outreach_email(id) ON DELETE CASCADE,
  kind       text NOT NULL,   -- open|proxy_open|click|delivered|bounce|complaint|unsubscribe
  ts         timestamptz NOT NULL DEFAULT now(),
  url        text,            -- for clicks: where they went
  ip         text,
  user_agent text,
  source     text             -- pixel|link|resend
);

CREATE INDEX IF NOT EXISTS dp_ev_email_idx ON dp_outreach_event (email_id, ts DESC);
CREATE INDEX IF NOT EXISTS dp_ev_kind_idx  ON dp_outreach_event (kind, ts DESC);

-- Suppression list. Survives target-list rebuilds on purpose: an unsubscribe or
-- a hard bounce must outlive whatever CSV produced the campaign.
CREATE TABLE IF NOT EXISTS dp_outreach_suppression (
  email      text PRIMARY KEY,
  reason     text NOT NULL,   -- unsubscribed|hard_bounce|complaint|manual
  note       text,
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT ALL ON dp_outreach_email, dp_outreach_event, dp_outreach_suppression TO expuser;
GRANT USAGE, SELECT ON SEQUENCE dp_outreach_email_id_seq  TO expuser;
GRANT USAGE, SELECT ON SEQUENCE dp_outreach_event_id_seq  TO expuser;
