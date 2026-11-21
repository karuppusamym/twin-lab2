# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""What of a UI Automation tree is structure (kept) and what is data (only its shape is kept).

The rule is conservative, the same as the web recorder's: anything that may hold a customer's
data is reduced to its shape (letters X/x, digits 9, other characters kept) before the tree is
written anywhere. The raw tree exists only in memory, inside the recorder.

Name, by ControlType:

- **structure** (kept): Button, CheckBox, RadioButton, MenuItem, Menu, MenuBar, TabItem, Tab,
  HeaderItem and Header (column headers), Group (a group box caption), ToolBar, TitleBar,
  ScrollBar, Thumb, SplitButton. Within kept text, tokens with three or more digits are still
  masked (`Order 10023` -> `Order 99999`), as the web path does for identifiers.
- **label** (kept when it is a label): Text. A Text control is a label when its AutomationId
  says so (`lbl...`, `label...`), its Name ends with `:`, or an input control is named after it
  (UI Automation names an Edit after the label next to it). Any other Text is data.
- **named after a label** (kept only then): Edit, Document, ComboBox, Spinner, Slider, List,
  Tree, Table, DataGrid, ProgressBar, Calendar. Their Name is kept only when it equals a label's
  text or their own AutomationId; otherwise it may be their content and is masked.
- **data** (masked): DataItem, ListItem, TreeItem, Hyperlink, Custom, Image, and every control
  inside a data container (a row, a list, a tree, a status bar), whatever its type. Exception:
  the items of a drop-down list (a ComboBox without an edit field) are kept as a code list when
  there are at most 30, each at most 40 characters, with no digit; otherwise they are masked too.
  (Risk accepted: a short drop-down of names without digits would be kept; an editable combo box,
  the usual picker for people or accounts, never is.)
- **message** (kept): the text of a message box (`#32770`), the application's own words.

Value (ValuePattern) is always data: only its shape is kept, and for a password field not even
that. Window titles are structure with data-like parts masked: every token holding a digit,
everything after a `:`, and any word the recording has already seen as data.
"""

from __future__ import annotations

import re

from twinlab2_contract.evidence import sanitize

STRUCTURE = {"Button", "CheckBox", "RadioButton", "MenuItem", "Menu", "MenuBar", "TabItem", "Tab",
             "HeaderItem", "Header", "Group", "ToolBar", "TitleBar", "ScrollBar", "Thumb",
             "SplitButton"}  # fmt: skip
LABELLED = {"Edit", "Document", "ComboBox", "Spinner", "Slider", "List", "Tree", "Table",
            "DataGrid", "ProgressBar", "Calendar"}  # fmt: skip
# Inside these, every Name and Value is a record's data.
DATA_CONTAINERS = {"DataItem", "ListItem", "TreeItem", "List", "Tree", "StatusBar"}
DATA = {"DataItem", "ListItem", "TreeItem", "Hyperlink", "Custom", "Image"}
VALUE_DATA = {
    "Edit",
    "Document",
    "ComboBox",
    "DataItem",
    "ListItem",
    "TreeItem",
    "Custom",
    "Spinner",
}
CODE_LIST_MAX, CODE_ITEM_MAX = 30, 40
LABEL_ID = re.compile(r"^(lbl|label|lab|caption|static)", re.I)
DIGIT_TOKEN = re.compile(r"[\w./-]*\d[\w./-]*")
WORD = re.compile(r"[^\W\d_]{3,}", re.U)


def shape(text: str | None, limit: int = 64) -> str | None:
    """A value's shape (`C-1001` -> `X-9999`), capped; None for no value."""
    if text is None:
        return None
    text = str(text)
    out = sanitize.mask(text[:limit])
    return out + "…" if len(text) > limit else out


def words(text: str | None) -> set[str]:
    """The words of a data value, lower-case, for masking them wherever they show again."""
    return {w.lower() for w in WORD.findall(str(text or ""))}


def _mask_known(text: str, known: set[str], structure: set[str] = frozenset()) -> str:
    if not known:
        return text

    def one(m: re.Match) -> str:
        w = m.group().lower()
        return sanitize.mask(m.group()) if w in known and w not in structure else m.group()

    return WORD.sub(one, text)


