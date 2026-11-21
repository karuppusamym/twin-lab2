# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Step labels a portal may receive, so that a label is never a customer's data.

A flow step is `{"n", "action", "control", "label", "screen"}`:

- desktop: the control's AutomationId when it is an identifier (`IDENT`) and passes `plain()`;
  else its Name, only for a control type whose name is the application's own words (`NAMED`:
  buttons, menus, check boxes, an input's label), at most three words, passing `plain()`; else
  no label. Never the name of a tab, a window, a group, a Text, ListItem, DataItem, TreeItem,
  Hyperlink, Custom or Document (they are named after records). The screen is the window's
  AutomationId when it is an identifier; for a message box (`#32770`, which has none) its
  title when it is plain and at most four words; else a part of its title only when it is the
  application's own name; else none (a title names the record or document open).
- web: the element's id or name from the selector when it is an identifier; the control is the
  tag. Never link text, row text, an aria-label or a quoted text selector. The screen is the
  origin and a route template: the application's path prefix when the URL is under one the
  session's targets name (the portal's catalog: `/finance/billing`, the longest one winning,
  whole segments only), else the first path segment when it is an identifier; after it the
  common words of routes (`ROUTE_WORDS`: edit, view, new, search...) and any segment of two to
  24 lower-case letters (`route_word`: `/orders/new/items`), except right after a segment that
  names people or accounts (`PEOPLE`: `/users/jdoe` keeps `{id}`); every other segment
  becomes `{id}` (a segment can be a record: `/customers/jane-doe`, `/orders/10442`, a UUID,
  an e-mail address); no query, fragment or user
  information. A prefix is kept as the target names it, never read from the URL, so a record's
  segment is never kept because it is in the place where a prefix could be.
- mainframe: a field by its label, a key by its name, a screen by its title (see tn3270.py).

`plain(text)`: at most 40 characters, no digit, no `@`, not a shape the recorder wrote
(`X-9999`: a run of X/x/9 with punctuation), and not empty. `ident(text)`: the contract's rule
for an identifier (twinlab2_contract.structure.ident): letters, `_`, `.`, `-` and at most one
digit run of at most two digits after a letter (`addressLine2`, `button1`, `btn2FA`), never an
all-digit id, a long or repeated digit run or a counter after a separator (`mat-input-0`).
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlsplit

from twinlab2_recorder.workstation.levels import origin_of

NAMED = {"Button", "CheckBox", "RadioButton", "MenuItem", "Menu", "MenuBar", "ComboBox", "Edit",
         "ToolBar", "SplitButton", "HeaderItem"}  # fmt: skip
IDENT = re.compile(r"^[A-Za-z_][A-Za-z_.\-]*$")
ROUTE_WORDS = {
    "edit", "view", "new", "create", "add", "update", "delete", "remove", "search", "find",
    "list", "detail", "details", "show", "summary", "history", "overview", "settings", "home",
    "login", "logout", "signin", "signout", "dashboard", "index", "results", "report", "reports",
    "admin", "profile", "help", "confirm", "review", "submit", "approve", "status", "api",
}  # fmt: skip
NEVER = {"Text", "ListItem", "DataItem", "TreeItem", "Hyperlink", "Custom", "Document", "Image"}
SHAPE = re.compile(r"^[Xx9\W_]+$")
UIA_PART = re.compile(r"(\w+)=([^;]*)")
WEB_ID = re.compile(r"#([A-Za-z_][\w-]*)")
WEB_ATTR = re.compile(r"\[(?:name|id|data-testid|data-test)\s*=\s*['\"]?([^'\"\]]+)['\"]?\]")
WEB_TAG = re.compile(r"^\s*([a-z][a-z0-9-]*)")
ACTIONS = {
    "click", "dblclick", "double_click", "right_click", "type", "select", "toggle", "expand",
    "collapse", "menu", "menu_open", "key", "keypress", "check", "uncheck", "submit", "goto",
    "download", "window_open", "window_close", "focus", "tab-open", "tab-switch", "tab-close",
    "screen", "aid",
}  # fmt: skip


def plain(text: object) -> bool:
    s = str(text or "").strip()
    if not s or len(s) > 40 or "@" in s or any(c.isdigit() for c in s):
        return False
    return not SHAPE.match(s)


