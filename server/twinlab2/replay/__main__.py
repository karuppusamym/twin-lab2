"""Replay one recording against a running app and write the fidelity report as JSON.

    python -m twinlab2.replay <recording folder> --base-url <deployment> --json <report.json>

This is how Verify runs a replay (`twinlab2.independent`), in its own process with a timeout.
The report is `fidelity.replay`'s (DOM comparison after every step, then the acceptance cases
declared beside the recording). When the replay reaches a Twin Lab path that TwinLab2 did not
copy (`NotAbsorbed`), the report says `not-run` with the reason: a scenario is never passed on a
replay that could not finish here.

Needs Playwright and its Chromium (`pip install -e "server[replay]"`, then
`python -m playwright install chromium`).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from twinlab2_contract.upstream import NotAbsorbed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m twinlab2.replay", description=__doc__.split("\n\n")[0])
    parser.add_argument("recording", type=Path, help="recording folder (with steps.jsonl)")
    parser.add_argument("--base-url", required=True, help="the deployment to replay against")
    parser.add_argument("--json", type=Path, required=True, dest="json_out", help="report file to write")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    from twinlab2.replay import fidelity

    try:
        report = fidelity.replay(args.recording, args.base_url, {}, headless=not args.headed)
    except NotAbsorbed as exc:
        args.json_out.write_text(json.dumps({"status": "not-run", "verdict": None, "problems": [str(exc)],
                                             "cases": []}, indent=2) + "\n", encoding="utf-8")
        print(exc, file=sys.stderr)
        return 1
    args.json_out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(report.markdown())
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
