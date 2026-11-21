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
  AutomationId when it is an identifier; else a part of its title only when it is the
  application's own name; else none (a title names the record or document open).
- web: the element's id or name from the selector when it is an identifier; the control is the
  tag. Never link text, row text, an aria-label or a quoted text selector. The screen is the
  origin and a route template: the first path segment when it is an identifier, then only the
  common words of routes (`ROUTE_WORDS`: edit, view, new, search...); every other segment
  becomes `{id}` (a segment can be a record: `/customers/jane-doe`); no query, fragment or user
  information.
- mainframe: a field by its label, a key by its name, a screen by its title (see tn3270.py).

`plain(text)`: at most 40 characters, no digit, no `@`, not a shape the recorder wrote
(`X-9999`: a run of X/x/9 with punctuation), and not empty.
"""

from __future__ import annotations

import re
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
    return plain(text) and bool(IDENT.match(str(text).strip()))


def _uia(selector: object) -> dict:
    s = str(selector or "")
    if s.startswith("uia:"):
        s = s[4:]
    return {k: v for k, v in UIA_PART.findall(s)}


def route_template(url: str) -> str:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    segs = []
    for seg in (parts.path or "/").split("/"):
        if not seg:
            continue
        keep = seg.lower() in ROUTE_WORDS or (not segs and ident(seg) and len(seg) <= 24)
        segs.append(seg if keep else "{id}")
    return "/" + "/".join(segs)


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
    if not (screen and ident(screen)):
        # A title names the record or document open ("Jane Doe - App"): only a part that is the
        # application's own name is kept.
        own = {str(v).strip().lower() for v in (app or {}).values() if isinstance(v, str)}
        own |= {o.rsplit(".", 1)[0] for o in own if o.endswith(".exe")}
        title = str(window.get("title") or "")
        parts = [p.strip() for p in title.split(" - ")] if title else []
        screen = next((p for p in parts if p.lower() in own and plain(p)), None)
    return {"control": control if control and plain(control) else None, "label": label,
            "screen": screen}  # fmt: skip


def web_step(step: dict) -> dict:
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
            "screen": f"{origin}{route_template(url)}" if origin else None}  # fmt: skip


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


def label_step(step: dict, n: int, app: dict | None = None) -> dict:
    """One flow step of the portal's references."""
    action = str(step.get("action") or "")
    out = {"n": n, "action": action if action in ACTIONS else "other"}
    surface = step.get("surface")
    if surface == "desktop":
        facts = desktop_step(step, app)
    elif surface == "mainframe":
        facts = mainframe_step(step)
    else:
        facts = web_step(step)
    out.update({k: v for k, v in facts.items() if v})
    if step.get("key") and str(step["key"]) in {
        "Enter", "Tab", "Esc", "Escape", "Delete", "Backspace", "F1", "F2", "F3", "F4", "F5",
        "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    }:  # fmt: skip
        out["key"] = str(step["key"])
    return out
