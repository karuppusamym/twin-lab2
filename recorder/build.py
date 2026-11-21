"""Build the TwinLabRecorder folder with PyInstaller (and, with --msi, the installer).

    pip install -e contract -e "recorder[build]"
    python recorder/build.py         -> dist/TwinLabRecorder/TwinLabRecorder.exe
    python recorder/build.py --msi   -> also dist/TwinLabRecorder-<version>-x64.msi (WiX 5,
                                        recorder/installer/build_msi.py)

The folder is the program: copy it to a Windows PC and run TwinLabRecorder.exe (no install, no
administrator rights), or install the MSI per machine (docs/recorder-deployment.md). It carries
Python, the recorder and its collectors (`twinlab2_recorder`: workstation session, desktop, web,
mainframe), the recording format and recorder contract it shares with the portal
(`twinlab2_contract`, with the contract's schema), UI Automation's wrapper generated beforehand
(comtypes.gen: the program's folder may be read-only), Playwright's driver (the web channel), and
`_build.json` (the recorder and contract versions, the Twin Lab commit the collectors were copied
from, and this repository's commit). No browser: it uses the Edge on the PC.

Size: modules the recorder never imports, which PyInstaller follows through optional imports of
its dependencies, are left out (`EXCLUDES`: numpy through comtypes, cryptography and PyCryptodome
through pypdf, setuptools, httpx/dnspython through pydantic's e-mail check, test and doc tools),
as are the files of Playwright's driver that only its own tooling reads (`DRIVER_CUT`). The
build prints and writes `size.json`: the folder, the web channel (Playwright's driver, about 90
of it, an optional feature of the MSI) and the largest parts.

UPX is never used (`--noupx`, even when upx is on PATH): a packed executable or DLL is a classic
false positive for endpoint protection on managed PCs, packing a signed file breaks its
signature, and the MSI compresses the files anyway.

Signing (recorder/installer/sign.py): with `TWINLAB_SIGN_CERT` (a code-signing certificate's
SHA-1 thumbprint in the user's or machine's store) or `TWINLAB_SIGN_PFX` (+ `_PASSWORD`), the
program, Playwright's `node.exe` and the MSI are signed with signtool and timestamped
(`TWINLAB_SIGN_TIMESTAMP`). Without one the build says it is unsigned and goes on: the office
supplies its certificate. Unsigned, a program with low-level keyboard hooks is likely to be
stopped by endpoint protection on a managed PC.
"""

from __future__ import annotations

import argparse
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
CONTRACT_SCHEMA = PROJECT / "contract" / "twinlab2_contract" / "recorder-contract.schema.json"

# Never imported by the recorder; reached only through optional imports of its dependencies.
EXCLUDES = [
    "numpy",  # comtypes needs its _npsupport module, which guards the numpy import
    "cryptography", "Crypto", "cffi", "pypdf._crypt_providers._cryptography",  # pypdf: encrypted PDFs
    "pypdf._crypt_providers._pycryptodome",
    "setuptools", "pkg_resources", "distutils", "_distutils_hack",
    "httpx", "httpcore", "anyio", "h11", "sniffio", "dns", "email_validator", "certifi",  # pydantic EmailStr
    "pydantic.v1", "pydantic.mypy", "mypy", "toml", "orjson", "tzdata",
    "rich", "pygments", "markdown_it", "mdurl", "chardet", "charset_normalizer", "zstandard",
    "PIL", "pytesseract",  # only for Tesseract OCR; the built recorder uses Windows' own OCR (ocr.py)
    "psutil", "IPython", "matplotlib", "pytest", "_pytest", "doctest", "pdb", "unittest",
    "xmlrpc", "lib2to3", "pyreadline3", "tkinter.test", "test", "pydoc_data",
]  # fmt: skip
# Files of Playwright's driver its runtime never reads (`print-api-json`, TypeScript types).
DRIVER_CUT = ["package/api.json", "package/types", "package/index.d.ts", "package/README.md"]


def driver_dir(folder: Path) -> Path:
    return folder / "_internal" / "playwright" / "driver"


def tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def size_report(folder: Path) -> dict:
    internal = folder / "_internal"
    parts = sorted(((p.name, tree_size(p)) for p in internal.iterdir()), key=lambda x: -x[1])
    total, web = tree_size(folder), tree_size(driver_dir(folder)) if driver_dir(folder).exists() else 0
    return {"total_mb": round(total / 1e6, 1), "web_channel_mb": round(web / 1e6, 1),
            "core_mb": round((total - web) / 1e6, 1), "files": sum(1 for f in folder.rglob("*") if f.is_file()),
            "largest_mb": {name: round(size / 1e6, 1) for name, size in parts[:12]}}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--msi", action="store_true", help="also build the MSI (needs the wix tool, WiX 5)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(PROJECT / "contract"))
    from twinlab2_recorder.settings import VERSION
    from twinlab2_contract.recorder_contract import CONTRACT_VERSION
    from twinlab2_contract.upstream import UPSTREAM_COMMIT
    from installer import sign

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
    folder = dist / "TwinLabRecorder"
    shutil.rmtree(folder, ignore_errors=True)
    args_pi = [
        sys.executable, "-m", "PyInstaller", str(PACKAGE / "main.py"), "--name", "TwinLabRecorder",
        "--onedir", "--console", "--noconfirm", "--clean", "--noupx",
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--paths", str(HERE), "--paths", str(PROJECT / "contract"),
        # the collectors are imported lazily (and by name in the collector processes): all of them
        "--collect-submodules", "twinlab2_recorder", "--collect-submodules", "twinlab2_contract",
        # recorder-contract.schema.json beside its module, from THIS checkout only: collect-data
        # would take it from wherever the contract package is pip-installed (an editable install
        # of another checkout), and that schema won over this one (bundled_schema_version)
        "--add-data", f"{CONTRACT_SCHEMA}{os.pathsep}twinlab2_contract",
        "--hidden-import", "comtypes.gen.UIAutomationClient", "--collect-submodules", "comtypes.gen",
        "--collect-data", "playwright", "--collect-binaries", "playwright",
        "--add-data", f"{PACKAGE / '_build.json'}{os.pathsep}.",
    ]  # fmt: skip
    for name in EXCLUDES:
        args_pi += ["--exclude-module", name]
    print(" ".join(args_pi))
    subprocess.run(args_pi, check=True, cwd=PROJECT)
    bundled = bundled_schema_version(folder)
    if bundled != CONTRACT_VERSION:
        raise SystemExit(f"the program carries recorder contract schema {bundled}, its code speaks "
                         f"{CONTRACT_VERSION}: it would refuse to send. Rebuild from one checkout.")
    for cut in DRIVER_CUT:
        target = driver_dir(folder) / cut
        shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
    exe = folder / "TwinLabRecorder.exe"
    signed = sign.sign_files([exe, *folder.rglob("node.exe")])
    report = size_report(folder)
    report.update(version=VERSION, signed=signed)
    (dist / "size.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"built {exe} ({report['total_mb']:.0f} MB: core {report['core_mb']:.0f} MB, web channel "
          f"{report['web_channel_mb']:.0f} MB); source {(info['source_commit'] or 'unknown')[:12]}"
          f"{' (with uncommitted changes)' if dirty else ''}; recorder contract {CONTRACT_VERSION}"
          f"; {'signed' if signed else 'not signed'}")  # fmt: skip
    print("largest: " + ", ".join(f"{k} {v} MB" for k, v in report["largest_mb"].items()))
    if args.msi:
        from installer import build_msi

        return build_msi.main(["--folder", str(folder), "--out", str(dist)])
    return 0


def bundled_schema_version(folder: Path) -> str | None:
    """The `x-contract-version` of the recorder contract schema a built program carries."""
    found = sorted(Path(folder).rglob("recorder-contract.schema.json"))
    if len(found) != 1:
        return None
    try:
        return json.loads(found[0].read_text(encoding="utf-8")).get("x-contract-version")
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
