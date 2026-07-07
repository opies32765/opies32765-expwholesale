-- 2026-07-07  EW Recon: add "Dealer → Buyer Picking Up" staging lane (FIRST staging tab).
-- Bought from a dealer, buyer collects it directly (no EW transport). Manual lane,
-- mirrors the other 4 staging routes. sort_order 9 => first in staging (New=5, d2d=10).
-- Idempotent: safe to re-run.
INSERT INTO recon_step_defs
    (code, name, sort_order, is_parallel, is_gate, is_terminal, is_pauses_sla, store_id, active, created_at, updated_at)
VALUES
    ('dealer_to_buyer', 'Dealer → Buyer Picking Up', 9, false, false, false, false, 1, true, now(), now())
ON CONFLICT (store_id, code) DO UPDATE
    SET name = EXCLUDED.name,
        sort_order = EXCLUDED.sort_order,
        active = true,
        updated_at = now();
