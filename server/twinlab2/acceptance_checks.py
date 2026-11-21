"""Verify's acceptance checks: each one judged on its own and shown on its own on the release page.

A build provider finishing ("Ready for verification") says nothing about acceptance. A release
becomes Tested only through Verify, and only when none of these checks failed:

- `ci`: the commit's required GitHub checks (and Windows validation) are green;
- `commit_identity`: the deployment reports exactly the release's commit (all 40 characters) and,
  for a build session's release, that commit is the one the portal published from the session;
- `scenarios`: every business scenario of the brief replayed and passed;
- `fixture_reset`: the lab reset its data to the baseline (`POST /_lab/reset`);
- `persistence`: the data the replays saved is still there after the app restarted
  (`POST /_lab/restart`, then the lab's data again);
- `failure_scenarios`: each failure case the brief names is covered by a passing acceptance case
  whose id starts with `failure` (in acceptance.yaml beside the recording), and none failed;
- `visual_coverage`: the required picture states passed (reported-only states never decide).

Status: pass, fail, or not-checked (with why); a not-checked check does not block Tested, but the
page says what was not covered. `blocking` lists what keeps a release from Tested.
"""

from __future__ import annotations

LABELS = {
    "ci": "CI checks of the commit",
    "commit_identity": "Exact commit identity",
    "scenarios": "Business scenarios",
    "fixture_reset": "Fixture reset",
    "persistence": "Data persists across a restart",
    "failure_scenarios": "Failure scenarios",
    "visual_coverage": "Visual coverage",
}
ORDER = tuple(LABELS)


def _check(key, status, detail, required=True):
    return {"id": key, "label": LABELS[key], "status": status, "detail": detail, "required": required}


def failure_cases(manifest):
    """{scenario id: [failure case text]} named by the brief."""
    out = {}
    for number, item in enumerate(manifest.get("scenarios") or [], 1):
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("key") or f"scenario-{number}")
        cases = item.get("failure_cases") or item.get("failures") or []
        if isinstance(cases, str):
            cases = [line.strip() for line in cases.splitlines() if line.strip()]
        if cases:
            out[key] = [str(c) for c in cases]
    return out


def build(*, gate, sha, deployment, session_commit, twin, manifest, dims):
    """The checks of one verification (see the module docstring)."""
    checks = [_check("ci", "pass" if gate.get("ok") else "fail",
                     "required checks green" if gate.get("ok") else "; ".join(gate.get("reasons") or ["not green"]))]
    reported = deployment.get("reported_commit")
    if deployment.get("url") is None:
        identity = _check("commit_identity", "not-checked", "no deployment of this commit was reached")
    elif reported != sha:
        identity = _check("commit_identity", "fail", f"{deployment['url']} reports "
                          + (reported or "no commit") + f", not {sha}")
    elif session_commit is not None and session_commit != sha:
        identity = _check("commit_identity", "fail", f"the build session published {session_commit or 'no commit'}, "
                          f"not the release's {sha}")
    else:
        identity = _check("commit_identity", "pass", f"{deployment['url']} reports {sha}"
                          + ("; it is the commit the build session published" if session_commit else ""))
    checks.append(identity)
    scenarios = (twin or {}).get("scenarios") or []
    if not twin or not scenarios:
        checks.append(_check("scenarios", "not-checked", "the scenarios were not replayed"))
    else:
        bad = [s for s in scenarios if s.get("result") != "pass"]
        checks.append(_check("scenarios", "pass" if not bad else "fail",
                             f"{len(scenarios) - len(bad)} of {len(scenarios)} passed"
                             + ("".join(f"; {s['name']}: {s.get('result')}" for s in bad[:5]))))
    reset = (twin or {}).get("reset") or {}
    checks.append(_check("fixture_reset", {"pass": "pass", "fail": "fail"}.get(reset.get("result"), "not-checked"),
                         reset.get("notes") or "the reset was not checked"))
    kept = (twin or {}).get("persistence") or {}
    checks.append(_check("persistence", {"pass": "pass", "fail": "fail"}.get(kept.get("result"), "not-checked"),
                         kept.get("notes") or "persistence across a restart was not checked"))
    checks.append(_failures(failure_cases(manifest), scenarios))
    visual = (dims or {}).get("visual") or {}
    status = visual.get("status")
    checks.append(_check("visual_coverage", {"pass": "pass", "fail": "fail"}.get(status, "not-checked"),
                         visual.get("scope") or "no pictures compared", required=bool(visual.get("required"))))
    return checks


def _failures(named, scenarios):
    if not named:
        return _check("failure_scenarios", "not-checked", "the brief names no failure cases", required=False)
    passed = failed = 0
    uncovered = []
    by_id = {s.get("id"): s for s in scenarios}
    for scenario_id, cases in named.items():
        found = [c for r in (by_id.get(scenario_id) or {}).get("replays") or [] for c in r.get("cases") or []
                 if str(c.get("id") or "").lower().startswith("failure")]
        failed += sum(c.get("status") == "FAIL" for c in found)
        ok = sum(c.get("status") == "PASS" for c in found)
        passed += ok
        if ok < len(cases):
            uncovered.append(f"{scenario_id}: {len(cases) - ok} of {len(cases)} not covered")
    total = sum(len(c) for c in named.values())
    if failed:
        return _check("failure_scenarios", "fail", f"{failed} failure case(s) did not hold")
    if uncovered:
        return _check("failure_scenarios", "not-checked", f"{passed} of {total} failure case(s) covered by a passing "
                      "acceptance case (id failure-…); " + "; ".join(uncovered[:5]), required=False)
    return _check("failure_scenarios", "pass", f"all {total} failure case(s) held")


def blocking(checks):
    return [c for c in checks if c["status"] == "fail"]
