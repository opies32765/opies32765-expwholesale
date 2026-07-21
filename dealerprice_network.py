"""
dealerprice_network.py — DealerPrice "Become part of the Dealer Network" gate.
DEALERPRICE_NETWORK_2026_06_30.

Adds the vetting/application funnel for dealerprice.net WITHOUT touching the
live bid/enrichment path. Two halves:

  • Public (X-Auth, login-exempt via the /api/dealerprice/ prefix):
      POST /api/dealerprice/apply           — a dealer applies to the network
      POST /api/dealerprice/check-existing   — Q0 "already an EW dealer?" lookup

  • Operator (behind the app-level require_login, NOT under /api/):
      GET  /network/applications                  — review queue
      GET  /network/application/<id>              — full vetting packet
      GET  /network/application/<id>/doc/<which>  — serve the PRIVATE license/Tax-ID image
      POST /network/application/<id>/approve      — mint member token + invite the dealer
      POST /network/application/<id>/reject
      POST /network/application/<id>/needs-info

HARD RULES honored: no FK to bids (HR1 — can never block enrichment); LSL is
read-only (HR6); no cloud LLM (HR4); C1-only (HR5); no import-time DDL — the
tables ship via ops/migrations/2026-06-30_dealer_network.sql (HR8). Uploaded
license/Tax-ID docs are stored OUTSIDE /static and served only through the
login-gated /network/.../doc route.

Registered by wsgi.py on every gunicorn worker boot (drift-resistant), the same
pattern as recon / wholesaler_review / network_push.
"""
from __future__ import annotations
import os
import re
import json
import time
import base64
import secrets
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, jsonify, abort,
                   session, redirect, url_for, send_file)

bp = Blueprint('dealerprice_network', __name__)

SECRET = (os.environ.get('EW_DEALERPRICE_SECRET') or '').strip()
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
PRIV_DOC_ROOT = os.environ.get('DP_DOC_ROOT', '/opt/expwholesale/private/dealer_docs')
# public base for the magic link we text/email an approved dealer
DP_PUBLIC_BASE = os.environ.get('DP_PUBLIC_BASE', 'https://dealerprice.net')

DEALER_TYPES = ['Exotic', 'High-Volume Commodity', 'Niche / Specialty',
                'Wholesale', 'Large-Volume Mix', 'Subprime']


