# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Windows only: UI Automation through comtypes, and the few Win32 calls the recorder needs.

Imported lazily by the recorder and verify, after `twinlab2_recorder.desktop.require()`. Nothing here keeps
a value: `capture` returns the raw tree for `privacy.clean` to reduce, in memory.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

if "comtypes" not in sys.modules:
    sys.coinit_flags = 0  # COINIT_MULTITHREADED: UI Automation calls its handlers on its threads

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402

from twinlab2_recorder.desktop import locator
from twinlab2_recorder.desktop import winlook  # noqa: E402

UIA = comtypes.client.GetModule("UIAutomationCore.dll")
_auto = None

# One coordinate system for hooks, UI Automation and SendInput: physical pixels. A DPI-unaware
# process gets scaled coordinates from UI Automation but physical ones from the mouse hook.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except (AttributeError, OSError):
    pass

CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar",
    50038: "SemanticZoom", 50039: "AppBar",
}  # fmt: skip
TYPE_IDS = {v: k for k, v in CONTROL_TYPES.items()}

P_RUNTIME, P_RECT, P_PID, P_TYPE, P_NAME = 30000, 30001, 30002, 30003, 30005
P_ACCEL, P_ACCESS, P_FOCUSABLE, P_ENABLED, P_AID, P_CLASS = 30006, 30007, 30009, 30010, 30011, 30012
P_FOCUSED = 30008  # UIA_HasKeyboardFocusPropertyId
P_HELP, P_PASSWORD, P_HWND, P_OFFSCREEN, P_FW = 30013, 30019, 30020, 30022, 30024
P_VALUE, P_READONLY, P_HAS_VALUE = 30045, 30046, 30043
P_HAS_INVOKE, P_HAS_TOGGLE, P_HAS_SELITEM, P_HAS_EXPAND = 30031, 30041, 30036, 30028
P_TOGGLE, P_EXPAND, P_SELECTED, P_ROWS, P_COLS = 30086, 30070, 30079, 30062, 30063
PROPS = [P_RUNTIME, P_RECT, P_PID, P_TYPE, P_NAME, P_ACCEL, P_ACCESS, P_FOCUSABLE, P_ENABLED,
         P_AID, P_CLASS, P_HELP, P_PASSWORD, P_HWND, P_OFFSCREEN, P_FW, P_VALUE, P_READONLY,
         P_HAS_VALUE, P_HAS_INVOKE, P_HAS_TOGGLE, P_HAS_SELITEM, P_HAS_EXPAND, P_TOGGLE,
         P_EXPAND, P_SELECTED, P_ROWS, P_COLS, P_FOCUSED]  # fmt: skip
PAT_INVOKE, PAT_VALUE, PAT_EXPAND, PAT_SELITEM, PAT_TOGGLE, PAT_LEGACY = (
    10000, 10002, 10005, 10010, 10015, 10018)  # fmt: skip
E_MENU_OPENED, E_FOCUS, E_INVOKED, E_SELECTED, E_WINDOW_OPENED = 20003, 20005, 20009, 20012, 20016
TOGGLE = {0: "off", 1: "on", 2: "indeterminate"}
EXPAND = {0: "collapsed", 1: "expanded", 2: "partial", 3: "leaf"}
TREE_SCOPE_SUBTREE = 7
MAX_NODES, MAX_ROWS, MAX_DEPTH = 4000, 25, 40

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]  # fmt: skip
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]  # fmt: skip
psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE,
    wintypes.HMODULE,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
version.GetFileVersionInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wintypes.UINT)]  # fmt: skip
for _name, _args in (
    ("GetWindowDC", [wintypes.HWND]),
    ("ReleaseDC", [wintypes.HWND, ctypes.c_void_p]),
):
    getattr(user32, _name).argtypes = _args
user32.GetWindowDC.restype = ctypes.c_void_p
user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]  # fmt: skip
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
GA_ROOT, GW_OWNER = 2, 4
IGNORED_CLASSES = {"SysShadow", "IME", "MSCTFIME UI", "tooltips_class32"}
COR_E_TIMEOUT, UIA_E_TIMEOUT = -2146233083, -2146233083 & 0xFFFFFFFF


