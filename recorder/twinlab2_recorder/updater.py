"""Updates: the recorder asks its portal for a newer version, checks it, and installs it when allowed.

    TwinLabRecorder update             is there a newer version? (the portal's answer)
    TwinLabRecorder update --install   download, check and install it (asks first; --yes)

The portal says which version is current (`GET /api/agent/update`, api/openapi.yaml
`RecorderUpdate`; it serves the MSI an administrator put in its releases folder). The machine
policy (twinlab2_recorder.policy `updates`) decides what the recorder does with it:

- `off`: never asks (the office pushes new versions itself, with Intune or SCCM);
- `notify` (the default): the window says a version is available; the person installs it;
- `auto`: downloaded, checked and installed silently while nothing is being recorded, when the
  recorder runs with the rights to install a per-machine MSI; otherwise as `notify`.

Before anything is installed, the downloaded file must:

1. have exactly the size and the SHA-256 the portal announced (a download is never trusted
   half-way: it is written to `updates\\<file>.part`, checked, then renamed);
2. carry a valid Authenticode signature (Windows' own check, `Get-AuthenticodeSignature`); with
   `update_signers` in the policy, by one of those certificates. An unsigned MSI is accepted only
   when the policy says `allow_unsigned_updates` (a trial without the office's certificate);
   a signature that is present but not valid is always refused.

Never during a recording: `install()` refuses while a session records (`recording_active`: the
lock a running session holds, `recording.lock` in the data folder, and the window's own state),
and the window waits until the recording has ended.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from twinlab2_recorder import policy as machine
from twinlab2_recorder import settings as st
from twinlab2_recorder.client import PortalError

LOCK = "recording.lock"
FILE_RE = re.compile(r"TwinLabRecorder-\d+\.\d+\.\d+(-x64)?\.msi")
CHUNK = 1024 * 1024
MAX_BYTES = 1024 * 1024 * 1024


class UpdateRefused(Exception):
    """The update cannot be installed now, or not at all; the message says why."""


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(candidate: str, current: str = st.VERSION) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


# -- "never during a recording" ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    from twinlab2_recorder.workstation.session import _alive as alive

    return alive(pid)


def take_lock(folder: Path) -> Path:
    """Written by a recording session (main.run_session) for as long as it records."""
    path = Path(folder) / LOCK
    path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    return path


def release_lock(folder: Path) -> None:
    path = Path(folder) / LOCK
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def recording_active(folder: Path) -> bool:
    """Does a live process record a session in this data folder? (A lock left by a process that
    died is not a recording.)"""
    try:
        pid = json.loads((Path(folder) / LOCK).read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(pid, int) and pid > 0 and _alive(pid)


# -- the offer ------------------------------------------------------------------------------------------------


@dataclass
class Offer:
    version: str
    file: str
    url: str
    size: int
    sha256: str
    notes: str = ""
    published_at: str | None = None

    @classmethod
    def from_answer(cls, doc: dict) -> Offer | None:
        """The portal's `latest`, checked (a malformed one is refused, not half-used)."""
        latest = doc.get("latest") if isinstance(doc, dict) else None
        if not isinstance(latest, dict):
            return None
        ok = (parse_version(latest.get("version")) and isinstance(latest.get("file"), str)
              and FILE_RE.fullmatch(latest["file"]) and isinstance(latest.get("url"), str)
              and isinstance(latest.get("size"), int) and 0 < latest["size"] <= MAX_BYTES
              and isinstance(latest.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", latest["sha256"]))  # fmt: skip
        if not ok:
            raise PortalError("the portal's update answer is malformed")
        return cls(latest["version"], latest["file"], latest["url"], latest["size"], latest["sha256"],
                   str(latest.get("notes") or "")[:2000], latest.get("published_at"))  # fmt: skip


def check(portal, pol: machine.MachinePolicy | None = None) -> Offer | None:
    """A newer version the portal offers, or None (also when the policy turns updates off)."""
    pol = pol or machine.load()
    if pol.updates == "off":
        return None
    offer = Offer.from_answer(portal.update_info(st.VERSION))
    return offer if offer and newer(offer.version) else None


# -- download and checks ----------------------------------------------------------------------------------------


def download(portal, offer: Offer, folder: Path) -> Path:
    """The MSI in `folder/updates/`, with the announced size and SHA-256 (else PortalError)."""
    out_dir = Path(folder) / "updates"
    out_dir.mkdir(parents=True, exist_ok=True)
    final, part = out_dir / offer.file, out_dir / (offer.file + ".part")
    if final.is_file() and final.stat().st_size == offer.size and _sha256(final) == offer.sha256:
        return final
    digest, size = hashlib.sha256(), 0
    with part.open("wb") as f:
        for chunk in portal.download(offer.url, max_bytes=offer.size):
            digest.update(chunk)
            size += len(chunk)
            f.write(chunk)
    if size != offer.size or digest.hexdigest() != offer.sha256:
        part.unlink(missing_ok=True)
        raise PortalError(f"the downloaded update does not match the portal's checksum ({size} bytes)")
    os.replace(part, final)
    return final


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


AUTHENTICODE = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:TWINLAB_SIGNED_FILE; "
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "@{status = [string]$s.Status; thumbprint = [string]$s.SignerCertificate.Thumbprint; "
    "subject = [string]$s.SignerCertificate.Subject} | ConvertTo-Json -Compress"
)


def authenticode(path: Path) -> dict:
    """Windows' verdict on a file's signature: {"status": Valid | NotSigned | HashMismatch | ...,
    "thumbprint", "subject"}."""
    if sys.platform != "win32":
        return {"status": "NotSigned", "thumbprint": "", "subject": ""}
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", AUTHENTICODE],
                         capture_output=True, timeout=60, check=False,
                         env={**os.environ, "TWINLAB_SIGNED_FILE": str(Path(path).resolve())},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # fmt: skip
    try:
        doc = json.loads(out.stdout.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        doc = {}
    return {"status": str(doc.get("status") or "UnknownError"), "thumbprint": str(doc.get("thumbprint") or ""),
            "subject": str(doc.get("subject") or "")}  # fmt: skip


def verify_signature(path: Path, pol: machine.MachinePolicy, verifier=authenticode) -> str:
    """'signed by <subject>' or 'unsigned (allowed by policy)'; UpdateRefused otherwise."""
    sig = verifier(path)
    status = sig.get("status")
    if status == "Valid":
        thumb = sig.get("thumbprint", "").upper()
        if pol.update_signers and thumb not in pol.update_signers:
            raise UpdateRefused(f"the update is signed by {sig.get('subject') or thumb}, which this PC's policy "
                                "does not name (update_signers)")  # fmt: skip
        return f"signed by {sig.get('subject') or thumb}"
    if status == "NotSigned":
        if pol.allow_unsigned_updates and not pol.update_signers:
            return "unsigned (allowed by policy)"
        raise UpdateRefused("the update is not signed; this PC accepts only signed updates")
    raise UpdateRefused(f"the update's signature is not valid ({status}): it is not installed")


# -- installing -----------------------------------------------------------------------------------------------------


def can_install_silently() -> bool:
    """A per-machine MSI installs without asking only for an administrator (or SYSTEM)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def install_command(msi: Path, quiet: bool, log: Path) -> list[str]:
    ui = ["/qn"] if quiet else ["/passive"]
    return ["msiexec", "/i", str(msi), *ui, "/norestart", "/l*v", str(log)]


def install(msi: Path, folder: Path, quiet: bool, busy=lambda: False, run=subprocess.Popen):
    """Start the installer (which replaces this program: the caller exits next). Refused while a
    recording runs."""
    if busy() or recording_active(folder):
        raise UpdateRefused("a recording is running: the update waits until it has ended")
    log = Path(folder) / "logs" / f"update-{msi.stem}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return run(install_command(msi, quiet, log), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def prepare(portal, cfg_folder: Path, pol: machine.MachinePolicy | None = None,
            verifier=authenticode) -> tuple[Offer, Path, str] | None:  # fmt: skip
    """check, download and verify: (offer, msi, signature note), or None when there is nothing newer."""
    pol = pol or machine.load()
    offer = check(portal, pol)
    if offer is None:
        return None
    msi = download(portal, offer, cfg_folder)
    try:
        note = verify_signature(msi, pol, verifier)
    except UpdateRefused:
        msi.unlink(missing_ok=True)
        raise
    return offer, msi, note


def describe(offer: Offer, pol: machine.MachinePolicy) -> str:
    how = {"auto": "installed automatically when nothing is recording", "notify": "ready to install"}
    return f"TwinLab Recorder {offer.version} is available ({how.get(pol.updates, 'off')}); this is {st.VERSION}"
