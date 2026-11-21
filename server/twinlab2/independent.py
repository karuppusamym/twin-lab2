"""Independent checks of a built app with TwinLab2's own replay and lab-agent check.

Besides GitHub CI, a release is checked against a deployment that reports the release's commit at
/.twinlab/version (so the checks ran on exactly the built commit):

- **scenario replay**: each brief scenario's recordings are replayed on every surface the
  application has, each in its own process with a timeout
  (`python -m twinlab2.replay <recording>/<surface> ... --json <report>`):
  - **web**: DOM comparison after every step, plus the acceptance and visual cases declared
    beside the recording. A recording that spans several applications (two applications on one
    host, told apart by path) replays each application's steps against that application's own
    deployment: `plan["web"]["origins"]` maps each source origin or origin + path prefix to its
    replica (the portal builds it from each application's web identity and its release's
    deployment URL). Without a declared `visual.yaml`, a report-only one is generated from the
    recorder's pictures at the size they were taken (it never decides the result);
  - **mainframe**: a socket 3270 terminal against the rebuilt host (`twinlab2.replay.tn3270`:
    title line and input fields per screen, acceptance cases on the screen text);
  - **desktop**: UI Automation against the built executable (`twinlab2.replay.desktop`). It
    opens the application's windows, so it runs only on a Windows verifier that allows it;
    elsewhere the scenario is not-run, with the reason;
- **business outcome**: the required acceptance cases of those replays. MET when all passed,
  NOT_MET when one failed, CANNOT_CHECK when none is declared or one was blocked;
- **reset check**: the app's lab agent is reset (`POST /_lab/reset`) and its data must then equal
  its baseline (`twinlab2.labagent.Agent`).

A scenario passes only when every replay passed and its outcome is MET; it fails when a replay
failed or the outcome is NOT_MET; otherwise it is not-run (no recording, nothing that judges the
business result, or a replay that could not run here), never reported as passed.

The report has the results per dimension (`dimensions`): behavioural (acceptance cases),
structural (the step-by-step comparison: DOM, screens, window trees), visual (pictures) and,
filled in by the portal, deployment (the commit the deployment reports, the reset check, CI),
each with what it covered (`scope`).

The replay and the lab-agent client are Twin Lab's, copied at 90bdd91 (docs/UPSTREAM.md). The web
replay needs Playwright and Chromium (`pip install -e "server[replay]"`). Recording folders
(`<recording id>/steps.jsonl`, or `web/`, `mainframe/`, `desktop/` folders inside it) come from
`TWINLAB2_RECORDINGS`; the lab agent token from `TWINLAB2_LAB_TOKEN`.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from twinlab2 import paths
from twinlab2.paths import STATE
from twinlab2_contract.upstream import UPSTREAM_COMMIT

SURFACES = ("web", "mainframe", "desktop")
RESET_SCRIPT = r"""
import json, sys
from twinlab2.labagent import Agent, LabError
agent = Agent(sys.argv[1], token=sys.argv[2])
try:
    agent.reset()
    data, baseline = agent.data(), agent.baseline_rows()
except LabError as exc:
    print(json.dumps({"result": "fail", "notes": f"lab agent: {exc}"})); sys.exit(0)
if baseline is None:
    print(json.dumps({"result": "fail", "notes": "the lab has no baseline to reset to"}))
elif data == baseline:
    print(json.dumps({"result": "pass", "notes": f"reset restored the baseline ({len(baseline)} table(s))"}))
else:
    changed = sorted(t for t in set(data) | set(baseline) if data.get(t) != baseline.get(t))
    print(json.dumps({"result": "fail", "notes": "after reset these tables differ from the baseline: "
                      + ", ".join(changed[:20])}))
