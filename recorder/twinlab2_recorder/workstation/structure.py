"""The screen structure of a session's applications (recorder contract 1.3), from what the
collectors already wrote: the web recorder's after-snapshots (`dom/<seq>.html`, values blanked),
the desktop recorder's value-free control trees (`trees/<seq>.json`) and the 3270 proxy's field
tables (`screens/<seq>.json`).

For each step of an application recorded at level `full` that has one of those files, the
screen it was on (the flow label's `screen`) gets the state the file shows: its controls, regions
and grids (twinlab2_contract.structure has the shape and the word rules). States are deduplicated
per screen by content, and each keeps the event ids of the steps that showed it. A transition is
the step that took the application from one state to the next (a click, key, select or other
action between them, labelled as in the flow).

What is never read or sent: text content (except a button's own caption, an input's label and
column headers, each by the label rules, and never inside a table row), link text, values (a field
is `empty` or `filled`), option texts that are not a code list, identifiers that hold a digit or
look like a record. Steps-level applications have no files and get no structure.
"""

from __future__ import annotations

import json
from pathlib import Path

from twinlab2_contract import structure as st
from twinlab2_contract.evidence import dom

ACTIONS = {"click", "dblclick", "double_click", "right_click", "select", "keypress", "key", "aid",
           "check", "uncheck", "toggle", "menu", "expand", "collapse", "submit", "type"}  # fmt: skip
PREFERRED = ("click", "dblclick", "double_click", "aid", "keypress", "key", "select", "check",
             "uncheck", "toggle", "menu", "expand", "collapse", "submit")  # fmt: skip


# --- web --------------------------------------------------------------------------------------------


def _in_row(node: dom.Node) -> bool:
    """Inside a table's data row (a `tr` not in `thead`): what it holds is a record's."""
    cur = node.parent
    while cur is not None and cur.tag != "#document":
        if cur.tag == "tr" and not (cur.parent and cur.parent.tag == "thead") and not all(
            c.tag == "th" for c in cur.children if c.tag in ("td", "th")
        ):
            return True
        cur = cur.parent
    return False


def _labels_for(root: dom.Node) -> dict[str, str]:
    """{input id: the text of its <label for>}, as label words."""
    out = {}
    for node in dom._walk(root):  # noqa: SLF001 - the contract's own walk
        if node.tag == "label" and node.attrs.get("for") and not _in_row(node):
            text = st.words(dom._text(node))  # noqa: SLF001
            if text:
                out[node.attrs["for"]] = text
    return out


def _web_kind(node: dom.Node) -> str:
    if node.tag == "input":
        t = (node.attrs.get("type") or "text").lower()
        if t in ("checkbox", "radio"):
            return t
    return "link" if node.tag == "a" else node.tag


def _web_control(node: dom.Node, labels: dict[str, str], row: bool) -> dict:
    c: dict = {"kind": _web_kind(node), "tag": node.tag}
    if node.tag == "input" or (node.tag == "button" and node.attrs.get("type")):
        c["type"] = (node.attrs.get("type") or "text").lower()
    if node.id:
        c["id"] = node.id
    if node.attrs.get("name"):
        c["name"] = node.attrs["name"]
    classes = [x for x in node.classes if x not in dom.POINTER_CLASSES]
    if classes:
        c["classes"] = classes
    if not row:
        caption = None
        if node.tag == "button":
            caption = dom._text(node)  # noqa: SLF001
        elif node.tag == "input" and c.get("type") in ("submit", "button", "reset"):
            caption = node.attrs.get("value")
        elif node.id and node.id in labels:
            caption = labels[node.id]
        else:
            up = node.parent
            while up is not None and up.tag != "#document" and up.tag != "label":
                up = up.parent
            if up is not None and up.tag == "label" and node.tag != "a":
                own = dom._text(up).replace(dom._text(node), "")  # noqa: SLF001
                caption = own
        if caption is not None:
            c["label"] = caption
    c["visible"] = dom._visible(node)  # noqa: SLF001 - what the replay compares
    c["enabled"] = dom._enabled(node)  # noqa: SLF001
    for attr, key in (("readonly", "readonly"), ("required", "required")):
        if attr in node.attrs or node.attrs.get(f"aria-{attr}") == "true":
            c[key] = True
    if c["kind"] in ("checkbox", "radio"):
        c["checked"] = "checked" in node.attrs
    elif node.tag == "select":
        opts = [d for d in dom._walk(node) if d.tag == "option"]  # noqa: SLF001
        c["options"] = len(opts)
        texts = [dom._text(o) for o in opts]  # noqa: SLF001
        if not row and st.code_list(texts) is not None:
            c["option_texts"] = texts
    elif node.tag in ("input", "textarea") and c.get("type") not in ("submit", "button", "reset", "password"):
        value = node.attrs.get("value") if node.tag == "input" else (node.attrs.get("value") or dom._text(node))  # noqa: SLF001
        c["value"] = "filled" if (value or "").strip() else "empty"
    return c


