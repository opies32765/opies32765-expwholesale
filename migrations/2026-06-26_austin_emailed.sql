-- 2026-06-26: track when Austin (transport guy) was emailed to arrange transport.
-- One-shot, hand-run (HR8: no boot DDL). Idempotent.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS austin_emailed_at timestamptz;

-- Backfill existing units from the email outbox (every manual Austin email is logged there).
UPDATE recon_units u
   SET austin_emailed_at = sub.ts
  FROM (SELECT unit_id, MAX(created_at) AS ts
          FROM recon_email_outbox
         WHERE kind = 'austin_manual'
         GROUP BY unit_id) sub
 WHERE sub.unit_id = u.id
   AND u.austin_emailed_at IS NULL;
