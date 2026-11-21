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
import re

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
# Containers named by an AutomationId: a region, and the `in` of what is inside it. Custom holds
# a modern app's navigation view (Calculator's NavView), MenuBar and TitleBar a window's own.
DESKTOP_REGIONS = {"Pane", "Group", "Window", "Tab", "ToolBar", "StatusBar", "Text", "Custom", "MenuBar",
                   "TitleBar"}
DESKTOP_NAMED = {"Button", "CheckBox", "RadioButton", "MenuItem", "ComboBox", "Edit", "SplitButton",
                 "TabItem"}  # fmt: skip
DATA_ITEMS = {"DataItem", "ListItem", "TreeItem"}
NAMED_REGIONS = {"Group", "Pane", "Custom"}  # containers a label alone can name (contract 1.6)
# An item outside any grid or list with an identifier that is a plain word (Windows Calculator's
# navigation: `Standard`, `Scientific` in a Group) is a place of the application, not a record.
NAV_ID = re.compile(r"[A-Za-z][A-Za-z_]{1,39}")


def _rows(node: dict) -> int:
    if isinstance(node.get("rows"), int):
        return node["rows"]
    return sum(1 for c in node.get("children") or [] if c.get("type") in DATA_ITEMS)


def desktop_state(tree: dict, combos: dict | None = None) -> dict:
    """`combos`: {combo box automation id: {"items": [...]} or {"item_count": n}}, what the
    recording's expand steps saw in each drop-down's list (the tree is read while it is closed:
    its items are not in it)."""
    controls: list[dict] = []
    regions: list[dict] = []
    grids: list[dict] = []
    texts: list[dict] = []
    withheld = 0

    def walk(n: dict, in_data: bool, parent: str | None = None, region: str | None = None) -> None:
        nonlocal withheld
        t = n.get("type") or "Custom"
        where = {"in": region} if region else {}
        if t in DATA_ITEMS:
            if (t == "ListItem" and parent not in DESKTOP_GRIDS and not in_data
                    and NAV_ID.fullmatch(n.get("aid") or "")):  # fmt: skip
                item: dict = {"kind": t, "id": n["aid"], "visible": bool(n.get("visible", True)),
                              "enabled": bool(n.get("enabled", True)), **where}  # fmt: skip
                if n.get("name_is") == "structure" and n.get("name"):
                    item["label"] = n["name"]
                if n.get("selected") is not None:
                    item["selected"] = bool(n["selected"])
                controls.append(item)
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
            grid.update(where)
            grids.append(grid)
            return
        if t in DESKTOP_CONTROLS and not in_data:
            c: dict = {"kind": t, **where}
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
                seen = (combos or {}).get(aid) if aid else None
                if not items and seen:  # the list as its expand step saw it
                    if seen.get("items"):
                        c["options"] = len(seen["items"])
                        c["option_texts"] = list(seen["items"])
                    elif isinstance(seen.get("item_count"), int):
                        c["options"] = seen["item_count"]
            if t in ("Edit", "Document", "ComboBox", "Spinner") and not n.get("password"):
                c["value"] = "filled" if str(n.get("value") or "").strip() else "empty"
            controls.append(c)
            if t == "TabItem" and aid:
                region = aid  # a tab holds its page: what is on it is in the tab (1.6 `in`)
        elif aid and t in DESKTOP_REGIONS:
            if t != "Text" or n.get("name_is") in ("structure", "message"):
                regions.append({"kind": t, "id": aid, "visible": bool(n.get("visible", True)), **where})
                if t != "Text" and n is not tree:  # the window itself is the screen
                    region = aid  # what it holds is in it (contract 1.6 `in`)
        elif (t in NAMED_REGIONS and n is not tree and n.get("name_is") == "structure"
              and st.words(n.get("name"), 4) == n.get("name")):  # fmt: skip
            # a container named only by its caption ("Scientific functions"): a region by label
            regions.append({"kind": t, "label": n["name"], "visible": bool(n.get("visible", True)), **where})
            region = n["name"]
        if t == "Text" and n.get("name_is") == "message" and n.get("name") and not in_data:
            # A message box's (#32770) message is the application's own text (privacy.clean
            # keeps it, digit tokens masked); its Win32 id (65535) is no identifier.
            texts.append({"kind": "message", "text": n["name"]})
        for child in n.get("children") or []:
            walk(child, in_data or t in DATA_ITEMS, t, region)

    walk(tree, False)
    out = {"controls": controls, "regions": regions, "grids": grids, "withheld": withheld}
    if texts:
        out["texts"] = texts
    return out


