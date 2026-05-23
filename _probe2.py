"""Broader probe — try /Va/ paths + various ExtJS patterns."""
from vauto_enrichment import _get_jar
import requests
jar = _get_jar()
hdrs = jar.get_headers()
cookies = jar.get_cookies()

# Loose 'application/json' Accept so we don't get HTML auth wrappers
hdrs2 = dict(hdrs)
hdrs2["Accept"] = "application/json"

# Use the base provision host
BFFS = [
    "https://provision.vauto.app.coxautoinc.com",
    "https://bff.vaweb.vauto.app.coxautoinc.com",
]
candidates = [
    "/Va/Appraisal/List",
    "/Va/Appraisal/Grid",
    "/Va/Appraisal/Data",
    "/Va/Appraisal/Search",
    "/Va/Reports/Appraisal",
    "/Va/Reports/AppraisalList",
    "/Va/AppraisalService.svc/List",
    "/Va/Services/AppraisalService.svc/List",
    "/Va/Provision/Appraisal/List",
    "/Va/Reports/AppraisalListing",
    "/Va/Appraisal/AppraisalListAjax",
    "/Va/Reports/AppraisalReports/List",
    "/Va/Provision/Service.svc/GetSavedAppraisals",
    "/api/Va/Appraisal/List",
    "/api/reports/appraisalList",
    "/api/reports/appraisals",
]
for bff in BFFS:
    print(f"=== {bff} ===")
    for path in candidates:
        try:
            r = requests.get(bff + path, headers=hdrs2, cookies=cookies, timeout=4,
                             allow_redirects=False)
            ct = r.headers.get("content-type", "")
            body_preview = r.text[:80].replace("\n", " ")
            print(f"GET  {path}  -> {r.status_code}  {ct[:25]}  {body_preview!r}")
        except Exception as e:
            print(f"GET  {path}  -> ERR {type(e).__name__}")
