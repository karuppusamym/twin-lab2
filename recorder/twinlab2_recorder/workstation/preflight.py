# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Before a workstation session records: can each channel it needs start, and reach its target?

    preflight(targets, out=Path("recordings"), cdp=None)
    -> {"checks": [{"name", "ok", "detail"}], "ready": bool}

The recorder shows "Ready" only when `ready` is true. The checks:

- `recorder`: the session format and the recorder contract this build writes are ones the portal
  takes (`portal_contract`, when one is given: same major version, minor not older);
- `disk`: free space where the session folder goes, above `min_free_mb`;
- `browser` (web targets): the recorder-controlled browser (Edge, Chrome, or Playwright's
  Chromium) is installed and Playwright is there to drive it; in attach mode, a browser answers
  at the DevTools address and is Chromium-based;
- `reach <origin>` (each web target): its host answers HTTP (any status counts);
- `desktop` (desktop targets): this is Windows and UI Automation answers;
- `mainframe <host:port>` (each TN3270 target): the host answers on that port, and the local port
  the terminal connects to is free.

Nothing is recorded and no browser window is opened.
"""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from twinlab2_recorder.workstation import levels as lv

MIN_FREE_MB = 1024
TIMEOUT_S = 5.0
UIA_TIMEOUT_S = 15.0
CHROMIUM_MARKERS = ("chrome", "chromium", "edg", "headlesschrome", "opera", "brave")


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def recorder_contract() -> str | None:
    """The recorder contract version this build writes, when it states one."""
    from twinlab2_recorder.workstation import timeline

    return getattr(timeline, "CONTRACT_VERSION", None)


def _version(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in str(text).strip().lstrip("v").split("."))
    except ValueError:
        return None


def check_recorder(portal_contract: str | None, contract: str | None = None) -> dict:
    from twinlab2_recorder.workstation import timeline

    fmt = getattr(timeline, "FORMAT", "")
    if not str(fmt).startswith("twinlab-workstation-session/"):
        return _check("recorder", False, f"unknown session format {fmt!r}")
    mine = contract if contract is not None else recorder_contract()
    if not portal_contract:
        return _check("recorder", True, f"{fmt}, contract {mine or 'not stated'}; no portal "
                      "contract to compare with")  # fmt: skip
    if not mine:
        return _check("recorder", False, f"the portal needs contract {portal_contract}; this "
                      "recorder states none: update the recorder")  # fmt: skip
    a, b = _version(mine), _version(portal_contract)
    if a is None or b is None:
        return _check("recorder", False, f"contract versions not understood: {mine!r}, "
                      f"{portal_contract!r}")  # fmt: skip
    if a[0] != b[0] or a[:2] < b[:2]:
        return _check("recorder", False, f"recorder contract {mine} does not fit the portal's "
                      f"{portal_contract}: update the recorder")  # fmt: skip
    return _check("recorder", True, f"{fmt}, contract {mine} (portal {portal_contract})")


def check_disk(out: Path, min_free_mb: float = MIN_FREE_MB) -> dict:
    p = Path(out).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        free = shutil.disk_usage(p).free / (1024 * 1024)
    except OSError as exc:
        return _check("disk", False, f"free space at {p} cannot be read: {type(exc).__name__}")
    ok = free >= min_free_mb
    return _check("disk", ok, f"{free:,.0f} MB free at {p}; at least {min_free_mb:,.0f} MB "
                  "needed" + ("" if ok else ": free some space"))  # fmt: skip


def _cdp_version(cdp: str, timeout: float) -> dict:
    with urllib.request.urlopen(cdp.rstrip("/") + "/json/version", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def web_channel_missing() -> str | None:
    """Why the web channel cannot run in this build, or None. The installer's web channel is an
    optional feature (Playwright's driver, recorder/installer): a PC installed without it records
    desktop and mainframe applications only."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        from playwright._impl._driver import compute_driver_executable
    except ImportError:
        return "the web channel is not part of this build"
    node, cli = compute_driver_executable()
    if not (Path(node).is_file() and Path(cli).is_file()):
        return ("the web channel is not installed on this PC (the installer's WebChannel feature): "
                "only desktop and mainframe applications can be recorded")  # fmt: skip
    return None


def check_browser(browser: str | None = None, cdp: str | None = None,
                  timeout: float = TIMEOUT_S) -> dict:  # fmt: skip
    missing = web_channel_missing()
    if missing:
        return _check("browser", False, missing)
    if cdp:
        try:
            info = _cdp_version(cdp, timeout)
        except (OSError, ValueError) as exc:
            return _check("browser", False, f"attach mode: no browser answers at {cdp} "
                          f"({type(exc).__name__}); start it with --remote-debugging-port")  # fmt: skip
        name = str(info.get("Browser") or "")
        if not any(m in name.lower() for m in CHROMIUM_MARKERS):
            return _check("browser", False, f"attach mode is Chromium-only; {cdp} is "
                          f"{name or 'not a Chromium browser'}")  # fmt: skip
        return _check("browser", True, f"attach mode (qualified, lower fidelity): {name} at {cdp}")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return _check("browser", False, "Playwright is not installed: the web channel cannot "
                      "drive a browser")  # fmt: skip
    from twinlab2_recorder.workstation.session import find_browser
    from twinlab2_recorder.workstation.supervisor import _playwright_chromium

    exe = find_browser(browser) or _playwright_chromium()
    if not exe:
        return _check("browser", False, "no Edge, Chrome or Playwright Chromium on this machine "
                      "(python -m playwright install chromium)")  # fmt: skip
    return _check("browser", True, f"recorder-controlled browser: {exe}")


def check_origin(origin: str, timeout: float = TIMEOUT_S) -> dict:
    name = f"reach {origin}"
    req = urllib.request.Request(origin.rstrip("/") + "/", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _check(name, True, f"answers ({r.status})")
    except urllib.error.HTTPError as exc:  # it answers: a status is an answer
        return _check(name, True, f"answers ({exc.code})")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return _check(name, True, "answers, but Python does not trust its certificate "
                          "(the browser may)")  # fmt: skip
        return _check(name, False, f"does not answer: {reason}")
    except (OSError, ValueError) as exc:
        return _check(name, False, f"does not answer: {type(exc).__name__}: {exc}")


def check_desktop(timeout: float = UIA_TIMEOUT_S) -> dict:
    if sys.platform != "win32":
        return _check("desktop", False, "desktop recording needs Windows (UI Automation)")
    got: list = []

    def probe() -> None:
        try:
            from twinlab2_recorder.desktop import require
            from twinlab2_recorder.desktop import win

            require()
            import comtypes

            comtypes.CoInitializeEx(0)  # this thread's COM, multithreaded as the recorder's
            win.auto().GetRootElement()
            got.append(None)
        except Exception as exc:  # noqa: BLE001 - reported
            got.append(f"{type(exc).__name__}: {str(exc)[:200]}")

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    if not got:
        return _check("desktop", False, f"UI Automation did not answer within {timeout:.0f} s")
    if got[0] is not None:
        return _check("desktop", False, f"UI Automation is not available: {got[0]}")
    from twinlab2_recorder.workstation.supervisor import desktop_locked

    if desktop_locked():
        return _check("desktop", False, "the desktop is locked: unlock it first")
    return _check("desktop", True, "UI Automation answers")


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def check_mainframe(target: lv.Target, index: int, timeout: float = TIMEOUT_S) -> list[dict]:
    host, port = target.tn3270.rsplit(":", 1)
    out = []
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        out.append(_check(f"mainframe {target.tn3270}", True, "the host answers"))
    except OSError as exc:
        out.append(_check(f"mainframe {target.tn3270}", False,
                          f"the host does not answer: {type(exc).__name__}"))  # fmt: skip
    listen = target.listen if target.listen is not None else 23270 + index
    if listen:
        free = _port_free(int(listen))
        out.append(_check(f"proxy port {listen}", free, "free" if free else
                          "taken by another program: the terminal would connect to another "
                          "port; close it or choose another listen port"))  # fmt: skip
    return out


def preflight(targets: lv.Targets, *, out: Path = Path("recordings"), browser: str | None = None,
              cdp: str | None = None, min_free_mb: float = MIN_FREE_MB,
              portal_contract: str | None = None, timeout: float = TIMEOUT_S) -> dict:  # fmt: skip
    from twinlab2_recorder.workstation.supervisor import requested

    want = requested(targets)
    checks = [check_recorder(portal_contract), check_disk(out, min_free_mb)]
    if "web" in want:
        checks.append(check_browser(browser, cdp, timeout))
        checks += [check_origin(o, timeout) for o in sorted(targets.origins())]
    if "desktop" in want:
        checks.append(check_desktop())
    for i, m in enumerate(targets.mainframes()):
        checks += check_mainframe(m, i, timeout)
    return {"checks": checks, "ready": all(c["ok"] for c in checks)}
