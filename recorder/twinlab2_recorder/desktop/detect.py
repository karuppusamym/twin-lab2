# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Which toolkit a desktop application is built with, from what UI Automation and Windows show.

Signals (names only, never a value the application shows):

- `framework_ids`: the FrameworkId of the controls (`WinForm`, `WPF`, `XAML`, `Win32`, `DirectUI`,
  `Chrome`, ...);
- `classes`: window class names (`WindowsForms10.*`, `HwndWrapper[...]`, `SunAwtFrame`, ...);
- `modules`: base names of the DLLs loaded in the process (`System.Windows.Forms.dll`,
  `Qt5Core.dll`, `jvm.dll`, ...), and `runtimes`, the .NET shared runtimes they come from
  (`Microsoft.WindowsDesktop.App 6.0.36`), read from the module folders;
- `exe`: the executable's version resource (file description, product name and version, original
  file name) and `files`: a few marker files next to it (`resources/app.asar` for Electron).

The result has the shape the web detection uses (Twin Lab's har `_framework`): the toolkit with its
marker and confidence, `also` for other toolkits seen, and `evidence`, every rule that fired,
each with its marker and confidence (certain: only this toolkit does it; likely: usually it;
guess: a convention others share).
"""

from __future__ import annotations

import re
from typing import NamedTuple

CERTAIN, LIKELY, GUESS = "certain", "likely", "guess"
RANK = {CERTAIN: 3, LIKELY: 2, GUESS: 1}

# What the recorder can do with each toolkit (docs/09-desktop.md).
SUPPORT = {
    "WinForms": "full",
    "WPF": "full",
    "UWP / WinUI": "full",
    "Win32 / MFC": "full",
    "Win32": "full",
    "Delphi / VCL": "detected only (standard VCL controls would expose UI Automation through "
    "Win32 proxies, custom-drawn grids show as one Pane)",
    "Qt": "detected only (Qt exposes UI Automation through its accessibility plug-in; custom "
    "widgets may expose nothing)",
    "Java Swing / AWT": "full through the Java Access Bridge (twinlab2_recorder.desktop.java): enabled for "
    "all Java programs with `jabswitch -enable` (then restart the application), or for one run "
    "with -Djavax.accessibility.assistive_technologies=com.sun.java.accessibility.AccessBridge",
    "JavaFX": "through UI Automation: JavaFX (8u40 and later) is its own UI Automation provider "
    "(GlassWindowClass windows); proven by unit fixtures only, not on a live JavaFX app",
    "Electron / CEF": "use the web recorder: this is a web application inside a desktop shell; "
    "start it with --remote-debugging-port and use Twin Lab's `evidence attach`",
    "VB6": "detected only (intrinsic controls are Win32 windows; ActiveX grids expose nothing)",
    "PowerBuilder": "detected only (DataWindows are drawn by PowerBuilder and expose little or "
    "no control tree)",
    "Remote session (Citrix / RDP)": "not supported: no control tree — not supported by this "
    "recorder. Only pixels reach this machine; record inside the session where the "
    "application runs",
}


class Rule(NamedTuple):
    source: str  # framework_id | class | module | runtime | exe | file | process
    rx: re.Pattern
    toolkit: str
    conf: str
    marker: str
    version: str | None = None  # a group name in rx that holds the version


def _r(source: str, rx: str, toolkit: str, conf: str, marker: str) -> Rule:
    return Rule(source, re.compile(rx, re.I), toolkit, conf, marker)


RULES: list[Rule] = [
    # WinForms
    _r("framework_id", r"^WinForm$", "WinForms", CERTAIN, "FrameworkId WinForm"),
    _r("class", r"^WindowsForms10\.", "WinForms", CERTAIN, "window class WindowsForms10.*"),
    _r("module", r"^System\.Windows\.Forms\.dll$", "WinForms", CERTAIN, "System.Windows.Forms.dll loaded"),
    # WPF
    _r("framework_id", r"^WPF$", "WPF", CERTAIN, "FrameworkId WPF"),
    _r("class", r"^HwndWrapper\[", "WPF", CERTAIN, "window class HwndWrapper[...]"),
    _r("module", r"^PresentationFramework(\.ni)?\.dll$", "WPF", CERTAIN, "PresentationFramework.dll loaded"),
    _r("module", r"^wpfgfx_", "WPF", LIKELY, "wpfgfx loaded"),
    # UWP / WinUI
    _r("framework_id", r"^XAML$", "UWP / WinUI", CERTAIN, "FrameworkId XAML"),
    _r("class", r"^Windows\.UI\.Core\.CoreWindow$", "UWP / WinUI", CERTAIN, "window class Windows.UI.Core.CoreWindow"),
    _r("class", r"^ApplicationFrameWindow$", "UWP / WinUI", LIKELY, "window class ApplicationFrameWindow"),
    _r("class", r"^WinUIDesktopWin32WindowClass$", "UWP / WinUI", CERTAIN, "window class WinUIDesktopWin32WindowClass (WinUI 3)"),
    _r("module", r"^Microsoft\.UI\.Xaml\.dll$", "UWP / WinUI", CERTAIN, "Microsoft.UI.Xaml.dll loaded (WinUI 3)"),
    _r("module", r"^Windows\.UI\.Xaml\.dll$", "UWP / WinUI", LIKELY, "Windows.UI.Xaml.dll loaded (UWP XAML)"),
    # Win32 / MFC
    _r("class", r"^Afx:", "Win32 / MFC", CERTAIN, "window class Afx:*"),
    _r("module", r"^mfc\d+u?d?\.dll$", "Win32 / MFC", CERTAIN, "MFC runtime loaded"),
    _r("framework_id", r"^Win32$", "Win32", GUESS, "FrameworkId Win32 (also every toolkit's plain windows)"),
    _r("class", r"^#32770$", "Win32", GUESS, "dialog class #32770 (also message boxes of any toolkit)"),
    # Delphi / VCL
    _r("class", r"^TApplication$", "Delphi / VCL", CERTAIN, "window class TApplication"),
    _r("class", r"^T(Form|Edit|Button|Memo|ComboBox|ListBox|Panel|PageControl|StringGrid|DBGrid)\w*$",
       "Delphi / VCL", LIKELY, "VCL window classes (TForm..., TEdit, TButton)"),
    _r("module", r"^(vcl|rtl)\d+\.bpl$", "Delphi / VCL", CERTAIN, "VCL runtime package loaded"),
    _r("module", r"^borlndmm\.dll$", "Delphi / VCL", LIKELY, "borlndmm.dll loaded"),
    # Qt
    _r("class", r"^Qt(?P<v>\d)[\w]*QWindow", "Qt", CERTAIN, "window class Qt*QWindow*"),
    _r("module", r"^Qt(?P<v>\d)Core[d]?\.dll$", "Qt", CERTAIN, "Qt core library loaded"),
    _r("module", r"^QtCore\d?\.dll$", "Qt", CERTAIN, "Qt 4 core library loaded"),
    # Java
    _r("class", r"^SunAwt(Frame|Dialog|Canvas|Window)$", "Java Swing / AWT", CERTAIN, "window class SunAwtFrame/SunAwtDialog"),
    _r("module", r"^(jvm|awt)\.dll$", "Java Swing / AWT", LIKELY, "Java VM loaded (jvm.dll/awt.dll)"),
    _r("module", r"^(Windows|java)AccessBridge(-64|-32)?\.dll$", "Java Swing / AWT", LIKELY, "Java Access Bridge loaded"),
    _r("class", r"^GlassWndClass-GlassWindowClass-", "JavaFX", CERTAIN, "window class GlassWndClass-GlassWindowClass-*"),
    _r("module", r"^(glass|prism_d3d|javafx_font)\.dll$", "JavaFX", CERTAIN, "JavaFX native library loaded"),
    # Electron / CEF / embedded browsers
    _r("class", r"^Chrome_WidgetWin_\d$", "Electron / CEF", LIKELY, "window class Chrome_WidgetWin_*"),
    _r("class", r"^Chrome_RenderWidgetHostHWND$", "Electron / CEF", CERTAIN, "window class Chrome_RenderWidgetHostHWND"),
    _r("class", r"^CefBrowserWindow$", "Electron / CEF", CERTAIN, "window class CefBrowserWindow"),
    _r("module", r"^libcef\.dll$", "Electron / CEF", CERTAIN, "libcef.dll loaded"),
    _r("file", r"^resources/app\.asar$", "Electron / CEF", CERTAIN, "resources/app.asar next to the executable (Electron)"),
    _r("framework_id", r"^Chrome$", "Electron / CEF", LIKELY, "FrameworkId Chrome"),
    # VB6
    _r("class", r"^ThunderRT6\w+$", "VB6", CERTAIN, "window class ThunderRT6* (VB6 runtime)"),
    _r("class", r"^Thunder\w+$", "VB6", LIKELY, "window class Thunder* (VB6 IDE run)"),
    _r("module", r"^msvbvm60\.dll$", "VB6", CERTAIN, "msvbvm60.dll loaded"),
    # PowerBuilder
    _r("class", r"^(FNWND|FNWNS|FNUDO|FNHELP)\d*", "PowerBuilder", CERTAIN, "window class FNWND* (PowerBuilder)"),
    _r("class", r"^pbdw\d+", "PowerBuilder", CERTAIN, "DataWindow class pbdw*"),
    _r("module", r"^pbvm\d+\.dll$", "PowerBuilder", CERTAIN, "PowerBuilder VM loaded (pbvm*.dll)"),
    _r("module", r"^pbdwe?\d+\.dll$", "PowerBuilder", CERTAIN, "PowerBuilder DataWindow engine loaded"),
    # Remote sessions: pixels only
    _r("class", r"^Transparent Windows Client$", "Remote session (Citrix / RDP)", CERTAIN, "window class Transparent Windows Client (Citrix Workspace)"),
    _r("process", r"^(wfica32|CDViewer|SelfService)\.exe$", "Remote session (Citrix / RDP)", CERTAIN, "Citrix Workspace process"),
    _r("class", r"^(TscShellContainerClass|IHWindowClass|OPContainerClass)$", "Remote session (Citrix / RDP)", CERTAIN, "Remote Desktop client window class"),
    _r("process", r"^(mstsc|msrdc|vmconnect)\.exe$", "Remote session (Citrix / RDP)", CERTAIN, "Remote Desktop client process"),
]  # fmt: skip

# Detected and reported, never recorded (decided 2026-11-17: out of scope for now).
NOT_SUPPORTED = {"Delphi / VCL", "Qt", "VB6", "PowerBuilder"}

RUNTIME = re.compile(r"^Microsoft\.(?P<kind>NETCore|WindowsDesktop|AspNetCore)\.App (?P<v>[\d.]+)$")
SPECIFIC_OVER = {"Win32": ("WinForms", "WPF", "Win32 / MFC", "Delphi / VCL", "VB6", "PowerBuilder",
                           "Qt", "Java Swing / AWT", "JavaFX", "Electron / CEF", "UWP / WinUI",
                           "Remote session (Citrix / RDP)")}  # fmt: skip


def _hits(signals: dict) -> list[dict]:
    values: dict[str, list[str]] = {
        "framework_id": sorted(signals.get("framework_ids") or []),
        "class": sorted(signals.get("classes") or []),
        "module": sorted(signals.get("modules") or []),
        "file": sorted(signals.get("files") or []),
        "process": [signals["process"]] if signals.get("process") else [],
    }
    out = []
    for rule in RULES:
        for value in values.get(rule.source, []):
            m = rule.rx.search(value)
            if m:
                out.append({"rule": rule, "version": m.groupdict().get("v")})
                break
    return out


def _runtime(signals: dict) -> dict | None:
    """The .NET runtime: from the shared-runtime folders of loaded modules, else the CLR module."""
    found = {}
    for text in signals.get("runtimes") or []:
        m = RUNTIME.match(text)
        if m:
            found.setdefault(m.group("kind"), m.group("v"))
    mods = {m.lower() for m in signals.get("modules") or []}
    if found:
        version = found.get("WindowsDesktop") or found.get("NETCore")
        name = ".NET" if version and int(version.split(".")[0]) >= 5 else ".NET Core"
        return {"name": name, "version": version, "marker": "shared runtime folder of loaded "
                "modules: " + ", ".join(f"Microsoft.{k}.App {v}" for k, v in sorted(found.items())),
                "confidence": CERTAIN}  # fmt: skip
    if "coreclr.dll" in mods:
        return {"name": ".NET", "version": None, "marker": "coreclr.dll loaded (self-contained)",
                "confidence": CERTAIN}  # fmt: skip
    if "clr.dll" in mods:
        return {"name": ".NET Framework", "version": "4.x", "marker": "clr.dll loaded",
                "confidence": CERTAIN}  # fmt: skip
    if "mscorwks.dll" in mods:
        return {"name": ".NET Framework", "version": "2.0-3.5", "marker": "mscorwks.dll loaded",
                "confidence": CERTAIN}  # fmt: skip
    return None


def detect(signals: dict) -> dict:
    """The toolkit the signals show; see the module docstring for the shape."""
    hits = _hits(signals)
    by: dict[str, list[dict]] = {}
    for h in hits:
        by.setdefault(h["rule"].toolkit, []).append(h)
    if any(t in by for t in SPECIFIC_OVER["Win32"]) and "Win32" in by:
        win32 = by.pop("Win32")  # plain Win32 windows are in every toolkit's process
    else:
        win32 = []

    def score(t: str) -> tuple[int, int]:
        return max(RANK[h["rule"].conf] for h in by[t]), len(by[t])

    ranked = sorted(by, key=score, reverse=True)
    evidence = [
        {"kind": "desktop", "name": h["rule"].toolkit, "marker": h["rule"].marker,
         "confidence": h["rule"].conf, **({"version": h["version"]} if h["version"] else {})}
        for h in sorted(hits, key=lambda h: (-RANK[h["rule"].conf], h["rule"].toolkit))
    ]  # fmt: skip
    result: dict = {"surface": "desktop"}
    exe = signals.get("exe") or {}
    if ranked:
        top = ranked[0]
        best = max(by[top], key=lambda h: (RANK[h["rule"].conf], bool(h["version"])))
        result.update({"toolkit": top, "marker": best["rule"].marker,
                       "confidence": best["rule"].conf, "support": SUPPORT.get(top, "unknown")})  # fmt: skip
        version = next((h["version"] for h in by[top] if h["version"]), None)
        if version:
            result["version"] = version
        others = [{"toolkit": t, "marker": by[t][0]["rule"].marker,
                   "confidence": max((h["rule"].conf for h in by[t]), key=RANK.get)}
                  for t in ranked[1:]]  # fmt: skip
        if others:
            result["also"] = others
    elif win32:
        h = win32[0]
        result.update({"toolkit": "Win32", "marker": h["rule"].marker,
                       "confidence": h["rule"].conf, "support": SUPPORT["Win32"]})  # fmt: skip
    else:
        result.update({"toolkit": None, "marker": "no toolkit marker seen", "confidence": None,
                       "support": "unknown"})  # fmt: skip
    runtime = _runtime(signals)
    if runtime:
        result["runtime"] = runtime
        evidence.append({"kind": "runtime", "name": runtime["name"], "marker": runtime["marker"],
                         "confidence": runtime["confidence"],
                         **({"version": runtime["version"]} if runtime["version"] else {})})  # fmt: skip
    if exe:
        result["executable"] = {k: exe[k] for k in ("file", "description", "product",
                                                    "product_version", "file_version")
                                if exe.get(k)}  # fmt: skip
    tk = result.get("toolkit")
    if tk == "Remote session (Citrix / RDP)":
        result["supported"] = False
        result["problem"] = "no control tree — not supported by this recorder"
    elif tk in NOT_SUPPORTED:
        result["supported"] = False
        result["problem"] = (f"{tk} is detected but not supported by this recorder: "
                             f"{SUPPORT.get(tk, '')}")  # fmt: skip
    elif tk == "Electron / CEF":
        result["supported"] = False
        result["problem"] = ("a web application inside a desktop shell: record it with the web "
                             "recorder (Twin Lab's `evidence attach` to its --remote-debugging-port)")  # fmt: skip
    elif tk == "Java Swing / AWT" and not any(
        m.lower().startswith(("windowsaccessbridge", "javaaccessbridge"))
        for m in signals.get("modules") or []
    ):
        result["supported"] = True
        result["problem"] = ("Java Access Bridge is not loaded: the window has no control tree "
                             "until it is enabled (`jabswitch -enable`) and the application restarted")  # fmt: skip
    else:
        result["supported"] = tk is not None
    result["evidence"] = evidence
    return result


def signals_from_trees(trees: list[dict]) -> dict:
    """FrameworkIds and window class names seen in recorded window trees."""
    fws, classes = set(), set()

    def walk(n: dict) -> None:
        if n.get("fw"):
            fws.add(n["fw"])
        if n.get("cls"):
            classes.add(n["cls"])
        for c in n.get("children") or []:
            walk(c)

    for t in trees:
        walk(t)
    return {"framework_ids": fws, "classes": classes}
