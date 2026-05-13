-- ============================================================
-- 2026-05-12 — Dealer Buy Profile
-- Nightly-rebuilt JSONB derived from dealer_inventory daily scan.
-- Drives match-routing in the broker-ask flow.
-- ============================================================

ALTER TABLE dealers
  ADD COLUMN IF NOT EXISTS buy_profile          JSONB,
  ADD COLUMN IF NOT EXISTS buy_profile_built_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_dealers_buy_profile_built_at
  ON dealers(buy_profile_built_at);

-- ============================================================
-- build_dealer_buy_profile(dealer_id) — recompute one dealer's profile
-- ============================================================
CREATE OR REPLACE FUNCTION build_dealer_buy_profile(p_dealer_id INT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  v_sample  JSONB;
  v_bands   JSONB;
  v_makes   JSONB;
  v_profile JSONB;
BEGIN
  SELECT jsonb_build_object(
    'active_n',      COUNT(*) FILTER (WHERE status='active'),
    'history_n',     COUNT(*),
    'sold_n_180d',   COUNT(*) FILTER (WHERE status!='active'
                                     AND sold_at >= NOW() - INTERVAL '180 days'),
    'first_scan_at', MIN(first_seen_at)::date,
    'days_scanned',  GREATEST(EXTRACT(DAY FROM NOW() - MIN(first_seen_at))::int, 0)
  ) INTO v_sample
  FROM dealer_inventory WHERE dealer_id = p_dealer_id;

  SELECT jsonb_build_object(
    'year_p10',  percentile_disc(0.10) WITHIN GROUP (ORDER BY year),
    'year_p90',  percentile_disc(0.90) WITHIN GROUP (ORDER BY year),
    'year_min',  MIN(year), 'year_max', MAX(year),
    'price_p10', percentile_disc(0.10) WITHIN GROUP (ORDER BY price),
    'price_p90', percentile_disc(0.90) WITHIN GROUP (ORDER BY price),
    'miles_p10', percentile_disc(0.10) WITHIN GROUP (ORDER BY mileage),
    'miles_p90', percentile_disc(0.90) WITHIN GROUP (ORDER BY mileage)
  ) INTO v_bands
  FROM dealer_inventory
  WHERE dealer_id = p_dealer_id AND status='active' AND price > 0;

  WITH weighted AS (
    SELECT UPPER(make) AS make, year, price, mileage, status, sold_at, first_seen_at,
           EXP(-EXTRACT(EPOCH FROM (NOW() - first_seen_at)) / (86400 * 90.0)) AS w
      FROM dealer_inventory
     WHERE dealer_id = p_dealer_id AND make IS NOT NULL
  ),
  agg AS (
    SELECT make,
           SUM(w) AS w_n,
           ROUND(AVG(year)    FILTER (WHERE status='active'))::int AS avg_year,
           ROUND(AVG(price)   FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(mileage) FILTER (WHERE status='active'))::int AS avg_miles,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted GROUP BY make
  ),
  tot AS (SELECT SUM(w_n) AS t FROM agg)
  SELECT jsonb_object_agg(make, jsonb_build_object(
           'share',           ROUND((w_n / NULLIF(tot.t,0) * 100.0)::numeric, 1),
           'avg_year',        avg_year,
           'avg_price',       avg_price,
           'avg_miles',       avg_miles,
           'sold_n',          sold_n,
           'avg_days_on_lot', avg_days,
           'last_seen',       last_seen
         )) INTO v_makes
    FROM agg, tot;

  SELECT jsonb_build_object(
    'version',    1,
    'built_at',   NOW(),
    'sample',     COALESCE(v_sample, '{}'::jsonb),
    'bands',      COALESCE(v_bands,  '{}'::jsonb),
    'makes',      COALESCE(v_makes,  '{}'::jsonb),
    'behavioral', COALESCE(buy_profile->'behavioral', '{}'::jsonb),
    'overrides',  COALESCE(buy_profile->'overrides',  '{}'::jsonb)
  ) INTO v_profile
  FROM dealers WHERE id = p_dealer_id;

  UPDATE dealers
     SET buy_profile          = v_profile,
         buy_profile_built_at = NOW()
   WHERE id = p_dealer_id;

  RETURN v_profile;
END $$;

-- ============================================================
-- rebuild_all_buy_profiles() — nightly cron entrypoint
-- ============================================================
CREATE OR REPLACE FUNCTION rebuild_all_buy_profiles() RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE r RECORD; n INT := 0;
BEGIN
  FOR r IN SELECT id FROM dealers WHERE portal_slug IS NOT NULL ORDER BY id LOOP
    PERFORM build_dealer_buy_profile(r.id);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- ============================================================
-- record_push_outcome — real-time behavioral updater
-- Called on push fire, view, accept, pass.
-- ============================================================
CREATE OR REPLACE FUNCTION record_push_outcome(
  p_dealer_id INT, p_bid_id INT, p_outcome TEXT
) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE v_make TEXT; v_b JSONB; v_by_make JSONB; v_pair JSONB;
BEGIN
  SELECT UPPER(make) INTO v_make FROM bids WHERE id = p_bid_id;
  IF v_make IS NULL THEN RETURN; END IF;

  v_b := COALESCE((SELECT buy_profile->'behavioral' FROM dealers
                    WHERE id = p_dealer_id), '{}'::jsonb);

  v_b := jsonb_set(v_b, '{pushes}',
                   to_jsonb(COALESCE((v_b->>'pushes')::int, 0) + 1));
  IF p_outcome IN ('opened','accepted') THEN
    v_b := jsonb_set(v_b, '{opened}',
                     to_jsonb(COALESCE((v_b->>'opened')::int, 0) + 1));
  END IF;
  IF p_outcome = 'accepted' THEN
    v_b := jsonb_set(v_b, '{accepted}',
                     to_jsonb(COALESCE((v_b->>'accepted')::int, 0) + 1));
  END IF;

  v_by_make := COALESCE(v_b->'by_make', '{}'::jsonb);
  v_pair    := COALESCE(v_by_make->v_make,
                        jsonb_build_object('pushed',0,'accepted',0));
  v_pair := jsonb_set(v_pair, '{pushed}',
                      to_jsonb(COALESCE((v_pair->>'pushed')::int,0) + 1));
  IF p_outcome = 'accepted' THEN
    v_pair := jsonb_set(v_pair, '{accepted}',
                        to_jsonb(COALESCE((v_pair->>'accepted')::int,0) + 1));
  END IF;
  v_b := jsonb_set(v_b, '{by_make}', jsonb_set(v_by_make, ARRAY[v_make], v_pair));

  UPDATE dealers
     SET buy_profile = jsonb_set(COALESCE(buy_profile,'{}'::jsonb),
                                 '{behavioral}', v_b)
   WHERE id = p_dealer_id;
END $$;

-- ============================================================
-- Ownership / grants (mirrors 2026-05-11 migration pattern)
-- ============================================================
ALTER FUNCTION build_dealer_buy_profile(INT)        OWNER TO expuser;
ALTER FUNCTION rebuild_all_buy_profiles()           OWNER TO expuser;
ALTER FUNCTION record_push_outcome(INT, INT, TEXT)  OWNER TO expuser;

-- ============================================================
-- Initial backfill — populate all 14 partners now
-- ============================================================
SELECT rebuild_all_buy_profiles() AS profiles_built;