def keep_text(text: str, known: set[str] = frozenset(), structure: set[str] = frozenset()) -> str:
    """Structural text: kept, except identifier-like tokens and words seen as data (unless the
    application also shows the word as structure, on a button or a label)."""
    return _mask_known(sanitize.mask_identifiers(text), known, structure)


def code_list(names: list[str], known: set[str] = frozenset()) -> bool:
    """Whether a combo box's item names may be kept as a code list (see the module docstring)."""
    names = [(n or "").strip() for n in names]
    # `known` is not consulted: a code value (Open, Resolved) also shows in the grid's cells.
    return 0 < len(names) <= CODE_LIST_MAX and all(
        0 < len(n) <= CODE_ITEM_MAX and not re.search(r"\d", n) for n in names
    )


def mask_title(title: str | None, known: set[str] = frozenset(),
               structure: set[str] = frozenset()) -> str | None:  # fmt: skip
    """A window title with its data-like parts masked: `Case C-1001` -> `Case X-9999`,
    `Customer: Ada Example - CaseDesk` -> `Customer: Xxx Xxxxxxx - CaseDesk`."""
    if title is None:
        return None
    parts = re.split(r"(\s+[-|–—]\s+)", title)
    out = []
    for part in parts:
        if re.fullmatch(r"\s+[-|–—]\s+", part):
            out.append(part)
            continue
        head, colon, tail = part.partition(":")
        head = DIGIT_TOKEN.sub(lambda m: sanitize.mask(m.group()), head)
        out.append(
            _mask_known(head, known, structure) + (colon + sanitize.mask(tail) if colon else "")
        )
    return "".join(out)


def is_label(node: dict) -> bool:
    name = (node.get("name") or "").strip()
    return (
        node.get("type") == "Text"
        and bool(name)
        and (bool(LABEL_ID.match(node.get("aid") or "")) or name.endswith(":"))
    )


def _labels(raw: dict) -> set[str]:
    """Text names that label something: label-like Text, or the Name of an input control that
    equals some Text in the same window (UI Automation names inputs after their label)."""
    texts, inputs, out = set(), set(), set()

    def walk(n: dict, data: bool) -> None:
        t = n.get("type")
        name = (n.get("name") or "").strip()
        if t == "Text" and name and not data:
            texts.add(name)
            if is_label(n):
                out.add(name)
        if t in LABELLED and name:
            inputs.add(name)
        inside = data or t in DATA_CONTAINERS
        for c in n.get("children") or []:
            walk(c, inside)

    walk(raw, False)
    return out | (texts & inputs)


def _code_list(items: list[dict], known: set[str]) -> bool:
    return code_list([i.get("name") or "" for i in items], known)


def _structure_words(raw: dict, labels: set[str]) -> set[str]:
    out: set[str] = set()

    def walk(n: dict, data: bool) -> None:
        t, name = n.get("type"), (n.get("name") or "").strip()
        if name and not data and (t in STRUCTURE or name in labels):
            out.update(words(name))
        for c in n.get("children") or []:
            walk(c, data or t in DATA_CONTAINERS)

    walk(raw, False)
    return out


def _plain(text: str | None) -> str:
    """A caption without its access-key marker (`_Search`, `&Search` -> `search`)."""
    return re.sub(r"[_&]", "", str(text or "")).strip().lower()


def _is_header(n: dict) -> bool:
    kids = n.get("children") or []
    return n.get("type") in ("Header", "HeaderItem") or any(
        k.get("type") in ("Header", "HeaderItem") for k in kids
    )


