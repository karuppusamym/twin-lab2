"""Screen structure (recorder contract 1.3): what each screen of an application is made of, never
what it shows.

A recorder that records an application at level `full` sends, beside its flow, one entry per
screen template (the step's `screen`: a web route template, a desktop window's identifier, a
mainframe screen title or layout id). A screen holds the distinct *states* it was seen in (the
search page before and after a search are two states of one screen) and the *transitions* that
led from one state to another. A state holds:

- `controls`, in page order: `kind` (web: the tag, `checkbox`/`radio` for those inputs; desktop:
  the UI Automation ControlType; mainframe: `field`), the identifiers the replay locates and
  compares by (`id`, `name`, `classes`, `type`), a `label`, and the control's state: `visible`,
  `enabled`, `readonly`, `required`, `checked`, `selected`; `value` is only `empty` or `filled`;
  a select or drop-down has `options` (a count) and `option_texts` only when the list is a code
  list (at most 30 entries of at most 40 characters, no digit: the rule the desktop recorder
  already applies to drop-downs); identical controls repeated in the rows of a table are one
  entry with `repeat` (a row-count shape); a mainframe field has `row`, `col`, `len`, `numeric`,
  `hidden` (non-display) and `intensified`;
- `regions`: the other elements with an identifier (containers, messages, pagers; a form by its
  `name` when it has no id) and whether they are shown;
- `grids`: tables, lists and data grids, with their column headers and `rows` as a shape;
- `withheld`: how many identifiers, classes and labels were left out because they could be data;
- `events`: the event ids of the steps after which the screen was in this state.

Every word in it passes the label rules of the flow (twinlab2_recorder.workstation.flow): an
identifier is `ident()` (no `@`, at most 40 characters, letters, `_`, `.`, `-`, and since 1.4 at
most one short digit run, as in `addressLine2`); a
label, column header or code-list entry is `plain()` and at most three words (a column header
four). Row counts are shapes: `0`, `1`, `few` (2 to 9), `many` (10 or more). Never a value, a
row, a record's identifier or a typed character.

`clean_screens` is what the portal keeps of a sender's block (anything else is dropped, and
every word is checked again); `merge` combines two sends of one recording; `select` keeps the
states the owner's reviewed selection of events covers. The recorder builds the block with the
same functions (`state_key`, `clean_screens`), so both sides agree on what a state is.
"""

from __future__ import annotations

import hashlib
import json
import re

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
DIGIT_RUN = re.compile(r"\d+")
IDENT_DIGITS = 2  # the longest digit run an identifier may hold (addressLine2, btn2FA, tab12)
SHAPE = re.compile(r"^[Xx9\W_]+$")
EVENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROWS = ("0", "1", "few", "many")
SURFACES = ("web", "desktop", "mainframe")
VALUES = ("empty", "filled")
LIMITS = {"screens": 500, "states": 20, "controls": 400, "regions": 200, "grids": 50,
          "columns": 40, "options": 30, "events": 2000, "transitions": 100, "classes": 12,
          "texts": 200}  # fmt: skip
# 1.4: a screen's own texts (`states[].texts`): a 3270 screen's protected fields (title, labels,
# message line, key line, and other protected text as a shape only) and a desktop message box's
# message. TEXT_SHOWN kinds keep their text; `text` is anything that may be data: a shape.
TEXT_KINDS = ("title", "label", "message", "keys", "text")
TEXT_SHOWN = ("title", "label", "message", "keys")
TEXT_MAX = 160
KEY_NAME = re.compile(r"\b(?:PF|PA|F)\d{1,2}\b")
BOOLS = ("visible", "enabled", "readonly", "required", "checked", "selected", "numeric", "hidden",
         "intensified")  # fmt: skip
INTS = ("options", "row", "col", "len")
CODE_LIST_MAX, CODE_ITEM_MAX = 30, 40


