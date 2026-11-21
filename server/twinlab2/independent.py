"""Independent checks of a built app with TwinLab2's own replay and lab-agent check.

Besides GitHub CI, a release is checked against a deployment that reports the release's commit at
/.twinlab/version (so the checks ran on exactly the built commit):

- **scenario replay**: each brief scenario's recordings are replayed with
  `python -m twinlab2.replay <recording> --base-url <deployment> --json <report>` (DOM
  comparison after every step, plus the acceptance cases declared beside the recording);
- **business outcome**: the required acceptance cases of those replays. MET when all passed,
  NOT_MET when one failed, CANNOT_CHECK when none is declared or one was blocked;
- **reset check**: the app's lab agent is reset (`POST /_lab/reset`) and its data must then equal
  its baseline (`twinlab2.labagent.Agent`).

A scenario passes only when every replay passed and its outcome is MET; it fails when a replay
failed or the outcome is NOT_MET; otherwise it is not-run (no recording, nothing that judges the
business result, or a replay that reached a Twin Lab path TwinLab2 did not copy), never reported
as passed.

The replay and the lab-agent client are Twin Lab's, copied at 90bdd91 (docs/UPSTREAM.md); each
runs in its own process with a timeout. The replay needs Playwright and Chromium
(`pip install -e "server[replay]"`). Recording folders (`<recording id>/steps.jsonl`, or a `web/`
folder inside it) come from `TWINLAB2_RECORDINGS`; the lab agent token from `TWINLAB2_LAB_TOKEN`.
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


class ReplayUnavailable(Exception):
    """The replay cannot run on this machine (Playwright is not installed)."""


class TwinLabChecks:
    """Runs the replay and the reset check. Tests replace it with a fake with the same methods."""

    def __init__(self, recordings=None, lab_token=None, timeout=900):
        self.recordings = Path(recordings or os.environ.get("TWINLAB2_RECORDINGS") or
                               STATE / "recordings")
        self.lab_token = lab_token if lab_token is not None else os.environ.get("TWINLAB2_LAB_TOKEN", "")
        self.timeout = timeout

    def source(self):
        """What runs the checks: {commit (the Twin Lab commit the copy came from), replay}.
        ReplayUnavailable when Playwright is missing here."""
        missing = [name for name in ("playwright", "pydantic", "PIL") if importlib.util.find_spec(name) is None]
        if missing:
            raise ReplayUnavailable(f"the replay needs {', '.join(missing)}: pip install -e \"server[replay]\", "
                                    "then python -m playwright install chromium")
        return {"commit": UPSTREAM_COMMIT, "replay": "twinlab2.replay"}

    def _env(self):
        env = paths.child_env()
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def recording(self, recording_id):
        for folder in (self.recordings / recording_id, self.recordings / recording_id / "web"):
            if (folder / "steps.jsonl").is_file():
                return folder
        return None

    def replay(self, recording_id, base_url):
        """The fidelity report of one recording replayed against base_url, as a dict with at least
        `status`; None when the recording folder is not available."""
        folder = self.recording(recording_id)
        if folder is None:
            return None
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "fidelity.json"
            done = subprocess.run(
                [sys.executable, "-m", "twinlab2.replay", str(folder), "--base-url", base_url, "--json", str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=self._env(), timeout=self.timeout, check=False)
            if not out.is_file():
                return {"status": "blocked", "problems": [
                    "the replay did not write a report: " + (done.stderr or done.stdout).strip()[-500:]]}
            return json.loads(out.read_text(encoding="utf-8"))

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


def run(runner, scenarios, manifest, base_url, lab_url):
    """Twin Lab's verification of one deployment: {twinlab, scenarios: [...], reset: {...}}."""
    report = {"base_url": base_url, "lab_url": lab_url, "scenarios": [], "reset": None}
    try:
        report["twinlab"] = runner.source()
    except (ReplayUnavailable, OSError) as error:
        report["twinlab"] = {"error": str(error)}
        report["problems"] = [f"The replay is not available here: {error}"]
        return report
    # A replay changes the deployment's data (it saves what the recording saved), so a recording is
    # replayed once per verification and its report is shared by every scenario linked to it:
    # replaying it again would start from the first replay's data, not from the seed.
    replayed = {}
    for scenario in scenarios:
        entry = {"id": scenario["id"], "name": scenario["name"], "replays": []}
        reports = []
        for recording_id in scenario_recordings(manifest, scenario["id"]):
            if recording_id not in replayed:
                replayed[recording_id] = runner.replay(recording_id, base_url)
            result = replayed[recording_id]
            if result is None:
                entry["replays"].append({"recording": recording_id, "status": "not-available"})
                continue
            reports.append(result)
            entry["replays"].append({
                "recording": recording_id, "status": result.get("status"), "verdict": result.get("verdict"),
                "problems": list(result.get("problems") or [])[:20],
                "cases": [{key: case.get(key) for key in ("id", "check", "required", "status", "note")}
                          for case in result.get("cases") or []]})
        entry["outcome"] = outcome(reports)
        not_run = [r for r in reports if r.get("status") == "not-run"]
        if not reports:
            entry["result"], entry["why"] = "not-run", "No recording of this scenario is available to replay."
        elif not_run and entry["outcome"] != "NOT_MET" and all(r.get("status") in ("pass", "not-run") for r in reports):
            # the replay reached a Twin Lab path that TwinLab2 did not copy (NotAbsorbed)
            entry["result"] = "not-run"
            entry["why"] = "The replay could not finish here: " + "; ".join(
                str(p) for r in not_run for p in (r.get("problems") or ["no reason given"]))[:500]
        elif any(r.get("status") != "pass" for r in reports) or entry["outcome"] == "NOT_MET":
            entry["result"] = "fail"
            entry["why"] = "A replay did not pass." if entry["outcome"] != "NOT_MET" else "The business outcome was not met."
        elif entry["outcome"] == "MET" and len(reports) == len(entry["replays"]):
            entry["result"] = "pass"
        else:
            entry["result"] = "not-run"
            entry["why"] = ("Replayed, but no acceptance case judges the business outcome."
                            if entry["outcome"] == "CANNOT_CHECK" else "A recording of this scenario is not available.")
        report["scenarios"].append(entry)
    report["reset"] = runner.reset(lab_url) if lab_url else {
        "result": None, "notes": "No lab agent URL is configured for this application, so reset was not checked."}
    return report