def auto():
    """The IUIAutomation object (one per process)."""
    global _auto
    if _auto is None:
        _auto = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=UIA.IUIAutomation
        )
    return _auto


# --- processes and windows ---------------------------------------------------------------------


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def top_windows(pids: set[int]) -> list[int]:
    """Visible top-level windows of these processes, in Z order."""
    out: list[int] = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and window_pid(hwnd) in pids:
            out.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def app_pid(hwnd: int) -> int:
    """The process an application window shows. A modern (UWP / WinUI) application's top-level
    window belongs to ApplicationFrameHost.exe; the application's own CoreWindow is its child:
    its process is the application's. Any other window: its own process."""
    pid = window_pid(hwnd)
    if not hwnd or class_name(hwnd) != "ApplicationFrameWindow":
        return pid
    found: list[int] = []

    def cb(child, _):
        if class_name(child) == "Windows.UI.Core.CoreWindow":
            other = window_pid(child)
            if other and other != pid:
                found.append(other)
                return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return found[0] if found else pid


def frame_windows(pids: set[int]) -> dict[int, int]:
    """Visible ApplicationFrameWindows that show one of these processes: {frame: app pid}."""
    out: dict[int, int] = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and class_name(hwnd) == "ApplicationFrameWindow":
            pid = app_pid(hwnd)
            if pid in pids:
                out[int(hwnd)] = pid
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def root_window(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def owner(hwnd: int) -> int:
    return int(user32.GetWindow(hwnd, GW_OWNER) or 0)


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def process_path(pid: int) -> str | None:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def process_name(pid: int) -> str | None:
    path = process_path(pid)
    return os.path.basename(path) if path else None


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]  # fmt: skip


def pids_by_name(name: str) -> list[int]:
    """Process ids whose executable is `name` (case-insensitive, `.exe` optional)."""
    want = name.lower()
    want = want if want.endswith(".exe") else want + ".exe"
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == want:
                out.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def pids_by_title(pattern: str) -> list[int]:
    """Processes with a visible top-level window whose title matches (regex, case-insensitive).
    Titles of other windows are read to compare and then dropped; nothing is kept."""
    rx = re.compile(pattern, re.I)
    found: list[int] = []
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value and rx.search(buf.value):
                found.append(window_pid(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return sorted(set(found))


def alive(pid: int) -> bool:
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


RUNTIME_DIR = re.compile(r"[\\/]shared[\\/](Microsoft\.\w+\.App)[\\/]([\d.]+)[\\/]", re.I)


def modules(pid: int) -> tuple[list[str], list[str]]:
    """Base names of the modules loaded in the process, and the .NET shared runtimes they come
    from (`Microsoft.WindowsDesktop.App 6.0.36`). Folder paths are never kept."""
    h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return [], []
    try:
        arr = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(needed), 3):
            return [], []
        names, runtimes = set(), set()
        buf = ctypes.create_unicode_buffer(1024)
        for i in range(min(needed.value // ctypes.sizeof(wintypes.HMODULE), 2048)):
            if psapi.GetModuleFileNameExW(h, arr[i], buf, 1024):
                names.add(os.path.basename(buf.value))
                m = RUNTIME_DIR.search(buf.value)
                if m:
                    runtimes.add(f"{m.group(1)} {m.group(2)}")
        return sorted(names), sorted(runtimes)
    finally:
        kernel32.CloseHandle(h)


def exe_info(path: str | None) -> dict:
    """The version resource of an executable: description, product, versions, file name."""
    if not path:
        return {}
    out = {"file": os.path.basename(path)}
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return out
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return out
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    langs = []
    if version.VerQueryValueW(
        data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)
    ):
        raw = ctypes.string_at(ptr, n.value)
        langs = [struct.unpack_from("<HH", raw, i) for i in range(0, len(raw) - 3, 4)]
    langs = langs or [(0x0409, 0x04B0)]
    keys = {"FileDescription": "description", "ProductName": "product", "CompanyName": "company",
            "ProductVersion": "product_version", "FileVersion": "file_version",
            "OriginalFilename": "original_file"}  # fmt: skip
    lang, cp = langs[0]
    for key, name in keys.items():
        sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
        if version.VerQueryValueW(data, sub, ctypes.byref(ptr), ctypes.byref(n)) and n.value:
            out[name] = ctypes.wstring_at(ptr, n.value).rstrip("\x00")[:120]
    return out


ntdll = ctypes.WinDLL("ntdll")
ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]  # fmt: skip
ntdll.NtQueryInformationProcess.restype = ctypes.c_long
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
kernel32.GetPackageFamilyName.restype = wintypes.LONG


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("ExitStatus", ctypes.c_long), ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_size_t), ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_size_t), ("InheritedFromUniqueProcessId", ctypes.c_size_t)]  # fmt: skip