"""
STRUCTURE = {"web": "the page's DOM after every step (texts and data not compared)",
             "mainframe": "each screen's title line and input fields (row, column, length, numeric, non-display)",
             "desktop": "each window's controls (type, locator, state) after every step"}


class ReplayUnavailable(Exception):
    """The replay cannot run on this machine (Playwright is not installed)."""


class TwinLabChecks:
    """Runs the replay and the reset check. Tests replace it with a fake with the same methods
    (`source`, `replay(recording_id, base_url)`, `reset`; optionally `surfaces` and replay's
    keyword arguments)."""

    def __init__(self, recordings=None, lab_token=None, timeout=900):
        self.recordings = Path(recordings or os.environ.get("TWINLAB2_RECORDINGS") or
                               STATE / "recordings")
        self.lab_token = lab_token if lab_token is not None else os.environ.get("TWINLAB2_LAB_TOKEN", "")
        self.timeout = timeout

    def source(self, surfaces=None):
        """What runs the checks: {commit (the Twin Lab commit the copy came from), replay}.
        ReplayUnavailable when a web replay is needed and Playwright is missing here."""
        needed = ("pydantic", "yaml")
        if surfaces is None or "web" in surfaces:
            needed += ("playwright", "PIL")
        missing = [name for name in needed if importlib.util.find_spec(name) is None]
        if missing:
            raise ReplayUnavailable(f"the replay needs {', '.join(missing)}: pip install -e \"server[replay]\", "
                                    "then python -m playwright install chromium")
        return {"commit": UPSTREAM_COMMIT, "replay": "twinlab2.replay"}

    def _env(self):
        env = paths.child_env()
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def recording(self, recording_id, surface="web"):
        """The folder of a recording's steps on one surface, or None."""
        base = self.recordings / recording_id
        candidates = [base, base / "web"] if surface == "web" else [base / surface]
        for folder in candidates:
            if (folder / "steps.jsonl").is_file():
                return folder
        return None

    def surfaces(self, recording_id):
        """The surfaces a recording has steps on."""
        return [s for s in SURFACES if self.recording(recording_id, s) is not None]

    def _visual(self, folder, work):
        """(visual.yaml to use or None, how it was decided: declared | generated | none)."""
        from twinlab2.replay import visual

        if (folder / visual.FILE).is_file():
            return None, "declared"
        try:
            if not visual.pictures_of(folder):
                return None, "none"
            from twinlab2_contract.evidence import recording as recmod

            out = Path(work) / visual.FILE
            out.write_text(visual.template(recmod.load(folder), required=False), encoding="utf-8")
            return out, "generated"
        except Exception as error:  # noqa: BLE001 - visual is reported, never blocks the replay
            return None, f"none (the template could not be generated: {type(error).__name__}: {error})"[:300]

    def replay(self, recording_id, base_url, surface="web", target=None, origins=None, twin_label=False):
        """The report of one recording replayed on one surface (web: against base_url, or each
        source in `origins` against its replica; mainframe: the TN3270 host `target`; desktop:
        the executable `target`), as a dict with at least `status`; None when the recording has
        no folder for that surface."""
        folder = self.recording(recording_id, surface)
        if folder is None:
            return None
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "report.json"
            cmd = [sys.executable, "-m", "twinlab2.replay", str(folder), "--surface", surface, "--json", str(out)]
            if twin_label:  # a format-3 package: the twin's label on every screen, out of the comparisons
                cmd.append("--twin-label")
            visual_from = None
            if surface == "web":
                if importlib.util.find_spec("playwright") is None:
                    return {"status": "not-run", "surface": surface, "cases": [], "problems": [
                        "the web replay needs Playwright here: pip install -e \"server[replay]\", "
                        "then python -m playwright install chromium"]}
                if base_url:
                    cmd += ["--base-url", base_url]
                for source, replica in (origins or {}).items():
                    cmd += ["--map", f"{source}={replica}"]
                generated, visual_from = self._visual(folder, work)
                if generated is not None:
                    cmd += ["--visual", str(generated), "--artifacts", str(Path(work) / "pictures")]
            else:
                if not target:
                    return {"status": "not-run", "surface": surface, "cases": [], "problems": [
                        f"no {surface} target to replay against: " + (
                            "the deployment URL names the TN3270 host (host:port), or give one with the request"
                            if surface == "mainframe" else "a desktop release names its built executable")]}
                cmd += ["--target", str(target)]
            done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=self._env(), timeout=self.timeout, check=False)
            if not out.is_file():
                return {"status": "blocked", "surface": surface, "problems": [
                    "the replay did not write a report: " + (done.stderr or done.stdout).strip()[-500:]]}
            report = json.loads(out.read_text(encoding="utf-8"))
            report.setdefault("surface", surface)
            if visual_from:
                report["visual_declaration"] = visual_from
            return report

    def reset(self, lab_url):
        done = subprocess.run([sys.executable, "-c", RESET_SCRIPT, lab_url, self.lab_token],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self._env(), timeout=self.timeout, check=False)
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return {"result": "fail", "notes": "the reset check did not run: " + done.stderr.strip()[-500:]}


