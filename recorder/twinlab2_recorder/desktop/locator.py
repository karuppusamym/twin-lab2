# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Locators for desktop controls: one canonical syntax, and its UiPath, pywinauto and WinAppDriver
forms.

Canonical syntax (what the app model, the brief and verify use):

    uia:AutomationId=txtCase;ControlType=Edit
    uia:Name=Save;ControlType=Button
    uia:ControlType=Edit;ClassName=WindowsForms10.Edit.app.0.*;Index=2

Properties in this order: AutomationId, Name, ControlType, ClassName, FrameworkId, Index. `;`, `=`
and `\\` inside a value are escaped with `\\`. `*` in ClassName matches anything (class names
carry a per-build suffix in WinForms and WPF, which is normalised away). Index is the position
among siblings of the same ControlType (0-based), used only when nothing else identifies the
control. A locator is scoped to its window: the window itself has one in the same syntax, and the
ancestor path (window first, parent last) is kept beside it:

    path: [uia:AutomationId=MainForm;ControlType=Window, uia:AutomationId=dgvCases;ControlType=Table]

Matching is exact on every property the locator names; a locator that matches more than one
control in its window is **ambiguous**, and is reported as such, never resolved by taking the
first match.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

PREFIX = "uia:"
JAB_PREFIX = "jab:"  # a Java control, through the Java Access Bridge: Name and Role
# Has (windows only): the window contains a control of this role. It tells a Java message box
# (`Has=option pane`) from the dialog it answers when both have the same title and class; UiPath
# anchors the same way, on the `<java role='option pane' />` inside the `<wnd>`.
ORDER = ("AutomationId", "Name", "ControlType", "Role", "ClassName", "FrameworkId", "Has", "Index")
KEY_OF = {"AutomationId": "aid", "Name": "name", "ControlType": "type", "Role": "role",
          "ClassName": "cls", "FrameworkId": "fw"}  # fmt: skip
# Class names with a per-build or per-run suffix, and how far they are kept.
CLASS_SUFFIX = [
    (re.compile(r"^(WindowsForms10\.[\w.]+?\.app\.\d+\.).*$"), r"\1*"),
    (re.compile(r"^(HwndWrapper\[[^;\]]*;;).*\]$"), r"\1*]"),
    (re.compile(r"^(Afx:).*$"), r"\1*"),
    (re.compile(r"^(GlassWndClass-GlassWindowClass-)\d+$"), r"\1*"),
    (re.compile(r"^(ATL:)[0-9A-Fa-f]+$"), r"\1*"),
]
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def normalise_class(cls: str | None) -> str | None:
    if not cls:
        return cls
    for rx, repl in CLASS_SUFFIX:
        if rx.match(cls):
            return rx.sub(repl, cls)
    return cls


def stable_id(aid: str | None) -> bool:
    """Whether an AutomationId can identify a control across runs. Win32 gives numeric control
    ids (stable within one dialog template, but not across builds); GUIDs and runtime ids are not
    stable; an empty id says nothing."""
    if not aid:
        return False
    if aid.isdigit() or GUID.search(aid) or re.fullmatch(r"[\d.,;-]+", aid):
        return False
    return "9999" not in aid  # masked: a record's identifier, not the control's


def _esc(value: str) -> str:
    return re.sub(r"([\\;=])", r"\\\1", str(value))


def format(props: dict) -> str:  # noqa: A001 - the locator's text form
    parts = [f"{k}={_esc(props[k])}" for k in ORDER if props.get(k) not in (None, "")]
    return (JAB_PREFIX if props.get("Role") else PREFIX) + ";".join(parts)


def _kind(p: dict) -> dict:
    """What kind of control: its Role for a Java control, else its ControlType."""
    return {"Role": p["Role"]} if p.get("Role") else {"ControlType": p.get("ControlType")}


def parse(selector: str) -> dict:
    """`uia:AutomationId=x;ControlType=Edit` -> {"AutomationId": "x", "ControlType": "Edit"}."""
    text = selector.split(":", 1)[1] if selector.startswith((PREFIX, JAB_PREFIX)) else selector
    out, key, buf, esc = {}, None, [], False
    for ch in text + ";":
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "=" and key is None:
            key, buf = "".join(buf), []
        elif ch == ";":
            if key:
                out[key] = "".join(buf)
            key, buf = None, []
        else:
            buf.append(ch)
    if "Index" in out:
        try:
            out["Index"] = int(out["Index"])
        except ValueError:
            pass
    return out