def plain(text: object) -> bool:
    """The flow's rule for a label: at most 40 characters, no digit, no `@`, not a shape the
    recorder wrote, not empty."""
    s = str(text or "").strip()
    if not s or len(s) > 40 or "@" in s or any(c.isdigit() for c in s) or re.search(r"[\x00-\x1f]", s):
        return False
    return not SHAPE.match(s)


KEY = re.compile(r"^(?:Enter|Tab|Esc|Escape|Delete|Backspace|Clear|PA[1-3]|P?F(?:[1-9]|1[0-9]|2[0-4]))$")


def key_name(text: object) -> bool:
    """A key's own name (`Enter`, `F3`, a 3270 AID: `PF3`, `PA1`, `Clear`): no data, though it
    holds a digit, so a transition keeps it as its `key` (and as a 3270 key step's `label`)."""
    return isinstance(text, str) and bool(KEY.match(text))


def ident(text: object) -> bool:
    """An identifier a control or screen may be named by: at most 40 characters of letters,
    digits, `_`, `.` and `-`, no `@`, not a shape the recorder wrote. Since contract 1.4 it may
    hold digits the way a developer names things: it starts with a letter, holds at least three
    letters and at most one run of at most two digits, and that run does not follow `-`, `_` or
    `.` (`addressLine2`, `button1`, `btn2FA`, `txtLine2` are identifiers; `12345`, `cust10442`,
    `a1b2c3`, `mat-input-0`, `row_12` and `INV-1001` are not: an all-digit id, a long or
    repeated digit run and a counter after a separator are records or generated ids)."""
    s = str(text or "").strip()
    if not s or len(s) > 40 or "@" in s or SHAPE.match(s) or not IDENT.match(s):
        return False
    runs = DIGIT_RUN.findall(s)
    if not runs:
        return True
    if len(runs) > 1 or len(runs[0]) > IDENT_DIGITS or not s[0].isalpha():
        return False
    at = s.index(runs[0])
    if s[at - 1] in "-_.":
        return False
    return sum(c.isalpha() for c in s) >= 3


def words(text: object, most: int = 3) -> str | None:
    """`text` as a label (whitespace collapsed) when it is `plain` and at most `most` words."""
    s = re.sub(r"\s+", " ", str(text or "")).strip().rstrip(":").strip()
    return s if plain(s) and len(s.split()) <= most else None


def rows_shape(count: int) -> str:
    return "0" if count <= 0 else "1" if count == 1 else "few" if count < 10 else "many"


def code_list(texts: list[str]) -> list[str] | None:
    """Option texts kept as a code list: at most 30, each at most 40 characters, no digit
    (blank entries, a placeholder option, are kept as blank); else None (a count only)."""
    if len(texts) > CODE_LIST_MAX:
        return None
    out = []
    for t in texts:
        s = re.sub(r"\s+", " ", str(t or "")).strip()
        if s and not (plain(s) and len(s) <= CODE_ITEM_MAX and len(s.split()) <= 4):
            return None
        out.append(s)
    return out


# --- cleaning (what the portal keeps; the recorder runs it too) ----------------------------------


def _text(value, most: int = 160) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= most and not re.search(r"[\x00-\x1f]", value):
        return value
    return None


def region_ref(text: object) -> bool:
    """`in` (contract 1.6): the region a control, region or grid is inside, by the region's
    identifier or, for a region named only by its caption, by that label (words)."""
    return ident(text) or (isinstance(text, str) and words(text, 4) == text)


