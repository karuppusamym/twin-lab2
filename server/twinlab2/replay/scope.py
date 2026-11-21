# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Reviewed exclusions, declared before a replay (`scope.yaml` beside the recording).

Full-page comparison and a reviewed scope can disagree: a link the builder was told to leave out
(it leads out of scope) is missing on every page of the replica. A person declares, before the
run, what the comparison leaves out and why; the replay reports what each declaration excluded
in its own section, with counts, and never counts it as passed. A difference no declaration
covers still fails.

```yaml
version: 1
recording: order-to-invoice
approved_by: J. Reviewer            # who approved these exclusions (required)
approved_on: 2026-11-18
exclusions:
  - id: index-link
    element: a#homeLink             # an element: tag, #id, .class, [name=x] (all must match)
    reason: the index page is out of scope (BRIEF.md, Out of scope)
  - id: help-screens
    screen: /help/*                 # every difference on steps whose page path matches
    reason: help pages were not built
  - id: fx-widget
    origin: https://widget.example  # requests to it are refused and counted here, not failed
    reason: third-party widget, not replicated
```

An entry without `reason` is not used (and says so); a file without `approved_by` is not used at
all. Twin Lab's `behaviour scope <recording> --from-brief apps/<app>/BRIEF.md` drafts the file from
the brief's "Out of scope" section and model.yaml's out-of-scope links, unapproved, for review.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

FILE = "scope.yaml"


@dataclass
class Exclusion:
    id: str
    reason: str
    element: str | None = None
    screen: str | None = None
    origin: str | None = None
    approved_by: str | None = None
    problem: str | None = None


@dataclass
class Scope:
    exclusions: list[Exclusion] = field(default_factory=list)
    approved_by: str | None = None
    approved_on: str | None = None
    path: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[Exclusion]:
        return [e for e in self.exclusions if not e.problem] if self.approved_by else []

    def origins(self) -> set[str]:
        from twinlab2.replay.extracts import normalise_origin

        return {normalise_origin(e.origin) for e in self.usable if e.origin}

    def name(self) -> str:
        return self.path.name if self.path else "the declared scope"


def load(path: Path | None) -> Scope | None:
    if path is None or not Path(path).exists():
        return None
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a scope file")
    sc = Scope(path=Path(path), approved_by=_str(data.get("approved_by")),
               approved_on=_str(data.get("approved_on")))  # fmt: skip
    if not sc.approved_by:
        sc.problems.append(f"{Path(path).name}: no approved_by: its exclusions are not used")
    for n, raw in enumerate(data.get("exclusions") or [], 1):
        if not isinstance(raw, dict):
            sc.problems.append(f"exclusion {n}: not a mapping")
            continue
        ex = Exclusion(id=str(raw.get("id") or f"exclusion-{n}"), reason=_str(raw.get("reason")) or "",
                       element=_str(raw.get("element")), screen=_str(raw.get("screen")),
                       origin=_str(raw.get("origin")),
                       approved_by=_str(raw.get("approved_by")) or sc.approved_by)  # fmt: skip
        if not ex.reason or ex.reason.upper() == "TODO":
            ex.problem = "no reason given"
        elif not (ex.element or ex.screen or ex.origin):
            ex.problem = "names no element, screen or origin"
        if ex.problem:
            sc.problems.append(f"exclusion {ex.id}: {ex.problem}: not used")
        sc.exclusions.append(ex)
    return sc


def _str(v) -> str | None:
    return str(v).strip() if v is not None and str(v).strip() else None


_PART = re.compile(r"([#.])([\w-]+)|\[name=['\"]?([\w-]+)['\"]?\]")


def _parts(text: str) -> tuple[str | None, str | None, set[str], str | None]:
    """(tag, id, classes, name) of a simple selector or of a difference's element key."""
    text = text.strip().split(" ")[-1]
    tag = re.match(r"^[a-zA-Z][\w-]*", text)
    ident, classes, name = None, set(), None
    for m in _PART.finditer(text):
        if m.group(1) == "#":
            ident = m.group(2)
        elif m.group(1) == ".":
            classes.add(m.group(2))
        elif m.group(3):
            name = m.group(3)
    return (tag.group(0).lower() if tag else None), ident, classes, name


