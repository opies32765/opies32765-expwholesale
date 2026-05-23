-- ============================================================
-- 2026-05-22 — Buy Profile v3: colors at make AND model level
-- Supersedes v2 (already applied). Strictly additive vs v2.
-- New keys:
--   makes[MAKE].colors{ COLOR: {active_n, sold_n, avg_days_on_lot} }
--   makes[MAKE].models[MODEL].colors{ COLOR: {active_n, sold_n, avg_days_on_lot} }
-- Profile version bumped 2 -> 3.
-- ============================================================

CREATE OR REPLACE FUNCTION build_dealer_buy_profile(p_dealer_id INT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  v_sample  JSONB;
  v_bands   JSONB;
  v_makes   JSONB;
  v_profile JSONB;
BEGIN
  -- sample (unchanged)
  SELECT jsonb_build_object(
    'active_n',      COUNT(*) FILTER (WHERE status='active'),
    'history_n',     COUNT(*),
    'sold_n_180d',   COUNT(*) FILTER (WHERE status!='active'
                                     AND sold_at >= NOW() - INTERVAL '180 days'),
    'first_scan_at', MIN(first_seen_at)::date,
    'days_scanned',  GREATEST(EXTRACT(DAY FROM NOW() - MIN(first_seen_at))::int, 0)
  ) INTO v_sample
  FROM dealer_inventory WHERE dealer_id = p_dealer_id;

  -- bands (unchanged)
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
    SELECT UPPER(make)      AS make,
           UPPER(model)     AS model,
           UPPER(ext_color) AS color,
           year, price, mileage, status, sold_at, first_seen_at,
           EXP(-EXTRACT(EPOCH FROM (NOW() - first_seen_at)) / (86400 * 90.0)) AS w
      FROM dealer_inventory
     WHERE dealer_id = p_dealer_id AND make IS NOT NULL
  ),
  -- per-make + color rollup
  make_color_agg AS (
    SELECT make, color,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           ROUND(AVG(price) FILTER (WHERE status='active'))::int AS avg_price
      FROM weighted
     WHERE color IS NOT NULL AND color <> ''
     GROUP BY make, color
  ),
  make_color_obj AS (
    SELECT make,
           jsonb_object_agg(color, jsonb_build_object(
             'active_n',        active_n,
             'sold_n',          sold_n,
             'avg_days_on_lot', avg_days,
             'avg_price',       avg_price
           )) AS colors
      FROM make_color_agg
     GROUP BY make
  ),
  -- per-make+model+color rollup
  model_color_agg AS (
    SELECT make, model, color,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days
      FROM weighted
     WHERE model IS NOT NULL AND model <> ''
       AND color IS NOT NULL AND color <> ''
     GROUP BY make, model, color
  ),
  model_color_obj AS (
    SELECT make, model,
           jsonb_object_agg(color, jsonb_build_object(
             'active_n',        active_n,
             'sold_n',          sold_n,
             'avg_days_on_lot', avg_days
           )) AS colors
      FROM model_color_agg
     GROUP BY make, model
  ),
  -- per-make+model rollup (now with nested colors{})
  model_agg AS (
    SELECT make, model,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(price) FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted
     WHERE model IS NOT NULL AND model <> ''
     GROUP BY make, model
  ),
  model_obj AS (
    SELECT m.make,
           jsonb_object_agg(m.model, jsonb_build_object(
             'active_n',        m.active_n,
             'sold_n',          m.sold_n,
             'avg_price',       m.avg_price,
             'avg_days_on_lot', m.avg_days,
             'last_seen',       m.last_seen,
             'colors',          COALESCE(mc.colors, '{}'::jsonb)
           )) AS models
      FROM model_agg m
           LEFT JOIN model_color_obj mc ON mc.make = m.make AND mc.model = m.model
     GROUP BY m.make
  ),
  -- per-make rollup (now with active_n + colors{} + models{})
  agg AS (
    SELECT w.make,
           SUM(w.w) AS w_n,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           ROUND(AVG(year)    FILTER (WHERE status='active'))::int AS avg_year,
           ROUND(AVG(price)   FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(mileage) FILTER (WHERE status='active'))::int AS avg_miles,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted w GROUP BY w.make
  ),
  tot AS (SELECT SUM(w_n) AS t FROM agg)
  SELECT jsonb_object_agg(agg.make, jsonb_build_object(
           'share',           ROUND((agg.w_n / NULLIF(tot.t,0) * 100.0)::numeric, 1),
           'active_n',        agg.active_n,
           'avg_year',        agg.avg_year,
           'avg_price',       agg.avg_price,
           'avg_miles',       agg.avg_miles,
           'sold_n',          agg.sold_n,
           'avg_days_on_lot', agg.avg_days,
           'last_seen',       agg.last_seen,
           'models',          COALESCE(mo.models, '{}'::jsonb),
           'colors',          COALESCE(co.colors, '{}'::jsonb)
         )) INTO v_makes
    FROM agg
         LEFT JOIN model_obj      mo ON mo.make = agg.make
         LEFT JOIN make_color_obj co ON co.make = agg.make,
         tot;

  SELECT jsonb_build_object(
    'version',    3,
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

ALTER FUNCTION build_dealer_buy_profile(INT) OWNER TO expuser;