def clean_control(c) -> tuple[dict | None, int]:
    """(the control as the contract allows it, how many of its words were dropped)."""
    if not isinstance(c, dict):
        return None, 0
    kind = c.get("kind")
    if not (isinstance(kind, str) and IDENT.match(kind) and len(kind) <= 24):
        return None, 1
    out, dropped = {"kind": kind}, 0
    for k in ("tag", "type", "id", "name"):
        v = c.get(k)
        if v is None:
            continue
        if ident(v):
            out[k] = v
        else:
            dropped += 1
    classes = c.get("classes")
    if isinstance(classes, list):
        kept = [x for x in classes if ident(x)][: LIMITS["classes"]]
        dropped += len(classes) - len(kept)
        if kept:
            out["classes"] = kept
    if region_ref(c.get("in")):
        out["in"] = c["in"]
    elif c.get("in") is not None:
        dropped += 1
    if c.get("label") is not None:
        label = words(c["label"])
        if label:
            out["label"] = label
        else:
            dropped += 1
    for k in BOOLS:
        if isinstance(c.get(k), bool):
            out[k] = c[k]
    for k in INTS:
        if isinstance(c.get(k), int) and not isinstance(c.get(k), bool) and 0 <= c[k] <= 100000:
            out[k] = c[k]
    if c.get("value") in VALUES:
        out["value"] = c["value"]
    if c.get("repeat") in ROWS:
        out["repeat"] = c["repeat"]
    if isinstance(c.get("option_texts"), list):
        texts = code_list(c["option_texts"])
        if texts is not None:
            out["option_texts"] = texts
            out["options"] = len(texts)
        else:
            dropped += 1
    return out, dropped


def clean_state(s) -> dict | None:
    if not isinstance(s, dict):
        return None
    withheld = s.get("withheld") if isinstance(s.get("withheld"), int) and s["withheld"] >= 0 else 0
    controls = []
    for c in (s.get("controls") or [])[: LIMITS["controls"]]:
        item, dropped = clean_control(c)
        withheld += dropped
        if item is not None:
            controls.append(item)
    regions = []
    for r in (s.get("regions") or [])[: LIMITS["regions"]]:
        if not isinstance(r, dict) or not (isinstance(r.get("kind"), str) and IDENT.match(r["kind"])):
            withheld += 1
            continue
        # 1.6: a region named only by its caption ("Scientific functions") carries it as `label`
        labelled = (r.get("id") is None and r.get("name") is None and isinstance(r.get("label"), str)
                    and words(r["label"], 4) == r["label"])
        if not (ident(r.get("id")) or (r.get("id") is None and ident(r.get("name"))) or labelled):
            withheld += 1
            continue
        item = {"kind": r["kind"][:24], **({"id": r["id"]} if r.get("id") is not None else
                                          {"name": r["name"]} if r.get("name") is not None else
                                          {"label": r["label"]})}
        classes = [x for x in (r.get("classes") or []) if ident(x)][: LIMITS["classes"]] if isinstance(r.get("classes"), list) else []
        if classes:
            item["classes"] = classes
        if isinstance(r.get("visible"), bool):
            item["visible"] = r["visible"]
        if region_ref(r.get("in")):  # 1.6: the region it is in
            item["in"] = r["in"]
        regions.append(item)
    grids = []
    for g in (s.get("grids") or [])[: LIMITS["grids"]]:
        if not isinstance(g, dict) or g.get("rows") not in ROWS or not (isinstance(g.get("kind"), str) and IDENT.match(g["kind"])):
            continue
        item = {"kind": g["kind"][:24], "rows": g["rows"]}
        if g.get("id") is not None:
            if ident(g["id"]):
                item["id"] = g["id"]
            else:
                withheld += 1
        columns = []
        for col in (g.get("columns") or [])[: LIMITS["columns"]] if isinstance(g.get("columns"), list) else []:
            label = words(col, 4) if col else None
            if col and not label:
                withheld += 1
            columns.append(label or "")
        if any(columns):
            item["columns"] = columns
        if isinstance(g.get("visible"), bool):
            item["visible"] = g["visible"]
        if region_ref(g.get("in")):
            item["in"] = g["in"]
        grids.append(item)
    texts = []
    for t in (s.get("texts") or [])[: LIMITS["texts"]] if isinstance(s.get("texts"), list) else []:
        item = clean_text(t)
        if item is None:
            withheld += 1
        else:
            texts.append(item)
    events = sorted({e for e in (s.get("events") or []) if isinstance(e, str) and EVENT_ID.match(e)})[: LIMITS["events"]]
    out = {"controls": controls, "regions": regions, "grids": grids, "withheld": withheld, "events": events}
    if texts:  # 1.4; a state without texts keeps the id it had in 1.3
        out["texts"] = texts
    return out


