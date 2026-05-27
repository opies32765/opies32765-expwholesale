#!/bin/bash
# Validation test for porsche_arb v2 (PORSCHE_ARB_OPTIONS_2026_05_26).
# Run from C1: bash /opt/expwholesale/ops/scripts/test_porsche_arb_v2.sh
set -u
export PGPASSWORD='ExpWholesale2026!'

echo "=== Coverage stats ==="
psql -U expuser -d expwholesale -p 5433 -h localhost <<'EOF'
SELECT
  count(*) FILTER (WHERE flagged) AS flagged,
  count(*) FILTER (WHERE flagged AND arb_score_v2 IS NOT NULL) AS v2_scored,
  count(*) FILTER (WHERE flagged AND 'thin_like_pool'=ANY(flag_reasons)) AS thin_pool
FROM porsche_arb_candidates WHERE snapshot_date=CURRENT_DATE;
EOF

echo ""
echo "=== Options enrichment by source ==="
psql -U expuser -d expwholesale -p 5433 -h localhost -c "SELECT source, count(*) FROM porsche_arb_options GROUP BY source ORDER BY count(*) DESC"

echo ""
echo "=== Top 5 by v2 score ==="
psql -U expuser -d expwholesale -p 5433 -h localhost <<'EOF'
SELECT id, subject_year, subject_model, subject_trim,
  asking_price::int AS asking, net_spread::int AS v1_spread,
  like_filtered_net_spread::int AS v2_spread,
  arb_score AS v1, arb_score_v2 AS v2,
  option_jaccard_to_anchor AS jacc
FROM porsche_arb_candidates
WHERE snapshot_date=CURRENT_DATE AND flagged
ORDER BY arb_score_v2 DESC NULLS LAST LIMIT 5;
EOF

echo ""
echo "=== Biggest v1->v2 drops (option-mix noise filtered out) ==="
psql -U expuser -d expwholesale -p 5433 -h localhost <<'EOF'
SELECT id, subject_year, subject_model, subject_trim,
  asking_price::int AS asking, net_spread::int AS v1_spread,
  like_filtered_net_spread::int AS v2_spread,
  arb_score AS v1, arb_score_v2 AS v2,
  option_jaccard_to_anchor AS jacc, anchor_trim
FROM porsche_arb_candidates
WHERE snapshot_date=CURRENT_DATE AND flagged AND arb_score_v2 IS NOT NULL
  AND arb_score - arb_score_v2 >= 30
ORDER BY (arb_score - arb_score_v2) DESC LIMIT 5;
EOF