# NO_CACHE_2026_07_17 — the operator review pages (/network/...) re-run live LSL
# roster + deal-ledger lookups on every load, so a stale browser/CDN copy shows
# out-of-date match data (bit us: a corrected packet kept showing the old copy
# on refresh because the response carried no Cache-Control at all). Force
# no-store on every response from this blueprint (review pages AND the JSON
# APIs — none of them should ever be cached).
@bp.after_request
def _no_store(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ── small coercion helpers ──────────────────────────────────────────────────
def _s(v):
    return ('' if v is None else str(v)).strip()


def _digits(v):
    return re.sub(r'[^0-9]', '', _s(v))


def _int(v):
    d = _digits(v)
    try:
        return int(d) if d else None
    except ValueError:
        return None


def _num(v):
    s = re.sub(r'[^0-9.]', '', _s(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _b(v):
    return v in (True, 'true', 'on', '1', 1, 'yes', 'YES')


def _date(v):
    s = _s(v)
    if not s:
        return None
    if re.fullmatch(r'\d{4}-\d{2}', s):
        s += '-01'
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return s
    except ValueError:
        return None


# ── DB / LSL ────────────────────────────────────────────────────────────────
def _db():
    from app import get_db
    return get_db()


def _lsl_conn():
    import sqlite3
    c = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


_LEGAL_SUFFIXES = ('incorporated', 'corporation', 'company', 'llc', 'inc', 'corp', 'ltd', 'llp', 'co')


def _normalize_name(name):
    """Collapse a dealership name to bare alphanumerics (+ drop a trailing
    legal suffix) so 'AutoStreetUSA' and LSL's stored 'Auto Street Usa'
    compare equal despite spacing/punctuation/case drift that raw SQL
    LIKE patterns miss (a dealer types their own stylization; LSL has
    whatever was typed in at onboarding — they rarely match verbatim)."""
    s = re.sub(r'[^a-z0-9]', '', (name or '').lower())
    for suf in _LEGAL_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[:-len(suf)]
            break
    return s


_JUNK_PHONES = {str(dig) * 10 for dig in range(10)} | {'1234567890'}


def _norm_phone10(v):
    """Digits-only, normalized to 10 digits (drops a leading US country '1')
    so '5616850133' and '+1(561)-685-0133' compare equal. Returns '' for
    degenerate placeholder numbers (LSL has real rows with '999-999-9999'
    filler) so junk can never satisfy the phone-match fallback below."""
    d = _digits(v)
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    if len(d) == 10 and d in _JUNK_PHONES:
        return ''
    return d


def _supplier_match_dict(row, matched_via=None):
    """Review-packet match dict for a suppliers row, ENRICHED with the vetting
    signals LSL already holds — verified dealer license / tax cert / W9,
    onboard date, active/blocked status — the data that actually tells the
    operator whether a rostered dealer is safe, beyond just name/city/phone."""
    d = {'matched': True, 'source': 'suppliers', 'name': _s(row['name']),
         'supplier_id': row['id'],
         'contact': _s(row['primary_contact']),
         'phone': _s(row['office'] or row['primary_contact_mobile']),
         'city': _s(row['city']), 'state': _s(row['state']),
         'email': _s(row['email']),
         'address': _s(row['full_address'] or row['address1']),
         'status': _s(row['status']),
         'is_blocked': bool(row['is_blocked']),
         'approved': bool(row['approved']),
         'trusted': bool(row['trusted']),
         'has_license': bool(_s(row['license_url'])),
         'has_tax_cert': bool(_s(row['tax_cert_url'])),
         'license_exp': _s(row['license_expiration'])[:10],
         'tax_cert_exp': _s(row['tax_cert_expiration'])[:10]}
    # onboard date + W9 status live only inside the raw LSL payload
    try:
        rj = json.loads(row['raw_json'] or '{}')
        d['onboarded'] = (_s(rj.get('createdAt'))[:10] or None)
        d['has_w9'] = bool(rj.get('w9Status') or rj.get('w9FileLocation'))
        d['verified'] = bool(rj.get('verified'))
    except Exception:
        pass
    if matched_via:
        d['matched_via'] = matched_via
    return d


def _roster_match(name, phone=None):
    """Is this dealership/referrer already an EW counterparty? Read-only LSL
    lookup against suppliers (sellers/wholesalers) then customers (buyers).
    Tries exact/prefix/contains on the raw name first (cheap, covers the
    common case), then falls back to a normalized-name compare and a
    phone-number compare over the full suppliers table (~2.6k rows, cheap
    to scan in Python) — a dealer's own stylization of their name often
    doesn't literally substring-match LSL's stored form, but their phone
    number never drifts. Returns {} when unknown, else an enriched match
    dict for the review packet."""
    name = _s(name)
    phone10 = _norm_phone10(phone) if phone else ''
    if len(name) < 3 and len(phone10) != 10:
        return {}
    try:
        c = _lsl_conn()
        try:
            r = None
            if len(name) >= 3:
                r = c.execute(
                    "SELECT * FROM suppliers WHERE name=? COLLATE NOCASE LIMIT 1",
                    (name,)).fetchone()
                if not r:
                    r = c.execute(
                        "SELECT * FROM suppliers WHERE name LIKE ? "
                        "ORDER BY length(name) LIMIT 1", (name + '%',)).fetchone()
                if not r and len(name) >= 5:                 # contains fallback (partial dealership / referrer)
                    r = c.execute(
                        "SELECT * FROM suppliers WHERE name LIKE ? "
                        "ORDER BY length(name) LIMIT 1", ('%' + name + '%',)).fetchone()
            if r:
                return _supplier_match_dict(r)

            # normalized-name / phone fallback — catches e.g. "AutoStreetUSA"
            # vs LSL's "Auto Street Usa" (same dealer, different spacing).
            norm_target = _normalize_name(name) if len(name) >= 3 else ''
            if norm_target or len(phone10) == 10:
                for row in c.execute("SELECT * FROM suppliers WHERE name<>''"):
                    if norm_target and _normalize_name(row['name']) == norm_target:
                        return _supplier_match_dict(row, 'normalized_name')
                    if len(phone10) == 10 and phone10 in (
                            _norm_phone10(row['office']), _norm_phone10(row['primary_contact_mobile'])):
                        return _supplier_match_dict(row, 'phone')

            if len(name) >= 3:
                r = c.execute(
                    "SELECT company_name, full_name, mobile FROM customers "
                    "WHERE company_name=? COLLATE NOCASE OR full_name=? COLLATE NOCASE "
                    "LIMIT 1", (name, name)).fetchone()
                if r:
                    return {'matched': True, 'source': 'customers',
                            'name': _s(r['company_name'] or r['full_name']),
                            'contact': _s(r['full_name']), 'phone': _s(r['mobile'])}
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_match: %s' % e, flush=True)
    return {}


def _roster_search(q, limit=8):
    """Typeahead for the Q0 existing-dealer path — distinct supplier names."""
    q = _s(q)
    if len(q) < 2:
        return []
    out = []
    try:
        c = _lsl_conn()
        try:
            rows = c.execute(
                "SELECT DISTINCT name, city, state FROM suppliers "
                "WHERE name LIKE ? AND name<>'' "
                "ORDER BY (name LIKE ?) DESC, length(name) LIMIT ?",
                ('%' + q + '%', q + '%', limit)).fetchall()
            out = [{'name': r['name'], 'city': _s(r['city']), 'state': _s(r['state'])}
                   for r in rows]
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_search: %s' % e, flush=True)
    return out


def _lsl_history_agg(c, swhere, sparams, bwhere, bparams):
    """Aggregate the deals ledger for a supplier-side + buyer-side WHERE clause.
    Returns the history dict, or {} if the clauses matched nothing."""
    s = c.execute(
        "SELECT count(*) n, COALESCE(SUM(purchase_cost),0) paid, "
        "COALESCE(SUM(front_value),0) gross, MIN(sold_at) f, MAX(sold_at) l "
        "FROM deals WHERE " + swhere, sparams).fetchone()
    b = c.execute(
        "SELECT count(*) n, COALESCE(SUM(sale_price),0) spent, "
        "MIN(sold_at) f, MAX(sold_at) l "
        "FROM deals WHERE " + bwhere, bparams).fetchone()
    sn, bn = (s['n'] or 0), (b['n'] or 0)
    if sn + bn == 0:
        return {}
    names = [r['nm'] for r in c.execute(
        "SELECT supplier_name nm FROM deals WHERE " + swhere + " "
        "UNION SELECT customer_name nm FROM deals WHERE " + bwhere + " "
        "LIMIT 6", list(sparams) + list(bparams)).fetchall() if r['nm']]
    firsts = [d for d in (s['f'], b['f']) if d]
    lasts = [d for d in (s['l'], b['l']) if d]
    return {
        'matched': True,
        'names': names,
        'total_deals': sn + bn,
        'source_deals': sn, 'source_paid': int(s['paid'] or 0), 'source_gross': int(s['gross'] or 0),
        'buyer_deals': bn, 'buyer_spent': int(b['spent'] or 0),
        'first_deal': (min(firsts)[:10] if firsts else None),
        'last_deal': (max(lasts)[:10] if lasts else None),
    }


def _lsl_history(name, supplier_id=None, matched_name=None):
    """VERIFIED transaction history for a dealer resolved to a suppliers.id,
    using ONLY authoritative dealer keys (multi-agent LSL audit, 2026-07-17):

      • CARS EW BOUGHT from them = distinct VINs across
          payments(vendor_id=id, type='Purchased', payee_type NOT IN
                   ('Customer','Bank'), vendor_name agrees with the dealer)
          ∪ deals(supplier_id=id)              -- older cars EW bought & resold
      • CARS EW SOLD to them = deals with a REAL customer entity link (0 for
        wholesale suppliers; the customer-side resolver is pending the
        systematic matcher).

    ⛔ Three false-positive classes this REPLACES (all found in the audit):
      1. `customer_name` is a denormalized MIRROR of `supplier_name` (with a
         NULL customer_id) on every wholesale deal → matching it double-counted
         the cars we BOUGHT as cars "sold to them" (showed Naples a bogus "10
         sold / $580k"; real = 0). We now NEVER match customer_name.
      2. `payee_type` Customer/Bank = a consumer/lender payment, NOT dealer
         activity (a stranger's $31k Mustang mis-attributed to a same-named
         dealer). Now excluded.
      3. NAME as an identity key collides (43 dealer names → multiple ids; one
         switchboard phone → 13 rooftops). We require a resolved supplier_id and
         corroborate the payments vendor_name; a name alone never counts.
    Requires supplier_id. Pure read-only (HR6)."""
    if not supplier_id:
        return {}
    try:
        c = _lsl_conn()
        try:
            # cars EW bought — payments leg. payee_type='Supplier' is the
            # entity-space discriminator: payments.vendor_id references
            # suppliers.id ONLY for Supplier-payee rows. Customer/Bank vendor_ids
            # live in a DIFFERENT id-space — that's exactly how a private
            # individual 'Oscar Pastrana' (payee_type=Customer) collided with the
            # same-numbered dealer. Audit-verified: of Supplier-payee vendor_ids
            # in suppliers, all but 2 (legit DBA/parent variants) name-match, and
            # every real dealer purchase has a suppliers row — so the id alone is
            # reliable under this filter; no name guard needed.
            prows = c.execute(
                "SELECT vin_no, amount, stock_no, title_status, created_at "
                "FROM payments WHERE vendor_id=? AND type='Purchased' "
                "AND payee_type='Supplier'",
                (supplier_id,)).fetchall()
            # cars EW bought — older resold cars (deals where THEY are the supplier)
            drows = c.execute(
                "SELECT vin_no, purchase_cost, front_value, sold_at, stock_no "
                "FROM deals WHERE supplier_id=?", (supplier_id,)).fetchall()
            # cars EW SOLD to them — real customer entity only (NOT the mirror name)
            sold_cars = 0   # pending customer-entity resolver; mirror-name excluded

            pay_vins = set(_s(r['vin_no']) for r in prows if _s(r['vin_no']))
            deal_vins = set(_s(r['vin_no']) for r in drows if _s(r['vin_no']))
            bought_vins = pay_vins | deal_vins
            if not bought_vins and not sold_cars:
                return {}
            pay_dates = sorted((_s(r['created_at']))[:10] for r in prows if _s(r['created_at']))
            # per-VIN car list (for the expandable "all cars" panel on the packet)
            cars = []
            for r in prows:
                cars.append({'order': _s(r['stock_no']), 'vin': _s(r['vin_no']),
                             'amount': int(r['amount'] or 0),
                             'date': (_s(r['created_at']))[:10] or None, 'kind': 'Bought'})
            for r in drows:
                cars.append({'order': _s(r['stock_no']), 'vin': _s(r['vin_no']),
                             'amount': int(r['purchase_cost'] or 0),
                             'date': (_s(r['sold_at']))[:10] or None,
                             'kind': ('Bought + resold (+$%s gross)' % '{:,.0f}'.format(int(r['front_value'] or 0)))})
            cars.sort(key=lambda x: x['date'] or '', reverse=True)
            # attach year/make/model per VIN: inventory (in-stock cars) then
            # deals.vehicle_info (a clean full description) which wins when present
            vlist = [car['vin'] for car in cars if car['vin']]
            vmap = {}
            if vlist:
                vph = ','.join('?' * len(vlist))
                for r in c.execute(
                        "SELECT vin_no, group_model_trim_year y, vehicle_make_name mk, "
                        "vehicle_series_name sr, group_model_name gm FROM inventory "
                        "WHERE vin_no IN (%s)" % vph, vlist):
                    desc = ' '.join(_s(x) for x in
                                    (r['y'], r['mk'], (r['sr'] or r['gm'])) if _s(x))
                    if _s(r['vin_no']) and desc:
                        vmap[_s(r['vin_no'])] = desc
                for r in c.execute(
                        "SELECT vin_no, vehicle_info FROM deals WHERE vin_no IN (%s)" % vph, vlist):
                    if _s(r['vin_no']) and _s(r['vehicle_info']):
                        vmap[_s(r['vin_no'])] = _s(r['vehicle_info'])
            for car in cars:
                car['vehicle'] = vmap.get(car['vin'], '')
            return {
                'matched': True,
                'bought_cars': len(bought_vins),
                'bought_paid': int(sum(r['amount'] or 0 for r in prows)
                                   + sum(r['purchase_cost'] or 0 for r in drows)),
                'payments_cars': len(pay_vins),
                'payments_paid': int(sum(r['amount'] or 0 for r in prows)),
                'pay_first': pay_dates[0] if pay_dates else None,
                'pay_last': pay_dates[-1] if pay_dates else None,
                'titles_pending': sum(1 for r in prows if _s(r['title_status']) != 'Yes'),
                'resold_cars': len(deal_vins),
                'resold_gross': int(sum(r['front_value'] or 0 for r in drows)),
                'sold_cars': sold_cars,
                'tx_count': len(bought_vins) + sold_cars,
                'last_activity': max((car['date'] for car in cars if car['date']), default=None),
                'cars': cars,
            }
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] lsl_history: %s' % e, flush=True)
    return {}


# ── PERSON-LEVEL HISTORY MERGE (PERSON_MERGE_2026_07_21) ─────────────────────
# Bug management flagged (Sam Beatty / The Naples Source): dealer history was
# scoped to ONE suppliers row, so an individual who worked at multiple rooftops
# showed a fragmented, wrong picture ("on file since 2025-05-09" but "first
# purchase 2026-03-08"). Two fixes:
#   1) first_activity = earliest across BOTH legs (deals sold_at + payments
#      created_at), not payments-only (payments.paid_at is NULL so its created_at
#      is a data-entry date, later than the real first deal).
#   2) merge the same INDIVIDUAL across stores, keyed on contact NAME + PHONE:
#        CONFIRMED  exact full name + matching phone   -> auto-merge
#        STRONG     exact full name + same state       -> auto-merge
#        REVIEW     typo / first-initial-only / diff-state / diff-phone -> ask
#      The first-name guard means family (Sam vs Dave/Adam Beatty) is NEVER
#      auto-merged. Additive: wraps the audited single-store _lsl_history.
# Chosen by operator 2026-07-21 ("confirm weak matches", "name + phone").

def _person_tokens(nm):
    """Lowercase alpha tokens of a person name: 'Sam  Beatty' -> ['sam','beatty']."""
    import re as _re
    return [t for t in _re.sub(r'[^a-z ]', ' ', _s(nm).lower()).split() if t]


def _editdist(a, b, cap=2):
    """Bounded Levenshtein (returns cap+1 once it exceeds cap). Cheap for names."""
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > cap:
            return cap + 1
        prev = cur
    return prev[lb]


def _same_person_name(a_tokens, b_name):
    """Compare applicant contact-name tokens to a store's primary_contact.
    Returns 'exact' | 'fuzzy' (last name typo) | 'initial' (last = initial) | ''.
    FIRST names must agree — this is the family guard (Sam != Dave/Adam)."""
    b = _person_tokens(b_name)
    if len(a_tokens) < 2 or len(b) < 1:
        return ''
    af, al = a_tokens[0], a_tokens[-1]
    if b[0] != af:                       # different first name -> not this person
        return ''
    if len(b) < 2:                       # store contact is just a first name
        return ''
    bl = b[-1]
    if bl == al:
        return 'exact'
    if len(bl) <= 2 and bl[:1] == al[:1]:
        return 'initial'                 # 'Sam B' vs 'Sam Beatty'
    if len(al) >= 4 and len(bl) >= 4 and _editdist(al, bl) <= 2:
        return 'fuzzy'                    # 'Beatty' vs 'Beety'
    return ''


_US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC'}


def _norm_state(s):
    """FL / Florida / florida -> 'FL' so state comparison survives LSL's mixed
    storage of full names vs abbreviations."""
    s = _s(s).strip().lower()
    if not s:
        return ''
    if len(s) == 2:
        return s.upper()
    return _US_STATES.get(s, s[:2].upper())


def _person_phone(c, norm_name):
    """Best known phone for a person, from dealer_profile (person-level agg)."""
    try:
        r = c.execute("SELECT best_phone FROM dealer_profile WHERE norm_name=? "
                      "AND best_phone IS NOT NULL AND best_phone<>'' LIMIT 1",
                      (norm_name,)).fetchone()
        return _norm_phone10(r['best_phone']) if r else ''
    except Exception:
        return ''


def _resolve_person_stores(c, contact_name, contact_phone, primary_sid, primary_state):
    """Other suppliers rows that are the SAME INDIVIDUAL as the applicant contact.
    Returns (merge:list[dict], review:list[dict]) — merge = auto-include
    (CONFIRMED/STRONG), review = operator-confirm (weak). Read-only."""
    atoks = _person_tokens(contact_name)
    aphone = _norm_phone10(contact_phone)
    merge, review = [], []
    if len(atoks) < 2:
        return merge, review
    seen = set()
    for row in c.execute(
            "SELECT id, name, primary_contact, city, state, office, "
            "primary_contact_mobile FROM suppliers "
            "WHERE primary_contact IS NOT NULL AND primary_contact<>''"):
        sid = row['id']
        if sid == primary_sid or sid in seen:
            continue
        kind = _same_person_name(atoks, row['primary_contact'])
        if not kind:
            continue
        seen.add(sid)
        sphone = (_norm_phone10(row['office']) or _norm_phone10(row['primary_contact_mobile'])
                  or _person_phone(c, _normalize_name(row['primary_contact'])))
        # phone is an UPGRADE signal only — a match confirms; a mismatch does NOT
        # veto (the same person legitimately has different lines office vs cell at
        # different rooftops, and family is already separated by the first-name
        # guard above). States are normalized (FL == Florida) before comparing.
        phone_ok = bool(aphone) and bool(sphone) and aphone == sphone
        sn, ps = _norm_state(row['state']), _norm_state(primary_state)
        states_differ = bool(sn) and bool(ps) and sn != ps
        item = {'id': sid, 'name': _s(row['name']), 'contact': _s(row['primary_contact']),
                'city': _s(row['city']), 'state': _s(row['state'])}
        if kind == 'exact' and phone_ok:
            item['tier'] = 'confirmed'
            merge.append(item)
        elif kind == 'exact' and not states_differ:
            item['tier'] = 'strong'
            merge.append(item)
        elif kind == 'exact':
            item['tier'] = 'review'
            item['reason'] = 'different state'
            review.append(item)
        else:
            item['tier'] = 'review'
            item['reason'] = ('name typo' if kind == 'fuzzy' else 'first-initial only')
            review.append(item)
    return merge, review


def _lsl_history_person(name, name_match, contact_name=None, contact_phone=None):
    """Person-level VERIFIED history: the matched store's ledger UNIONED with the
    other rooftops the SAME individual (contact name + phone) has worked at, so a
    dealer who moved stores shows ONE true history. Wraps the audited single-store
    _lsl_history per rooftop and merges; adds first_activity (earliest across BOTH
    legs), a per-store breakdown, and a review list of weak matches to confirm.
    Read-only (HR6)."""
    nm = name_match or {}
    base_sid = nm.get('supplier_id')
    if not base_sid:
        return _lsl_history(name, None)
    merge, review = [], []
    try:
        c = _lsl_conn()
        try:
            merge, review = _resolve_person_stores(
                c, contact_name or nm.get('contact'), contact_phone,
                base_sid, nm.get('state'))
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] person_stores: %s' % e, flush=True)

    order = [{'id': base_sid, 'name': nm.get('name'), 'contact': nm.get('contact'),
              'state': nm.get('state'), 'tier': 'primary'}] + merge
    hists = []
    for st in order:
        h = _lsl_history(name, st['id'])
        if h and h.get('matched') and h.get('tx_count'):
            h['_store'] = st
            hists.append(h)

    if not hists:
        base = _lsl_history(name, base_sid) or {}
        if review:
            base['review'] = review
        return base

    # union car rows across rooftops, dedupe by VIN (keep the earliest date)
    byvin, misc = {}, []
    for h in hists:
        for car in h.get('cars', []):
            v = _s(car.get('vin'))
            if not v:
                misc.append(car)
                continue
            if v not in byvin or (car.get('date') or '~') < (byvin[v].get('date') or '~'):
                byvin[v] = car
    mcars = list(byvin.values()) + misc
    mcars.sort(key=lambda x: x.get('date') or '', reverse=True)
    dates = sorted(car['date'] for car in mcars if car.get('date'))

    def _sfirst(h):
        ds = [c['date'] for c in h.get('cars', []) if c.get('date')]
        return min(ds) if ds else None
    stores = [{'id': h['_store']['id'], 'name': _s(h['_store'].get('name')),
               'contact': _s(h['_store'].get('contact')),
               'bought': h.get('bought_cars', 0), 'first': _sfirst(h),
               'tier': h['_store'].get('tier')} for h in hists]

    # single rooftop, no weak matches: return it as-is but with both-leg dates
    if len(hists) == 1 and not review:
        h = dict(hists[0])
        h['first_activity'] = dates[0] if dates else h.get('pay_first')
        h['last_activity'] = dates[-1] if dates else h.get('pay_last')
        h.pop('_store', None)
        return h

    n_cars = len(byvin) + len(misc)
    return {
        'matched': True,
        'bought_cars': n_cars,
        'bought_paid': sum(int(car.get('amount') or 0) for car in mcars),
        'payments_cars': sum(h.get('payments_cars', 0) for h in hists),
        'payments_paid': sum(h.get('payments_paid', 0) for h in hists),
        'pay_first': dates[0] if dates else None,
        'pay_last': dates[-1] if dates else None,
        'first_activity': dates[0] if dates else None,
        'last_activity': dates[-1] if dates else None,
        'titles_pending': sum(h.get('titles_pending', 0) for h in hists),
        'resold_cars': sum(h.get('resold_cars', 0) for h in hists),
        'resold_gross': sum(h.get('resold_gross', 0) for h in hists),
        'sold_cars': 0,
        'tx_count': n_cars,
        'cars': mcars,
        'merged_store_count': len(hists),
        'stores': stores,
        'review': review,
    }
# ── end PERSON_MERGE_2026_07_21 ──────────────────────────────────────────────


def _auto_classify(lsl_hist):
    """Assign a classification from the VERIFIED ledger — NOT self-declaration
    or a bare roster match (operator directive 2026-07-17, 12-month window):
      • current_partner  — real transactions, last activity within 12 months
      • previous_partner — real history, but nothing in 12+ months
      • new_applicant    — no verified transactions (whatever they claimed)
    The operator can still override manually on the packet."""
    h = lsl_hist or {}
    if not (h.get('tx_count') or 0):
        return 'new_applicant'
    last = h.get('last_activity')
    if last:
        try:
            from datetime import date, datetime as _dt
            d = _dt.strptime(str(last)[:10], '%Y-%m-%d').date()
            return 'current_partner' if (date.today() - d).days <= 365 else 'previous_partner'
        except Exception:
            pass
    return 'current_partner'   # has real transactions, date unknown → treat as current


# ── private document storage (NOT under /static) ────────────────────────────
def _save_doc(app_id, which, data_url):
    """Persist a base64 data-url (license / tax-id image or PDF) to a private,
    0600 file under PRIV_DOC_ROOT/<app_id>/. Returns the absolute path or None."""
    if not data_url:
        return None
    media = 'image/jpeg'
    s = data_url
    if isinstance(s, str) and s.startswith('data:'):
        try:
            head, s = s.split(',', 1)
            media = head.split(';')[0].split(':', 1)[1] or media
        except Exception:
            return None
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    if not raw or len(raw) > 18_000_000:           # guard: empty / >18MB
        return None
    ext = {'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
           'image/webp': 'webp', 'application/pdf': 'pdf'}.get(media.lower(), 'bin')
    d = os.path.join(PRIV_DOC_ROOT, str(app_id))
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(PRIV_DOC_ROOT, 0o700)
        os.chmod(d, 0o700)
    except Exception:
        pass
    path = os.path.join(d, '%s.%s' % (which, ext))
    with open(path, 'wb') as f:
        f.write(raw)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _doc_mime(path):
    ext = (path or '').rsplit('.', 1)[-1].lower()
    return {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp',
            'pdf': 'application/pdf'}.get(ext, 'application/octet-stream')


# ── notifications ───────────────────────────────────────────────────────────
def _tg(msg):
    try:
        from app import _tg_worker_alert
        _tg_worker_alert(msg)
    except Exception as e:
        print('[dp-network] tg: %s' % e, flush=True)


def _email(to_addr, subject, html):
    """Best-effort email via the Resend path recon already uses."""
    if not to_addr:
        return
    try:
        from recon_routes import _recon_send_raw
        _recon_send_raw(to_addr, subject, html)
    except Exception as e:
        print('[dp-network] email: %s' % e, flush=True)


def _invite_member(m):
    """Text + email an approved dealer their private portal link (hex token =
    SMS-safe; /d/<token> = encrypted-looking + bookmarkable)."""
    link = '%s/d/%s' % (DP_PUBLIC_BASE.rstrip('/'), m['token'])
    name = _s(m.get('dealership_name')) or 'there'
    phone = _digits(m.get('contact_phone'))
    if len(phone) == 10:
        try:
            from app import send_sms
            send_sms('+1' + phone,
                     "You're approved for the Experience Wholesale dealer "
                     "network. Submit vehicles for a bid here: %s" % link)
        except Exception as e:
            print('[dp-network] invite sms: %s' % e, flush=True)
    _email(_s(m.get('contact_email')),
           'Approved — Experience Wholesale Dealer Network',
           "<p>Welcome to the network, %s.</p>"
           "<p>You're approved to submit vehicles for a wholesale bid. "
           "Use your private link any time:</p>"
           "<p><a href='%s'>%s</a></p>"
           "<p>— Experience Wholesale</p>" % (name, link, link))


# ── auth ────────────────────────────────────────────────────────────────────
def _bad_secret():
    """Return a JSON 401 if X-Auth is wrong, else None. Returning a response
    (not abort) keeps us off the app's HTML error-handler path, which 500s on
    /api/ routes — matches the existing /api/dealerprice/bid pattern."""
    if not SECRET or (request.headers.get('X-Auth') or '').strip() != SECRET:
        return jsonify({'error': 'bad auth'}), 401
    return None


def _reviewer():
    return (session.get('user') or session.get('username')
            or session.get('reviewer') or 'operator')


# ── dashboard nav badge: pending-application count (cached 15s, drift-resistant
#    via @bp.app_context_processor like recon_enabled) ────────────────────────
_PENDING_CACHE = {'t': 0.0, 'n': 0}


@bp.app_context_processor
def _inject_dp_network():
    def dealer_apps_pending():
        now = time.time()
        if now - _PENDING_CACHE['t'] < 15:
            return _PENDING_CACHE['n']
        try:
            db = _db(); cur = db.cursor()
            cur.execute("SELECT count(*) AS n FROM dealer_applications WHERE status='pending'")
            _PENDING_CACHE['n'] = cur.fetchone()['n']
            _PENDING_CACHE['t'] = now
            db.close()
        except Exception:
            pass
        return _PENDING_CACHE['n']
    return {'dealer_apps_pending': dealer_apps_pending}


CLASS_LABELS = {'current_partner': 'Current Partner',
                'previous_partner': 'Previous Partner',
                'new_applicant': 'New Applicant'}


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC API  (dealerprice.net -> EW, shared-secret)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/api/dealerprice/check-existing', methods=['POST'])
def api_dp_check_existing():
    """Q0 existing-dealer typeahead -> candidate roster names."""
    r = _bad_secret()
    if r:
        return r
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'matches': _roster_search(data.get('name') or data.get('q'))})


