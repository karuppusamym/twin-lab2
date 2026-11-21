# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91. Licensed
# under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports renamed. A test
# helper only (the CaseDesk end to end drives CaseDesk with it); docs/UPSTREAM.md.
"""Drive a desktop application through UI Automation patterns, for scripted evidence runs and
for verify. Windows only.

Patterns (Invoke, Value, Toggle, SelectionItem, ExpandCollapse) act on controls without moving
the mouse or typing on the keyboard, so nothing reaches another application. `key()` and
`keys()` are the exceptions: they send real key presses (SendInput), and only when a window of
the driven process is in the foreground; otherwise they refuse.
"""

from __future__ import annotations

import time

from twinlab2_recorder.desktop import locator, require


class DriveError(RuntimeError):
    pass


class Ambiguous(DriveError):
    def __init__(self, selector: str, count: int):
        super().__init__(f"{selector} matches {count} controls; not choosing one")
        self.selector, self.count = selector, count


class Driver:
    # How long a check that depends on other windows waits before refusing: the foreground (a
    # background driver must first win it from the foreground lock), the window under a point (a
    # toast, a window still closing) and a combo box's items (a list slow to fill).
    FOREGROUND_S = 3.0
    UNCOVERED_S = 3.0
    ITEMS_S = 3.0

    def __init__(self, pids: set[int], pause: float = 0.6):
        require()
        from twinlab2_recorder.desktop import win

        self.win, self.pids, self.pause_s = win, set(pids), pause

    def pause(self, seconds: float | None = None) -> None:
        time.sleep(self.pause_s if seconds is None else seconds)

    def _owns_foreground(self) -> bool:
        fg = self.win.foreground()
        return bool(fg) and self.win.window_pid(fg) in self.pids

    def _to_front(self, top: int) -> bool:
        """Put the driven window `top` in the foreground, waiting up to FOREGROUND_S. Windows
        refuses SetForegroundWindow to a process that did not send the last input event, so each
        try first sends a zero-pixel mouse move (win.nudge_input), which moves and clicks nothing.
        True when a window of the driven process owns the foreground."""
        end = time.monotonic() + self.FOREGROUND_S
        while True:
            if self._owns_foreground():
                return True
            if not top or time.monotonic() > end:
                return False
            self.win.nudge_input()
            self.win.user32.SetForegroundWindow(top)
            time.sleep(0.2)

    def _uncovered(self, x: int, y: int) -> bool:
        """True when the window under (x, y) is the driven process's, waiting up to UNCOVERED_S
        for something else on top there to go. It never clicks to make it go."""
        end = time.monotonic() + self.UNCOVERED_S
        while True:
            if self.win.window_pid(self.win.window_at(x, y)) in self.pids:
                return True
            if time.monotonic() > end:
                return False
            time.sleep(0.2)

    def windows(self) -> list:
        out = []
        for hwnd in self.win.top_windows(self.pids):
            try:
                out.append((hwnd, self.win.element_from_handle(hwnd)))
            except Exception:  # noqa: BLE001 - closing
                pass
        return out

    def window(self, selector: str, timeout: float = 10.0):
        """The top-level window of the driven process that the locator matches (waits)."""
        want = locator.parse(selector)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = []
            for _hwnd, el in self.windows():
                node = {"type": self.win.CONTROL_TYPES.get(el.CurrentControlType),
                        "aid": el.CurrentAutomationId, "name": el.CurrentName,
                        "name_is": "title", "cls": el.CurrentClassName,
                        "fw": el.CurrentFrameworkId}  # fmt: skip
                if locator.matches(node, want):
                    found.append(el)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise Ambiguous(selector, len(found))
            time.sleep(0.2)
        raise DriveError(f"no window {selector} within {timeout:.0f} s")

    def control(self, window, selector: str, timeout: float = 5.0):
        end = time.monotonic() + timeout
        while True:
            found = self.win.find_all(window, selector)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise Ambiguous(selector, len(found))
            if time.monotonic() > end:
                raise DriveError(f"no control {selector}")
            time.sleep(0.2)

    def type(self, window, selector: str, text: str) -> None:
        el = self.control(window, selector)
        try:
            el.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        self.win.set_value(el, text)
        self.pause()

    def keys(self, window, selector: str, text: str) -> None:
        """Type `text` into a control with real key presses (SendInput, one key per character),
        as a person would: for a field that raises no event when its Value pattern sets it
        (WPF's PasswordBox), so that a recorder sees typing at all. The field is emptied through
        its Value pattern first. Each key is sent only while the driven process owns the
        foreground window and the control has the keyboard focus; else DriveError."""
        el = self.control(window, selector)
        top = self.win.root_window(window.CurrentNativeWindowHandle or 0) or 0
        if top and self.win.foreground() != top:
            self._to_front(top)
        el.SetFocus()
        self.win.set_value(el, "")
        time.sleep(0.2)
        for ch in text:
            fg = self.win.foreground()
            if not fg or self.win.window_pid(fg) not in self.pids or not el.CurrentHasKeyboardFocus:
                raise DriveError(f"{selector} does not have the keyboard focus; not typing")
            _send_char(ch)
            time.sleep(0.03)
        self.pause()

    def press(self, window, selector: str) -> str:
        """Press a button without input devices: BM_CLICK posted to its own window when it has
        one (returns at once, even when the button opens a modal window), else its pattern."""
        el = self.control(window, selector)
        how = self.win.post_click(el) or self.win.invoke(el)
        self.pause()
        return how

    def click(self, window, selector: str) -> str:
        """A real mouse click (SendInput) in the middle of the control, as a person's hand would
        make it, so the recorder's mouse hook sees it. Only when the driven process owns the
        foreground window and the window under that point; the cursor is put back afterwards.
        Each of those is waited for a little (FOREGROUND_S, UNCOVERED_S), then DriveError rather
        than click anywhere else."""
        el = self.control(window, selector)
        rect = el.CurrentBoundingRectangle
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        top = self.win.root_window(window.CurrentNativeWindowHandle or 0) or 0
        if top and self.win.foreground() != top:
            self._to_front(top)
        if not self._owns_foreground():
            raise DriveError("the driven application is not in the foreground; not clicking")
        if not self._uncovered(x, y):
            raise DriveError(f"{selector}: another window covers it; not clicking")
        self.win.mouse_click(x, y)
        self.pause()
        return "mouse"

    def toggle(self, window, selector: str) -> None:
        self.win.toggle(self.control(window, selector))
        self.pause()

    def choose(self, window, selector: str, item: str) -> None:
        """Pick `item` in a combo box: expand, select the item by name, collapse. A drop-down
        list that is still filling is waited for (ITEMS_S) while the item is not there yet."""
        combo = self.control(window, selector)
        try:
            combo.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        self.win.expand(combo, True)
        self.pause(0.4)
        sel = locator.format({"Name": item, "ControlType": "ListItem"})
        end = time.monotonic() + self.ITEMS_S
        found = self.win.find_all(combo, sel)
        while not found and time.monotonic() < end:
            time.sleep(0.2)
            found = self.win.find_all(combo, sel)
        if len(found) != 1:
            raise DriveError(f"{selector}: item {item!r} found {len(found)} time(s)")
        self.win.select(found[0])
        self.pause(0.3)
        try:
            self.win.expand(combo, False)
        except Exception:  # noqa: BLE001 - a drop-down list closes by itself
            pass
        self.pause()

    def menu(self, window, *names: str) -> None:
        """Open a menu path by item names (`menu(w, "Case", "Refresh")`)."""
        scope = window
        for n, name in enumerate(names):
            sel = locator.format({"Name": name, "ControlType": "MenuItem"})
            el = None
            end = time.monotonic() + 5
            while el is None and time.monotonic() < end:
                for _h, w in [(0, scope), *self.windows()]:
                    found = self.win.find_all(w, sel)
                    if found:
                        el = found[0] if len(found) == 1 else None
                        if el is None:
                            raise Ambiguous(sel, len(found))
                        break
                if el is None:
                    time.sleep(0.2)
            if el is None:
                raise DriveError(f"no menu item {name!r}")
            if n < len(names) - 1:
                try:
                    self.win.expand(el, True)
                except Exception:  # noqa: BLE001
                    self.win.invoke(el)
            else:
                self.win.invoke(el)
            self.pause(0.5)

    def click_menu(self, window, *names: str) -> None:
        """Open a menu path with real mouse clicks (`click_menu(w, "Case", "Refresh")`): the
        first item in the window's menu bar, the rest in the menu windows that open. Like
        click(): only in the foreground and where no other window covers the item, each waited
        for a little, else DriveError."""
        top = self.win.root_window(window.CurrentNativeWindowHandle or 0) or 0
        if top and self.win.foreground() != top:
            self._to_front(top)
        if not self._owns_foreground():
            raise DriveError("the driven application is not in the foreground; not clicking")
        for n, name in enumerate(names):
            sel = locator.format({"Name": name, "ControlType": "MenuItem"})
            end = time.monotonic() + 5
            while True:
                scopes = [window] if n == 0 else [w for _h, w in self.windows()]
                hit = [(w, len(self.win.find_all(w, sel))) for w in scopes]
                hit = [(w, c) for w, c in hit if c]
                if hit:
                    break
                if time.monotonic() > end:
                    raise DriveError(f"no menu item {name!r}")
                time.sleep(0.2)
            scope = hit[0][0]
            el = self.control(scope, sel)
            rect = el.CurrentBoundingRectangle
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            if not self._uncovered(x, y):
                raise DriveError(f"menu item {name!r} is covered by another window; not clicking")
            self.win.mouse_click(x, y)
            self.pause(0.6)

    def key(self, chord: str) -> bool:
        """Send a key chord (`Enter`, `Ctrl+O`) with SendInput, only when the driven process
        owns the foreground window. Returns False (and sends nothing) otherwise."""
        fg = self.win.foreground()
        if not fg or self.win.window_pid(fg) not in self.pids:
            return False
        _send_chord(chord)
        self.pause()
        return True


VK = {"Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B, "Ctrl": 0x11, "Alt": 0x12, "Shift": 0x10,
      "Delete": 0x2E, **{f"F{i}": 0x6F + i for i in range(1, 13)}}  # fmt: skip


def _send_chord(chord: str) -> None:
    keys = [VK.get(k, ord(k.upper()) if len(k) == 1 else 0) for k in chord.split("+")]
    _send_input([(k, 0, 0) for k in keys] + [(k, 0, 2) for k in reversed(keys)])  # 2: key up


def _send_char(ch: str) -> None:
    """One character as a key press that carries the character itself (KEYEVENTF_UNICODE)."""
    _send_input([(0, ord(ch), 4), (0, ord(ch), 4 | 2)])


def _send_input(seq: list[tuple[int, int, int]]) -> None:
    """Key events (virtual key, scan code or character, flags) through SendInput."""
    import ctypes
    from ctypes import wintypes

    class KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KI), ("pad", ctypes.c_byte * 32)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    arr = (INPUT * len(seq))()
    for i, (vk, scan, flags) in enumerate(seq):
        arr[i].type = 1
        arr[i].ki = KI(vk, scan, flags, 0, 0)
    ctypes.windll.user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
