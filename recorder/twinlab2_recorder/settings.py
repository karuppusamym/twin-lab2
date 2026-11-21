"""Where the recorder keeps its settings, token and recordings, and how the token is protected.

Data folder: `%LOCALAPPDATA%\\TwinLabRecorder\\`, or `data\\` beside the program when it runs
with `--portable-data` (or a file `portable.txt` beside it). Nothing is written anywhere else.

The machine token is protected with Windows DPAPI for the current user (`CryptProtectData`):
a copied folder does not carry a usable token to another machine or another user, who pairs
again. Off Windows (tests), the token is kept as it is, and the settings say so.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

APP = "TwinLabRecorder"
VERSION = "0.2.0"  # 0.2.0: the first that declares a recorder contract version


def program_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir(portable: bool = False) -> Path:
    if portable or (program_dir() / "portable.txt").is_file():
        base = program_dir() / "data"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP
    base.mkdir(parents=True, exist_ok=True)
    (base / "recordings").mkdir(exist_ok=True)
    return base


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    entropy_raw = b"TwinLabRecorder machine token"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = _Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (ctypes.byref(inp), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out))
    if protect:
        args = (ctypes.byref(inp), "TwinLabRecorder", ctypes.byref(entropy), None, None, 0x1,
                ctypes.byref(out))  # fmt: skip
    if not fn(*args):
        raise OSError("DPAPI failed: pair this machine again")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(token: str) -> dict:
    if sys.platform == "win32":
        return {"dpapi": base64.b64encode(_dpapi(token.encode(), True)).decode()}
    return {"plain": token}


def unprotect(stored: dict | None) -> str | None:
    if not stored:
        return None
    if "dpapi" in stored and sys.platform == "win32":
        return _dpapi(base64.b64decode(stored["dpapi"]), False).decode()
    return stored.get("plain")


class Settings:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.portable = False
        self.path = self.folder / "settings.json"
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.values = {}

    @property
    def recordings(self) -> Path:
        return self.folder / "recordings"

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def paired(self, portal: str, machine_id: int, name: str, token: str) -> None:
        self.values.update(portal=portal, machine_id=machine_id, machine=name, token=protect(token))
        self.save()

    def token(self) -> str | None:
        return unprotect(self.values.get("token"))

    # -- the exclude list (twinlab2_recorder.workstation.exclude) ---------------------------------

    def exclude_policy(self, managed: Path | None = None):
        """This PC's exclude list: the managed policy file's locked entries, then the person's own
        (the defaults until they change it)."""
        from twinlab2_recorder.workstation import exclude

        return exclude.load(self.values, managed)

    def set_exclude_rules(self, rules) -> None:
        """Keep the person's own entries (managed ones come from their file, never from here)."""
        from twinlab2_recorder.workstation import exclude

        exclude.store(self.values, list(rules))
        self.save()

    def forget(self) -> None:
        for k in ("portal", "machine_id", "machine", "token"):
            self.values.pop(k, None)
        self.save()
