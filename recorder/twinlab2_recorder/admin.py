"""What IT runs: the self-check of an installed recorder, the uninstaller's clean-up, the policy.

    TwinLabRecorder selfcheck            every module of the build imports; exit 0 when it does
    TwinLabRecorder policy               the machine policy and the network settings in effect
    TwinLabRecorder cleanup              delete this user's recorder data
    TwinLabRecorder cleanup --all-users  every profile's and ProgramData's (the MSI's uninstall, as SYSTEM)

The clean-up deletes, per profile, `%LOCALAPPDATA%\\TwinLabRecorder` (settings, the paired
token, recordings, the upload queue, the kept browser profile, logs) and the OCR helper script in
its temp folder; with `--all-users` also `%ProgramData%\\TwinLabRecorder` (the policy file).
Nothing else is written by the recorder (docs/recorder-deployment.md, What is collected).
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import sys
from pathlib import Path

from twinlab2_recorder import settings as st

# imported by name at run time (collector processes, lazily): a build that lost one cannot record
RUNTIME_MODULES = ["playwright.sync_api", "pydantic", "pypdf", "yaml", "tkinter", "ssl",
                   "multiprocessing", "twinlab2_contract.recorder_contract", "twinlab2_contract.evidence"]  # fmt: skip
WINDOWS_MODULES = ["comtypes.client", "comtypes.gen.UIAutomationClient"]


def selfcheck() -> dict:
    """Import every recorder module and the runtime dependencies; the failures."""
    import twinlab2_recorder

    names = [m.name for m in pkgutil.walk_packages(twinlab2_recorder.__path__, "twinlab2_recorder.")]
    names += RUNTIME_MODULES + (WINDOWS_MODULES if sys.platform == "win32" else [])
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - reported
            failed[name] = f"{type(exc).__name__}: {exc}"
    from twinlab2_recorder.workstation.preflight import web_channel_missing

    return {"modules": len(names), "failed": failed, "web_channel": web_channel_missing() or "installed",
            "frozen": bool(getattr(sys, "frozen", False)), "version": st.VERSION}  # fmt: skip


def _profile_dirs() -> list[Path]:
    """Every user profile folder on this PC (the registry's ProfileList, else C:\\Users\\*)."""
    found: list[Path] = []
    if sys.platform == "win32":
        import winreg

        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                i = 0
                while True:
                    try:
                        sid = winreg.EnumKey(handle, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(handle, sid) as sub:
                            path = winreg.QueryValueEx(sub, "ProfileImagePath")[0]
                            found.append(Path(os.path.expandvars(path)))
                    except OSError:
                        continue
        except OSError:
            pass
    users = Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users"
    if users.is_dir():
        found += [p for p in users.iterdir() if p.is_dir()]
    return list(dict.fromkeys(found))


def data_targets(all_users: bool, profiles: list[Path] | None = None, program_data: Path | None = None,
                 local: Path | None = None) -> list[Path]:  # fmt: skip
    """The folders and files the clean-up deletes (only what exists)."""
    targets: list[Path] = []
    if all_users:
        for profile in profiles if profiles is not None else _profile_dirs():
            targets += [profile / "AppData" / "Local" / st.APP,
                        profile / "AppData" / "Local" / "Temp" / "twinlab-ocr.ps1"]  # fmt: skip
        base = program_data or Path(os.environ.get("ProgramData") or r"C:\ProgramData")
        targets.append(base / st.APP)
        targets.append(st.program_dir() / "data")  # a portable run's data beside the program
    else:
        base = local or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        targets.append(base / st.APP)
        import tempfile

        targets.append(Path(tempfile.gettempdir()) / "twinlab-ocr.ps1")
    return [t for t in dict.fromkeys(targets) if t.exists()]


def cleanup(all_users: bool = False, **where) -> dict:
    removed, failed = [], {}
    for target in data_targets(all_users, **where):
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(str(target))
        except OSError as exc:
            failed[str(target)] = str(exc)
    return {"removed": removed, "failed": failed}


def policy_report(ca_bundle: str | None = None, proxy: str | None = None, url: str | None = None) -> dict:
    from twinlab2_recorder import net, policy

    pol = policy.load()
    network = net.Network.resolve(ca_bundle, proxy, pol)
    out = {"policy": pol.as_dict(), "network": network.describe()}
    if url:
        out["proxy_for"] = {url: network.proxy_for(url) or "direct"}
    sysp = net.system_proxy_config()
    out["system_proxy"] = {"auto_detect": sysp.auto_detect, "pac_url": sysp.pac_url, "proxy": sysp.proxy,
                           "bypass": sysp.bypass}  # fmt: skip
    return out


def print_json(doc: dict) -> None:
    print(json.dumps(doc, indent=1))