def ident(text: object) -> bool:
    """An identifier (twinlab2_contract.structure.ident, the contract's one rule): letters, `_`,
    `.`, `-`, and at most one short digit run as developers name things (`addressLine2`)."""
    from twinlab2_contract.structure import ident as contract_ident

    return contract_ident(text)


def _uia(selector: object) -> dict:
    s = str(selector or "")
    if s.startswith("uia:"):
        s = s[4:]
    return {k: v for k, v in UIA_PART.findall(s)}


def prefix_segments(path: object) -> tuple[str, ...]:
    """A target's path prefix as its segments (`/finance/billing` -> ("finance", "billing")):
    empty when it is not a plain path (a query, fragment, `.`/`..` or an empty segment)."""
    text = str(path or "").strip()
    if not text.startswith("/") or len(text) > 120 or re.search(r"[?#\\\s]", text):
        return ()
    segs = tuple(text.strip("/").split("/")) if text.strip("/") else ()
    if any(not seg or seg in (".", "..") for seg in segs):
        return ()
    return segs


def route_template(url: str, prefixes: Iterable[str] = ()) -> str:
    """The URL's path as a route template (see the module). `prefixes`: the path prefixes the
    session's targets name on this URL's origin; the longest one the path starts with (whole
    segments) is kept as it is."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    path = [seg for seg in (parts.path or "/").split("/") if seg]
    best: tuple[str, ...] = ()
    for prefix in prefixes or ():
        segs = prefix_segments(prefix)
        if len(segs) > len(best) and tuple(path[: len(segs)]) == segs:
            best = segs
    out = list(best)
    for i in range(len(best), len(path)):
        seg, before = path[i], (path[i - 1] if i else "").lower()  # the segment as it was
        keep = seg.lower() in ROUTE_WORDS or (not out and ident(seg) and len(seg) <= 24) or (
            route_word(seg) and before not in PEOPLE
        )
        out.append(seg if keep else "{id}")
    return "/" + "/".join(out)


# A segment that names a person or an account when it comes right after one of these: kept as
# `{id}` even when it is a plain lower-case word (`/users/jdoe`, `/customers/priya`).
PEOPLE = {
    "user", "users", "u", "people", "person", "persons", "member", "members", "customer",
    "customers", "client", "clients", "account", "accounts", "employee", "employees", "staff",
    "profile", "profiles", "contact", "contacts", "patient", "patients", "author", "authors",
    "owner", "owners", "student", "students", "agent", "agents", "partner", "partners", "vendor",
    "vendors", "supplier", "suppliers", "team", "teams", "org", "orgs", "tenant", "tenants",
    "party", "parties", "applicant", "applicants", "beneficiary", "beneficiaries", "payee",
    "payees", "@", "~",
}  # fmt: skip
ROUTE_WORD = re.compile(r"[a-z]{2,24}")


def route_word(seg: str) -> bool:
    """A path segment that is a route's own word, kept as itself (`/orders/new/items`,
    `/billing/lookup`): two to 24 lower-case letters a to z, nothing else. A record's segment
    nearly always has a digit, an upper-case letter, a separator or more length (ids, UUIDs,
    order numbers, `jane-doe`, `Priya`, e-mail addresses); those stay `{id}`.

    The privacy side: a lower-case word can still be a username or a name used as a slug
    (`/users/jdoe`, `/u/priya`). Such a segment is `{id}` when it follows a segment that names
    people or accounts (`PEOPLE`), and a typed value in a whole path segment was already
    replaced by its shape when the step was cleaned (`Xxxx`: not lower-case letters). What is
    left is a lower-case single word that comes after another route word and is not something
    the person typed: a route's name in practice (the risk accepted, documented here)."""
    return bool(ROUTE_WORD.fullmatch(seg))


# The keys a flow step names by `key`: keyboard keys that are no data, and every 3270 attention
# key (an AID: Enter, PF1-PF24, PA1-PA3, Clear), so a mainframe step that sent PF3 or Clear keeps
# it in the flow, the transitions and the package (gap 20).
KEYS = frozenset(
    {"Enter", "Tab", "Esc", "Escape", "Delete", "Backspace", "Clear", "PA1", "PA2", "PA3"}
    | {f"F{n}" for n in range(1, 25)} | {f"PF{n}" for n in range(1, 25)}
)


