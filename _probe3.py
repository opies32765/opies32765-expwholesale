"""Use the criteria shape from the URL fragment to POST to likely endpoints.
ExtJS grids typically expect the criteria object in the body."""
from vauto_enrichment import _get_jar
import requests
import json

jar = _get_jar()
hdrs = dict(jar.get_headers())
hdrs["Accept"] = "application/json"
hdrs["Content-Type"] = "application/json"
cookies = jar.get_cookies()

# Criteria shape from the URL fragment user shared
criteria = {
    "_pageSize": 20,
    "_sortBy": "AppraisalLastModified DESC",
    "sorts": [{"sort": "AppraisalLastModified", "dir": "DESC"}],
    "LastModifiedDaySpan": None,
    "_mandatoryFilters": [],
    "QuickSearch": "",
}
# Same payload pageData
pageData = {"total": 0, "activePage": 1, "pages": 0, "pageSize": 20}

BFF = "https://provision.vauto.app.coxautoinc.com"
candidates = [
    # ExtJS service endpoints
    ("POST", "/Va/Appraisal/AppraisalListService.svc/GetAppraisals"),
    ("POST", "/Va/Services/AppraisalListService.svc/GetAppraisals"),
    ("POST", "/Provision/Appraisal/AppraisalListService.svc/GetAppraisals"),
    ("POST", "/Va/Services/AppraisalService.svc/GetAppraisals"),
    # ASPX page methods
    ("POST", "/Va/Appraisal/List.aspx/GetAppraisals"),
    ("POST", "/Va/Appraisal/List.aspx/GetData"),
    ("POST", "/Va/Appraisal/List.aspx/List"),
    # JSON RPC variants
    ("POST", "/Va/Services/Inventory/AppraisalGrid"),
    ("POST", "/Va/Reports/AppraisalReports"),
    # API-style guesses
    ("POST", "/Provision/api/appraisal/list"),
    ("POST", "/Provision/api/appraisal/search"),
    # OData-style
    ("GET",  "/odata/Appraisals?$top=20&$orderby=AppraisalLastModified%20desc"),
]
for method, path in candidates:
    try:
        if method == "POST":
            r = requests.post(BFF + path, headers=hdrs, cookies=cookies,
                              json={"criteria": criteria, "pageData": pageData},
                              timeout=5, allow_redirects=False)
        else:
            r = requests.get(BFF + path, headers=hdrs, cookies=cookies,
                             timeout=5, allow_redirects=False)
        ct = r.headers.get("content-type", "")
        body_preview = r.text[:100].replace("\n", " ")
        print(f"{method} {path:60s} -> {r.status_code}  {ct[:30]}  {body_preview!r}")
    except Exception as e:
        print(f"{method} {path:60s} -> ERR {type(e).__name__}")
