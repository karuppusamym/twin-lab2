# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Targets for a session, proposed from a UiPath project before recording it.

    Twin Lab workstation propose <project folder>      (or TwinLabRecorder propose <folder>)

Every `.xaml` file of the project is read (as text; nothing is run) for:

- desktop applications: the `app` of window selectors (`<wnd app='casedesk.exe' .../>`), and the
  executable of Open/Use Application activities (`FileName`, `ApplicationPath`);
- web applications: the origin of `url` attributes in browser selectors, of `Url`/`TargetUrl`
  arguments and of any address written in the file (an argument's default value), and which
  browser (`msedge.exe`, `chrome.exe`, `BrowserType`);
- mainframes: `Host`/`Port` of terminal sessions (UiPath.Terminal activities);
- input methods the recorder sees differently: `SimulateClick`, `SendWindowMessages`,
  `InputMode`/`ClickType` set to Simulate, ChromiumAPI or WindowMessages (clicks made that way
  raise no mouse event).

The result is a proposal for a person to check: `targets` (the shape `Targets.from_list` reads),
`browser`, `notes`. An address held in a variable or a config file is not visible here: it is
listed under `notes` as such, never guessed.
"""

from __future__ import annotations

import ntpath
import re
from collections import Counter
from html import unescape
from pathlib import Path

from twinlab2_recorder.workstation.hosts import RUNTIMES
from twinlab2_recorder.workstation.levels import BROWSERS, HOSTS, denied_exe, origin_of

WND_APP = re.compile(r"<wnd\b[^>]*?\bapp='([^']+)'", re.I)
HTML_APP = re.compile(r"<html\b[^>]*?\bapp='([^']+)'", re.I)
URL_ATTR = re.compile(r"\burl='([^']+)'", re.I)
URL_ARG = re.compile(r'\b(?:Url|TargetUrl|BrowserUrl)="([^"{\[]+)"')
FILE_ARG = re.compile(r'\b(?:FileName|ApplicationPath|Path)="([^"{\[]*\.exe)"', re.I)
TERMINAL = re.compile(r"<(?:\w+:)?Terminal\w*\b[^>]*>", re.I)
HOST = re.compile(r'\bHost="([^"{\[]+)"', re.I)
PORT = re.compile(r'\bPort="(\d{1,5})"', re.I)
BROWSER_TYPE = re.compile(r'\bBrowserType="(\w+)"', re.I)
SIMULATE = re.compile(
    r'\b(SimulateClick|SimulateType|SendWindowMessages)="True"|'
    r'\b(?:InputMode|ClickType|InputMethod)="(Simulate|ChromiumAPI|WindowMessages|SendWindowMessages)"',
    re.I,
)  # fmt: skip
XMLNS = re.compile(r'xmlns(?::[\w.]+)?="[^"]*"')
SCHEMA_HOSTS = ("schemas.microsoft.com", "schemas.uipath.com", "schemas.openxmlformats.org",
                "w3.org", "schemas.xmlsoap.org", "purl.org")  # fmt: skip
ANY_URL = re.compile(r"https?://[^\s\"'<>&\]\[{}|]+")
VARIABLE_URL = re.compile(r'\b(?:Url|TargetUrl|Host)="[\[{]', re.I)


def _texts(project: Path) -> list[tuple[str, str]]:
    out = []
    for path in sorted(Path(project).rglob("*.xaml")):
        if any(part in (".local", ".objects", ".tmh", ".entities") for part in path.parts):
            continue
        try:
            out.append(
                (
                    str(path.relative_to(project)),
                    unescape(path.read_text(encoding="utf-8-sig", errors="replace")),
                )
            )
        except OSError:
            continue
    return out


def propose(project: Path) -> dict:
    project = Path(project)
    files = _texts(project)
    if not files:
        raise FileNotFoundError(f"no .xaml file under {project}")
    exes, origins, browsers, hosts, simulate = Counter(), Counter(), Counter(), Counter(), Counter()
    notes: list[str] = []
    for name, text in files:
        for app in WND_APP.findall(text) + [ntpath.basename(p) for p in FILE_ARG.findall(text)]:
            exes[app.lower()] += 1
        for app in HTML_APP.findall(text):
            browsers[app.lower()] += 1
        for kind in BROWSER_TYPE.findall(text):
            browsers[{"edge": "msedge.exe", "chrome": "chrome.exe", "firefox": "firefox.exe"}.get(
                kind.lower(), kind.lower())] += 1  # fmt: skip
        # selectors and arguments, and any address written in the file (a default value)
        body = XMLNS.sub("", text)  # namespace declarations are not addresses
        for url in URL_ATTR.findall(text) + URL_ARG.findall(text) + ANY_URL.findall(body):
            if any(h in url for h in SCHEMA_HOSTS):
                continue
            origin = origin_of(url.replace("*", ""))
            if origin:
                origins[origin] += 1
        for tag in TERMINAL.findall(text):
            host, port = HOST.search(tag), PORT.search(tag)
            if host:
                hosts[f"{host.group(1).strip().lower()}:{port.group(1) if port else 23}"] += 1
        for m in SIMULATE.finditer(text):
            simulate[m.group(1) or m.group(2)] += 1
        if VARIABLE_URL.search(text):
            notes.append(
                f"{name}: an address or host comes from a variable: add that target by hand"
            )
    targets: list[dict] = []
    for exe, n in exes.most_common():
        if exe in BROWSERS:
            continue
        item = {"exe": exe, "level": "full"}
        if exe in RUNTIMES and exe != "msedgewebview2.exe":
            notes.append(
                f"{exe} hosts other programs: each application it runs is recorded as its own "
                f"(desktop:{RUNTIMES[exe]}/<file>); one whose file cannot be told only as used"
            )
        elif exe in HOSTS or denied_exe(None, exe):
            notes.append(
                f"{exe} is recorded only as used unless the target says allow_denied: true"
            )
        targets.append(item | {"uses": n})
    targets += [{"origin": o, "level": "full", "uses": n} for o, n in origins.most_common()]
    targets += [{"tn3270": h, "uses": n} for h, n in hosts.most_common()]
    browser = None
    if browsers:
        top = browsers.most_common(1)[0][0]
        browser = "chrome" if "chrome" in top else "edge" if "edge" in top else None
    if simulate:
        notes.append("clicks set to " + ", ".join(sorted(simulate)) + " raise no mouse event: use "
                     "hardware events for the recording run, or check those steps")  # fmt: skip
    if browsers and not origins:
        notes.append(
            "the workflow uses a browser but no address is written in it: add the web target by hand"
        )
    return {"project": project.name, "files": len(files), "targets": targets, "browser": browser,
            "notes": sorted(set(notes))}  # fmt: skip