def desktop_step(step: dict, app: dict | None = None) -> dict:
    target = step.get("target") or {}
    props = dict(target.get("props") or {})
    sel = _uia(target.get("selector") or step.get("selector"))
    control = str(props.get("ControlType") or sel.get("ControlType") or "") or None
    label = None
    aid = props.get("AutomationId") or sel.get("AutomationId")
    if aid and ident(aid):
        label = aid
    elif control in NAMED and control not in NEVER:
        name = props.get("Name") or sel.get("Name")
        if plain(name) and len(str(name).split()) <= 3:
            label = name
    window = step.get("window") or {}
    wprops = dict(window.get("props") or {})
    wsel = _uia(window.get("selector"))
    screen = wprops.get("AutomationId") or wsel.get("AutomationId")
    dialog = (wprops.get("ClassName") or wsel.get("ClassName")) == "#32770"
    title = " ".join(str(window.get("title") or "").split())
    if dialog and not (screen and ident(screen)) and plain(title) and len(title.split()) <= 4:
        # A message box (#32770) has no AutomationId: its title is the application's own
        # words ("Sign in", "Delete case"), so it names the dialog's screen.
        screen = title
    elif not (screen and ident(screen)):
        # A title names the record or document open ("Jane Doe - App"): only a part that is the
        # application's own name is kept.
        own = {str(v).strip().lower() for v in (app or {}).values() if isinstance(v, str)}
        own |= {o.rsplit(".", 1)[0] for o in own if o.endswith(".exe")}
        title = str(window.get("title") or "")
        parts = [p.strip() for p in title.split(" - ")] if title else []
        screen = next((p for p in parts if p.lower() in own and plain(p)), None)
    return {"control": control if control and plain(control) else None, "label": label,
            "screen": screen}  # fmt: skip


def web_step(step: dict, prefixes: Iterable[str] = ()) -> dict:
    sel = str(step.get("selector") or "")
    if ">>>" in sel:  # inside a frame: the element's own part
        sel = sel.rsplit(">>>", 1)[-1]
    label = None
    if "text=" not in sel and "has-text" not in sel and ":text" not in sel:
        for rx in (WEB_ID, WEB_ATTR):
            m = rx.search(sel)
            if m and ident(m.group(1)):
                label = m.group(1)
                break
    m = WEB_TAG.match(sel)
    control = m.group(1) if m and plain(m.group(1)) else None
    url = str(step.get("url") or "")
    origin = origin_of(url)
    return {"control": control, "label": label,
            "screen": f"{origin}{route_template(url, prefixes)}" if origin else None}  # fmt: skip


def mainframe_step(step: dict) -> dict:
    """A 3270 step: a field by its label (the protected text before it), a key by its name (from
    a fixed table), a screen by its title, else its layout id (a hash)."""
    screen = step.get("screen") or {}
    title = screen.get("title")
    out = {"screen": title if plain(title) else screen.get("id")}
    action = step.get("action")
    if action == "type":
        label = (step.get("field") or {}).get("label")
        out.update(control="field", label=label if plain(label) else None)
    elif action == "aid":
        out.update(control="key", label=str(step.get("key") or "")[:12] or None)
    elif action == "screen":
        out["control"] = "screen"
    return out


def label_step(step: dict, n: int, app: dict | None = None, prefixes: Iterable[str] = ()) -> dict:
    """One flow step of the portal's references. `prefixes`: a web step's target path prefixes
    on its origin (route_template)."""
    action = str(step.get("action") or "")
    out = {"n": n, "action": action if action in ACTIONS else "other"}
    surface = step.get("surface")
    if surface == "desktop":
        facts = desktop_step(step, app)
    elif surface == "mainframe":
        facts = mainframe_step(step)
    else:
        facts = web_step(step, prefixes)
    out.update({k: v for k, v in facts.items() if v})
    if step.get("key") and str(step["key"]) in KEYS:
        out["key"] = str(step["key"])
    return out