def clean(raw: dict, known: set[str] = frozenset(), structure: set[str] | None = None) -> dict:
    """The value-free copy of a raw window tree (see the module docstring). `known` holds words
    already seen as data in this recording (values, and names of rows and items); data met here
    is added to it. `structure` collects the words the application shows as structure (buttons,
    labels), which are never masked as data. A message box (`#32770`) keeps its text: it is the
    application's own message, masked like other structural text."""
    labels = _labels(raw)
    seen: set[str] = set()
    struct = _structure_words(raw, labels)
    if structure is not None:
        structure |= struct
        struct = structure
    message_box = (raw.get("cls") or "") == "#32770" or bool(raw.get("message_box"))

    def walk(n: dict, data: bool, combo: bool, caption: str | None = None) -> dict:
        t = n.get("type") or "Custom"
        name = n.get("name")
        aid = n.get("aid") or ""
        out: dict = {"type": t}
        if aid:
            out["aid"] = sanitize.mask_identifiers(aid) if data else aid
        keep = False
        if name:
            if combo and t == "ListItem":
                keep = True  # decided by the combo box, below
            elif data or t in DATA:
                keep = False
            elif t in ("Window", "Pane"):
                out["name"] = mask_title(name, known, struct)
                out["name_is"] = "title"
                name = None
            elif t in STRUCTURE:
                keep = True
            elif t == "Text":
                # A caption drawn inside a kept control (WPF: the TextBlock of a Button, a
                # column header or a Label, `_` marking the access key) is that control's text.
                keep = (name.strip() in labels or message_box
                        or (caption is not None and _plain(name) == _plain(caption)))  # fmt: skip
            elif t in LABELLED:
                keep = name.strip() in labels or name == aid
            if name is not None:
                if keep:
                    out["name"] = keep_text(name, known, struct)
                    out["name_is"] = "message" if message_box and t == "Text" else "structure"
                else:
                    if t in DATA and t != "Custom":
                        seen.update(words(name))
                    out["name"] = shape(name)
                    out["name_is"] = "data"
        for key in ("cls", "fw", "access_key", "accel", "role", "message_box"):
            if n.get(key):
                out[key] = n[key]
        if n.get("help"):
            out["help"] = keep_text(str(n["help"])[:120], known, struct)
        for key in ("enabled", "visible", "focusable", "readonly", "selected", "focused"):
            if key in n and n[key] is not None:
                out[key] = bool(n[key])
        # How it looks and what it accepts (twinlab2_recorder.desktop.winlook): fonts, frame, limits.
        for key in ("toggle", "expand", "rect", "patterns", "rows", "columns", "win32", "frame",
                    "text_attrs"):  # fmt: skip
            if n.get(key) is not None:
                out[key] = n[key]
        if n.get("password"):
            out["password"] = True
        elif n.get("value") is not None:
            raw_value = str(n["value"])
            if data or t in VALUE_DATA:  # a title bar's value is the title, not data
                seen.update(words(raw_value))
            out["value"] = shape(raw_value)
            if data or t in ("DataItem", "Edit", "Custom", "Text"):
                out["kind"] = sanitize.cell_shape(raw_value) if raw_value.strip() else None
        children = n.get("children") or []
        inside = data or t in DATA_CONTAINERS
        if t == "ComboBox":
            items = [i for c in children for i in ([c] if c.get("type") == "ListItem" else
                                                   c.get("children") or [])
                     if i.get("type") == "ListItem"]  # fmt: skip
            editable = any(c.get("type") == "Edit" for c in children)
            kept = _code_list(items, known) and not editable
            out["items_are"] = "code-list" if kept else "data"
            kids = []
            for c in children:
                # The drop-down button ("Open"/"Close" in WinForms) is the toolkit's, not data.
                button = c.get("type") == "Button" and not c.get("children")
                kids.append(walk(c, False, False) if button else walk(c, not kept, kept))
            out["children"] = kids
            return out
        kept_caption = n.get("name") if out.get("name_is") in ("structure", "message") else None
        if children and t in ("Table", "DataGrid") and not data:
            # A grid's cells are data; its column headers (and a header row) are structure.
            out["children"] = [walk(c, not _is_header(c), False) for c in children]
        elif children:
            out["children"] = [walk(c, inside and not combo, combo, kept_caption)
                               for c in children]  # fmt: skip
        if n.get("truncated"):
            out["truncated"] = n["truncated"]
        return out

    result = walk(raw, False, False)
    if isinstance(known, set):
        known |= seen - struct
    return result
