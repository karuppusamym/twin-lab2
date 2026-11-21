# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Judging a replay beyond structure: acceptance cases (R05), visual states (R08), and the scoped
result (the report fields of docs/replication-gaps.md).

`Judging` is driven by `fidelity.replay`: after each step it evaluates the cases and visual
states declared for that step, at the end those declared for the end. `finish` gives every case
that was not reached its result (BLOCKED when required, SKIPPED when optional) and `scoped`
computes the separate results per category and the overall status:

- capture completeness (the expected actions against the captured ones), replay (structure),
  behaviour, data and appearance (required cases passed / total in each);
- `PASS WITHIN DECLARED SCOPE` only when no required category failed or was blocked and at least
  one required case ran; `FAIL` when any required case or category failed; otherwise `BLOCKED`.
  Never PASS from zero executed cases, and no average: one failed required category decides.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from twinlab2.replay import acceptance as acc
from twinlab2.replay import visual as vis

SCOPED_PASS, SCOPED_FAIL, SCOPED_BLOCKED = "PASS WITHIN DECLARED SCOPE", "FAIL", "BLOCKED"
NOT_DECLARED = "NOT_DECLARED"
CATEGORY_NAMES = ("capture", "replay", "behaviour", "data", "appearance")
CASE_CATEGORIES = ("behaviour", "data", "appearance")
# The assurance tier beside a scoped PASS: what the PASS rests on.
VERIFIED, DECLARED_ONLY = "VERIFIED COMPLETE", "DECLARED CASES ONLY"


class Category(BaseModel):
    status: str = NOT_DECLARED  # PASS | FAIL | BLOCKED | NOT_DECLARED
    required_passed: int = 0
    required_total: int = 0
    optional_passed: int = 0
    optional_total: int = 0
    note: str | None = None


class Divergence(BaseModel):
    step: int | None = None
    app: str | None = None  # the application the step belongs to (id, else source origin)
    kind: str = "step"  # step | case | visual
    what: str | None = None  # the case or state id, or the step's action
    locator: str | None = None
    state: str | None = None  # what differs
    before: str | None = None  # the source's picture after that step
    after: str | None = None  # the replica's picture
    diff: str | None = None
    requests: list[str] = []  # the source's requests around that step (method and path pattern)


class Requirements(BaseModel):
    scope: list[str] = []
    excluded: list[str] = []
    evidence_backed: list[str] = []
    assumed: list[str] = []
    unobserved: list[str] = []
    unsupported: list[str] = []