def props_of(node: dict) -> dict:
    """All locator properties of a snapshot node (masked names never count: they are data)."""
    if node.get("role"):  # a Java control: role and name are what a bot has
        out = {"Role": node["role"]}
        if node.get("name") and node.get("name_is") in ("structure", "title"):
            out["Name"] = node["name"]
        if node.get("cls"):
            out["ClassName"] = normalise_class(node["cls"])
        return out
    out = {"ControlType": node.get("type")}
    if node.get("aid"):
        out["AutomationId"] = node["aid"]
    if node.get("name") and node.get("name_is") in ("structure", "title"):
        out["Name"] = node["name"]
    if node.get("cls"):
        out["ClassName"] = normalise_class(node["cls"])
    if node.get("fw"):
        out["FrameworkId"] = node["fw"]
    return out


def matches(node: dict, want: dict, index: int | None = None) -> bool:
    for key, value in want.items():
        if key == "Index":
            if index is not None and index != value:
                return False
            continue
        have = node.get(KEY_OF[key]) if key in KEY_OF else None
        if key == "ClassName":
            have = normalise_class(have)
            if isinstance(value, str) and value.endswith("*"):
                if not (have or "").startswith(value[:-1]):
                    return False
                continue
        if key == "Has":
            if not any(n.get("role") == value for n, _a, _i in walk(node)):
                return False
            continue
        if key == "Name" and node.get("name_is") not in ("structure", "title"):
            return False
        if have != value:
            return False
    return True


def walk(tree: dict, path: tuple = ()) -> Iterator[tuple[dict, tuple, int]]:
    """Every node with its ancestors (window first) and its index among same-type siblings."""
    yield tree, path, tree.get("idx", 0) if path else 0
    counts: dict[tuple, int] = {}
    for child in tree.get("children") or []:
        t = (child.get("type"), child.get("role"))
        n = counts.get(t, 0)
        counts[t] = n + 1
        child = dict(child)
        child["idx"] = n
        yield from walk(child, (*path, tree))


def find(tree: dict, selector: str, path: list[str] | None = None) -> list[tuple[dict, tuple]]:
    """Every node of a window tree the locator matches. With `path`, only nodes whose ancestors
    match that path (window first), each step by its own locator."""
    want = parse(selector)
    steps = [parse(p) for p in path or []]
    out, seen = [], set()
    for node, ancestors, idx in walk(tree):
        if not matches(node, want, idx if "Index" in want else None):
            continue
        if steps and not _path_ok(ancestors, steps):
            continue
        # One control listed twice (Java lists an open menu's items under the menu and in its
        # popup): same kind, name and place on the screen.
        key = _same_control(node)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append((node, ancestors))
    return out


def _same_control(node: dict):
    rect = node.get("rect")
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return None
    return (node.get("type"), node.get("role"), node.get("name"), tuple(rect))


def _path_ok(ancestors: tuple, steps: list[dict]) -> bool:
    if len(ancestors) < len(steps):
        return False
    tail = ancestors[-len(steps) :] if steps else ()
    # An unnamed container in the path (Java's root pane, layered pane and panels) is matched by
    # its kind only: toolkits reorder and add such containers between runs.
    return all(matches(a, s, a.get("idx", 0) if "Index" in s and _named_step(s) else None)
               for a, s in zip(tail, steps, strict=True))  # fmt: skip


def _named_step(step: dict) -> bool:
    return bool(step.get("Name") or step.get("AutomationId"))


def step_of(node: dict, idx: int = 0) -> str:
    """One step of an ancestor path: the node's own best locator, without uniqueness checks."""
    p = props_of(node)
    k = _kind(p)
    if stable_id(p.get("AutomationId")):
        return format({"AutomationId": p["AutomationId"], **k})
    if p.get("Name"):
        return format({"Name": p["Name"], **k})
    return format({**k, "ClassName": p.get("ClassName"), "Index": idx})