def masked_text(text: str, kind: str) -> str:
    """A shown text with every digit as the shape digit `9`, except a key's own name on a key
    line (`PF3=EXIT`): the recorder masks digit tokens already, and this holds on any sender."""
    keep = {m.span(): m.group() for m in KEY_NAME.finditer(text)} if kind == "keys" else {}
    out, i = [], 0
    for (a, b), name in sorted(keep.items()):
        out.append(re.sub(r"\d", "9", text[i:a]))
        out.append(name)
        i = b
    out.append(re.sub(r"\d", "9", text[i:]))
    return "".join(out)


def clean_text(t) -> dict | None:
    """One of a screen's own texts (1.4): `kind` (title, label, message, keys: the text is kept;
    text: only its `shape`), and for 3270 `row`, `col`, `len`, `intensified`, `hidden`. None when
    it is outside the rules (an `@`, a control character, too long, a shape that is not one)."""
    if not isinstance(t, dict) or t.get("kind") not in TEXT_KINDS:
        return None
    out: dict = {"kind": t["kind"]}
    for k in ("row", "col", "len"):
        if isinstance(t.get(k), int) and not isinstance(t.get(k), bool) and 0 <= t[k] <= 100000:
            out[k] = t[k]
    for k in ("intensified", "hidden"):
        if isinstance(t.get(k), bool):
            out[k] = t[k]
    if t["kind"] in TEXT_SHOWN:
        text = re.sub(r"\s+", " ", str(t.get("text") or "")).strip()
        if not text or len(text) > TEXT_MAX or "@" in text or re.search(r"[\x00-\x1f]", text):
            return None
        out["text"] = masked_text(text, t["kind"])
    elif t.get("shape") is not None:
        shape = str(t["shape"])
        if not shape or len(shape) > TEXT_MAX or not SHAPE.match(shape):
            return None
        out["shape"] = shape
    return out


