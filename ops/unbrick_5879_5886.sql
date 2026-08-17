-- UNBRICK_2026_08_17: bids 5879-5886 (Rob) permanently wedged by the
-- AUTOGIVE 5-strike give-up counting our OWN infra releases
-- (released_watchdog x4 + released_reaper x1) during the 08-16 20:34-21:10
-- vAuto browser-leg outage. Reprocess can never work: the give-up re-stamps
-- __not_found__ on the very next /api/vauto/pending poll.
-- iPacket rows deliberately UNTOUCHED (never-retry hard rule).
BEGIN;
UPDATE worker_jobs SET status='released_admin_reprocess'
 WHERE bid_id BETWEEN 5879 AND 5886 AND job_type='vauto'
   AND status IN ('released_watchdog','released_reaper');
DELETE FROM vauto_lookups WHERE bid_id BETWEEN 5879 AND 5886
   AND appraisal_url='__not_found__' AND raw_json IS NULL;
UPDATE bids SET vauto_claimed_by=NULL, vauto_claimed_at=NULL,
       ai_assessed_at=NULL, ai_price=NULL, ai_assessment=NULL
 WHERE id BETWEEN 5879 AND 5886;
COMMIT;