def scenario_recordings(manifest, scenario_id):
    """The recording ids that show a brief scenario."""
    if scenario_id.startswith("recording:"):
        return [scenario_id.split(":", 1)[1]]
    found = []
    for number, item in enumerate(manifest.get("scenarios") or [], 1):
        if isinstance(item, dict) and str(item.get("id") or item.get("key") or f"scenario-{number}") == scenario_id:
            for event in item.get("events") or []:
                if event.get("recording_id") and event["recording_id"] not in found:
                    found.append(event["recording_id"])
    return found


def outcome(reports):
    """MET / NOT_MET / CANNOT_CHECK from the required acceptance cases of the replays."""
    cases = [case for report in reports for case in report.get("cases") or [] if case.get("required", True)]
    if any(case.get("status") == "FAIL" for case in cases):
        return "NOT_MET"
    if cases and all(case.get("status") == "PASS" for case in cases):
        return "MET"
    return "CANNOT_CHECK"


def _source(runner, surfaces):
    try:
        return runner.source(surfaces=surfaces)
    except TypeError:  # a runner without surfaces (a test fake)
        return runner.source()


def _surfaces_of(runner, recording_id):
    finder = getattr(runner, "surfaces", None)
    return finder(recording_id) if finder is not None else ["web"]


def _accepts(method, name):
    """The runner's replay takes this keyword (a test fake or an older runner may not)."""
    import inspect

    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _replay(runner, recording_id, surface, base_url, plan):
    origins = ((plan.get("web") or {}).get("origins") or None) if surface == "web" else None
    label = {"twin_label": True} if plan.get("twin_label") and _accepts(runner.replay, "twin_label") else {}
    if surface == "web" and not origins:
        return runner.replay(recording_id, base_url, **label)
    target = plan.get(surface) if surface != "web" else None
    return runner.replay(recording_id, base_url, surface=surface, target=target, origins=origins, **label)