def _signature(c: dict) -> tuple:
    return (c["kind"], c.get("tag"), c.get("type"), c.get("id"), c.get("name"), tuple(c.get("classes") or ()))


def web_state(html: str) -> dict:
    root = dom.parse_html(html)
    labels = _labels_for(root)
    controls: list[dict] = []
    regions: list[dict] = []
    grids: list[dict] = []
    repeats: dict[int, int] = {}  # index of a control repeated in table rows -> how many
    row_index: dict[tuple, int] = {}
    for node in dom._walk(root):  # noqa: SLF001 - page order, as the replay walks it
        if dom._is_interactive(node):  # noqa: SLF001
            row = _in_row(node)
            c = _web_control(node, labels, row)
            if row and not c.get("id"):
                sig = _signature(c) + (c["visible"], c["enabled"])
                if sig in row_index:
                    repeats[row_index[sig]] += 1
                    continue
                row_index[sig] = len(controls)
                repeats[len(controls)] = 1
            controls.append(c)
            continue
        if node.tag in ("table", "ul", "ol") and (node.tag == "table" or node.id):
            grid: dict = {"kind": "table" if node.tag == "table" else "list",
                          "visible": dom._visible(node)}  # noqa: SLF001
            if node.id:
                grid["id"] = node.id
            if node.tag == "table":
                heads = [d for d in dom._walk(node) if d.tag == "th"  # noqa: SLF001
                         and not _in_row(d)]  # fmt: skip
                grid["columns"] = [dom._text(h) for h in heads]  # noqa: SLF001
                state = dom._state(node, "#")  # noqa: SLF001 - the replay's own row count
                grid["rows"] = st.rows_shape(state.rows or 0)
            else:
                grid["rows"] = st.rows_shape(sum(1 for d in node.children if d.tag == "li"))
            grids.append(grid)
            continue
        if node.tag == "form" and not node.id and node.attrs.get("name") and not _in_row(node):
            regions.append({"kind": "form", "name": node.attrs["name"], "visible": dom._visible(node)})  # noqa: SLF001
            continue
        if node.id and node.tag not in ("html", "body", "head", "tl-frame", "tl-shadow"):
            if node.tag == "tr" or _in_row(node):
                continue  # a row or a row's element: there is one per record
            region = {"kind": node.tag if st.IDENT.match(node.tag) else "element", "id": node.id,
                      "visible": dom._visible(node)}  # noqa: SLF001
            classes = [x for x in node.classes if x not in dom.POINTER_CLASSES]
            if classes:
                region["classes"] = classes
            regions.append(region)
    for i, n in repeats.items():
        controls[i]["repeat"] = st.rows_shape(n)
    return {"controls": controls, "regions": regions, "grids": grids}


# --- desktop ------------------------------------------------------------------------------------------

DESKTOP_CONTROLS = {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "TabItem",
                    "SplitButton", "Spinner", "Slider", "Hyperlink", "Document"}  # fmt: skip