def state_key(state: dict) -> str:
    """A state's id: a hash of everything but its events and id (two observations with the same
    structure are one state, in any send)."""
    body = {k: v for k, v in state.items() if k not in ("events", "id")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


STATE_ID = re.compile(r"^[0-9a-f]{16}$")


def clean_transition(t) -> dict | None:
    """`from_state` and `to_state` are state ids; `to` the screen the step led to."""
    if not isinstance(t, dict) or not _text(t.get("to")):
        return None
    out = {"to": t["to"]}
    for k in ("from_state", "to_state"):
        if isinstance(t.get(k), str) and STATE_ID.match(t[k]):
            out[k] = t[k]
    if "from_state" not in out or "to_state" not in out:
        return None
    action = t.get("action")
    if isinstance(action, str) and IDENT.match(action) and len(action) <= 24:
        out["action"] = action
    for k in ("control", "label", "key"):
        v = t.get(k)
        if v is not None and (plain(v) and len(str(v).split()) <= 3 or k != "control" and key_name(v)):
            out[k] = v
    out["count"] = t["count"] if isinstance(t.get("count"), int) and not isinstance(t["count"], bool) and t["count"] > 0 else 1
    return out


def clean_screen(sc) -> dict | None:
    if not isinstance(sc, dict) or not _text(sc.get("screen")) or sc.get("surface") not in SURFACES:
        return None
    states: dict[str, dict] = {}
    sent_ids = {}
    for s in (sc.get("states") or [])[: LIMITS["states"]]:
        st = clean_state(s)
        if st is None:
            continue
        k = state_key(st)
        if isinstance(s.get("id"), str):
            sent_ids[s["id"]] = k
        if k in states:  # equal once cleaned: one state
            states[k]["events"] = sorted(set(states[k]["events"]) | set(st["events"]))
            continue
        states[k] = {"id": k, **st}
    if not states:
        return None
    transitions = []
    for t in (sc.get("transitions") or [])[: LIMITS["transitions"]]:
        item = clean_transition(t)
        if item is None:
            continue
        # a state id the sender computed before cleaning names the state it became
        item["from_state"] = sent_ids.get(item["from_state"], item["from_state"])
        if item["from_state"] in states:
            transitions.append(item)
    return {"screen": sc["screen"], "surface": sc["surface"], "states": list(states.values()),
            "transitions": transitions}


def _fix_targets(screens: list[dict], renamed: dict[str, str]) -> list[dict]:
    if renamed:
        for sc in screens:
            for t in sc["transitions"]:
                t["to_state"] = renamed.get(t["to_state"], t["to_state"])
    return screens


def clean_screens(raw, keys=None, normalise=lambda k: k) -> dict:
    """{application key: [screen]} as the contract allows it; applications not in `keys` (when
    given) and entries outside the contract are dropped."""
    out: dict[str, list[dict]] = {}
    if not isinstance(raw, dict):
        return out
    for key, screens in raw.items():
        nkey = normalise(key)
        if nkey is None or (keys is not None and nkey not in keys) or not isinstance(screens, list):
            continue
        renamed = {}
        clean = []
        for sc in screens[: LIMITS["screens"]]:
            item = clean_screen(sc)
            if item is None:
                continue
            for s in sc.get("states") or []:
                if isinstance(s, dict) and isinstance(s.get("id"), str):
                    st = clean_state(s)
                    if st is not None and s["id"] != state_key(st):
                        renamed[s["id"]] = state_key(st)
            clean.append(item)
        clean = _fix_targets(clean, renamed)
        if clean:
            out[nkey] = merge(out.get(nkey, []), clean)
    return out


# --- combining and selecting ----------------------------------------------------------------------


def _merge_screen(a: dict, b: dict) -> dict:
    states = {s["id"]: dict(s) for s in a["states"]}
    for s in b["states"]:
        if s["id"] in states:
            states[s["id"]]["events"] = sorted(set(states[s["id"]]["events"]) | set(s["events"]))
        elif len(states) < LIMITS["states"]:
            states[s["id"]] = dict(s)
    trans = {_tkey(t): dict(t) for t in a["transitions"]}
    for t in b["transitions"]:
        if t["from_state"] not in states:
            continue
        k = _tkey(t)
        if k in trans:
            trans[k]["count"] = max(trans[k]["count"], t["count"])
        else:
            trans[k] = dict(t)
    return dict(a, states=list(states.values()), transitions=list(trans.values())[: LIMITS["transitions"]])


def _tkey(t: dict) -> tuple:
    return tuple(t.get(k) for k in ("from_state", "to", "to_state", "action", "control", "label", "key"))


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Screens of two sends (or two identities resolved to one application): one entry per
    screen, states by id, events joined, the earlier order kept."""
    out = {sc["screen"]: sc for sc in old}
    order = [sc["screen"] for sc in old]
    for sc in new:
        if sc["screen"] in out:
            out[sc["screen"]] = _merge_screen(out[sc["screen"]], sc)
        else:
            out[sc["screen"]] = sc
            order.append(sc["screen"])
    return [out[s] for s in order]


def select(screens: list[dict], events: set[str]) -> list[dict]:
    """The screens and states the selected events show, each state with the selected event ids
    only; the transitions between kept states."""
    kept = []
    for sc in screens:
        states = [dict(st, events=[e for e in st["events"] if e in events]) for st in sc["states"]]
        states = [st for st in states if st["events"]]
        if states:
            kept.append((sc, states))
    ids = {st["id"] for _, states in kept for st in states}
    return [{"screen": sc["screen"], "surface": sc["surface"], "states": states,
             "transitions": [dict(t) for t in sc["transitions"]
                             if t["from_state"] in ids and t["to_state"] in ids]}
            for sc, states in kept]  # fmt: skip


def counts(screens: list[dict]) -> dict:
    """How much a block holds, for pages and reports."""
    return {"screens": len(screens), "states": sum(len(sc["states"]) for sc in screens),
            "controls": sum(len(st["controls"]) for sc in screens for st in sc["states"]),
            "transitions": sum(len(sc["transitions"]) for sc in screens)}  # fmt: skip
