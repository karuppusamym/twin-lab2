# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Fidelity replay (Prod Echo I-22a): replay a recording against a running demo app and compare,
after each step, the live DOM with the recorded after-snapshot.

Compared: the set of interactive elements (tag, id and classes), visibility, enabled state,
option lists (texts) and value format (shape only, so data values and masked values compare by
shape). Text content is not compared: it is data. A step fails with the element named.

The replay is fail-closed. Each step's target is found from its ranked locator candidates and
must match exactly one element (twinlab2_contract.evidence.locate.resolve); values come from the bindings
(twinlab2.replay.bindings). A step whose target is missing or ambiguous, that needs a value no
binding gives, or whose action this replay cannot perform is BLOCKED, and the replay stops there.
The report counts what happened to every step (replayed, implied by the step that caused it,
failed, skipped, unresolved, missing value, unsupported) and, given the actions a flow is
expected to have, which of them the recording lacks. Its status is:

- `pass`: every step ran, none failed or was skipped, every target resolved, every value was
  bound, and every expected action was captured (never from zero steps run);
- `fail`: a step's action failed or its page differs, or an expected action was not captured;
- `blocked`: a step could not be run as recorded (and the steps after it were not run).

The first step that did not pass decides between `fail` and `blocked` (a step blocked because an
earlier one failed is its consequence). A recording across applications and tabs replays in one
browser context through an origin map (`OriginMap`): tabs are opened, switched and closed as
recorded, Back/Forward go back and forward, and no request goes to a source or to an origin with
no replica. Setup events are kept and not counted. Differences a reviewed `scope.yaml` excludes
are reported in their own section and never counted as passed.

`compare_dom` works on two snapshots alone, so a replay result can be checked without a live app.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from twinlab2.replay import bindings as bindings_mod
from twinlab2_contract import package
from twinlab2_contract.evidence import dom as domlib
from twinlab2.replay.acceptance import CaseResult
from twinlab2.replay.judging import Category, Divergence, Requirements
from twinlab2.replay.visual import VisualResult
from twinlab2_contract.evidence.recording import Recording, RecStep
from twinlab2_contract.evidence.recording import load as load_recording

Check = Literal[
    "missing", "extra", "visibility", "enabled", "options", "value_format", "action", "target",
    "value", "url", "unsupported",
    # "changed" and "ambiguous" come from desktop verify (twinlab2_recorder.desktop.verify): a control
    # found by its locator whose ClassName, FrameworkId or parent path differs, and a locator
    # that matches more than one control.
    "changed", "ambiguous",
    # Opt-in desktop judge checks (Twin Lab's `desktop verify --geometry --rows --pictures`): a
    # control's bounds or font, a grid's row count, a window picture that differs.
    "geometry", "font", "rows", "picture",
]  # fmt: skip
SUPPORTED = {
    "goto",
    "click",
    "dblclick",
    "type",
    "select",
    "submit",
    "keypress",
    "check",
    "uncheck",
}
# Performed too, with what they need (twinlab2.replay.capabilities lists each with its proof):
# an upload (a bound synthetic file), and dialogs, downloads and tab steps as recorded.
ALSO_PERFORMED = {"upload", "dialog", "download", "tab-open", "tab-switch", "tab-close"}


class Difference(BaseModel):
    step: int
    element: str
    check: Check
    expected: Any = None
    actual: Any = None

    def text(self) -> str:
        return (
            f"step {self.step}: {self.element} {self.check}"
            f" (recorded: {self.expected!r}, live: {self.actual!r})"
        )


class StepReport(BaseModel):
    seq: int
    action: str
    selector: str | None = None
    url: str | None = None
    status: Literal["pass", "fail", "skipped", "blocked"]
    differences: list[Difference] = []
    note: str | None = None
    target: str | None = None  # the candidate that found the target
    # replayed | implied | failed | not_run | unresolved | missing_value | unsupported | unmapped
    # | setup
    outcome: str | None = None
    compared: bool = False
    # Added for the replay across applications (optional, so older reports still load):
    app: str | None = None  # the application the step belongs to (its id, else source origin)
    replica_origin: str | None = None  # where the step's page was in the replay
    excluded: list[Difference] = []  # differences a declared exclusion covers (scope.yaml)
    excluded_by: list[str] = []  # the ids of those declarations, in the same order


class Excluded(BaseModel):
    """What the declared scope excluded: reported in its own section, never counted as passed."""

    declaration: str | None = None
    approved_by: str | None = None
    differences: int = 0
    steps: int = 0  # steps that differ only by excluded differences
    requests: int = 0  # requests refused to a declared-excluded origin
    by_id: dict[str, int] = {}
    reasons: dict[str, str] = {}
    problems: list[str] = []  # declarations not used, and why


class Coverage(BaseModel):
    """What happened to the recording's steps, and which expected actions it lacks."""

    setup: int = 0  # setup events (a blank page): kept, not counted as steps
    steps: int = 0
    replayed: int = 0  # ran (implied ones included)
    implied: int = 0  # a submit or navigation the step before caused: checked, not repeated
    compared: int = 0  # with a recorded snapshot to compare
    failed: int = 0
    skipped: int = 0  # not run (after a blocked step, or after a failure with stop_on_fail)
    unresolved: int = 0
    missing_value: int = 0
    unsupported: int = 0
    unmapped: int = 0  # on an origin the origin map gives no replica (blocked)
    expected: int | None = None  # actions the flow is declared to have (None: not declared)
    captured: int = 0  # semantic actions in the recording
    missing_expected: list[str] = []