def run(runner, scenarios, manifest, base_url, lab_url, progress=None, plan=None):
    """Twin Lab's verification of one deployment: {twinlab, scenarios: [...], reset: {...},
    dimensions: {...}}. `plan`: {surfaces (None: every surface a recording has), web: {origins},
    mainframe: host:port, desktop: executable}. `progress` (verifications.Progress) is told
    each scenario's state as it runs."""
    plan = plan or {}
    wanted = plan.get("surfaces")
    report = {"base_url": base_url, "lab_url": lab_url, "scenarios": [], "reset": None,
              "surfaces": list(wanted) if wanted else None}
    if (plan.get("web") or {}).get("origins"):
        report["origins"] = dict(plan["web"]["origins"])
    try:
        report["twinlab"] = _source(runner, wanted)
    except (ReplayUnavailable, OSError) as error:
        report["twinlab"] = {"error": str(error)}
        report["problems"] = [f"The replay is not available here: {error}"]
        report["dimensions"] = dimensions([], [])
        return report
    if progress is not None:
        progress.scenarios(scenarios)
        progress.stage("replaying the scenarios")
    # A replay changes the deployment's data (it saves what the recording saved), so a recording is
    # replayed once per verification (and surface) and its report is shared by every scenario
    # linked to it: replaying it again would start from the first replay's data, not the seed.
    replayed = {}
    for scenario in scenarios:
        if progress is not None:
            progress.scenario(scenario["id"], "running")
        entry = {"id": scenario["id"], "name": scenario["name"], "replays": []}
        reports, missing = [], []
        for recording_id in scenario_recordings(manifest, scenario["id"]):
            present = _surfaces_of(runner, recording_id)
            chosen = [s for s in present if not wanted or s in wanted]
            if not present:  # not available here: the replay says so (None)
                chosen = [wanted[0]] if wanted else ["web"]
            elif not chosen:
                missing.append(f"recording {recording_id} has no steps on this application's surface "
                               f"({', '.join(wanted)}); it has {', '.join(present)}")
                continue
            for surface in chosen:
                key = (recording_id, surface)
                if key not in replayed:
                    replayed[key] = _replay(runner, recording_id, surface, base_url, plan)
                result = replayed[key]
                if result is None:
                    entry["replays"].append({"recording": recording_id, "surface": surface, "status": "not-available"})
                    continue
                reports.append(result)
                entry["replays"].append({
                    "recording": recording_id, "surface": result.get("surface") or surface,
                    "status": result.get("status"), "verdict": result.get("verdict"),
                    "problems": list(result.get("problems") or [])[:20],
                    "twin_label_missing": [st.get("seq", st.get("step")) for st in result.get("steps") or []
                                           if any(d.get("check") == "twin_label" for d in st.get("differences") or [])],
                    "cases": [{key: case.get(key) for key in ("id", "check", "required", "status", "note")}
                              for case in result.get("cases") or []]})
        entry["outcome"] = outcome(reports)
        not_run = [r for r in reports if r.get("status") == "not-run"]
        if not reports:
            entry["result"] = "not-run"
            entry["why"] = "; ".join(missing) or "No recording of this scenario is available to replay."
        elif not_run and entry["outcome"] != "NOT_MET" and all(r.get("status") in ("pass", "not-run") for r in reports):
            # the replay could not run here (a cut Twin Lab path, no desktop verifier, no Playwright)
            entry["result"] = "not-run"
            entry["why"] = "The replay could not finish here: " + "; ".join(
                str(p) for r in not_run for p in (r.get("problems") or ["no reason given"]))[:500]
        elif any(r.get("status") != "pass" for r in reports) or entry["outcome"] == "NOT_MET":
            entry["result"] = "fail"
            entry["why"] = "A replay did not pass." if entry["outcome"] != "NOT_MET" else "The business outcome was not met."
        elif entry["outcome"] == "MET" and len(reports) == len(entry["replays"]) and not missing:
            entry["result"] = "pass"
        else:
            entry["result"] = "not-run"
            entry["why"] = ("Replayed, but no acceptance case judges the business outcome."
                            if entry["outcome"] == "CANNOT_CHECK" else
                            "; ".join(missing) or "A recording of this scenario is not available.")
        report["scenarios"].append(entry)
        if progress is not None:
            progress.scenario(scenario["id"], entry["result"], result=entry["result"], why=entry.get("why"),
                              surface=", ".join(sorted({r.get("surface") or "web" for r in reports})) or None)
    if progress is not None:
        progress.stage("checking the reset")
    report["reset"] = runner.reset(lab_url) if lab_url else {
        "result": None, "notes": "No lab agent URL is configured for this application, so reset was not checked."}
    report["dimensions"] = dimensions(list(replayed.values()), report["scenarios"])
    return report


# --- dimensions --------------------------------------------------------------------------------------


def _status(passed, failed, blocked, total):
    if not total:
        return "not-checked"
    if failed:
        return "fail"
    if blocked:
        return "blocked"
    return "pass"


