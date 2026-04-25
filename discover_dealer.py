"""
AI-driven dealer adapter discovery.

When a new dealer is added with platform=NULL (no fingerprint match in
detect_platform), spawn Claude Opus 4.7 with a fetch_url tool. The model
investigates the site, finds where inventory lives (HTML grid, JSON API,
__NEXT_DATA__ blob, sitemap, etc.) and returns a JSON config that the
generic config-driven extractor in dealer_scanner.py can execute.

Run:
    python discover_dealer.py --dealer-id 4
    python discover_dealer.py --auto    # process all dealers with platform IS NULL

Cost: ~$1-3 per dealer (Opus 4.7, ~50-150K input tokens, multi-turn).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import psycopg2
import psycopg2.extras
import requests

try:
    from anthropic import Anthropic
except ImportError:
    print("Install: pip install anthropic", file=sys.stderr)
    sys.exit(1)

DB_URL = os.environ.get("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("DISCOVERY_MODEL", "claude-opus-4-5")  # 4.7 alias maps server-side
MAX_TURNS = 8
MAX_TOKENS_PER_RESPONSE = 4096
FETCH_BUDGET_BYTES = 250_000   # cap each tool fetch to keep token use predictable
TOOL_RESULT_CAP_CHARS = 35_000 # trim each tool result before adding to context

# Pricing for cost tracking (Opus 4.x: $15/M input, $75/M output)
INPUT_PRICE_PER_MTOK = 15.00
OUTPUT_PRICE_PER_MTOK = 75.00


def db_conn():
    return psycopg2.connect(DB_URL)


# ─── Deterministic auto-detection (NEXT_DATA / NUXT / Apollo) ────────────
# Tries to build a scrape_config WITHOUT calling the AI. Handles ~90% of
# modern SPA dealer sites (Next.js especially) for free.
VEHICLE_FIELD_HINTS = {
    "vin":      ["vin", "VIN", "vehicleVin", "stockVin"],
    "year":     ["year", "modelYear", "vehicleYear"],
    "make":     ["make", "manufacturer", "brand", "vehicleMake"],
    "model":    ["model", "carName", "modelName", "vehicleModel"],
    "trim":     ["trim", "trimLevel", "engine"],
    "price":    ["price", "priceUsd", "salesPrice", "askingPrice", "msrp"],
    "miles":    ["miles", "mileage", "odometer", "vehicleMileage"],
    "stock_number":   ["stock", "stockNumber", "stockId"],
    "exterior_color": ["exteriorColor", "extColor", "color"],
    "interior_color": ["interiorColor", "intColor", "trimColor"],
}


def _walk_for_vehicle_arrays(obj, path="$", depth=0, max_depth=10, found=None):
    """Walk a JSON tree, collect arrays whose first element looks like a vehicle."""
    if found is None:
        found = []
    if depth > max_depth:
        return found
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        first = obj[0]
        keys = set(first.keys())
        score = 0
        if any(h in keys for h in VEHICLE_FIELD_HINTS["vin"]):
            score += 10  # VIN is the killer signal
        if any(h in keys for h in VEHICLE_FIELD_HINTS["year"]):  score += 1
        if any(h in keys for h in VEHICLE_FIELD_HINTS["make"]):  score += 1
        if any(h in keys for h in VEHICLE_FIELD_HINTS["model"]): score += 1
        if any(h in keys for h in VEHICLE_FIELD_HINTS["price"]): score += 1
        if score >= 8:
            found.append({"score": score, "count": len(obj),
                          "path": path + "[*]", "sample_keys": list(keys)[:25],
                          "sample": first})
        # Don't recurse into the array — first hit wins for this branch
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_for_vehicle_arrays(v, f"{path}.{k}", depth + 1, max_depth, found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            _walk_for_vehicle_arrays(v, f"{path}[{i}]", depth + 1, max_depth, found)
    return found


def _build_field_map(sample):
    """Given a sample vehicle dict, build a fields map by matching common
    field-name synonyms. Returns dict suitable for scrape_config.extraction.fields."""
    fields = {}
    for our_name, hints in VEHICLE_FIELD_HINTS.items():
        for h in hints:
            if h in sample:
                fields[our_name] = h
                break
    # Handle nested model object e.g. {"model": {"name": "SF90"}}
    if "model" in fields:
        m = sample.get(fields["model"])
        if isinstance(m, dict):
            for k in ("name", "displayName", "label"):
                if k in m:
                    fields["model"] = f"{fields['model']}.{k}"
                    break
    return fields


def auto_detect_from_html(html, dealer_url):
    """Try to build a scrape_config from a static HTML fetch by inspecting
    __NEXT_DATA__, __NUXT_DATA__, or Apollo state. Returns (config, info_str)
    or (None, reason)."""
    if not html:
        return None, "empty html"
    sources = []
    nd = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if nd:
        try:
            sources.append(("next_data", "$", json.loads(nd.group(1))))
        except Exception:
            pass
    nu = re.search(r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if nu:
        try:
            sources.append(("next_data", "$", json.loads(nu.group(1))))  # reuse next_data extractor
        except Exception:
            pass
    apollo = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});', html, re.DOTALL)
    if apollo:
        try:
            sources.append(("next_data", "$", json.loads(apollo.group(1))))
        except Exception:
            pass

    if not sources:
        return None, "no embedded SPA state found (no NEXT_DATA / NUXT / Apollo)"

    # Walk each source for vehicle-shaped arrays
    best = None
    for src_type, root_path, data in sources:
        candidates = _walk_for_vehicle_arrays(data)
        for c in candidates:
            if best is None or c["score"] > best["score"] or \
               (c["score"] == best["score"] and c["count"] > best["count"]):
                best = {**c, "src_type": src_type}

    if not best:
        return None, "found embedded JSON but no vehicle-shaped arrays inside"

    fields = _build_field_map(best["sample"])
    if "vin" not in fields:
        return None, f"detected array at {best['path']} but no VIN field"

    # Detect single-brand sites — if every URL contains a brand name and
    # samples don't have a "make" field, hardcode the make.
    if "make" not in fields:
        for brand in ("Ferrari", "Porsche", "Lamborghini", "Bentley", "Rolls-Royce",
                      "Maserati", "Aston Martin", "McLaren", "BMW", "Mercedes-Benz",
                      "Audi", "Lexus"):
            if brand.lower() in dealer_url.lower():
                fields["make"] = {"literal": brand}
                break

    config = {
        "version": 1,
        "fetch_strategy": "static",
        "inventory_source": {
            "type": best["src_type"],
            "url_template": "{base}",
            "method": "GET",
            "pagination": {"type": "none"}
        },
        "extraction": {
            "list_path": best["path"],
            "fields": fields,
        },
        "confidence": "high" if best["score"] >= 12 else "medium",
        "discovered_by": "deterministic_next_data",
        "discovered_at": datetime.now().isoformat(),
        "notes": f"auto-detected from embedded SPA state. "
                 f"score={best['score']}, count={best['count']}, "
                 f"path={best['path']}, sample_keys={best['sample_keys'][:10]}",
    }
    return config, f"auto-detected {best['count']} vehicles at {best['path']} (score {best['score']})"


# ─── HTTP fetch tool exposed to the model ─────────────────────────────────
def _strip_html(html):
    """Aggressively strip HTML to save tokens. Drops <script>, <style>, <svg>,
    HTML comments, base64 images, and most attributes — keeps __NEXT_DATA__
    blob, link rel=, all anchor hrefs, and text content."""
    if not html:
        return html
    # Preserve __NEXT_DATA__ and __NUXT_DATA__ blobs (they're our gold)
    next_data = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    nuxt_data = re.search(r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    apollo = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});', html, re.DOTALL)
    keepers = []
    if next_data:
        keepers.append(("__NEXT_DATA__", next_data.group(1)[:200_000]))
    if nuxt_data:
        keepers.append(("__NUXT_DATA__", nuxt_data.group(1)[:200_000]))
    if apollo:
        keepers.append(("__APOLLO_STATE__", apollo.group(1)[:200_000]))
    # Strip
    s = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    s = re.sub(r'<script\b[^>]*>.*?</script>', '', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'<style\b[^>]*>.*?</style>', '', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'<svg\b.*?</svg>', '<svg/>', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'data:image/[^"\']{50,}', 'data:image/...', s)
    # Strip noisy class= and style= attributes
    s = re.sub(r'\s(class|style|aria-[a-z-]+|data-[a-z-]+)="[^"]{30,}"', '', s)
    # Compress whitespace
    s = re.sub(r'\n\s*\n+', '\n', s)
    s = re.sub(r' +', ' ', s)
    out = s
    if keepers:
        out += "\n\n<!-- preserved blobs -->\n"
        for name, content in keepers:
            out += f"\n=== {name} ===\n{content}\n"
    return out


def fetch_url(url, max_bytes=FETCH_BUDGET_BYTES):
    """Fetch a URL, return text body (stripped + truncated)."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        }
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        body = r.text or ""
        ctype = r.headers.get("content-type", "")
        original_size = len(r.content)
        # Strip HTML to save tokens (keeps __NEXT_DATA__ etc).
        if "html" in ctype.lower() or "<html" in body[:500].lower():
            body = _strip_html(body)
        if len(body.encode("utf-8")) > max_bytes:
            body = body.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            body += "\n\n[TRUNCATED — fetch a more specific URL if you need more]"
        return {
            "status": r.status_code,
            "url": r.url,
            "content_type": ctype,
            "original_size_bytes": original_size,
            "body": body,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def extract_jsonpath_hint(body, max_chars=800):
    """If body looks like JSON, return a tiny structural sketch to help the
    agent find the right path without re-reading the full payload."""
    try:
        data = json.loads(body)
    except Exception:
        return None
    def sketch(obj, depth=0):
        if depth > 3:
            return "…"
        if isinstance(obj, dict):
            keys = list(obj.keys())[:8]
            return "{" + ", ".join(f"{k}: {sketch(obj[k], depth+1)}" for k in keys) + "}"
        if isinstance(obj, list):
            n = len(obj)
            if n == 0:
                return "[]"
            return f"[{n} items, first: {sketch(obj[0], depth+1)}]"
        if isinstance(obj, str):
            return f'"{obj[:30]}"' if len(obj) > 30 else f'"{obj}"'
        return str(obj)
    return sketch(data)[:max_chars]


# ─── Tool schema for the model ────────────────────────────────────────────
TOOLS = [
    {
        "name": "fetch_url",
        "description": (
            "Fetch any URL on the dealer's site (homepage, inventory page, "
            "an /api/* endpoint, sitemap.xml, etc) and return its body. "
            "Use this to investigate where vehicle inventory data lives. "
            "Body is truncated to ~600KB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL to fetch"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "submit_config",
        "description": (
            "Submit your final scrape config for this dealer. Call this ONCE "
            "when you've identified how to extract the inventory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fetch_strategy": {
                    "type": "string",
                    "enum": ["static", "playwright"],
                    "description": "static = plain HTTP fetch works; playwright = needs JS rendering",
                },
                "inventory_source": {
                    "type": "object",
                    "description": "How to fetch the inventory listing",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["api_json", "html_listing", "next_data", "sitemap"],
                        },
                        "url_template": {"type": "string"},
                        "method": {"type": "string", "default": "GET"},
                        "headers": {"type": "object"},
                        "body_template": {"type": "string"},
                        "pagination": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["page_param", "offset", "next_link", "none"]},
                                "param": {"type": "string"},
                                "start": {"type": "integer"},
                                "step": {"type": "integer"},
                                "max_pages": {"type": "integer"},
                            },
                        },
                    },
                    "required": ["type", "url_template"],
                },
                "extraction": {
                    "type": "object",
                    "description": "How to extract vehicle records from each fetched response",
                    "properties": {
                        "list_path": {
                            "type": "string",
                            "description": (
                                "JSONPath (e.g. $.results[*]) for api_json/next_data, "
                                "or CSS selector (e.g. div.vehicle-card) for html_listing"
                            ),
                        },
                        "fields": {
                            "type": "object",
                            "description": (
                                "Map of vehicle field name to extraction path. "
                                "Required: vin, year, make, model, price. "
                                "Optional: trim, miles, stock_number, url, photos, exterior_color, interior_color."
                            ),
                        },
                    },
                    "required": ["list_path", "fields"],
                },
                "notes": {"type": "string", "description": "Human-readable notes about quirks, gotchas, manual review needed"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["fetch_strategy", "inventory_source", "extraction", "confidence"],
        },
    },
]