def build(tree: dict, node: dict, ancestors: tuple, idx: int) -> dict:
    """The locator of `node` in its window `tree`: the primary selector (the shortest set of
    properties that is unique in the window), the ancestor path, fallback candidates, and how
    many controls the primary matches (`matches` > 1 means ambiguous)."""
    p = props_of(node)
    k = _kind(p)
    tries: list[dict] = []
    aid_ok = stable_id(p.get("AutomationId"))
    if aid_ok:
        tries.append({"AutomationId": p["AutomationId"], **k})
    if p.get("Name"):
        tries.append({"Name": p["Name"], **k})
    if aid_ok and p.get("Name"):
        tries.append({"AutomationId": p["AutomationId"], "Name": p["Name"], **k})
    if aid_ok:
        tries.append({"AutomationId": p["AutomationId"], **k, "ClassName": p.get("ClassName")})
    tries.append({**k, "ClassName": p.get("ClassName"), "Index": idx})
    path = [step_of(a, a.get("idx", 0)) for a in ancestors]
    seen, candidates = set(), []
    primary, count = None, 0
    for t in tries:
        sel = format(t)
        if sel in seen:
            continue
        seen.add(sel)
        n = len(find(tree, sel, path if "Index" in t else None))
        candidates.append({"selector": sel, "matches": n, "scoped": "Index" in t})
        if primary is None and n == 1:
            primary, count = sel, 1
    if primary is None:  # nothing flat is unique: keep the first try and say it is ambiguous
        primary = candidates[0]["selector"]
        count = candidates[0]["matches"]
    out = {
        "selector": primary,
        "matches": count,
        "path": path,
        "candidates": [c for c in candidates if c["selector"] != primary],
        "props": p,
        "index": idx,
    }
    if count > 1:
        out["ambiguous"] = True
    return out


ANCHOR_ROLES = ("password text", "table", "combo box", "text", "check box", "push button")


def window_locator(tree: dict, process: str | None = None) -> dict:
    """A top-level window: its AutomationId when stable, else its title with its ClassName (a
    message box `#32770` and the dialog it answers often share a title)."""
    p = props_of(tree)
    if stable_id(p.get("AutomationId")):
        sel = format({"AutomationId": p["AutomationId"], **_kind(p)})
    else:
        has = None
        if p.get("Role"):  # Java: titles and classes repeat; anchor on what the window holds
            roles = {n.get("role") for n, _a, _i in walk(tree)}
            box = tree.get("message_box")
            has = (
                (box if isinstance(box, str) else "option pane")
                if box
                else next((r for r in ANCHOR_ROLES if r in roles), None)
            )
        sel = format(
            {"Name": p.get("Name"), **_kind(p), "ClassName": p.get("ClassName"), "Has": has}
        )
    return {"selector": sel, "props": p, "process": process}


# --- other tools' forms ------------------------------------------------------------------------

ROLE = {"Edit": "editable text", "Button": "push button", "CheckBox": "check box",
        "RadioButton": "radio button", "ComboBox": "combo box", "ListItem": "list item",
        "List": "list", "MenuItem": "menu item", "MenuBar": "menu bar", "TabItem": "page tab",
        "Tab": "page tab list", "Text": "text", "DataItem": "row", "Table": "table",
        "TreeItem": "outline item", "Tree": "outline", "HeaderItem": "column header",
        "Window": "window", "Pane": "pane", "Document": "editable text", "Hyperlink": "link",
        "Group": "grouping", "StatusBar": "status bar", "ToolBar": "tool bar"}  # fmt: skip


