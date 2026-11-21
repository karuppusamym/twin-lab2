"""The live Windows Calculator proof (the user approved one live run on 2026-11-21). OPENS
CALCULATOR'S WINDOW on this PC; nothing else is clicked or typed into.

    python tools/rebuild_proof/calc_live.py probe            # open Calculator, dump its controls, close it
    python tools/rebuild_proof/calc_live.py drive <out.json> # the scripted clerk on a running Calculator
    python tools/rebuild_proof/calc_live.py shot <pid> <png> # a picture of one process's window only

The clerk works only through UI Automation patterns (Invoke, SelectionItem, ExpandCollapse) on
Calculator's own elements: no mouse or keyboard input is synthesised, so nothing can land in
another window. Calculator is started with `calc.exe` (which hands over to CalculatorApp.exe and
exits) and closed by the PID of the CalculatorApp.exe this script saw appear.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401 - puts this checkout's recorder package on sys.path

from twinlab2_recorder.desktop import win  # noqa: E402

APP = "CalculatorApp.exe"
WM_CLOSE = 0x0010


def calc_pids() -> set[int]:
    return set(win.pids_by_name(APP))


def launch(timeout: float = 30) -> tuple[int, int]:
    """Start Calculator; -> (CalculatorApp pid, its frame window). Refuses when one already runs
    (that one is the user's)."""
    if calc_pids():
        raise SystemExit("a Calculator is already running: close it first (it is not this script's)")
    subprocess.Popen([r"C:\Windows\System32\calc.exe"], creationflags=common.no_window())
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pids = calc_pids()
        if pids:
            pid = next(iter(pids))
            frames = win.frame_windows({pid})
            if frames:
                time.sleep(1.5)
                return pid, next(iter(frames))
        time.sleep(0.3)
    raise SystemExit("Calculator did not open")


def frame_of(pid: int) -> int:
    frames = win.frame_windows({pid})
    if not frames:
        raise SystemExit(f"no Calculator window for pid {pid}")
    return next(iter(frames))


def close(pid: int) -> None:
    """Close the Calculator this script started: WM_CLOSE to its frame, then end that PID."""
    for hwnd in win.frame_windows({pid}):
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    end = time.monotonic() + 10
    while win.alive(pid) and time.monotonic() < end:
        time.sleep(0.3)
    if win.alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=common.no_window())


def tree(el, depth=0, out=None, max_depth=25):
    out = [] if out is None else out
    try:
        aid, name = el.CurrentAutomationId, el.CurrentName
        ctype = win.CONTROL_TYPES.get(el.CurrentControlType, el.CurrentControlType)
        cls, fw = el.CurrentClassName, el.CurrentFrameworkId
    except Exception:  # noqa: BLE001
        return out
    out.append({"depth": depth, "aid": aid, "name": name, "type": ctype, "cls": cls, "fw": fw})
    if depth >= max_depth:
        return out
    walker = win.auto().ControlViewWalker
    child = walker.GetFirstChildElement(el)
    while child:
        tree(child, depth + 1, out, max_depth)
        try:
            child = walker.GetNextSiblingElement(child)
        except Exception:  # noqa: BLE001
            break
    return out


def find(hwnd: int, aid: str | None = None, name: str | None = None, ctype: str | None = None, timeout=8.0):
    """The one element in Calculator's window (and its popups) with this AutomationId / Name."""
    parts = {k: v for k, v in (("AutomationId", aid), ("Name", name), ("ControlType", ctype)) if v}
    from twinlab2_recorder.desktop import locator

    sel = locator.format(parts)
    end = time.monotonic() + timeout
    while True:
        roots = [hwnd] + [h for h in win.top_windows({win.window_pid(hwnd), PID[0]}) if h != hwnd]
        found = []
        for h in roots:
            try:
                found += win.find_all(win.element_from_handle(h), sel)
            except Exception:  # noqa: BLE001
                pass
        if found:
            return found[0]
        if time.monotonic() > end:
            raise SystemExit(f"Calculator has no {sel}")
        time.sleep(0.3)


PID = [0]
LOG: list[dict] = []


