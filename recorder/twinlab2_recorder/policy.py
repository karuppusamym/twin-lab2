"""The office's machine policy: what IT sets for every person on a managed PC.

Two places, read every time a value is needed (a changed policy applies without a restart):

1. the registry, `HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\TwinLab\\Recorder` (Group Policy, Intune
   "custom OMA-URI"/ADMX ingestion, or a `reg add` in an SCCM package), and
2. `%ProgramData%\\TwinLabRecorder\\policy.json` (or the file `TWINLAB_RECORDER_POLICY` names),
   which also holds the locked exclude list (twinlab2_recorder.workstation.exclude).

A value in the registry wins over the same value in the file; both win over what the person sets
in the recorder (their settings.json) and over command-line options. Nothing here is written by the
recorder: only an administrator can change either place.

| policy.json            | registry value (type)                | meaning                                  |
|------------------------|--------------------------------------|------------------------------------------|
| `portal_url`           | `PortalUrl` (REG_SZ)                 | the office's portal; pre-filled to pair  |
| `lock_portal`          | `LockPortal` (REG_DWORD 1)           | pairing with any other portal is refused |
| `proxy`                | `Proxy` (REG_SZ)                     | `system` (default), `direct`, or a proxy |
|                        |                                      | URL `http://proxy.corp:8080`             |
| `proxy_bypass`         | `ProxyBypass` (REG_SZ, `;` between)  | hosts that go direct (`*.corp;10.*`)     |
| `ca_bundle`            | `CaBundle` (REG_SZ)                  | PEM file of extra root CAs (TLS          |
|                        |                                      | inspection), added to Windows' store     |
| `updates`              | `Updates` (REG_SZ)                   | `off`, `notify` (default), `auto`        |
| `update_signers`       | `UpdateSigners` (REG_MULTI_SZ)       | code-signing certificate thumbprints an  |
|                        |                                      | update must be signed with               |
| `allow_unsigned_updates` | `AllowUnsignedUpdates` (REG_DWORD) | accept an update without a signature     |
| `exclude`              | `Exclude` (REG_MULTI_SZ,             | locked exclude entries                   |
|                        | `kind:pattern` or `kind:pattern|name`) |                                        |
| `allow_user_changes`   | `AllowUserExcludeChanges` (REG_DWORD)| 0: the person's own list is locked too   |

A policy file that cannot be read is reported (`errors`) and applies nothing of its own, except
that the exclude list is then locked (exclude.read_managed): the office's intent is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys

REGISTRY_KEY = r"SOFTWARE\Policies\TwinLab\Recorder"
UPDATE_MODES = ("off", "notify", "auto")

# registry value -> (policy.json key, kind)
REGISTRY_VALUES = {
    "PortalUrl": ("portal_url", "str"),
    "LockPortal": ("lock_portal", "bool"),
    "Proxy": ("proxy", "str"),
    "ProxyBypass": ("proxy_bypass", "list"),
    "CaBundle": ("ca_bundle", "str"),
    "Updates": ("updates", "str"),
    "UpdateSigners": ("update_signers", "list"),
    "AllowUnsignedUpdates": ("allow_unsigned_updates", "bool"),
    "Exclude": ("exclude", "exclude"),
    "AllowUserExcludeChanges": ("allow_user_changes", "bool"),
}


def policy_path() -> Path:
    from twinlab2_recorder.workstation.exclude import managed_path

    return managed_path()


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace("\n", ";").split(";")
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and str(v).strip()]


def _exclude_entry(text: str) -> dict | None:
    """`process:WINWORD.EXE|Microsoft Word` -> an exclude entry (exclude.clean_rule checks it)."""
    kind, sep, rest = text.partition(":")
    if not sep or not rest.strip():
        return None
    pattern, _, name = rest.partition("|")
    entry = {"kind": kind.strip().lower(), "pattern": pattern.strip()}
    if name.strip():
        entry["name"] = name.strip()
    return entry


def convert(raw: dict, kinds: dict[str, str] | None = None) -> dict:
    """Registry values (name -> value as winreg reads it) as policy.json keys."""
    out = {}
    for name, value in raw.items():
        if name not in REGISTRY_VALUES:
            continue
        key, kind = REGISTRY_VALUES[name]
        if kind == "str" and isinstance(value, str) and value.strip():
            out[key] = value.strip()
        elif kind == "bool" and isinstance(value, (int, str)):
            out[key] = str(value).strip().lower() in ("1", "true", "yes")
        elif kind == "list":
            out[key] = _as_list(value)
        elif kind == "exclude":
            out[key] = [e for e in (_exclude_entry(t) for t in _as_list(value)) if e]
    return out


def read_registry(key: str = REGISTRY_KEY) -> dict:
    """The policy values under HKLM (raw, by registry name); {} off Windows or without the key."""
    if sys.platform != "win32":
        return {}
    import winreg

    found = {}
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            handle = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with handle:
            i = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(handle, i)
                except OSError:
                    break
                found.setdefault(name, value)
                i += 1
    return found


def read_file(path: Path | None = None) -> tuple[dict, str | None]:
    path = Path(path) if path is not None else policy_path()
    if not path.is_file():
        return {}, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, f"the policy file {path} cannot be read"
    if not isinstance(doc, dict):
        return {}, f"the policy file {path} is not an object"
    return doc, None


@dataclass
class MachinePolicy:
    portal_url: str | None = None
    lock_portal: bool = False
    proxy: str = "system"  # system | direct | a proxy URL
    proxy_bypass: list[str] = field(default_factory=list)
    ca_bundle: str | None = None
    updates: str = "notify"
    update_signers: list[str] = field(default_factory=list)
    allow_unsigned_updates: bool = False
    exclude: list[dict] = field(default_factory=list)
    allow_user_changes: bool | None = None
    managed: bool = False  # any policy was found
    sources: dict[str, str] = field(default_factory=dict)  # key -> "registry" | "file"
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_values(cls, file_doc: dict, registry: dict) -> MachinePolicy:
        pol = cls()
        merged: dict = {}
        for source, doc in (("file", file_doc), ("registry", registry)):
            for key, value in doc.items():
                if value is None or value == "" or value == []:
                    continue
                merged[key] = value
                pol.sources[key] = source
        pol.managed = bool(file_doc or registry)
        if isinstance(merged.get("portal_url"), str):
            pol.portal_url = merged["portal_url"].strip().rstrip("/") or None
        pol.lock_portal = merged.get("lock_portal") is True and bool(pol.portal_url)
        proxy = merged.get("proxy")
        if isinstance(proxy, str) and proxy.strip():
            proxy = proxy.strip()
            if proxy.lower() in ("system", "direct"):
                pol.proxy = proxy.lower()
            elif "://" in proxy or ":" in proxy:
                pol.proxy = proxy if "://" in proxy else "http://" + proxy
            else:
                pol.errors.append(f"the proxy policy {proxy!r} is not system, direct or host:port")
        pol.proxy_bypass = _as_list(merged.get("proxy_bypass"))
        if isinstance(merged.get("ca_bundle"), str) and merged["ca_bundle"].strip():
            pol.ca_bundle = os.path.expandvars(merged["ca_bundle"].strip())
        updates = merged.get("updates")
        if isinstance(updates, str) and updates.strip().lower() in UPDATE_MODES:
            pol.updates = updates.strip().lower()
        elif updates is not None:
            pol.errors.append(f"the updates policy {updates!r} is not one of {', '.join(UPDATE_MODES)}")
        pol.update_signers = [t.replace(" ", "").upper() for t in _as_list(merged.get("update_signers"))]
        pol.allow_unsigned_updates = merged.get("allow_unsigned_updates") is True
        pol.exclude = [e for e in merged.get("exclude") or [] if isinstance(e, dict)]
        if isinstance(merged.get("allow_user_changes"), bool):
            pol.allow_user_changes = merged["allow_user_changes"]
        return pol

    def as_dict(self) -> dict:
        return {"portal_url": self.portal_url, "lock_portal": self.lock_portal, "proxy": self.proxy,
                "proxy_bypass": self.proxy_bypass, "ca_bundle": self.ca_bundle, "updates": self.updates,
                "update_signers": self.update_signers, "allow_unsigned_updates": self.allow_unsigned_updates,
                "exclude": len(self.exclude), "allow_user_changes": self.allow_user_changes,
                "managed": self.managed, "sources": self.sources, "errors": self.errors}  # fmt: skip


def load(path: Path | None = None, registry: dict | None = None) -> MachinePolicy:
    """The machine policy now. `path`/`registry` for tests (registry: raw values by name)."""
    file_doc, error = read_file(path)
    raw = read_registry() if registry is None else registry
    pol = MachinePolicy.from_values(file_doc, convert(raw))
    if error:
        pol.errors.insert(0, error)
        pol.managed = True
    return pol


def check_portal_allowed(pol: MachinePolicy, url: str) -> None:
    """Refuse pairing with another portal than the office's, when the policy locks it."""
    from twinlab2_recorder.client import PortalError, check_portal

    if pol.lock_portal and pol.portal_url and check_portal(url) != check_portal(pol.portal_url):
        raise PortalError(f"this PC's policy allows only the portal {pol.portal_url}")
