"""The exclude list of this PC: applications the recorder collects nothing about.

The person using the PC (or the office, through a managed policy file) names applications that
must never be recorded: Word, Excel, Outlook, Teams, a password manager, a personal web mail. An
entry is one of:

- `process`: an executable name, case-insensitive, `*` and `?` allowed (`WINWORD.EXE`,
  `adobe*.exe`; `.exe` may be left out);
- `title`: a window-title pattern, case-insensitive: with `*` or `?` it must match the whole
  title (`* - Excel`), without them it is looked for anywhere in the title (`Personal`);
- `web`: a host (`mail.google.com`; `*.slack.com`; a host also covers its subdomains) or an
  address prefix (`https://intranet.example.test/hr/`, on whole path segments).

It is applied when an event is captured, before anything is stored. An excluded application gets
no UI Automation tree, no event, no picture, no step, no presence row and no network or HAR
entry. The only thing the session keeps is the entry's name, once, so that the recording's
completeness can say "N applications were excluded on this PC" without content: no title, no
address, no time (`Policy.label`, the journal's `excluded` rows, completeness.local_exclusions).

A desktop picture is printed from the window itself (PrintWindow), so a window on top of it is not
in the pixels; still, an excluded window over the target, or one embedded in it, is painted over
(`masks`), and a picture mostly covered by one is skipped (`MASK_LIMIT`).

The list is kept in the recorder's settings (`settings.json` `exclude`), seeded with `DEFAULTS`
the first time, every entry editable. A managed policy file (`managed_path()`:
`%ProgramData%\\TwinLabRecorder\\policy.json`, or `TWINLAB_RECORDER_POLICY`) adds entries that
are locked; with `"allow_user_changes": false` the person's own list cannot be changed either:

    {"exclude": [{"kind": "process", "pattern": "WINWORD.EXE", "name": "Microsoft Word"}],
     "allow_user_changes": false}

The local list wins over the portal: an application the portal offers as a target, but this PC
excludes, is not recorded, and the session's notes say so (its name is the portal's own).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

KINDS = ("process", "title", "web")
KIND_NAMES = {"process": "Program", "title": "Window title", "web": "Web site"}
SOURCES = ("default", "user", "managed")
PATTERN_MAX = 200
NAME_MAX = 80
# a picture whose window is covered more than this by excluded windows is not kept at all
MASK_LIMIT = 0.5
MASK_FILL = 0x808080  # the grey (a Windows COLORREF) an excluded window's place is painted


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str
    name: str = ""
    source: str = "user"

    @property
    def locked(self) -> bool:
        return self.source == "managed"

    @property
    def label(self) -> str:
        """What the recording may say about this entry: its name, or its pattern."""
        return self.name or self.pattern

    def key(self) -> tuple[str, str]:
        return self.kind, self.pattern.lower()

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "pattern": self.pattern}
        if self.name:
            out["name"] = self.name
        if self.source != "user":
            out["source"] = self.source
        return out

    # -- matching ------------------------------------------------------------------------------

    def matches_process(self, exe: str | None) -> bool:
        if self.kind != "process" or not exe:
            return False
        name = exe.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        pat = self.pattern.lower()
        if not pat.endswith((".exe", "*")):
            pat += ".exe"
        return fnmatch.fnmatchcase(name, pat)

    def matches_title(self, title: str | None) -> bool:
        if self.kind != "title" or not title:
            return False
        text, pat = " ".join(title.split()).lower(), " ".join(self.pattern.split()).lower()
        if any(c in pat for c in "*?["):
            return fnmatch.fnmatchcase(text, pat)
        return pat in text

    def matches_url(self, url: str | None) -> bool:
        if self.kind != "web" or not url:
            return False
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if not host:
            return False
        pat = self.pattern.strip().lower()
        if "://" in pat:  # an address prefix, on whole path segments
            try:
                p = urlsplit(pat)
            except ValueError:
                return False
            if (p.scheme and p.scheme != parts.scheme) or (p.hostname or "") != host:
                return False
            if p.port != parts.port:
                return False
            want = [s for s in (p.path or "/").split("/") if s]
            have = [s.lower() for s in (parts.path or "/").split("/") if s]
            return have[: len(want)] == want
        pat = pat.strip("/").split("/", 1)[0].split(":", 1)[0]
        if pat.startswith("*."):
            return host == pat[2:] or host.endswith(pat[1:])
        if any(c in pat for c in "*?["):
            return fnmatch.fnmatchcase(host, pat)
        return host == pat or host.endswith("." + pat)


def _d(kind: str, pattern: str, name: str) -> Rule:
    return Rule(kind, pattern, name, "default")


# Productivity and personal applications: excluded out of the box, every entry editable.
DEFAULTS: tuple[Rule, ...] = (
    _d("process", "WINWORD.EXE", "Microsoft Word"),
    _d("process", "EXCEL.EXE", "Microsoft Excel"),
    _d("process", "POWERPNT.EXE", "Microsoft PowerPoint"),
    _d("process", "ONENOTE.EXE", "Microsoft OneNote"),
    _d("process", "MSACCESS.EXE", "Microsoft Access"),
    _d("process", "OUTLOOK.EXE", "Microsoft Outlook"),
    _d("process", "olk.exe", "Outlook (new)"),
    _d("process", "ms-teams.exe", "Microsoft Teams"),
    _d("process", "Teams.exe", "Microsoft Teams (classic)"),
    _d("process", "1Password.exe", "1Password"),
    _d("process", "KeePass.exe", "KeePass"),
    _d("process", "KeePassXC.exe", "KeePassXC"),
    _d("process", "Bitwarden.exe", "Bitwarden"),
    _d("process", "LastPass.exe", "LastPass"),
    _d("process", "Dashlane.exe", "Dashlane"),
    _d("web", "outlook.office.com", "Outlook on the web"),
    _d("web", "outlook.office365.com", "Outlook on the web"),
    _d("web", "outlook.live.com", "Outlook.com"),
    _d("web", "teams.microsoft.com", "Teams on the web"),
    _d("web", "teams.live.com", "Teams on the web"),
    _d("web", "mail.google.com", "Gmail"),
    _d("web", "1password.com", "1Password on the web"),
    _d("web", "vault.bitwarden.com", "Bitwarden on the web"),
    _d("web", "lastpass.com", "LastPass on the web"),
)


def clean_rule(d: object, source: str = "user", keep_source: bool = False) -> Rule | None:
    """A rule from a settings or policy entry, or None when it is not one. `source` "managed":
    an entry of the managed file. `keep_source`: the entry's own `source` is trusted (a session's
    copy of the policy, written by this recorder); otherwise an entry can be a default or the
    person's, never managed."""
    if not isinstance(d, dict):
        return None
    kind = str(d.get("kind") or "").strip().lower()
    pattern = " ".join(str(d.get("pattern") or "").split())[:PATTERN_MAX]
    if kind not in KINDS or not pattern or pattern in ("*", "*.*", "*.exe"):
        return None  # an entry that would exclude everything is a mistake, not a policy
    name = " ".join(str(d.get("name") or "").split())[:NAME_MAX]
    given = d.get("source")
    if source == "managed":
        src = "managed"
    elif keep_source and given in SOURCES:
        src = given
    else:
        src = "default" if given == "default" else "user"
    return Rule(kind, pattern, name, src)


