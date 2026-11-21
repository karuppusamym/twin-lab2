"""What a builder package's `evidence.json` holds beyond the scenarios, shared by the portal (which
writes and validates it) and Verify (which checks it against the deployments).

Format 3 (twinlab2 package format; format 2 packages stay readable, see `upgrade`) adds:

- **fixture bindings**: per scenario, each step that types or selects is bound to one synthetic
  fixture field (`{"event": "<recording>#<event id>", "field": "Order number"}`) or to a shared
  identifier of the interface contract (`{"event": ..., "identifier": "order-number"}`). The
  fixture fields are the scenario's example lines, `Field: value` or `Field (type): value`;
  `TYPES` are the types a field can have (inferred from the value when not written);
- **visual baselines**: each approved baseline names one screen, one recorded state of it, the
  viewport (`1280x720`), theme (`light` or `dark`) and scale (`1`, `2`, ...) it was taken at, and
  the picture in the package (`baselines/<id>.png`);
- **interfaces**: for applications recorded together, the interface contract: shared synthetic
  identifiers (with the application that produces each and those that consume it), the producer
  and consumer relationships (by identifier, API call or file, with their schema), the launch
  and reset order, and what each consumer does when its producer is down.

Everything here is pure: no database, no web framework.
"""

from __future__ import annotations

import re

PACKAGE_FORMAT = 3
READABLE_FORMATS = (2, 3)
INTERFACES_VERSION = 1

TYPES = ("text", "number", "email", "date", "phone", "option", "identifier")
THEMES = ("light", "dark")
LEVELS = ("documented", "observed", "owner-confirmed", "replay-tested")
RELATIONSHIP_KINDS = ("identifier", "api", "file")

