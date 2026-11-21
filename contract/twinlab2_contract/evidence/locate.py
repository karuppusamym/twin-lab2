# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Selectors that cross frames: `iframe#content >>> button#save`, and resolving a recorded step's
target from its ranked locator candidates.

CSS cannot go into a frame, so Twin Lab writes a frame boundary as ` >>> `: every part before
the last names a frame, the last names the element. The recorder writes selectors this way for
actions inside frames, the UiPath analyser for selectors that go through an IFRAME, and
conformance, fidelity replay and the harness bot use `locate()` to find them. Open shadow roots
need nothing: Playwright's CSS looks inside them. Elements in closed shadow roots cannot be
located by any selector; recordings still show them.

`resolve()` finds a recorded step's target. The recorder keeps, per step, candidates in rank
order, each with the number of elements it matched when recorded: a unique test id or id, role
and accessible name, label, placeholder, the text of a link or button, and the structure (the
nearest ancestor with an id, the path from it, and the element's index). A control repeated once
per record (a list item's checkbox, a row's delete button) has a `record` candidate instead: the
record whose title is the value typed at an earlier step (or named in the bindings), and the
control's path inside it; candidates that would pick such a control by position are not kept.
The first candidate that matches exactly one element is the target. When none does, the step is
BLOCKED with what each candidate matched: one of several is never taken.

An id, test id or class that was a whole typed value is recorded as its placeholder
(`tl-typed-a`, twinlab2_contract.evidence.typedvalues), as the rebuilt page has it. The step names the step that
value was typed at (`typed_placeholders`); given the values the replay types (`typed`), each
such candidate is also tried with the value in place of the placeholder, right after it, so the
same step finds the element on the rebuilt page and on the application itself.
"""

from __future__ import annotations

import json
import re
import time

FRAME = " >>> "


def parts(selector: str) -> tuple[list[str], str]:
    """(frames, element) of a selector."""
    bits = [b.strip() for b in selector.split(">>>")]
    return [b for b in bits[:-1] if b], bits[-1]


def locate(page, selector: str):
    """A Playwright Locator for `selector`, entering each frame it names."""
    frames, element = parts(selector)
    scope = page
    for frame in frames:
        scope = scope.frame_locator(frame)
    return scope.locator(element)


def join(frames: list[str], element: str) -> str:
    return FRAME.join([*frames, element]) if frames else element


# --- resolving a recorded step's target: ranked candidates, exactly one match -------------------

# A record's title: its text without the text of its controls. The recorder and the replay use
# this same function, so a title typed at one step names the same record at a later one.
ROW_TITLE_FN = r"""(row) => {
  let out = "";
  const walk = (node) => {
    for (const c of node.childNodes) {
      if (c.nodeType === 3) out += " " + c.data;
      else if (c.nodeType === 1 && !/^(button|select|option|script|style|textarea|template)$/.test(c.localName)) walk(c);
    }
  };
  walk(row);
  return out.replace(/\s+/g, " ").trim();
}"""
ROW_SELECTOR = "li, tr, [role=row], [role=listitem], [role=option], [role=treeitem]"
_TITLES_JS = "(rows) => rows.map(" + ROW_TITLE_FN + ")"
# The real ids of elements whose id, with its digits masked, is the recorded (masked) id, each
# with the title of the record it is on.
_UNMASK_ALL_JS = (
    "(root, masked) => [...root.ownerDocument.querySelectorAll('[id]')]"
    ".filter(e => e.id.replace(/[0-9]/g, '9') === masked)"
    ".map(e => [e.id, (" + ROW_TITLE_FN + ")(e.closest('" + ROW_SELECTOR + "') || e)])"
)
NO_BINDING = "no binding names the record"


class Unresolved(Exception):  # noqa: N818 - a result, reported as BLOCKED
    """No candidate matched exactly one element. `tried` says what each matched."""

    def __init__(self, message: str, tried: list[str], missing_binding: bool = False):
        super().__init__(message)
        self.tried = tried
        self.missing_binding = missing_binding


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def row_selectors(rows: str) -> list[str]:
    """A record locator's rows, then the forms that also fit a list that is itself the
    identified element: `#list div.row > div.col` (written by an older recorder for
    `<div id="list" class="row">`) is also `#list > div.col` and `#list div.col`."""
    out = [rows]
    m = re.fullmatch(r"(\S+)\s+(\S+)\s*>\s*(\S+)", rows.strip())
    if m:
        out += [f"{m.group(1)} > {m.group(3)}", f"{m.group(1)} {m.group(3)}"]
    return out


def title_hits(titles: list[str], title: str) -> list[int]:
    """The rows a binding names: those whose whole text is the binding (the recorder's form),
    else those whose text holds it as a whole phrase (a record's title: `Nokia lumia 1520` in
    `Nokia lumia 1520 820 Delete`), ignoring case. The caller wants exactly one."""
    want = _norm(title)
    exact = [i for i, t in enumerate(titles) if _norm(t) == want]
    if exact or not want:
        return exact
    rx = re.compile(r"(?<!\w)" + re.escape(want.lower()) + r"(?!\w)")
    return [i for i, t in enumerate(titles) if rx.search(_norm(t).lower())]


def describe(cand: dict) -> str:
    kind = cand.get("kind")
    if kind == "testid":
        return f'[{cand.get("attr")}="{cand.get("value")}"]'
    if kind == "id":
        return f"#{cand.get('value')}"
    if kind == "role":
        return f'role {cand.get("role")} "{cand.get("name")}"'
    if kind in ("label", "placeholder"):
        return f'{kind} "{cand.get("text")}"'
    if kind == "text":
        return f'{cand.get("tag")} with text "{cand.get("text")}"'
    if kind == "structure":
        return f"structure {cand.get('css')} [{cand.get('index')} of {cand.get('count')}]"
    if kind == "record":
        whose = (f"the record titled as typed at step {cand['text_from']}"
                 if cand.get("text_from") is not None else "the record named in the bindings")  # fmt: skip
        inside = f" > {cand['control']}" if cand.get("control") else ""
        return f"{whose} ({cand.get('rows')}{inside})"
    return f"css {cand.get('css')}"


# A placeholder in a selector: `#tl-typed-a`, `.tl-typed-a`, or an attribute selector's value.
_PLACEHOLDER = re.compile(
    r"""([#.])(tl-typed-[a-z]+)(?![\w-])|(["'])(tl-typed-[a-z]+)["']|(?<==)(tl-typed-[a-z]+)\b"""
)


def _unplaced(cand: dict, typed: dict[str, str]) -> dict | None:
    """The candidate with each placeholder it names replaced by the value typed (None when it
    names none the replay has a value for)."""

    def css(m: re.Match) -> str:
        ph = m.group(2) or m.group(4) or m.group(5)
        value = typed.get(ph)
        if value is None:
            return m.group()
        if m.group(1) == "#":
            return f"[id={json.dumps(value)}]"
        if m.group(1) == ".":
            return f"[class~={json.dumps(value)}]"
        return json.dumps(value)

    out = dict(cand)
    if cand.get("kind") in ("id", "testid") and cand.get("value") in typed:
        out["value"] = typed[cand["value"]]
    for k in ("css", "rows", "control"):
        if isinstance(cand.get(k), str):
            out[k] = _PLACEHOLDER.sub(css, cand[k])
    return out if out != cand else None


def candidates(step, typed: dict[str, str] | None = None) -> list[dict]:
    """The step's ranked candidates. A step from an older recording has its selector only; a
    newer one also has its selector last, unless the target is a per-record control. With
    `typed` (placeholder -> the value the replay typed for it), a candidate naming a
    placeholder is followed by the same candidate naming the value."""
    element = parts(step.selector)[1] if step.selector else None
    out = [dict(c) for c in getattr(step, "locators", None) or []]
    per_record = any(c.get("kind") == "record" for c in out)
    if element and not per_record and element not in ("body", "html"):
        out.append({"kind": "css", "css": element})
    if typed:
        out = [x for c in out for x in (c, _unplaced(c, typed)) if x is not None]
    return out


def typed_values(step, values: dict[int, str]) -> dict[str, str]:
    """placeholder -> the value bound to the step it was typed at, for a step that names
    placeholders (`typed_placeholders`)."""
    out = {}
    for note in getattr(step, "typed_placeholders", None) or []:
        value = values.get(note.get("typed_at")) if isinstance(note, dict) else None
        if isinstance(value, str) and value.strip():
            out[str(note.get("placeholder"))] = value.strip()
    return out


def record_of(step) -> dict | None:
    return next(
        (c for c in getattr(step, "locators", None) or [] if c.get("kind") == "record"), None
    )


def _scope(page, step):
    frame = getattr(step, "frame", None)
    frames = parts(frame + FRAME + "x")[0] if frame else parts(step.selector or "x")[0]
    scope = page
    for f in frames:
        scope = scope.frame_locator(f)
    return scope


def _one(scope, cand: dict, title: str | None):
    """(locator or None, live count or None when not tried, why not)."""
    kind = cand.get("kind")
    recorded = cand.get("count")
    if kind not in ("css", "structure", "record") and recorded not in (None, 1):
        return None, None, f"matched {recorded} when recorded, so it did not name one element"
    if kind == "testid":
        loc = scope.locator(f"[{cand['attr']}={json.dumps(str(cand['value']))}]")
    elif kind == "id":
        loc = scope.locator(f"[id={json.dumps(str(cand['value']))}]")
    elif kind == "role":
        loc = scope.get_by_role(cand["role"], name=cand["name"], exact=True)
    elif kind == "label":
        loc = scope.get_by_label(cand["text"], exact=True)
    elif kind == "placeholder":
        loc = scope.get_by_placeholder(cand["text"], exact=True)
    elif kind == "text":
        loc = scope.locator(f"{cand['tag']}:text-is({json.dumps(cand['text'])})")
    elif kind == "structure":
        loc = scope.locator(cand["css"])
        n = loc.count()
        want = int(recorded or 0)
        if want == 1 and n == 1:
            return loc, n, None
        if want > 1 and n == want:  # the same structure as recorded: the same position in it
            return loc.nth(int(cand.get("index") or 0)), n, None
        return None, n, f"{want} when recorded"
    elif kind == "record":
        if not title:
            return None, None, NO_BINDING
        rows = scope.locator(cand["rows"])
        for alt in row_selectors(cand["rows"])[1:]:
            if rows.count():
                break
            rows = scope.locator(alt)  # an older recording's locator for a list that is itself
        titles = rows.evaluate_all(_TITLES_JS)
        hits = title_hits(titles, title)
        if len(hits) != 1:
            return None, len(hits), f"{len(hits)} of {len(titles)} record(s) have that title"
        row = rows.nth(hits[0])
        loc = row.locator(":scope > " + cand["control"]) if cand.get("control") else row
        n = loc.count()
        return (loc, 1, None) if n == 1 else (None, n, "the control is not once in the record")
    elif kind == "css":
        return _css(scope, cand["css"], title)
    else:
        return None, None, f"unknown candidate kind {kind}"
    n = loc.count()
    return (loc, n, None) if n == 1 else (None, n, None)


def _css(scope, selector: str, title: str | None):
    """A plain selector. A recording masks record ids (`#loan-L-200040` is kept as
    `#loan-L-999999`): when no element has the masked id, the element whose id has that shape
    stands for it if there is exactly one, or the one on the record the bindings name
    (`record:`). With several and no record named, none is chosen: the step blocks."""
    loc = scope.locator(selector)
    n = loc.count()
    if n == 1:
        return loc, 1, None
    simple = selector.startswith("#") and not any(c in selector for c in " >+~[.:,")
    if n or not simple or "9" not in selector:
        return None, n, None
    try:
        found = scope.locator("html").evaluate(_UNMASK_ALL_JS, selector[1:])
    except Exception:  # noqa: BLE001 - a frame that went away
        return None, 0, None
    if title:
        found = [f for f in found if _norm(f[1]) == _norm(title)]
    if len(found) == 1:
        return scope.locator(f"[id={json.dumps(found[0][0])}]"), 1, None
    if len(found) > 1:
        return None, len(found), "ids of this shape; name the record in the bindings to choose"
    return None, 0, None


def resolve(page, step, title: str | None = None, timeout_ms: int = 5000,
            typed: dict[str, str] | None = None):  # fmt: skip
    """(Locator, what found it) for the step's target: the first candidate, in rank order, that
    matches exactly one element, waiting up to `timeout_ms` for the page to show it. One of
    several is never taken: Unresolved is raised, saying what each candidate matched.
    (None, None) when the step names no element (a key pressed on the page itself). `typed`:
    see `candidates`."""
    cands = candidates(step, typed)
    if not cands:
        return None, None
    scope = _scope(page, step)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        tried, unbound = [], False
        for cand in cands:
            try:
                loc, live, why = _one(scope, cand, title)
            except Exception as exc:  # noqa: BLE001 - a selector this page cannot read
                loc, live, why = None, None, type(exc).__name__
            if loc is not None:
                return loc, describe(cand)
            unbound = unbound or why == NO_BINDING
            seen = "not tried" if live is None else f"matched {live}"
            tried.append(f"{describe(cand)}: {seen}" + (f" ({why})" if why else ""))
        if time.monotonic() >= deadline:
            raise Unresolved("no candidate matched exactly one element: " + "; ".join(tried),
                             tried, missing_binding=unbound)  # fmt: skip
        page.wait_for_timeout(100)