@bp.route('/api/dealerprice/apply', methods=['POST'])
def api_dp_apply():
    """A dealer applies to the network. Lands as a dealer_applications row for
    operator review. New dealers MUST include license + tax-id (number + image)
    up front. NEVER auto-provisions (impersonation guard) — approval is an
    operator action in /network/applications."""
    r = _bad_secret()
    if r:
        return r
    d = request.get_json(silent=True) or {}

    is_existing = _b(d.get('is_existing'))
    dealership = _s(d.get('dealership_name'))
    cname = _s(d.get('contact_name'))
    cemail = _s(d.get('contact_email')).lower()
    cphone = _digits(d.get('contact_phone'))

    # base requirements for everyone
    miss = [lbl for k, lbl in (('dealership_name', 'Dealership name'),
                               ('contact_name', 'Your name'),
                               ('contact_email', 'Email'),
                               ('contact_phone', 'Mobile')) if not _s(d.get(k))]
    if miss:
        return jsonify({'ok': False, 'error': '%s required.' % ', '.join(miss)}), 400

    # NEW dealers: license + tax-id (number + image) + attestation up front
    if not is_existing:
        if not _s(d.get('license_number')) or not _s(d.get('tax_id')):
            return jsonify({'ok': False, 'error': 'Dealer license number and Tax ID are required.'}), 400
        if not d.get('license_image') or not d.get('taxid_image'):
            return jsonify({'ok': False, 'error': 'A photo of your dealer license and Tax ID / resale certificate is required.'}), 400
        if not _b(d.get('attestation')):
            return jsonify({'ok': False, 'error': 'Please confirm the information is accurate.'}), 400

    types = d.get('dealer_types')
    if isinstance(types, list):
        types = ', '.join(_s(x) for x in types if _s(x))
    else:
        types = _s(types)

    name_match = _roster_match(dealership, cphone)
    lsl_hist = _lsl_history_person(dealership, name_match, _s(d.get('contact_name')), cphone)
    referrer = _s(d.get('referrer_name'))
    referrer_match = _roster_match(referrer) if referrer and referrer.lower() not in ('none', 'n/a') else {}

    # keep an audit copy of the submission WITHOUT the big base64 blobs
    audit = {k: v for k, v in d.items() if k not in ('license_image', 'taxid_image', 'photos')}

    from psycopg2.extras import Json
    db = _db(); cur = db.cursor()
    try:
        # ── DEDUP GUARD (NO_DUPES_2026_07_17) — never create a second
        # application row for the same dealer. Match on normalized dealership
        # name OR phone OR email. An already-APPROVED (or member-provisioned)
        # match wins: return it untouched — don't dupe, don't downgrade. Any
        # non-approved matches (pending/needs_info/rejected) are superseded by
        # this fresh submission and deleted (+ their private doc dirs), so the
        # review queue holds exactly ONE row per dealer, always the latest.
        norm_new = _normalize_name(dealership)
        phone_new = _norm_phone10(cphone)
        cur.execute("SELECT id, status, dealership_name, contact_phone, "
                    "contact_email, member_id FROM dealer_applications")
        approved_hit = None
        dupe_ids = []
        for row in cur.fetchall():
            if not ((norm_new and _normalize_name(row['dealership_name']) == norm_new)
                    or (len(phone_new) == 10 and _norm_phone10(row['contact_phone']) == phone_new)
                    or (cemail and _s(row['contact_email']).lower() == cemail)):
                continue
            if row['status'] == 'approved' or row['member_id']:
                approved_hit = row
            else:
                dupe_ids.append(row['id'])
        if approved_hit:
            db.close()
            return jsonify({'ok': True, 'application_id': approved_hit['id'],
                            'status': approved_hit['status'], 'existing': True,
                            'already': True,
                            'message': 'You already have an account with us — no need to reapply.'}), 200
        for did in dupe_ids:
            cur.execute("DELETE FROM dealer_applications WHERE id=%s "
                        "AND status<>'approved' AND member_id IS NULL", (did,))
            import shutil
            shutil.rmtree(os.path.join(PRIV_DOC_ROOT, str(did)), ignore_errors=True)

        cur.execute("""
            INSERT INTO dealer_applications (
                status, is_existing, dealership_name, dba, dealer_group, franchises,
                entity_type, entity_state, years_in_business, years_at_location,
                units_per_month, units_annual, avg_investment_band, avg_investment_num,
                credit_line, floorplan_provider, floorplan_line, dealer_types,
                primary_makes, price_tier, license_number, license_state, license_exp,
                tax_id, bond_provider, bond_amount, physical_lot, lot_address, website,
                reputation_url, auction_access, payment_ready, bank_reference,
                trade_reference, referrer_name, contact_name, contact_email,
                contact_phone, attestation, tcpa_consent, notes, name_match,
                referrer_match, raw_payload)
            VALUES ('pending',%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,%s)
            RETURNING id
        """, (
            is_existing, dealership, _s(d.get('dba')), _s(d.get('dealer_group')),
            _s(d.get('franchises')), _s(d.get('entity_type')), _s(d.get('entity_state')),
            _int(d.get('years_in_business')), _int(d.get('years_at_location')),
            _int(d.get('units_per_month')), _int(d.get('units_annual')),
            _s(d.get('avg_investment_band')), _num(d.get('avg_investment_num') or d.get('avg_investment')),
            _num(d.get('credit_line')), _s(d.get('floorplan_provider')), _num(d.get('floorplan_line')),
            types, _s(d.get('primary_makes')), _s(d.get('price_tier')),
            _s(d.get('license_number')), _s(d.get('license_state')), _date(d.get('license_exp')),
            _s(d.get('tax_id')), _s(d.get('bond_provider')), _num(d.get('bond_amount')),
            (_b(d.get('physical_lot')) if d.get('physical_lot') is not None else None),
            _s(d.get('lot_address')), _s(d.get('website')), _s(d.get('reputation_url')),
            _s(d.get('auction_access')), _s(d.get('payment_ready')), _s(d.get('bank_reference')),
            _s(d.get('trade_reference')), referrer, cname, cemail, cphone,
            _b(d.get('attestation')), _b(d.get('tcpa_consent')), _s(d.get('notes')),
            Json(name_match or None), Json(referrer_match or None), Json(audit),
        ))
        app_id = cur.fetchone()['id']

        lic = _save_doc(app_id, 'license', d.get('license_image'))
        tax = _save_doc(app_id, 'taxid', d.get('taxid_image'))
        # classify from the VERIFIED ledger (12-month recency), never from
        # self-declaration/roster — see _auto_classify. Operator can override.
        classification = _auto_classify(lsl_hist)
        cur.execute("UPDATE dealer_applications SET license_doc_path=%s, taxid_doc_path=%s, classification=%s, lsl_history=%s WHERE id=%s",
                    (lic, tax, classification, Json(lsl_hist or None), app_id))
        db.commit()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] apply insert: %s' % e, flush=True)
        return jsonify({'ok': False, 'error': 'Could not submit your application — please try again.'}), 500
    db.close()

    tag = 'EXISTING ✓' if is_existing else 'NEW'
    mtag = (' · roster:%s' % name_match['name']) if name_match.get('matched') else ''
    if lsl_hist.get('bought_cars'):
        ltag = '\n📊 LSL: EW bought <b>%d</b> cars from them ($%s)' % (
            lsl_hist['bought_cars'], '{:,.0f}'.format(lsl_hist.get('bought_paid') or 0))
    else:
        ltag = '\n📊 no prior LSL purchase history'
    _tg('🪪 <b>New Dealer-Network application</b> #%d (%s)\n%s%s%s\n%s · %s\nReview: /network/applications'
        % (app_id, tag, dealership or '?', mtag, ltag, cname, cemail or cphone))
    return jsonify({'ok': True, 'application_id': app_id, 'status': 'pending', 'existing': is_existing})


