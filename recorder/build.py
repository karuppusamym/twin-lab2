"""Build the portable TwinLabRecorder folder with PyInstaller.

    pip install -e contract -e "recorder[build]"
    python recorder/build.py         -> dist/TwinLabRecorder/TwinLabRecorder.exe

The folder is the program: copy it to a Windows PC and run TwinLabRecorder.exe (no install, no
administrator rights). It carries Python, the recorder and its collectors (`twinlab2_recorder`:
workstation session, desktop, web, mainframe), the recording format and recorder contract it
shares with the portal (`twinlab2_contract`, with the contract's schema), UI Automation's
wrapper generated beforehand (comtypes.gen: the program's folder may be read-only), Playwright's
driver, and `_build.json` (the recorder and contract versions, the Twin Lab commit the
collectors were copied from, and this repository's commit). No browser: it uses the Edge on the
PC.

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

HERE = Path(__file__).resolve().parent  # recorder/
PROJECT = HERE.parent  # the checkout
PACKAGE = HERE / "twinlab2_recorder"


def main() -> int:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(PROJECT / "contract"))
    from twinlab2_recorder.settings import VERSION
    from twinlab2_contract.recorder_contract import CONTRACT_VERSION
    from twinlab2_contract.upstream import UPSTREAM_COMMIT

    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")  # writes comtypes.gen.UIAutomationClient
    git = subprocess.run(["git", "-C", str(PROJECT), "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = bool(subprocess.run(["git", "-C", str(PROJECT), "status", "--porcelain", "--", "recorder", "contract"],
                                capture_output=True, text=True).stdout.strip())  # fmt: skip
    info = {"version": VERSION, "contract_version": CONTRACT_VERSION, "twinlab_commit": UPSTREAM_COMMIT,
            "source_commit": git.stdout.strip() or None, "source_dirty": dirty,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}  # fmt: skip
    (PACKAGE / "_build.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    work = PROJECT / "build"
    dist = Path(os.environ.get("TWINLAB_DIST") or PROJECT / "dist")
    shutil.rmtree(dist / "TwinLabRecorder", ignore_errors=True)
    args = [
        sys.executable, "-m", "PyInstaller", str(PACKAGE / "main.py"), "--name", "TwinLabRecorder",
        "--onedir", "--console", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--paths", str(HERE), "--paths", str(PROJECT / "contract"),
        # the collectors are imported lazily (and by name in the collector processes): all of them
        "--collect-submodules", "twinlab2_recorder", "--collect-submodules", "twinlab2_contract",
        "--collect-data", "twinlab2_contract",  # recorder-contract.schema.json beside its module
        "--hidden-import", "comtypes.gen.UIAutomationClient", "--collect-submodules", "comtypes.gen",
        "--collect-data", "playwright", "--collect-binaries", "playwright",
        "--add-data", f"{PACKAGE / '_build.json'}{os.pathsep}.",
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
    print(f"built {exe} ({size / 1e6:.0f} MB); source {(info['source_commit'] or 'unknown')[:12]}"
          f"{' (with uncommitted changes)' if dirty else ''}; recorder contract {CONTRACT_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