class Judging:
    def __init__(self, rec, spec: acc.Spec | None, vspec: vis.Spec | None, *,
                 binding: Callable[[int], str | None], record: Callable[[int], str | None],
                 artifacts: Path | None = None, outcome_judge=None, fresh=None,
                 run_viewport: str = "desktop", settle: Callable | None = None) -> None:  # fmt: skip
        self.rec = rec
        self.spec, self.vspec = spec, vspec
        self.binding, self.record = binding, record
        self.artifacts = artifacts
        self.outcome_judge = outcome_judge
        self.fresh = fresh  # () -> (context, page) on the current URL, from the baseline
        self.run_viewport = run_viewport
        self.settle = settle or (lambda page: None)
        self.pictures = vis.pictures_of(rec.path)
        self.cases: dict[str, acc.CaseResult] = {}
        self.visual: dict[str, vis.VisualResult] = {}
        self.case_shot: str | None = None

    @property
    def declared(self) -> bool:
        return bool((self.spec and self.spec.cases) or (self.vspec and self.vspec.states))

    def _for(self, when) -> tuple[list[acc.Case], list[vis.State]]:
        cases = [c for c in (self.spec.cases if self.spec else []) if c.after == when]
        # the page as it is first, then after a reload, then in a fresh context
        cases.sort(key=lambda c: (c.fresh_context, c.reload))
        states = [s for s in (self.vspec.states if self.vspec else []) if s.after == when]
        return cases, states

    def at(self, page, when, seq: int | None = None) -> None:
        cases, states = self._for(when)
        for st in states:  # pictures before any case reloads the page
            out = (self.artifacts or Path(".")) / "visual"
            res = vis.check_state(page, st, self.vspec, self.rec.path,
                                  self.pictures, out, self.run_viewport)  # fmt: skip
            self.visual[_free_key(self.visual, st.id)] = res
        for case in cases:
            self.cases[_free_key(self.cases, case.id)] = self._one(page, case, seq)

    def _one(self, page, case: acc.Case, seq) -> acc.CaseResult:
        res = acc.CaseResult(id=case.id, check=case.check, category=case.category,
                             required=case.required, after=case.when, status=acc.BLOCKED,
                             step=seq, basis=case.basis)  # fmt: skip
        if case.problem:
            res.note = case.problem
            return res
        try:
            expected = acc.resolve_expected(case.expected, self.binding, self.record)
        except acc._Unbound as exc:
            res.note = str(exc)
            return res
        res.expected = acc.show(expected)
        target, ctx = page, None
        try:
            if case.fresh_context:
                if self.fresh is None:
                    res.note = "a fresh context cannot be opened here"
                    return res
                ctx, target = self.fresh(page.url)
            elif case.reload:
                page.reload()
                self.settle(page)
            res.status, res.actual, res.note = acc.evaluate(target, case, expected,
                                                            self.outcome_judge)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - one case, not the run
            res.status = acc.BLOCKED
            res.note = f"could not be evaluated: {type(exc).__name__}: {exc}"[:300]
        finally:
            if ctx is not None:
                ctx.close()
        if res.status == acc.FAIL and res.required and self.artifacts and self.case_shot is None:
            try:
                path = self.artifacts / f"case-{case.id}.after.png"
                vis.shoot(page, path)
                self.case_shot = res.screenshot = str(path)
            except Exception:  # noqa: BLE001 - a missing picture never loses the result
                pass
        return res

    def finish(self, reached: set[int], stopped_at: int | None) -> None:
        """Results for what the replay never reached."""
        for case in self.spec.cases if self.spec else []:
            if case.id in self.cases:
                continue
            seqs = {s.seq for s in self.rec.steps}
            if case.after != "end" and case.after not in seqs:
                why = "the recording has no such step"
            elif stopped_at is not None:
                why = "the replay stopped before it"
            else:
                why = "the step did not run as recorded"
            note = case.problem or f"not run: {why} ({case.when})"
            self.cases[case.id] = acc.CaseResult(
                id=case.id, check=case.check, category=case.category, required=case.required,
                after=case.when, status=acc.BLOCKED if case.required else acc.SKIPPED, note=note,
                basis=case.basis)  # fmt: skip
        for st in self.vspec.states if self.vspec else []:
            if st.id in self.visual:
                continue
            self.visual[st.id] = vis.VisualResult(
                id=st.id, after=str(st.after), viewport=st.viewport or self.vspec.viewport,
                required=st.required, status=vis.BLOCKED if st.required else vis.SKIPPED,
                mode=st.mode, max_ratio=st.max_ratio,
                note=st.problem or "not run: the replay did not reach this state")  # fmt: skip

    def picture_of(self, seq: int | None) -> str | None:
        if seq is None or seq not in self.pictures:
            return None
        return str((Path(self.rec.path) / self.pictures[seq]).resolve())


def _free_key(held: dict, key: str) -> str:
    """A result never replaces another: a second result under the same id is kept beside it."""
    if key not in held:
        return key
    n = 2
    while f"{key}#{n}" in held:
        n += 1
    return f"{key}#{n}"


# --- the scoped result --------------------------------------------------------------------------


def _from_results(results: list, label) -> Category:
    """A category from its case (or visual) results."""
    cat = Category()
    if not results:
        return cat
    req = [r for r in results if r.required]
    opt = [r for r in results if not r.required]
    cat.required_total, cat.optional_total = len(req), len(opt)
    cat.required_passed = sum(r.status == acc.PASS for r in req)
    cat.optional_passed = sum(r.status == acc.PASS for r in opt)
    failed = [label(r) for r in req if r.status == acc.FAIL]
    blocked = [label(r) for r in req if r.status == acc.BLOCKED]
    if failed:
        cat.status, cat.note = acc.FAIL, "failed: " + ", ".join(failed[:8])
    elif blocked:
        cat.status, cat.note = acc.BLOCKED, "blocked: " + ", ".join(blocked[:8])
    elif not req:
        cat.status, cat.note = NOT_DECLARED, "only optional cases"
    else:
        cat.status = acc.PASS
    return cat


