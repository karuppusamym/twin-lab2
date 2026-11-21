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


# -- the "AI generated Twin App" label -------------------------------------------------------------
# Every rebuilt application shows that it is an AI generated twin, on every screen. The package
# requires it (`target.twin_label`), the validator refuses a package without the requirement, and
# Verify checks it on every replayed screen and leaves it out of the structural and visual
# comparisons (it is not part of the original), so it never makes a step differ.

TWIN_LABEL = "AI generated Twin App"
TWIN_META = "twin-app"  # web: <meta name="twin-app" content="AI generated Twin App">
TWIN_ATTR = "data-twin-app"  # web: the banner element carries it
TWIN_AID = "TwinAppLabel"  # desktop: the AutomationId of the status-bar or footer label
TWIN_TITLE_SUFFIX = " – " + TWIN_LABEL  # desktop: appended to every top-level window title
_META = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']?" + TWIN_META + r"[\"'\s>][^>]*>", re.I)
_BANNER = re.compile(r"<(?P<tag>[a-z][a-z0-9]*)\b[^>]*\b" + TWIN_ATTR + r"\b[^>]*>.*?</(?P=tag)\s*>", re.I | re.S)


def twin_label(surface, host_rows=None) -> dict:
    """The label requirement a package carries for one surface. `host_rows`: the rows a 3270
    application's recorded screens use (the label goes on the last row none of them uses, else
    the last row of a 24-row screen)."""
    req = {"text": TWIN_LABEL, "surfaces": {}}
    if surface == "web":
        req["surfaces"]["web"] = {
            "meta": f'<meta name="{TWIN_META}" content="{TWIN_LABEL}">',
            "banner": f'a fixed banner on every page, an element with the attribute {TWIN_ATTR} whose text is '
                      f'"{TWIN_LABEL}", outside every recorded element (for example the first child of <body>, '
                      "position: fixed), not interactive"}
    elif surface == "desktop":
        req["surfaces"]["desktop"] = {
            "title_suffix": TWIN_TITLE_SUFFIX,
            "status": f'a status-bar or footer label (AutomationId "{TWIN_AID}", ControlType Text) whose text is '
                      f'"{TWIN_LABEL}", in every top-level window'}
    elif surface == "mainframe":
        used = {r for r in host_rows or [] if isinstance(r, int)}
        row = next((r for r in range(24, 1, -1) if r not in used), 24)
        req["surfaces"]["mainframe"] = {
            "row": row, "col": 2,
            "field": f'a protected field at row {row}, column 2, with the text "{TWIN_LABEL}", on every screen; '
                     "it moves no recorded field (the row is one the recorded screens leave empty)"}
    return req


def html_has_label(html) -> bool:
    """The page carries the twin meta tag and the banner with its text."""
    html = str(html or "")
    banner = _BANNER.search(html)
    return bool(_META.search(html)) and banner is not None and TWIN_LABEL in banner.group(0)


def strip_label_html(html) -> str:
    """The page without the twin meta tag and banner: what the structural comparison reads."""
    return _BANNER.sub("", _META.sub("", str(html or "")))


def tree_has_label(tree) -> bool:
    """A desktop window tree shows the title suffix and the label control."""
    if not isinstance(tree, dict) or not str(tree.get("name") or "").endswith(TWIN_TITLE_SUFFIX):
        return False
    stack = list(tree.get("children") or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("aid") == TWIN_AID and TWIN_LABEL in str(node.get("name") or ""):
                return True
            stack += node.get("children") or []
    return False


def strip_label_tree(tree):
    """The window tree without the label control and the title suffix."""
    if not isinstance(tree, dict):
        return tree
    out = dict(tree)
    if str(out.get("name") or "").endswith(TWIN_TITLE_SUFFIX):
        out["name"] = out["name"][: -len(TWIN_TITLE_SUFFIX)]
    if out.get("children"):
        out["children"] = [strip_label_tree(c) for c in out["children"]
                           if not (isinstance(c, dict) and c.get("aid") == TWIN_AID)]
    return out
