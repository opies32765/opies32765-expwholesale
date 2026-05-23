"""Probe candidate saved-appraisal list endpoints with our cookie pool."""
from vauto_enrichment import _get_jar
import requests
jar = _get_jar()
hdrs = jar.get_headers()
cookies = jar.get_cookies()
BFF = "https://provision.vauto.app.coxautoinc.com"

candidates = [
    "/api/appraisal/list",
    "/api/appraisal/list?pageSize=20&activePage=1",
    "/api/appraisal/search?pageSize=20",
    "/api/appraisalGrid/search",
    "/api/appraisal/all",
    "/api/appraisal/recent",
    "/api/appraisals",
    "/api/appraisal/saved",
    "/api/appraisal/savedList",
    "/Va/Appraisal/List.aspx/GetAppraisals",
    "/api/saved-appraisals",
    "/api/inventory/list",
    "/api/appraisalQuery",
]
for path in candidates:
    try:
        r = requests.get(BFF + path, headers=hdrs, cookies=cookies, timeout=5)
        ct = r.headers.get("content-type", "")
        body_preview = r.text[:100]
        print(f"GET  {path}  ->  {r.status_code}  {ct[:30]}  body={body_preview!r}")
    except Exception as e:
        print(f"GET  {path}  ->  ERR {e}")

# Also try POST since ExtJS grids often use POST
print("---POST---")
for path in [
    "/api/appraisal/search",
    "/api/appraisal/list",
    "/api/appraisalGrid/search",
    "/api/appraisal/query",
]:
    try:
        r = requests.post(BFF + path,
                          json={"pageSize": 20, "activePage": 1},
                          headers=hdrs, cookies=cookies, timeout=5)
        ct = r.headers.get("content-type", "")
        body_preview = r.text[:100]
        print(f"POST {path}  ->  {r.status_code}  {ct[:30]}  body={body_preview!r}")
    except Exception as e:
        print(f"POST {path}  ->  ERR {e}")
