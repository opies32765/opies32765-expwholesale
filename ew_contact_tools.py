"""Anna contact + SMS-group management voice tools (WRITE). Save a contact (name->number) into
bill_contacts (the directory lookup_contact reads), and add contacts/numbers to named SMS groups
(the anna_sms_groups.json the send tools use). Loaded into ew_mcp namespace for the dispatcher."""
import os as _os, re as _re, json as _json, psycopg2, psycopg2.extras, asyncio as _a

_GROUPS_FILE = _os.environ.get("ANNA_SMS_GROUPS_FILE", "/opt/expwholesale/anna_sms_groups.json")


def _dburl():
    u = _os.environ.get("DATABASE_URL")
    if not u:
        raise RuntimeError("DATABASE_URL not set")
    return u


def _e164(raw):
    """Normalize a spoken/typed phone number to E.164 (+1XXXXXXXXXX for US). Returns None if not valid."""
    s = (raw or "").strip()
    d = _re.sub(r"\D", "", s)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    if s.startswith("+") and 11 <= len(d) <= 15:
        return "+" + d
    return None


def _rows(sql, args=()):
    with psycopg2.connect(_dburl()) as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]


def _exec(sql, args=()):
    with psycopg2.connect(_dburl()) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        c.commit()
    return n


def _load_groups():
    try:
        with open(_GROUPS_FILE) as f:
            g = _json.load(f)
            return g if isinstance(g, dict) else {}
    except Exception:
        return {}


def _save_groups(g):
    tmp = _GROUPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(g, f, indent=2)
    _os.replace(tmp, _GROUPS_FILE)


async def save_contact(name: str = "", phone: str = "", role: str = "", caller_name: str = "") -> dict:
    """Save a person to the contact directory so Anna can text them later by name. name: the person's
    name. phone: their number (spoken digits are fine). role: optional label. ALWAYS read the number
    back to the caller and confirm before calling this — a single wrong digit texts the wrong person.
    Updates the number if the name already exists. Returns the saved name + normalized number."""
    nm = (name or "").strip()
    e = _e164(phone)
    if not nm:
        return {"error": "need a name"}
    if not e:
        return {"error": f"'{phone}' is not a valid 10-digit phone number; ask the caller to repeat it"}
    # Upsert keyed on the phone number (bill_contacts has a unique constraint on phone_e164):
    # re-saving an existing number renames it; a name with a new number adds a second entry.
    by_name = await _a.to_thread(_rows, "select id, phone_e164 from bill_contacts where lower(name)=lower(%s) limit 1", (nm,))
    if by_name and by_name[0]["phone_e164"] == e:
        action = "already saved"
    elif by_name and not (await _a.to_thread(_rows, "select 1 from bill_contacts where phone_e164=%s and id<>%s", (e, by_name[0]["id"]))):
        # same name, changed number, and the new number isn't used by anyone else -> update in place
        await _a.to_thread(_exec,
            "update bill_contacts set phone_e164=%s, bill_can_text=true, updated_at=now(),"
            " role=coalesce(nullif(%s,''),role) where id=%s", (e, role.strip(), by_name[0]["id"]))
        action = "updated"
    else:
        await _a.to_thread(_exec,
            "insert into bill_contacts (name, phone_e164, role, source, bill_can_text, created_at, updated_at)"
            " values (%s,%s,%s,'anna voice',true, now(), now())"
            " on conflict (phone_e164) do update set name=excluded.name, bill_can_text=true, updated_at=now()",
            (nm, e, role.strip() or None))
        action = "saved"
    return {"ok": True, "action": action, "name": nm, "phone": e,
            "spoken_phone": "-".join([e[2:5], e[5:8], e[8:12]]) if e.startswith("+1") and len(e) == 12 else e}


async def add_to_group(who: str = "", group: str = "", caller_name: str = "") -> dict:
    """Add a person (by saved contact name) or a raw phone number to a named SMS group so 'text the
    <group>' reaches them. who: a contact name OR a phone number. group: the group name (created if new,
    e.g. 'partners', 'owners'). Confirm the person and group with the caller first. Returns the group's
    updated member count."""
    who_s = (who or "").strip()
    grp = (group or "").strip().lower()
    if not who_s or not grp:
        return {"error": "need both a person/number and a group name"}
    number = _e164(who_s)
    resolved_name = None
    if not number:
        hits = await _a.to_thread(_rows,
            "select name, phone_e164 from bill_contacts where bill_can_text=true and lower(name) like lower(%s) order by name limit 5",
            (f"%{who_s}%",))
        if not hits:
            return {"matches": 0, "_must_say": f"No saved contact named '{who_s}'. Save them first, or give the number."}
        if len(hits) > 1:
            return {"ambiguous": [h["name"] for h in hits], "_must_say": "Which contact? Ask the caller to clarify."}
        number = hits[0]["phone_e164"]
        resolved_name = hits[0]["name"]
    groups = _load_groups()
    members = groups.get(grp, [])
    created = grp not in groups
    if number in members:
        return {"ok": True, "already_in_group": True, "group": grp, "who": resolved_name or number, "members": len(members)}
    members.append(number)
    groups[grp] = members
    await _a.to_thread(_save_groups, groups)
    return {"ok": True, "group": grp, "group_created": created,
            "added": resolved_name or number, "members": len(members)}


async def list_groups(caller_name: str = "") -> dict:
    """List the SMS groups and how many people are in each. Use for 'what groups do I have',
    'who's in the partners group'."""
    groups = _load_groups()
    return {"groups": {k: len(v if isinstance(v, list) else [v]) for k, v in groups.items()}}
