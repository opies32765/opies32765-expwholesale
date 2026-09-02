-- DEALER_SUBMIT_ALERT_2026_09_02 — Sam on Performance Luxury Sport.
-- match_phone is the member's submitting number and is the predicate that
-- actually works; supplier_id/name_ilike are bonus ORs.
INSERT INTO dealer_submit_alerts
    (label, member_id, supplier_id, name_ilike, match_phone, notify_phone, notify_name, active)
SELECT 'Performance Luxury Sport', m.id, 668675, '%performance luxury sport%',
       regexp_replace(m.contact_phone,'[^0-9]','','g'), '2395954021', 'Sam B', true
  FROM dealerprice_members m
 WHERE m.application_id = 39
   AND NOT EXISTS (SELECT 1 FROM dealer_submit_alerts a WHERE a.member_id = m.id);
SELECT id, label, member_id, supplier_id, match_phone, notify_phone, notify_name, active
  FROM dealer_submit_alerts;
