-- Owners: split staging into From-Dealer / From-Individual × to-Dealer / to-Home (4 statuses).
BEGIN;
UPDATE recon_step_defs SET active=FALSE, updated_at=now() WHERE store_id=1 AND code IN ('d2d','d2h');
INSERT INTO recon_step_defs (code,name,sort_order,is_gate,is_terminal,active) VALUES
 ('dealer_to_dealer','Dealer to Dealer',10,FALSE,FALSE,TRUE),
 ('dealer_to_home','Dealer to Home',11,FALSE,FALSE,TRUE),
 ('indiv_to_dealer','Individual to Dealer',12,FALSE,FALSE,TRUE),
 ('indiv_to_home','Individual to Home',13,FALSE,FALSE,TRUE)
ON CONFLICT (store_id, code) DO UPDATE SET name=EXCLUDED.name, sort_order=EXCLUDED.sort_order,
  is_gate=EXCLUDED.is_gate, is_terminal=EXCLUDED.is_terminal, active=TRUE, updated_at=now();
-- remap any cars still on the old 2-way staging
UPDATE recon_units SET current_step_id=(SELECT id FROM recon_step_defs WHERE store_id=1 AND code='dealer_to_dealer'), updated_at=now()
 WHERE current_step_id IN (SELECT id FROM recon_step_defs WHERE store_id=1 AND code='d2d');
UPDATE recon_units SET current_step_id=(SELECT id FROM recon_step_defs WHERE store_id=1 AND code='dealer_to_home'), updated_at=now()
 WHERE current_step_id IN (SELECT id FROM recon_step_defs WHERE store_id=1 AND code='d2h');
COMMIT;
