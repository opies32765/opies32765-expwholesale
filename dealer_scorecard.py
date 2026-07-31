"""
dealer_scorecard.py — DEALER_SCORECARD_2026_07_31

Rebuilds dp_dealer_scorecard: the permanent, always-current dealer
profitability board plus the batting average.

WHY THIS EXISTS
    The historical-profit figures management liked were computed once, for the
    DealerPrice outreach email, and frozen into dp_outreach_targets. Only the
    clocks in that table refresh; the money never does. This recomputes the
    whole board from LSL on a schedule so the numbers stay true.

THE MATH IS NOT NEW
    Every profit rule here is lifted from _lsl_history() in
    dealerprice_network.py, which was audited on 2026-07-28 (DIRECTION_SPLIT).
    What changes is the shape, not the arithmetic: _lsl_history answers one
    dealer per call, and calling it 2,600 times would mean 2,600 passes over
    the same tables. This does ONE pass and buckets by supplier_id.

    If you change a rule here, change it there too, or the board and the
    per-dealer packet will quietly disagree.

    The rules, restated so this file stands on its own:
      * deals.supplier_id is the dealer EW SOLD TO -- not bought from. Verified
        by joining deals->inventory on VIN across 31,139 wholesale rows.
      * Cars EW BOUGHT come from two places that must be unioned:
          payments(type=Purchased, payee_type=Supplier, vendor_id=sid)
          inventory.purchased_from_id = sid
        The payments leg is a strict subset of the source leg in every case
        measured, so the union never double-counts. Without the source leg,
        1,856 of 1,999 source dealers read "0 bought".
      * payee_type=Supplier is the entity-space discriminator. Customer/Bank
        vendor_ids live in a DIFFERENT id space -- that is how a private
        individual once collided with a same-numbered dealer.
      * A VIN can carry several deal rows. Dedupe by VIN (keep latest sold_at)
        before summing, or the gross double-counts.
      * SELF_DEAL: one deal row can name the same dealer as both source and
        customer. That is ONE transaction and belongs to the buy leg, so those
        VINs are dropped from the sell leg.
      * supplier_id only. A name never counts on its own -- 43 dealer names map
        to multiple rooftops.

THE BATTING AVERAGE
    set_in_cars   distinct VINs tagged to this dealer on bids.source_supplier_id
    acquired_cars of those, the ones LSL shows EW actually bought FROM THIS
                  dealer -- the strict, correct numerator
    acquired_any  of those, the ones EW acquired from ANYONE. Diagnostic only:
                  if acquired_any runs far ahead of acquired_cars, attribution
                  is drifting (we bought the car, but LSL booked it to a
                  different rooftop) and the batting average is understating.
    batting       acquired_cars / set_in_cars, NULL when nothing was set in.
                  NULL means "we have not measured this dealer", which is not
                  the same as 0.00 ("they sent cars and we bought none"). The
                  page must keep those visually distinct.

    Tagging started 2026-07-31. Bids before that carry no dealer by design --
    the operator chose to start clean rather than backfill inferred guesses.

WHO COUNTS AS A DEALER  (operator correction, 2026-07-31)
    suppliers holds 2,645 rows and they are NOT all dealers. Only 1,967 have an
    uploaded licence or tax cert; the rest are largely private individuals who
    sold one car to the desk, plus marketplaces (Backlotcars, TradeRev,
    Manheim). Measured here: 1,180 licenced dealers carry $67.9M of the $71.1M
    gross, and the 1,168 unlicenced carry $3.2M.

    The uploaded licence is the discriminator -- 83% of the outreach targets
    have one against 0% of known retail. source_deals is NOT dealer proof: 96%
    of known-retail people have source deals, because an individual selling
    their own car creates exactly those rows.

    is_dealer is a FLAG, not a filter. 53 real-looking franchise stores have no
    uploaded licence, so dropping unlicenced rows would lose real dealers. The
    board defaults to dealers and can show everything.

ONE ROOFTOP, MANY SUPPLIER IDS
    A single dealership can hold several suppliers.id. That fragmentation is
    what hid Scott Ales from the outreach list. Ids are merged on a
    CONSERVATIVE key -- case, punctuation and invisible-character folding only,
    trade words KEPT.

    classify.py normalize_name is deliberately NOT used here. It strips
    auto/motors/group/sales, which merges "Wholesale Auto Group" with
    "Wholesale Inc" and "Dealer Wholesale Group LLC" into one dealer. The
    conservative key folds 56 ids into 26 groups and every one is a genuine
    duplicate (Signature Auto Group x4, F.c. Kerbeck & Sons x3, BMW FINANCIAL
    SERVICES vs Bmw Financial Services).

    Operator decisions in dealerprice_person_links are unioned on top and win
    over the name key: confirmed pairs are merged, rejected pairs never are.

HARD RULES
    HR6  crm.db is opened read-only, mode=ro. Nothing here writes to LSL.
    HR1  Never touches the bid/enrichment path. Read-side only; a failure here
         cannot delay or hide an enrichment leg.
    HR5  C1 only.
"""
from __future__ import annotations

