"""Build the portable TwinLabRecorder folder with PyInstaller.

    python -m agent.twinlab fetch    the pinned Twin Lab (twinlab.lock.json), checked by digest
    python agent/build.py            -> dist/TwinLabRecorder/TwinLabRecorder.exe

The folder is the program: copy it to a Windows PC and run TwinLabRecorder.exe (no install, no
administrator rights). It carries Python, Twin Lab's collectors (labctl), UI Automation's
wrapper generated beforehand (comtypes.gen: the program's folder may be read-only), Playwright's
driver, the recorder contract's schema, and `_build.json` (the Twin Lab commit and tree digest,
the contract version). No browser: it uses the Edge on the PC.

The collectors are the pinned commit's, vendored by `agent.twinlab` and verified against the
lock's digest before the build. TWINLAB_PATH builds from another checkout instead, only with
TWINLAB_ALLOW_UNPINNED=1, and `_build.json` then says `"pinned": false`: such a build is for
development, not for managed PCs.

Signing: with `TWINLAB_SIGN_CERT` set to the SHA-1 thumbprint of a code-signing certificate in
the user's or machine's store, the program and Playwright's `node.exe` are signed with signtool
(Windows SDK) and timestamped (`TWINLAB_SIGN_TIMESTAMP`). Unsigned, a program with low-level
keyboard hooks is likely to be stopped by endpoint protection on a managed PC.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def main() -> int:
    sys.path.insert(0, str(PROJECT))
    from agent import twinlab as pin
    from agent.settings import VERSION
    from recorder_contract import CONTRACT_VERSION, SCHEMA_PATH

    override = os.environ.get("TWINLAB_PATH")
    if override:
        if os.environ.get("TWINLAB_ALLOW_UNPINNED") != "1":
            raise SystemExit("TWINLAB_PATH builds from an unpinned checkout: set TWINLAB_ALLOW_UNPINNED=1 "
                             "for a development build, or unset it to build from twinlab.lock.json")  # fmt: skip
        twinlab = Path(override)
        labctl = twinlab / "lab" / "labctl"
        if not (labctl / "labctl" / "workstation" / "session.py").is_file():
            raise SystemExit(f"no labctl.workstation in {twinlab}")
        commit = subprocess.run(["git", "-C", str(twinlab), "rev-parse", "HEAD"], capture_output=True,
                                text=True).stdout.strip()  # fmt: skip
        dirty = bool(subprocess.run(["git", "-C", str(twinlab), "status", "--porcelain", "--",
                                     "lab/labctl/labctl"], capture_output=True, text=True).stdout.strip())  # fmt: skip
        digest, pinned = pin.tree_digest(labctl / "labctl"), False
    else:
        lock = pin.lock()
        try:
            pin.verify(lock)
        except pin.PinError as exc:
            raise SystemExit(str(exc)) from exc
        labctl, commit, dirty = pin.labctl_parent(lock), lock["commit"], False
        digest, pinned = lock["tree_sha256"], True
    sys.path.insert(0, str(labctl))
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")  # writes comtypes.gen.UIAutomationClient
    info = {"version": VERSION, "contract_version": CONTRACT_VERSION, "twinlab_commit": commit or None,
            "twinlab_tree_sha256": digest, "pinned": pinned, "twinlab_dirty": dirty,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}  # fmt: skip
    (HERE / "_build.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    work = PROJECT / "build"
    dist = Path(os.environ.get("TWINLAB_DIST") or PROJECT / "dist")
    shutil.rmtree(dist / "TwinLabRecorder", ignore_errors=True)
    args = [
        sys.executable, "-m", "PyInstaller", str(HERE / "main.py"), "--name", "TwinLabRecorder",
        "--onedir", "--console", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--paths", str(labctl), "--paths", str(PROJECT),
        "--collect-submodules", "labctl.workstation", "--collect-submodules", "labctl.desktop",
        "--collect-submodules", "labctl.behaviour",
        "--hidden-import", "labctl.recorder", "--hidden-import", "comtypes.gen.UIAutomationClient",
        "--collect-submodules", "comtypes.gen",
        "--collect-data", "playwright", "--collect-binaries", "playwright",
        "--add-data", f"{HERE / '_build.json'}{os.pathsep}.",
        "--add-data", f"{SCHEMA_PATH}{os.pathsep}contract", "--hidden-import", "recorder_contract",
        "--exclude-module", "matplotlib", "--exclude-module", "IPython", "--exclude-module", "pytest",
    ]  # fmt: skip
    print(" ".join(args))
    subprocess.run(args, check=True, cwd=PROJECT)
    exe = dist / "TwinLabRecorder" / "TwinLabRecorder.exe"
    thumb = os.environ.get("TWINLAB_SIGN_CERT")  # a code-signing certificate's SHA-1 thumbprint
    if thumb:
        signtool = shutil.which("signtool") or "signtool"
        stamp = os.environ.get("TWINLAB_SIGN_TIMESTAMP", "http://timestamp.digicert.com")
        for target in [exe, *(dist / "TwinLabRecorder").rglob("node.exe")]:
            subprocess.run([signtool, "sign", "/sha1", thumb, "/fd", "SHA256", "/tr", stamp,
                            "/td", "SHA256", str(target)], check=True)  # fmt: skip
        print(f"signed with certificate {thumb[:8]}...")
    else:
        print("not signed: set TWINLAB_SIGN_CERT to a code-signing certificate's thumbprint")
    size = sum(f.stat().st_size for f in (dist / "TwinLabRecorder").rglob("*") if f.is_file())
    print(f"built {exe} ({size / 1e6:.0f} MB); Twin Lab {commit[:12]}{' (with uncommitted changes)' if dirty else ''}"
          f"{'' if pinned else ' NOT PINNED'}; recorder contract {CONTRACT_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
