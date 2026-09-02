#!/usr/bin/env python3
"""bill_brain_ab.py — head-to-head: Bill's want-list turn on the 9B vs the 27B.

Sends the REAL rep_wantlist_prompt.txt system prompt, wrapped in a THIS CALL
block byte-shaped like rep_wantlist_server._refresh_ctx builds it, and scores
each brain on: per-turn wall latency (first turn = COLD, what a caller waits),
strict-JSON validity incl. the trailing-brace failure, and whether the emitted
action matches what the rep actually asked for.

Every case uses a DIFFERENT caller number so the THIS CALL prefix differs and
--enable-prefix-caching cannot hand us warm numbers that production won't see.

Read-only. Touches no service, no DB, no env file.
"""
import json, os, sys, time, urllib.request

PROMPT_PATH = os.environ.get("BILL_PROMPT", "/opt/expwholesale/rep_wantlist_prompt.txt")
SYSTEM_PROMPT = open(PROMPT_PATH).read()
SYSTEM_PROMPT += (
    "\nAfter you emit an action, the next message will be `[tool_result] {json}` — "
    "the actual outcome. Reply with a new JSON object whose \"say\" words that outcome "
    "naturally: brief, factual, strictly from the result. Never claim success before "
    "seeing the result.\n")  # REP_MODE=raw, matches the live service


def ctx_none(digits):
    return "═══ THIS CALL ═══\ncaller_phone_digits: %s\nOPEN REQUESTS: none\n\n" % digits


def ctx_three(digits):
    return ("═══ THIS CALL ═══\ncaller_phone_digits: %s\n"
            "OPEN REQUESTS for this caller (id | want | since):\n"
            "  #41 | black Escalade under 90k | 3 days ago\n"
            "  #42 | 2021+ Ford F-250 Lariat | 3 days ago\n"
            "  #43 | 2023-2025 Porsche 911 Turbo S | 1 day ago\n\n") % digits


BRAINS = {
    "9B  (ew-mt3, .150)":  {"url": "https://brain.experience-wholesale.net",
                            "model": "ew-brain", "key": os.environ.get("EW_BRAIN_KEY", "")},
    "27B (Qwen3.8, .104)": {"url": "http://127.0.0.1:18001",
                            "model": "anna", "key": ""},
}

# name, ctx-builder, turns, expectation
CASES = [
    ("plain add", ctx_none,
     ["Hey Bill, my guy's looking for a black Escalade, twenty-two or newer, under ninety grand."],
     dict(action="add_want", make="cadillac", model="escalade")),
    ("make split: Corvette Grand Sport", ctx_none,
     ["I need a Corvette Grand Sport, twenty-twenty and up."],
     dict(action="add_want", make="chevrolet", model="corvette", trim="grand sport")),
    ("two-word make: Range Rover", ctx_none,
     ["Customer wants a Range Rover, low miles."],
     dict(action="add_want", make="land rover", model="range rover")),
    # ---- vagueness: make and/or model missing => Bill must DRILL, never add ----
    ("vague: body style only", ctx_none,
     ["I need a truck for a guy."],
     dict(drill=True)),
    ("vague: three rows under forty", ctx_none,
     ["Customer wants something with three rows, under forty."],
     dict(drill=True)),
    ("vague: make only", ctx_none,
     ["He's looking for a Ford."],
     dict(drill=True)),
    ("vague: drills then dodges twice", ctx_none,
     ["I need something nice for a customer.",
      "I dunno, whatever's good.",
      "Just something clean, you know?"],
     dict(action=None)),
    ("truck trim: F-250 Lariat", ctx_none,
     ["Looking for an F-250 Lariat, twenty-one or newer."],
     dict(action="add_want", make="ford", model="f-250", trim="lariat")),
    ("no-price trap", ctx_none,
     ["Just get me a Tahoe, any year, don't care about miles."],
     dict(action="add_want", make="chevrolet", model="tahoe", no_price=True)),
    ("read back open list", ctx_three,
     ["What am I watching for right now?"],
     dict(action_in=("list_wants", None), mentions_any=("escalade",))),
    ("correction = cancel+add pair", ctx_three,
     ["That 911 Turbo S — make it twenty-four to twenty-five, not twenty-three."],
     dict(pair=("cancel_want", "add_want"))),
    ("cancel all three", ctx_three,
     ["Go ahead and cancel all of them for me."],
     dict(action="cancel_want", cancels={41, 42, 43})),
    ("wrap-up, no action", ctx_three,
     ["No, that's it. Thanks Bill."],
     dict(action=None)),
    ("multi-turn: add then confirm", ctx_none,
     ["Hey Bill.",
      "Yeah, need a Denali, twenty-three or newer.",
      "[tool_result] {\"ok\": true, \"alert_id\": 77, \"desc\": \"2023+ GMC Denali\"}"],
     dict(action=None, mentions_any=("denali", "gmc"))),
]