SYSTEM_PROMPT = """You are a dealer-website reverse-engineering specialist. \
Your job: given a US car dealer's website URL, find how to extract their used \
inventory programmatically. Return a config that a generic Python extractor \
can execute without ever revisiting the site.

CRITICAL — check these in order BEFORE anything else:

1. **Next.js sites** (look for __NEXT_DATA__ in the HTML — preserved at the \
bottom of every fetch result for you). The inventory is ALMOST ALWAYS embedded \
in this JSON blob. Drill into the JSON structure to find a list of vehicles. \
Common paths: `props.pageProps.initialState.search.searchResults.ads[*]`, \
`props.pageProps.vehicles[*]`, `props.pageProps.results[*]`, \
`props.pageProps.inventory[*]`. DO NOT waste turns hunting for separate /api/ \
endpoints if __NEXT_DATA__ has the data. set inventory_source.type to \
"next_data" and inventory_source.url_template to "{base}" (the page itself).

2. **Nuxt/Vue sites** — check __NUXT_DATA__ or window.__NUXT__ similarly.

3. **Apollo/GraphQL** — check window.__APOLLO_STATE__ similarly.

4. Only if the page truly has NO embedded data, try /api/ endpoints, sitemap.xml, \
or HTML grid extraction.

Approach:
1. Fetch the dealer's URL.
2. Look at the preserved blobs at the bottom of the response — if __NEXT_DATA__ \
is present, drill into it FIRST. You should be able to submit a config in 2-3 \
turns for any Next.js site.
3. If the site is purely HTML server-rendered, identify CSS selectors for \
the vehicle listing grid.
4. Identify pagination if the listing spans multiple pages.

Required vehicle fields (do your best — partial extraction is fine):
- vin (17 chars; CRITICAL — if you can't find VINs, the dealer can't be \
verified or matched). Sometimes VIN is only on the VDP page, not the listing.
- year, make, model, price, miles
- trim, stock_number, url, photos, colors (nice-to-have)

When you have enough signal to write a reliable config, call submit_config. \
Don't burn turns exhaustively if you've found a clean JSON API on turn 2 — \
just submit. Aim for 3-6 fetch turns total. Keep your reasoning in the notes \
field for whoever reviews this later.

If the site genuinely cannot be scraped without browser JS (heavily obfuscated, \
Cloudflare-walled, or React-only with no embedded data), set fetch_strategy \
to "playwright" and provide whatever selectors a headless browser would need.
"""


