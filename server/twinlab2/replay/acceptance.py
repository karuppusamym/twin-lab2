# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Acceptance cases beyond structure (replication gap R05).

A fidelity replay compares DOM structure, so a replica that shows a wrong count, title, flag,
saved value or message can still pass it. Acceptance cases say what must be true, declared
independently of the replica's output, in `acceptance.yaml` beside the recording (next to
`bindings.yaml`; `--acceptance` names another file). The file is kept with the evidence, not under
`apps/<app>/`, because whoever builds the app writes there and must not change what judges it.

```yaml
version: 1
recording: 20261117-101500
scope: [add tasks, complete a task, reload]        # what this run claims, published with it
excluded: [banner and footer styling]              # declared out of scope
unobserved: [server-side validation]               # needed but not seen in the source
cases:
  - id: titles-after-reload
    after: end              # a step number, or end (the default)
    category: behaviour     # behaviour | data | appearance (default from the check)
    required: true          # the default; an optional case never decides the result
    reload: true            # reload the page first (refresh persistence)
    check: texts            # see below
    locator: .todo-list li label
    equals: [{binding: 4}, {binding: 6}]   # the values bound to steps 4 and 6
  - id: cart-titles
    check: texts
    locator: "#tbodyid > tr > td:nth-child(2)"
    unordered: true         # the same items in any order (a multiset): for a list whose order
    equals: [Nokia, Samsung]   # the recording does not show to be stable
  - id: one-left
    check: text
    locator: {role: status}                # or css text, {css}, {text}, {label}, {testid}
    equals: "1 item left"
  - id: saved
    check: storage
    area: localStorage      # localStorage | sessionStorage | indexedDB | cookie
    key: tl-todos
    path: $[0].completed    # a JSON path into the stored value ($, .name, [n], .length)
    equals: true
  - id: isolated
    fresh_context: true     # a new browser context from the declared baseline: nothing leaks
    check: count
    locator: .todo-list li
    equals: 0
  - id: db-outcome
    check: outcome          # an automation's `outcome:` case (outcome.py), on the lab database
    automation: settle-claims
    case: default
```