def command_line(pid: int) -> str | None:
    """The process's command line (ProcessCommandLineInformation: Windows 8.1 and later, no
    administrator rights for a process of the same user). Read to identify a host's
    application (workstation.hosts); never kept."""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
        if not size.value or size.value > 1 << 20:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
            return None
        us = _UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else None
    finally:
        kernel32.CloseHandle(h)


def _wow64(handle) -> bool:
    flag = wintypes.BOOL(False)
    return bool(kernel32.IsWow64Process(handle, ctypes.byref(flag)) and flag.value)


def current_directory(pid: int) -> str | None:
    """The process's current folder, from its process parameters (a 64-bit process read by this
    64-bit one; None otherwise). For a host given a relative file (`java -jar app.jar`)."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return None
    try:
        if _wow64(h):
            return None
        pbi = _PROCESS_BASIC_INFORMATION()
        if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0:
            return None

        def read(address, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t()
            ok = kernel32.ReadProcessMemory(h, ctypes.c_void_p(address), buf, size, ctypes.byref(got))
            return buf.raw if ok and got.value == size else None

        raw = read((pbi.PebBaseAddress or 0) + 0x20, 8)  # PEB.ProcessParameters
        params = struct.unpack("<Q", raw)[0] if raw else 0
        raw = read(params + 0x38, 16) if params else None  # CurrentDirectory.DosPath
        if not raw:
            return None
        length, _max, _pad, buffer = struct.unpack("<HHIQ", raw)
        text = read(buffer, length) if buffer and 0 < length < 4096 else None
        return text.decode("utf-16-le").rstrip("\\") + "\\" if text else None
    finally:
        kernel32.CloseHandle(h)


def parent_pid(pid: int) -> int | None:
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID) or None
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return None


def package_family(pid: int) -> str | None:
    """The package family name of a packaged (MSIX / UWP) process, else None."""
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        n = wintypes.UINT(256)
        buf = ctypes.create_unicode_buffer(256)
        return buf.value or None if kernel32.GetPackageFamilyName(h, ctypes.byref(n), buf) == 0 else None
    finally:
        kernel32.CloseHandle(h)


def host_facts(pid: int) -> dict:
    """What workstation.hosts.resolve reads of a process: `{"argv", "cwd", "package"}`. Only
    the application's file, module or package name is kept from it."""
    from twinlab2_recorder.workstation.hosts import split_args

    line = command_line(pid)
    return {"argv": split_args(line) if line else [], "cwd": current_directory(pid),
            "package": package_family(pid)}  # fmt: skip


def marker_files(path: str | None) -> list[str]:
    """Marker files next to the executable (names only)."""
    if not path:
        return []
    base = Path(path).parent
    found = []
    for rel in ("resources/app.asar", "resources/electron.asar", "libcef.dll", "jre/bin/java.exe"):
        if (base / rel).exists():
            found.append(rel)
    return found


def signals(pid: int) -> dict:
    """Detection signals of a process. Only the module names a detection rule knows are kept
    (plus the .NET runtime modules): the rest would describe the machine, not the application."""
    from twinlab2_recorder.desktop import detect

    path = process_path(pid)
    mods, runtimes = modules(pid)
    known = [m for m in mods if any(r.source == "module" and r.rx.search(m) for r in detect.RULES)
             or m.lower() in ("coreclr.dll", "clr.dll", "mscorwks.dll", "hostfxr.dll")]  # fmt: skip
    return {"process": os.path.basename(path) if path else None, "modules": known,
            "runtimes": runtimes, "exe": exe_info(path), "files": marker_files(path)}  # fmt: skip


