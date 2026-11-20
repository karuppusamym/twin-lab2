"""Build the portable TwinLabRecorder folder with PyInstaller.

    set TWINLAB_PATH=<twin-lab checkout with labctl.workstation>
    python agent/build.py            -> dist/TwinLabRecorder/TwinLabRecorder.exe

The folder is the program: copy it to a Windows PC and run TwinLabRecorder.exe (no install, no
administrator rights). It carries Python, Twin Lab's collectors (labctl), UI Automation's
wrapper generated beforehand (comtypes.gen: the program's folder may be read-only), Playwright's
driver, and `_build.json` (the Twin Lab commit). No browser: it uses the Edge on the PC.

The build is not signed. Sign it before it runs on any machine other than a developer's: an
unsigned program with low-level keyboard hooks is likely to be stopped by endpoint protection.
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
    twinlab = Path(os.environ.get("TWINLAB_PATH") or PROJECT.parent / "twin-lab")
    labctl = twinlab / "lab" / "labctl"
    if not (labctl / "labctl" / "workstation" / "session.py").is_file():
        raise SystemExit(f"no labctl.workstation in {twinlab}: set TWINLAB_PATH")
    sys.path.insert(0, str(labctl))
    sys.path.insert(0, str(PROJECT))
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")  # writes comtypes.gen.UIAutomationClient
    commit = subprocess.run(["git", "-C", str(twinlab), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()  # fmt: skip
    dirty = subprocess.run(["git", "-C", str(twinlab), "status", "--porcelain", "--", "lab/labctl/labctl"],
                           capture_output=True, text=True).stdout.strip()  # fmt: skip
    from agent.settings import VERSION

    info = {"version": VERSION, "twinlab_commit": commit or None, "twinlab_dirty": bool(dirty),
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
        "--exclude-module", "matplotlib", "--exclude-module", "IPython", "--exclude-module", "pytest",
    ]  # fmt: skip
    print(" ".join(args))
    subprocess.run(args, check=True, cwd=PROJECT)
    exe = dist / "TwinLabRecorder" / "TwinLabRecorder.exe"
    size = sum(f.stat().st_size for f in (dist / "TwinLabRecorder").rglob("*") if f.is_file())
    print(f"built {exe} ({size / 1e6:.0f} MB); Twin Lab {commit[:12]}{' (with uncommitted changes)' if dirty else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