# ════════════════════════════════════════════════════════════════════════════
# OPERATOR REVIEW  (behind app-level require_login; NOT under /api/)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/network/applications')
def network_applications():
    db = _db(); cur = db.cursor()
    cur.execute("""SELECT id, created_at, status, is_existing, dealership_name,
                          dealer_types, units_per_month, avg_investment_band,
                          credit_line, license_number, contact_name, contact_email,
                          contact_phone, name_match, member_id, classification, lsl_history
                     FROM dealer_applications
                    ORDER BY (status='pending') DESC, created_at DESC LIMIT 300""")
    rows = cur.fetchall()
    cur.execute("SELECT status, count(*) AS n FROM dealer_applications GROUP BY status")
    counts = {r['status']: r['n'] for r in cur.fetchall()}
    db.close()
    # Recompute the roster match + unified transaction count LIVE per row so the
    # queue badge is current (the stored lsl_history is deals-only + goes stale
    # as new purchases land). Only for existing/matched dealers — a genuinely
    # new applicant has nothing to look up. tx_count = cars bought + cars sold.
    for r in rows:
        r['tx_count'] = 0
        if not (r.get('is_existing') or r.get('name_match')):
            continue                      # genuinely-new applicant — nothing to look up
        m = _roster_match(r['dealership_name'], r.get('contact_phone'))
        h = _lsl_history_person(r['dealership_name'], m, r.get('contact_name'), r.get('contact_phone'))
        r['tx_count'] = (h or {}).get('tx_count') or 0
        if m:
            r['name_match'] = m
    return render_template('network/applications.html', rows=rows, counts=counts,
                           types=DEALER_TYPES, class_labels=CLASS_LABELS)