def element_matches(selector: str, element: str) -> bool:
    want, have = _parts(selector), _parts(element)
    if want[0] and have[0] and want[0] != have[0]:
        return False
    if want[0] and not have[0] and not (want[1] or want[3]):
        return False
    if want[1] and want[1] != have[1]:
        return False
    if want[3] and want[3] != have[3]:
        return False
    return want[2] <= have[2]


def screen_matches(pattern: str, url: str | None) -> bool:
    path = urlsplit(url or "").path or "/"
    return fnmatch.fnmatchcase(path, pattern)


def subtree_names(html: str | None, selector: str) -> set[str]:
    """The names differences use (`#id`, `tag[name='x']` and `tag#id.class` signatures, as
    twinlab2.replay.fidelity.compare_dom writes them) of every element the selector matches in
    a snapshot and of every element inside one: a widget's inner parts are named by their own
    ids (`#example-video_html5_api`), never by the widget's class."""
    from twinlab2_contract.evidence import dom as domlib
    from twinlab2.replay.selectors import _matches, _parse

    out: set[str] = set()
    parsed = _parse(selector or "")
    if not html or not parsed:
        return out
    root = domlib.parse_html(html)

    def sig(node) -> str:
        s = node.tag + (f"#{node.id}" if node.id else "")
        return s + "".join(f".{c}" for c in node.classes if c not in domlib.POINTER_CLASSES)

    inside: set[int] = set()
    sigs: set[str] = set()
    for top in [n for n in domlib._walk(root) if _matches(n, parsed)]:
        for node in [top, *domlib._walk(top)]:
            if not node.tag or node.tag.startswith("#") or id(node) in inside:
                continue
            inside.add(id(node))
            if node.id:
                out.add(f"#{node.id}")
            if node.attrs.get("name") and node.tag in domlib.INTERACTIVE:
                out.add(f"{node.tag}[name='{node.attrs['name']}']")
            sigs.add(sig(node))
    if sigs:
        # a signature an element outside the matched ones also has (`a.nav-link`) is not theirs
        # alone: a difference on it is still compared
        outside = {sig(n) for n in domlib._walk(root)
                   if n.tag and id(n) not in inside}  # fmt: skip
        out |= {s for s in sigs if "#" in s or s not in outside}
    return out


def resolve(scope: Scope | None, *snapshots: str | None) -> dict[str, set[str]]:
    """exclusion id -> the names of the elements its `element:` matches, with everything inside
    them, in these snapshots (the recorded one, and the live one for what only the replica's
    widget has)."""
    if scope is None:
        return {}
    out: dict[str, set[str]] = {}
    for ex in scope.usable:
        if ex.element:
            out[ex.id] = set().union(*(subtree_names(h, ex.element) for h in snapshots))
    return out


def which(scope: Scope | None, element: str, url: str | None,
          names: dict[str, set[str]] | None = None) -> Exclusion | None:  # fmt: skip
    """The declaration that excludes a difference on `element` at a step on `url`, if any.
    `names` (from `resolve`) also lets an element exclusion cover the elements inside the one
    it names."""
    if scope is None:
        return None
    for ex in scope.usable:
        if ex.screen and screen_matches(ex.screen, url):
            return ex
        if ex.element and element_matches(ex.element, element):
            return ex
        if ex.element and names and element in names.get(ex.id, ()):
            return ex
    return None


# --- drafting from a brief ---------------------------------------------------------------------


def _out_of_scope_lines(brief: str) -> list[str]:
    m = re.search(r"^## Out of scope\s*$(.*?)(?=^## |\Z)", brief, flags=re.M | re.S)
    if not m:
        return []
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]


_LINK = re.compile(r"<a\b[^>]*>", re.I)