def variant(value):
    """A property-changed event's VARIANT as a Python value."""
    return getattr(value, "value", value)


# --- UI Automation elements --------------------------------------------------------------------


def _cache_request():
    a = auto()
    cr = a.CreateCacheRequest()
    for pid in PROPS:
        cr.AddProperty(pid)
    cr.TreeScope = TREE_SCOPE_SUBTREE
    cr.TreeFilter = a.ControlViewCondition
    return cr


def element_from_handle(hwnd: int):
    return auto().ElementFromHandle(hwnd)


def element_from_point(x: int, y: int):
    return auto().ElementFromPoint(UIA.tagPOINT(x, y))


def focused():
    return auto().GetFocusedElement()


# A click on the text or picture inside one of these presses the control itself.
PRESSED_BY_CONTENT = {"Button", "CheckBox", "RadioButton", "MenuItem", "TabItem", "SplitButton",
                      "Hyperlink"}  # fmt: skip


def click_target(el):
    """The control a click on `el` acts on. `ElementFromPoint` gives the innermost element, in
    WPF the TextBlock a button, check box or menu item draws its caption with: outside the
    control view (the nearest control element is taken) or a Text child of the control (its
    parent is taken). Any other element is returned as it is."""
    try:
        walker = auto().ControlViewWalker
        el = walker.NormalizeElement(el) or el
        if CONTROL_TYPES.get(el.CurrentControlType) in ("Text", "Image"):
            parent = walker.GetParentElement(el)
            if parent and CONTROL_TYPES.get(parent.CurrentControlType) in PRESSED_BY_CONTENT:
                return parent
    except (comtypes.COMError, OSError, ValueError):
        pass
    return el


def _get(el, pid, cached: bool):
    try:
        return el.GetCachedPropertyValue(pid) if cached else el.GetCurrentPropertyValue(pid)
    except (comtypes.COMError, OSError, ValueError):
        return None


def _node(el, cached: bool, origin: tuple[float, float]) -> dict:
    g = lambda p: _get(el, p, cached)  # noqa: E731
    t = CONTROL_TYPES.get(g(P_TYPE) or 0, "Custom")
    rect = g(P_RECT)
    node: dict = {"type": t, "aid": g(P_AID) or "", "name": g(P_NAME),
                  "cls": locator.normalise_class(g(P_CLASS) or "") or None,
                  "fw": g(P_FW) or None, "enabled": bool(g(P_ENABLED)),
                  "visible": not bool(g(P_OFFSCREEN)), "focusable": bool(g(P_FOCUSABLE)),
                  "access_key": g(P_ACCESS) or None, "accel": g(P_ACCEL) or None,
                  "help": g(P_HELP) or None, "rid": tuple(g(P_RUNTIME) or ()),
                  "hwnd": int(g(P_HWND) or 0)}  # fmt: skip
    if g(P_FOCUSED):
        node["focused"] = True  # the keyboard focus when the tree was read
    if rect and len(rect) == 4:
        node["rect"] = [
            int(rect[0] - origin[0]),
            int(rect[1] - origin[1]),
            int(rect[2]),
            int(rect[3]),
        ]
    pats = []
    if g(P_HAS_INVOKE):
        pats.append("invoke")
    if g(P_HAS_VALUE):
        pats.append("value")
        if g(P_PASSWORD):
            node["password"] = True
        else:
            node["value"] = g(P_VALUE)
        node["readonly"] = bool(g(P_READONLY))
    if g(P_HAS_TOGGLE):
        pats.append("toggle")
        node["toggle"] = TOGGLE.get(g(P_TOGGLE))
    if g(P_HAS_SELITEM):
        pats.append("select")
        node["selected"] = bool(g(P_SELECTED))
    if g(P_HAS_EXPAND):
        pats.append("expand")
        node["expand"] = EXPAND.get(g(P_EXPAND))
    if t in ("Table", "DataGrid"):
        node["rows"], node["columns"] = g(P_ROWS), g(P_COLS)
    if pats:
        node["patterns"] = pats
    return node