def combo_lists(folder: Path, rows: list[dict]) -> dict[tuple, dict]:
    """{(application, combo box automation id): what its expand or collapse step saw in the
    list}: `items` (a code list, kept by the desktop recorder's rule) or `item_count`."""
    out: dict[tuple, dict] = {}
    for row in rows:
        step = row.get("_step") or {}
        if row.get("surface") != "desktop" or step.get("action") not in ("expand", "collapse"):
            continue
        sel = str((step.get("target") or {}).get("selector") or step.get("selector") or "")
        m = re.search(r"AutomationId=([^;]+)", sel)
        if not m or "ComboBox" not in sel:
            continue
        key = (row.get("app"), m.group(1))
        if isinstance(step.get("items"), list) and step["items"]:
            out[key] = {"items": [str(i) for i in step["items"]]}
        elif isinstance(step.get("item_count"), int) and key not in out:
            out[key] = {"item_count": step["item_count"]}
    return out


def _descend(n: dict):
    for c in n.get("children") or []:
        yield c
        yield from _descend(c)


# --- mainframe ------------------------------------------------------------------------------------------


KEY_LINE = re.compile(r"\b(?:PF|PA|F)\d{1,2}\s*=|\bENTER\s*=", re.I)
LABEL_END = re.compile(r"(?::|=|\.\s*\.|\.{2,}|>)\s*$")


def mainframe_texts(screen: dict) -> list[dict]:
    """A 3270 screen's protected fields (contract 1.4 `texts`), with row, column, length and
    attributes: the title (the protected text of the first row that has any, as the recorder
    names the screen), labels (the protected text right before an input field on its row, or
    one that ends as a label does: `NAME:`, `NAME . . .`), the key line (`PF3=EXIT`) and the
    message line (protected text on the last three rows) keep their text, as the 3270 proxy
    cleaned it (digit tokens and typed values as shapes). Any other protected text may be data
    (an account holder's name shown in an output field): its shape only."""
    from twinlab2_recorder.workstation.tn3270 import shape

    rows = (screen.get("screen") or {}).get("rows") or 24
    fields = sorted(screen.get("fields") or [], key=lambda f: (f.get("row", 0), f.get("col", 0)))
    shown = [f for f in fields if f.get("protected") and (str(f.get("text") or "").strip() or f.get("hidden"))]
    title_row = next((f.get("row") for f in shown if not f.get("hidden") and str(f.get("text") or "").strip()), None)
    out = []
    for i, f in enumerate(fields):
        if f not in shown:
            continue
        text = " ".join(str(f.get("text") or "").split())
        item: dict = {"row": f.get("row"), "col": f.get("col"), "len": f.get("len")}
        if f.get("intensified"):
            item["intensified"] = True
        if f.get("hidden"):
            item.update(kind="text", hidden=True)
            out.append(item)
            continue
        nxt = fields[i + 1] if i + 1 < len(fields) else None
        before_input = nxt is not None and not nxt.get("protected") and nxt.get("row") == f.get("row")
        if f.get("row") == title_row:
            kind = "title"
        elif KEY_LINE.search(text):
            kind = "keys"
        elif before_input or LABEL_END.search(text):
            kind = "label"
        elif isinstance(f.get("row"), int) and f["row"] > rows - 3:
            kind = "message"
        else:
            kind = "text"
        if kind == "text":
            item.update(kind="text", shape=shape(text))
        else:
            item.update(kind=kind, text=text)
        out.append(item)
    return out


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
    return {"controls": controls, "regions": [], "grids": [], "texts": mainframe_texts(screen)}


# --- the session ---------------------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def state_of(folder: Path, row: dict, combos: dict[tuple, dict] | None = None) -> dict | None:
    """The cleaned state a step's file shows, or None. `combos`: combo_lists()."""
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
            if surface == "desktop":
                mine = {aid: v for (app, aid), v in (combos or {}).items() if app == row.get("app")}
                raw = desktop_state(data, mine)
            else:
                raw = mainframe_state(data)
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
    combos = combo_lists(folder, [r for r in rows if r.get("app") in full])
    for row in rows:
        app = row.get("app")
        if app not in full or row.get("_step") is None:
            continue
        label = row.get("_label") or {}
        pending.setdefault(app, []).append(label)
        state = state_of(folder, row, combos)
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
        via = _via(pending[app])
        # A 3270 key that leaves the screen as it was (Clear redrawing it, PF8 at the last page,
        # a key the host ignores) is how the host answers that key (gap 20): kept as a transition.
        same_key = row["surface"] == "mainframe" and via.get("action") == "aid" and bool(via.get("key"))
        if prev is not None and (prev != (screen, sid) or same_key):
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
