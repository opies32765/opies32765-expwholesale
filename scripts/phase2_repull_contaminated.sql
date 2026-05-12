-- Phase 2: requeue contaminated AccuTrade bids for re-pull via overseer-aware workers.
-- Identified 2026-05-11 — pre-fix AccuTrade rows with values diverging significantly
-- from vAuto KBB/MMR or actual purchase cost (strong signal that wrong trim was clicked).

BEGIN;

-- Backup the rows we are about to delete (audit trail)
CREATE TABLE IF NOT EXISTS accutrade_lookups_phase2_backup AS
  SELECT a.*, NOW() AS backed_up_at FROM accutrade_lookups a WHERE FALSE;

INSERT INTO accutrade_lookups_phase2_backup
SELECT a.*, NOW() FROM accutrade_lookups a
WHERE a.bid_id IN (1133, 401, 350, 448, 1139, 1141, 1003);

-- Delete the lookups so /api/vauto/pending picks them up again
DELETE FROM accutrade_lookups WHERE bid_id IN (1133, 401, 350, 448, 1139, 1141, 1003);
DELETE FROM vauto_lookups     WHERE bid_id IN (1133, 401, 350, 448, 1139, 1141, 1003);
DELETE FROM ipacket_lookups   WHERE bid_id IN (1133, 401, 350, 448, 1139, 1141, 1003);

-- Reset claim so the bid is immediately eligible (not the 5-min cooldown)
UPDATE bids SET vauto_claimed_at = NULL, vauto_claimed_by = NULL
 WHERE id IN (1133, 401, 350, 448, 1139, 1141, 1003);

-- Reset worker_jobs counter so the auto-give-up gate doesn't fire
DELETE FROM worker_jobs WHERE bid_id IN (1133, 401, 350, 448, 1139, 1141, 1003);

COMMIT;

SELECT 'requeued:' AS status, b.id, b.vin, b.year, b.make, b.model,
       (SELECT COUNT(*) FROM accutrade_lookups_phase2_backup WHERE bid_id = b.id) AS backed_up
FROM bids b
WHERE b.id IN (1133, 401, 350, 448, 1139, 1141, 1003)
ORDER BY b.id;
