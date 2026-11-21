"""The code-signing hook of the recorder's build: signtool when the office gives a certificate.

    TWINLAB_SIGN_CERT       SHA-1 thumbprint of a code-signing certificate in the current user's
                            or the machine's certificate store (a hardware token or an HSM-backed
                            certificate shows up there too), or
    TWINLAB_SIGN_PFX        a .pfx file, with TWINLAB_SIGN_PFX_PASSWORD (a CI secret)
    TWINLAB_SIGN_TIMESTAMP  RFC 3161 timestamp server (default http://timestamp.digicert.com)
    TWINLAB_SIGNTOOL        signtool.exe, when it is not on PATH (found in the Windows SDK too)

Without a certificate nothing is signed and the build says so: this repository has no
certificate, the office that deploys the recorder supplies its own (docs/recorder-deployment.md,
Code signing). Signed files are checked with `signtool verify /pa` afterwards.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path


def configured() -> bool:
    return bool(os.environ.get("TWINLAB_SIGN_CERT") or os.environ.get("TWINLAB_SIGN_PFX"))


def signtool() -> str:
    found = os.environ.get("TWINLAB_SIGNTOOL") or shutil.which("signtool")
    if found:
        return found
    kits = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    if kits:
        return kits[-1]
    raise SystemExit("signing asked for (TWINLAB_SIGN_CERT / TWINLAB_SIGN_PFX), but signtool.exe was not "
                     "found: install the Windows SDK or set TWINLAB_SIGNTOOL")  # fmt: skip


def command(target: Path, tool: str = "signtool") -> list[str]:
    """The signtool command line for one file (no secrets in it except a PFX password, which is
    passed from the environment and never printed)."""
    stamp = os.environ.get("TWINLAB_SIGN_TIMESTAMP", "http://timestamp.digicert.com")
    cmd = [tool, "sign", "/fd", "SHA256", "/tr", stamp, "/td", "SHA256"]
    if os.environ.get("TWINLAB_SIGN_CERT"):
        cmd += ["/sha1", os.environ["TWINLAB_SIGN_CERT"].replace(" ", "")]
    else:
        cmd += ["/f", os.environ["TWINLAB_SIGN_PFX"]]
        if os.environ.get("TWINLAB_SIGN_PFX_PASSWORD"):
            cmd += ["/p", os.environ["TWINLAB_SIGN_PFX_PASSWORD"]]
    return cmd + ["/d", "TwinLab Recorder", str(target)]


def sign_files(targets: list[Path]) -> bool:
    """Sign and verify each file; False (and a note) when no certificate is configured."""
    if not configured():
        print("not signed: set TWINLAB_SIGN_CERT (a certificate thumbprint) or TWINLAB_SIGN_PFX; the office "
              "deploying the recorder supplies the certificate")  # fmt: skip
        return False
    tool = signtool()
    for target in targets:
        subprocess.run(command(Path(target), tool), check=True)
        subprocess.run([tool, "verify", "/pa", "/q", str(target)], check=True)
    print(f"signed {len(targets)} file(s)")
    return True