def cost_usd(input_tokens, output_tokens):
    return round(
        input_tokens * INPUT_PRICE_PER_MTOK / 1_000_000
        + output_tokens * OUTPUT_PRICE_PER_MTOK / 1_000_000,
        4,
    )


def discover(dealer_id, dealer_name, dealer_url):
    """Run an Opus discovery agent for one dealer. Returns config dict or None."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    user_msg = (
        f"Dealer: **{dealer_name}**\n"
        f"URL: {dealer_url}\n\n"
        "Investigate this site and produce a scrape config. Start by fetching "
        "the URL above. Aim to call submit_config within 3-6 turns. "
        "If you find an API endpoint, that's almost always the right answer."
    )

    messages = [{"role": "user", "content": user_msg}]
    total_input = 0
    total_output = 0
    config = None
    error = None

    for turn in range(MAX_TURNS):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_PER_RESPONSE,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            error = f"API call failed turn {turn}: {e}"
            print(f"  ERROR: {error}", flush=True)
            break

        total_input += resp.usage.input_tokens
        total_output += resp.usage.output_tokens
        running_cost = cost_usd(total_input, total_output)
        print(f"  turn {turn+1}: in={resp.usage.input_tokens} out={resp.usage.output_tokens} running=${running_cost}", flush=True)

        if resp.stop_reason == "end_turn":
            error = "model stopped without submitting config"
            break

        # Append assistant message
        messages.append({"role": "assistant", "content": resp.content})

        # Process tool calls
        tool_results = []
        submitted = False
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_config":
                config = block.input
                submitted = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Config received. Thank you.",
                })
                break
            elif block.name == "fetch_url":
                url = block.input.get("url", "")
                print(f"    → fetch_url({url[:80]})", flush=True)
                result = fetch_url(url)
                # Add a JSON sketch if the body looks like JSON, to help the model
                if isinstance(result, dict) and "body" in result and result.get("content_type", "").startswith("application/json"):
                    sketch = extract_jsonpath_hint(result["body"])
                    if sketch:
                        result["json_structure_sketch"] = sketch
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)[:TOOL_RESULT_CAP_CHARS],
                })

        if submitted:
            break
        if not tool_results:
            error = f"turn {turn+1} produced no tool calls; stop_reason={resp.stop_reason}"
            break

        messages.append({"role": "user", "content": tool_results})

        # Memory hygiene: replace tool_result contents from older turns with
        # a one-line summary so context doesn't balloon. Keep the last 2 fetches.
        old_tool_msgs = [i for i, m in enumerate(messages) if isinstance(m.get("content"), list)
                         and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])]
        for idx in old_tool_msgs[:-2]:  # all but the most recent two
            for block in messages[idx]["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content_str = str(block.get("content", ""))
                    if len(content_str) > 500:
                        block["content"] = content_str[:300] + "...[older fetch result trimmed]"

    if not config and not error:
        error = f"hit MAX_TURNS={MAX_TURNS} without submitting config"

    return {
        "config": config,
        "error": error,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost_usd(total_input, total_output),
        "model": MODEL,
        "turns_used": turn + 1 if 'turn' in dir() else 0,
    }


def run_discovery(dealer_id, force=False):
    """Discover a config for one dealer, persist results."""
    conn = db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, url, platform, scrape_config FROM dealers WHERE id=%s", (dealer_id,))
    dealer = cur.fetchone()
    if not dealer:
        print(f"dealer #{dealer_id} not found")
        return False
    if dealer["platform"] and not force:
        print(f"dealer #{dealer_id} already has platform={dealer['platform']}; use --force to re-discover")
        return False

    print(f"\n=== discovering dealer #{dealer_id} {dealer['name']} ===")
    print(f"URL: {dealer['url']}")
    started = time.time()

    # ── STAGE 1: deterministic auto-detect (free) ──
    print("  [stage 1] trying deterministic NEXT_DATA / NUXT / Apollo detection...")
    try:
        page = fetch_url(dealer["url"], max_bytes=2_000_000)  # bigger fetch for raw HTML
        if isinstance(page, dict) and "body" in page:
            # The fetch_url helper strips HTML — we need raw for SPA state extraction.
            # Re-fetch raw without stripping just for auto-detect.
            raw = requests.get(dealer["url"], headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
                "Accept-Language": "en-US,en;q=0.5",
            }, timeout=20).text
            cfg, info = auto_detect_from_html(raw, dealer["url"])
            if cfg:
                print(f"  [stage 1] ✓ {info}")
                cur.execute("""
                    UPDATE dealers SET
                      scrape_config=%s, scrape_config_version=COALESCE(scrape_config_version,0)+1,
                      scrape_config_at=NOW(),
                      platform=COALESCE(platform, 'ai-generated'),
                      scrape_method=COALESCE(scrape_method, 'config-driven')
                    WHERE id=%s
                """, (json.dumps(cfg), dealer_id))
                cur.execute("""
                    INSERT INTO dealer_discovery_runs
                      (dealer_id, model, finished_at, status, config_produced, cost_usd)
                    VALUES (%s, 'deterministic', NOW(), 'ok', %s, 0)
                """, (dealer_id, json.dumps(cfg)))
                conn.commit()
                conn.close()
                print(f"  done in {round(time.time()-started, 1)}s ($0.00)")
                return True
            else:
                print(f"  [stage 1] miss: {info}")
        else:
            print(f"  [stage 1] miss: fetch failed")
    except Exception as e:
        print(f"  [stage 1] error: {e}")

    # ── STAGE 2: AI fallback (Opus) ──
    print("  [stage 2] falling back to Opus 4.7 agent...")
    cur.execute(
        "INSERT INTO dealer_discovery_runs (dealer_id, model) VALUES (%s, %s) RETURNING id",
        (dealer_id, MODEL),
    )
    run_id = cur.fetchone()["id"]
    conn.commit()

    result = discover(dealer_id, dealer["name"], dealer["url"])
    elapsed = round(time.time() - started, 1)

    config = result["config"]
    error = result["error"]
    cost = result["cost_usd"]

    if config:
        print(f"\n  ✓ config produced in {elapsed}s (${cost})")
        print(f"    fetch_strategy: {config.get('fetch_strategy')}")
        print(f"    inventory: {config.get('inventory_source', {}).get('type')} → {config.get('inventory_source', {}).get('url_template', '')[:80]}")
        print(f"    confidence: {config.get('confidence')}")
        # Store on dealer + bump platform
        cur.execute("""
            UPDATE dealers SET
              scrape_config=%s,
              scrape_config_version=COALESCE(scrape_config_version,0)+1,
              scrape_config_at=NOW(),
              platform=COALESCE(platform, 'ai-generated'),
              scrape_method=COALESCE(scrape_method, 'config-driven')
            WHERE id=%s
        """, (json.dumps(config), dealer_id))
        cur.execute("""
            UPDATE dealer_discovery_runs
              SET finished_at=NOW(), input_tokens=%s, output_tokens=%s,
                  cost_usd=%s, status='ok', config_produced=%s
              WHERE id=%s
        """, (result["input_tokens"], result["output_tokens"], cost, json.dumps(config), run_id))
        conn.commit()
        conn.close()
        return True
    else:
        print(f"\n  ✗ failed in {elapsed}s (${cost}): {error}")
        cur.execute("""
            UPDATE dealer_discovery_runs
              SET finished_at=NOW(), input_tokens=%s, output_tokens=%s,
                  cost_usd=%s, status='failed', error=%s
              WHERE id=%s
        """, (result["input_tokens"], result["output_tokens"], cost, error, run_id))
        conn.commit()
        conn.close()
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dealer-id", type=int)
    ap.add_argument("--auto", action="store_true",
                    help="Process all dealers where platform IS NULL")
    ap.add_argument("--force", action="store_true",
                    help="Re-discover even if platform is already set")
    args = ap.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not DB_URL:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    if args.dealer_id:
        ok = run_discovery(args.dealer_id, force=args.force)
        return 0 if ok else 1

    if args.auto:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM dealers WHERE platform IS NULL AND active=true ORDER BY id"
        )
        ids = [r[0] for r in cur.fetchall()]
        conn.close()
        if not ids:
            print("no dealers needing discovery")
            return 0
        print(f"discovering {len(ids)} dealer(s): {ids}")
        for did in ids:
            run_discovery(did)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
