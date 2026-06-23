"""accutrade_api.py - server-side AccuTrade valuation via the sirius/hercules API.
Replaces the fragile browser DOM scrape. Discovered + validated 2026-06-22.

Auth: header `token: <sirius_key>` (accutrade_auth.get_key) + REQUIRED query params
      ?u=<email>&is_mobile=false&client_platform=chrome  on EVERY call.
Flow: vehicleByVIN -> pick gid -> create appraisal (full template) -> hercules
      pricing-test -> read accuprice. Value map: guaranteed_offer<-trade,
      trade_in<-market, retail<-targetRetail (verified vs EW DB).

Create/pricing use captured full payload templates (accu_templates.json) because
AccuTrade's CAPL backend 500s on a sparse create. NOT wired into the pipeline.
Run standalone:  python accutrade_api.py <VIN> <MILES>
"""
import os, json, copy, subprocess
import accutrade_auth as auth

EMAIL = os.environ.get("ACCUTRADE_EMAIL", "opies32765@gmail.com")
DEALER_ZIP = os.environ.get("ACCUTRADE_ZIP", "33308")
QP = "?u=" + EMAIL.replace("@", "%40") + "&is_mobile=false&client_platform=chrome"
_TPL = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "accu_templates.json")))

# value/identity fields nulled on create so CAPL recomputes fresh for the new VIN
_CREATE_NULL = ("originalOffer", "trade", "market", "targetRetail", "vehicleBasePrice",
                "vehicleMarketBasePrice", "high_offer", "low_offer", "initial_offer_price",
                "report_id", "user_offer_price")


def _curl(key, method, base, path, payload=None, timeout=40):
    h = auth.api_headers(key)
    args = ["curl", "-s", "--http2", "--compressed", "-m", str(timeout),
            "-X", method, "-w", "\n__C__%{http_code}", base + path]
    for k, v in h.items():
        args += ["-H", "%s: %s" % (k, v)]
    if payload is not None:
        args += ["-H", "content-type: application/json", "--data-binary", json.dumps(payload)]
    ck = auth.load_pool_cookies()
    if ck:
        args += ["-b", "; ".join("%s=%s" % (k, v) for k, v in ck.items())]
    out = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
    body, _, code = out.stdout.rpartition("__C__")
    body = body[:-1] if body.endswith("\n") else body
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, out.stdout


def _json(b):
    try: return json.loads(b)
    except Exception: return None


def vehicle_by_vin(key, vin):
    st, b = _curl(key, "GET", auth.SIRIUS, "/vehicleByVIN/" + vin + QP)
    return st, _json(b)


def create_appraisal(key, veh, vin, odometer):
    """veh = a vehicleByVIN entry {gid, year, make, model, style}."""
    p = copy.deepcopy(_TPL["create"])
    gid = veh["gid"]
    p.update({"guid": gid, "gid": gid, "id": None, "vin": vin,
              "year": veh.get("year"), "make": veh.get("make"), "model": veh.get("model"),
              "style": veh.get("style"), "odometer": odometer, "userSetOdometer": 1, "is_miles": 1})
    for f in _CREATE_NULL:
        if f in p: p[f] = None
    p["reports"] = []
    st, b = _curl(key, "POST", auth.SIRIUS, "/accuprice/appraisal" + QP, p)
    return st, _json(b)


def pricing_test(key, gid, vin, appraisal_id, miles, zip_=None):
    p = copy.deepcopy(_TPL["pricing"])
    p.update({"appraisal": appraisal_id, "mileage": miles, "region": zip_ or DEALER_ZIP})
    st, b = _curl(key, "POST", auth.HERCULES,
                  "/api/vehicles/%s/%s/pricing-test/" % (gid, vin), p)
    return st, b


def read_appraisal(key, appraisal_id):
    st, b = _curl(key, "GET", auth.SIRIUS, "/accuprice/appraisal/%s" % appraisal_id + QP)
    d = _json(b)
    row = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else None)
    return st, row


def value_vin(vin, miles, zip_=None, gid=None, trim_pick=None):
    key = auth.get_key()
    out = {"vin": vin, "miles": miles}
    veh = None
    st, vlist = vehicle_by_vin(key, vin)
    if st != 200 or not vlist:
        return {**out, "error": "vin_decode_failed", "status": st}
    out["trim_choices"] = [{"gid": v.get("gid"), "style": v.get("style"),
                            "ymm": "%s %s %s" % (v.get("year"), v.get("make"), v.get("model"))}
                           for v in vlist]
    if gid is not None:
        veh = next((v for v in vlist if v.get("gid") == gid), vlist[0])
    elif len(vlist) > 1 and trim_pick:
        gid = trim_pick(vlist); veh = next((v for v in vlist if v.get("gid") == gid), vlist[0])
    else:
        veh = vlist[0]; gid = veh["gid"]
    out["gid"] = gid
    st, cr = create_appraisal(key, veh, vin, miles)
    if st != 200 or not isinstance(cr, dict) or not cr.get("id"):
        return {**out, "error": "create_failed", "status": st, "resp": cr}
    aid = cr["id"]
    out["appraisal_id"] = aid
    out["appraisal_url"] = "https://appraiser3.accu-trade.com/appraisal/%s" % aid
    out["pricing_status"], pbody = pricing_test(key, gid, vin, aid, miles, zip_)
    rst, row = read_appraisal(key, aid)
    if rst == 200 and row:
        out["guaranteed_offer"] = row.get("trade")
        out["trade_in"] = row.get("market")
        out["retail"] = row.get("targetRetail")
        out["odometer"] = row.get("odometer")
    else:
        out["error"] = "read_failed"; out["read_status"] = rst
    return out


if __name__ == "__main__":
    import sys
    vin = sys.argv[1] if len(sys.argv) > 1 else "1FTFW6LD8SFC49480"
    miles = int(sys.argv[2]) if len(sys.argv) > 2 else 15000
    print(json.dumps(value_vin(vin, miles), indent=2, default=str))