@bp.route('/network/members')
def network_members():
    """Onboarded-dealer roster + their bid activity (the per-dealer tracking)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT m.*,
                          (SELECT count(*) FROM bids b WHERE b.dp_member_id=m.id) AS bid_count,
                          (SELECT max(b.created_at) FROM bids b WHERE b.dp_member_id=m.id) AS last_bid
                         FROM dealerprice_members m
                        ORDER BY m.approved_at DESC LIMIT 500""")
        rows = cur.fetchall()
    except Exception as e:
        print('[dp-network] members list: %s' % e, flush=True)
        cur.execute("SELECT m.*, 0 AS bid_count, NULL AS last_bid FROM dealerprice_members m ORDER BY approved_at DESC LIMIT 500")
        rows = cur.fetchall()
    db.close()
    return render_template('network/members.html', rows=rows)


@bp.route('/network/application/<int:app_id>')
def network_application(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    member = None
    if a.get('member_id'):
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (a['member_id'],))
        member = cur.fetchone()
    db.close()
    member_bids = _member_bids(member['id']) if member else []
    # Re-run the match live so matcher improvements apply retroactively to old
    # applications without a backfill migration; then feed the resolved
    # supplier_id into the history lookup so it can read the payments ledger.
    a['name_match'] = _roster_match(a.get('dealership_name'), a.get('contact_phone')) or a.get('name_match')
    lsl_hist = _lsl_history_person(a.get('dealership_name'), a.get('name_match'), a.get('contact_name'), a.get('contact_phone'))
    return render_template('network/application.html', a=a, member=member, member_bids=member_bids,
                           class_labels=CLASS_LABELS, lsl_hist=lsl_hist)


