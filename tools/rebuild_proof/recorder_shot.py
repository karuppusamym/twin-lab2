"""A picture of the recorder's own window (the built exe's GUI), started with the proof's data
folder and closed by its PID. Only that window is captured (PrintWindow).

    python tools/rebuild_proof/recorder_shot.py <png> [seconds]
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from twinlab2_recorder.desktop import win  # noqa: E402
import live_calc  # noqa: E402


def main(argv) -> int:
    png, wait = Path(argv[1]), float(argv[2]) if len(argv) > 2 else 25
    env = common.env()
    env["LOCALAPPDATA"] = str(live_calc.DATA)
    proc = subprocess.Popen([str(live_calc.EXE)], env=env, creationflags=common.no_window())
    try:
        end = time.monotonic() + 60
        hwnds = []
        while not hwnds and time.monotonic() < end:
            time.sleep(0.5)
            pids = {proc.pid} | set(_children(proc.pid))
            hwnds = [h for h in win.top_windows(pids) if win.class_name(h) == "TkTopLevel"]
        if not hwnds:
            raise SystemExit("no recorder window")
        time.sleep(wait)  # the first poll of the portal fills the tabs
        ok = win.screenshot(hwnds[0], png)
        print(png, ok)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=common.no_window())
    return 0


def _children(pid: int) -> list[int]:
    import psutil

    try:
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except psutil.Error:
        return []


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
