-- SCORECARD_SUBMITTED_2026_07_31
-- Operator: the term is "submitted", not "set in".
-- Renamed in the schema too, not just the labels, so the column and the screen
-- never drift apart. dp_dealer_scorecard is a derived cache with exactly one
-- writer (dealer_scorecard.py) and one reader (dealerprice_network.py), both
-- updated in the same change, so the rename is cheap and total.
ALTER TABLE dp_dealer_scorecard RENAME COLUMN set_in_cars  TO submitted_cars;
ALTER TABLE dp_dealer_scorecard RENAME COLUMN set_in_first TO submitted_first;
ALTER TABLE dp_dealer_scorecard RENAME COLUMN set_in_last  TO submitted_last;