class FidelityReport(BaseModel):
    recording: str
    base_url: str | None = None
    # The replay's result, as current readers (portal, conformance, e2e) know it. With acceptance
    # cases declared it is also `fail` when a required case failed, `blocked` when one was blocked.
    status: Literal["pass", "fail", "blocked"]
    steps: list[StepReport] = []
    coverage: Coverage | None = None
    problems: list[str] = []  # why the result is not pass, first one first
    # Added for R05/R06/R08/R11; all default, so older reports still load.
    verdict: str | None = None  # PASS WITHIN DECLARED SCOPE | FAIL | BLOCKED (None: older report)
    verdict_reasons: list[str] = []
    # beside a scoped PASS: VERIFIED COMPLETE | DECLARED CASES ONLY (None: no PASS, older report)
    assurance: str | None = None
    assurance_reasons: list[str] = []
    categories: dict[str, Category] = {}  # capture, replay, behaviour, data, appearance
    cases: list[CaseResult] = []
    visual: list[VisualResult] = []
    visual_setup: dict | None = None
    baseline: dict | None = None  # what was declared and applied (names only, no values)
    requirements: Requirements | None = None
    first_divergence: Divergence | None = None
    fingerprint: dict | None = None  # what was compared (twinlab2.replay.durable)
    artifacts: str | None = None  # the folder with the replica's pictures and diff images
    origins: dict[str, str] = {}  # source origin -> replica origin, as the replay used them
    blocked_requests: list[str] = []  # refused: to a source, or to an origin with no replica
    excluded: Excluded | None = None

    def first_failure(self) -> Difference | None:
        for s in self.steps:
            if s.differences:
                return s.differences[0]
        return None

    def markdown(self) -> str:
        lines = [f"# Fidelity: {self.recording}", "", f"**Result: {self.status}**", ""]
        if self.verdict:
            lines += [f"**Status: {self.verdict}**", ""]
            if self.assurance:
                lines += [f"**Assurance: {self.assurance}**", ""]
                lines += [f"- {r}" for r in self.assurance_reasons]
                lines += [""] if self.assurance_reasons else []
            lines += [f"- {r}" for r in self.verdict_reasons] + (
                [""] if self.verdict_reasons else []
            )
        if self.categories:
            lines += ["| Category | Result | Required passed / total | Optional passed / total "
                      "| Note |", "|---|---|---|---|---|"]  # fmt: skip
            for name, c in self.categories.items():
                lines.append(f"| {name} | {c.status} | {c.required_passed} / {c.required_total} "
                             f"| {c.optional_passed} / {c.optional_total} | {c.note or ''} |")  # fmt: skip
            lines.append("")
        if self.fingerprint:
            from twinlab2.replay.durable import describe

            lines += [describe(self.fingerprint), ""]
        if self.first_divergence:
            d = self.first_divergence
            lines += ["## First divergence", "", f"- {d.kind} {d.what} at step {d.step}"
                      + (f" of {d.app}" if d.app else "")
                      + (f", locator {d.locator}" if d.locator else ""), f"- {d.state}"]  # fmt: skip
            for label, p in (("source", d.before), ("replica", d.after), ("diff", d.diff)):
                if p:
                    lines.append(f"- {label} picture: {p}")
            if d.requests:
                lines.append("- source requests: " + ", ".join(d.requests))
            lines.append("")
        if self.cases:
            lines += ["## Acceptance cases", "", "| Case | Category | Required | When | Check "
                      "| Result | Note |", "|---|---|---|---|---|---|---|"]  # fmt: skip
            for c in self.cases:
                note = (c.note or "").replace("|", "/")
                lines.append(f"| {c.id} | {c.category} | {'yes' if c.required else 'no'} "
                             f"| {c.after} | {c.check} | {c.status} | {note} |")  # fmt: skip
            lines.append("")
        if self.visual:
            lines += ["## Visual states", "", "| State | Viewport | Mode | Compared | Differing "
                      "| Ratio | Excluded | Masked | Result |",
                      "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
            for v in self.visual:
                lines.append(f"| {v.id} | {v.viewport} | {v.mode} | {v.compared} | {v.differing} "
                             f"| {v.ratio:.4%} | {v.excluded} | {v.masked} | {v.status} |")  # fmt: skip
            lines.append("")
        if self.requirements:
            r = self.requirements
            for label, items in (("Scope", r.scope), ("Excluded", r.excluded),
                                 ("Evidence-backed", r.evidence_backed), ("Assumed", r.assumed),
                                 ("Unobserved", r.unobserved), ("Unsupported", r.unsupported)):  # fmt: skip
                if items:
                    lines += [f"**{label}:**", ""] + [f"- {i}" for i in items] + [""]
        c = self.coverage
        if c is not None:
            expected = "not declared" if c.expected is None else str(c.expected)
            lines += [
                "| Steps | Replayed | Implied | Compared | Failed | Skipped | Unresolved "
                "| Missing value | Unsupported | Expected actions | Captured | Not captured |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
                f"| {c.steps} | {c.replayed} | {c.implied} | {c.compared} | {c.failed} "
                f"| {c.skipped} | {c.unresolved} | {c.missing_value} | {c.unsupported} "
                f"| {expected} | {c.captured} | {len(c.missing_expected)} |",
                "",
            ]
        if self.problems:
            lines += ["## Why not pass", ""] + [f"- {p}" for p in self.problems] + [""]
        if self.origins:
            lines += ["## Replicas", "", "| Source origin | Replica origin |", "|---|---|"]
            lines += [f"| {s} | {r} |" for s, r in self.origins.items()] + [""]
        x = self.excluded
        if x is not None and (x.differences or x.requests or x.problems or x.by_id):
            lines += ["## Excluded by declared scope", "",
                      f"{x.differences} difference(s) on {x.steps} step(s) and {x.requests} "
                      f"request(s) excluded under {x.declaration} (approved by "
                      f"{x.approved_by or 'nobody'}); not counted as passed.", ""]  # fmt: skip
            lines += [f"- {k}: {n} ({x.reasons.get(k, '')})" for k, n in x.by_id.items()]
            lines += [f"- not used: {p}" for p in x.problems] + [""]
        apps = any(s.app for s in self.steps)
        lines += ["| Step | Action | Selector | Target | Result |" + (" App |" if apps else ""),
                  "|---|---|---|---|---|" + ("---|" if apps else "")]  # fmt: skip
        for s in self.steps:
            target = (s.target or "").replace("|", "/")
            lines.append(f"| {s.seq} | {s.action} | {s.selector or ''} | {target} | {s.status} |"
                         + (f" {s.app or ''} |" if apps else ""))  # fmt: skip
        diffs = [d for s in self.steps for d in s.differences]
        if diffs:
            lines += ["", "## Differences", ""] + [f"- {d.text()}" for d in diffs]
        return "\n".join(lines) + "\n"


def action_label(step: RecStep) -> str:
    """A step as a semantic action: `keypress:Enter`, `keypress:Control+s`, `goto:reload`,
    `check`, `dblclick`..."""
    if step.action == "keypress":
        return "keypress:" + "+".join([*step.modifiers, step.key or "?"])
    if step.action == "goto" and step.nav == "reload":
        return "goto:reload"
    return step.action


def captured_actions(rec: Recording) -> list[str]:
    return [action_label(s) for s in rec.steps if not s.setup]  # a setup event is no action


def missing_actions(expected: list[str], captured: list[str]) -> list[str]:
    """The expected actions the captured ones lack, matched in order. `keypress` matches any key
    press; `keypress:Enter` only that one."""
    out, i = [], 0
    for want in expected:
        j = i
        while j < len(captured) and not (
            captured[j] == want or (":" not in want and captured[j].split(":")[0] == want)
        ):
            j += 1
        if j < len(captured):
            i = j + 1
        else:
            out.append(want)
    return out


def reports_dir(root: Path, app_id: str) -> Path:
    from twinlab2_contract.upstream import NotAbsorbed
    raise NotAbsorbed("Twin Lab's lab")  # cut from the copy: docs/UPSTREAM.md


def save(root: Path, app_id: str, report: FidelityReport) -> str:
    """Keep a replay's report where the portal shows it and readiness reads it. Returns its name
    (`<recording>--<UTC stamp>`). TwinLab2 keeps the report with the release instead
    (`twinlab2.independent`)."""
    from twinlab2_contract.upstream import NotAbsorbed
    raise NotAbsorbed("Twin Lab's lab and conformance (keeping a replay report)")  # cut from the copy: docs/UPSTREAM.md


def latest(root: Path, app_id: str) -> tuple[str, FidelityReport] | None:
    """The newest replay report kept for the app, or None."""
    folder = reports_dir(root, app_id)
    found = sorted(folder.glob("*--*.json"), key=lambda p: p.stem.rsplit("--", 1)[-1])
    for path in reversed(found):
        try:
            return path.stem, FidelityReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return None


def twin_label_required() -> bool:
    """Verify replays a twin built from a format-3 package: its label must show on every screen
    (`--twin-label`), and is left out of every comparison."""
    import os

    return os.environ.get("TWINLAB2_TWIN_LABEL") == "1"


class hidden_label:
    """The twin's banner hidden while a picture is taken, so the visual comparison never sees it."""

    def __init__(self, page):
        self.page = page

    def __enter__(self):
        if twin_label_required():
            try:
                self.page.evaluate(f"document.querySelectorAll('[{package.TWIN_ATTR}]')"
                                   ".forEach(e => {{ e.dataset.twinVis = e.style.visibility; e.style.visibility = 'hidden'; }})")
            except Exception:  # noqa: BLE001 - a picture is still taken
                pass
        return self

    def __exit__(self, *exc):
        if twin_label_required():
            try:
                self.page.evaluate(f"document.querySelectorAll('[{package.TWIN_ATTR}]')"
                                   ".forEach(e => {{ e.style.visibility = e.dataset.twinVis || ''; }})")
            except Exception:  # noqa: BLE001
                pass
        return False


def _data_row(sig: str) -> bool:
    """An element repeated once per record: its id holds a masked value (`loan-L-999999`). How
    many there are depends on the data, which a demo app never shares with the real one."""
    return "#" in sig and "9" in sig.split("#", 1)[1].split(".")[0]


def compare_dom(
    expected_html: str,
    actual_html: str,
    step: int = 0,
    supplied: frozenset[str] | set[str] = frozenset(),
) -> list[Difference]:
    """Differences between a recorded after-snapshot and a live one, each naming the element.

    A per-record element counts as the same when both pages have at least one. `supplied` names
    the fields (element keys) whose value a person typed for the replay: their value format says
    nothing about the app, so it is not compared.
    """
    exp, act = domlib.snapshot(expected_html), domlib.snapshot(actual_html)
    diffs: list[Difference] = []
    ce, ca = Counter(exp.signatures), Counter(act.signatures)
    missing = ce - ca
    for sig in list(missing):
        if _data_row(sig) and ca[sig]:
            del missing[sig]
            continue
        diffs.append(
            Difference(step=step, element=sig, check="missing", expected=ce[sig], actual=ca[sig])
        )
    for sig in ca - ce:
        if _data_row(sig) and ce[sig]:
            continue
        diffs.append(
            Difference(step=step, element=sig, check="extra", expected=ce[sig], actual=ca[sig])
        )
    missing_ids = {s.split(".")[0].split("#", 1)[-1] for s in missing if "#" in s}
    for key, e in exp.elements.items():
        a = act.elements.get(key)
        if e.interactive and e.id in missing_ids:
            continue
        ev, av = e.visible, bool(a and a.visible)
        if ev != av:
            diffs.append(
                Difference(step=step, element=key, check="visibility", expected=ev, actual=av)
            )
            continue
        if a is None or not ev:
            continue
        if e.interactive and e.enabled != a.enabled:
            diffs.append(
                Difference(
                    step=step, element=key, check="enabled", expected=e.enabled, actual=a.enabled
                )
            )
        if e.options is not None and e.options != a.options:
            diffs.append(
                Difference(
                    step=step, element=key, check="options", expected=e.options, actual=a.options
                )
            )
        if e.tag in domlib.FORM_TAGS and e.value is not None and key not in supplied:
            es, as_ = domlib.shape(e.value), domlib.shape(a.value)
            if es != as_:
                diffs.append(
                    Difference(
                        step=step, element=key, check="value_format", expected=es, actual=as_
                    )
                )
    return diffs


def compare_files(expected: Path, actual: Path, step: int = 0) -> list[Difference]:
    return compare_dom(
        Path(expected).read_text(encoding="utf-8"), Path(actual).read_text(encoding="utf-8"), step
    )


def _rebase(url: str, base_url: str | None) -> str:
    if not base_url:
        return url
    b, u = urlsplit(base_url), urlsplit(url)
    if u.scheme not in ("http", "https"):  # about:blank (a new tab) is no address of the app
        return url
    return _keep_empty_fragment(url, urlunsplit((b.scheme, b.netloc, u.path, u.query, u.fragment)))


SETTLE_MAX_MS = 30000


def _track(page) -> None:
    """Count requests in flight: `networkidle` alone returns at once on a page that was idle
    before, so a slow login's POST would be outrun by the next step."""
    page._tl_inflight = 0

    def start(_r):
        page._tl_inflight += 1

    def end(_r):
        page._tl_inflight = max(0, page._tl_inflight - 1)

    page.on("request", start)
    page.on("requestfinished", end)
    page.on("requestfailed", end)


def _settle(page, settle_ms: int, page_work: bool = True) -> None:
    """Wait until no request is in flight for `settle_ms`; with `page_work`, also while the page
    holds a request back (`_page_busy`). A typed value's snapshot is the page when the person
    moved on, not a settled page: what the page does a moment after typing is not waited for."""
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:  # noqa: BLE001 - a page that never goes idle is still compared
        pass
    if not hasattr(page, "_tl_inflight"):
        page.wait_for_timeout(settle_ms)
        return
    quiet = waited = moving = 0
    while quiet < settle_ms and waited < SETTLE_MAX_MS:
        page.wait_for_timeout(50)
        waited += 50
        idle = page._tl_inflight == 0 and not (page_work and _page_busy(page))
        if idle and page_work and moving < ANIMATION_MAX_MS and _animating(page):
            # a dialog fading out, a panel sliding in: the recorder's snapshot shows the end
            moving += 50
            idle = False
        quiet = quiet + 50 if idle else 0


# How long a replay waits for CSS transitions and animations to end, at most, per step: a page
# whose transitions never stop (a carousel sliding every few seconds) is compared after it.
ANIMATION_MAX_MS = 3000
# Read-only: a CSS transition or animation (or a Web Animation) running now that ends by itself.
# One that repeats forever (a spinner) is never waited for.
ANIMATING_JS = """() => {
  try {
    return document.getAnimations().some(a => {
      if (a.playState !== "running" || !a.effect) return false;
      const end = a.effect.getComputedTiming().endTime;
      return Number.isFinite(end) && end > 0;
    });
  } catch (e) { return false; }
}"""


def _animating(page) -> bool:
    try:
        return bool(page.evaluate(ANIMATING_JS))
    except Exception:  # noqa: BLE001 - a page that is navigating is not animating in this sense
        return False


def recorder_quiet_ms(rec: Recording) -> int | None:
    """How long the recorder waited for a quiet page before each snapshot (`recording.json`,
    settle.quiet_ms), or None when the recording does not say."""
    import json

    path = Path(rec.path) / "recording.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        quiet = int((meta.get("settle") or {}).get("quiet_ms"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return quiet if quiet > 0 else None


def _page_busy(page) -> bool:
    """PrimeFaces holds a request back (a filter's delay, its queue) or jQuery animates: no
    request is in flight yet, but the page is not done (twinlab2.replay.extracts.BUSY_JS, read-only)."""
    from twinlab2.replay import extracts as jsfviews

    try:
        return bool(page.evaluate(jsfviews.BUSY_JS))
    except Exception:  # noqa: BLE001 - a page that is navigating is not busy in this sense
        return False


class MissingValue(Exception):  # noqa: N818 - reported as BLOCKED
    pass


class Unsupported(Exception):  # noqa: N818 - reported as BLOCKED
    pass


def _bindings(rec: Recording, given, values: dict[str, str] | None) -> bindings_mod.Bindings:
    """The bindings file (given, or beside the recording), with older per-selector values for
    the steps it does not bind."""
    if isinstance(given, bindings_mod.Bindings):
        bnd = given
    elif given is not None:
        bnd = bindings_mod.load(Path(given))
    else:
        bnd = bindings_mod.load(rec.path / bindings_mod.FILE)
    return bnd.merged(bindings_mod.from_values(rec, values))


def _values(rec: Recording, bnd: bindings_mod.Bindings) -> dict[int, str]:
    """The value each step types or selects: bound, or held by the recording itself."""
    out: dict[int, str] = {}
    for step in rec.steps:
        if step.action not in bindings_mod.VALUED and not (
            step.action == "dialog" and bindings_mod.needs_value(step)
        ):
            continue
        value = bnd.values.get(step.seq)
        if value is None:
            value = bindings_mod.recorded_value(rec, step)
        if value is not None:
            out[step.seq] = value
    return out


def _declared(mod, given, rec: Recording):
    """(the declared object, its file) for an acceptance, baseline or visual declaration: given
    as an object, a file, False (none), or None (the file beside the recording, if any)."""
    if given is False:
        return None, None
    if given is None:
        path = rec.path / mod.FILE
        return (mod.load(path), path) if path.exists() else (None, None)
    if isinstance(given, (str, Path)):
        path = Path(given)
        if not path.exists():
            raise FileNotFoundError(f"{path}: no such file")
        return mod.load(path), path
    return given, getattr(given, "path", None)


def _viewport(viewport, vspec) -> tuple[dict, str]:
    from twinlab2.replay.visual import VIEWPORTS, viewport_size

    if isinstance(viewport, str):
        size = viewport_size(viewport)
        if size is None:
            raise ValueError(f"viewport {viewport!r}: one of {', '.join(VIEWPORTS)}, or WIDTHxHEIGHT")
        return size, viewport
    if isinstance(viewport, dict):
        name = next((k for k, v in VIEWPORTS.items() if v == viewport), "custom")
        return dict(viewport), name
    if vspec is not None:
        return vspec.size(), vspec.viewport
    return {"width": 1280, "height": 900}, "replay-default"


def _no_cookies(state, notes: dict):
    """The given browser data without its cookies: a replay sends only the cookies its declared
    baseline makes up, never one taken from the source (R06)."""
    if state is None:
        return None
    if isinstance(state, (str, Path)):
        import json

        state = json.loads(Path(state).read_text(encoding="utf-8"))
    state = dict(state)
    dropped = state.get("cookies") or []
    if dropped:
        notes["cookies_not_sent"] = len(dropped)
    state["cookies"] = []
    return state


REPLICAS = "replicas.yaml"


class OriginMap:
    """Where each source origin's steps and requests go in the replay.

    Given explicitly (`origins`: {source: replica}, or the CLI's `--map source=replica`), or by a
    `replicas.yaml` beside the recording (`apps: [{id, source, replica}]`; ids name the
    applications in the report). With one `base_url` (a single-application recording) the
    recording's first origin goes there. With none of these, every origin stays as recorded.

    Strict (an explicit map or replicas.yaml): a request to a source origin, or to an origin with
    no replica, is refused and reported; an origin the declared scope excludes is refused and
    counted as excluded. With a base URL only, requests to the source are refused (as before).

    A source with a path (`http://host/billing=http://127.0.0.1:9002`) maps only the addresses
    under that path prefix (whole segments; the longest prefix wins): two applications that share
    one host, told apart by path, each go to their own replica. A replica without a path keeps
    the address's whole path (the replica serves its prefix itself, as on the shared host); a
    replica with a path takes the prefix's place (`http://h/demo` for `/billing/x` is `/demo/x`)."""

    def __init__(self, mapping: dict[str, str], names: dict[str, str] | None = None,
                 strict: bool = False, as_recorded: bool = False) -> None:  # fmt: skip
        from twinlab2.replay.extracts import normalise_origin

        self.mapping: dict[str, str] = {}
        self.prefixes: list[tuple[str, str, str]] = []  # (source origin, path prefix, replica URL)
        for src, dst in mapping.items():
            so, ro = normalise_origin(src), normalise_origin(dst)
            if not so or not ro:
                continue
            path = _prefix_of(src)
            if path:
                self.prefixes.append((so, path, ro + _prefix_of(dst)))
            else:
                self.mapping[so] = ro
        self.prefixes.sort(key=lambda p: -len(p[1]))
        self.names = {}
        for src, n in (names or {}).items():
            path = _prefix_of(src)
            self.names[normalise_origin(src) + path] = n
        self.strict, self.as_recorded = strict, as_recorded
        self.excluded: set[str] = set()

    def replica(self, origin: str) -> str | None:
        if self.as_recorded:
            return origin
        if origin in self.mapping:
            return self.mapping[origin]
        return next((normalise_origin_of(r) for o, _p, r in self.prefixes if o == origin), None)

    def _prefix(self, url: str) -> tuple[str, str] | None:
        """(path prefix, replica URL) of the longest prefix that holds the address."""
        from twinlab2.replay.extracts import normalise_origin

        origin, path = normalise_origin(url), urlsplit(url).path or "/"
        for o, prefix, replica in self.prefixes:
            if o == origin and (path == prefix or path.startswith(prefix + "/")):
                return prefix, replica
        return None

    def rebase(self, url: str | None) -> str | None:
        """The step's address on its replica; None when its origin has no replica."""
        from twinlab2.replay.extracts import normalise_origin

        u = urlsplit(url or "")
        if u.scheme not in ("http", "https"):  # about:blank and the like: no application's
            return url
        path = u.path
        hit = self._prefix(url or "") if not self.as_recorded else None
        if hit is not None:
            prefix, target = hit
            b = urlsplit(target)
            if b.path.rstrip("/"):
                path = b.path.rstrip("/") + u.path[len(prefix):]
        else:
            origin = normalise_origin(url)
            # an origin whose prefixes do not hold this address: only its own (root) replica
            target = origin if self.as_recorded else self.mapping.get(origin)
            if target is None:
                return None
            b = urlsplit(target)
        return _keep_empty_fragment(url or "",
                                    urlunsplit((b.scheme, b.netloc, path, u.query, u.fragment)))  # fmt: skip

    def name(self, origin: str | None) -> str | None:
        return self.names.get(origin or "") or origin or None

    def name_of(self, url: str | None) -> str | None:
        """The application a step's address belongs to: its prefix's id, else its origin's."""
        from twinlab2.replay.extracts import normalise_origin

        hit = self._prefix(url or "") if url else None
        if hit is not None:
            key = normalise_origin(url) + hit[0]
            return self.names.get(key) or key
        return self.name(normalise_origin(url or "")) if url and normalise_origin(url) else None

    def allowed(self) -> set[str]:
        return set(self.mapping.values()) | {normalise_origin_of(r) for _o, _p, r in self.prefixes}

    def sources(self) -> set[str]:
        return set(self.mapping) | {o for o, _p, _r in self.prefixes}

    def described(self) -> dict[str, str]:
        """source -> replica as the replay used them (prefixes as origin + path)."""
        return {**self.mapping, **{o + p: r for o, p, r in self.prefixes}}


def _prefix_of(url: str) -> str:
    """The path of a source or replica address without its trailing slash ('' for none or /)."""
    import re

    return re.sub(r"/{2,}", "/", urlsplit(str(url)).path or "").rstrip("/")


def normalise_origin_of(url: str) -> str:
    from twinlab2.replay.extracts import normalise_origin

    return normalise_origin(url)


def origin_map(rec: Recording, base_url: str | None = None, origins=None,
               replicas=None) -> OriginMap:  # fmt: skip
    """The OriginMap for a replay (see OriginMap). `origins` is a dict or a list of
    `source=replica`; `replicas` a replicas.yaml (default: beside the recording; False: none)."""
    from twinlab2.replay.extracts import normalise_origin

    if isinstance(origins, (list, tuple)):
        pairs = {}
        for text in origins:
            src, sep, dst = str(text).partition("=")
            if not sep or not normalise_origin(src) or not normalise_origin(dst):
                raise ValueError(f"--map {text!r}: give source=replica, both like http://host:port")
            pairs[src] = dst
        origins = pairs
    names: dict[str, str] = {}
    from_file: dict[str, str] = {}
    path = Path(replicas) if replicas not in (None, False) else rec.path / REPLICAS
    if replicas is not False and path.is_file():
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for app in data.get("apps") or []:
            if isinstance(app, dict) and app.get("source"):
                if app.get("id"):
                    names[str(app["source"])] = str(app["id"])
                if app.get("replica"):
                    from_file[str(app["source"])] = str(app["replica"])
        for src, dst in (data.get("origins") or {}).items():
            from_file[str(src)] = str(dst)
    if origins:
        return OriginMap(dict(origins), names, strict=True)
    if from_file:
        return OriginMap(from_file, names, strict=True)
    first = next((normalise_origin(s.url) for s in rec.steps
                  if normalise_origin(s.url or "")), None)  # fmt: skip
    if base_url and first:
        return OriginMap({first: base_url}, names)
    return OriginMap({}, names, as_recorded=True)


def _origin(url: str | None) -> str:
    from twinlab2.replay.extracts import normalise_origin

    return normalise_origin(url or "")


def _observed_at_navigation(rec: Recording) -> dict[int, int]:
    """Steps whose after-state is the page a new document load they caused shows: step ->
    that load. Such a step (a click, key press or submit that loaded another page) is compared
    at that load, never by a snapshot taken before it (a page slower than the recorder's settle
    time left one; a newer recorder says `after: navigation` and keeps none)."""
    out: dict[int, int] = {}
    order = [s.seq for s in rec.steps]
    for g in rec.steps:
        if g.action != "goto" or g.caused_by is None or g.nav not in (None, "navigate"):
            continue
        for s in rec.steps:
            if (
                g.caused_by <= s.seq < g.seq
                and s.action != "goto"
                and (s.tab == g.tab)
                and not s.action.startswith("tab-")
            ):
                out[s.seq] = g.seq
    for s in rec.steps:
        if s.after == "navigation" and s.seq not in out:
            nxt = next((x for x in rec.steps if x.seq > s.seq and x.action == "goto"), None)
            out[s.seq] = nxt.seq if nxt else s.seq
    del order
    return out


class Unmapped(Exception):  # noqa: N818 - reported as BLOCKED
    pass


UNSUPPORTED_ACTIONS = {
    "drag": "drag and drop is not replayed by DOM replay (unsupported surface): no element state "
            "describes the gesture; check the result with an acceptance case after a step the "
            "replay can perform, or exclude this flow from the declared scope",
}  # fmt: skip


def same_message(recorded: str, shown: str) -> bool:
    """Whether a dialog's message says what the recorded one said: the same words, where each
    masked value of the recording (`Order 9999 saved`: a shape of x, X and 9) stands for any one
    word of the replica (its own synthetic value)."""
    from twinlab2_contract.evidence import sanitize

    want, got = recorded.split(), sanitize.mask_message(shown).split()
    if len(want) != len(got):
        return False
    for w, g in zip(want, got, strict=True):
        if w.lower() == g.lower():
            continue
        if re.fullmatch(r"[\W_]*[Xx9][Xx9\W_]*", w) and re.search(r"[Xx9]", w):
            continue
        return False
    return True


class _Run:
    """One replay: the browser context, its tabs, and how each step is performed."""

    def __init__(self, context, rec: Recording, omap: OriginMap, values: dict[int, str],
                 bnd: bindings_mod.Bindings, timeout_ms: int, settle_ms: int) -> None:  # fmt: skip
        self.context, self.rec, self.omap = context, rec, omap
        self.values, self.bnd, self.timeout_ms, self.settle_ms = values, bnd, timeout_ms, settle_ms
        self.tabs: dict[str, object] = {}
        self.current: str | None = None
        self.ours: set[int] = set()  # pages the replay opened itself
        self.popups: list = []  # pages the replica opened (popups), oldest first
        self.history: dict[str, tuple[list[str], int]] = {}
        self.opened_by: dict[str, int] = {}  # tab -> the step that opened it (a popup)
        self.first_goto: set[str] = set()  # tabs whose first page load is still to come
        self.downloads: list = []
        self.dialogs: list[dict] = []  # dialogs the replica showed: {kind, step}
        self.armed: dict[int, dict] = {}  # page id -> the dialog the next action expects
        self.by_seq = {s.seq: s for s in rec.steps}
        self.next_of = {a.seq: b for a, b in zip(rec.steps, rec.steps[1:], strict=False)}
        context.on("page", self._on_page)

    # --- pages ----------------------------------------------------------------------------------

    def _on_page(self, page) -> None:
        if id(page) not in self.ours:
            self.popups.append(page)
        self._watch(page)

    def _watch(self, page) -> None:
        if getattr(page, "_tl_watched", False):
            return
        page._tl_watched = True
        _track(page)
        page.on("download", lambda d: self.downloads.append(d))
        page.on("dialog", lambda d, page=page: self._on_dialog(page, d))

    def new_page(self):
        page = self.context.new_page()
        self.ours.add(id(page))
        if page in self.popups:
            self.popups.remove(page)
        self._watch(page)
        return page

    def adopt(self, tab: str | None, page) -> None:
        self.tabs[tab or "t1"] = page
        self.current = tab or "t1"

    def page_for(self, step: RecStep):
        tab = step.tab or self.current
        if tab in self.tabs:
            return self.tabs[tab]
        if len(self.tabs) == 1 and not any(s.action == "tab-open" and s.tab == tab
                                           for s in self.rec.steps):  # fmt: skip
            # the recording's first tab is the page the replay started with, whatever its name
            (only,) = self.tabs.values()
            self.tabs[tab] = only
            return only
        raise _TabMissing(f"tab {tab} is not open in the replica")

    # --- dialogs -------------------------------------------------------------------------------

    def _on_dialog(self, page, dialog) -> None:
        want = self.armed.pop(id(page), None)
        try:
            message = str(dialog.message or "")
        except Exception:  # noqa: BLE001 - a driver without the message
            message = ""
        self.dialogs.append({"kind": dialog.type, "step": want["step"] if want else None,
                             "message": message})  # fmt: skip
        try:
            if want is None or want["choice"] != "accept":
                dialog.dismiss()
            elif dialog.type == "prompt":
                dialog.accept(want.get("value") or "")
            else:
                dialog.accept()
        except Exception:  # noqa: BLE001 - a dialog the page closed itself
            pass

    def _arm(self, page, step: RecStep) -> None:
        """Before an action that showed a dialog in the source: answer it as recorded."""
        nxt = dialog_after(self.rec.steps, step)
        if nxt is None:
            return
        d = nxt.dialog or {}
        value = self.values.get(nxt.seq)
        if d.get("kind") == "prompt" and d.get("choice") == "accept" and value is None:
            raise MissingValue(f"the dialog at step {nxt.seq} is a prompt answered with typed "
                               f"text (shape {nxt.typed_shape!r}): give its value in bindings.yaml")  # fmt: skip
        self.armed[id(page)] = {"step": nxt.seq, "choice": d.get("choice", "dismiss"),
                                "value": value}  # fmt: skip

    # --- history -------------------------------------------------------------------------------

    def _visit(self, tab: str, url: str | None, nav: str | None, caused: bool) -> str | None:
        """Keep the tab's history as the source's (recorded addresses) and say how a history
        step is made: back, forward, or None (load the address)."""
        urls, idx = self.history.get(tab, ([], -1))
        how = None
        if nav in ("back_forward", "pop") and not caused:
            if idx > 0 and urls[idx - 1] == url:
                idx, how = idx - 1, "back"
            elif idx + 1 < len(urls) and urls[idx + 1] == url:
                idx, how = idx + 1, "forward"
            elif nav == "back_forward" and urls and (idx > 0) != (idx + 1 < len(urls)):
                # The entry gone to was answered with another page (after sign-out, Back to a
                # protected page is sent to sign-in). Only one direction exists, so the step is
                # made that way and the replica must arrive at the recorded address by itself;
                # loading that address would hide a replica that still shows the old page.
                idx, how = (idx - 1, "back") if idx > 0 else (idx + 1, "forward")
                urls[idx] = url
        elif nav == "replace" and urls:
            urls[idx] = url
        elif nav == "reload":
            pass
        else:
            urls = urls[: idx + 1] + [url]
            idx = len(urls) - 1
        self.history[tab] = (urls, idx)
        return how

    # --- steps ---------------------------------------------------------------------------------

    def act(self, step: RecStep) -> tuple[str | None, bool]:
        """Perform one step. (what found the target or how it was done, whether it was implied
        by an earlier step rather than performed)."""
        how, implied = self._perform(step)
        if step.opens_outside:
            how = "; ".join(x for x in (how, self._window_outside(step)) if x)
        return how, implied

    def _window_outside(self, step: RecStep) -> str:
        """The step opened another application's window in the source (a part of a split
        recording): the replica must open one too. That window is the other application's, so
        it is closed and not followed; the whole recording's replay follows it."""
        import time

        app = step.opens_outside.get("app") or "another application"
        deadline = time.monotonic() + self.timeout_ms / 1000
        while not self.popups and time.monotonic() < deadline:
            (self.tabs.get(self.current) or next(iter(self.tabs.values()))).wait_for_timeout(50)
        if not self.popups:
            raise _NoWindow(f"the source opened a window of {app} here "
                            f"({step.opens_outside.get('url') or 'address not recorded'}); the "
                            "replica opened none")  # fmt: skip
        page = self.popups.pop(0)
        try:
            page.close()
        except Exception:  # noqa: BLE001 - a window that closed itself
            pass
        return f"opened a window of {app} (another application: closed, not followed)"

    def _perform(self, step: RecStep) -> tuple[str | None, bool]:
        from twinlab2_contract.evidence.locate import Unresolved, resolve, typed_values

        if step.action == "tab-open":
            return self._tab_open(step)
        if step.action == "tab-switch":
            page = self.tabs.get(step.tab)
            if page is None or page.is_closed():
                raise _TabMissing(f"tab {step.tab} is not open in the replica")
            self.current = step.tab
            page.bring_to_front()
            return f"switched to tab {step.tab}", False
        if step.action == "tab-close":
            page = self.tabs.pop(step.tab, None)
            if page is not None and not page.is_closed():
                page.close()
            if self.current == step.tab:
                self.current = next(iter(self.tabs), None)
            return f"closed tab {step.tab}", False
        page = self.page_for(step)
        if step.action == "goto":
            return self._goto(page, step)
        if step.action == "submit" and step.caused_by is not None:
            return f"submitted by step {step.caused_by}", True
        if step.action == "dialog":
            return self._dialog_seen(step)
        if step.action == "download":
            return self._download_seen(step)
        if step.action in UNSUPPORTED_ACTIONS:
            raise Unsupported(UNSUPPORTED_ACTIONS[step.action])
        if step.shadow == "closed":
            raise Unsupported("the target is inside a closed shadow root: no selector can reach it "
                              "(unsupported surface); the snapshot shows it, the replay cannot act "
                              "on it")  # fmt: skip
        if step.surface == "canvas":
            raise Unsupported("a canvas-only interaction (unsupported surface): what it changes is "
                              "drawn, not in the DOM; it is never claimed as replayed")  # fmt: skip
        if step.action not in SUPPORTED and step.action not in ALSO_PERFORMED:
            raise Unsupported(f"this replay cannot perform {step.action!r}")
        value = None
        if step.action == "upload":
            path = self.bnd.files.get(step.seq)
            if not path or not Path(path).is_file():
                shapes = ", ".join(f.get("name_shape") or "?" for f in step.files)
                raise MissingValue(f"no synthetic file for the upload at step {step.seq} "
                                   f"({shapes}): add file: <path> to bindings.yaml")  # fmt: skip
        elif step.action in bindings_mod.VALUED:
            value = self.values.get(step.seq)
            if value is None and not (step.action == "select" and step.seq in self.bnd.indexes):
                shape = f" (recorded shape {step.typed_shape!r})" if step.typed_shape else ""
                raise MissingValue(f"no binding gives the value for step {step.seq} "
                                   f"({step.selector}){shape}: add it to bindings.yaml")  # fmt: skip
        loc, found = resolve(page, step, _record_title(step, self.values, self.bnd),
                             self.timeout_ms, typed_values(step, self.values))  # fmt: skip
        if loc is None and step.action != "keypress":
            raise Unresolved("the step names no element", [])
        self._arm(page, step)
        if step.action in ("click", "dblclick") and not loc.is_visible():
            # A control shown only while its record is under the pointer (a list item's delete
            # button): a person points at the record first; so does the replay.
            row = loc.locator("xpath=ancestor::*[self::li or self::tr or @role='row'][1]")
            if row.count() == 1:
                row.hover(timeout=10000)
        if step.action == "type":
            loc.fill(value, timeout=10000)
            if step.left:
                # The source's snapshot was taken after focus left the field: what the page does
                # then (a message for a required field, a framework marking it as visited) is in
                # it. A field removed by its own input has nothing to leave.
                try:
                    loc.blur(timeout=2000)
                except Exception:  # noqa: BLE001 - the field went away as it was filled
                    pass
        elif step.action == "select":
            if step.seq in self.bnd.indexes:
                loc.select_option(index=self.bnd.indexes[step.seq], timeout=10000)
            else:
                try:
                    loc.select_option(label=value, timeout=10000)
                except Exception:  # noqa: BLE001 - recorded as text; fall back to the option value
                    loc.select_option(value=value, timeout=10000)
        elif step.action == "upload":
            loc.set_input_files(self.bnd.files[step.seq], timeout=10000)
        elif step.action == "click":
            loc.click(timeout=10000)
        elif step.action == "dblclick":
            loc.dblclick(timeout=10000)
        elif step.action in ("check", "uncheck"):
            if loc.is_checked(timeout=10000) != (step.action == "check"):
                loc.click(timeout=10000)
        elif step.action == "keypress":
            combo = "+".join([*step.modifiers, step.key or ""])
            if not step.key:
                raise Unsupported("a key press without its key")
            if loc is None:
                page.keyboard.press(combo)
            else:
                loc.press(combo, timeout=10000)
        elif step.action == "submit":
            loc.evaluate(
                "el => (el.form || el).requestSubmit ? (el.form || el).requestSubmit() : el.click()"
            )
        return found, False

    def _goto(self, page, step: RecStep) -> tuple[str | None, bool]:
        if step.setup:
            return f"setup ({step.setup})", True
        url = self.omap.rebase(step.url)
        if url is None:
            origin = _origin(step.url)
            raise Unmapped(f"step {step.seq} is on {origin}, which has no replica: give one with "
                           f"--map {origin}=http://127.0.0.1:<port> (or in replicas.yaml beside "
                           "the recording), or declare it out of scope")  # fmt: skip
        tab = step.tab or self.current or "t1"
        first = tab in self.first_goto
        self.first_goto.discard(tab)
        caused = step.caused_by is not None or step.nav in ("push", "replace") or first
        how = self._visit(tab, step.url, step.nav, caused)
        if first:  # a popup's first page: the replica must have opened it there by itself
            _wait_address(page, url, self.timeout_ms)
            return f"the page the popup of step {self.opened_by.get(tab)} opened", True
        if caused:
            _wait_address(page, url, self.timeout_ms)
            why = f"step {step.caused_by}" if step.caused_by is not None else "the page itself"
            return f"navigation caused by {why}", True
        if step.caused_by_outside:
            o = step.caused_by_outside
            note = f" (supplied from outside: caused by step {o.get('step')} of {o.get('app')})"
        else:
            note = ""
        if how == "back":
            page.go_back()
            _wait_address(page, url, self.timeout_ms)
            return "history back", False
        if how == "forward":
            page.go_forward()
            _wait_address(page, url, self.timeout_ms)
            return "history forward", False
        if step.nav == "reload" and page.url not in ("", "about:blank"):
            page.reload()
            _wait_address(page, url, self.timeout_ms)
            return "reload", False
        page.goto(url)
        return (f"opened{note}" if note else None), False

    def _tab_open(self, step: RecStep) -> tuple[str, bool]:
        tab = step.tab or f"t{len(self.tabs) + 1}"
        cause = next((s.seq for s in reversed(self.rec.steps)
                      if s.seq < step.seq and not s.action.startswith("tab-")), None)  # fmt: skip
        if step.opener:
            import time

            deadline = time.monotonic() + self.timeout_ms / 1000
            while not self.popups and time.monotonic() < deadline:
                (self.tabs.get(self.current) or next(iter(self.tabs.values()))).wait_for_timeout(50)
            if not self.popups:
                raise _NoTab(f"the source opened tab {tab} here (from tab {step.opener}); the "
                             "replica opened no tab")  # fmt: skip
            page = self.popups.pop(0)
            self.first_goto.add(tab)
            self.opened_by[tab] = cause
            how = f"popup opened by step {cause}"
        else:
            page = self.new_page()
            how = "a new tab opened by the replay"
            if step.opened_by_outside:
                o = step.opened_by_outside
                how += (f" (supplied from outside: opened by step {o.get('step')} of "
                        f"{o.get('app')})")  # fmt: skip
        self.tabs[tab] = page
        self.current = tab
        return how, bool(step.opener)

    def _dialog_seen(self, step: RecStep) -> tuple[str, bool]:
        d = step.dialog or {}
        seen = next((x for x in self.dialogs if x["step"] == step.seq), None)
        if seen is None:
            raise _NotShown(f"the source showed a {d.get('kind', 'dialog')} here (answered "
                            f"{d.get('choice')}); the replica showed none")  # fmt: skip
        if d.get("kind") and seen["kind"] != d["kind"]:
            raise _NotShown(f"the source showed a {d['kind']}, the replica a {seen['kind']}")
        if d.get("message") and not same_message(d["message"], seen.get("message") or ""):
            from twinlab2_contract.evidence import sanitize

            raise _NotShown(f"the source's {d.get('kind', 'dialog')} said {d['message']!r}, the "
                            f"replica's {sanitize.mask_message(seen.get('message') or '')!r}")  # fmt: skip
        return f"{seen['kind']} answered {d.get('choice', 'dismiss')}", True

    def _download_seen(self, step: RecStep) -> tuple[str, bool]:
        import time

        want = step.download or {}
        deadline = time.monotonic() + self.timeout_ms / 1000
        page = self.tabs.get(self.current)
        while not self.downloads and time.monotonic() < deadline and page is not None:
            page.wait_for_timeout(50)
        if not self.downloads:
            raise _NotShown("the source downloaded a file here; the replica downloaded nothing")
        got = self.downloads.pop(0)
        name = got.suggested_filename or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if want.get("ext") and ext != want["ext"]:
            raise _NotShown(f"the source downloaded a .{want['ext']} file, the replica a .{ext}")
        try:
            got.cancel()
        except Exception:  # noqa: BLE001
            pass
        return f"a download ({domlib.shape(name)})", True


class _TabMissing(AssertionError):
    pass


class _NoWindow(AssertionError):
    """The source opened another application's window at this step; the replica opened none."""


class _NoTab(AssertionError):
    pass


class _NotShown(AssertionError):
    pass


def replay(
    recording: Recording | Path,
    base_url: str | None = None,
    values: dict[str, str] | None = None,
    headless: bool = True,
    settle_ms: int = 150,
    stop_on_fail: bool = False,
    *,
    bindings: bindings_mod.Bindings | Path | None = None,
    expected: list[str] | None = None,
    storage_state: dict | str | Path | None = None,
    setup: Callable | None = None,
    after: Callable | None = None,
    resolve_timeout_ms: int = 5000,
    acceptance=None,
    baseline=None,
    visual=None,
    artifacts: Path | None = None,
    outcome_judge: Callable | None = None,
    viewport: str | dict | None = None,
    origins: dict[str, str] | list[str] | None = None,
    replicas: Path | bool | None = None,
    scope=None,
    browser: str = "chromium",
) -> FidelityReport:
    """Replay the recording's steps with Playwright and compare each step's after-snapshot.

    The steps run in one browser context, in the recorded order, across every tab: a tab the
    source opened (a popup, target=_blank) must open in the replica, and is matched to the
    recorded tab; switches and closes are performed; Back and Forward go back and forward. Each
    step goes to its application's replica by the origin map (`origins`: {source: replica}, a
    `replicas.yaml` beside the recording, or `base_url` for a single application; see
    OriginMap). A step on an origin with no replica BLOCKS with what to add; a request to a
    source origin, or to an origin with no replica, is refused and reported.

    A click (key press, submit) that loaded another page is compared at that page, the moment the
    source's snapshot describes (`_observed_at_navigation`). Values come from `bindings` (a
    Bindings, or a bindings file; by default `bindings.yaml` beside the recording). `values`
    are the older per-selector values (`"#loanQuery"`, or `"6:#loanQuery"` for step 6 only),
    used for steps the bindings do not bind. `expected` lists the semantic actions the flow
    should have (see `action_label`): one the recording lacks fails the replay. `storage_state`
    is browser data to start from (its cookies are never sent); `setup(context)` runs before the
    first step and `after(page)` after the last.

    `acceptance`, `baseline`, `visual` and `scope` are the declarations beside the recording by
    default (`acceptance.yaml`, `baseline.yaml`, `visual.yaml`, `scope.yaml`; False for none):
    acceptance cases and visual states are evaluated where they are declared, the baseline is
    applied to the fresh context before the first step, and differences a reviewed exclusion
    covers are reported apart (never passed). `outcome_judge(automation, case)` judges
    `outcome` cases on the lab database. Pictures go to `artifacts`. `viewport` is desktop,
    mobile or a size. `browser` is chromium, firefox or webkit.
    """
    from playwright.sync_api import sync_playwright

    from twinlab2.replay import acceptance as acc_mod
    from twinlab2.replay import baseline as base_mod
    from twinlab2.replay import durable
    from twinlab2.replay import judging as judge_mod
    from twinlab2.replay import scope as scope_mod
    from twinlab2.replay import visual as vis_mod
    from twinlab2_contract.evidence.locate import Unresolved

    rec = recording if isinstance(recording, Recording) else load_recording(Path(recording))
    _infer_causes(rec)
    bnd = _bindings(rec, bindings, values)
    vals = _values(rec, bnd)
    supplied = {k for k in (domlib.key_of(_selector_of(name)) for name in values or {}) if k}
    supplied |= bindings_mod.supplied_keys(rec, bnd)
    spec, spec_path = _declared(acc_mod, acceptance, rec)
    base, base_path = _declared(base_mod, baseline, rec)
    vspec, vspec_path = _declared(vis_mod, visual, rec)
    sc, sc_path = _declared(scope_mod, scope, rec)
    omap = origin_map(rec, base_url, origins, replicas)
    if sc is not None:
        omap.excluded = sc.origins()
    if isinstance(bindings, (str, Path)):
        bnd_path = Path(bindings)
    elif bindings is None and (rec.path / bindings_mod.FILE).exists():
        bnd_path = rec.path / bindings_mod.FILE
    else:
        bnd_path = None
    size, run_viewport = _viewport(viewport, vspec)
    # After a step the replay waits as the recorder did before its snapshot: for a quiet page at
    # least as long, and until the page's CSS transitions and animations have ended (_settle). A
    # typed value's snapshot was taken when the person moved on: `settle_ms` alone.
    step_settle = max(settle_ms, recorder_quiet_ms(rec) or 0)
    reports: list[StepReport] = []
    problems: list[str] = recording_damage(rec)
    stopped = False
    notes: dict = {}
    state = _no_cookies(storage_state, notes)
    source_origin = next((o for o in (base_mod.origin_of(s.url) for s in rec.steps) if o), None)
    replica_origin = omap.replica(_origin(source_origin)) or source_origin
    if base is not None and base.problems:
        problems += [f"baseline: {p}" for p in base.problems]
        stopped = True  # nothing runs on a baseline that cannot be applied
    step_by_seq = {s.seq: s for s in rec.steps}
    deferred = _observed_at_navigation(rec)
    placed = _placed(rec, vals)

    def record_title(seq) -> str | None:
        if isinstance(seq, dict):
            return bnd.from_outside(seq)
        step = step_by_seq.get(seq)
        return _record_title(step, vals, bnd) if step is not None else None

    def binding(seq) -> str | None:
        return bnd.from_outside(seq) if isinstance(seq, dict) else vals.get(seq)

    judging = None
    if (spec and spec.cases) or (vspec and vspec.states):
        if artifacts is None:
            import tempfile

            artifacts = Path(tempfile.mkdtemp(prefix="twinlab-replay-"))
    if artifacts is not None:
        artifacts = Path(artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
    blocked_requests: list[str] = []
    excluded_requests: list[str] = []
    step_shots: dict[int, str] = {}
    running: dict = {"seq": None, "done": False}  # the step being replayed, for the guard
    browser_info = visual_setup = None
    service_worker = any(s.service_worker for s in rec.steps)
    with sync_playwright() as pw:
        engine = getattr(pw, browser)
        launched = engine.launch(headless=headless)
        browser_info = {"name": launched.browser_type.name, "version": launched.version}
        options: dict = {"viewport": size, "service_workers": "block", "accept_downloads": True}
        if vspec is not None:
            options.update(vis_mod.context_options(vspec))
            options["viewport"] = size

        holder: dict = {"run": None}

        def guard(ctx):
            """Nothing of the replay goes to a source, or anywhere the map gives no replica."""
            if omap.as_recorded:
                return
            allowed = omap.allowed()
            sources = omap.sources() - allowed
            # Another application's window, opened by a step of this part (`opens_outside`): its
            # page load is answered with nothing, so the replay never reaches that application.
            outside = {_origin(s.opens_outside.get("url") or "") for s in rec.steps
                       if s.opens_outside} - allowed - {""}  # fmt: skip

            def route(r):
                url = r.request.url
                o = _origin(url)
                if o in outside and holder["run"] is not None:
                    try:
                        own = r.request.frame.page in holder["run"].tabs.values()
                    except Exception:  # noqa: BLE001 - a request with no frame yet
                        own = True
                    if not own:  # the window the replica opened, not a tab of the replay
                        r.fulfill(status=200, content_type="text/html", body="")
                        return
                if not o or o in allowed or (not omap.strict and o not in sources):
                    r.continue_()
                    return
                if o in omap.excluded:  # declared out of scope: answered with nothing, counted
                    excluded_requests.append(r.request.method + " " + url[:200])
                    r.fulfill(status=200, content_type="text/html", body="")
                    return
                else:
                    why = "a source origin" if o in omap.sources() else "an origin with no replica"
                    # which step was running: a refusal fails a replay whose steps all passed,
                    # so the report says where it came from
                    when = (f"; during step {running['seq']}" if running["seq"] is not None
                            else "; before the first step" if not running["done"]
                            else "; after the last step")  # fmt: skip
                    blocked_requests.append(f"{r.request.method} {url[:200]} ({why}{when})")
                r.abort()

            ctx.route("**/*", route)

        def new_context():
            ctx = launched.new_context(storage_state=state, **options)
            ctx.add_init_script(domlib.SHADOW_HOOK_JS)  # closed shadow roots, as recorded
            guard(ctx)
            return ctx

        def fresh(url: str):
            ctx = new_context()
            p = ctx.new_page()
            if vspec is not None:
                vis_mod.prepare(p, vspec)
            base_mod.apply(ctx, p, base, replica_origin)
            p.goto(url)
            _settle(p, step_settle)
            return ctx, p

        try:
            context = new_context()
            if setup is not None:
                setup(context)
            run = _Run(context, rec, omap, vals, bnd, resolve_timeout_ms, settle_ms)
            holder["run"] = run
            page = run.new_page()
            first_tab = next((s.tab for s in rec.steps if s.tab), None)
            run.adopt(first_tab, page)
            if vspec is not None:
                vis_mod.prepare(page, vspec)
            if spec is not None or vspec is not None or base is not None:
                judging = judge_mod.Judging(
                    rec, spec, vspec, binding=binding, record=record_title, artifacts=artifacts,
                    outcome_judge=outcome_judge, fresh=fresh, run_viewport=run_viewport,
                    settle=lambda p: _settle(p, step_settle))  # fmt: skip
            if not stopped and base is not None:
                try:
                    notes.update(base_mod.apply(context, page, base, replica_origin))
                except Exception as exc:  # noqa: BLE001 - reported: nothing runs without it
                    problems.append(f"baseline: could not be applied: {exc}"[:300])
                    stopped = True
            reached: set[int] = set()
            stopped_at = None
            for step in rec.steps:
                report = StepReport(seq=step.seq, action=step.action, selector=step.selector,
                                    url=step.url, status="pass",
                                    app=omap.name_of(step.url))  # fmt: skip
                reports.append(report)
                running["seq"] = step.seq
                if stopped:
                    report.status, report.outcome = "skipped", "not_run"
                    report.note = "not run: the replay stopped at an earlier step"
                    if stopped_at is None:
                        stopped_at = step.seq
                    continue
                if step.setup:
                    report.outcome, report.note = (
                        "setup",
                        f"setup event ({step.setup}): kept, not run",
                    )
                    continue
                expected_html = rec.dom(step)
                where = step.selector or step.url or "?"
                current = run.tabs.get(run.current)
                try:
                    report.target, implied = run.act(step)
                except (Unresolved, MissingValue, Unsupported, Unmapped) as exc:
                    report.status = "blocked"
                    if isinstance(exc, Unresolved):
                        report.outcome = "missing_value" if exc.missing_binding else "unresolved"
                        check = "target"
                    elif isinstance(exc, MissingValue):
                        report.outcome, check = "missing_value", "value"
                    elif isinstance(exc, Unmapped):
                        report.outcome, check = "unmapped", "url"
                    else:
                        report.outcome, check = "unsupported", "unsupported"
                    report.note = str(exc)
                    report.differences.append(Difference(step=step.seq, element=where, check=check,
                                                         expected=step.action,
                                                         actual=str(exc)[:500]))  # fmt: skip
                    problems.append(f"step {step.seq} ({step.action}) BLOCKED: {exc}")
                    stopped = True
                    _shoot(run.tabs.get(run.current) or current, artifacts, step.seq, step_shots)
                    continue
                except Exception as exc:  # noqa: BLE001 - reported as the step's failure
                    if _closed_its_own_window(exc, step, run, rec.steps):
                        # the action closed its window, as the recording's next step in that
                        # tab says: the driver can report it before the click returns
                        report.outcome = "replayed"
                        reached.add(step.seq)
                        continue
                    report.status, report.outcome = "fail", "failed"
                    check = "url" if isinstance(exc, _WrongAddress) else "action"
                    report.differences.append(
                        Difference(step=step.seq, element=where, check=check,
                                   expected=step.action,
                                   actual=f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
                    )  # fmt: skip
                    _shoot(run.tabs.get(run.current) or current, artifacts, step.seq, step_shots)
                    if stop_on_fail or isinstance(exc, (_NoTab, _TabMissing)):
                        stopped = True
                    continue
                report.outcome = "implied" if implied else "replayed"
                page = run.tabs.get(step.tab) or run.tabs.get(run.current)
                if step.action == "tab-close" or page is None or page.is_closed():
                    reached.add(step.seq)
                    continue
                typed = step.action == "type"
                _settle(page, settle_ms if typed else step_settle, page_work=not typed)
                reached.add(step.seq)
                report.replica_origin = _origin(page.url) or None
                if step.seq in deferred:
                    report.note = (f"observed at the page it loaded (step {deferred[step.seq]}), "
                                   "the moment the source's snapshot describes")  # fmt: skip
                elif expected_html is None:
                    report.note, lost = uncompared(step)
                    if lost:
                        problems.append(lost)
                else:
                    report.compared = True
                    live = domlib.page_html(page)
                    if placed is not None:  # ids that were a typed value: as recorded
                        live = placed.redact_html(live)
                    label_missing = False
                    if twin_label_required():  # the twin's own label: required, never compared
                        label_missing = not package.html_has_label(live)
                        live = package.strip_label_html(live)
                    diffs = compare_dom(expected_html, live, step.seq, supplied)
                    if label_missing:
                        diffs.append(Difference(step=step.seq, element="(AI generated Twin App label)",
                                                check="twin_label", expected=package.TWIN_LABEL, actual=None))
                    # an element exclusion covers the element and what is inside it
                    names = scope_mod.resolve(sc, expected_html, live) if diffs else {}
                    for d in diffs:
                        ex = scope_mod.which(sc, d.element, step.url, names)
                        if ex is None:
                            report.differences.append(d)
                        else:
                            report.excluded.append(d)
                            report.excluded_by.append(ex.id)
                    if report.differences:  # ran, and the page differs
                        report.status = "fail"
                        _shoot(page, artifacts, step.seq, step_shots)
                        if stop_on_fail:
                            stopped = True
                if judging is not None:
                    judging.at(page, step.seq, step.seq)
            running["seq"], running["done"] = None, True
            page = run.tabs.get(run.current) or next(iter(run.tabs.values()), None)
            if after is not None and page is not None:
                after(page)
            if judging is not None:
                if not stopped and page is not None:
                    judging.at(page, "end", rec.steps[-1].seq if rec.steps else None)
                judging.finish(reached, stopped_at)
            if vspec is not None and page is not None:
                visual_setup = vis_mod.setup_report(launched, page, vspec)
        finally:
            launched.close()
    report = _result(rec, base_url, reports, problems, expected)
    used = {"bindings": bnd_path, "acceptance": spec_path, "baseline": base_path,
            "visual": vspec_path, "scope": sc_path}  # fmt: skip
    report.fingerprint = durable.for_run(
        rec, {k: v for k, v in used.items() if k != "scope"}, browser_info, size
    )
    if sc_path is not None:  # the reviewed exclusions this run was compared under
        report.fingerprint["scope"] = {
            "path": str(Path(sc_path).resolve()),
            "digest": durable.file_digest(sc_path),
        }
    report.visual_setup = visual_setup
    report.artifacts = str(artifacts) if artifacts is not None else None
    report.origins = omap.described() if not omap.as_recorded else {}
    report.blocked_requests = blocked_requests[:200]
    report.baseline = {**(base.summary() if base is not None else {"file": None, "empty": True}),
                       **notes, "source_requests_blocked": len(blocked_requests)}  # fmt: skip
    if blocked_requests:
        where = sorted({_origin(r.rsplit(" (", 1)[0].split(" ", 1)[1]) for r in blocked_requests})
        report.problems.append(
            f"{len(blocked_requests)} request(s) to the source or to an origin with no replica "
            f"were refused ({', '.join(where)}): map each one (--map) or declare it out of scope"
        )
        if omap.strict and report.status == "pass":
            report.status = "fail"  # the replica depends on something outside the lab
    if sc is not None:
        report.excluded = _excluded(sc, reports, excluded_requests)
    _scope(report, judging, spec, rec, step_shots)
    if service_worker and report.requirements is not None:
        report.requirements.unsupported.append(
            "service worker behaviour (offline cache, push, background sync): the page registered "
            "one; the replay blocks service workers, so it is not compared")  # fmt: skip
    for r in reports:
        if r.outcome == "unsupported" and report.requirements is not None:
            line = f"step {r.seq}: {r.action} ({r.note})"
            if line not in report.requirements.unsupported:
                report.requirements.unsupported.append(line)
    return report


def _excluded(sc, reports: list[StepReport], requests: list[str]) -> Excluded:
    by_id: dict[str, int] = {}
    for r in reports:
        for ex_id in r.excluded_by:
            by_id[ex_id] = by_id.get(ex_id, 0) + 1
    for req in requests:
        o = _origin(req.split(" ", 1)[-1])
        ex = next((e for e in sc.usable if e.origin and _origin(e.origin) == o), None)
        if ex is not None:
            by_id[ex.id] = by_id.get(ex.id, 0) + 1
    return Excluded(declaration=sc.name(), approved_by=sc.approved_by,
                    differences=sum(len(r.excluded) for r in reports),
                    steps=sum(1 for r in reports if r.excluded and not r.differences),
                    requests=len(requests), by_id=by_id,
                    reasons={e.id: e.reason for e in sc.usable}, problems=list(sc.problems))  # fmt: skip


def _shoot(page, artifacts: Path | None, seq: int, shots: dict[int, str]) -> None:
    """The replica at the first step that diverged."""
    if artifacts is None or shots or page is None:
        return
    try:
        path = Path(artifacts) / f"step-{seq:04d}.after.png"
        with hidden_label(page):
            page.screenshot(path=str(path), animations="disabled", caret="hide")
        shots[seq] = str(path)
    except Exception:  # noqa: BLE001 - a missing picture never loses the result
        pass


def _scope(report: FidelityReport, judging, spec, rec: Recording, step_shots) -> None:
    """The scoped result beside the replay's: categories, overall status, requirements, first
    divergence. A failed or blocked required case also makes the replay's own status fail or
    blocked, so current readers of `status` see it."""
    from twinlab2.replay import judging as judge_mod

    cases = list(judging.cases.values()) if judging else []
    visual = list(judging.visual.values()) if judging else []
    report.cases, report.visual = cases, visual
    required = [r for r in [*cases, *visual] if r.required]
    failed = [r.id for r in required if r.status == "FAIL"]
    blocked = [r.id for r in required if r.status == "BLOCKED"]
    if failed:
        report.problems.append("required acceptance case(s) failed: " + ", ".join(failed[:10]))
        if report.status == "pass":
            report.status = "fail"
    if blocked:
        report.problems.append("required acceptance case(s) blocked: " + ", ".join(blocked[:10]))
        if report.status == "pass":
            report.status = "blocked"
    report.categories = judge_mod.categories(report.coverage, report.status, cases, visual)
    # the replay category is the structure alone: not the cases folded into `status`
    structural = _structural(report.steps, report.coverage)
    if structural == "pass" and report.blocked_requests and report.origins:
        structural = "fail"
    rep = report.categories["replay"]
    rep.status = {"pass": "PASS", "fail": "FAIL"}.get(structural, "BLOCKED")
    x = report.excluded
    if x is not None and x.steps:
        # passed only within the declared scope: not counted as passed
        rep.required_passed = max(0, rep.required_passed - x.steps)
        rep.required_total = max(0, rep.required_total - x.steps)
        rep.note = (f"{x.steps} step(s) differ only by {x.differences} difference(s) excluded "
                    f"under {x.declaration}: not counted as passed")  # fmt: skip
    report.verdict, report.verdict_reasons = judge_mod.scoped(report.categories, cases, visual)
    if report.verdict == judge_mod.SCOPED_PASS and report.status != "pass":
        # one report never says both: the replay's own status is part of the result
        report.verdict = (
            judge_mod.SCOPED_FAIL if report.status == "fail" else judge_mod.SCOPED_BLOCKED
        )
        report.verdict_reasons.append(f"the replay's status is {report.status}")
    report.assurance, report.assurance_reasons = judge_mod.assurance(
        report.verdict, report.categories, cases, visual
    )
    if x is not None and (x.differences or x.requests):
        report.verdict_reasons.append(
            f"{x.differences} difference(s) and {x.requests} request(s) excluded under "
            f"{x.declaration} (approved by {x.approved_by}); "
            + ", ".join(f"{k}: {n}" for k, n in x.by_id.items()))  # fmt: skip
    report.requirements = judge_mod.requirements(spec, cases, visual, report.steps,
                                                 report.coverage)  # fmt: skip
    if x is not None:
        report.requirements.excluded += [f"{k} ({x.reasons.get(k, '')}): {n} excluded"
                                         for k, n in x.by_id.items()]  # fmt: skip
    report.first_divergence = judge_mod.first_divergence(judging, report.steps, rec, step_shots)


def _structural(steps: list[StepReport], cov: Coverage | None) -> str:
    """The replay's structural result: BLOCKED when a step could not be run as recorded (or
    nothing ran), FAIL when a step failed or was not run, else PASS."""
    if cov is not None and cov.replayed == 0:
        return "blocked"
    if any(s.status == "blocked" for s in steps):
        return "blocked"
    if any(s.status == "fail" for s in steps) or (cov is not None and (cov.skipped or cov.failed)):
        return "fail"
    return "pass"


def _result(rec, base_url, reports, problems, expected) -> FidelityReport:
    count = Counter(r.outcome for r in reports)
    captured = captured_actions(rec)
    run = [r for r in reports if r.outcome != "setup"]
    cov = Coverage(
        setup=count["setup"],
        steps=len(run),
        replayed=count["replayed"] + count["implied"],
        implied=count["implied"],
        compared=sum(1 for r in run if r.compared),
        failed=sum(1 for r in run if r.status == "fail"),
        skipped=count["not_run"],
        unresolved=count["unresolved"],
        missing_value=count["missing_value"],
        unsupported=count["unsupported"],
        unmapped=count["unmapped"],
        expected=None if expected is None else len(expected),
        captured=len(captured),
        missing_expected=missing_actions(list(expected), captured) if expected else [],
    )
    problems = list(problems)
    if cov.missing_expected:
        problems.append("expected actions not in the recording: " + ", ".join(cov.missing_expected))
    if cov.failed:
        first = next(r for r in run if r.status == "fail")
        problems.append(f"{cov.failed} step(s) failed, first step {first.seq} ({first.action})")
    if cov.replayed == 0:
        problems.append("no step was replayed: a replay of nothing never passes")
    status = _structural(run, cov)
    if status == "pass" and cov.missing_expected:
        status = "fail"
    return FidelityReport(recording=rec.id, base_url=base_url, status=status, steps=reports,
                          coverage=cov, problems=problems)  # fmt: skip


def recording_damage(rec: Recording) -> list[str]:
    """The loader's problems that change what a replay replays: lines of steps.jsonl that were
    dropped and step numbers used twice. (A snapshot that is not there is reported at its step,
    when the step ran; the HAR is not used by the replay.)"""
    return [f"recording: {p}" for p in rec.problems if p.startswith("steps.jsonl")]


def uncompared(step: RecStep) -> tuple[str, str | None]:
    """(the step's note, a problem for the report or None) for a step that ran and has no
    after-snapshot to compare with. A step that names a snapshot the recording does not have
    (the recorder stopped before writing it) is a problem; a step that names none is not."""
    if step.dom:
        why = f"its snapshot {step.dom} is not in the recording"
        return (f"ran; not compared: {why}",
                f"step {step.seq} ({step.action}) ran but was not compared: {why}")  # fmt: skip
    return "ran; no snapshot was recorded after this step to compare", None


def _recorded_value(rec: Recording, step) -> str | None:
    return bindings_mod.recorded_value(rec, step)


def _selector_of(name: str) -> str:
    """The selector of a `values` key: `"6:#loanQuery"` -> `"#loanQuery"`."""
    return bindings_mod.selector_of(name)


def _value_for(rec: Recording, step, values: dict[str, str]) -> str | None:
    """The older per-selector lookup: step-specific, then for the field, then as recorded."""
    for key in (f"{step.seq}:{step.selector}", step.selector):
        if key in values:
            return values[key]
    if getattr(step, "value", None) is not None:
        return step.value
    html = rec.dom(step)
    key = domlib.key_of(step.selector)
    if html is None or key is None:
        return None
    el = domlib.snapshot(html).elements.get(key)
    if el is None:
        return None
    return el.selected if el.tag == "select" else el.value


def _infer_causes(rec: Recording) -> None:
    """An older recording has no `caused_by`. Its recorder kept a click and the submit (or
    navigation) it caused in one batch, the snapshot after the last of them only: such a submit
    or navigation is implied by the click, as a newer recording says, not performed again."""
    for prev, step in zip(rec.steps, rec.steps[1:], strict=False):
        # A recorder between the two generations named the click that caused a submit but not
        # the page load the submit caused, and kept the form as the submit's snapshot. A load
        # within half a second of a submit, in the same tab, is that submit's result.
        if (
            step.action == "goto"
            and step.caused_by is None
            and step.nav in (None, "navigate")
            and prev.action == "submit"
            and prev.tab == step.tab
            and prev.ts
            and step.ts
            and 0 <= (step.ts - prev.ts).total_seconds() < 0.5
        ):
            step.caused_by = prev.caused_by or prev.seq
    if bindings_mod.new_format(rec):
        return
    for prev, step in zip(rec.steps, rec.steps[1:], strict=False):
        if step.caused_by is not None or prev.dom is not None or not (prev.ts and step.ts):
            continue
        gap = (step.ts - prev.ts).total_seconds()
        if step.action == "submit" and prev.action == "click" and 0 <= gap < 0.15:
            step.caused_by = prev.caused_by or prev.seq
        elif step.action == "goto" and prev.action in ("click", "submit") and 0 <= gap < 1.5:
            step.caused_by = prev.caused_by or prev.seq


class _WrongAddress(AssertionError):
    pass


_SESSION_IN_PATH = re.compile(r"(;jsessionid=)[^;/?#]*", re.I)


def same_address(url: str) -> str:
    """An address as two sessions can share it. A Java server writes the session's id into the
    path (`/login.xhtml;jsessionid=node01ab...`) until it knows the browser keeps cookies; the id
    is new in every session, so that it is there is compared, never its value.

    An empty fragment is no fragment: a link `href="#"` leaves the address ending in `#`, which
    names no part of the page (and an older recorder wrote the address without it). A fragment
    that is not empty (`#/orders`, `#section`) is part of the address and still compared."""
    url = _SESSION_IN_PATH.sub(r"\1*", url)
    return url[:-1] if url.endswith("#") else url


def _keep_empty_fragment(original: str, rebuilt: str) -> str:
    """`urlunsplit` drops an empty fragment (`/#` becomes `/`): put it back, so a replayed
    address is the recorded one."""
    if original.endswith("#") and not rebuilt.endswith("#") and "#" not in rebuilt:
        return rebuilt + "#"
    return rebuilt


def _wait_address(page, url: str, timeout_ms: int = 5000) -> None:
    """A navigation the step before caused (or the app made by itself) is not made again: the
    replica must arrive at the same address on its own."""
    import time

    deadline = time.monotonic() + max(timeout_ms, 5000) / 1000
    while same_address(page.url) != same_address(url):
        if time.monotonic() >= deadline:
            raise _WrongAddress(f"expected {url}, the page is at {page.url}")
        page.wait_for_timeout(50)


def dialog_after(steps: list[RecStep], step: RecStep) -> RecStep | None:
    """The dialog a step caused: the first `dialog` step after it before the person acted again.
    Between them may be steps the action caused (a hash navigation, `caused_by` the step); the
    recorder often cannot tie the dialog itself to its action (`caused_by` None)."""
    later = [s for s in steps if s.seq > step.seq]
    chain = {step.seq}
    for s in later:
        if s.action == "dialog":
            return s if s.caused_by is None or s.caused_by in chain else None
        if s.caused_by is not None and s.caused_by in chain:
            chain.add(s.seq)
            continue
        return None
    return None


def _placed(rec: Recording, values: dict[int, str]):
    """The recorder's rule for typed values (twinlab2_contract.evidence.typedvalues), with the values this replay
    types, when the recording has an id or name that was a typed value (its placeholder): the
    live page is compared with those placeholders in place, as the recording has them."""
    from twinlab2_contract.evidence.locate import typed_values
    from twinlab2_contract.evidence.typedvalues import TypedValues

    typed: dict[str, str] = {}
    for step in rec.steps:
        typed.update(typed_values(step, values))
    return TypedValues.for_replay(typed) if typed else None


def _record_title(step: RecStep, values: dict[int, str], bnd: bindings_mod.Bindings) -> str | None:
    """The title of the record a step acts on: named in the bindings, the value bound to the
    step that typed it, or (in a split part) the value of another application's step, supplied
    from outside."""
    from twinlab2_contract.evidence.locate import record_of

    if step.seq in bnd.records:
        return bnd.records[step.seq]
    cand = record_of(step)
    if cand is not None and cand.get("text_from") is not None:
        return values.get(int(cand["text_from"]))
    if cand is not None and isinstance(cand.get("text_from_outside"), dict):
        return bnd.from_outside(cand["text_from_outside"])
    return None


def _closed_its_own_window(exc: Exception, step: RecStep, run, steps: list[RecStep]) -> bool:
    """True when an action failed only because its own window closed under it (a lookup
    window whose button calls `window.close()`), the window is closed now, and the recording's
    next step in that tab is its `tab-close`."""
    if type(exc).__name__ != "TargetClosedError" or not step.tab:
        return False
    page = run.tabs.get(step.tab)
    if page is not None and not page.is_closed():
        return False
    later = [s for s in steps if s.seq > step.seq and s.tab == step.tab]
    return bool(later) and later[0].action == "tab-close"


def _act(page, step: RecStep, base_url: str | None, values: dict[int, str],
         bnd: bindings_mod.Bindings, timeout_ms: int = 5000) -> tuple[str | None, bool]:  # fmt: skip
    """One step on one page (kept for callers of the older single-page replay)."""
    rec = Recording(id="-", path=Path("."), app=None, steps=[step])
    run = _Run(page.context, rec, origin_map(rec, base_url, None, False), values, bnd,
               timeout_ms, 0)  # fmt: skip
    run.adopt(step.tab, page)
    return run.act(step)