@bp.route('/network/application/<int:app_id>/doc/<which>')
def network_application_doc(app_id, which):
    """Serve the PRIVATE license / tax-id file. Operator-only (require_login)."""
    if which not in ('license', 'taxid'):
        abort(404)
    db = _db(); cur = db.cursor()
    cur.execute("SELECT license_doc_path, taxid_doc_path FROM dealer_applications WHERE id=%s", (app_id,))
    r = cur.fetchone(); db.close()
    if not r:
        abort(404)
    path = r['license_doc_path'] if which == 'license' else r['taxid_doc_path']
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=_doc_mime(path),
                     download_name='%s-%s.%s' % (which, app_id, path.rsplit('.', 1)[-1]))


@bp.route('/network/application/<int:app_id>/approve', methods=['POST'])
def network_application_approve(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    if a.get('member_id'):
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    from psycopg2.extras import Json
    # hex token (no -/_): survives SMS auto-linkifiers + looks like a secure key
    token = secrets.token_hex(16)
    try:
        cur.execute("""INSERT INTO dealerprice_members
                         (application_id, dealership_name, contact_name, contact_email,
                          contact_phone, token, is_existing, lsl_match, approved_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (app_id, a['dealership_name'], a['contact_name'], a['contact_email'],
                     a['contact_phone'], token, a['is_existing'],
                     Json(a.get('name_match') or None), _reviewer()))
        member_id = cur.fetchone()['id']
        cur.execute("""UPDATE dealer_applications SET status='approved', member_id=%s,
                          reviewer=%s, reviewed_at=now(),
                          review_notes=COALESCE(%s, review_notes) WHERE id=%s""",
                    (member_id, _reviewer(), _s(request.form.get('review_notes')) or None, app_id))
        db.commit()
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (member_id,))
        m = cur.fetchone()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] approve: %s' % e, flush=True)
        abort(500)
    db.close()
    try:
        _invite_member(m)
    except Exception as e:
        print('[dp-network] approve invite: %s' % e, flush=True)
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/reject', methods=['POST'])
def network_application_reject(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("""UPDATE dealer_applications SET status='rejected', reviewer=%s,
                      reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                    WHERE id=%s""",
                (_reviewer(), _s(request.form.get('review_notes')) or None, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/needs-info', methods=['POST'])
def network_application_needs_info(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("""UPDATE dealer_applications SET status='needs_info', reviewer=%s,
                      reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                    WHERE id=%s""",
                (_reviewer(), _s(request.form.get('review_notes')) or None, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/classify', methods=['POST'])
def network_application_classify(app_id):
    """Operator override of the auto-classification."""
    c = _s(request.form.get('classification'))
    if c not in CLASS_LABELS:
        c = None
    db = _db(); cur = db.cursor()
    cur.execute("UPDATE dealer_applications SET classification=%s WHERE id=%s", (c, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


# ── member token lookup / per-dealer bids ───────────────────────────────────
def _member_by_token(token, touch=False, count_submit=False):
    """Active member row for a token, or None. touch=update last_used_at;
    count_submit=also bump submit_count (used on the /bid path)."""
    token = _s(token)
    if not token:
        return None
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM dealerprice_members WHERE token=%s AND status='active' LIMIT 1", (token,))
        m = cur.fetchone()
        if m and (touch or count_submit):
            if count_submit:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now(), submit_count=submit_count+1 WHERE id=%s", (m['id'],))
            else:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now() WHERE id=%s", (m['id'],))
            db.commit()
        return m
    except Exception as e:
        print('[dp-network] member lookup: %s' % e, flush=True)
        return None
    finally:
        db.close()


def validate_member_token(token):
    """For the /bid path: validate the token + count a submit. Returns row|None."""
    return _member_by_token(token, count_submit=True)


def _member_bids(member_id, limit=200):
    """All EW bids tagged to this network member (newest first)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT id, year, make, model, trim, mileage, status, ai_price, created_at
                         FROM bids WHERE dp_member_id=%s ORDER BY id DESC LIMIT %s""", (member_id, limit))
        return cur.fetchall()
    except Exception as e:
        print('[dp-network] member_bids: %s' % e, flush=True)
        return []
    finally:
        db.close()


@bp.route('/api/dealerprice/member', methods=['GET', 'POST'])
def api_dp_member():
    """Validate a member token -> member info, for the /access magic link and
    the pre-filled Get-a-Bid form. Shared-secret; never exposes the token."""
    r = _bad_secret()
    if r:
        return r
    token = request.args.get('token') or (request.get_json(silent=True) or {}).get('token')
    m = _member_by_token(token, touch=True)
    if not m:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'member': {
        'member_id': m['id'],
        'dealership_name': m['dealership_name'],
        'contact_name': m['contact_name'],
        'contact_email': m['contact_email'],
        'contact_phone': m['contact_phone'],
    }})
