-- DP_OUTREACH_2026_07_06: DealerPrice launch outreach (dormant never-lost dealer list)
CREATE TABLE IF NOT EXISTS dp_outreach_targets(
  id serial PRIMARY KEY,
  name text NOT NULL,
  norm_name text UNIQUE,
  email text NOT NULL,
  phone text,
  src_deals int DEFAULT 0,
  buy_deals int DEFAULT 0,
  total_profit numeric,
  last_deal date,
  days_since int,
  history text,
  status text NOT NULL DEFAULT 'pending',
  sent_at timestamptz,
  error text,
  created_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dp_outreach_template(
  id int PRIMARY KEY,
  subject text,
  body text,
  updated_at timestamptz DEFAULT now()
);
GRANT ALL ON dp_outreach_targets, dp_outreach_template TO expuser;
GRANT USAGE, SELECT ON SEQUENCE dp_outreach_targets_id_seq TO expuser;