def dimensions(reports, scenarios):
    """Behavioural, structural and visual results of the replays, each with its scope (what it
    covered). Deployment is added by the portal (portal.verify_release)."""
    reports = [r for r in reports if r]
    ran = [r for r in reports if r.get("status") != "not-run"]
    cases = [c for r in ran for c in r.get("cases") or []]
    required = [c for c in cases if c.get("required", True)]
    behavioural = {
        "status": _status(sum(c.get("status") == "PASS" for c in required), sum(c.get("status") == "FAIL" for c in required),
                          sum(c.get("status") not in ("PASS", "FAIL") for c in required), len(required)),
        "required": len(required), "passed": sum(c.get("status") == "PASS" for c in required),
        "failed": sum(c.get("status") == "FAIL" for c in required),
        "blocked": sum(c.get("status") not in ("PASS", "FAIL") for c in required),
        "optional": len(cases) - len(required),
        "scenarios": {s["id"]: s.get("outcome") for s in scenarios},
        "scope": ("the required acceptance cases declared beside each recording (acceptance.yaml): "
                  f"{len(required)} case(s)" if required else
                  "no acceptance case is declared beside the replayed recordings: the business result is not judged"),
    }
    steps = compared = failed = 0
    surfaces = {}
    for r in ran:
        cov = r.get("coverage") or {}
        steps += int(cov.get("steps") or len(r.get("steps") or []))
        compared += int(cov.get("compared") or 0)
        failed += int(cov.get("failed") or sum(1 for s in r.get("steps") or [] if s.get("status") == "fail"))
        surface = r.get("surface") or "web"
        cat = ((r.get("categories") or {}).get("replay") or {}).get("status")
        surfaces[surface] = {"status": {"PASS": "pass", "FAIL": "fail"}.get(cat, "blocked" if cat else r.get("status")),
                             "compares": STRUCTURE.get(surface, "")}
    statuses = [v["status"] for v in surfaces.values()]
    structural = {
        "status": "not-checked" if not ran else "fail" if "fail" in statuses else
        "blocked" if any(s not in ("pass",) for s in statuses) else "pass",
        "steps": steps, "compared": compared, "failed": failed, "surfaces": surfaces,
        "scope": "; ".join(f"{k}: {v['compares']}" for k, v in surfaces.items()) or "nothing was replayed",
    }
    states = [v for r in ran for v in r.get("visual") or []]
    declared = sorted({r.get("visual_declaration") or ("declared" if r.get("visual") else "none")
                       for r in ran if (r.get("surface") or "web") == "web"})
    by = {}
    for v in states:
        by[v.get("status")] = by.get(v.get("status"), 0) + 1
    req = [v for v in states if v.get("required")]
    viewports = sorted({v.get("viewport") for v in states if v.get("viewport")})
    visual = {
        "status": ("not-checked" if not states else _status(0, sum(v.get("status") == "FAIL" for v in req),
                                                              sum(v.get("status") == "BLOCKED" for v in req), len(req))
                   if req else "reported"),
        "states": len(states), "by_status": by, "required": len(req), "viewports": viewports,
        "declaration": declared[0] if len(declared) == 1 else declared,
        "scope": (_visual_scope(declared, len(states), len(req), viewports)),
    }
    if states and not req:
        ratios = sorted(float(v.get("ratio") or 0) for v in states if v.get("compared"))
        visual["median_ratio"] = ratios[len(ratios) // 2] if ratios else None
    visual["not_passed"] = [{"id": v.get("id"), "status": v.get("status"), "required": bool(v.get("required")),
                             "note": (v.get("note") or "")[:200]} for v in states if v.get("status") != "PASS"][:12]
    return {"behavioural": behavioural, "structural": structural, "visual": visual}


def _visual_scope(declared, states, required, viewports):
    if not states:
        if "none" in declared or not declared:
            return "not compared: the replayed recordings have no pictures (mainframe and desktop pictures are not compared)"
        return "not compared"
    size = f" at {', '.join(viewports)}" if viewports else ""
    if required:
        return f"{states} picture state(s){size} declared in visual.yaml, {required} required"
    return (f"{states} picture state(s){size} generated from the recorder's pictures: reported only, "
            "they do not decide Tested (declare visual.yaml to require them)")