EXAMPLE = re.compile(r"^\s*(?P<field>[^:=()]{1,60}?)\s*(?:\((?P<type>[a-z]+)\))?\s*[:=]\s*(?P<value>\S.*)$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
NUMBER = re.compile(r"^[-+]?(?:\d{1,3}(?:[,. ']\d{3})+|\d+)(?:[.,]\d+)?(?:\s?[A-Z]{3}|\s?%)?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
PHONE = re.compile(r"^\+?[\d ()-]{7,20}$")
VIEWPORT = re.compile(r"^\d{2,5}x\d{2,5}$")
SLUG = re.compile(r"[^a-z0-9]+")


def slug(text) -> str:
    """An identifier name from a field name: `Order number` -> `order-number`."""
    return SLUG.sub("-", str(text or "").lower()).strip("-")[:60]


def infer_type(value) -> str:
    """The type a synthetic value looks like: email, date, number, phone, else text."""
    v = str(value or "").strip()
    if EMAIL.match(v):
        return "email"
    if DATE.match(v):
        return "date"
    if NUMBER.match(v):
        return "number"
    if PHONE.match(v) and sum(c.isdigit() for c in v) >= 7:
        return "phone"
    return "text"


def fixture_fields(example_lines) -> list[dict]:
    """The synthetic fixture fields of a scenario's example lines: [{field, type, value}], the
    type written (`Field (email): x`) or inferred. Lines without `Field: value` are not fields.
    A value ending in a parenthesis such as `(synthetic)` keeps it out of the value."""
    out, seen = [], set()
    for line in example_lines or []:
        for part in re.split(r";\s+", str(line)):
            m = EXAMPLE.match(part)
            if not m:
                continue
            name = m["field"].strip()
            value = re.sub(r"\s*\((?:synthetic|made[- ]up)[^)]*\)\s*\.?$", "", m["value"].strip(), flags=re.I).strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            kind = m["type"] if m["type"] in TYPES else infer_type(value)
            out.append({"field": name, "type": kind, "value": value})
    return out


def control_type(control) -> str | None:
    """The value type a recorded control accepts: email, number, date, phone, option (a list with
    its options), or None (any text)."""
    if not isinstance(control, dict):
        return None
    if control.get("option_texts") or control.get("kind") in ("select", "combobox", "listbox") or "options" in control:
        return "option"
    kind = str(control.get("type") or "").lower()
    if kind in ("email",):
        return "email"
    if kind in ("number", "range") or control.get("numeric"):
        return "number"
    if kind in ("date", "datetime-local", "month"):
        return "date"
    if kind == "tel":
        return "phone"
    return None


def incompatible(field, control) -> str | None:
    """Why a fixture field's value cannot go into a recorded control, or None."""
    want = control_type(control)
    value = str((field or {}).get("value") or "")
    if want is None:
        return None
    if want == "option":
        options = [str(o) for o in control.get("option_texts") or []]
        if options and value not in options:
            return f"\"{value}\" is not one of the options ({', '.join(options[:8])})"
        return None
    if infer_type(value) != want and not (want == "number" and NUMBER.match(value)):
        return f"the control takes a{'n' if want[0] in 'aeiou' else ''} {want}, \"{value}\" is not one"
    return None


def baseline_key(b) -> tuple:
    """What one approved baseline is for: (screen, state, viewport, theme, scale)."""
    return (b.get("screen"), b.get("state"), b.get("viewport"), b.get("theme"), str(b.get("scale")))


def check_baseline(b) -> list[str]:
    """What is wrong with a baseline's description (empty when it is complete)."""
    out = []
    if not b.get("screen") or not b.get("state"):
        out.append("name the screen and its recorded state")
    if not VIEWPORT.match(str(b.get("viewport") or "")):
        out.append("give the viewport as WIDTHxHEIGHT, for example 1280x720")
    if b.get("theme") not in THEMES:
        out.append("choose the theme: light or dark")
    try:
        if not 0.5 <= float(b.get("scale")) <= 4:
            raise ValueError
    except (TypeError, ValueError):
        out.append("give the scale (1, 1.5, 2, ...)")
    return out


def empty_interfaces() -> dict:
    return {"version": INTERFACES_VERSION, "apps": [], "identifiers": [], "relationships": [],
            "launch_order": [], "reset_order": []}


def check_interfaces(doc) -> list[str]:
    """What is structurally wrong with an interface contract (as an owner edits it)."""
    out = []
    if not isinstance(doc, dict):
        return ["the interface contract is not an object"]
    apps = {a.get("id") for a in doc.get("apps") or [] if isinstance(a, dict)}
    names = set()
    for i in doc.get("identifiers") or []:
        if not isinstance(i, dict) or not slug(i.get("name")):
            out.append("every shared identifier needs a name")
            continue
        if slug(i["name"]) in names:
            out.append(f"identifier {i['name']} is listed twice")
        names.add(slug(i["name"]))
        if i.get("type", "identifier") not in TYPES:
            out.append(f"identifier {i['name']}: unknown type {i.get('type')}")
        if i.get("producer") is not None and i.get("producer") not in apps:
            out.append(f"identifier {i['name']}: its producer is not one of the applications")
    for r in doc.get("relationships") or []:
        if not isinstance(r, dict) or r.get("kind") not in RELATIONSHIP_KINDS:
            out.append("every relationship is an identifier, api or file relationship")
            continue
        if r.get("producer") not in apps or r.get("consumer") not in apps or r.get("producer") == r.get("consumer"):
            out.append("a relationship names two different applications of the contract")
        if r["kind"] == "identifier" and slug(r.get("identifier")) not in names:
            out.append(f"relationship by identifier {r.get('identifier')}: no such shared identifier")
    for key in ("launch_order", "reset_order"):
        order = doc.get(key) or []
        if not isinstance(order, list) or len(set(order)) != len(order) or set(order) - apps:
            out.append(f"{key.replace('_', ' ')} lists each application of the contract at most once")
    return out


def upgrade(doc) -> dict:
    """An evidence.json of any readable format in the format-3 shape (a format-2 package has no
    bindings, baselines or interfaces: they are empty). ValueError for an unknown format."""
    if not isinstance(doc, dict):
        raise ValueError("evidence.json is not an object")
    fmt = doc.get("format", 2)
    if fmt not in READABLE_FORMATS:
        raise ValueError(f"package format {fmt} is not readable here (readable: {READABLE_FORMATS})")
    out = dict(doc)
    out["scenarios"] = [dict(s, bindings=list(s.get("bindings") or [])) if isinstance(s, dict) else s
                        for s in doc.get("scenarios") or []]
    out.setdefault("visual_baselines", [])
    out.setdefault("interfaces", [])
    return out