def parse_entry(text: str) -> Rule | None:
    """A rule from what a person typed: an address or host (web), a program name (process), or
    anything else (a window title). `title: Personal`, `web: example.com` and
    `process: app.exe` say it outright."""
    text = " ".join(str(text or "").split())
    if not text:
        return None
    m = re.match(r"^(process|program|title|window|web|site|host)\s*:\s*(.+)$", text, re.I)
    if m:
        kind = {"program": "process", "window": "title", "site": "web", "host": "web"}.get(
            m.group(1).lower(), m.group(1).lower())  # fmt: skip
        return clean_rule({"kind": kind, "pattern": m.group(2)})
    if re.match(r"^https?://", text, re.I):
        return clean_rule({"kind": "web", "pattern": text})
    if re.fullmatch(r"[\w.*?\-]+\.exe", text, re.I):
        return clean_rule({"kind": "process", "pattern": text})
    if re.fullmatch(r"(\*\.)?[a-z0-9\-]+(\.[a-z0-9\-]+)+", text, re.I):
        return clean_rule({"kind": "web", "pattern": text})
    return clean_rule({"kind": "title", "pattern": text})


@dataclass
class Policy:
    """The exclude list a session applies (picklable: collector processes get a copy)."""

    rules: list[Rule] = field(default_factory=list)
    allow_user_changes: bool = True
    managed: bool = False  # a managed policy file was read

    @classmethod
    def from_list(cls, items, managed: bool = False) -> Policy:
        """A session's copy of the policy (`as_session()["rules"]`, entries with their source)."""
        rules, seen = [], set()
        for d in items or []:
            r = d if isinstance(d, Rule) else clean_rule(d, keep_source=True)
            if r is not None and r.key() not in seen:
                seen.add(r.key())
                rules.append(r)
        return cls(rules, managed=managed)

    @classmethod
    def from_session(cls, doc) -> Policy:
        if isinstance(doc, Policy):
            return doc
        if isinstance(doc, dict):
            return cls.from_list(doc.get("rules"), managed=bool(doc.get("managed")))
        return cls.from_list(doc if isinstance(doc, list) else None)

    def as_list(self) -> list[dict]:
        return [r.as_dict() for r in self.rules]

    def as_session(self) -> dict:
        """What a session's configuration carries (main.session_config)."""
        return {"rules": self.as_list(), "managed": self.managed}

    def __bool__(self) -> bool:
        return bool(self.rules)

    def process(self, exe: str | None) -> Rule | None:
        return next((r for r in self.rules if r.matches_process(exe)), None)

    def title(self, title: str | None) -> Rule | None:
        return next((r for r in self.rules if r.matches_title(title)), None)

    def url(self, url: str | None) -> Rule | None:
        return next((r for r in self.rules if r.matches_url(url)), None)

    def window(self, exe: str | None, title: str | None) -> Rule | None:
        """The rule a window falls under: its program's, else its title's."""
        return self.process(exe) or self.title(title)

    def ident(self, ident: dict) -> Rule | None:
        """The rule an application identity (levels.desktop_identity, web_identity) falls under:
        its executable, or the application a host runs (its file name), or its web origin."""
        if ident.get("surface") == "web":
            return self.url(ident.get("origin"))
        return self.process(ident.get("exe")) or (
            self.process(ident.get("app_file")) if ident.get("app_file") else None)  # fmt: skip