def categories(coverage, status: str, cases: list, visual: list) -> dict[str, Category]:
    out: dict[str, Category] = {}
    cap = Category()
    if coverage is not None and coverage.expected is not None:
        cap.required_total = coverage.expected
        cap.required_passed = coverage.expected - len(coverage.missing_expected)
        if coverage.missing_expected:
            cap.status = acc.FAIL
            cap.note = "not captured: " + ", ".join(coverage.missing_expected[:8])
        elif coverage.captured == 0:
            cap.status, cap.note = acc.BLOCKED, "nothing was captured"
        else:
            cap.status = acc.PASS
    else:
        cap.note = "no expected action list was declared"
    out["capture"] = cap
    rep = Category(status={"pass": acc.PASS, "fail": acc.FAIL}.get(status, acc.BLOCKED))
    if coverage is not None:
        rep.required_total = coverage.steps
        rep.required_passed = max(0, coverage.replayed - coverage.failed)
    out["replay"] = rep
    # A case whose category cannot be read is still a case: it is counted under behaviour, so
    # a required one that failed or is blocked decides the result like any other.
    unknown = [c for c in cases if c.category not in CASE_CATEGORIES]
    for name in ("behaviour", "data"):
        mine = [c for c in cases if c.category == name]
        out[name] = _from_results(mine + (unknown if name == "behaviour" else []), lambda r: r.id)
    if unknown:
        said = "unknown category: " + ", ".join(f"{c.id} ({c.category!r})" for c in unknown[:8])
        beh = out["behaviour"]
        beh.note = f"{beh.note}; {said}" if beh.note else said
    appearance = [c for c in cases if c.category == "appearance"] + list(visual)
    out["appearance"] = _from_results(appearance, lambda r: r.id)
    return out


def scoped(cats: dict[str, Category], cases: list, visual: list) -> tuple[str, list[str]]:
    """The overall status and why, first reason first."""
    why: list[str] = []
    failed = [n for n in CATEGORY_NAMES if cats[n].status == acc.FAIL]
    blocked = [n for n in CATEGORY_NAMES if cats[n].status == acc.BLOCKED]
    ran = [r for r in [*cases, *visual] if r.required and r.status in (acc.PASS, acc.FAIL)]
    for n in failed:
        why.append(f"{n} FAILED" + (f" ({cats[n].note})" if cats[n].note else ""))
    for n in blocked:
        why.append(f"{n} BLOCKED" + (f" ({cats[n].note})" if cats[n].note else ""))
    if not ran:
        why.append("no required acceptance case ran: structure alone is never a scoped PASS")
    # Every required result decides, whatever category it was filed under.
    required = [r for r in [*cases, *visual] if r.required]
    lone_failed = [r.id for r in required if r.status == acc.FAIL]
    lone_blocked = [r.id for r in required if r.status == acc.BLOCKED]
    if lone_failed and not failed:
        why.append("required case(s) FAILED: " + ", ".join(lone_failed[:8]))
    if lone_blocked and not blocked:
        why.append("required case(s) BLOCKED: " + ", ".join(lone_blocked[:8]))
    if failed or lone_failed:
        return SCOPED_FAIL, why
    if blocked or lone_blocked or not ran:
        return SCOPED_BLOCKED, why
    return SCOPED_PASS, why


def assurance(verdict: str | None, cats: dict[str, Category], cases: list,
              visual: list) -> tuple[str | None, list[str]]:  # fmt: skip
    """What a scoped PASS rests on. VERIFIED COMPLETE needs, beside the PASS, an expected action
    list declared independently of the recording with nothing missing, and no required case
    that rests on an assumption. Otherwise the PASS covers the declared cases only. No tier
    without a PASS."""
    if verdict != SCOPED_PASS:
        return None, []
    why: list[str] = []
    cap = cats.get("capture")
    if cap is None or cap.status != acc.PASS or cap.required_total < 1:
        why.append("capture completeness is not verified: no expected action list was declared")
    assumed = [r.id for r in [*cases, *visual]
               if r.required and getattr(r, "basis", None) == "assumed"]  # fmt: skip
    if assumed:
        why.append("required case(s) rest on an assumption, not on evidence: "
                   + ", ".join(assumed[:8]))  # fmt: skip
    return (DECLARED_ONLY if why else VERIFIED), why


