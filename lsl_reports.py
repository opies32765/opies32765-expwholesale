"""LSL_REPORTS_9B_2026_07_05 — owner self-serve LSL data & reports on the 9B.

/lsl-reports            page (session-gated by the app's global login)
/api/lsl-reports/ask    {question} -> 9B tool loop over ew_mcp LSL tools
                        -> {answer, steps: [{tool, args, result}]}

Design (per the rep-line lessons): the 9B plans tool calls and narrates;
every TABLE the owner sees is rendered client-side from the RAW tool result
JSON — the model never retypes figures. Read-only tool surface only.
Brain = local ew-brain (vLLM); zero external LLM calls.
"""
from __future__ import annotations

import inspect
import json
import logging
import os

import requests as _rq
from flask import Blueprint, jsonify, render_template, request

log = logging.getLogger("lsl-reports")

bp = Blueprint("lsl_reports", __name__)

EW_BRAIN_URL = os.environ.get("EW_BRAIN_URL", "https://brain.experience-wholesale.net")
EW_BRAIN_KEY = os.environ.get("EW_BRAIN_KEY", "")
EW_BRAIN_MODEL = os.environ.get("EW_BRAIN_MODEL", "ew-brain")
MAX_TOOL_CALLS = 5
DEFAULT_CALLER = "Oscar"  # owner gate is open (_is_owner True); name used for logs

# Read-only owner tool surface. Anything not listed cannot be called.
ALLOWED_TOOLS = [
    "lsl_data_query", "lsl_deals_booked", "lsl_salesperson_stats",
    "lsl_inventory_now", "lsl_customer_history", "lsl_top_makes",
    "lsl_make_volume", "lsl_lookup_sale", "lsl_payments",
    "lsl_appraisal_history", "lsl_dealer_intel", "lsl_deal_parties",
    "lsl_service_requests", "lsl_customer_lookup", "dashboard_stats",
    "recent_bids", "search_bids", "get_bid",
]

_catalog_cache = None


def _tool_fn(name):
    import ew_mcp
    obj = getattr(ew_mcp, name, None)
    if obj is None:
        return None
    return getattr(obj, "fn", obj)


def _catalog() -> str:
    """Auto-built tool catalog from ew_mcp signatures + docstrings, so the
    prompt stays current with the code."""
    global _catalog_cache
    if _catalog_cache:
        return _catalog_cache
    lines = []
    for name in ALLOWED_TOOLS:
        fn = _tool_fn(name)
        if fn is None:
            continue
        try:
            sig = str(inspect.signature(fn))
        except (TypeError, ValueError):
            sig = "(...)"
        doc = " ".join((inspect.getdoc(fn) or "").split())[:400]
        lines.append(f"- {name}{sig}\n    {doc}")
    _catalog_cache = "\n".join(lines)
    return _catalog_cache


def _system_prompt() -> str:
    return (
        "You are the data analyst for Experience Wholesale's owners, answering "
        "questions about the LSL sales ledger (deals, gross, salespeople, inventory, "
        "customers, payments) and the EW bid board.\n\n"
        "═══ OUTPUT — STRICT ═══\n"
        "Reply with EXACTLY ONE JSON object, nothing else:\n"
        '  {"answer": null, "tool": {"name": "lsl_data_query", "args": {...}}}   to call a tool\n'
        '  {"answer": "<final answer text>", "tool": null}                        when done\n'
        "After each tool call you receive a [tool_result] message with the JSON result.\n\n"
        "═══ RULES ═══\n"
        "- You have ZERO knowledge of EW's data. You CANNOT answer any data question "
        "from memory. Your FIRST reply to every question MUST be a tool call — an "
        "answer given before at least one [tool_result] arrives is INVALID and rejected.\n"
        "- Every figure in your final answer must come from a tool result verbatim. "
        "Never estimate, never do math beyond reading fields, never invent.\n"
        "- NEVER add rows together yourself. group_by results are TRUNCATED to `limit` "
        "rows (default 25) — a total computed from them is WRONG. For any total, make a "
        "separate call with agg=sum (or count) and NO group_by, and quote its value.\n"
        "- The owner SEES every tool result as a table automatically — your answer "
        "should summarize the headline, not repeat whole tables.\n"
        f"- Always pass caller_name=\"{DEFAULT_CALLER}\" when a tool requires it.\n"
        "- Prefer lsl_data_query for totals/counts/rankings/filters "
        "(agg=list|count|sum|avg|min|max; group_by; filters 'field:op:value;...'; "
        "period ''|today|yesterday|this_month|last_month|last_7_days|last_30_days|ytd|last_year "
        "or 'YYYY-MM-DD:YYYY-MM-DD'). front_value = NET deal profit.\n"
        "- DATA DICTIONARY: recon_cost is NOT populated in LSL (0 on almost every deal — "
        "do not use it). Recon + transport + fees live COMBINED in total_supp_costs; for "
        "any recon/expense-per-car question use total_supp_costs and say it includes transport.\n"
        "- A zero/empty result is still an answer — explain what it means, never reply blank.\n"
        "- AVERAGES LIE WHEN SKEWED: for any per-car avg (costs, profit), also call agg=max "
        "and a count above a threshold (e.g. field:gt:5000). If a few deals dominate, report "
        "the typical band too (e.g. 'most cars under $1,000; mean pulled up by N big deals').\n"
        "- Loss/loser questions: filters=\"front_value:lt:0\".\n"
        "- period is its OWN argument — never put it inside filters. Example: top 5 "
        "salespeople by gross YTD = {\"agg\": \"sum\", \"agg_field\": \"front_value\", "
        "\"group_by\": \"sales_person\", \"period\": \"ytd\", \"limit\": 5}.\n"
        "- If a question is ambiguous, make the sensible choice and say what you chose.\n"
        f"- You get at most {MAX_TOOL_CALLS} tool calls per question — plan them.\n\n"
        "═══ TOOLS ═══\n" + _catalog()
    )