# -- the settings and the managed file ----------------------------------------------------------


def managed_path() -> Path:
    if os.environ.get("TWINLAB_RECORDER_POLICY"):
        return Path(os.environ["TWINLAB_RECORDER_POLICY"])
    base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "TwinLabRecorder" / "policy.json"


def read_managed(path: Path | None = None) -> dict | None:
    """`{"rules": [Rule], "allow_user_changes": bool}` from the managed policy file, or None
    when there is none. A file that cannot be read locks the list and excludes nothing more,
    rather than letting the person think the office policy applies."""
    path = Path(path) if path is not None else managed_path()
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"rules": [], "allow_user_changes": False, "error": "the managed policy file cannot be read"}
    if not isinstance(doc, dict):
        return {"rules": [], "allow_user_changes": False, "error": "the managed policy file is not an object"}
    rules = [r for r in (clean_rule(d, "managed") for d in doc.get("exclude") or []) if r]
    return {"rules": rules, "allow_user_changes": doc.get("allow_user_changes") is not False}


def user_rules(values: dict) -> list[Rule]:
    """The person's own list from the settings; the defaults when there is none yet."""
    stored = values.get("exclude")
    if not isinstance(stored, dict) or not isinstance(stored.get("rules"), list):
        return list(DEFAULTS)
    return [r for r in (clean_rule(d) for d in stored["rules"]) if r]


def store(values: dict, rules: list[Rule]) -> None:
    """Keep the person's list (managed entries are never stored: the file is their source)."""
    values["exclude"] = {"rules": [r.as_dict() for r in rules if not r.locked]}


