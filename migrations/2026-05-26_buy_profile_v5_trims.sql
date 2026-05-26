-- ============================================================
-- 2026-05-26 — Buy Profile v5: trim-level rollup keyed by ymmt_catalog
--
-- Joins dealer_inventory to ymmt_catalog via ymmt_id and aggregates by the
-- catalog's canonical (model, trim) strings. Rows with ymmt_id IS NULL are
-- EXCLUDED per operator decision 2026-05-26 ("if we don't know the trim,
-- don't count the vehicle").
--
-- New shape (strictly additive on v4):
--   makes[MAKE].models[MODEL].trims[TRIM] = {
--       active_n, sold_n, avg_price, avg_days_on_lot,
--       last_seen, colors{ COLOR: {active_n, sold_n, avg_days_on_lot} }
--   }
--
-- Model-level keys (active_n, sold_n, colors{}, etc.) also derive from the
-- catalog's model string now — so dealers whose inventory is fully resolved
-- get cleaner model keys (no more "G" vs "G-CLASS" vs "G550" splits).
--
-- Version bumped 4 -> 5. Behavioral + overrides preserved.
--
-- WARNING: First post-deploy run of rebuild_all_buy_profiles() will produce
-- THINNER profiles than v4 for any dealer whose inventory isn't fully
-- ymmt-tagged. That's intentional — unknown trims silently inflated v4.
--
-- Apply on C1 only:
--   ssh root@62.146.226.100 'sudo -u postgres psql -p 5433 -d expwholesale \
--     -f /opt/expwholesale/migrations/2026-05-26_buy_profile_v5_trims.sql'
-- Then run: SELECT rebuild_all_buy_profiles();
-- ============================================================

CREATE OR REPLACE FUNCTION build_dealer_buy_profile(p_dealer_id INT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  v_sample  JSONB;
  v_bands   JSONB;
  v_makes   JSONB;
  v_caps    JSONB;
  v_profile JSONB;
BEGIN
  -- sample (unchanged from v4; counts BOTH resolved and unresolved rows so
  -- the operator can see whether inventory health is degrading)
  SELECT jsonb_build_object(
    'active_n',      COUNT(*) FILTER (WHERE status='active'),
    'history_n',     COUNT(*),
    'sold_n_180d',   COUNT(*) FILTER (WHERE status!='active'
                                     AND sold_at >= NOW() - INTERVAL '180 days'),
    'first_scan_at', MIN(first_seen_at)::date,
    'days_scanned',  GREATEST(EXTRACT(DAY FROM NOW() - MIN(first_seen_at))::int, 0),
    'ymmt_resolved_n',     COUNT(*) FILTER (WHERE ymmt_id IS NOT NULL),
    'ymmt_unresolved_n',   COUNT(*) FILTER (WHERE ymmt_id IS NULL)
  ) INTO v_sample
  FROM dealer_inventory WHERE dealer_id = p_dealer_id;

  -- bands (unchanged; raw price/year/miles still meaningful even for unresolved rows)
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

  -- ── Build the makes/models/trims rollup ──
  -- All aggregations restricted to ymmt_id IS NOT NULL.
  WITH weighted AS (
    SELECT
      UPPER(c.make)        AS make,
      c.model              AS model,
      c.trim               AS trim,
      UPPER(di.ext_color)  AS color,
      di.year, di.price, di.mileage, di.status, di.sold_at, di.first_seen_at,
      EXP(-EXTRACT(EPOCH FROM (NOW() - di.first_seen_at)) / (86400 * 90.0)) AS w
    FROM dealer_inventory di
    JOIN ymmt_catalog c ON c.id = di.ymmt_id
    WHERE di.dealer_id = p_dealer_id
  ),
  -- per-make+color
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
             'active_n', active_n, 'sold_n', sold_n,
             'avg_days_on_lot', avg_days, 'avg_price', avg_price)) AS colors
      FROM make_color_agg GROUP BY make
  ),
  -- per-make+model+trim+color (NEW for v5)
  trim_color_agg AS (
    SELECT make, model, trim, color,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days
      FROM weighted
     WHERE color IS NOT NULL AND color <> ''
     GROUP BY make, model, trim, color
  ),
  trim_color_obj AS (
    SELECT make, model, trim,
           jsonb_object_agg(color, jsonb_build_object(
             'active_n', active_n, 'sold_n', sold_n, 'avg_days_on_lot', avg_days)) AS colors
      FROM trim_color_agg GROUP BY make, model, trim
  ),
  -- per-make+model+trim (NEW for v5 — the trims level)
  trim_agg AS (
    SELECT make, model, trim,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(price) FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted
     GROUP BY make, model, trim
  ),
  trim_obj AS (
    SELECT t.make, t.model,
           jsonb_object_agg(t.trim, jsonb_build_object(
             'active_n',        t.active_n,
             'sold_n',          t.sold_n,
             'avg_price',       t.avg_price,
             'avg_days_on_lot', t.avg_days,
             'last_seen',       t.last_seen,
             'colors',          COALESCE(tc.colors, '{}'::jsonb)
           )) AS trims
      FROM trim_agg t
           LEFT JOIN trim_color_obj tc
             ON tc.make=t.make AND tc.model=t.model AND tc.trim=t.trim
     GROUP BY t.make, t.model
  ),
  -- per-make+model (model rollup over all trims in this model line)
  model_agg AS (
    SELECT make, model,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(price) FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted
     GROUP BY make, model
  ),
  model_color_agg AS (
    SELECT make, model, color,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days
      FROM weighted
     WHERE color IS NOT NULL AND color <> ''
     GROUP BY make, model, color
  ),
  model_color_obj AS (
    SELECT make, model,
           jsonb_object_agg(color, jsonb_build_object(
             'active_n', active_n, 'sold_n', sold_n, 'avg_days_on_lot', avg_days)) AS colors
      FROM model_color_agg GROUP BY make, model
  ),
  model_obj AS (
    SELECT m.make,
           jsonb_object_agg(m.model, jsonb_build_object(
             'active_n', m.active_n, 'sold_n', m.sold_n,
             'avg_price', m.avg_price, 'avg_days_on_lot', m.avg_days,
             'last_seen', m.last_seen,
             'colors', COALESCE(mc.colors, '{}'::jsonb),
             'trims',  COALESCE(to_.trims,  '{}'::jsonb)
           )) AS models
      FROM model_agg m
           LEFT JOIN model_color_obj mc ON mc.make=m.make AND mc.model=m.model
           LEFT JOIN trim_obj         to_ ON to_.make=m.make AND to_.model=m.model
     GROUP BY m.make
  ),
  -- per-make rollup
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
  SELECT
    jsonb_object_agg(agg.make, jsonb_build_object(
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

  -- learned_caps (unchanged v4 logic, but now over ymmt-resolved rows only)
  SELECT COALESCE(jsonb_object_agg(tier, observed_peak), '{}'::jsonb)
    INTO v_caps
    FROM (
      SELECT
        CASE
          WHEN avg_price >= 250000 THEN 'tier_250k_plus'
          WHEN avg_price >= 150000 THEN 'tier_150k_250k'
          WHEN avg_price >=  80000 THEN 'tier_80k_150k'
          WHEN avg_price >=  40000 THEN 'tier_40k_80k'
          WHEN avg_price IS NOT NULL THEN 'tier_lt_40k'
          ELSE NULL
        END AS tier,
        MAX(active_n) AS observed_peak
      FROM (
        SELECT c.model AS model,
               ROUND(AVG(di.price) FILTER (WHERE di.status='active'))::int AS avg_price,
               COUNT(*) FILTER (WHERE di.status='active') AS active_n
          FROM dealer_inventory di
          JOIN ymmt_catalog c ON c.id = di.ymmt_id
         WHERE di.dealer_id = p_dealer_id
         GROUP BY c.model
      ) ma
      WHERE avg_price IS NOT NULL
      GROUP BY 1
    ) t
    WHERE tier IS NOT NULL;

  SELECT jsonb_build_object(
    'version',      5,
    'built_at',     NOW(),
    'sample',       COALESCE(v_sample, '{}'::jsonb),
    'bands',        COALESCE(v_bands,  '{}'::jsonb),
    'makes',        COALESCE(v_makes,  '{}'::jsonb),
    'learned_caps', COALESCE(v_caps,   '{}'::jsonb),
    'behavioral',   COALESCE(buy_profile->'behavioral', '{}'::jsonb),
    'overrides',    COALESCE(buy_profile->'overrides',  '{}'::jsonb)
  ) INTO v_profile
  FROM dealers WHERE id = p_dealer_id;

  UPDATE dealers
     SET buy_profile          = v_profile,
         buy_profile_built_at = NOW()
   WHERE id = p_dealer_id;

  RETURN v_profile;
END $$;

ALTER FUNCTION build_dealer_buy_profile(INT) OWNER TO expuser;

-- rebuild_all_buy_profiles() and record_push_outcome() are unchanged.
-- DO NOT auto-trigger rebuild here — let the operator decide when to
-- rebuild (or wait for nightly 08:30 cron).
