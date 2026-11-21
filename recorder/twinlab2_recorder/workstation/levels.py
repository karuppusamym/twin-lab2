# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Who an application is, and how much of it a session records.

Identity (`desktop_identity`, `web_identity`): a desktop application is its company and product
from the version resource (`desktop:<company>/<product>`), or its executable name when it has
none (`desktop:exe/<name>`); a web application is its origin (`web:<origin>`). The executable
name, version and path are facts, not identity, so a new version or install folder is the same
application.

Levels: `full` (everything the recorders keep), `steps` (desktop steps without tree files or
pictures), `presence` (that it was used, nothing else). `Targets.level` gives an application's
level in a session: the deny list and host processes are presence unless a target names them
explicitly with `allow_denied: true`; a target's own level otherwise; presence for everything
else.
"""

from __future__ import annotations

import fnmatch
import ntpath
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

FULL, STEPS, PRESENCE = "full", "steps", "presence"
LEVELS = (FULL, STEPS, PRESENCE)

# Executables that run many applications under one name: never an application by themselves.
HOSTS = {
    "java.exe", "javaw.exe", "electron.exe", "python.exe", "pythonw.exe", "dotnet.exe",
    "node.exe", "applicationframehost.exe", "msedgewebview2.exe", "rundll32.exe", "dllhost.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "windowsterminal.exe", "wscript.exe",
    "cscript.exe", "mshta.exe",
}  # fmt: skip
# Browsers are never applications: only the Edge a session launched is recorded, per origin.
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe"}
# Always presence, never registered (case-insensitive file name patterns).
DENY_EXE = [
    "explorer.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "lockapp.exe", "taskmgr.exe", "systemsettings.exe",
    "textinputhost.exe", "sihost.exe", "ctfmon.exe", "mmc.exe", "regedit.exe",
    "acrobat.exe", "acrord32.exe", "acrocef.exe", "adobe*.exe",
    "outlook.exe", "olk.exe", "ms-teams.exe", "msteams.exe", "teams.exe", "lync.exe",
    "1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe", "lastpass.exe",
    "dashlane.exe", "twinlabrecorder.exe", "uipath*.exe", "uirobot.exe", "uiexplorer.exe",
]  # fmt: skip
DENY_DIRS = ["c:\\windows\\"]
DENY_ORIGINS = [
    "https://outlook.office.com", "https://outlook.office365.com", "https://outlook.live.com",
    "https://teams.microsoft.com", "https://teams.live.com", "https://login.microsoftonline.com",
    "https://login.live.com", "https://accounts.google.com", "https://mail.google.com",
]  # fmt: skip


def _clean(text: object, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def desktop_identity(path: str | None, exe: dict | None = None) -> dict:
    """`{"key", "surface", "exe", "company", "product", "version", "display", "host",
    "browser"}` of a process from its image path and version resource
    (twinlab2_recorder.desktop.win.exe_info). The path itself is not kept."""
    exe = exe or {}
    name = ntpath.basename(path or "") or _clean(exe.get("file")) or "unknown.exe"
    company, product = _clean(exe.get("company")), _clean(exe.get("product"))
    if company and product:
        key = f"desktop:{company}/{product}"
    else:
        key = f"desktop:exe/{name.lower()}"
    return {
        "key": key, "surface": "desktop", "exe": name, "company": company or None,
        "product": product or None, "version": _clean(exe.get("product_version"), 40) or None,
        "display": product or _clean(exe.get("description")) or ntpath.splitext(name)[0],
        "description": _clean(exe.get("description")) or None,
        "host": name.lower() in HOSTS, "browser": name.lower() in BROWSERS,
    }  # fmt: skip


def origin_of(url: str) -> str:
    """Scheme, host and port of an address, lower case, without user information."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    default = {"http": 80, "https": 443}[parts.scheme]
    try:
        port = parts.port
    except ValueError:
        return ""
    netloc = host if port in (None, default) else f"{host}:{port}"
    if ":" in host and not host.startswith("["):  # an IPv6 address
        netloc = f"[{host}]" if port in (None, default) else f"[{host}]:{port}"
    return f"{parts.scheme}://{netloc}"


def tn3270_address(text: str) -> tuple[str, int] | None:
    """`host:port` (or `tn3270://host:port`, `[v6]:port`) of a mainframe target; port 23 when
    none is given."""
    text = (text or "").strip()
    if text.lower().startswith(("tn3270://", "tn3270s://")):
        text = text.split("://", 1)[1]
    text = text.rstrip("/")
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?|([A-Za-z0-9.\-]+)(?::(\d{1,5}))?", text)
    if not m:
        return None
    host = (m.group(1) or m.group(3) or "").lower()
    port = int(m.group(2) or m.group(4) or 23)
    return (host, port) if host and 0 < port < 65536 else None


def web_identity(url: str) -> dict:
    origin = origin_of(url)
    return {"key": f"web:{origin}" if origin else "web:", "surface": "web", "origin": origin,
            "display": urlsplit(origin).hostname if origin else ""}  # fmt: skip


def web_path(path: object) -> str | None:
    """A web target's path prefix without a trailing slash (`/finance/billing`), or None for the
    whole origin or a path that is not a plain one (flow.prefix_segments)."""
    from twinlab2_recorder.workstation.flow import prefix_segments

    segs = prefix_segments(path)
    return "/" + "/".join(segs) if segs else None


