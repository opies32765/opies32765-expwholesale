#!/bin/bash
# DEALER_SCORECARD_2026_07_31 — rebuild the dealer profitability board.
# Cheap (~2s: one pass over a local SQLite mirror + 2,341 row rewrite), so it
# runs hourly and the board is never more than an hour behind. The LSL nightly
# lands 07:00 UTC; the :20 run after it picks up the new deals.
# Read-only against crm.db (HR6). Cannot touch the bid/enrichment path (HR1).
cd /opt/expwholesale || exit 1
exec /usr/bin/python3 dealer_scorecard.py >> /var/log/ew_dealer_scorecard.log 2>&1