def requirements(spec: acc.Spec | None, cases: list, visual: list, steps, coverage) -> Requirements:
    req = Requirements()
    if spec is not None:
        req.scope, req.excluded = list(spec.scope), list(spec.excluded)
        req.assumed += spec.assumed
        req.unobserved += spec.unobserved
        req.unsupported += spec.unsupported
    for c in cases:
        line = f"{c.id} ({c.category}, {'required' if c.required else 'optional'}): {c.status}"
        (req.assumed if c.basis == "assumed" else req.evidence_backed).append(line)
    for v in visual:
        req.evidence_backed.append(
            f"{v.id} (appearance, {'required' if v.required else 'optional'}): {v.status}"
        )
    if coverage is not None and coverage.expected is None:
        req.assumed.append("the recording is complete (no expected action list was declared)")
    for s in steps:
        if s.outcome == "unsupported":
            req.unsupported.append(f"step {s.seq}: {s.action} ({s.note})")
    return req


def first_divergence(judging: Judging | None, steps, rec, step_shots: dict[int, str]) -> Any:
    """The earliest step or required case where the replica differs (or could not be judged)."""
    order = {s.seq: i for i, s in enumerate(rec.steps)}
    last = rec.steps[-1].seq if rec.steps else None
    found: list[tuple[int, Divergence]] = []
    for s in steps:
        if s.status in ("fail", "blocked"):
            d = s.differences[0] if s.differences else None
            # the element that differs; for an action that failed, what found its target
            element = d.element if d is not None and d.check not in ("action", "target", "value",
                                                                     "url", "unsupported") else None  # fmt: skip
            found.append((order.get(s.seq, 0), Divergence(
                step=s.seq, kind="step", what=s.action,
                locator=element or s.target or s.selector,
                state=d.text() if d else s.note, after=step_shots.get(s.seq))))  # fmt: skip
            break
    if judging is not None:
        spec_cases = {c.id: c for c in (judging.spec.cases if judging.spec else [])}
        for c in judging.cases.values():
            if c.required and c.status in (acc.FAIL, acc.BLOCKED):
                seq = c.step if c.step is not None else last
                loc = spec_cases[c.id].locator if c.id in spec_cases else None
                found.append((order.get(seq, len(order)) + 0.5, Divergence(
                    step=seq, kind="case", what=c.id,
                    locator=acc.describe_locator(loc) if loc not in (None, "") else None,
                    state=f"{c.status}: expected {c.expected!r}, saw {c.actual!r}"
                    + (f" ({c.note})" if c.note else ""), after=c.screenshot)))  # fmt: skip
        for v in judging.visual.values():
            if v.required and v.status in (vis.FAIL, vis.BLOCKED):
                seq = int(v.after) if v.after.isdigit() else last
                found.append((order.get(seq, len(order)) + 0.25, Divergence(
                    step=seq, kind="visual", what=v.id, before=v.before, after=v.after_path,
                    diff=v.diff, state=f"{v.status}: {v.differing} pixel(s) differ"
                    + (f" ({v.note})" if v.note else ""))))  # fmt: skip
    if not found:
        return None
    found.sort(key=lambda x: x[0])
    div = found[0][1]
    div.app = next((s.app for s in steps if s.seq == div.step and getattr(s, "app", None)), None)
    if div.before is None and judging is not None:
        div.before = judging.picture_of(div.step)
    div.requests = requests_near(rec, div.step)
    return div


def requests_near(rec, seq: int | None) -> list[str]:
    """The source's requests from that step's start to the next step's (method, path pattern)."""
    if seq is None:
        return []
    steps = rec.steps
    for i, s in enumerate(steps):
        if s.seq == seq:
            nxt = steps[i + 1].ts if i + 1 < len(steps) else None
            keys = [e.key() for e in rec.requests_between(s.ts, nxt) if not e.is_asset()]
            return list(dict.fromkeys(keys))[:20]
    return []