import os
import re
import sys
import time
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, date

import psycopg2
import psycopg2.extras

LSL_DB = os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")


def _s(v):
    if v is None:
        return ""
    return v.strip() if isinstance(v, str) else str(v).strip()


def _d10(v):
    """First 10 chars of an LSL timestamp -> YYYY-MM-DD, or None."""
    s = _s(v)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except Exception:
        return None


def _lsl():
    c = sqlite3.connect("file:%s?mode=ro" % LSL_DB, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _pg():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _dealer_key(name):
    """Case / punctuation / invisible-character fold. Trade words are KEPT, so
    'Wholesale Auto Group' stays distinct from 'Wholesale Inc'. See the module
    docstring for why classify.py normalize_name is wrong for this job."""
    s = unicodedata.normalize("NFKC", name or "")
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def build_groups(c, pg_cur):
    """Fold suppliers.id into one row per real dealership.

    Returns (group_of, meta):
      group_of[sid]  -> representative sid for that dealership
      meta[rep]      -> {ids, name, has_license, has_tax_cert, is_dealer}
    """
    sup = {}
    for r in c.execute("SELECT id, name, license_url, tax_cert_url FROM suppliers"):
        sid = int(r["id"])
        sup[sid] = {"name": _s(r["name"]),
                    "lic": bool(_s(r["license_url"])),
                    "tax": bool(_s(r["tax_cert_url"]))}

    parent = {sid: sid for sid in sup}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # keep the lower id as representative so the key is stable across runs
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    # name key. An empty key (NULL/blank supplier name) never groups -- those
    # rows would otherwise all collapse into one giant fake dealer.
    by_key = defaultdict(list)
    for sid, d in sup.items():
        k = _dealer_key(d["name"])
        if k:
            by_key[k].append(sid)
    for k, ids in by_key.items():
        for other in ids[1:]:
            union(ids[0], other)

    # operator decisions win over the name key
    rejected = set()
    try:
        pg_cur.execute("SELECT primary_sid, linked_sid, decision "
                       "FROM dealerprice_person_links")
        for r in pg_cur.fetchall():
            a, b = int(r["primary_sid"]), int(r["linked_sid"])
            if a not in parent or b not in parent:
                continue
            if r["decision"] == "confirmed":
                union(a, b)
            elif r["decision"] == "rejected":
                rejected.add((min(a, b), max(a, b)))
    except Exception as e:
        print("[scorecard] person_links unavailable, name key only: %s" % e, flush=True)

    # A rejected pair must never end up merged. The name key alone has never
    # produced one (checked 2026-07-31), but say so loudly rather than silently
    # overriding an operator decision.
    for a, b in rejected:
        if find(a) == find(b):
            print("[scorecard] WARNING: operator REJECTED merging %s/%s but the "
                  "name key groups them -- left merged, needs a manual split" % (a, b),
                  flush=True)

    group_of, members = {}, defaultdict(list)
    for sid in sup:
        rep = find(sid)
        group_of[sid] = rep
        members[rep].append(sid)

    meta = {}
    for rep, ids in members.items():
        # display name: the longest non-empty one, so 'Professional Sales, Inc.'
        # beats 'Professional Sales Inc' and a blank never wins
        names = sorted((sup[i]["name"] for i in ids if sup[i]["name"]),
                       key=len, reverse=True)
        lic = any(sup[i]["lic"] for i in ids)
        tax = any(sup[i]["tax"] for i in ids)
        meta[rep] = {"ids": sorted(ids), "name": names[0] if names else None,
                     "has_license": lic, "has_tax_cert": tax,
                     "is_dealer": lic or tax}
    return group_of, meta


# -- the one pass over LSL ---------------------------------------------------
def build_profit(c, group_of=None):
    """Return {rep_supplier_id: {...profit legs...}} for every dealership with
    any activity. One pass per table, bucketed by the GROUP representative.

    Bucketing before the fold (rather than folding per id and summing after) is
    what makes the merge correct: a VIN that appears under two of a dealership's
    supplier ids lands in one set and is counted once. Summing per-id totals
    would double-count it."""

    # vin -> latest deal (front_value, sold_at). Needed for buy_resale_gross:
    # when EW buys a car from a dealer and resells it, the EW gross on that
    # relationship is the front_value on the RESALE deal.
    vin_deal = {}
    for r in c.execute(
            "SELECT vin_no, front_value, sold_at FROM deals "
            "WHERE vin_no IS NOT NULL AND vin_no <> ''"):
        v = _s(r["vin_no"]).upper()
        if not v:
            continue
        prev = vin_deal.get(v)
        so = _s(r["sold_at"])
        if prev is None or so > prev[1]:
            vin_deal[v] = (float(r["front_value"] or 0), so)

    S = {}
    gmap = group_of or {}

    def slot(sid):
        # an id LSL references but suppliers has no row for keeps its own bucket
        rep = gmap.get(sid, sid)
        if rep not in S:
            S[rep] = {"pay_vins": set(), "pay_paid": 0.0, "pay_dates": [],
                      "src": {}, "sell": {}, "sell_novin": []}
        return S[rep]

    # -- buy leg A: payments EW made to them --------------------------------
    for r in c.execute(
            "SELECT vendor_id, vin_no, amount, created_at FROM payments "
            "WHERE type='Purchased' AND payee_type='Supplier' "
            "AND vendor_id IS NOT NULL"):
        d = slot(int(r["vendor_id"]))
        v = _s(r["vin_no"]).upper()
        if v:
            d["pay_vins"].add(v)
        d["pay_paid"] += float(r["amount"] or 0)
        dt = _d10(r["created_at"])
        if dt:
            d["pay_dates"].append(dt)

    # -- buy leg B: cars sourced from them with no payment row ---------------
    # keyed on inventory.purchased_from_id (an id, never source_name)
    for r in c.execute(
            "SELECT i.purchased_from_id sid, d.vin_no, d.purchase_cost, "
            "       d.front_value, d.sold_at "
            "FROM deals d JOIN inventory i ON i.vin_no = d.vin_no "
            "WHERE i.purchased_from_id IS NOT NULL"):
        v = _s(r["vin_no"]).upper()
        if not v:
            continue
        d = slot(int(r["sid"]))
        prev = d["src"].get(v)
        so = _s(r["sold_at"])
        if prev is None or so > prev["sold_at"]:
            d["src"][v] = {"purchase_cost": float(r["purchase_cost"] or 0),
                           "sold_at": so}

    # -- sell leg: deals EW invoiced them for --------------------------------
    for r in c.execute(
            "SELECT supplier_id sid, vin_no, sale_price, front_value, sold_at "
            "FROM deals WHERE supplier_id IS NOT NULL"):
        d = slot(int(r["sid"]))
        v = _s(r["vin_no"]).upper()
        row = {"sale_price": float(r["sale_price"] or 0),
               "front_value": float(r["front_value"] or 0),
               "sold_at": _s(r["sold_at"])}
        if not v:
            d["sell_novin"].append(row)
            continue
        prev = d["sell"].get(v)
        if prev is None or row["sold_at"] > prev["sold_at"]:
            d["sell"][v] = row

    # -- fold each supplier --------------------------------------------------
    out = {}
    for sid, d in S.items():
        src_vins = set(d["src"].keys())
        pay_vins = d["pay_vins"]

        # SELF_DEAL: a car EW bought from them must not also count as a sale
        sell = {v: r for v, r in d["sell"].items() if v not in src_vins}
        sell_rows = list(sell.values()) + d["sell_novin"]
        sold_vins = set(sell.keys())

        bought_vins = pay_vins | src_vins
        if not bought_vins and not sell_rows:
            continue

        # ROUNDING_PARITY: money is summed as float and truncated ONCE, at the
        # same points _lsl_history truncates. Truncating per row instead loses
        # up to a dollar a car and puts the board a few dollars off the packet
        # for the same dealer -- small, but it makes two screens showing the
        # same relationship disagree, which is worse than being round.
        src_only_paid = int(sum(r["purchase_cost"] for v, r in d["src"].items()
                                if v not in pay_vins))
        bought_paid = int(d["pay_paid"]) + src_only_paid

        sold_gross = int(sum(r["front_value"] for r in sell_rows))
        sold_revenue = int(sum(r["sale_price"] for r in sell_rows))

        # EW gross on cars bought from them and resold elsewhere
        resale_vins = bought_vins - sold_vins
        buy_resale_cars = 0
        _resale_gross = 0.0
        for v in resale_vins:
            hit = vin_deal.get(v)
            if hit:
                buy_resale_cars += 1
                _resale_gross += hit[0]
        buy_resale_gross = int(_resale_gross)

        src_dates = [x for x in (_d10(r["sold_at"]) for r in d["src"].values()) if x]
        buy_dates = sorted(d["pay_dates"] + src_dates)
        sell_dates = sorted(x for x in
                            (_d10(r["sold_at"]) for r in sell_rows) if x)
        all_dates = sorted(buy_dates + sell_dates)

        out[sid] = {
            "bought_cars": len(bought_vins),
            "bought_paid": bought_paid,
            "buy_first": buy_dates[0] if buy_dates else None,
            "buy_last": buy_dates[-1] if buy_dates else None,
            "sold_cars": len(sell_rows),
            "sold_revenue": sold_revenue,
            "sold_gross": sold_gross,
            "sell_first": sell_dates[0] if sell_dates else None,
            "sell_last": sell_dates[-1] if sell_dates else None,
            "buy_resale_cars": buy_resale_cars,
            "buy_resale_gross": buy_resale_gross,
            "total_gross": sold_gross + buy_resale_gross,
            "tx_count": len(bought_vins) + len(sell_rows),
            "first_activity": all_dates[0] if all_dates else None,
            "last_activity": all_dates[-1] if all_dates else None,
            # kept for the batting join below
            "_bought_vins": bought_vins,
        }
    return out


def build_batting(pg_cur, profit, group_of=None):
    """{rep_supplier_id: (set_in, first, last, acquired, acquired_any)} from the
    dealer tags on bids. Only VINs are counted -- a submission with no VIN
    cannot be matched to an acquisition, so counting it would depress the
    average with something unmeasurable.

    Tags are mapped through the same grouping as the profit legs. A rep who
    tagged one rooftop of a dealership and a deal that booked to another must
    land on the same row, or the car reads as set-in-but-never-bought."""
    pg_cur.execute("""
        SELECT source_supplier_id sid, upper(vin) vin, created_at
          FROM bids
         WHERE source_supplier_id IS NOT NULL
           AND vin IS NOT NULL AND length(vin) = 17
    """)
    gmap = group_of or {}
    per = {}
    for r in pg_cur.fetchall():
        sid = gmap.get(int(r["sid"]), int(r["sid"]))
        e = per.setdefault(sid, {"vins": set(), "first": None, "last": None})
        e["vins"].add(r["vin"])
        ts = r["created_at"].date() if r["created_at"] else None
        if ts:
            if e["first"] is None or ts < e["first"]:
                e["first"] = ts
            if e["last"] is None or ts > e["last"]:
                e["last"] = ts

    # every VIN EW has ever acquired, from anyone
    any_acquired = set()
    for p in profit.values():
        any_acquired |= p["_bought_vins"]

    out = {}
    for sid, e in per.items():
        vins = e["vins"]
        mine = profit.get(sid, {}).get("_bought_vins", set())
        acquired = len(vins & mine)
        out[sid] = (len(vins), e["first"], e["last"],
                    acquired, len(vins & any_acquired))
    return out


def refresh(verbose=True):
    t0 = time.time()
    pg = _pg()
    cur = pg.cursor()
    cur.execute("INSERT INTO dp_dealer_scorecard_run (started_at) "
                "VALUES (now()) RETURNING id")
    run_id = cur.fetchone()["id"]
    pg.commit()

    try:
        c = _lsl()
        try:
            group_of, meta = build_groups(c, cur)
            profit = build_profit(c, group_of)
        finally:
            c.close()

        batting = build_batting(cur, profit, group_of)
        today = date.today()

        def info(sid):
            return meta.get(sid, {"ids": [sid], "name": None, "has_license": False,
                                  "has_tax_cert": False, "is_dealer": False})

        rows = []
        for sid, p in profit.items():
            m = info(sid)
            b = batting.get(sid)
            set_in = b[0] if b else 0
            acquired = b[3] if b else 0
            last = _date(p["last_activity"])
            rows.append((
                sid, m["name"],
                p["bought_cars"], p["bought_paid"],
                _date(p["buy_first"]), _date(p["buy_last"]),
                p["sold_cars"], p["sold_revenue"], p["sold_gross"],
                _date(p["sell_first"]), _date(p["sell_last"]),
                p["buy_resale_cars"], p["buy_resale_gross"],
                p["total_gross"], p["tx_count"],
                _date(p["first_activity"]), last,
                (today - last).days if last else None,
                set_in, b[1] if b else None, b[2] if b else None,
                acquired, b[4] if b else 0,
                (round(100.0 * acquired / set_in, 2) if set_in else None),
                _dealer_key(m["name"]) or ("sid:%d" % sid), m["ids"], len(m["ids"]),
                m["is_dealer"], m["has_license"], m["has_tax_cert"],
            ))

        # A dealer can be tagged on a bid before LSL has any history for them
        # (a brand-new DealerPrice member). They still belong on the board --
        # a dealer setting in cars we never buy is exactly what management
        # asked to see.
        for sid, b in batting.items():
            if sid in profit:
                continue
            m = info(sid)
            rows.append((sid, m["name"],
                         0, 0, None, None, 0, 0, 0, None, None, 0, 0, 0, 0,
                         None, None, None,
                         b[0], b[1], b[2], b[3], b[4],
                         (round(100.0 * b[3] / b[0], 2) if b[0] else None),
                         _dealer_key(m["name"]) or ("sid:%d" % sid), m["ids"],
                         len(m["ids"]), m["is_dealer"], m["has_license"],
                         m["has_tax_cert"]))

        # Full rebuild in one transaction. It is a derived cache, so replacing
        # it wholesale is safe -- and it means a dealer whose last deal was
        # voided in LSL actually loses the row instead of lingering forever.
        cur.execute("DELETE FROM dp_dealer_scorecard")
        psycopg2.extras.execute_values(cur, """
            INSERT INTO dp_dealer_scorecard (
              supplier_id, supplier_name,
              bought_cars, bought_paid, buy_first, buy_last,
              sold_cars, sold_revenue, sold_gross, sell_first, sell_last,
              buy_resale_cars, buy_resale_gross, total_gross, tx_count,
              first_activity, last_activity, days_since,
              set_in_cars, set_in_first, set_in_last,
              acquired_cars, acquired_any, batting,
              dealer_key, supplier_ids, rooftops,
              is_dealer, has_license, has_tax_cert)
            VALUES %s
        """, rows, page_size=500)
        cur.execute("UPDATE dp_dealer_scorecard SET refreshed_at = now()")
        secs = round(time.time() - t0, 2)
        cur.execute("UPDATE dp_dealer_scorecard_run SET finished_at=now(), "
                    "dealers=%s, ok=TRUE, secs=%s WHERE id=%s",
                    (len(rows), secs, run_id))
        pg.commit()
        if verbose:
            dealers = sum(1 for r in rows if r[-3])
            print("[scorecard] %d rows (%d licenced dealers, %d unlicenced) in %ss"
                  % (len(rows), dealers, len(rows) - dealers, secs), flush=True)
        return len(rows)
    except Exception as e:
        pg.rollback()
        try:
            cur.execute("UPDATE dp_dealer_scorecard_run SET finished_at=now(), "
                        "ok=FALSE, error=%s WHERE id=%s", (str(e)[:500], run_id))
            pg.commit()
        except Exception:
            pass
        print("[scorecard] FAILED: %s" % e, flush=True)
        raise
    finally:
        pg.close()


if __name__ == "__main__":
    refresh(verbose="-q" not in sys.argv)