def links_to(rec, screens: list[str]) -> list[tuple[str, str]]:
    """(selector, screen) of each link on the recorded pages that leads to one of `screens`:
    the elements a builder keeps but sends elsewhere, so a full-page comparison sees them
    change."""
    from urllib.parse import urljoin

    out: dict[str, str] = {}
    for step in rec.steps:
        html = rec.dom(step) or ""
        for tag in _LINK.findall(html):
            href = re.search(r'\bhref="([^"]*)"', tag)
            if not href:
                continue
            path = urlsplit(urljoin(step.url or "", href.group(1))).path or "/"
            hit = next((s for s in screens if screen_matches(s, "http://x" + path)), None)
            if hit is None:
                continue
            ident = re.search(r'\bid="([^"]+)"', tag)
            cls = re.search(r'\bclass="([^"]+)"', tag)
            sel = (
                f"a#{ident.group(1)}"
                if ident
                else ("a." + ".".join(cls.group(1).split()[:2]) if cls else None)
            )
            if sel:
                out.setdefault(sel, hit)
    return sorted(out.items())


def draft(recording_id: str, brief_text: str = "", model: dict | None = None,
          links: list[tuple[str, str]] = ()) -> str:  # fmt: skip
    """A scope file to review: one exclusion per out-of-scope screen the brief names, per link to
    one on the recorded pages (`links`, from `links_to`) and per out-of-scope element model.yaml
    marks; `approved_by` empty (unused until a person approves it)."""
    entries: list[dict] = []
    seen: set[str] = set()
    for sel, screen in links:
        seen.add(sel)
        entries.append({"id": f"link-{_slug(sel)}", "element": sel,
                        "reason": f"leads to {screen}, out of scope in BRIEF.md: the builder keeps "
                                  "the link and sends it to an in-scope screen"})  # fmt: skip
    for line in _out_of_scope_lines(brief_text):
        for path in re.findall(r"`(/[^`\s]*)`|\*\*(/[^*\s]*)\*\*", line):
            p = path[0] or path[1]
            if p and p not in seen:
                seen.add(p)
                entries.append({"id": f"screen-{_slug(p)}", "screen": p.rstrip("/") or "/",
                                "reason": f"BRIEF.md, Out of scope: {line[:160]}"})  # fmt: skip
        for sel in re.findall(r"`([a-z]+[#.][\w.#-]+)`", line):
            if sel not in seen:
                seen.add(sel)
                entries.append({"id": f"element-{_slug(sel)}", "element": sel,
                                "reason": f"BRIEF.md, Out of scope: {line[:160]}"})  # fmt: skip
    for screen in (model or {}).get("screens") or []:
        for el in screen.get("elements") or []:
            sel = el.get("selector")
            if sel and (el.get("out_of_scope") or el.get("scope") == "out") and sel not in seen:
                seen.add(sel)
                entries.append({"id": f"element-{_slug(sel)}", "element": sel,
                                "reason": f"model.yaml: out-of-scope element on {screen.get('path')}"})  # fmt: skip
    head = ("# Exclusions for comparing a replay with this recording, declared BEFORE the run.\n"
            "# Drafted from the brief: check each one, delete what should still be compared, then\n"
            "# set approved_by. Until then nothing here is used. Excluded differences are\n"
            "# reported in their own section, never counted as passed.\n")  # fmt: skip
    body = {"version": 1, "recording": recording_id, "approved_by": "TODO", "approved_on": None,
            "exclusions": entries}  # fmt: skip
    text = head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
    return text.replace("approved_by: TODO", "approved_by: null  # TODO: who approved")


def screens_of(brief_text: str) -> list[str]:
    """The out-of-scope screen paths a brief lists."""
    out = []
    for line in _out_of_scope_lines(brief_text):
        if line.lower().startswith("screens"):
            out += [p.rstrip("/") or "/" for p in re.findall(r"`(/[^`\s]*)`", line)]
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"