def act(hwnd, what: str, *, aid=None, name=None, ctype=None, how="invoke", pause=0.9):
    el = find(hwnd, aid, name, ctype)
    if how == "invoke":
        did = win.invoke(el)
    elif how == "select":
        win.select(el)
        did = "Select"
    else:
        raise ValueError(how)
    LOG.append({"t": time.strftime("%H:%M:%S"), "what": what, "aid": aid, "name": name, "via": did})
    print(f"  {what}: {did}", flush=True)
    time.sleep(pause)


def result(hwnd) -> str:
    try:
        return find(hwnd, aid="CalculatorResults", timeout=3).CurrentName
    except SystemExit:
        return "?"


def mode(hwnd, which: str) -> None:
    act(hwnd, "open the navigation", aid="TogglePaneButton", pause=1.2)
    act(hwnd, f"choose {which}", aid=which, ctype="ListItem", how="select", pause=1.8)


def drive(pid: int, out: Path) -> dict:
    """The session: standard 12 + 7 = (19), x 3 = (57), clear; Scientific, 9 square root (3);
    History; back to Standard."""
    PID[0] = pid
    hwnd = frame_of(pid)
    seen = {}
    mode(hwnd, "Standard")  # Calculator opens in the mode last used
    for what, aid in (("1", "num1Button"), ("2", "num2Button"), ("+", "plusButton"), ("7", "num7Button"),
                      ("=", "equalButton")):
        act(hwnd, what, aid=aid)
    seen["12+7"] = result(hwnd)
    for what, aid in (("x", "multiplyButton"), ("3", "num3Button"), ("=", "equalButton")):
        act(hwnd, what, aid=aid)
    seen["x3"] = result(hwnd)
    act(hwnd, "C", aid="clearButton")
    seen["clear"] = result(hwnd)
    mode(hwnd, "Scientific")
    act(hwnd, "9", aid="num9Button")
    act(hwnd, "square root", aid="squareRootButton")
    seen["sqrt9"] = result(hwnd)
    # History: a narrow window has a History button (a flyout); a wide one docks History and
    # Memory as tabs beside the keys
    try:
        find(hwnd, aid="HistoryButton", timeout=1.5)
        narrow = True
    except SystemExit:
        narrow = False
    if narrow:
        act(hwnd, "History", aid="HistoryButton", pause=1.8)
    else:
        act(hwnd, "Memory tab", aid="MemoryLabel", how="select", pause=1.2)
        act(hwnd, "History tab", aid="HistoryLabel", how="select", pause=1.5)
    try:
        items = find(hwnd, aid="HistoryListView", timeout=3)
        seen["history_items"] = len(tree(items)) - 1
    except SystemExit:
        seen["history_items"] = None
    if narrow:  # the flyout closes with a second press of History
        act(hwnd, "close History", aid="HistoryButton", pause=1.2)
    mode(hwnd, "Standard")
    seen["end"] = result(hwnd)
    doc = {"pid": pid, "actions": LOG, "seen": seen}
    common.write_json(out, doc)
    return doc


def shot(pid: int, png: Path) -> bool:
    hwnds = list(win.frame_windows({pid})) or win.top_windows({pid})
    return bool(hwnds) and win.screenshot(hwnds[0], png)


def main(argv) -> int:
    what = argv[1] if len(argv) > 1 else ""
    if what == "probe":
        out = Path(argv[2])
        pid, hwnd = launch()
        PID[0] = pid
        try:
            rows = tree(win.element_from_handle(hwnd))
            common.write_json(out, {"pid": pid, "frame_class": win.class_name(hwnd), "controls": rows})
            shot(pid, out.with_suffix(".png"))
            print(f"{len(rows)} controls -> {out}")
        finally:
            close(pid)
    elif what == "drive":
        print(json.dumps(drive(int(argv[2]), Path(argv[3]))["seen"]))
    elif what == "launch":
        pid, _ = launch()
        print(pid)
    elif what == "close":
        close(int(argv[2]))
    elif what == "shot":
        print(shot(int(argv[2]), Path(argv[3])))
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
