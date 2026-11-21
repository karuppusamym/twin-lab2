"""Replay one recording against a running app and write the report as JSON.

    python -m twinlab2.replay <recording folder> --base-url <deployment> --json <report.json>
        [--map <source>=<replica> ...] [--visual <visual.yaml>]
    python -m twinlab2.replay <recording>/mainframe --surface mainframe --target tn3270://host:port --json r.json
    python -m twinlab2.replay <recording>/desktop --surface desktop --target <built exe> --json r.json

This is how Verify runs a replay (`twinlab2.independent`), in its own process with a timeout.

- **web** (the default): `fidelity.replay`'s report (DOM comparison after every step, then the
  acceptance and visual cases declared beside the recording). `--map` sends a source (an origin,
  or an origin and path prefix: two applications on one host) to its replica; without it the
  recording's first origin goes to `--base-url`. `--visual` names the visual.yaml to use
  (Verify generates a report-only one when none is declared).
- **mainframe**: `twinlab2.replay.tn3270` (a socket terminal: title line and input fields per
  screen, acceptance cases on the screen text).
- **desktop**: `twinlab2.replay.desktop` (UI Automation; opens the application's windows, so only
  on a Windows verifier with TWINLAB2_DESKTOP_VERIFY=1).

When the replay reaches a Twin Lab path that TwinLab2 did not copy (`NotAbsorbed`), or cannot run
here (`DesktopUnavailable`), the report says `not-run` with the reason: a scenario is never passed
on a replay that could not finish here.

The web replay needs Playwright and its Chromium (`pip install -e "server[replay]"`, then
`python -m playwright install chromium`).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from twinlab2_contract.upstream import NotAbsorbed

SURFACES = ("web", "mainframe", "desktop")


def _not_run(out: Path, why: str, surface: str) -> int:
    out.write_text(json.dumps({"status": "not-run", "verdict": None, "problems": [why], "cases": [],
                               "surface": surface}, indent=2) + "\n", encoding="utf-8")
    print(why, file=sys.stderr)
    return 1


def surface_of(folder: Path) -> str:
    return folder.name if folder.name in ("mainframe", "desktop") else "web"


LABEL_ENV = "TWINLAB2_TWIN_LABEL"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m twinlab2.replay", description=__doc__.split("\n\n")[0])
    parser.add_argument("recording", type=Path, help="recording folder (with steps.jsonl)")
    parser.add_argument("--surface", choices=SURFACES, help="default: from the folder's name (web otherwise)")
    parser.add_argument("--base-url", help="web: the deployment to replay against")
    parser.add_argument("--target", help="mainframe: tn3270://host:port; desktop: the built executable")
    parser.add_argument("--map", action="append", default=[], dest="origins",
                        help="web: source=replica (an origin, or an origin and path prefix); repeatable")
    parser.add_argument("--visual", type=Path, help="web: the visual.yaml to use (default: beside the recording)")
    parser.add_argument("--artifacts", type=Path, help="web: where the replica's pictures go")
    parser.add_argument("--json", type=Path, required=True, dest="json_out", help="report file to write")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--twin-label", action="store_true",
                        help="require the \"AI generated Twin App\" label on every screen and leave it out of the "
                             "comparisons (twinlab2_contract.package)")
    args = parser.parse_args(argv)
    if args.twin_label:
        os.environ[LABEL_ENV] = "1"
    surface = args.surface or surface_of(args.recording)
    if surface == "mainframe":
        from twinlab2.replay import tn3270

        if not args.target and not args.base_url:
            parser.error("a mainframe replay needs --target tn3270://host:port")
        report = tn3270.replay(args.recording, args.target or args.base_url)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{report['status']}: {len(report['steps'])} steps, {len(report['cases'])} cases")
        return 0 if report["status"] == "pass" else 1
    if surface == "desktop":
        from twinlab2.replay import desktop

        if not args.target:
            parser.error("a desktop replay needs --target <the built executable>")
        try:
            report = desktop.replay(args.recording, args.target)
        except desktop.DesktopUnavailable as exc:
            return _not_run(args.json_out, str(exc), surface)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 0 if report["status"] == "pass" else 1
    from twinlab2.replay import fidelity

    if not args.base_url and not args.origins:
        parser.error("a web replay needs --base-url (or --map)")
    try:
        report = fidelity.replay(args.recording, args.base_url, {}, headless=not args.headed,
                                 origins=args.origins or None, visual=args.visual,
                                 artifacts=args.artifacts)
    except NotAbsorbed as exc:
        return _not_run(args.json_out, str(exc), surface)
    args.json_out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(report.markdown())
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
