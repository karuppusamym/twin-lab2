# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Record a person (or a script) using a Windows desktop application, through UI Automation.

A desktop recording is a folder holding:

- `desktop.json`: the session: format, when, the processes recorded (executable name, the
  toolkit detected and the signals it rests on), which UI Automation events were listened to and
  what was polled or hooked instead, and notes;
- `steps.jsonl`: one JSON object per user action (shape below);
- `trees/<seq>.json`: the value-free control tree of the step's window after the action
  (`twinlab2_recorder.desktop.privacy` decides what is structure and what is data);
- `screens/<seq>.png`: a picture of that window only, when `screenshots=True` (off by default:
  pictures show data, and stay in `evidence/` like everything else here).

A step: `{"seq", "ts", "surface": "desktop", "action", "process", "pid", "window": {"selector",
"title", "props"}, "target": {"selector", "matches", "path", "candidates", "props", "index"},
"selector", "via", "tree", "screenshot", "url": null, "dom": null}` plus, by action,
`typed_shape`, `item`, `state`, `key`, `menu_path`, `focus_after`, `invoked`, `effects` (what the
application did by itself because of the step: `[{"action": "select", "selector", "container",
"item_is": "data", "window", "via", "ts"}]`; seen, not a user step). `action` is one of
`click`, `double_click`, `right_click`, `type`, `select`, `toggle`, `expand`, `collapse`, `menu`,
`key`, `window_open`, `window_close`, `menu_open`, `focus`. `url` and `dom` are the web recorder's fields,
kept empty, so desktop and web steps can share one timeline (`twinlab2_recorder.desktop.split.interleave`).

How each action is seen (and where UI Automation events were not enough):

| Action | Source |
|---|---|
| click, double_click, right_click | low-level mouse hook (WH_MOUSE_LL) on a thread of its own, then `ElementFromPoint` (the caption a WPF button, check box or menu item draws is taken as the control); replaced by the pattern event for the same control that follows within 0.8 s; the step keeps the time of the mouse press, so a window the click opens comes after it |
| click on a button by a pattern (a script, an accessibility tool) | `Invoke_Invoked` event, when the toolkit raises it; one that follows a key press within 0.4 s is what the key did (Enter on the default button) and is noted on the key step as `invoked` |
| type | `ValueProperty` change events (shape only, computed in the handler); also read once when focus leaves an edit control, for toolkits that raise no value events; in a password field, a character key pressed while it has the focus (WPF's PasswordBox raises no value event; the key itself is not kept) |
| select | `SelectionItem_ElementSelected` event. One the application raises by itself (a grid that selects its row again after Save: the user's last input was not on that item, in its list, grid, tree, combo box or tab control) is no step: it is an `effects` entry of the step that caused it (`self_made_selection`) |
| toggle, expand, collapse | `ToggleState` / `ExpandCollapseState` property change events |
| menu | `Invoke_Invoked` on a MenuItem; `menu_open` when a menu window appears (polled; a WPF `Popup` window that holds menu items is one) |
| key (Enter, Tab, Esc, F-keys, Delete, chords with Ctrl or Alt) | low-level keyboard hook; plain characters are never recorded |
| window_open, window_close, focus (between windows) | polled every 250 ms (EnumWindows, GetForegroundWindow) |

Only windows of the recorded processes are read: the hooks check the process of the window under
the mouse or in the foreground before anything else, and every UI Automation event is dropped
unless its element belongs to a recorded process.
"""

from __future__ import annotations

import json
import os as _os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from twinlab2_recorder.desktop import FORMAT, SURFACE, require
from twinlab2_recorder.desktop import detect
from twinlab2_recorder.desktop import locator
from twinlab2_recorder.desktop import privacy

DEBUG = bool(_os.environ.get("TWINLAB_DESKTOP_DEBUG"))
SETTLE_S = 0.4
CLICK_GRACE_S = 0.8
KEY_GRACE_S = 0.4  # an Invoke event this soon after a key press is what the key did
MAX_BATCH_S = 3.0
POLL_S = 0.25
REHOOK_S = 20.0  # the input hooks are renewed this often (Windows may remove a slow one)
NAMED_KEYS = {0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc", 0x2E: "Delete", 0x21: "PageUp",
              0x22: "PageDown", 0x24: "Home", 0x23: "End",
              **{0x70 + i: f"F{i + 1}" for i in range(12)}}  # fmt: skip
ARROWS = {0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down"}
POPUP_TYPES = {"Menu", "ToolTip"}
POPUP_CLASSES = (
    "#32768",
    "tooltips_class32",
    "ComboLBox",
    "WindowsForms10.Window.808.",
    "WindowsForms10.Window.20808.",
)  # 808: tool strip drop-downs
# Keys that put a character into a field (or take one out). Which key is never looked at
# further: the hook only says "a key was typed", for password fields that raise no value event.
TYPING_KEYS = {0x08, 0x20, 0xE7, *range(0x30, 0x3A), *range(0x41, 0x5B), *range(0x60, 0x70),
               *range(0xBA, 0xC1), *range(0xDB, 0xE0), 0xE2}  # fmt: skip
MODIFIER_KEYS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}
# Controls that hold selectable items: a selection in one belongs to the user when the user's
# last action was anywhere in it.
ITEM_CONTAINERS = {"ComboBox", "List", "DataGrid", "Table", "Tree", "Tab"}


def self_made_selection(sel: dict, last: dict | None, popup: bool = False) -> bool:
    """Whether a selection event is the application's own doing and not a user step.

    `sel` is the selected item (`rid`, `ancestors`: runtime ids from its parent up,
    `ancestor_types`), `last` the control of the user's last action: the control a mouse button
    was pressed on, the control that had the keyboard focus when a key was pressed, or the
    control of the last pattern action recorded (a script expanding a combo box before it
    selects in it). The selection is the user's when that control is the item, is inside it,
    holds it, is its sibling (the next radio button reached with an arrow key) or is in the
    same list, grid, tree, combo box or tab control. It is the application's own (a grid filled
    again with its first row selected after Save) only when both are known and unrelated.
    Anything unknown, and every selection in a drop-down list, stays a user step."""
    if last is None or popup:
        return False
    rid, a_rid = tuple(sel.get("rid") or ()), tuple(last.get("rid") or ())
    ancestors = [tuple(r) for r in sel.get("ancestors") or ()]
    a_ancestors = [tuple(r) for r in last.get("ancestors") or ()]
    if not rid or not a_rid or not ancestors:
        return False
    if a_rid == rid or a_rid in ancestors or rid in a_ancestors:
        return False
    if a_ancestors and a_ancestors[0] == ancestors[0]:
        return False
    types = sel.get("ancestor_types") or []
    containers = {r for r, t in zip(ancestors, types, strict=False) if t in ITEM_CONTAINERS}
    return not (containers & {a_rid, *a_ancestors})


SCRIPT_SELECT_S = 1.0  # a selection this long after a scripted action is the script's own step


def scripted_selection(t: float, last: dict | None) -> bool:
    """Whether a selection that `self_made_selection` would give to the application is a step of
    a script (an accessibility tool, a robot or a test driving the application through UI
    Automation patterns): the user's last action came from a pattern event with no mouse or key
    behind it (`via` uia-..., not `mouse`), and the selection came at least SCRIPT_SELECT_S after
    it. An application selects by itself at once, while it handles the action (a grid selecting
    its row again after Save); a script that opens a navigation pane, waits, then selects one of
    its items (Windows Calculator's modes) acts a moment later, with no input in between."""
    action = (last or {}).get("action")
    if not isinstance(action, dict) or action.get("mouse"):
        return False
    via = str(action.get("via") or "")
    return via.startswith("uia-") and t - float(last.get("t") or 0) >= SCRIPT_SELECT_S


def order_batch(batch: list[dict]) -> list[dict]:
    """The actions of one batch in the order they happened: by time, where the time of an
    action is the user's input (the mouse press, the key press) when there was one.

    A click known only from its Invoke event (a pattern call: no mouse press gave it a time)
    carries the time its event handler ran, which can be after the window poll already saw the
    modal window the click opened. The window's owner cannot be clicked once that window is
    up, so such a click in the owner within CLICK_GRACE_S after the window opened is put before
    it."""
    for w in batch:
        if w["kind"] != "window_open" or not w.get("owner"):
            continue
        for a in batch:
            if (a["kind"] in ("click", "menu") and a.get("via") == "uia-invoke"
                    and not a.get("mouse") and (a.get("desc") or {}).get("window") == w["owner"]
                    and 0 <= a["t"] - w["t"] < CLICK_GRACE_S):  # fmt: skip
                a["t"] = w["t"] - 0.001
    return sorted(batch, key=lambda a: a["t"])


def popup_content(raw: dict | None) -> set[str]:
    """What a WPF popup window holds: {"MenuItem"} for a menu's drop-down, {"ListItem"} for a
    combo box's list. WPF shows both in a top-level window of its own whose window class is the
    application's `HwndWrapper[...]` and whose UI Automation element is a Window of class
    `Popup`, so neither the window class nor the control type says which it is."""
    if not raw or raw.get("type") != "Window" or raw.get("cls") != "Popup":
        return set()
    return {n.get("type") for n, _a, _i in locator.walk(raw)} & {"MenuItem", "ListItem", "ToolTip"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- input hooks, on a thread of their own --------------------------------------------------------


class _Hooks(threading.Thread):
    """Low-level mouse and keyboard hooks. They need a message loop that never blocks, so they
    run here and only queue (x, y) or the chord; the recorder reads the element afterwards."""

    def __init__(self, out: queue.Queue, wanted: Callable[[int], bool],
                 resolve: Callable[[int, int], object] | None = None,
                 on_key: Callable[[int], object] | None = None):  # fmt: skip
        super().__init__(daemon=True, name="twinlab-desktop-hooks")
        self.out, self.wanted, self.resolve, self.on_key = out, wanted, resolve, on_key
        self.thread_id = 0
        self.ready = threading.Event()
        self.failed: str | None = None
        self.renewed = 0  # how many times the hooks were put in again
        self._last_down = (0.0, 0, 0)

    def run(self) -> None:  # noqa: C901 - one Win32 message loop
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        class MSLL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.c_size_t)]  # fmt: skip

        class KBDLL(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]  # fmt: skip

        def pid_of(hwnd) -> int:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value

        dbl = user32.GetDoubleClickTime() / 1000.0

        def on_mouse(code, wparam, lparam):
            try:
                if code == 0 and wparam in (0x201, 0x204):
                    info = ctypes.cast(lparam, ctypes.POINTER(MSLL)).contents
                    x, y = info.pt.x, info.pt.y
                    if self.wanted(pid_of(user32.WindowFromPoint(info.pt))):
                        t = time.time()
                        kind = "right_click" if wparam == 0x204 else "click"
                        last_t, lx, ly = self._last_down
                        if (
                            kind == "click"
                            and t - last_t < dbl
                            and abs(x - lx) < 5
                            and abs(y - ly) < 5
                        ):
                            kind = "double_click"
                        if kind != "right_click":
                            self._last_down = (t, x, y)
                        # The element is taken now, before the click is delivered: a moment
                        # later a dialog the click opens may cover the point.
                        el = quick = None
                        if self.resolve is not None:
                            try:
                                el, quick = self.resolve(x, y, user32.WindowFromPoint(info.pt))
                            except Exception:  # noqa: BLE001
                                el = quick = None
                        self.out.put({"kind": kind, "x": x, "y": y, "t": t, "el": el, "quick": quick,
                                      "injected": bool(info.flags & 1)})  # fmt: skip
            except Exception:  # noqa: BLE001 - a hook must never raise
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def on_key(code, wparam, lparam):
            try:
                if code == 0 and wparam in (0x100, 0x104):
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLL)).contents
                    fg = user32.GetForegroundWindow()
                    if self.wanted(pid_of(fg)):
                        t = time.time()
                        chord = self._chord(info.vk, user32)
                        if chord:
                            quick = None
                            if self.on_key is not None:
                                try:
                                    quick = self.on_key(fg)
                                except Exception:  # noqa: BLE001
                                    quick = None
                            self.out.put({"kind": "key", "key": chord, "t": t, "quick": quick,
                                          "injected": bool(info.flags & 0x10)})  # fmt: skip
                        elif info.vk in TYPING_KEYS:
                            # Not which key: only that one was typed (see Recorder._typing).
                            self.out.put({"kind": "typing", "t": t})
                        elif info.vk not in MODIFIER_KEYS:
                            # An arrow, a navigation key: only that the user pressed a key
                            # (a selection that follows in the focused control is the user's).
                            self.out.put({"kind": "input", "t": t})
            except Exception:  # noqa: BLE001
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        if self.resolve is not None:
            import comtypes

            comtypes.CoInitializeEx(0)  # the multi-threaded apartment, as the recorder
        self._procs = (HOOKPROC(on_mouse), HOOKPROC(on_key))
        mod = kernel32.GetModuleHandleW(None)

        def install():
            return (user32.SetWindowsHookExW(14, self._procs[0], mod, 0),
                    user32.SetWindowsHookExW(13, self._procs[1], mod, 0))  # fmt: skip

        hm, hk = install()
        self.thread_id = kernel32.GetCurrentThreadId()
        if not hm or not hk:
            self.failed = f"SetWindowsHookEx failed (error {ctypes.get_last_error()})"
        self.ready.set()
        # Windows removes a low-level hook, without telling, when one callback took too long (a
        # busy application answering UI Automation). The hooks are put in again every
        # REHOOK_S seconds, so a removed hook costs at most that long of input.
        user32.SetTimer(None, 0, int(REHOOK_S * 1000), None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0113 and not msg.hWnd and hm and hk:  # WM_TIMER: renew
                new_m, new_k = install()
                if new_m and new_k:
                    user32.UnhookWindowsHookEx(hm)
                    user32.UnhookWindowsHookEx(hk)
                    hm, hk = new_m, new_k
                    self.renewed += 1
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for h in (hm, hk):
            if h:
                user32.UnhookWindowsHookEx(h)

    @staticmethod
    def _chord(vk: int, user32) -> str | None:
        down = lambda k: bool(user32.GetAsyncKeyState(k) & 0x8000)  # noqa: E731
        ctrl, alt, shift = down(0x11), down(0x12), down(0x10)
        if vk in MODIFIER_KEYS:
            return None  # a modifier on its own
        name = NAMED_KEYS.get(vk)
        if name is None and (ctrl or alt) and vk in ARROWS:
            name = ARROWS[vk]
        if name is None and (ctrl != alt) and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
            name = chr(vk)  # a shortcut (Ctrl+S, Alt+F); Ctrl+Alt is AltGr typing: never kept
        if name is None:
            return None
        mods = [m for m, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift)) if on]
        return "+".join([*mods, name])

    def stop(self) -> None:
        if self.thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT


# --- the recorder ----------------------------------------------------------------------------------


# Actions a person takes (the recorder's own events: window opened, closed, focus, menu shown).
USER_INPUT = {"click", "double_click", "right_click", "type", "select", "toggle", "expand",
              "collapse", "menu", "key"}  # fmt: skip


class Recorder:
    """Records the windows of a set of processes until they exit, `stop()` says so, Ctrl+C, or
    `max_seconds`."""

    last_user: dict | None = None  # the user's last action: {"desc", "t", "action"}
    effects_unplaced = 0  # selections the application made, their cause already written
    journal = None  # a workstation session's capture journal (set in __init__)

    def __init__(self, out_dir: Path, pids: set[int], *, screenshots: bool = False,
                 settle_ms: int = 400, max_seconds: float = 1800, hooks: bool = True,
                 stop: Callable[[], bool] | None = None, pictures: bool = True,
                 session: bool = False, levels: dict[int, str] | None = None,
                 admit: Callable[[int], str | None] | None = None,
                 unfocused_values: bool = False, journal=None,
                 resume: bool = False,
                 exclude_window: Callable[[int], str | None] | None = None,
                 exclusion_masks: Callable[[int], list | None] | None = None,
                 paused: Callable[[], bool] | None = None) -> None:  # fmt: skip
        require()
        from twinlab2_recorder.desktop import win

        self.win = win
        self.out = Path(out_dir)
        (self.out / "trees").mkdir(parents=True, exist_ok=True)
        self.pids = set(pids)
        # In a workstation session (twinlab2_recorder.workstation) the recorder starts with the processes
        # it has (maybe none), is given more by add_process(), and stops only on stop(). A
        # process at level "steps" gets its steps, without tree files or pictures.
        self.session = session
        self.levels: dict[int, str] = dict(levels or {})
        self.adding: queue.Queue = queue.Queue()
        self.frame_pid: dict[int, int] = {}  # a modern application's frame window -> its process
        # admit(pid) -> "full" | "steps" | None: a process the session records, decided the
        # moment its first input or event arrives (a robot acts before any poll would see it).
        self.admit = admit
        self.refused: set[int] = set()
        # The PC's exclude list (twinlab2_recorder.workstation.exclude), in a session that has one:
        # exclude_window(hwnd) names the entry a window falls under (its program or its title),
        # and such a window gets no event, tree or picture; exclusion_masks(hwnd) gives where
        # excluded windows cover a picture of hwnd (None: the picture is not kept).
        self.exclude_window = exclude_window
        self.exclusion_masks = exclusion_masks
        self.excluded_events = 0  # counted, never described
        # A paused session (twinlab2_recorder.workstation.pause): no event is taken, no element or
        # window read, until it resumes.
        self.paused_fn = paused
        # A robot (UiPath's Simulate input) sets values without the keyboard focus: in a session
        # recording one, such a change is a step too.
        self.unfocused_values = unfocused_values
        self.screenshots = screenshots
        # Pictures without data (twinlab2_recorder.desktop.pictures); what data was seen stays in memory.
        self.pictures = pictures
        self.secrets: set[str] = set()
        self.kept_texts: set[str] = set()
        self.pending_pictures: list[tuple] = []
        self.settle = settle_ms / 1000.0
        self.max_seconds = max_seconds
        self.use_hooks = hooks
        self.stop_fn = stop
        self.q: queue.Queue = queue.Queue()
        self.seq = 0
        self.known: set[str] = set()  # words seen as data, in memory only
        self.structure: set[str] = set()  # words the application shows as structure
        self.trees: dict[int, tuple[dict, dict]] = {}  # hwnd -> (raw, clean), last seen
        self.titles: dict[int, dict] = {}  # hwnd -> window locator info
        self.windows: set[int] = set()
        self.popups: dict[int, int] = {}  # menu window -> owner window, while open
        self.popup_owner: dict[int, int] = {}  # every popup seen -> owner window
        self.last_combo: tuple[dict, float] | None = None  # the combo box last expanded
        self.opened_at: dict[int, float] = {}  # window -> when it was first seen
        self.last_window: dict = {"selector": None, "title": None, "props": {}}
        self.combo_items: dict[tuple, dict] = {}
        self.pending_items: dict | None = None  # items of the drop-down list seen last
        self.pending: list[dict] = []
        self.last_activity = 0.0
        self.focus: dict | None = None
        self.typed_since_focus = False
        self.registered: set[int] = set()
        self.fg = 0
        self.processes: dict[int, dict] = {}
        self.notes: list[str] = []
        # resume: a workstation collector started again after a failure goes on with the steps
        # already written (and their numbers), instead of writing over them.
        steps_file = self.out / "steps.jsonl"
        if resume and steps_file.is_file():
            for line in steps_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    self.seq = max(self.seq, int(json.loads(line).get("seq") or 0))
                except (ValueError, AttributeError, TypeError):
                    pass
            self.notes.append(f"started again after a failure, after step {self.seq}")
        self.file = steps_file.open("a" if resume else "w", encoding="utf-8")
        self.started = _now()
        self._handlers: list = []
        # A workstation session's capture journal (twinlab2_recorder.workstation.journal): an action's
        # event line when it is captured, its step when the batch settles.
        self.journal = journal
        self._marked: set[str] = set()  # event ids captured, not yet written or dropped

    # -- events from UI Automation (their threads) ------------------------------------------------

    def add_process(self, pid: int, level: str = "full") -> None:
        """Record this process too, from now on (any thread; the recorder's loop takes it)."""
        self.adding.put((int(pid), level))

    def _take_added(self) -> None:
        while True:
            try:
                pid, level = self.adding.get_nowait()
            except queue.Empty:
                return
            self.levels[pid] = level
            self.pids.add(pid)
            if pid not in self.processes:
                self._process(pid)

    def _wanted(self, pid: int) -> bool:
        """Is this process recorded? Asks the session about one it has not seen yet."""
        if pid in self.pids:
            return True
        if self.admit is None or not pid or pid in self.refused:
            return False
        try:
            level = self.admit(pid)
        except Exception:  # noqa: BLE001 - the hook must answer at once
            level = None
        if level in ("full", "steps"):
            self.levels[pid] = level
            self.pids.add(pid)
            self.adding.put((pid, level))
            return True
        self.refused.add(pid)
        return False

    def _is_paused(self) -> bool:
        fn = getattr(self, "paused_fn", None)
        return bool(fn is not None and fn())

    def _wanted_now(self, pid: int) -> bool:
        """The hooks' question: nothing is wanted while the session is paused."""
        return not self._is_paused() and self._wanted(pid)

    def _level(self, pid: int | None) -> str:
        return self.levels.get(pid, "full") if pid is not None else "full"

    def _ours(self, el) -> bool:
        if self._is_paused():
            return False
        try:
            return self._wanted(el.CurrentProcessId)
        except Exception:  # noqa: BLE001
            return False

    def _make_handlers(self):
        import comtypes

        rec, win = self, self.win

        class Event(comtypes.COMObject):
            _com_interfaces_ = [win.UIA.IUIAutomationEventHandler]

            def HandleAutomationEvent(self, sender, event_id):
                if not rec._ours(sender):
                    return
                t = time.time()  # when it came: reading the element takes a while
                kind = {win.E_INVOKED: "invoke", win.E_SELECTED: "select"}.get(event_id)
                desc = win.describe(sender)
                if kind and desc:
                    rec.q.put({"kind": kind, "desc": desc, "t": t})

        class Prop(comtypes.COMObject):
            _com_interfaces_ = [win.UIA.IUIAutomationPropertyChangedEventHandler]

            def HandlePropertyChangedEvent(self, sender, prop, value):
                if not rec._ours(sender):
                    return
                value = win.variant(value)
                ev: dict = {"t": time.time()}
                if prop == win.P_VALUE:
                    try:
                        secret = bool(sender.CurrentIsPassword)
                        # Only what the user changes: a value the application fills in (a
                        # dialog loading its record) changes controls without keyboard focus.
                        if not sender.CurrentHasKeyboardFocus and not rec.unfocused_values:
                            return
                        if sender.CurrentControlType == 50003:
                            return  # a combo box: its selection event says what was chosen
                        if not secret:
                            value = sender.GetCurrentPropertyValue(win.P_VALUE)
                    except Exception:  # noqa: BLE001
                        secret = True
                    ev["kind"] = "value"
                    if secret:
                        ev["password"] = True
                    else:
                        rec.known |= privacy.words(value) - rec.structure
                        ev["shape"] = privacy.shape(
                            value if isinstance(value, str) else str(value or "")
                        )
                    value = None  # the value itself goes no further
                elif prop == win.P_TOGGLE:
                    ev["kind"], ev["state"] = "toggle", win.TOGGLE.get(value)
                    if ev["state"] is None:
                        # WPF raises the event a second time with a value that is no toggle
                        # state: the state is then read from the control.
                        ev["state"] = win.toggle_state(sender)
                elif prop == win.P_EXPAND:
                    ev["kind"], ev["state"] = "expand", win.EXPAND.get(value)
                else:
                    return
                ev["desc"] = win.describe(sender)
                if ev["desc"]:
                    rec.q.put(ev)

        class Focus(comtypes.COMObject):
            _com_interfaces_ = [win.UIA.IUIAutomationFocusChangedEventHandler]

            def HandleFocusChangedEvent(self, sender):
                if rec._ours(sender):
                    t = time.time()
                    desc = win.describe(sender)
                    if desc:
                        rec.q.put({"kind": "focus", "desc": desc, "t": t})

        return Event(), Prop(), Focus()

    def _register(self, hwnd: int) -> None:
        if hwnd in self.registered:
            return
        import ctypes

        win, a = self.win, self.win.auto()
        try:
            el = win.element_from_handle(hwnd)
            for eid in (win.E_INVOKED, win.E_SELECTED):
                a.AddAutomationEventHandler(
                    eid, el, win.TREE_SCOPE_SUBTREE, None, self._handlers[0]
                )
            ids = (ctypes.c_int * 3)(win.P_VALUE, win.P_TOGGLE, win.P_EXPAND)
            a.AddPropertyChangedEventHandlerNativeArray(el, win.TREE_SCOPE_SUBTREE, None,
                                                        self._handlers[1], ids, 3)  # fmt: skip
            self.registered.add(hwnd)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(f"could not listen to a window's events: {type(exc).__name__}")

    # -- the main loop --------------------------------------------------------------------------

    def run(self) -> Path:
        win = self.win
        self._handlers = list(self._make_handlers())
        hooks = None
        try:
            win.auto().AddFocusChangedEventHandler(None, self._handlers[2])
            for pid in self.pids:
                self._process(pid)
            if self.use_hooks:

                def resolve(x, y, hwnd):
                    el = win.click_target(win.element_from_point(x, y))
                    return el, win.describe_now(el, hwnd or 0)

                hooks = _Hooks(self.q, self._wanted_now, resolve)
                hooks.start()
                hooks.ready.wait(5)
                if hooks.failed:
                    self.notes.append(f"input hooks unavailable ({hooks.failed}): mouse clicks "
                                      "and key chords are not recorded, only pattern events")  # fmt: skip
            started = time.monotonic()
            next_poll = 0.0
            was_paused = False
            try:
                while True:
                    now = time.monotonic()
                    if self.stop_fn is not None and self.stop_fn():
                        break
                    if now - started > self.max_seconds:
                        self.notes.append(f"stopped after max_seconds={self.max_seconds:.0f}")
                        break
                    if self._is_paused():
                        if not was_paused:  # what was under way began before the pause
                            was_paused = True
                            self._guarded("handling an event", self._drain)
                            self._guarded("writing a step", lambda: self._finish(force=True))
                        self._discard()
                        time.sleep(0.05)
                        continue
                    was_paused = False
                    if now >= next_poll:
                        next_poll = now + POLL_S
                        self._take_added()
                        if not self.session and not any(win.alive(p) for p in self.pids):
                            break
                        self._guarded("reading the windows", self._poll_windows)
                    self._guarded("handling an event", self._drain)
                    self._guarded("writing a step", lambda: self._finish(force=False))
                    time.sleep(0.02)
            except KeyboardInterrupt:
                self.notes.append("stopped with Ctrl+C")
            time.sleep(0.2)
            self._guarded("handling an event", self._drain)
            self._guarded("writing a step", lambda: self._finish(force=True))
        finally:
            if hooks is not None:
                hooks.stop()
            try:
                win.auto().RemoveAllEventHandlers()
            except Exception:  # noqa: BLE001
                pass
            self.file.close()
            self._write_session()
        return self.out

    def _guarded(self, what: str, fn: Callable[[], None]) -> bool:
        """One part of the recording loop. A call into UI Automation or a window that fails
        (the application closed while it was read) loses at most what that call was handling:
        it is counted, each kind of failure noted once in `desktop.json`, and the recording goes
        on, instead of the whole channel ending on the error. -> whether it ran cleanly."""
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001 - noted; the recording goes on
            failures = self.__dict__.setdefault("loop_failures", {})
            key = f"{what}: {type(exc).__name__}"
            failures[key] = failures.get(key, 0) + 1
            if failures[key] == 1:
                self.notes.append(f"a call failed while {key} ({str(exc)[:160]}); what it was "
                                  "handling is not recorded, the recording went on")  # fmt: skip
            return False

    @staticmethod
    def _system() -> dict:
        """The machine settings that change how windows look (twinlab2_recorder.desktop.winlook)."""
        from twinlab2_recorder.desktop import winlook

        try:
            return winlook.system_facts()
        except (OSError, ValueError, AttributeError):
            return {}

    def _check_pictures(self) -> None:
        """OCR every picture while the data values seen are in memory; drop any that shows
        one, or that could not be checked (twinlab2_recorder.desktop.pictures.check). Then forget them."""
        from twinlab2_recorder.desktop import pictures

        if not (self.out / pictures.DIR).is_dir():
            return
        try:
            doc = pictures.check(self.out, self.secrets, self.kept_texts)
        finally:
            self.secrets, self.kept_texts = set(), set()
        if doc["dropped"]:
            self.notes.append(f"{len(doc['dropped'])} picture(s) dropped by the data check "
                              f"({doc['checked_with']})")  # fmt: skip

    def _process(self, pid: int) -> None:
        if pid in self.processes:
            return
        sig = self.win.signals(pid)
        self.processes[pid] = {"pid": pid, "process": sig.get("process"), "signals": sig}

    def _poll_windows(self) -> None:
        win = self.win
        # A modern application's frame window belongs to ApplicationFrameHost: it is the
        # application's window all the same (win.frame_windows).
        frames = win.frame_windows(self.pids)
        self.frame_pid.update(frames)
        now = win.top_windows(self.pids) + list(frames)
        current = set(now)
        for hwnd in now:
            if hwnd in self.windows or hwnd in self.popups:
                continue
            el_type, cls, holds = None, win.class_name(hwnd), set()
            if cls in win.IGNORED_CLASSES:
                continue
            try:
                el = win.element_from_handle(hwnd)
                el_type = win.CONTROL_TYPES.get(el.CurrentControlType)
                if el_type == "Window" and el.CurrentClassName == "Popup":
                    holds = popup_content(win.capture(hwnd)) or {"Popup"}  # a WPF popup
            except Exception:  # noqa: BLE001
                pass
            self._register(hwnd)
            if el_type in POPUP_TYPES or cls.startswith(POPUP_CLASSES) or holds:
                owner = self._owner_window(hwnd)
                self.popups[hwnd] = owner
                self.popup_owner[hwnd] = owner
                if cls == "ComboLBox" or el_type == "List" or "ListItem" in holds:
                    self._combo_list(hwnd)
                if el_type == "Menu" or cls == "#32768" or "808" in cls or "MenuItem" in holds:
                    snap = self._snapshot(hwnd)
                    if snap is not None:
                        self.trees[hwnd] = snap
                    self._add({"kind": "menu_open", "hwnd": hwnd, "owner": owner,
                               "t": time.time(), "snap": snap})  # fmt: skip
                continue
            self.windows.add(hwnd)
            self.opened_at[hwnd] = time.time()
            # A window can come and go within one batch (a message box answered at once): its
            # tree is read now, while it is there.
            snap = self._snapshot(hwnd)
            if snap is not None:
                self.trees[hwnd] = snap
                self._window_info(hwnd, snap)
            self._add({"kind": "window_open", "hwnd": hwnd, "t": time.time(), "snap": snap,
                       "owner": win.owner(hwnd)})  # fmt: skip
        for hwnd in list(self.windows - current):
            self.windows.discard(hwnd)
            self._add({"kind": "window_close", "hwnd": hwnd, "t": time.time()})
        for hwnd in list(set(self.popups) - current):
            self.popups.pop(hwnd, None)
        fg = win.foreground()
        if fg != self.fg and fg in self.windows:
            just_opened = any(
                a["kind"] == "window_open" and a.get("hwnd") == fg for a in self.pending
            )
            if self.fg and not just_opened:
                self._add({"kind": "focus", "hwnd": fg, "t": time.time()})
            self.fg = fg

    def _combo_list(self, hwnd: int) -> None:
        """A combo box's drop-down list opened: its items, kept as a code list when the rule
        allows (twinlab2_recorder.desktop.privacy.code_list), else only counted."""
        raw = self.win.capture(hwnd)
        if raw is None:
            return
        names = [
            n.get("name") or "" for n, _a, _i in locator.walk(raw) if n.get("type") == "ListItem"
        ]
        if names and privacy.code_list(names, self.known):
            kept = [privacy.keep_text(n) for n in names]
            found = {"items": kept}
            for n in kept:
                self.structure |= privacy.words(n)
        elif names:
            found = {"items_are": "data", "item_count": len(names)}
        else:
            return
        # The list can be seen before the expand event that opened it is handled.
        self.pending_items = found
        if self.last_combo is not None:
            self.combo_items[tuple(self.last_combo[0].get("rid") or ())] = found

    def _owner_window(self, popup: int) -> int:
        own = self.win.owner(popup)
        if own in self.windows:
            return own
        return self.fg if self.fg in self.windows else next(iter(self.windows), 0)

    def _marks(self) -> set[str]:
        """Event ids captured, not yet written or dropped."""
        return self.__dict__.setdefault("_marked", set())

    def _excluded(self, hwnd) -> bool:
        """Is this window excluded? A window once excluded stays so until it closes (its close
        is not a step either), even when its title changes."""
        if self.exclude_window is None or not hwnd:
            return False
        seen = self.__dict__.setdefault("excluded_hwnds", set())
        if int(hwnd) in seen:
            return True
        try:
            hit = self.exclude_window(int(hwnd)) is not None
        except Exception:  # noqa: BLE001 - a window gone while it was read is not excluded
            return False
        if hit:
            seen.add(int(hwnd))
        return hit

    def _add(self, action: dict) -> None:
        from twinlab2_recorder.workstation import journal as capture

        hwnd = action.get("hwnd") or (action.get("desc") or {}).get("window")
        if getattr(self, "exclude_window", None) is not None and self._excluded(hwnd):
            self.excluded_events += 1  # in an excluded window: no event, step or journal line
            return
        if "event_id" not in action:  # captured now: its id is permanent from here on
            when = datetime.fromtimestamp(action.get("t") or time.time(), UTC)
            marker = capture.stamp({"action": action.get("kind"), "ts": when.isoformat(
                timespec="milliseconds").replace("+00:00", "Z")}, SURFACE)  # fmt: skip
            action["event_id"], action["captured_at"] = marker["event_id"], marker["captured_at"]
            if self.journal is not None:
                self.journal.event(marker, self.out.name)
        self._marks().add(action["event_id"])
        self.pending.append(action)
        self.last_activity = time.monotonic()

    def _discard(self) -> None:
        """Drop what came in while paused (an event already on its way): nothing is read."""
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def _drain(self) -> None:
        win = self.win
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                return
            kind = ev["kind"]
            if DEBUG:
                import sys as _sys

                print(
                    "DEBUG",
                    kind,
                    {k: v for k, v in ev.items() if k not in ("el",)},
                    file=_sys.stderr,
                )
            if kind in ("click", "double_click", "right_click"):
                try:
                    el = ev.get("el") or win.click_target(win.element_from_point(ev["x"], ev["y"]))
                    desc = win.describe(el)
                    if not desc or desc.get("pid") not in self.pids or not desc.get("window"):
                        # Gone already (the click closed it), or no longer in any window (an
                        # item of a WPF menu that closed): what the hook saw.
                        desc = ev.get("quick") or desc
                except Exception:  # noqa: BLE001
                    desc = None
                if not desc or desc.get("pid") not in self.pids:
                    continue
                if desc.get("type") == "MenuItem" and kind == "click":
                    kind = "menu"
                self._add({"kind": kind, "desc": desc, "t": ev["t"], "via": "mouse-hook",
                           "provisional": kind == "click", "injected": ev.get("injected")})  # fmt: skip
                self._user(desc, ev["t"], self.pending[-1])
            elif kind == "key":
                self._add({"kind": "key", "key": ev["key"], "t": ev["t"], "via": "keyboard-hook",
                           "injected": ev.get("injected"), "hwnd": win.foreground()})  # fmt: skip
                self._user(self.focus, ev["t"], self.pending[-1])
            elif kind == "value":
                self._typed(ev)
                self._user(ev["desc"], ev["t"])
            elif kind == "typing":
                self._typing(ev)
                self._user(self.focus, ev["t"])
            elif kind == "input":
                self._user(self.focus, ev["t"])
            elif kind == "focus":
                self._focus(ev)
            elif kind in ("invoke", "select", "toggle", "expand"):
                self._semantic(ev)

    def _same(self, a: dict, b: dict) -> bool:
        ra, rb = a.get("rid"), b.get("rid")
        return bool(ra and rb) and (
            ra == rb or ra in b.get("ancestors", ()) or rb in a.get("ancestors", ())
        )

    def _user(self, desc: dict | None, t: float, action: dict | None = None) -> None:
        """Note the user's last action: the control it was on (for a key, the control that had
        the keyboard focus) and, when it is a step, the pending step itself."""
        self.last_user = {"desc": desc or {}, "t": t, "action": action}

    def _own_selection(self, ev: dict) -> bool:
        """A selection the application made by itself (see `self_made_selection`): no step. It
        is kept as an effect on the step that caused it, the user's last action, while that
        step is not written yet; later ones are only counted (a note in `desktop.json`)."""
        desc, last = ev["desc"], self.last_user
        types = desc.get("ancestor_types") or []
        popup = desc.get("window") in getattr(self, "popup_owner", {}) or not types
        if last is None or not self_made_selection(desc, last["desc"], popup):
            return False
        if scripted_selection(ev["t"], last):
            return False
        cause = last.get("action")
        if cause is None or not any(a is cause for a in self.pending):
            self.effects_unplaced += 1
            return True
        effects = cause.setdefault("effects", [])
        if not any(e["desc"].get("rid") == desc.get("rid") for e in effects):
            effects.append({"kind": "select", "desc": desc, "t": ev["t"], "via": "uia-selection"})
        return True

    def _semantic(self, ev: dict) -> None:
        desc, kind, state = ev["desc"], ev["kind"], ev.get("state")
        for a in reversed(self.pending[-4:]):  # the same event raised twice (toolkits do)
            if (a.get("raw_kind") == kind and (a.get("desc") or {}).get("rid") == desc.get("rid")
                    and a.get("state") == state
                    and ev["t"] - a.get("t_event", a["t"]) < 0.6):  # fmt: skip
                return
        if kind == "select" and self._own_selection(ev):
            return
        # A mouse click on the same control a moment before was this action.
        before = len(self.pending)
        clicks = [a for a in self.pending if (
            a.get("provisional") and a.get("desc") and self._same(a["desc"], desc)
            and 0 <= ev["t"] - a["t"] < CLICK_GRACE_S)]  # fmt: skip
        self.pending = [a for a in self.pending if not any(a is c for c in clicks)]
        if kind == "invoke" and len(self.pending) == before:
            # No click on it: a key pressed a moment before did this (Enter presses the default
            # button, Esc the cancel button, an access key its control; WPF raises Invoke for
            # each). The key step is what the user did; it notes the control the key pressed.
            for a in reversed(self.pending):
                if a["kind"] == "key" and 0 <= ev["t"] - a["t"] < KEY_GRACE_S:
                    a.setdefault("invoked_desc", desc)
                    return
        # The action happened when the mouse button was pressed: its event came later, and the
        # window poll may already have seen a window the click opened.
        base = {"desc": desc, "t": min([ev["t"], *(c["t"] for c in clicks)]), "t_event": ev["t"],
                "raw_kind": kind, "state": state}  # fmt: skip
        if clicks:
            base["mouse"] = True
            first = min(clicks, key=lambda c: c["t"])  # the click it was: the same event
            if first.get("event_id"):
                base["event_id"], base["captured_at"] = first["event_id"], first.get("captured_at")
            if any(c.get("injected") for c in clicks):
                base["injected"] = True
            for c in clicks:
                if c.get("effects"):
                    base.setdefault("effects", []).extend(c["effects"])
        n = len(self.pending)
        self._semantic_add(kind, state, desc, base, ev)
        if len(self.pending) > n:
            self._user(desc, base["t"], self.pending[-1])

    def _semantic_add(self, kind: str, state, desc: dict, base: dict, ev: dict) -> None:
        if kind == "invoke":
            menu = desc.get("type") == "MenuItem"
            self._add({**base, "kind": "menu" if menu else "click", "via": "uia-invoke"})
        elif kind == "toggle":
            self._add({**base, "kind": "toggle", "via": "uia-property"})
        elif kind == "expand":
            if desc.get("type") == "MenuItem" or state not in ("expanded", "collapsed"):
                return
            action = "expand" if state == "expanded" else "collapse"
            if desc.get("type") == "ComboBox":
                self.last_combo = (desc, ev["t"])
                rid = tuple(desc.get("rid") or ())
                if self.pending_items and rid not in self.combo_items:
                    self.combo_items[rid] = self.pending_items  # its list was seen first
            self._add({**base, "kind": action, "via": "uia-property"})
        elif kind == "select":
            self._add({**base, "kind": "select", "via": "uia-selection"})

    def _typed(self, ev: dict) -> None:
        desc = ev["desc"]
        for a in reversed(self.pending):
            if a["kind"] == "type" and a["desc"].get("rid") == desc.get("rid"):
                a["shape"] = ev.get("shape")
                if a.get("via") == "keyboard-hook":
                    a["via"] = "uia-value-event"  # the toolkit does say it: the better source
                self.last_activity = time.monotonic()
                break
        else:
            self._add({"kind": "type", "desc": desc, "shape": ev.get("shape"), "t": ev["t"],
                       "password": ev.get("password", False), "via": "uia-value-event"})  # fmt: skip
        if self.focus and self.focus.get("rid") == desc.get("rid"):
            self.typed_since_focus = True

    def _typing(self, ev: dict) -> None:
        """A character key was pressed. It matters only while a password field has the keyboard
        focus: WPF's PasswordBox raises no value event and gives no value to compare when focus
        leaves it, so the key press is the only sign that something was typed there. Nothing
        about the key is kept, not even how many there were."""
        desc = self.focus
        if not desc or not desc.get("password"):
            return
        for a in reversed(self.pending):
            if a["kind"] == "type" and a["desc"].get("rid") == desc.get("rid"):
                self.last_activity = time.monotonic()
                break
        else:
            self._add({"kind": "type", "desc": desc, "shape": None, "t": ev["t"],
                       "password": True, "via": "keyboard-hook"})  # fmt: skip
        self.typed_since_focus = True

    def _value_shape(self, rid: tuple) -> tuple[bool, str | None] | None:
        """The current value of the focused control, as (is_password, shape)."""
        win = self.win
        try:
            el = win.focused()
            if tuple(el.GetRuntimeId()) != rid:
                return None
            if el.CurrentIsPassword:
                return True, None
            raw = el.GetCurrentPropertyValue(win.P_VALUE)
            self.known |= privacy.words(raw)
            return False, privacy.shape(raw if isinstance(raw, str) else "")
        except Exception:  # noqa: BLE001
            return None

    def _focus(self, ev: dict) -> None:
        desc = ev["desc"]
        prev = self.focus
        if prev and prev.get("rid") != desc.get("rid") and not self.typed_since_focus:
            # No value event came for the control focus is leaving: compare its value now with
            # the value it had when it got focus (some toolkits raise no value events).
            now = self._last_shape_of(prev)
            if now is not None and now != prev.get("_shape"):
                self._add({"kind": "type", "desc": prev, "shape": now[1], "password": now[0],
                           "t": ev["t"], "via": "poll-at-focus-change"})  # fmt: skip
        for a in reversed(self.pending):
            if a["kind"] == "key" and a.get("key", "").endswith("Tab") and "focus_desc" not in a:
                a["focus_desc"] = desc
                break
        # The value it had when focus came (read in the event handler, so before any typing).
        raw = desc.pop("value", None)
        if "value" in (desc.get("patterns") or []):
            desc["_shape"] = (
                (True, None) if desc.get("password") else (False, privacy.shape(raw or ""))
            )
        else:
            desc["_shape"] = None
        self.focus = desc
        self.typed_since_focus = False

    def _last_shape_of(self, desc: dict):
        """The value shape of a control that just lost focus, read by its runtime id."""
        if "value" not in (desc.get("patterns") or []) or desc.get("readonly"):
            return None
        win = self.win
        try:
            cond = win.auto().CreatePropertyCondition(win.P_RUNTIME, list(desc["rid"]))
            el = win.element_from_handle(desc["window"]).FindFirst(win.TREE_SCOPE_SUBTREE, cond)
            if not el:
                return None
            if el.CurrentIsPassword:
                return (True, None)
            raw = el.GetCurrentPropertyValue(win.P_VALUE)
            self.known |= privacy.words(raw)
            return (False, privacy.shape(raw if isinstance(raw, str) else ""))
        except Exception:  # noqa: BLE001
            return None

    # -- steps ------------------------------------------------------------------------------------

    def _finish(self, force: bool) -> None:
        if not self.pending:
            return
        now = time.monotonic()
        quiet = now - self.last_activity >= self.settle
        oldest = time.time() - min(a["t"] for a in self.pending)
        if not force and not quiet and oldest < MAX_BATCH_S:
            return
        if not force and any(a.get("provisional") and time.time() - a["t"] < CLICK_GRACE_S
                             for a in self.pending):  # fmt: skip
            return
        batch = order_batch(self.pending)
        self.pending = []
        after: dict[int, tuple[dict, dict] | None] = {}
        for a in batch:
            for b in (a, *(a.get("effects") or ())):  # an effect can be in another window
                hwnd = self._window_of(b)
                if hwnd and hwnd not in after:
                    after[hwnd] = self._snapshot(hwnd)
        for a in reversed(batch):  # a window gone by now: its tree as it was when it opened
            hwnd = self._window_of(a)
            if a.get("snap") is not None and after.get(hwnd) is None and a["kind"] == "window_open":
                after[hwnd] = a["snap"]
        # One snapshot per window and batch: a step followed in the same batch by another user
        # action in its window gets a tree (and picture) of a later state. Say so on the step.
        for i, a in enumerate(batch):
            w = self._window_of(a)
            if any(self._window_of(b) == w and b["kind"] in USER_INPUT for b in batch[i + 1 :]):
                if a["kind"] == "window_open" and a.get("snap") is not None:
                    a["own_snap"] = True  # read when it opened: exact
                else:
                    a["tree_late"] = True
        for a in batch:
            if a["kind"] == "type" and self._unchanged(a, after):
                continue
            self._write_step(a, after)
        for hwnd, snap in after.items():
            if snap is not None:
                self.trees[hwnd] = snap
        # Captured but neither written nor waiting: folded into another step, or no step.
        marked = self._marks()
        gone = marked - {a.get("event_id") for a in self.pending}
        if gone:
            marked -= gone
            if self.journal is not None:
                self.journal.drop(sorted(gone), "not a step of its own")

    def _unchanged(self, a: dict, after: dict) -> bool:
        """A value event whose value is the one the window showed before (or when it opened):
        the application raising an event while it fills the window in, not the user typing."""
        if a.get("password") or a.get("via") != "uia-value-event":
            return False
        hwnd = self._window_of(a)
        for snap in (self.trees.get(hwnd), after.get(hwnd)):
            if snap is None:
                continue
            path = self.win.find_rid(snap[0], tuple(a["desc"].get("rid") or ()))
            if path is None:
                continue
            node = snap[0]
            for i in path:
                node = node["children"][i]
            return privacy.shape(str(node.get("value") or "")) == a.get(
                "shape"
            ) and snap is self.trees.get(hwnd)
        return False

    def _window_of(self, a: dict) -> int:
        if a["kind"] == "menu_open":
            return a["hwnd"]
        hwnd = a.get("hwnd") or (a.get("desc") or {}).get("window") or 0
        if hwnd in self.popup_owner:
            hwnd = self.popup_owner[hwnd]
        elif hwnd and hwnd not in self.windows and self.win.class_name(hwnd) in ("ComboLBox",):
            hwnd = self.fg
        elif hwnd and hwnd not in self.windows and self.frame_pid:
            # inside a modern application: its frame window is the application's window
            root = self.win.root_window(hwnd)
            if root in self.frame_pid:
                hwnd = root
            else:
                hwnd = next((f for f, p in self.frame_pid.items()
                             if p == self.win.window_pid(hwnd) and f in self.windows), hwnd)  # fmt: skip
        return hwnd

    def _snapshot(self, hwnd: int) -> tuple[dict, dict] | None:
        win = self.win
        if not hwnd or (self.frame_pid.get(hwnd) or win.window_pid(hwnd)) not in self.pids:
            return None
        if getattr(self, "exclude_window", None) is not None and self._excluded(hwnd):
            return None  # excluded on this PC (its title): no tree, no picture
        raw = win.capture(hwnd)
        if raw is None or not win.user32.IsWindowVisible(hwnd):
            # Gone while it was read: a window that is closing gives a tree without its
            # content (WPF takes the content away before the window itself goes).
            return None
        if getattr(self, "pictures", False):  # the pixels of this moment, for its picture
            from twinlab2_recorder.desktop import winlook

            try:
                masks = self._exclusion(hwnd)  # where excluded windows are, at this moment
                raw["_pixels"] = winlook.grab(hwnd) if masks is not None else None
                raw["_exclusion"] = masks
                winlook.mark_message_icon(raw)  # which standard icon a message box shows
            except (OSError, ValueError):
                pass
        clean = privacy.clean(raw, self.known, self.structure)
        return raw, clean

    def _exclusion(self, hwnd: int) -> list | None:
        """exclusion_masks(hwnd): [] without an exclude list; None when the picture is skipped."""
        if getattr(self, "exclusion_masks", None) is None:
            return []
        try:
            return self.exclusion_masks(hwnd)
        except Exception:  # noqa: BLE001 - not known where excluded windows are: no picture
            return None

    def _locate(self, desc: dict, snaps: list[tuple[dict, dict] | None]) -> dict:
        """The target's locator in the first snapshot that holds it (before, then after)."""
        for snap in snaps:
            if snap is None:
                continue
            raw, clean = snap
            path = self.win.find_rid(raw, tuple(desc.get("rid") or ()))
            if path is None:
                continue
            node, ancestors = {**clean, "idx": 0}, []
            for i in path:
                counts = {}
                kids = node.get("children") or []
                for k in kids[:i]:
                    counts[k.get("type")] = counts.get(k.get("type"), 0) + 1
                parent = dict(node)
                parent.setdefault("idx", 0)
                ancestors.append(parent)
                node = dict(kids[i])
                node["idx"] = counts.get(node.get("type"), 0)
            loc = locator.build(clean, node, tuple(ancestors), node["idx"])
            loc["type"] = node.get("type")
            if node.get("name_is") == "structure":
                loc["name"] = node.get("name")
            return loc
        # Not in any snapshot (it came and went between them): from its own properties.
        node = privacy.clean({k: v for k, v in desc.items() if k in ("type", "aid", "name", "cls", "fw")},
                             self.known)  # fmt: skip
        loc = {"selector": locator.step_of(node), "matches": None, "path": [], "candidates": [],
               "props": locator.props_of(node), "index": None, "type": node.get("type"),
               "note": "not found in a snapshot of its window; uniqueness not checked"}  # fmt: skip
        return loc

    def _window_info(self, hwnd: int, snap: tuple[dict, dict] | None) -> dict:
        if snap is not None:
            info = locator.window_locator(snap[1], self._pname(hwnd))
            info["title"] = snap[1].get("name")
            self.titles[hwnd] = info
        return dict(self.titles.get(hwnd) or {"selector": None, "title": None, "props": {}})

    def _pname(self, hwnd: int) -> str | None:
        pid = (self.frame_pid.get(hwnd) or self.win.window_pid(hwnd)) if hwnd else 0
        return (self.processes.get(pid) or {}).get("process")

    def _write_step(self, a: dict, after: dict) -> None:
        win = self.win
        hwnd = self._window_of(a)
        before = self.trees.get(hwnd)
        snap = after.get(hwnd)
        kind = a["kind"]
        self.seq += 1
        seq = self.seq
        pid = win.window_pid(hwnd) if hwnd and win.user32.IsWindow(hwnd) else None
        if pid is None or pid not in self.pids:
            pid = self.frame_pid.get(hwnd) or (a.get("desc") or {}).get("pid") or pid
        window = self._window_info(hwnd, snap or before)
        if not window.get("selector") and kind not in ("window_open", "menu_open"):
            window = dict(self.last_window)  # gone with the action (a menu that closed the app)
        elif window.get("selector"):
            self.last_window = window
        step: dict = {"seq": seq, "ts": datetime.fromtimestamp(a["t"], UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"), "surface": SURFACE, "action": kind,
            "process": (self.processes.get(pid) or {}).get("process"), "pid": pid,
            "window": {k: window.get(k) for k in ("selector", "title", "props")},
            "target": None, "selector": None, "via": a.get("via", "poll"), "tree": None,
            "screenshot": None, "url": None, "dom": None}  # fmt: skip
        if a.get("event_id"):
            step.update(event_id=a["event_id"], channel=SURFACE, captured_at=a.get("captured_at"))
        if a.get("injected"):
            step["injected"] = True  # synthetic input (a script), not a person's hand
        desc = a.get("desc")
        if desc:
            loc = self._locate(desc, [before, snap])
            step["target"] = loc
            step["selector"] = loc["selector"]
            if kind == "click" and desc.get("type") in ("CheckBox", "RadioButton"):
                # A click on a check box is a toggle: its state after, from the snapshot.
                kind = step["action"] = "toggle"
                path = win.find_rid(snap[0], tuple(desc.get("rid") or ())) if snap else None
                node = snap[0] if path is not None else None
                for i in path or []:
                    node = node["children"][i]
                a["state"] = (node or {}).get("toggle")
        if kind == "type":
            if a.get("password"):
                step["password"] = True
            elif a.get("shape") is not None:
                step["typed_shape"] = a["shape"]
        elif kind in ("toggle",):
            step["state"] = a.get("state")
        elif kind == "select" and desc and a.get("item") is not None:
            # The item is already known (a tree diff saw the combo box show it).
            step["item"], step["item_is"] = a["item"], a.get("item_is", "data")
            step["container"] = step["selector"]
        elif kind == "select" and desc:
            types = desc.get("ancestor_types") or []
            popup = desc.get("window") in self.popup_owner or not types
            if "ComboBox" in types or (
                popup and self.last_combo and a["t"] - self.last_combo[1] < 30
            ):
                if "ComboBox" in types:
                    rid = desc["ancestors"][types.index("ComboBox")]
                    cdesc = {"rid": rid, **self._desc_of(hwnd, rid)}
                else:
                    cdesc = self.last_combo[0]
                step["container"] = self._locate(cdesc, [before, snap])["selector"]
                step["selector"] = step["container"]  # a bot picks the item in the combo box
                name = desc.get("name") or ""
                if privacy.code_list([name], self.known):
                    step["item"], step["item_is"] = privacy.keep_text(name), "code"
                else:
                    step["item"], step["item_is"] = privacy.shape(name), "data"
            else:
                step["item"], step["item_is"] = (step["target"] or {}).get("name"), "data"
                step["container"] = self._container(desc, [before, snap])
        elif kind in ("expand", "collapse") and desc and desc.get("type") == "ComboBox":
            found = self.combo_items.get(tuple(desc.get("rid") or ())) or self.pending_items
            if found is None:
                names = win.item_names(win.by_rid(hwnd, tuple(desc.get("rid") or ())))
                if names and privacy.code_list(names, self.known):
                    found = {"items": [privacy.keep_text(n) for n in names]}
                elif names:
                    found = {"items_are": "data", "item_count": len(names)}
            step.update(found or {})
        elif kind == "key":
            step["key"] = a.get("key")
            if a.get("focus_desc"):
                step["focus_after"] = self._locate(a["focus_desc"], [snap, before])["selector"]
            if a.get("invoked_desc"):
                step["invoked"] = self._locate(a["invoked_desc"], [before, snap])["selector"]
        elif kind == "menu" and desc:
            step["menu_path"] = self._menu_path(desc, [before, snap])
        if a.get("tree_late"):
            step["tree_late"] = True  # its tree and picture show a later state (see _finish)
        if a.get("effects"):
            # What the application did by itself because of this step: seen, not a user step.
            step["effects"] = [self._effect(e, after) for e in a["effects"]]
        tree_snap = snap if kind != "window_close" else None
        if a.get("own_snap"):
            tree_snap = a["snap"]
        if kind == "menu_open":
            tree_snap = a.get("snap") or self._snapshot(a["hwnd"])
            step["window"] = {k: self._window_info(a["owner"], self.trees.get(a["owner"])).get(k)
                              for k in ("selector", "title", "props")}  # fmt: skip
            if step["window"].get("selector"):
                self.last_window = dict(step["window"])  # the window the menu belongs to
        if tree_snap is not None and self._level(pid) == "steps":
            step["level"] = "steps"  # a workstation session's steps-only process: no tree
        elif tree_snap is not None:
            rel = f"trees/{seq:04d}.json"
            clean = json.loads(json.dumps(tree_snap[1]))
            (self.out / rel).write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
            step["tree"] = rel
            shot_hwnd = hwnd if kind != "menu_open" else a["hwnd"]
            if self.screenshots and kind != "window_close" and (
                tree_snap[0]["_exclusion"] if "_exclusion" in tree_snap[0] else self._exclusion(shot_hwnd)
            ) == []:  # a raw screenshot only when no excluded window is anywhere over it
                shot = f"screens/{seq:04d}.png"
                if win.screenshot(hwnd if kind != "menu_open" else a["hwnd"], self.out / shot):
                    step["screenshot"] = shot
            if getattr(self, "pictures", False) and kind != "window_close":
                pic = self._picture(hwnd if kind != "menu_open" else a["hwnd"], tree_snap, seq)
                if pic:
                    step["picture"] = pic
                elif tree_snap[0].get("_exclusion", []) is None:
                    step["picture_note"] = "not kept: an excluded window covered it"
        self._emit(step)

    def _emit(self, step: dict) -> None:
        """Write a finished step to steps.jsonl and to the session's journal."""
        self.file.write(json.dumps(step, ensure_ascii=False) + "\n")
        self.file.flush()
        if step.get("event_id"):
            self._marks().discard(step["event_id"])
            if self.journal is not None:
                self.journal.step(step, self.out.name)

    def _picture(self, hwnd: int, snap: tuple[dict, dict], seq: int) -> str | None:
        """A picture of the window without data (twinlab2_recorder.desktop.pictures), or None."""
        from twinlab2_recorder.desktop import pictures
        from twinlab2_recorder.desktop import winlook

        from twinlab2_recorder.workstation import exclude

        try:
            raw, clean = snap
            masks = raw["_exclusion"] if "_exclusion" in raw else self._exclusion(hwnd)
            if masks is None:
                return None  # mostly under excluded windows: no picture at all
            found = pictures.places(raw, clean) + exclude.mask_places(masks)
            fonts: dict[int, bytes | None] = {}
            for p in found:  # the fonts now, while the controls exist; the drawing comes later
                if p.get("excluded"):
                    continue  # an excluded window's place: filled, nothing drawn in it
                h = p.get("hwnd") or 0
                if h not in fonts:
                    fonts[h] = winlook.logfont_bytes(h)
                p["lf"] = fonts[h]
            pixels = raw.get("_pixels") or winlook.grab(hwnd)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if pixels is None:
            return None
        rel = f"{pictures.DIR}/{seq:04d}.png"
        # Drawn when the recording ends (`_render_pictures`): drawing here would delay the next
        # snapshot. The pixels stay in memory only.
        self.pending_pictures.append((rel, hwnd, found, pixels))
        self.secrets |= pictures.secrets_of(found)
        self.kept_texts |= pictures.kept_texts(clean)
        return rel

    def _render_pictures(self) -> None:
        from twinlab2_recorder.desktop import winlook

        pending, self.pending_pictures = getattr(self, "pending_pictures", []), []
        for rel, hwnd, found, pixels in pending:
            try:
                got = winlook.masked_picture(hwnd, found, None, pixels)
            except (OSError, ValueError, KeyError, TypeError):
                got = None
            if got is not None:
                (self.out / rel).parent.mkdir(exist_ok=True)
                (self.out / rel).write_bytes(got[0])

    def _effect(self, e: dict, after: dict) -> dict:
        """A selection the application made because of a step: where, never what (a row holds
        data). `{"action": "select", "selector", "container", "window", "ts", "via"}`."""
        hwnd = self._window_of(e)
        snaps = [after.get(hwnd), self.trees.get(hwnd)]
        window = self._window_info(hwnd, snaps[0] or snaps[1])
        return {"action": e["kind"], "selector": self._locate(e["desc"], snaps)["selector"],
                "container": self._container(e["desc"], snaps), "item_is": "data",
                "window": window.get("selector"), "via": e.get("via"),
                "ts": datetime.fromtimestamp(e["t"], UTC).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")}  # fmt: skip

    def _desc_of(self, hwnd: int, rid: tuple) -> dict:
        """Properties of a control by runtime id (for one found only as an ancestor)."""
        el = self.win.by_rid(hwnd, tuple(rid or ()))
        d = self.win.describe(el) if el is not None else None
        return {k: v for k, v in (d or {}).items() if k in ("type", "aid", "name", "cls", "fw")}

    def _container(self, desc: dict, snaps) -> str | None:
        """The list, combo box or grid an item was selected in."""
        for snap in snaps:
            if snap is None:
                continue
            raw = snap[0]
            path = self.win.find_rid(raw, tuple(desc.get("rid") or ()))
            if not path:
                continue
            nodes, n = [raw], raw
            for i in path:
                n = n["children"][i]
                nodes.append(n)
            for k in range(len(nodes) - 2, -1, -1):
                if nodes[k].get("type") in ("ComboBox", "List", "DataGrid", "Table", "Tree", "Tab"):
                    cdesc = {"rid": nodes[k].get("rid")}
                    return self._locate(cdesc, [snap])["selector"]
        return None

    def _menu_path(self, desc: dict, snaps) -> str:
        """`Case > Open case`: the menu item names from the menu bar down (structure)."""
        names = []
        for snap in [*snaps, *list(self.trees.values())]:
            if snap is None:
                continue
            raw, clean = snap
            path = self.win.find_rid(raw, tuple(desc.get("rid") or ()))
            if path is None:
                continue
            n = clean
            for i in path:
                n = n["children"][i]
                if n.get("type") == "MenuItem" and n.get("name"):
                    names.append(n["name"])
            break
        if not names:
            names = [privacy.keep_text(desc.get("name") or "", self.known)]
        return " > ".join(names)

    def _write_session(self) -> None:
        self._render_pictures()
        self._check_pictures()
        procs = []
        for p in self.processes.values():
            sig = dict(p["signals"])
            trees = [json.loads(f.read_text(encoding="utf-8"))
                     for f in sorted((self.out / "trees").glob("*.json"))[:200]]  # fmt: skip
            sig.update(detect.signals_from_trees(trees))
            tech = detect.detect(sig)
            sig["framework_ids"] = sorted(sig["framework_ids"])
            sig["classes"] = sorted(sig["classes"])
            row = {"process": p["process"], "pid": p["pid"], "technology": tech, "signals": sig}
            if self.session:
                row["level"] = self._level(p["pid"])
            procs.append(row)
        if self.effects_unplaced:
            self.notes.append(f"{self.effects_unplaced} selection(s) the application made by "
                              "itself, after the step before them was written: not recorded")  # fmt: skip
        again = {k: n for k, n in self.__dict__.get("loop_failures", {}).items() if n > 1}
        if again:
            self.notes.append("calls that failed more than once: " +
                              "; ".join(f"{k} ({n} times)" for k, n in sorted(again.items())))  # fmt: skip
        doc = {
            "format": FORMAT,
            "surface": SURFACE,
            "recorder": "twinlab2_recorder.desktop.recorder",
            "started": self.started,
            "ended": _now(),
            "steps": self.seq,
            "processes": procs,
            "capture": {
                "uia_events": ["Invoke_Invoked", "SelectionItem_ElementSelected",
                               "ValueProperty changed", "ToggleState changed",
                               "ExpandCollapseState changed", "FocusChanged"],
                "hooks": ["WH_MOUSE_LL", "WH_KEYBOARD_LL"] if self.use_hooks else [],
                "polled": ["top-level windows of the recorded processes (open, close, menus)",
                           "foreground window", "edit value when focus leaves it"],
                "screenshots": self.screenshots,
                "pictures": "without data, checked (pictures.json)" if self.pictures else False,
            },
            "system": self._system(),
            "privacy": "names and values that may be data are kept as their shape only "
            "(twinlab2_recorder.desktop.privacy); typed values are never stored",
            "notes": self.notes,
        }  # fmt: skip
        (self.out / "desktop.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                              encoding="utf-8")  # fmt: skip


# --- entry points ----------------------------------------------------------------------------------


def _wait_window(pids: set[int], timeout: float) -> bool:
    from twinlab2_recorder.desktop import win

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if win.top_windows(pids):
            return True
        time.sleep(0.2)
    return False


def attach(out_dir: Path, *, pid: int | None = None, process: str | None = None,
           title: str | None = None, script: Callable[[set[int]], None] | None = None,
           recorder_cls=None, **kw) -> Path:  # fmt: skip
    """Record an application that is already running, found by pid, executable name or a
    regular expression on a window title. It is left running when the recording stops."""
    require()
    from twinlab2_recorder.desktop import win

    pids: set[int] = set()
    if pid:
        pids.add(int(pid))
    if process:
        pids |= set(win.pids_by_name(process))
    if title:
        pids |= set(win.pids_by_title(title))
    if not pids:
        raise RuntimeError("no running process matches: give --pid, --process or --title")
    return _run(Path(out_dir), pids, script, recorder_cls, **kw)


def record(exe: str, out_dir: Path, *, args: list[str] | None = None,
           script: Callable[[set[int]], None] | None = None, close: bool = True,
           wait: float = 20.0, recorder_cls=None, **kw) -> Path:  # fmt: skip
    """Start `exe` and record it until it exits (or Ctrl+C). With `script(pids)`, the script
    drives the application while it is recorded (scripted evidence runs and tests); the
    recording stops when the script returns. The application is closed at the end when
    `close` (the default), because the recorder started it."""
    require()
    proc = subprocess.Popen([exe, *(args or [])])
    try:
        if not _wait_window({proc.pid}, wait):
            raise RuntimeError(f"{Path(exe).name} showed no window within {wait:.0f} s")
        return _run(Path(out_dir), {proc.pid}, script, recorder_cls, **kw)
    finally:
        if close and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _run(out: Path, pids: set[int], script, recorder_cls=None, **kw) -> Path:
    recorder_cls = recorder_cls or Recorder
    if script is None:
        return recorder_cls(out, pids, **kw).run()
    done = threading.Event()
    failure: list[BaseException] = []

    def drive() -> None:
        try:
            time.sleep(0.8)  # the recorder has its handlers in place
            script(pids)
            time.sleep(1.2)  # the last step settles
        except BaseException as exc:  # noqa: BLE001 - reported after the recording is saved
            failure.append(exc)
        finally:
            done.set()

    def driver() -> None:
        import comtypes

        comtypes.CoInitializeEx(0)  # COINIT_MULTITHREADED
        try:
            drive()
        finally:
            comtypes.CoUninitialize()

    t = threading.Thread(target=driver, daemon=True, name="twinlab-desktop-script")
    t.start()
    path = recorder_cls(out, pids, stop=done.is_set, **kw).run()
    t.join(5)
    if failure:
        raise failure[0]
    return path