Checks: `text` (the visible text of one element; `equals`, `contains` or `matches`), `texts`
(the texts of every match, in order; `unordered: true` compares them as a multiset, the same
items as often in any order, for a list whose order is not stable), `count`, `value` (a field's value), `checked`, `disabled`,
`visible`, `url` (path, query and fragment, the origin left out), `storage` (`present`, or the
value at `path`), and `outcome`. A locator that must name one element and matches several is
BLOCKED, never the first of them. Expected values are literals or `{binding: <step>}` (the value
the bindings give that step) or `{record: <step>}` (the record a step acts on): synthetic values a
person supplies, never values taken from the recording. A value still `TODO` blocks its case.

Each case is PASS, FAIL, BLOCKED (could not be evaluated) or SKIPPED (optional and not run).
Twin Lab's `behaviour acceptance <recording>` writes a template of candidate cases from the
recording's structure (shapes and counts only, `TODO` in place of every value).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from twinlab2_contract.evidence import dom as domlib

FILE = "acceptance.yaml"
CATEGORIES = ("behaviour", "data", "appearance")
CHECKS = ("text", "texts", "count", "value", "checked", "disabled", "visible", "url", "storage",
          "outcome")  # fmt: skip
AREAS = ("localStorage", "sessionStorage", "indexedDB", "cookie")
PASS, FAIL, BLOCKED, SKIPPED = "PASS", "FAIL", "BLOCKED", "SKIPPED"
TODO = "TODO"
_DATA_CHECKS = {"storage", "url", "outcome"}


@dataclass
class Case:
    id: str
    check: str
    after: int | str = "end"
    category: str = "behaviour"
    required: bool = True
    locator: Any = None
    op: str | None = None  # equals | contains | matches | present
    expected: Any = None
    area: str | None = None
    key: str | None = None
    path: str | None = None
    database: str | None = None
    store: str | None = None
    reload: bool = False
    unordered: bool = False  # texts: compare as a multiset, in any order
    fresh_context: bool = False
    automation: str | None = None
    case: str | None = None
    basis: str = "evidence"  # evidence | assumed
    problem: str | None = None  # the case cannot be read: BLOCKED

    @property
    def when(self) -> str:
        return "end" if self.after == "end" else f"step {self.after}"


@dataclass
class Spec:
    cases: list[Case] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    unobserved: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    assumed: list[str] = field(default_factory=list)
    path: Path | None = None


class CaseResult(BaseModel):
    id: str
    check: str
    category: str
    required: bool = True
    after: str = "end"
    status: str  # PASS | FAIL | BLOCKED | SKIPPED
    expected: Any = None  # synthetic: from the bindings or a literal
    actual: Any = None  # what the replica showed (it was given synthetic values only)
    note: str | None = None
    step: int | None = None
    basis: str = "evidence"
    screenshot: str | None = None  # the replica when the case failed (when pictures are kept)
    kind: str = "case"  # case | visual


# --- reading the file ------------------------------------------------------------------------------


def _strs(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _has_todo(value) -> bool:
    if isinstance(value, str):
        return value.strip().upper() == TODO
    if isinstance(value, list):
        return any(_has_todo(v) for v in value)
    if isinstance(value, dict):
        return any(_has_todo(v) for v in value.values())
    return False


def parse_case(entry: dict, n: int) -> Case:
    cid = str(entry.get("id") or f"case-{n}")
    check = str(entry.get("check") or "")
    after = entry.get("after", "end")
    case = Case(id=cid, check=check)
    if isinstance(after, int) and not isinstance(after, bool):
        case.after = after
    elif str(after).strip().lower() == "end":
        case.after = "end"
    else:
        try:
            case.after = int(str(after))
        except ValueError:
            case.problem = f"after: {after!r} is neither a step number nor end"
    case.category = str(entry.get("category") or ("data" if check in _DATA_CHECKS else "behaviour"))
    case.required = bool(entry.get("required", True))
    case.locator = entry.get("locator")
    for op in ("equals", "contains", "matches", "present"):
        if op in entry:
            case.op, case.expected = op, entry[op]
            break
    for name in ("area", "key", "path", "database", "store", "automation", "case"):
        if entry.get(name) is not None:
            setattr(case, name, str(entry[name]))
    case.reload = bool(entry.get("reload", False))
    case.unordered = bool(entry.get("unordered", False))
    case.fresh_context = bool(entry.get("fresh_context", False))
    case.basis = str(entry.get("basis") or "evidence")
    if case.problem:
        return case
    if check not in CHECKS:
        case.problem = f"unknown check {check!r} (one of {', '.join(CHECKS)})"
    elif case.category not in CATEGORIES:
        case.problem = f"unknown category {case.category!r} (one of {', '.join(CATEGORIES)})"
    elif check == "storage" and case.area not in AREAS:
        case.problem = f"storage needs area: one of {', '.join(AREAS)}"
    elif check == "storage" and case.area != "indexedDB" and not case.key:
        case.problem = "storage needs key:"
    elif check == "outcome" and not case.automation:
        case.problem = "outcome needs automation: (and case:, or the automation's default)"
    elif check not in ("url", "storage", "outcome") and case.locator in (None, ""):
        case.problem = f"{check} needs locator:"
    elif case.op is None and check not in ("visible", "outcome", "storage"):
        case.problem = "no expectation: give equals, contains or matches"
    elif case.unordered and check != "texts":
        case.problem = "unordered: applies to a texts check only"
    elif case.unordered and case.op != "equals":
        case.problem = "unordered: needs equals: (a list of the expected texts)"
    elif case.unordered and not isinstance(case.expected, list):
        case.problem = "unordered: equals: must be a list"
    elif _has_todo(case.expected) or _has_todo(case.locator) or _has_todo(case.path):
        case.problem = "not completed: a value is still TODO"
    return case


def load(path: Path | None) -> Spec | None:
    """The acceptance file, or None when there is none. A case that cannot be read is kept, as
    BLOCKED, so a typo never makes a required case disappear."""
    if path is None or not Path(path).exists():
        return None
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not an acceptance file")
    spec = Spec(path=Path(path))
    for name in ("scope", "excluded", "unobserved", "unsupported", "assumed"):
        setattr(spec, name, _strs(data.get(name)))
    seen: set[str] = set()
    for n, entry in enumerate(data.get("cases") or [], 1):
        if not isinstance(entry, dict):
            spec.cases.append(Case(id=f"case-{n}", check="?", problem="not a mapping"))
            continue
        case = parse_case(entry, n)
        if case.id in seen:
            case.problem = case.problem or f"the id {case.id!r} is used twice"
            case.id = f"{case.id}#{n}"
        seen.add(case.id)
        spec.cases.append(case)
    return spec


# --- expected values -------------------------------------------------------------------------------


class _Unbound(Exception):
    pass


def resolve_expected(value, binding: Callable[[int], str | None],
                     record: Callable[[int], str | None]):  # fmt: skip
    """Literals stay; `{binding: n}` and `{record: n}` come from the replay's bindings."""
    if isinstance(value, list):
        return [resolve_expected(v, binding, record) for v in value]
    if isinstance(value, dict) and len(value) == 1:
        (kind, ref), = value.items()  # fmt: skip
        if kind in ("binding", "record") and isinstance(ref, dict):
            # another application's step, in a split part: supplied from outside
            got = (binding if kind == "binding" else record)(ref)
            if got is None:
                raise _Unbound(f"no value is supplied from outside for step {ref.get('step')} "
                               f"of {ref.get('app')} (bindings.yaml outside:)")  # fmt: skip
            return got
        if kind in ("binding", "record"):
            try:
                seq = int(ref)
            except (TypeError, ValueError) as exc:
                raise _Unbound(f"{kind}: {ref!r} is not a step number") from exc
            got = (binding if kind == "binding" else record)(seq)
            if got is None:
                raise _Unbound(f"no binding gives the {'value' if kind == 'binding' else 'record'}"
                               f" of step {seq}")  # fmt: skip
            return got
    return value


def show(value) -> Any:
    """A value as the report keeps it: short."""
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "..."
    if isinstance(value, list) and len(value) > 50:
        return [*value[:50], f"... {len(value) - 50} more"]
    return value


# --- locating -----------------------------------------------------------------------------------


def locator_for(page, spec):
    """A Playwright locator for a case's `locator:`; CSS may cross frames (`iframe#x >>> .y`)."""
    from twinlab2_contract.evidence.locate import locate

    if isinstance(spec, str):
        return locate(page, spec)
    if not isinstance(spec, dict):
        raise ValueError(f"locator {spec!r} is neither CSS nor a mapping")
    if spec.get("role"):
        if spec.get("name") is not None:
            return page.get_by_role(spec["role"], name=str(spec["name"]), exact=True)
        return page.get_by_role(spec["role"])
    if spec.get("testid") is not None:
        return page.get_by_test_id(str(spec["testid"]))
    if spec.get("label") is not None:
        return page.get_by_label(str(spec["label"]), exact=True)
    if spec.get("text") is not None:
        return page.get_by_text(str(spec["text"]), exact=True)
    if spec.get("css"):
        return locate(page, str(spec["css"]))
    raise ValueError(f"locator {spec!r}: give css, role (and name), testid, label or text")


def describe_locator(spec) -> str:
    if isinstance(spec, dict):
        return ", ".join(f"{k}={v}" for k, v in spec.items())
    return str(spec)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


_PATH = re.compile(r"\.([A-Za-z_$][\w$-]*)|\[(\d+)\]|\[\"([^\"]+)\"\]|\['([^']+)'\]")


def json_path(value, path: str | None):
    """`$[0].title`, `$.items.length`. Raises KeyError when the path is not there."""
    if not path or path.strip() == "$":
        return value
    text = path.strip()
    text = text[1:] if text.startswith("$") else "." + text.lstrip(".")
    pos = 0
    for m in _PATH.finditer(text):
        if m.start() != pos:
            raise KeyError(f"cannot read the path {path!r} at {text[pos:]!r}")
        pos = m.end()
        name, index = m.group(1) or m.group(3) or m.group(4), m.group(2)
        if index is not None:
            if not isinstance(value, list) or int(index) >= len(value):
                raise KeyError(f"{path}: no item {index}")
            value = value[int(index)]
        elif name == "length" and isinstance(value, (list, str, dict)):
            value = len(value)
        elif isinstance(value, dict) and name in value:
            value = value[name]
        else:
            raise KeyError(f"{path}: no {name!r}")
    if pos != len(text):
        raise KeyError(f"cannot read the path {path!r} at {text[pos:]!r}")
    return value


def _same(expected, actual) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual or expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(map(_same, expected, actual))
    if isinstance(expected, str) and isinstance(actual, str):
        return _norm(expected) == _norm(actual)
    return expected == actual


def same_multiset(expected, actual) -> bool:
    """The same items as often, in any order (each expected item matched to one actual item)."""
    if not isinstance(expected, list) or not isinstance(actual, list):
        return False
    if len(expected) != len(actual):
        return False
    left = list(actual)
    for want in expected:
        for i, got in enumerate(left):
            if _same(want, got):
                del left[i]
                break
        else:
            return False
    return True


def _compare(op: str | None, expected, actual, unordered: bool = False) -> bool:
    if unordered and op in (None, "equals"):
        return same_multiset(expected, actual)
    if op == "contains":
        if isinstance(actual, list):
            return any(_same(expected, a) for a in actual)
        return _norm(expected) in _norm(actual)
    if op == "matches":
        return re.search(str(expected), str(actual)) is not None
    return _same(expected, actual)


# --- evaluating -----------------------------------------------------------------------------------

_IDB_JS = r"""async ([db, store, key]) => {
  const dbs = indexedDB.databases ? await indexedDB.databases() : null;
  if (dbs && !dbs.some(d => d.name === db)) return {missing: "database"};
  return await new Promise((done) => {
    const req = indexedDB.open(db);
    req.onerror = () => done({missing: "database"});
    req.onsuccess = () => {
      const conn = req.result;
      if (!conn.objectStoreNames.contains(store)) { conn.close(); done({missing: "store"}); return; }
      const st = conn.transaction(store, "readonly").objectStore(store);
      const r = key === null ? st.getAll() : st.get(key);
      r.onsuccess = () => { conn.close(); done({value: r.result === undefined ? null : r.result}); };
      r.onerror = () => { conn.close(); done({missing: "record"}); };
    };
  });
}"""


def _storage_value(page, case: Case):
    """(present, value) of what a storage case reads."""
    if case.area == "cookie":
        found = [c for c in page.context.cookies(page.url) if c["name"] == case.key]
        return (bool(found), found[0]["value"] if found else None)
    if case.area == "indexedDB":
        key = case.key
        if key is not None and re.fullmatch(r"-?\d+", key):
            key = int(key)
        got = page.evaluate(_IDB_JS, [case.database or "", case.store or "", key])
        if "missing" in got:
            return False, None
        return got["value"] is not None, got["value"]
    raw = page.evaluate("([area, key]) => window[area].getItem(key)", [case.area, case.key])
    if raw is None:
        return False, None
    try:
        return True, json.loads(raw)
    except ValueError:
        return True, raw


def evaluate(page, case: Case, expected, outcome_judge=None) -> tuple[str, Any, str | None]:
    """(status, actual, note) of one case on the page as it is now."""
    check = case.check
    if check == "outcome":
        if outcome_judge is None:
            return BLOCKED, None, ("needs the lab database session of this run (outcome.py): "
                                   "run against a lab with --outcome-session")  # fmt: skip
        result = outcome_judge(case.automation, case.case)
        status = getattr(result, "status", None)
        summary = result.summary() if hasattr(result, "summary") else str(result)
        if status == "MET":
            return PASS, status, None
        if status == "NOT_MET":
            return FAIL, status, summary
        return BLOCKED, status, summary
    if check == "url":
        from urllib.parse import urlsplit

        u = urlsplit(page.url)
        actual = u.path + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment
                                                                  else "")  # fmt: skip
        return (PASS if _compare(case.op, expected, actual) else FAIL), actual, None
    if check == "storage":
        present, value = _storage_value(page, case)
        if case.op == "present":
            return (PASS if present == bool(expected) else FAIL), present, None
        if not present:
            return FAIL, None, f"{case.area} has no {case.key or case.store!r}"
        try:
            actual = json_path(value, case.path)
        except KeyError as exc:
            return FAIL, None, str(exc)
        if case.op in (None, "present"):
            return PASS, show(actual), None
        return (PASS if _compare(case.op, expected, actual) else FAIL), show(actual), None
    try:
        loc = locator_for(page, case.locator)
        n = loc.count()
    except Exception as exc:  # noqa: BLE001 - a locator this page cannot read
        return BLOCKED, None, f"the locator cannot be used: {type(exc).__name__}: {exc}"[:300]
    if check == "count":
        return (PASS if _compare(case.op, expected, n) else FAIL), n, None
    if check == "texts":
        actual = [_norm(t) for t in loc.all_inner_texts()]
        ok = _compare(case.op, expected, actual, unordered=case.unordered)
        note = "compared in any order (unordered)" if case.unordered else None
        return (PASS if ok else FAIL), show(actual), note
    if n == 0:
        return FAIL, None, f"no element matches {describe_locator(case.locator)}"
    if n > 1:
        return BLOCKED, None, (f"{describe_locator(case.locator)} matched {n} elements; a case on "
                               "one element needs a locator that names exactly one")  # fmt: skip
    if check == "visible":
        actual = loc.is_visible()
        want = True if case.op is None else expected
        return (PASS if _same(bool(want), actual) else FAIL), actual, None
    if check == "text":
        if not loc.is_visible():
            return FAIL, None, "the element is not visible"
        actual = _norm(loc.inner_text())
    elif check == "value":
        actual = loc.input_value()
    elif check == "checked":
        actual = loc.is_checked()
    elif check == "disabled":
        actual = loc.is_disabled()
    else:  # pragma: no cover - parse_case rejects it
        return BLOCKED, None, f"unknown check {check}"
    return (PASS if _compare(case.op, expected, actual) else FAIL), show(actual), None


# --- the template -------------------------------------------------------------------------------

_MESSAGE = re.compile(r"count|message|msg|error|alert|status|notice|toast|total|summary|result",
                      re.I)  # fmt: skip


def _css_of(node: domlib.Node) -> str | None:
    if node.id:
        return f"#{node.id}"
    if node.classes:
        return node.tag + "".join(f".{c}" for c in node.attrs.get("class", "").split()[:2])
    return None


def _walk(node: domlib.Node):
    for child in node.children:
        yield child
        yield from _walk(child)


def _visible(node: domlib.Node) -> bool:
    cur = node
    while cur is not None and cur.tag != "#document":
        if "hidden" in cur.attrs or "display:none" in (cur.attrs.get("style") or "").replace(
            " ", ""
        ):
            return False
        cur = cur.parent
    return True


def candidates_from_dom(html: str, after) -> list[dict]:
    """Candidate cases for one recorded snapshot: lists (count and order), messages, boxes."""
    root = domlib.parse_html(html)
    out: list[dict] = []
    seen: set[str] = set()
    for node in _walk(root):
        if node.tag in ("ul", "ol", "tbody") and _visible(node):
            items = [c for c in node.children if c.tag in ("li", "tr")]
            css = _css_of(node)
            if not items or not css or css in seen:
                continue
            seen.add(css)
            item = "li" if node.tag != "tbody" else "tr"
            if any("href" in d.attrs for i in items for d in _walk(i)) and len(items) <= 6:
                continue  # navigation links, not records
            out.append({"id": f"count-{css.strip('#.').replace('.', '-')}", "after": after,
                        "category": "behaviour", "check": "count", "locator": f"{css} > {item}",
                        "equals": TODO, "hint": f"{len(items)} when recorded"})  # fmt: skip
            out.append({"id": f"order-{css.strip('#.').replace('.', '-')}", "after": after,
                        "category": "behaviour", "check": "texts", "locator": f"{css} > {item}",
                        "equals": TODO,
                        "unordered": True,
                        "hint": "each item's text, from the bindings: [{binding: <step>}, ...]; "
                                "one recorded view does not show the order to be stable, so it "
                                "is compared in any order: delete unordered only when the order "
                                "follows a key that is not random or a sort control"})  # fmt: skip
        elif node.tag not in ("script", "style", "input", "select", "textarea", "button", "a"):
            css = _css_of(node)
            role = node.attrs.get("role")
            name = " ".join([node.attrs.get("class", ""), node.id or ""])
            if not css or css in seen or not (_MESSAGE.search(name) or role in ("alert", "status")):
                continue
            if not _visible(node):
                continue
            text = domlib._text(node)
            if not text:
                continue
            seen.add(css)
            out.append({"id": f"message-{css.strip('#.').replace('.', '-')}", "after": after,
                        "category": "behaviour", "check": "text", "locator": css,
                        "equals": TODO, "hint": f"shape when recorded: {domlib.shape(text)}"})  # fmt: skip
    for node in _walk(root):
        if node.tag == "input" and (node.attrs.get("type") or "").lower() in ("checkbox",
                                                                              "radio"):  # fmt: skip
            if node.id and _visible(node):
                out.append({"id": f"checked-{node.id}", "after": after, "category": "behaviour",
                            "check": "checked", "locator": f"#{node.id}", "equals": TODO})  # fmt: skip
    return out


def _raw_steps(rec_path: Path) -> list[dict]:
    """The recording's lines as written, for optional fields the step model may not carry yet
    (`storage`, `screenshot`, `tab`)."""
    out = []
    path = Path(rec_path) / "steps.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def storage_keys(rec_path: Path) -> dict[str, list[str]]:
    """The storage keys the recording saw last (from the optional per-step `storage` object:
    keys and masked shapes only)."""
    last: dict[str, list[str]] = {}
    for d in _raw_steps(rec_path):
        st = d.get("storage")
        if not isinstance(st, dict):
            continue
        for area in ("localStorage", "sessionStorage"):
            if isinstance(st.get(area), dict):
                last[area] = sorted(str(k) for k in st[area])
    return last


def template(rec) -> str:
    """Candidate cases for a recording, with TODO for every value. A person completes them (or
    deletes the ones that do not matter) before they judge anything."""
    from urllib.parse import urlsplit

    from twinlab2_contract.evidence.recording import path_pattern

    cases: list[dict] = []
    with_dom = [s for s in rec.steps if s.dom]
    last = with_dom[-1] if with_dom else None
    if last is not None:
        html = rec.dom(last) or ""
        cases += candidates_from_dom(html, "end")
        if last.url:
            u = urlsplit(last.url)
            cases.append({"id": "url-at-end", "after": "end", "category": "data", "check": "url",
                          "matches": TODO,
                          "hint": f"recorded path pattern {path_pattern(u.path)}"
                                  + (" with a fragment" if u.fragment else "")})  # fmt: skip
    for step in rec.steps:
        if step.action == "goto" and step.nav == "reload":
            html = rec.dom(step) or ""
            for c in candidates_from_dom(html, step.seq):
                if c["check"] == "count":
                    c["id"] = f"{c['id']}-after-reload-{step.seq}"
                    c["category"] = "data"
                    c["hint"] = "refresh persistence: " + c["hint"]
                    cases.append(c)
    for area, keys in storage_keys(rec.path).items():
        for key in keys:
            cases.append({"id": f"{area}-{key}-present", "after": "end", "category": "data",
                          "check": "storage", "area": area, "key": key, "present": True})  # fmt: skip
            cases.append({"id": f"{area}-{key}-value", "after": "end", "category": "data",
                          "check": "storage", "area": area, "key": key, "path": TODO,
                          "equals": TODO})  # fmt: skip
    counts = [c for c in cases if c["check"] == "count" and c["after"] == "end"]
    if counts:
        iso = dict(counts[0])
        iso.update(id="fresh-context-isolated", category="data", fresh_context=True,
                   hint="in a new browser context from the declared baseline: what this run "
                        "stored must not be there")  # fmt: skip
        cases.append(iso)
    head = (
        "# Acceptance cases for replaying this recording. Expected values are synthetic: literals,\n"
        "# or {binding: <step>} / {record: <step>} from bindings.yaml. Never a value of the real\n"
        "# system. Replace every TODO (a TODO blocks its case) and delete what does not matter.\n"
        "# hint: is for you; it is ignored. required: false makes a case optional.\n"
        "# unordered: true on a texts case compares the list in any order (as a multiset); keep\n"
        "# it unless the order is shown to be stable (a sortable key that is not random, or a\n"
        "# sort control the recording used).\n"
    )
    body = {"version": 1, "recording": rec.id, "scope": [TODO], "excluded": [], "unobserved": [],
            "unsupported": [], "cases": cases}  # fmt: skip
    return head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def write_template(rec, out: Path | None = None) -> Path:
    out = Path(out) if out else rec.path / FILE
    if out.exists():
        raise FileExistsError(f"{out} exists: it is not overwritten (delete it, or --out)")
    out.write_text(template(rec), encoding="utf-8")
    return out