def first_balanced_obj(s):
    start = s.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(start, len(s)):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:k + 1]
    return None


def scan_json_string_value(s, key):
    """Mirror of rep_wantlist_server._scan_json_string_value."""
    kpos = s.find(chr(34) + key + chr(34))
    if kpos == -1:
        return None
    q = s.find(chr(34), kpos + len(key) + 2)
    if q == -1:
        return None
    out, esc = [], False
    for k in range(q + 1, len(s)):
        ch = s[k]
        if esc:
            out.append(ch); esc = False
        elif ch == chr(92):
            out.append(ch); esc = True
        elif ch == chr(34):
            raw = "".join(out)
            try:
                return json.loads(chr(34) + raw + chr(34))
            except Exception:
                return raw
    return None


def parse_like_service(text):
    """EXACT mirror of brain_chat's parse chain: balanced object -> salvaged
    say/action -> prose fallback. Scoring against anything narrower measures the
    harness, not Bill: the service SPEAKS bare prose, it does not go silent."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    obj = first_balanced_obj(t)
    if obj is not None:
        try:
            out = json.loads(obj)
            if isinstance(out, dict):
                return {"say": str(out.get("say") or ""), "action": out.get("action")}, "json"
        except Exception:
            pass
    say = scan_json_string_value(t, "say")
    if say is not None:
        act = None
        ap = t.find(chr(34) + "action" + chr(34))
        if ap != -1:
            ao = first_balanced_obj(t[ap:])
            if ao is not None:
                try:
                    act = json.loads(ao)
                except Exception:
                    act = None
        return {"say": say, "action": act}, "salvaged"
    return {"say": " ".join(t.split())[:400] or "Sorry, say that again?", "action": None}, "prose"


def call(brain, system, history):
    payload = {
        "model": brain["model"],
        "messages": [{"role": "system", "content": system}] + history,
        "temperature": 0,
        "max_tokens": 300,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        brain["url"].rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + brain["key"],
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    dt = time.time() - t0
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message", {}) or {}
    return {"text": msg.get("content") or "", "dt": dt,
            "finish": ch.get("finish_reason"),
            "tool_calls": msg.get("tool_calls"),
            "usage": data.get("usage") or {}}


def actions_of(obj):
    a = obj.get("action")
    if a is None:
        return []
    return a if isinstance(a, list) else [a]


def norm(x):
    return str(x or "").strip().lower()


BANNED_OPENERS = ("got it", "sure thing", "absolutely", "so you're looking for",
                  "so you are looking for", "just to confirm", "let me", "of course",
                  "no problem", "understood", "alright, so", "okay, so")


def words(s):
    return len((s or "").split())


def score(obj, raw, blob, exp):
    notes = []
    acts = actions_of(obj)
    names = [norm(a.get("name")) for a in acts]
    say = norm(obj.get("say"))

    # ---- conciseness, measured on every reply that actually says something ----
    if say:
        w = words(say)
        if w > 20:
            notes.append("%dw" % w)
        for o in BANNED_OPENERS:
            if say.startswith(o):
                notes.append("filler opener %r" % o)
                break
        if say.count("?") > 1:
            notes.append("%d questions in one turn" % say.count("?"))

    # ---- drill: no action, exactly one question, and it must ASK something ----
    if exp.get("drill"):
        if names:
            return False, notes + ["should have drilled, but emitted %s" % names]
        if "?" not in say:
            return False, notes + ["no question asked: %r" % (obj.get("say") or "")]
        return True, notes

    # the documented 9B failure: junk (an extra closing brace) AFTER the object,
    # or prose BEFORE it. first_balanced_obj rescues both; flag either.
    if raw.strip() != (blob or ""):
        lead = raw.find("{")
        notes.append("junk %s JSON" % ("before+after" if lead > 0 and raw.strip() != blob
                                       else ("before" if lead > 0 else "after")))

    if "pair" in exp:
        if sorted(names) != sorted(exp["pair"]):
            return False, notes + ["expected %s, got %s" % (list(exp["pair"]), names or ["<none>"])]
        return True, notes

    if "action_in" in exp:
        allowed = [norm(x) if x else None for x in exp["action_in"]]
        got = names[0] if names else None
        if got not in allowed:
            return False, notes + ["expected one of %s, got %s" % (allowed, got)]
    elif "action" in exp and exp["action"] is None:
        if names:
            return False, notes + ["expected NO action, got %s" % names]
    elif "action" in exp:
        if names[:1] != [norm(exp["action"])]:
            return False, notes + ["expected %s, got %s" % (exp["action"], names or ["<none>"])]

    for a in acts:
        args = a.get("args") or {}
        if "make" in exp and norm(a.get("name")) == "add_want":
            if norm(args.get("make")) != exp["make"]:
                return False, notes + ["make=%r want %r" % (args.get("make"), exp["make"])]
            if exp["model"] not in norm(args.get("model")):
                return False, notes + ["model=%r want %r" % (args.get("model"), exp["model"])]
            if "trim" in exp and exp["trim"] not in norm(args.get("trim_contains")):
                return False, notes + ["trim=%r want %r" % (args.get("trim_contains"), exp["trim"])]
            if exp.get("no_price") and args.get("price_max"):
                return False, notes + ["INVENTED price_max=%s" % args.get("price_max")]

    if "cancels" in exp:
        got = set()
        for a in acts:
            if norm(a.get("name")) != "cancel_want":
                continue
            ids = (a.get("args") or {}).get("alert_id")
            ids = ids if isinstance(ids, list) else [ids]
            got |= {int(i) for i in ids if str(i).isdigit()}
        if got != exp["cancels"]:
            return False, notes + ["cancelled %s want %s" % (sorted(got), sorted(exp["cancels"]))]

    if exp.get("no_price"):
        for tok in ("$", "grand", "thousand"):
            if tok in say:
                return False, notes + ["price language in say: %r" % obj.get("say")]
    if exp.get("mentions_any") and not acts:
        if not any(m in say for m in exp["mentions_any"]):
            return False, notes + ["say mentions none of %s" % list(exp["mentions_any"])]
    return True, notes


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    summary = {}
    for bname, brain in BRAINS.items():
        if only and only not in bname:
            continue
        print("\n" + "=" * 84)
        print("BRAIN: %s  ->  %s  [%s]" % (bname, brain["url"], brain["model"]))
        print("=" * 84)
        print("  %-34s %8s %8s  %s" % ("case", "cold-1st", "total", "result"))
        passes, cold, totals, saywords = 0, [], [], []
        for i, (cname, ctxf, turns, exp) in enumerate(CASES):
            # unique caller per case so the prefix differs and prefix-caching
            # cannot hand us warm numbers production would never see
            system = ctxf("407555%04d" % (1000 + i * 137)) + SYSTEM_PROMPT
            history, per_turn, last, modes = [], [], None, []
            try:
                for t in turns:
                    history.append({"role": "user", "content": t})
                    last = call(brain, system, history)
                    per_turn.append(last["dt"])
                    history.append({"role": "assistant", "content": last["text"]})
                    # conciseness is a per-TURN property, not a per-case one
                    po, how = parse_like_service(last["text"])
                    if how != "json":
                        modes.append("%s:%s" % (len(per_turn), how))
                    sw = words(po.get("say"))
                    if sw:
                        saywords.append(sw)
            except Exception as e:
                print("  %-34s %8s %8s  ERROR %s" % (cname, "-", "-", e))
                continue
            raw = last["text"]
            blob = first_balanced_obj(raw)
            obj, how = parse_like_service(raw)
            cold.append(per_turn[0]); totals.append(sum(per_turn))
            ok, notes = score(obj, raw, blob, exp)
            if how != "json":
                notes.append("service parsed as %s" % how)
            passes += ok
            tail = ("  [" + "; ".join(notes) + "]") if notes else ""
            print("  %-34s %7.2fs %7.2fs  %s%s"
                  % (cname, per_turn[0], sum(per_turn), "PASS" if ok else "FAIL", tail))
            if not ok:
                print("        say:    %r" % (obj.get("say") or "")[:150])
                print("        action: %s" % json.dumps(obj.get("action"))[:240])
        if cold:
            c, sw = sorted(cold), sorted(saywords)
            wmed = sw[len(sw) // 2] if sw else 0
            wmax = sw[-1] if sw else 0
            summary[bname] = (passes, len(CASES), c[len(c) // 2], c[-1], wmed, wmax)
            print("  ---- %d/%d passed | COLD first-turn: median %.2fs  max %.2fs"
                  " | spoken words: median %d  max %d"
                  % (passes, len(CASES), c[len(c) // 2], c[-1], wmed, wmax))
    print("\n" + "=" * 84)
    print("SUMMARY  (cold first-turn = what a rep waits after speaking; words = length of one spoken reply)")
    for b, (p, n, med, mx, wmed, wmax) in summary.items():
        print("  %-22s %2d/%d   cold median %5.2fs  max %5.2fs   words median %2d  max %2d"
              % (b, p, n, med, mx, wmed, wmax))


if __name__ == "__main__":
    main()