def capture(hwnd_or_element) -> dict | None:
    """The raw control tree of a window (UI Automation control view), in one cross-process call.
    Raw: names and values as the application shows them; pass it to `privacy.clean` at once.
    Data rows beyond MAX_ROWS per grid or list are left out and counted in `truncated`."""
    el = (
        element_from_handle(hwnd_or_element)
        if isinstance(hwnd_or_element, int)
        else hwnd_or_element
    )
    try:
        el = el.BuildUpdatedCache(_cache_request())
    except (comtypes.COMError, OSError):
        return None
    rect = _get(el, P_RECT, True) or (0, 0, 0, 0)
    origin = (rect[0], rect[1])
    count = [0]

    def walk(e, depth: int, parent_hwnd: int = 0) -> dict:
        node = _node(e, True, origin)
        count[0] += 1
        own = node.get("hwnd") or 0
        if own and own != parent_hwnd:  # a control with a window of its own: what Win32 says
            try:
                if depth == 0:
                    node["frame"] = winlook.window_facts(own)
                else:
                    node["win32"] = winlook.control_facts(own, node["type"], node.get("cls") or "")
            except (OSError, ValueError):
                pass
        if depth >= MAX_DEPTH or count[0] >= MAX_NODES:
            node["truncated"] = "depth or size limit"
            return node
        try:
            kids = e.GetCachedChildren()
        except (comtypes.COMError, OSError):
            kids = None
        children, rows, skipped = [], 0, 0
        grid = node["type"] in ("DataGrid", "Table")
        for i in range(kids.Length if kids else 0):
            k = kids.GetElement(i)
            kt = CONTROL_TYPES.get(_get(k, P_TYPE, True) or 0, "Custom")
            kh = _get(k, P_HWND, True) or 0
            if kt == "Window" and kh and root_window(kh) == kh:
                continue  # an owned top-level window (a dialog): it is a window of its own
            if kh and class_name(kh) in IGNORED_CLASSES:
                continue
            if kt in ("DataItem", "ListItem", "TreeItem") or (grid and kt == "Custom" and i > 0):
                rows += 1
                if rows > MAX_ROWS:
                    skipped += 1
                    continue
            if count[0] >= MAX_NODES:
                skipped += 1
                continue
            children.append(walk(k, depth + 1, own or parent_hwnd))
        if children:
            node["children"] = children
        if skipped:
            node["truncated"] = f"{skipped} item(s) not kept"
        return node

    return walk(el, 0)


_element_request = None


def describe_now(el, hwnd: int = 0) -> dict | None:
    """A quick description of one element in one cross-process call (no ancestors): for the
    mouse hook, which must answer fast and before a click closes the window."""
    global _element_request
    try:
        if _element_request is None:
            cr = auto().CreateCacheRequest()
            for pid in PROPS:
                cr.AddProperty(pid)
            cr.TreeScope = 1  # the element only
            _element_request = cr
        c = el.BuildUpdatedCache(_element_request)
        node = _node(c, True, (0, 0))
        node["pid"] = _get(c, P_PID, True)
        own = _get(c, P_HWND, True) or hwnd
        node["window"] = root_window(own) if own else 0
        node["ancestors"], node["ancestor_types"] = [], []
        node.pop("rect", None)
        return node
    except (comtypes.COMError, OSError, ValueError):
        return None


def describe(el) -> dict | None:
    """What an event's element is (raw, in memory): its properties, runtime id, process, the
    runtime ids of its ancestors and the top-level window it is in."""
    try:
        node = _node(el, False, (0, 0))
        node["pid"] = _get(el, P_PID, False)
        hwnd = _get(el, P_HWND, False) or 0
        top = root_window(hwnd) if hwnd else 0
        walker = auto().ControlViewWalker
        root = auto().GetRootElement()
        ancestors, types, cur, steps = [], [], el, 0
        while steps < 60 and not (top and (_get(cur, P_HWND, False) or 0) == top):
            parent = walker.GetParentElement(cur)
            if not parent or auto().CompareElements(parent, root):
                break
            ancestors.append(tuple(_get(parent, P_RUNTIME, False) or ()))
            types.append(CONTROL_TYPES.get(_get(parent, P_TYPE, False) or 0, "Custom"))
            if not top:
                h = _get(parent, P_HWND, False) or 0
                top = root_window(h) if h else 0
            cur, steps = parent, steps + 1
        node["ancestors"] = ancestors
        node["ancestor_types"] = types
        node["window"] = int(top or _get(cur, P_HWND, False) or 0)
        node.pop("rect", None)
        return node
    except (comtypes.COMError, OSError, ValueError):
        return None


