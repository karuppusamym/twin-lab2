"""One replay with visual fidelity (the portal's `twinlab2.replay.visual`), in its own process.

    python tools/rebuild_proof/visual_run.py <recording folder> <base url> <report.json> <width>x<height> <pictures dir>

Why not `python -m twinlab2.replay`: visual.py compares only at its named viewports (desktop
1280x720, mobile 390x844), and pictures of another size FAIL. A recording made in attach mode
(`--cdp`) keeps the browser's own viewport (here 1258x570, a headless window of 1280x720), so its
pictures can never match. This runner changes the size of `desktop` in this process only to the
recorded pictures' size; everything else (masking, thresholds, the comparison) is visual.py's.
The behavioural result of this replay is not used: the strict replay (replay.py web) is that.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401 - this checkout's packages


def main(argv) -> int:
    folder, base, out, size, pictures = Path(argv[1]), argv[2], Path(argv[3]), argv[4], Path(argv[5])
    w, h = (int(x) for x in size.split("x"))
    from twinlab2.replay import fidelity, visual

    visual.VIEWPORTS["desktop"].update(width=w, height=h)
    report = fidelity.replay(folder, base, {}, headless=True, artifacts=pictures)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    vis = [v.model_dump() for v in report.visual]
    print(json.dumps({"status": report.status, "visual": len(vis)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