def _attr(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("'", "&apos;")


def to_uipath(selector: str, window: str | None = None, process: str | None = None) -> str:
    """A UiPath selector: `<wnd app='casedesk.exe' ... /><uia automationid='txtCase' ... />`.

    Mapping: the window becomes `<wnd>` (app = the executable in lower case, ctrlname or title,
    cls); the control becomes `<uia>` with automationid, name, cls and role (ControlType in
    UiPath's words), `idx` for Index (UiPath counts from 1). Wildcards stay `*`. Check the result
    once in UiPath's UI Explorer: UiPath may prefer `<ctrl>` (MSAA) or `<wnd ctrlname>` (WinForms)
    on some controls."""
    parts = []
    w = parse(window) if window else {}
    wnd = []
    if process:
        wnd.append(f"app='{_attr(process.lower())}'")
    if w.get("AutomationId"):
        wnd.append(f"ctrlname='{_attr(w['AutomationId'])}'")
    elif w.get("Name"):
        wnd.append(f"title='{_attr(w['Name'])}'")
    if w.get("ClassName"):
        wnd.append(f"cls='{_attr(w['ClassName'])}'")
    if wnd:
        parts.append(f"<wnd {' '.join(wnd)} />")
    p = parse(selector)
    if selector.startswith(JAB_PREFIX):
        # UiPath's Java selectors (UiPath Java extension, through the Access Bridge):
        # `<java role='push button' name='Save' idx='n' />` under the window's `<wnd>`.
        java = [f"role='{_attr(p['Role'])}'"] if p.get("Role") else []
        if p.get("Name"):
            java.append(f"name='{_attr(p['Name'])}'")
        if isinstance(p.get("Index"), int):
            java.append(f"idx='{p['Index'] + 1}'")
        parts.append(f"<java {' '.join(java)} />")
        return "".join(parts)
    uia = []
    if p.get("AutomationId"):
        uia.append(f"automationid='{_attr(p['AutomationId'])}'")
    if p.get("ClassName"):
        uia.append(f"cls='{_attr(p['ClassName'])}'")
    if p.get("Name"):
        uia.append(f"name='{_attr(p['Name'])}'")
    if p.get("ControlType"):
        uia.append(f"role='{ROLE.get(p['ControlType'], p['ControlType'].lower())}'")
    if isinstance(p.get("Index"), int):
        uia.append(f"idx='{p['Index'] + 1}'")
    parts.append(f"<uia {' '.join(uia)} />")
    return "".join(parts)


def to_pywinauto(selector: str) -> dict:
    """Keyword arguments for pywinauto's `child_window(...)` with `backend="uia"`."""
    p = parse(selector)
    out: dict = {}
    if p.get("AutomationId"):
        out["auto_id"] = p["AutomationId"]
    if p.get("Name"):
        out["title"] = p["Name"]
    if p.get("ControlType"):
        out["control_type"] = p["ControlType"]
    if p.get("ClassName"):
        cls = p["ClassName"]
        out["class_name_re" if cls.endswith("*") else "class_name"] = (
            re.escape(cls[:-1]) + ".*" if cls.endswith("*") else cls
        )
    if isinstance(p.get("Index"), int):
        out["found_index"] = p["Index"]
    return out


def to_winappdriver(selector: str) -> dict:
    """A WinAppDriver (Appium) locator: AccessibilityId when there is an AutomationId, else
    Name, else an XPath on the ControlType."""
    p = parse(selector)
    if p.get("AutomationId") and len(p) == 2:
        return {"using": "accessibility id", "value": p["AutomationId"]}
    conds = []
    if p.get("AutomationId"):
        conds.append(f"@AutomationId='{p['AutomationId']}'")
    if p.get("Name"):
        conds.append(f"@Name='{p['Name']}'")
    if p.get("ClassName") and not p["ClassName"].endswith("*"):
        conds.append(f"@ClassName='{p['ClassName']}'")
    pred = f"[{' and '.join(conds)}]" if conds else ""
    idx = f"[{p['Index'] + 1}]" if isinstance(p.get("Index"), int) else ""
    return {"using": "xpath", "value": f"//{p.get('ControlType', '*')}{pred}{idx}"}


def forms(selector: str, window: str | None = None, process: str | None = None) -> dict:
    if selector.startswith(JAB_PREFIX):
        p = parse(selector)
        return {"uipath": to_uipath(selector, window, process),
                "java_access_bridge": {k: v for k, v in (("role", p.get("Role")),
                                       ("name", p.get("Name")), ("index", p.get("Index")))
                                       if v is not None},
                "pywinauto": None, "winappdriver": None,
                "note": "Java Swing/AWT is not reachable through UI Automation: pywinauto and "
                "WinAppDriver cannot find it; use a Java Access Bridge client"}  # fmt: skip
    return {"uipath": to_uipath(selector, window, process), "pywinauto": to_pywinauto(selector),
            "winappdriver": to_winappdriver(selector)}  # fmt: skip