def by_rid(window: int, rid: tuple):
    """The live element with this runtime id in a window, or None."""
    try:
        cond = auto().CreatePropertyCondition(P_RUNTIME, list(rid))
        return element_from_handle(window).FindFirst(TREE_SCOPE_SUBTREE, cond) or None
    except (comtypes.COMError, OSError, ValueError):
        return None


def item_names(el) -> list[str]:
    """Raw names of the ListItems under an element (a combo box's list): for privacy.code_list."""
    try:
        cond = auto().CreatePropertyCondition(P_TYPE, TYPE_IDS["ListItem"])
        arr = el.FindAll(TREE_SCOPE_SUBTREE, cond)
        return [arr.GetElement(i).CurrentName or "" for i in range(min(arr.Length, 200))]
    except (comtypes.COMError, OSError, AttributeError):
        return []


def find_rid(tree: dict, rid: tuple) -> list[int] | None:
    """The child-index path to the node with this runtime id in a raw tree."""
    if not rid:
        return None
    if tree.get("rid") == rid:
        return []
    for i, c in enumerate(tree.get("children") or []):
        sub = find_rid(c, rid)
        if sub is not None:
            return [i, *sub]
    return None


def strip_rids(tree: dict) -> None:
    tree.pop("rid", None)
    for c in tree.get("children") or []:
        strip_rids(c)


# --- acting on elements (verify, and scripted evidence runs) -----------------------------------


def find_all(window_el, selector: str) -> list:
    """Live elements in a window that match a canonical locator (no Index: verify resolves the
    path itself)."""
    a = auto()
    p = locator.parse(selector)
    conds = []
    if p.get("AutomationId"):
        conds.append(a.CreatePropertyCondition(P_AID, p["AutomationId"]))
    if p.get("Name"):
        conds.append(a.CreatePropertyCondition(P_NAME, p["Name"]))
    if p.get("ControlType") in TYPE_IDS:
        conds.append(a.CreatePropertyCondition(P_TYPE, TYPE_IDS[p["ControlType"]]))
    cond = (
        conds[0]
        if len(conds) == 1
        else a.CreateAndConditionFromArray(conds)
        if conds
        else a.CreateTrueCondition()
    )
    arr = window_el.FindAll(TREE_SCOPE_SUBTREE, cond)
    return [arr.GetElement(i) for i in range(arr.Length if arr else 0)]


def _pattern(el, pid, iface):
    unk = el.GetCurrentPattern(pid)
    return unk.QueryInterface(iface) if unk else None


def invoke(el) -> str:
    """Press a control the way its pattern allows; returns which pattern did it."""
    for pid, iface, call in (
        (PAT_INVOKE, UIA.IUIAutomationInvokePattern, "Invoke"),
        (PAT_TOGGLE, UIA.IUIAutomationTogglePattern, "Toggle"),
        (PAT_SELITEM, UIA.IUIAutomationSelectionItemPattern, "Select"),
        (PAT_LEGACY, UIA.IUIAutomationLegacyIAccessiblePattern, "DoDefaultAction"),
    ):
        try:
            pat = _pattern(el, pid, iface)
        except comtypes.COMError:
            pat = None
        if pat:
            try:
                getattr(pat, call)()
            except comtypes.COMError as exc:
                # The provider answers after the handler returns; a handler that opens a modal
                # window (ShowDialog, MessageBox) makes the call time out although it worked.
                if exc.hresult in (COR_E_TIMEOUT, UIA_E_TIMEOUT):
                    return call + " (the application is busy: a modal window opened)"
                raise
            return call
    raise RuntimeError("the control has no pattern to press it with")