def _brain(messages: list[dict]) -> dict:
    r = _rq.post(
        EW_BRAIN_URL.rstrip("/") + "/v1/chat/completions",
        json={
            "model": EW_BRAIN_MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 700,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        headers={
            "Authorization": f"Bearer {EW_BRAIN_KEY}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126",
        },
        timeout=45,
    )
    r.raise_for_status()
    text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        return {"answer": " ".join(t.split())[:800] or None, "tool": None}
    try:
        out = json.loads(t[i:j + 1])
        return {"answer": out.get("answer"), "tool": out.get("tool")}
    except Exception:
        return {"answer": " ".join(t.split())[:800] or None, "tool": None}


def _run_tool(name: str, args: dict):
    if name not in ALLOWED_TOOLS:
        return {"error": f"tool {name} not allowed"}
    fn = _tool_fn(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        params = inspect.signature(fn).parameters
        if "caller_name" in params and "caller_name" not in args:
            args["caller_name"] = DEFAULT_CALLER
        args = {k: v for k, v in args.items() if k in params}
    except (TypeError, ValueError):
        pass
    if inspect.iscoroutinefunction(fn):
        import asyncio
        return asyncio.run(fn(**args))
    return fn(**args)


@bp.route("/lsl-reports")
def page():
    return render_template("lsl_reports.html")


def _numbers_in(text: str) -> set:
    """All integers >= 100 appearing in text (commas/decimals normalized).
    Small numbers and years are too noisy to police."""
    import re
    out = set()
    for m in re.finditer(r"\d[\d,]*\.?\d*", text or ""):
        try:
            n = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if n >= 100 and not (1900 <= n <= 2100 and n == int(n)):
            out.add(int(round(n)))
    return out


@bp.route("/api/lsl-reports/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400
    messages = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": question}]
    steps = []
    answer = None
    warning = None
    nudges = 0
    for _ in range(MAX_TOOL_CALLS + 4):
        try:
            out = _brain(messages)
        except Exception as e:
            log.exception("brain call failed")
            return jsonify({"error": f"brain unavailable: {type(e).__name__}"}), 502
        tool = out.get("tool")
        if tool and isinstance(tool, dict) and len(steps) < MAX_TOOL_CALLS:
            name = str(tool.get("name") or "")
            args = tool.get("args") or {}
            log.info(f"[lsl-reports] tool {name}({json.dumps(args, default=str)[:200]})")
            try:
                result = _run_tool(name, dict(args))
            except Exception as e:
                log.exception("tool failed")
                result = {"error": f"{type(e).__name__}: {e}"}
            steps.append({"tool": name, "args": args, "result": result})
            messages.append({"role": "assistant",
                             "content": json.dumps({"answer": None, "tool": tool}, default=str)})
            messages.append({"role": "user",
                             "content": "[tool_result] " + json.dumps(result, default=str)[:12000]})
            continue
        cand = (out.get("answer") or "").strip()
        # HARD GUARD 1: no answer without data. The 9B fabricated "1,011 deals,
        # $1,011,000 lost" tool-free on the very first canary — reject and force
        # a tool call instead of serving invented financials to an owner.
        if not steps and nudges < 2:
            nudges += 1
            log.warning(f"[lsl-reports] rejected tool-free answer: {cand[:120]!r}")
            messages.append({"role": "assistant", "content": json.dumps(out, default=str)})
            messages.append({"role": "user",
                             "content": "[system] INVALID: you answered without calling any tool. "
                                        "You have no data. Reply with a tool call."})
            continue
        # HARD GUARD 2: every large figure in the narrative must appear in the
        # actual tool results; one retry, then flag it for the owner.
        stray = _numbers_in(cand) - _numbers_in(json.dumps(steps, default=str))
        if stray and nudges < 2:
            nudges += 1
            log.warning(f"[lsl-reports] answer contains figures not in results: {sorted(stray)[:6]}")
            messages.append({"role": "assistant", "content": json.dumps(out, default=str)})
            messages.append({"role": "user",
                             "content": f"[system] INVALID: these figures are not in any tool result: "
                                        f"{sorted(stray)[:6]}. Rewrite the answer using ONLY verbatim "
                                        f"figures from the tool results."})
            continue
        if stray:
            warning = "Some figures in this summary could not be verified against the data — trust the tables below."
            log.warning(f"[lsl-reports] serving answer WITH unverified figures: {sorted(stray)[:6]}")
        answer = cand
        break
    if not steps:
        return jsonify({"answer": None, "steps": [],
                        "error": "The model wouldn't query the data for this question — try rephrasing."})
    if not (answer or "").strip():
        answer = "The queries ran — results are in the tables below."
    return jsonify({"answer": answer, "steps": steps, "warning": warning})