DESKTOP_GRIDS = {"Table", "DataGrid", "List", "Tree"}
DESKTOP_NAMED = {"Button", "CheckBox", "RadioButton", "MenuItem", "ComboBox", "Edit", "SplitButton",
                 "TabItem"}  # fmt: skip
DATA_ITEMS = {"DataItem", "ListItem", "TreeItem"}


def _rows(node: dict) -> int:
    if isinstance(node.get("rows"), int):
        return node["rows"]
    return sum(1 for c in node.get("children") or [] if c.get("type") in DATA_ITEMS)


def desktop_state(tree: dict) -> dict:
    controls: list[dict] = []
    regions: list[dict] = []
    grids: list[dict] = []
    withheld = 0

    def walk(n: dict, in_data: bool) -> None:
        nonlocal withheld
        t = n.get("type") or "Custom"
        if t in DATA_ITEMS:
            return  # a record: counted by its grid, never described
        aid = n.get("aid") or None
        if t in DESKTOP_GRIDS:
            grid: dict = {"kind": t, "rows": st.rows_shape(_rows(n))}
            if aid:
                grid["id"] = aid
            heads = [c for c in _descend(n) if c.get("type") in ("HeaderItem", "Header")
                     and c.get("name_is") == "structure"]  # fmt: skip
            if heads:
                grid["columns"] = [c.get("name") or "" for c in heads if c.get("type") == "HeaderItem"] or [
                    c.get("name") or "" for c in heads]
            if "visible" in n:
                grid["visible"] = bool(n["visible"])
            grids.append(grid)
            return
        if t in DESKTOP_CONTROLS and not in_data:
            c: dict = {"kind": t}
            if aid:
                c["id"] = aid
            if t in DESKTOP_NAMED and n.get("name_is") == "structure" and n.get("name"):
                c["label"] = n["name"]
            elif n.get("name") and n.get("name_is") == "data":
                withheld += 1
            c["visible"] = bool(n.get("visible", True))
            c["enabled"] = bool(n.get("enabled", True))
            if n.get("readonly") is not None:
                c["readonly"] = bool(n["readonly"])
            if t in ("CheckBox", "RadioButton") or n.get("toggle") is not None:
                c["checked"] = str(n.get("toggle")).lower() in ("on", "1", "true") or bool(n.get("selected"))
            elif n.get("selected") is not None and t == "TabItem":
                c["selected"] = bool(n["selected"])
            if t == "ComboBox":
                items = [i for i in _descend(n) if i.get("type") == "ListItem"]
                c["options"] = len(items)
                if n.get("items_are") == "code-list":
                    c["option_texts"] = [i.get("name") or "" for i in items]
            if t in ("Edit", "Document", "ComboBox", "Spinner") and not n.get("password"):
                c["value"] = "filled" if str(n.get("value") or "").strip() else "empty"
            controls.append(c)
        elif aid and t in ("Pane", "Group", "Window", "Tab", "ToolBar", "StatusBar", "Text"):
            if t != "Text" or n.get("name_is") in ("structure", "message"):
                regions.append({"kind": t, "id": aid, "visible": bool(n.get("visible", True))})
        for child in n.get("children") or []:
            walk(child, in_data or t in DATA_ITEMS)

    walk(tree, False)
    return {"controls": controls, "regions": regions, "grids": grids, "withheld": withheld}


def _descend(n: dict):
    for c in n.get("children") or []:
        yield c
        yield from _descend(c)


# --- mainframe ------------------------------------------------------------------------------------------


def mainframe_state(screen: dict) -> dict:
    fields = sorted(screen.get("fields") or [], key=lambda f: (f.get("row", 0), f.get("col", 0)))
    controls = []
    for i, f in enumerate(fields):
        if f.get("protected"):
            continue
        c = {"kind": "field", "row": f.get("row"), "col": f.get("col"), "len": f.get("len"),
             "numeric": bool(f.get("numeric")), "hidden": bool(f.get("hidden")),
             "intensified": bool(f.get("intensified")), "enabled": True, "visible": not f.get("hidden"),
             "value": "filled" if f.get("shape") else "empty"}  # fmt: skip
        before = fields[i - 1] if i else None
        if before and before.get("protected") and before.get("row") == f.get("row") and before.get("text"):
            c["label"] = before["text"]
        controls.append(c)
    return {"controls": controls, "regions": [], "grids": []}


