#!/usr/bin/env python3
"""EW's DealerAgent poster. v1 (DEALERAGENT_V1_2026_07_05)

EW's seller-side agent: auto-posts EVERY fully-enriched bid to the DealerAgent
network (dealeragent.net). Strictly a DOWNSTREAM CONSUMER of the bid pipeline —
read-only against the EW DB, triggered off bids.all_enriched_at. It can never
block, gate, or delay enrichment or the assessment (hard rule).

Runs from cron every 2 min under flock. State (what's been posted, content
sig) in a local JSON file, so re-posts only happen when the bid changed.
Dead/duplicate bids get withdrawn from the network. The 9B writes the one-line
dealer-facing blurb (deterministic fallback — a brain outage never stops a post).

Config: /opt/expwholesale/dealeragent_poster.env  (DA_URL, DA_KEY)
9B:     /etc/ew-brain.env                         (EW_BRAIN_URL, EW_BRAIN_KEY)
"""
import os, json, hashlib, subprocess, time
import requests

ENVFILE = "/opt/expwholesale/dealeragent_poster.env"
BRAINENV = "/etc/ew-brain.env"
STATE = "/opt/expwholesale/dealeragent_poster_state.json"
PHOTO_BASE = "https://experience-wholesale.net/p"
LOOKBACK_DAYS = 14
DEAD_STATUSES = ("dead", "duplicate", "deleted", "archived")


def _envfile(path):
    out = {}
    try:
        with open(path) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


CFG = _envfile(ENVFILE)
BRAIN = _envfile(BRAINENV)
DA_URL = (CFG.get("DA_URL") or "").rstrip("/")
HDRS = {"X-Agent-Key": CFG.get("DA_KEY", "")}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def psql_json(sql):
    """Read-only query via local psql as postgres; returns list of dicts."""
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-p", "5433", "-d", "expwholesale",
         "-tA", "-c", f"SELECT COALESCE(json_agg(t), '[]'::json) FROM ({sql}) t;"],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:400])
    return json.loads(out.stdout.strip() or "[]")


def fetch_bids():
    return psql_json(f"""
        SELECT b.id, b.vin, b.year, b.make, b.model, b.trim, b.mileage, b.color,
               b.status, b.network_ask,
               (SELECT json_agg(p.id ORDER BY p.id)
                  FROM (SELECT id FROM bid_photos
                         WHERE bid_id = b.id AND is_car IS NOT FALSE
                         ORDER BY id LIMIT 6) p) AS photo_ids
          FROM bids b
         WHERE b.all_enriched_at IS NOT NULL
           AND b.created_at > now() - interval '{LOOKBACK_DAYS} days'
         ORDER BY b.id
    """)


def sig(b):
    keys = ("vin", "year", "make", "model", "trim", "mileage", "color",
            "status", "network_ask", "photo_ids")
    return hashlib.sha1(json.dumps({k: b.get(k) for k in keys},
                                   sort_keys=True, default=str).encode()).hexdigest()


def blurb_for(b):
    car = f"{b.get('year') or ''} {b.get('make') or ''} {b.get('model') or ''} {b.get('trim') or ''}".strip()
    miles = f"{int(b['mileage']):,} miles" if b.get("mileage") else "mileage TBD"
    fallback = f"{car} — {miles}. Fresh on the EW desk, fully worked up."
    url = (BRAIN.get("EW_BRAIN_URL") or "https://brain.experience-wholesale.net").rstrip("/")
    key = BRAIN.get("EW_BRAIN_KEY")
    if not key:
        return fallback
    try:
        r = requests.post(
            f"{url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "Mozilla/5.0 (compatible; dealeragent/1.0)"},
            json={"model": "ew-brain", "temperature": 0.4, "max_tokens": 70,
                  "chat_template_kwargs": {"enable_thinking": False},
                  "messages": [
                      {"role": "system", "content":
                       "You write ONE short punchy line (max 16 words) describing a "
                       "wholesale car to dealer buyers. No price, no emojis, no hype "
                       "words like 'stunning'. Just what a buyer cares about."},
                      {"role": "user", "content": f"{car}, {miles}, color {b.get('color') or 'n/a'}"}]},
            timeout=20)
        txt = (r.json()["choices"][0]["message"]["content"] or "").strip().split("\n")[0].strip('"')
        return txt if 8 < len(txt) < 140 else fallback
    except Exception as e:
        log(f"brain fallback bid {b['id']}: {e}")
        return fallback


def post_listing(b, blurb):
    photos = [f"{PHOTO_BASE}/{pid}/strip" for pid in (b.get("photo_ids") or [])]
    status = "withdrawn" if (b.get("status") or "") in DEAD_STATUSES else "live"
    payload = {
        "ext_ref": f"ew:{b['id']}",
        "vin": b.get("vin"), "year": b.get("year"), "make": b.get("make"),
        "model": b.get("model"), "trim": b.get("trim"),
        "mileage": b.get("mileage"), "color": b.get("color"),
        "location": "EW network",
        "ask": b.get("network_ask"),
        "blurb": blurb, "photos": photos, "anchors": {}, "status": status,
    }
    r = requests.post(f"{DA_URL}/api/listings", headers=HDRS, json=payload, timeout=20)
    r.raise_for_status()
    return status


def main():
    if not DA_URL or not HDRS["X-Agent-Key"]:
        log("no DA_URL/DA_KEY configured — exiting")
        return
    try:
        with open(STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {"sig": {}}
    bids = fetch_bids()
    posted = 0
    for b in bids:
        bid_id, s = str(b["id"]), sig(b)
        if st["sig"].get(bid_id) == s:
            continue
        try:
            # blurb only recomputed when the bid is new (keep 9B load tiny)
            blurb = st.get("blurb", {}).get(bid_id) or blurb_for(b)
            status = post_listing(b, blurb)
            st["sig"][bid_id] = s
            st.setdefault("blurb", {})[bid_id] = blurb
            posted += 1
            log(f"posted ew:{bid_id} ({b.get('year')} {b.get('make')} {b.get('model')}) status={status}")
        except Exception as e:
            log(f"post failed ew:{bid_id}: {e}")
    if posted:
        with open(STATE, "w") as fh:
            json.dump(st, fh)
    log(f"run done — {len(bids)} enriched bids in window, {posted} posted/updated")


if __name__ == "__main__":
    main()