def load(values: dict, managed: Path | None = None) -> Policy:
    """The policy a session applies: the managed entries first, then the person's own."""
    got = read_managed(managed)
    rules = list(got["rules"]) if got else []
    seen = {r.key() for r in rules}
    for r in user_rules(values):
        if r.key() not in seen:
            seen.add(r.key())
            rules.append(r)
    pol = Policy(rules, allow_user_changes=got["allow_user_changes"] if got else True, managed=got is not None)
    return pol


def merge(*policies: Policy) -> Policy:
    """Every entry of every policy, once (a session never applies fewer than the PC's own)."""
    rules, seen = [], set()
    for pol in policies:
        for r in pol.rules if pol else ():
            if r.key() not in seen:
                seen.add(r.key())
                rules.append(r)
    return Policy(rules, allow_user_changes=all(p.allow_user_changes for p in policies if p is not None),
                  managed=any(p.managed for p in policies if p is not None))  # fmt: skip


def restore_defaults(rules: list[Rule]) -> list[Rule]:
    """The list with every default entry that was removed put back (edits are kept)."""
    have = {r.key() for r in rules}
    return list(rules) + [d for d in DEFAULTS if d.key() not in have]


# -- pictures -----------------------------------------------------------------------------------


def _intersect(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x1, y1, x2 - x1, y2 - y1) if x2 > x1 and y2 > y1 else None


def masks(target: tuple, others: list[tuple]) -> tuple[list[list[int]], float]:
    """Where excluded windows cover a picture of `target` (screen x, y, w, h): window-relative
    rectangles, and the share of the window they cover (an upper bound: overlaps between the
    excluded windows count twice). `others`: the screen rectangles of excluded windows above it
    or inside it."""
    if not target or target[2] <= 0 or target[3] <= 0:
        return [], 0.0
    out, area = [], 0
    for r in others:
        hit = _intersect(target, r) if r else None
        if hit:
            out.append([hit[0] - target[0], hit[1] - target[1], hit[2], hit[3]])
            area += hit[2] * hit[3]
    return out, min(1.0, area / float(target[2] * target[3]))


def mask_places(rects: list[list[int]]) -> list[dict]:
    """The rectangles as places for twinlab2_recorder.desktop.winlook.masked_picture."""
    return [{"rect": list(r), "text": "", "hwnd": 0, "secret": None, "excluded": True, "fill": MASK_FILL}
            for r in rects]


# -- what the recording says --------------------------------------------------------------------


def summary(names, entries: int = 0, managed: bool = False) -> dict | None:
    """completeness.local_exclusions: how many applications were excluded on this PC (the
    entries that matched) and those entries' names; None when the session had no exclude list.
    `entries`: how long the list was; `managed`: whether a managed policy file was part of it."""
    names = sorted({" ".join(str(n).split())[:NAME_MAX] for n in names or () if str(n).strip()})
    if not entries and not names:
        return None
    return {"count": len(names), "names": names[:50], "entries": int(entries or 0),
            "managed": bool(managed)}  # fmt: skip


# -- the recorder's window ----------------------------------------------------------------------

SOURCE_NAMES = {"default": "Default", "user": "Added on this PC", "managed": "Office policy (locked)"}


def rows(policy: Policy) -> list[dict]:
    """The list as the window shows it: one row per entry, managed ones first."""
    return [{"kind": KIND_NAMES[r.kind], "pattern": r.pattern, "name": r.name or "",
             "source": SOURCE_NAMES[r.source], "locked": r.locked or not policy.allow_user_changes,
             "rule": r} for r in policy.rules]  # fmt: skip


def running_programs() -> list[str]:
    """The programs with a visible window on this PC now (executable names only, this recorder
    left out), for "Pick from running apps". Nothing else is read: no title, no path kept."""
    import sys

    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    pids: set[int] = set()

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    user32.EnumWindows(proc(cb), 0)
    names = set()
    for pid in pids - {os.getpid(), 0}:
        h = kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                names.add(buf.value.replace("/", "\\").rsplit("\\", 1)[-1])
        finally:
            kernel32.CloseHandle(h)
    skip = {"explorer.exe", "applicationframehost.exe", "textinputhost.exe", "searchhost.exe",
            "shellexperiencehost.exe", "startmenuexperiencehost.exe", "twinlabrecorder.exe"}  # fmt: skip
    return sorted((n for n in names if n.lower() not in skip), key=str.lower)