# --- the session ---------------------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def state_of(folder: Path, row: dict) -> dict | None:
    """The cleaned state a step's file shows, or None."""
    step = row.get("_step") or {}
    surface = row.get("surface")
    raw = None
    if surface == "web" and step.get("dom"):
        html = _read(folder / str(row.get("recording") or "web") / step["dom"])
        raw = web_state(html) if html is not None else None
    elif surface in ("desktop", "mainframe") and step.get("tree"):
        text = _read(folder / surface / step["tree"])
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            raw = desktop_state(data) if surface == "desktop" else mainframe_state(data)
    if raw is None:
        return None
    raw["events"] = [row["event_id"]] if row.get("event_id") else []
    return st.clean_state(raw)


def collect(folder: Path, rows: list[dict], full: set[str]) -> dict[str, list[dict]]:
    """{application key: [screen]} for the applications in `full`. `rows` are the merged
    timeline rows in order, each with `_step` (the collector's step) and `_label` (its flow
    label)."""
    folder = Path(folder)
    screens: dict[str, dict[str, dict]] = {}
    last: dict[str, tuple[str, str]] = {}  # app -> (screen, state id) last seen
    pending: dict[str, list[dict]] = {}  # app -> labels of the steps since that state
    for row in rows:
        app = row.get("app")
        if app not in full or row.get("_step") is None:
            continue
        label = row.get("_label") or {}
        pending.setdefault(app, []).append(label)
        state = state_of(folder, row)
        if state is None:
            continue
        screen = label.get("screen") or _fallback_screen(row)
        sid = st.state_key(state)
        mine = screens.setdefault(app, {})
        sc = mine.setdefault(screen, {"screen": screen, "surface": row["surface"], "states": {}, "transitions": {}})
        if sid in sc["states"]:
            sc["states"][sid]["events"] = sorted(set(sc["states"][sid]["events"]) | set(state["events"]))
        elif len(sc["states"]) < st.LIMITS["states"]:
            sc["states"][sid] = {"id": sid, **state}
        else:
            pending[app] = []
            continue
        prev = last.get(app)
        if prev is not None and prev != (screen, sid):
            via = _via(pending[app])
            t = {"from_state": prev[1], "to": screen, "to_state": sid, **via}
            src = mine[prev[0]]["transitions"]
            k = tuple(t.get(x) for x in ("from_state", "to", "to_state", "action", "control", "label", "key"))
            if k in src:
                src[k]["count"] += 1
            elif len(src) < st.LIMITS["transitions"]:
                src[k] = dict(t, count=1)
        last[app] = (screen, sid)
        pending[app] = []
    out = {}
    for app, mine in screens.items():
        block = [{"screen": sc["screen"], "surface": sc["surface"], "states": list(sc["states"].values()),
                  "transitions": list(sc["transitions"].values())} for sc in mine.values()]  # fmt: skip
        out[app] = block
    return st.clean_screens(out)


def _via(labels: list[dict]) -> dict:
    """The step that changed the state: the last user action since the previous state, a click
    or key before a submit or page load it caused."""
    chosen = None
    for kind in PREFERRED:
        found = [lb for lb in labels if lb.get("action") == kind]
        if found:
            chosen = found[-1]
            break
    if chosen is None:
        chosen = labels[-1] if labels else {}
    return {k: chosen[k] for k in ("action", "control", "label", "key") if chosen.get(k)}


def _fallback_screen(row: dict) -> str:
    return {"desktop": "(window)", "mainframe": "(screen)"}.get(row.get("surface"), "(page)")
