-- DP_FOLLOWUP_REMOVE_APPLICANTS_2026_09_02
-- Take the dealers who ALREADY APPLIED off the outreach list permanently, so
-- neither this follow-up nor any future campaign can re-pitch a customer.
-- Run ONLY after the operator confirms the 9 rows below.
BEGIN;
UPDATE dp_outreach_targets
   SET removed_at = now(),
       removed_by = 'dp_followup_2026_09',
       removed_reason = 'applied to DealerPrice (dealer_applications #'
                        || CASE id
                             WHEN  695 THEN '32'   -- Hatfield Auto Sales      exact email
                             WHEN  583 THEN '34'   -- Mercedes-benz Atl South  exact email
                             WHEN  506 THEN '35'   -- Autobuy                  exact email
                             WHEN 1256 THEN '31'   -- Scott Ales Inc           name match
                             WHEN  603 THEN '34'   -- Porsche Atlanta NE       jimellis.com
                             WHEN  818 THEN '34'   -- Genesis Of Atlanta       jimellis.com
                             WHEN  671 THEN '34'   -- Audi Atlanta             jimellis.com
                             WHEN  938 THEN '34'   -- Porsche Atlanta Perim    jimellis.com
                             WHEN  577 THEN '34'   -- Jim Ellis Ford Sandy Spr jimellis.com
                           END || ')'
 WHERE id IN (695, 583, 506, 1256, 603, 818, 671, 938, 577)
   AND removed_at IS NULL;
SELECT id, name, email, removed_reason FROM dp_outreach_targets
 WHERE id IN (695, 583, 506, 1256, 603, 818, 671, 938, 577) ORDER BY id;
COMMIT;