def post_click(el) -> str | None:
    """BM_CLICK posted to a Win32 button's own window (WinForms, VB6, MFC buttons): returns at
    once. None when the control is not such a button."""
    try:
        hwnd = el.CurrentNativeWindowHandle
        is_button = CONTROL_TYPES.get(el.CurrentControlType) in (
            "Button",
            "CheckBox",
            "RadioButton",
        )
    except comtypes.COMError:
        return None
    if not hwnd or not is_button or "button" not in class_name(hwnd).lower():
        return None
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW(hwnd, 0x00F5, 0, 0)
    return "BM_CLICK"


def pids_focus(pids: set[int]) -> int:
    """The window handle of the focused control, when it belongs to one of these processes. A
    control without a window of its own (every WPF control) gives the nearest window above it:
    the top-level window, which is where WPF takes its keyboard messages from."""
    try:
        el = focused()
        if el.CurrentProcessId not in pids:
            return 0
        walker, steps = auto().ControlViewWalker, 0
        while el and not el.CurrentNativeWindowHandle and steps < 60:
            el, steps = walker.GetParentElement(el), steps + 1
        hwnd = int(el.CurrentNativeWindowHandle or 0) if el else 0
        return hwnd if hwnd and window_pid(hwnd) in pids else 0
    except (comtypes.COMError, OSError, ValueError):
        return 0


KEY_VK = {"Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B, "Delete": 0x2E,
          **{f"F{i}": 0x6F + i for i in range(1, 13)}}  # fmt: skip


def post_key(hwnd: int, key: str) -> bool:
    """Post a plain key (Enter, Tab, Esc, F-keys) to one window of the app: no keyboard input,
    so nothing reaches another application. Chords with Ctrl or Alt are not posted (they need
    the keyboard state): False."""
    vk = KEY_VK.get(key)
    if not hwnd or vk is None:
        return False
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW(hwnd, 0x0100, vk, 1)
    user32.PostMessageW(hwnd, 0x0101, vk, 0xC0000001)
    return True


def window_at(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)


def nudge_input() -> None:
    """A relative mouse move of zero pixels with SendInput: real input from this process that
    moves nothing and clicks nothing. Windows lets a process take the foreground only when it sent
    the last input event (the foreground lock), so a background driver sends this before
    SetForegroundWindow. A recorder's mouse hook sees at most a move, never a click."""

    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MI), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    move = INPUT(type=0)
    move.mi = MI(0, 0, 0, 0x0001, 0, 0)  # MOUSEEVENTF_MOVE, relative, 0 x 0
    user32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT))


def mouse_click(x: int, y: int) -> None:
    """Left click at a screen point with SendInput, then put the cursor back."""

    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MI), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    arr = (INPUT * 2)()
    for i, flag in enumerate((0x0002, 0x0004)):  # LEFTDOWN, LEFTUP
        arr[i].type = 0
        arr[i].mi = MI(0, 0, 0, flag, 0, 0)
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))
    import time as _t

    _t.sleep(0.05)
    user32.SetCursorPos(old.x, old.y)


def set_value(el, text: str) -> None:
    _pattern(el, PAT_VALUE, UIA.IUIAutomationValuePattern).SetValue(text)


def toggle(el) -> None:
    _pattern(el, PAT_TOGGLE, UIA.IUIAutomationTogglePattern).Toggle()


def select(el) -> None:
    _pattern(el, PAT_SELITEM, UIA.IUIAutomationSelectionItemPattern).Select()


def expand(el, open_: bool = True) -> None:
    pat = _pattern(el, PAT_EXPAND, UIA.IUIAutomationExpandCollapsePattern)
    pat.Expand() if open_ else pat.Collapse()


def toggle_state(el) -> str | None:
    return TOGGLE.get(_get(el, P_TOGGLE, False))


# --- a picture of one window (optional) --------------------------------------------------------


class _BMI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]  # fmt: skip


def screenshot(hwnd: int, path: Path) -> bool:
    """A PNG of one window only (PrintWindow), never the screen around it."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
            return False
        bmi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        raw = bytearray()
        for y in range(h):
            row = buf.raw[y * w * 4 : (y + 1) * w * 4]
            raw.append(0)
            for x in range(0, len(row), 4):
                raw += bytes((row[x + 2], row[x + 1], row[x]))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return True
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
