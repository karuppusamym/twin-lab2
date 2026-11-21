"""Shared paths, ports and process helpers for the rebuild proof (tools/rebuild_proof).

Everything the proof writes goes to the evidence folder (``REBUILD_PROOF_EVIDENCE``, default
``<parent of this checkout>/rebuild-proof``), never into the repository. The Twin Lab reference
apps are read from the sibling checkout (``TWINLAB_CHECKOUT``, default ``<parent>/twin-lab``) and
are never edited.

No step opens a visible window: browsers are headless Chromium, the mainframe terminal is a
socket client, and desktop work is only prepared (desktop_batch.ps1, run on "go desktop").
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]  # this twin-lab2 checkout
PARENT = ROOT.parent
EVIDENCE = Path(os.environ.get("REBUILD_PROOF_EVIDENCE") or PARENT / "rebuild-proof")
TWINLAB = Path(os.environ.get("TWINLAB_CHECKOUT") or PARENT / "twin-lab")
MULTIAPP = TWINLAB / "lab" / "labctl" / "tests" / "fixtures" / "multiapp"
DEMO_HOST = TWINLAB / "reference" / "mainframe" / "demo_host.py"
CASEDESK_REC = TWINLAB / "lab" / "labctl" / "tests" / "fixtures" / "desktop" / "casedesk-rec"
CASEDESK_SRC = TWINLAB / "reference" / "desktop" / "CaseDesk"
for _p in ("recorder", "contract", "server"):  # this checkout's packages, in this process too
    if str(ROOT / _p) not in sys.path:
        sys.path.insert(0, str(ROOT / _p))

PORTAL_PORT = int(os.environ.get("REBUILD_PROOF_PORTAL_PORT") or 8796)
PORTS = {
    # case A: Orders alone (billing and widget run beside it so its links resolve; not recorded)
    "a": {"orders": 18751, "billing": 18752, "widget": 18753},
    # case C: Orders and Billing on ONE host, told apart by path; widget on its own origin
    "c": {"shared": 18761, "widget": 18763},
    "tn3270": 13270,  # the synthetic TN3270 host (cases B and C)
    "proxy": 23270,  # the recorder's TN3270 proxy the terminal connects to
}
CASES = ("a", "b", "c", "d")


def env() -> dict:
    """The environment for portal, recorder and replay processes: this checkout's packages first
    (the shared main checkout may be on another commit while other sessions edit it)."""
    e = dict(os.environ)
    paths = [str(ROOT / "server"), str(ROOT / "contract"), str(ROOT / "recorder")]
    e["PYTHONPATH"] = os.pathsep.join(paths + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else []))
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e["PYTHONUNBUFFERED"] = "1"
    return e


def no_window() -> int:
    """creationflags that keep a child process from opening a console window."""
    return 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def spawn(args, log: Path, cwd=None, extra_env=None) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    e = env()
    e.update(extra_env or {})
    out = open(log, "a", encoding="utf-8")
    return subprocess.Popen(args, cwd=cwd, env=e, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=no_window())


def run(args, log: Path | None = None, cwd=None, extra_env=None, timeout=1800, check=True) -> subprocess.CompletedProcess:
    e = env()
    e.update(extra_env or {})
    done = subprocess.run(args, cwd=cwd, env=e, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                          creationflags=no_window())
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(map(str, args))}\n{done.stdout}\n{done.stderr}\n[exit {done.returncode}]\n")
    if check and done.returncode != 0:
        raise SystemExit(f"{args[:4]} failed ({done.returncode}): {(done.stderr or done.stdout)[-1500:]}")
    return done


def wait_port(port: int, host: str = "127.0.0.1", timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"nothing answers on {host}:{port} after {timeout:.0f} s")


def wait_http(url: str, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    raise SystemExit(f"{url} does not answer after {timeout:.0f} s")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def chromium() -> str:
    """Playwright's Chromium executable (launched headless, with a DevTools port)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        return pw.chromium.executable_path