def denied_exe(path: str | None, name: str | None = None) -> bool:
    name = (name or ntpath.basename(path or "")).lower()
    if any(fnmatch.fnmatch(name, pat) for pat in DENY_EXE):
        return True
    low = (path or "").lower().replace("/", "\\")
    return any(low.startswith(d) for d in DENY_DIRS)


def denied_origin(origin: str) -> bool:
    return origin.lower().rstrip("/") in DENY_ORIGINS


@dataclass
class Target:
    """A target of a session: an application recorded beyond presence. Matched by `key` (an
    identity key), `exe` (an executable name) or `origin` (a web origin)."""

    key: str | None = None
    exe: str | None = None
    origin: str | None = None
    level: str = FULL
    name: str | None = None
    start: str | None = None  # a web target's start address, or a desktop target's program
    allow_denied: bool = False
    app_id: int | None = None  # the portal's catalog id, when the target came from it
    tn3270: str | None = None  # a mainframe host, host:port: the session's proxy forwards to it
    listen: int | None = None  # the proxy's port on 127.0.0.1 (the terminal connects to it)
    tls: bool = False  # TLS from the proxy to the host
    # a web target's path prefix (`/finance/billing`), when applications share its origin: the
    # steps under it keep it in their route template (flow.route_template)
    path: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Target:
        if not isinstance(d, dict):
            raise ValueError("a target is a mapping")
        level = str(d.get("level") or FULL)
        if level not in (FULL, STEPS):
            raise ValueError(f"target level must be full or steps, not {level!r}")
        origin = origin_of(str(d["origin"])) if d.get("origin") else None
        key = str(d["key"]) if d.get("key") else None
        if key and key.startswith("web:") and not origin:
            origin = origin_of(key[4:])
        path = web_path(d.get("path")) if d.get("path") else None
        if path is None and origin:  # an address given with its path: `https://host/finance/billing`
            path = web_path(urlsplit(str(d.get("origin") or (key or "")[4:])).path)
        tn3270 = None
        mainframe = d.get("tn3270") or (key[10:] if key and key.startswith("mainframe:") else None)
        if mainframe:
            addr = tn3270_address(str(mainframe))
            if addr is None:
                raise ValueError(f"a mainframe target is host:port, not {mainframe!r}")
            tn3270 = f"{addr[0]}:{addr[1]}"
            key = f"mainframe:{tn3270}"
            level = FULL
        if not (key or d.get("exe") or origin):
            raise ValueError("a target needs key, exe, origin or tn3270")
        app_id = d.get("app_id")
        listen = d.get("listen")
        return cls(key=key, exe=str(d["exe"]).lower() if d.get("exe") else None, origin=origin,
                   level=level, name=_clean(d.get("name")) or None,
                   start=str(d["start"]) if d.get("start") else None,
                   allow_denied=bool(d.get("allow_denied")),
                   app_id=int(app_id) if isinstance(app_id, int) else None, tn3270=tn3270,
                   listen=int(listen) if isinstance(listen, int) and 0 <= listen < 65536 else None,
                   tls=bool(d.get("tls")) or str(mainframe or "").startswith("tn3270s://"),
                   path=path if origin else None)  # fmt: skip

    def matches(self, ident: dict) -> bool:
        if self.key and ident.get("key") == self.key:
            return True
        if self.exe and str(ident.get("exe") or "").lower() == self.exe:
            return True
        return bool(self.origin and ident.get("origin") == self.origin)


@dataclass
class Targets:
    targets: list[Target] = field(default_factory=list)
    deny_keys: set[str] = field(default_factory=set)  # applications marked not replicated

    @classmethod
    def from_list(cls, items: list[dict] | None, deny_keys: list[str] | None = None) -> Targets:
        return cls([Target.from_dict(d) for d in items or []], set(deny_keys or []))

    def target_of(self, ident: dict) -> Target | None:
        return next((t for t in self.targets if t.matches(ident)), None)

    def level(self, ident: dict, path: str | None = None) -> str:
        target = self.target_of(ident)
        if ident.get("key") in self.deny_keys and not (target and target.allow_denied):
            return PRESENCE
        if ident.get("surface") == "mainframe":
            return target.level if target else PRESENCE
        if ident.get("surface") == "web":
            if not ident.get("origin"):
                return PRESENCE
            if denied_origin(ident["origin"]) and not (target and target.allow_denied):
                return PRESENCE
            return target.level if target else PRESENCE
        if ident.get("browser"):
            return PRESENCE  # a browser the person opened; the session's own Edge is web
        if (ident.get("host") or denied_exe(path, ident.get("exe"))) and not (
            target is not None and target.allow_denied
        ):
            return PRESENCE
        return target.level if target else PRESENCE

    def prefixes(self, origin: str) -> list[str]:
        """The path prefixes the targets name on an origin (flow.route_template)."""
        return sorted({t.path for t in self.targets if t.origin == origin and t.path})

    def origins(self) -> set[str]:
        return {t.origin for t in self.targets if t.origin}

    def mainframes(self) -> list[Target]:
        return [t for t in self.targets if t.tn3270]

    def web_start(self) -> str | None:
        for t in self.targets:
            if t.origin:
                return t.start or t.origin + "/"
        return None

    def as_list(self) -> list[dict]:
        return [{k: v for k, v in vars(t).items() if v not in (None, False)} for t in self.targets]
