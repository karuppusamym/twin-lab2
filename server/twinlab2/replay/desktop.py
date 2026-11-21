# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91
# (lab/labctl/labctl/desktop/verify.py; DriveError and Ambiguous from desktop/drive.py,
# Recording from desktop/model.py). Licensed under the Apache License, Version 2.0 (LICENSE).
# Changed for TwinLab2: the UI Automation modules come from twinlab2_recorder.desktop (the
# recorder's copies, imported only when a desktop replay runs), acceptance cases on the live
# windows and the `replay` entry point added. Source and changes: docs/UPSTREAM.md.
"""Verify for a Windows desktop application: replay a recording against the built executable.

    python -m twinlab2.replay <recording>/desktop --surface desktop --target <exe> --json r.json

`compare_trees(recorded, live)` compares two value-free window trees (plain Python, runs
anywhere): each control the recording shows is looked up in the live tree by its locator and
reported **missing** (no match), **ambiguous** (more than one match: never resolved by taking
the first), **changed** (ClassName, FrameworkId or parent path differ), **enabled** or
**visibility** (state differs), **options** (a drop-down's code list differs) or
**value_format** (a value's shape differs); a control in the live tree that the recording does
not show is **extra**. Rows of grids and lists hold data and are compared by presence only.

`verify(exe, recording)` (Windows) starts the built app, walks the recorded steps by their
locators through UI Automation patterns and window messages (no mouse, no keyboard: nothing
reaches another application), compares each step's window with the recorded tree, closes the
app, and returns the report in the web fidelity format (twinlab2.replay.fidelity). It OPENS THE
APP'S WINDOWS: Verify runs it only on a Windows verifier that allows it
(`TWINLAB2_DESKTOP_VERIFY=1`), never on the portal's own worker by default.

Business checks are the acceptance cases beside the recording (`acceptance.yaml`), judged on the
live windows once the step `after` names ran (or at the `end`):

```yaml
version: 1
cases:
  - {id: signed-in, after: 4, check: window, selector: "uia:AutomationId=MainForm;ControlType=Window"}
  - {id: case-saved, after: end, check: control, selector: "uia:Name=Saved;ControlType=Text"}
  - {id: save-disabled, after: 9, check: control, selector: "uia:AutomationId=btnSave;ControlType=Button", expect: disabled}
```

`expect` is present (the default), absent, enabled or disabled; a selector that matches more
than one control is BLOCKED, never resolved by taking the first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from twinlab2.replay.fidelity import Difference, FidelityReport, StepReport

try:  # the recorder's UI Automation and tree modules (pure Python except win/winlook)
    from twinlab2_recorder.desktop import locator, privacy
except ImportError:  # pragma: no cover - the portal image has no recorder package
    locator = privacy = None

FILE_ACCEPTANCE, FILE_BINDINGS = "acceptance.yaml", "bindings.yaml"


class DesktopUnavailable(RuntimeError):
    """A desktop replay cannot run here (not Windows, no recorder package, not allowed)."""


class DriveError(RuntimeError):
    pass


class Ambiguous(DriveError):
    def __init__(self, selector: str, count: int):
        super().__init__(f"{selector} matches {count} controls; not choosing one")
        self.selector, self.count = selector, count


def require_modules() -> None:
    if locator is None:
        raise DesktopUnavailable("a desktop replay needs the recorder package (pip install -e ./recorder): "
                                 "its UI Automation and window-tree modules")


def require_live() -> None:
    """Refuse a live replay where it must not open windows."""
    require_modules()
    if os.environ.get("TWINLAB2_DESKTOP_VERIFY") != "1":
        raise DesktopUnavailable("desktop replays open the application's windows: allowed only on a Windows "
                                 "verifier with TWINLAB2_DESKTOP_VERIFY=1")
    if sys.platform != "win32":
        raise DesktopUnavailable("a desktop replay runs on Windows (a Windows verifier)")


class Recording:
    """A desktop recording folder, read."""

    def __init__(self, path: Path):
        self.path = Path(path)
        meta = self.path / "desktop.json"
        self.session = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
        self.steps = [json.loads(line) for line in
                      (self.path / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip()]  # fmt: skip
        self._trees: dict[str, dict] = {}

    @property
    def id(self) -> str:
        return self.path.parent.name if self.path.name == "desktop" else self.path.name

    def tree(self, rel: str | None) -> dict | None:
        if not rel:
            return None
        if rel not in self._trees:
            try:
                self._trees[rel] = unframe(json.loads((self.path / rel).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return None
        return self._trees[rel]


# A modern (UWP / WinUI) application's window is drawn by Windows' frame host: the top-level
# ApplicationFrameWindow holds the title bar (ApplicationFrameTitleBarWindow), an input sink and
# the application's own CoreWindow. A rebuild in another toolkit cannot have those Windows
# classes; what the application itself shows is the CoreWindow's content and the title bar.
FRAME_WINDOW = "ApplicationFrameWindow"
FRAME_TITLE_BAR = "ApplicationFrameTitleBarWindow"
FRAME_INPUT_SINK = "ApplicationFrameInputSinkWindow"
CORE_WINDOW = "Windows.UI.Core.CoreWindow"


def unframe(tree: dict) -> dict:
    """A recorded window tree as the application draws it: when the window is a frame host's,
    the CoreWindow's children are the window's own, the input sink is left out, and the frame
    host's classes are not compared (the title bar keeps its controls). Any other tree as it is."""
    if not isinstance(tree, dict) or locator.normalise_class(tree.get("cls")) != FRAME_WINDOW:
        return tree
    kids: list[dict] = []
    for child in tree.get("children") or []:
        cls = child.get("cls")
        if cls == FRAME_INPUT_SINK:
            continue
        if cls == CORE_WINDOW:
            kids += child.get("children") or []
        elif cls == FRAME_TITLE_BAR:
            kids.append({k: v for k, v in child.items() if k not in ("cls", "fw")})
        else:
            kids.append(child)
    out = {k: v for k, v in tree.items() if k not in ("cls", "fw")}
    out["children"] = kids
    out["framed"] = True
    return out


def window_wanted(selector: str) -> dict:
    """The parts of a recorded window locator a rebuilt window can have: a frame host's class
    (ApplicationFrameWindow) is left out, so the window is found by its name and type."""
    want = locator.parse(selector)
    if want.get("ClassName") == FRAME_WINDOW:
        want.pop("ClassName")
    return want


COMPARED = {"Button", "Edit", "Document", "CheckBox", "RadioButton", "ComboBox", "List",
            "DataGrid", "Table", "Tab", "TabItem", "MenuBar", "MenuItem", "Hyperlink", "Tree",
            "Spinner", "Slider", "Group", "StatusBar", "ToolBar", "SplitButton", "Calendar",
            "Text"}  # fmt: skip
ROW_TYPES = {"DataItem", "ListItem", "TreeItem"}
SKIP_UNDER = {"TitleBar"}


def _controls(tree: dict) -> list[tuple[dict, tuple, int]]:
    """Controls to compare: structural ones, never inside a data row, never the title bar."""
    out = []

    def walk(node: dict, ancestors: tuple, idx: int, data: bool) -> None:
        t = node.get("type")
        if t in SKIP_UNDER:
            return
        if ancestors and not data and t in COMPARED:
            if t != "Text" or node.get("name_is") in ("structure", "message"):
                out.append((node, ancestors, idx))
        inside = data or t in ROW_TYPES or t in ("DataGrid", "Table", "List", "Tree")
        counts: dict[str, int] = {}
        for child in node.get("children") or []:
            ct = child.get("type")
            n = counts.get(ct, 0)
            counts[ct] = n + 1
            c = {**child, "idx": n}
            walk(c, (*ancestors, {**node, "idx": node.get("idx", idx)}), n, inside)

    walk(dict(tree), (), 0, False)
    return out


def _named(path: list[str]) -> list[str]:
    """The steps of an ancestor path that name a control (AutomationId or Name). Unnamed
    containers in between (Java's root pane, layered pane and panels; WPF's borders) are left
    out: toolkits add and reorder them, bots do not anchor on them."""
    return [p for p in path if "AutomationId=" in p or "Name=" in p]


def _submenu_closed(other: dict, tree: dict, ancestors: tuple) -> bool:
    """Whether a menu item's parent menu item shows no items in the `other` tree. WPF lists the
    items of a drop-down menu under their parent only while the menu is open; whether a snapshot
    caught it open is timing. An item is then neither missing nor extra: nothing was seen."""
    if not ancestors or ancestors[-1].get("type") != "MenuItem":
        return False
    parent = ancestors[-1]
    loc = locator.build(tree, parent, ancestors[:-1], parent.get("idx", 0))
    hits = locator.find(other, loc["selector"],
                        loc["path"] if "Index=" in loc["selector"] else None)  # fmt: skip
    return len(hits) == 1 and not any(
        c.get("type") == "MenuItem" for c in hits[0][0].get("children") or []
    )


def _rows(node: dict) -> int:
    n = 0
    for c in node.get("children") or []:
        if c.get("type") in ROW_TYPES or (c.get("type") == "Custom" and c.get("children")
                                          and all(k.get("type") == "DataItem" for k in c["children"])):  # fmt: skip
            n += 1
    return n


def _client_origin(tree: dict) -> tuple[int, int]:
    c = (tree.get("frame") or {}).get("client")
    return (c[0], c[1]) if c and len(c) == 4 else (0, 0)


def compare_geometry(recorded: dict, live: dict, step: int = 0, tol: int = 2) -> list[Difference]:
    """The client area's size and each control's bounds in it (recorded vs live), within `tol`
    pixels, and a control's font where both trees say it (labctl.desktop.winlook)."""
    diffs: list[Difference] = []
    rc, lc = (recorded.get("frame") or {}).get("client"), (live.get("frame") or {}).get("client")
    if rc and lc and (abs(rc[2] - lc[2]) > tol or abs(rc[3] - lc[3]) > tol):
        diffs.append(Difference(step=step, element="(client area)", check="geometry",
                                expected=f"{rc[2]}x{rc[3]}", actual=f"{lc[2]}x{lc[3]}"))  # fmt: skip
    ro, lo = _client_origin(recorded), _client_origin(live)
    for node, ancestors, idx in _controls(recorded):
        if node.get("type") == "MenuItem" or not node.get("rect") or node.get("visible") is False:
            continue
        loc = locator.build(recorded, node, ancestors, idx)
        if loc.get("ambiguous"):
            continue
        scoped = loc["path"] if "Index=" in loc["selector"] else None
        hits = locator.find(live, loc["selector"], scoped)
        if len(hits) != 1 or not hits[0][0].get("rect"):
            continue
        a, b = node["rect"], hits[0][0]["rect"]
        ra = [a[0] - ro[0], a[1] - ro[1], a[2], a[3]]
        rb = [b[0] - lo[0], b[1] - lo[1], b[2], b[3]]
        if any(abs(x - y) > tol for x, y in zip(ra, rb, strict=True)):
            diffs.append(Difference(step=step, element=loc["selector"], check="geometry",
                                    expected=ra, actual=rb))  # fmt: skip
        fa = (node.get("win32") or {}).get("font")
        fb = (hits[0][0].get("win32") or {}).get("font")
        if fa and fb and (fa.get("face"), fa.get("px"), fa.get("weight")) != (
                fb.get("face"), fb.get("px"), fb.get("weight")):  # fmt: skip
            diffs.append(Difference(step=step, element=loc["selector"], check="font",
                                    expected=f"{fa.get('face')} {fa.get('px')}px {fa.get('weight')}",
                                    actual=f"{fb.get('face')} {fb.get('px')}px {fb.get('weight')}"))  # fmt: skip
    return diffs


def compare_rows(recorded: dict, live: dict, step: int = 0) -> list[Difference]:
    """The number of rows each grid shows (an outcome: a search found three cases)."""
    diffs: list[Difference] = []
    for node, ancestors, idx in _controls(recorded):
        if node.get("type") not in ("DataGrid", "Table", "List", "Tree"):
            continue
        loc = locator.build(recorded, node, ancestors, idx)
        scoped = loc["path"] if "Index=" in loc["selector"] else None
        hits = locator.find(live, loc["selector"], scoped)
        if len(hits) == 1 and _rows(node) != _rows(hits[0][0]):
            diffs.append(Difference(step=step, element=loc["selector"] + " (rows)", check="rows",
                                    expected=_rows(node), actual=_rows(hits[0][0])))  # fmt: skip
    return diffs


def picture_difference(a_png: bytes, b_png: bytes, tolerance: int = 16) -> float:
    """Percent of pixels whose colour differs by more than `tolerance` in a channel; 100 when
    the sizes differ."""
    from twinlab2_recorder.desktop import pictures

    wa, ha, a = pictures.read_png(a_png)
    wb, hb, b = pictures.read_png(b_png)
    if (wa, ha) != (wb, hb):
        return 100.0
    n = wa * ha
    diff = 0
    for i in range(0, n * 3, 3):
        if (abs(a[i] - b[i]) > tolerance or abs(a[i + 1] - b[i + 1]) > tolerance
                or abs(a[i + 2] - b[i + 2]) > tolerance):  # fmt: skip
            diff += 1
    return 100.0 * diff / n if n else 0.0


def compare_trees(recorded: dict, live: dict, step: int = 0,
                  supplied: frozenset[str] | set[str] = frozenset()) -> list[Difference]:  # fmt: skip
    """Differences between a recorded window tree and the live one, each naming the locator.
    `supplied` holds the locators whose values the replay typed (their shape is not compared)."""
    diffs: list[Difference] = []
    expected = []
    # A window a modal dialog has disabled reports its controls disabled. When the recorded and
    # the live window differ in that, it is timing (when each snapshot was taken, before or after
    # the message box opened or closed), not the app: enabled states are then not compared; the
    # message box itself is compared as its own window.
    modal = bool(recorded.get("enabled")) != bool(live.get("enabled"))
    if recorded.get("framed"):  # the window's own class is the frame host's: on neither side
        live = {k: v for k, v in live.items() if k not in ("cls", "fw")}
    for node, ancestors, idx in _controls(recorded):
        loc = locator.build(recorded, node, ancestors, idx)
        expected.append((loc, node, ancestors))
    matched_ids: set[int] = set()
    for loc, node, ancestors in expected:
        sel = loc["selector"]
        scoped = "Index=" in sel
        hits = locator.find(live, sel, loc["path"] if scoped else None)
        if loc.get("ambiguous"):
            # Ambiguous in the real application already (the brief lists it): a difference only
            # when the demo app has another number of matches.
            if len(hits) != loc["matches"]:
                diffs.append(Difference(step=step, element=sel, check="ambiguous",
                                        expected=loc["matches"], actual=len(hits)))  # fmt: skip
            continue
        if not hits:
            if node.get("type") == "MenuItem" and _submenu_closed(live, recorded, ancestors):
                continue
            diffs.append(Difference(step=step, element=sel, check="missing", expected=1, actual=0))
            continue
        if len(hits) > 1:
            diffs.append(Difference(step=step, element=sel, check="ambiguous", expected=1,
                                    actual=len(hits)))  # fmt: skip
            continue
        have, anc = hits[0]
        matched_ids.add(id(have))
        # A modern application's controls are XAML automation peers (FrameworkId XAML,
        # NamedContainerAutomationPeer...): toolkit classes a rebuild in WPF or WinForms cannot
        # have. For a frame host's window they are not compared (the report says so).
        for key, label in () if recorded.get("framed") else (("cls", "ClassName"), ("fw", "FrameworkId")):
            a = locator.normalise_class(node.get(key)) if key == "cls" else node.get(key)
            b = locator.normalise_class(have.get(key)) if key == "cls" else have.get(key)
            if a and a != b:
                diffs.append(Difference(step=step, element=sel, check="changed",
                                        expected=f"{label}={a}", actual=f"{label}={b}"))  # fmt: skip
        if node.get("type") == "MenuItem":
            continue  # menu items: present or not; where an open menu lists them, and whether it
            # is open, depends on timing (Java lists them under the menu and in its popup)
        live_path = [locator.step_of(a, a.get("idx", 0)) for a in anc]
        if _named(live_path) != _named(loc["path"]):
            diffs.append(Difference(step=step, element=sel, check="changed",
                                    expected=" > ".join(loc["path"]),
                                    actual=" > ".join(live_path)))  # fmt: skip
        if node.get("visible") is not None and bool(node.get("visible")) != bool(
            have.get("visible")
        ):
            diffs.append(Difference(step=step, element=sel, check="visibility",
                                    expected=node.get("visible"), actual=have.get("visible")))  # fmt: skip
        elif (not modal and node.get("enabled") is not None
              and bool(node["enabled"]) != bool(have.get("enabled"))):  # fmt: skip
            diffs.append(Difference(step=step, element=sel, check="enabled",
                                    expected=node.get("enabled"), actual=have.get("enabled")))  # fmt: skip
        if node.get("type") in ("DataGrid", "Table", "List", "Tree"):
            want, got = _rows(node) > 0, _rows(have) > 0
            if want != got:
                diffs.append(Difference(step=step, element=sel + " (rows)", check="missing" if want else "extra",
                                        expected="rows" if want else "none", actual="rows" if got else "none"))  # fmt: skip
        if node.get("items_are") == "code-list" and node.get("children") and have.get("children"):
            a = [c.get("name") for c, _p, _i in locator.walk(node) if c.get("type") == "ListItem"]
            b = [c.get("name") for c, _p, _i in locator.walk(have) if c.get("type") == "ListItem"]
            if a and a != b:
                diffs.append(
                    Difference(step=step, element=sel, check="options", expected=a, actual=b)
                )
        if (node.get("value") is not None and sel not in supplied and node.get("readonly")
                and have.get("value") is not None and node["value"] != have["value"]):  # fmt: skip
            diffs.append(Difference(step=step, element=sel, check="value_format",
                                    expected=node["value"], actual=have["value"]))  # fmt: skip
    for node, ancestors, idx in _controls(live):
        loc = locator.build(live, node, ancestors, idx)
        found = locator.find(
            recorded, loc["selector"], loc["path"] if "Index=" in loc["selector"] else None
        )
        if not found:
            if node.get("type") == "MenuItem" and _submenu_closed(recorded, live, ancestors):
                continue
            diffs.append(Difference(step=step, element=loc["selector"], check="extra",
                                    expected=0, actual=loc["matches"]))  # fmt: skip
    return diffs


def compare_files(recorded: Path, live: Path, step: int = 0) -> list[Difference]:
    return compare_trees(json.loads(Path(recorded).read_text(encoding="utf-8")),
                         json.loads(Path(live).read_text(encoding="utf-8")), step)  # fmt: skip


# --- replay (Windows) ------------------------------------------------------------------------------


def value_for(shape: str | None) -> str:
    """A synthetic value of a recorded shape: X -> A, x -> a, 9 -> 1, the rest kept."""
    return "".join("A" if c == "X" else "a" if c == "x" else "1" if c == "9" else c
                   for c in (shape or "").rstrip("…"))  # fmt: skip


def _async(fn, timeout: float = 2.0) -> str | None:
    """Run a UI Automation call that may block (a button that opens a modal window) on a thread
    of its own, and return after `timeout` whatever happened."""
    out: list = []

    def run():
        import comtypes

        comtypes.CoInitializeEx(0)
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001
            out.append(f"error: {type(exc).__name__}")
        finally:
            comtypes.CoUninitialize()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else "still running (a modal window opened)"


class _Live:
    def __init__(self, pids: set[int]):
        from twinlab2_recorder.desktop import win

        self.win, self.pids = win, pids
        self.known: set[str] = set()
        self.structure: set[str] = set()

    def windows(self) -> list[tuple[int, dict]]:
        """(hwnd, value-free tree) of each top-level window of the app."""
        out = []
        for hwnd in self.win.top_windows(self.pids):
            if self.win.class_name(hwnd) in self.win.IGNORED_CLASSES:
                continue
            raw = self.win.capture(hwnd)
            if raw is not None:
                self.win.strip_rids(raw)
                out.append((hwnd, privacy.clean(raw, self.known, self.structure)))
        return out

    def picture(self, hwnd: int) -> bytes | None:
        """The window as a picture without data, made as the recorder makes it."""
        from twinlab2_recorder.desktop import pictures, winlook

        raw = self.win.capture(hwnd)
        if raw is None:
            return None
        pixels = winlook.grab(hwnd)
        clean = privacy.clean(raw, self.known, self.structure)
        got = winlook.masked_picture(hwnd, pictures.places(raw, clean), winlook.font_handle,
                                     pixels)  # fmt: skip
        return got[0] if got else None

    def window(self, selector: str, timeout: float = 8.0) -> tuple[int, dict] | tuple[None, int]:
        """The window a recorded window locator names: (hwnd, tree), or (None, matches)."""
        want = window_wanted(selector)
        end = time.monotonic() + timeout
        count = 0
        while True:
            hits = [(h, t) for h, t in self.windows() if locator.matches(t, want)]
            count = len(hits)
            if count == 1:
                return hits[0]
            if count > 1 or time.monotonic() > end:
                return None, count
            time.sleep(0.25)


def _element(live: _Live, hwnd: int, step: dict):
    """The one live control a step's locator names; raises Ambiguous or DriveError."""
    target = step.get("target") or {}
    sel = step.get("container") if step.get("action") == "select" else step.get("selector")
    el = live.win.element_from_handle(hwnd)
    if sel and "Index=" not in sel:
        found = live.win.find_all(el, sel)
        if len(found) > 1:
            raise Ambiguous(sel, len(found))
        if not found:
            raise DriveError(f"no control {sel}")
        return found[0]
    raise DriveError(
        f"{sel}: located only by position (Index); not replayed ({target.get('note', '')})"
    )


def _act(live: _Live, hwnd: int, step: dict, values: dict[str, list[str]]) -> str:
    win = live.win
    action = step.get("action")
    props = (step.get("target") or {}).get("props") or {}
    if action == "type":
        el = _element(live, hwnd, step)
        aid = props.get("AutomationId") or ""
        given = values.get(aid) or []
        if step.get("password") and not given:
            raise ValueError(f"a password field: give its value with --value {aid}=...")
        if given:
            text = given.pop(0) if len(given) > 1 else given[0]  # in order; the last one repeats
        else:
            text = value_for(step.get("typed_shape"))
        try:
            el.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        win.set_value(el, text)
        return (
            "typed (value supplied)" if given else "typed a synthetic value of the recorded shape"
        )
    if action in ("click", "double_click"):
        el = _element(live, hwnd, step)
        return win.post_click(el) or _async(lambda: win.invoke(el))
    if action == "toggle":
        el = _element(live, hwnd, step)
        if step.get("state") and win.toggle_state(el) == step["state"]:
            return "already in the recorded state"
        return win.post_click(el) or _async(lambda: win.toggle(el)) or "toggled"
    if action == "select":
        if step.get("item_is") != "code":
            raise ValueError(
                "the item selected holds data (its name was not recorded); not replayed"
            )
        el = _element(live, hwnd, step)
        win.expand(el, True)
        time.sleep(0.3)
        found = win.find_all(el, locator.format({"Name": step["item"], "ControlType": "ListItem"}))
        if len(found) != 1:
            # Win32 drop-downs list their items in a window of their own (ComboLBox).
            found = [f for h in win.top_windows(live.pids) for f in
                     win.find_all(win.element_from_handle(h),
                                  locator.format({"Name": step["item"], "ControlType": "ListItem"}))]  # fmt: skip
        if len(found) != 1:
            raise ValueError(f"item {step['item']!r} found {len(found)} time(s)")
        win.select(found[0])
        try:
            win.expand(el, False)
        except Exception:  # noqa: BLE001
            pass
        return f"selected {step['item']}"
    if action == "menu":
        name = props.get("Name") or (step.get("menu_path") or "").split(" > ")[-1]
        sel = locator.format({"Name": name, "ControlType": "MenuItem"})
        found, rids = [], set()
        for h in [hwnd, *[x for x in win.top_windows(live.pids) if x != hwnd]]:
            for f in win.find_all(win.element_from_handle(h), sel):
                rid = tuple(f.GetRuntimeId())
                if rid not in rids:  # an open menu shows under its item and in its own window
                    rids.add(rid)
                    found.append(f)
        if len(found) > 1:
            raise Ambiguous(sel, len(found))
        if not found:
            raise ValueError(f"no menu item {name!r}")
        el = found[0]
        try:
            win.expand(el, True)
            return "opened the menu"
        except Exception:  # noqa: BLE001 - a leaf item
            return _async(lambda: win.invoke(el)) or "invoked"
    if action == "key":
        key = step.get("key") or ""
        if not win.post_key(win.pids_focus(live.pids), key):
            raise ValueError(f"key {key} could not be sent to the app's focused control")
        return f"posted {key}"
    raise ValueError(f"action {action} is not replayed")


def verify(exe: str, recording: Path, *, values: dict[str, str | list[str]] | None = None,
           settle: float = 0.8, wait: float = 20.0, geometry: bool = False, rows: bool = False,
           pictures_dir: Path | None = None, picture_threshold: float = 1.0,
           observe: dict | None = None) -> FidelityReport:  # fmt: skip
    """Start `exe`, replay `recording`, compare, close the app. Windows only.

    `observe` ({"after": {seq, ...}}) gets the live windows' trees once each of those steps ran
    (`observe["trees"][seq]`) and when the replay ends (`observe["trees"]["end"]`), for the
    acceptance cases (`judge_cases`).

    `geometry`: also compare the client area and every control's bounds and font. `rows`: also
    compare how many rows each grid shows. `pictures_dir`: save a picture without data of the
    window after each step there and compare it with the recorded one (percent of pixels that
    differ; above `picture_threshold` is a difference)."""
    from twinlab2_recorder.desktop import require

    require_live()
    require()
    rec = Recording(Path(recording))
    # AutomationId -> the values to type there, in order (a wrong password, then the right one).
    values = {k: list(v) if isinstance(v, list | tuple) else [v] for k, v in (values or {}).items()}
    proc = subprocess.Popen([exe])
    reports: list[StepReport] = []
    try:
        from twinlab2_recorder.desktop import win

        end = time.monotonic() + wait
        while not win.top_windows({proc.pid}) and time.monotonic() < end:
            time.sleep(0.2)
        live = _Live({proc.pid})
        supplied: set[str] = set()
        last_window = None
        wanted = set((observe or {}).get("after") or ())
        trees = observe.setdefault("trees", {}) if observe is not None else {}
        for step in rec.steps:
            try:
                action = step.get("action")
                wsel = (step.get("window") or {}).get("selector") or last_window
                last_window = wsel
                seq, sel = step.get("seq"), step.get("selector")
                diffs: list[Difference] = []
                note = None
                status = "pass"
                if action in ("focus", "menu_open", "expand", "collapse"):
                    reports.append(StepReport(seq=seq, action=action, selector=sel, status="skipped",
                                              note="covered by the steps around it"))  # fmt: skip
                    continue
                if action == "window_close":
                    time.sleep(settle)
                    hwnd, n = live.window(wsel, timeout=2) if wsel else (None, 0)
                    if hwnd is not None:
                        diffs.append(Difference(step=seq, element=wsel, check="extra", expected="closed",
                                                actual="still open"))  # fmt: skip
                    reports.append(StepReport(seq=seq, action=action, selector=wsel,
                                              status="fail" if diffs else "pass", differences=diffs))  # fmt: skip
                    continue
                hwnd, found = live.window(wsel) if wsel else (None, 0)
                if hwnd is None:
                    check = "ambiguous" if found > 1 else "missing"
                    reports.append(StepReport(seq=seq, action=action, selector=sel, status="fail",
                        differences=[Difference(step=seq, element=wsel or "(window)", check=check,
                                                expected=1, actual=found)]))  # fmt: skip
                    continue
                if action != "window_open":
                    try:
                        note = _act(live, hwnd, step, values)
                        if action == "type" and sel:
                            supplied.add(sel)
                    except Ambiguous as exc:
                        status = "fail"
                        diffs.append(Difference(step=seq, element=exc.selector, check="ambiguous",
                                                expected=1, actual=exc.count))  # fmt: skip
                    except (DriveError, ValueError) as exc:
                        status = "fail" if isinstance(exc, DriveError) else "skipped"
                        note = str(exc)
                        if isinstance(exc, DriveError):
                            diffs.append(Difference(step=seq, element=sel or "(control)", check="missing",
                                                    expected=1, actual=0))  # fmt: skip
                    time.sleep(settle)
                recorded = rec.tree(step.get("tree"))
                if recorded is not None:
                    after, _n = live.window(wsel, timeout=3)
                    tree = None
                    if after is not None:
                        tree = next((t for h, t in live.windows() if h == after), None)
                    if tree is not None:
                        from twinlab2.replay.fidelity import twin_label_required
                        from twinlab2_contract import package

                        if twin_label_required():  # the twin's own label: required, never compared
                            if not package.tree_has_label(tree):
                                diffs.append(Difference(step=seq, element="(AI generated Twin App label)",
                                                        check="twin_label", expected=package.TWIN_LABEL, actual=None))
                            tree = package.strip_label_tree(tree)
                        diffs += compare_trees(recorded, tree, seq, supplied)
                        late = bool(step.get("tree_late"))
                        if late:
                            note = "; ".join(x for x in (note, "the recording read this window only "
                                             "after later steps: rows and picture not compared")
                                             if x)  # fmt: skip
                        if rows and not late:
                            diffs += compare_rows(recorded, tree, seq)
                        if geometry:
                            diffs += compare_geometry(recorded, tree, seq)
                        if pictures_dir is not None and not late:
                            pic_note, pic_diff = _compare_picture(live, after, rec, step, pictures_dir,
                                                                  picture_threshold)  # fmt: skip
                            note = "; ".join(x for x in (note, pic_note) if x) or None
                            diffs += pic_diff
                    elif step.get("action") != "window_open":
                        note = (note or "") + "; the window was gone when it was compared"
                if diffs and status == "pass":
                    status = "fail"
                reports.append(StepReport(seq=seq, action=action, selector=sel, status=status,
                                          differences=diffs, note=note))  # fmt: skip
            finally:  # the windows once this step ran, for the acceptance cases
                if step.get("seq") in wanted:
                    trees[step.get("seq")] = [t for _h, t in live.windows()]

        if observe is not None:
            time.sleep(settle)
            trees["end"] = [t for _h, t in live.windows()]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    failed = any(r.status == "fail" for r in reports)
    report = FidelityReport(recording=rec.id, base_url=f"exe:{Path(exe).name}",
                            status="fail" if failed else "pass", steps=reports)  # fmt: skip
    judged(report, geometry=geometry, rows=rows, pictures=pictures_dir is not None)
    return report


def judged(report: FidelityReport, *, geometry: bool, rows: bool, pictures: bool) -> None:
    """The verdict and assurance of a desktop replay, in the web report's terms: VERIFIED
    COMPLETE only when every step passed with the structure, geometry, rows and pictures all
    compared, and no step was skipped except those the steps around them cover."""
    from twinlab2.replay import judging

    if report.status != "pass":
        report.verdict = "FAIL"
        report.verdict_reasons = [f"step {s.seq}: {d.text()}" for s in report.steps
                                  for d in s.differences][:20] or ["a step failed"]  # fmt: skip
        return
    report.verdict = "PASS WITHIN DECLARED SCOPE"
    missing = [n for n, on in (("geometry", geometry), ("rows", rows), ("pictures", pictures))
               if not on]  # fmt: skip
    skipped = [s.seq for s in report.steps
               if s.status == "skipped" and s.note != "covered by the steps around it"]  # fmt: skip
    if not missing and not skipped:
        report.assurance = judging.VERIFIED
        report.assurance_reasons = ["every step replayed; structure, geometry, fonts, grid rows "
                                    "and pictures compared"]  # fmt: skip
    else:
        report.assurance = judging.DECLARED_ONLY
        report.assurance_reasons = ([f"not compared: {', '.join(missing)}"] if missing else []) + (
            [f"skipped: steps {skipped}"] if skipped else [])  # fmt: skip


def _compare_picture(live: _Live, hwnd: int, rec, step: dict, folder: Path,
                     threshold: float) -> tuple[str | None, list[Difference]]:  # fmt: skip
    """Save the live window's picture (without data) and compare it with the recorded one."""
    png = live.picture(hwnd)
    if png is None:
        return "no picture of the live window", []
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    seq = step.get("seq")
    (folder / f"{seq:04d}.png").write_bytes(png)
    want = rec.path / step["picture"] if step.get("picture") else None
    if want is None or not want.is_file():
        return None, []
    pct = picture_difference(want.read_bytes(), png)
    note = f"picture differs {pct:.2f} %"
    if pct > threshold:
        return note, [Difference(step=seq, element="(picture)", check="picture",
                                 expected=f"<= {threshold} %", actual=f"{pct:.2f} %")]  # fmt: skip
    return note, []


# --- acceptance cases and the entry point Verify runs ------------------------------------------------


def _yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def judge_case(case: dict, windows: list[dict] | None) -> dict:
    """One acceptance case on the live windows' trees (value-free, as `_Live.windows` gives
    them): {id, check, required, after, status (PASS | FAIL | BLOCKED), note}."""
    require_modules()
    out = {"id": str(case.get("id") or "case"), "check": str(case.get("check") or "control"),
           "required": bool(case.get("required", True)), "after": str(case.get("after", "end")),
           "note": case.get("note")}
    sel, expect = case.get("selector"), str(case.get("expect") or "present")
    if windows is None:
        return {**out, "status": "BLOCKED", "note": f"step {out['after']} did not run"}
    if (not isinstance(sel, str) or not sel.startswith("uia:")
            or expect not in ("present", "absent", "enabled", "disabled")):
        return {**out, "status": "BLOCKED",
                "note": "a desktop case needs selector: uia:... and expect: present, absent, enabled or disabled"}
    if out["check"] == "window":
        want = window_wanted(sel)
        hits = [(t, ()) for t in windows if locator.matches(t, want)]
    elif out["check"] == "control":
        hits = [h for t in windows for h in locator.find(t, sel)]
    else:
        return {**out, "status": "BLOCKED", "note": f"check {out['check']!r}: window or control"}
    if len(hits) > 1 and expect != "absent":
        return {**out, "status": "BLOCKED", "note": f"{sel} matches {len(hits)} controls; not choosing one"}
    if expect == "absent":
        ok = not hits
    elif expect == "present":
        ok = len(hits) == 1
    else:
        ok = len(hits) == 1 and bool(hits[0][0].get("enabled")) == (expect == "enabled")
    if ok:
        return {**out, "status": "PASS"}
    state = ""
    if hits:
        state = " (enabled)" if hits[0][0].get("enabled") else " (disabled)"
    return {**out, "status": "FAIL", "note": f"{sel}: expected {expect}, found {len(hits)} match(es){state}"}


def judge_cases(decl: list[dict], trees: dict) -> list[dict]:
    out = []
    for n, case in enumerate(decl, 1):
        case = {"id": f"case-{n}", **case}
        after = case.get("after", "end")
        key = "end" if str(after).lower() == "end" else after
        if key != "end":
            try:
                key = int(key)
            except (TypeError, ValueError):
                out.append({"id": str(case["id"]), "check": str(case.get("check") or "control"),
                            "required": bool(case.get("required", True)), "after": str(after), "status": "BLOCKED",
                            "note": f"after: {after!r} is neither a step number nor end"})
                continue
        out.append(judge_case(case, trees.get(key)))
    return out


def values_of(folder: Path) -> dict[str, list[str]]:
    """AutomationId -> the values typed there, in order, from bindings.yaml beside the recording
    (`fields: {txtUser: [tester], txtPassword: [wrong, demo]}`)."""
    data = _yaml(Path(folder) / FILE_BINDINGS)
    return {str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
            for k, v in (data.get("fields") or {}).items()}


def finish(report: FidelityReport, cases: list[dict]) -> dict:
    """The replay's report with its acceptance cases folded in, as a dict (Verify's shape)."""
    out = json.loads(report.model_dump_json())
    out["surface"] = "desktop"
    out["cases"] = cases
    out["visual"] = []
    required = [c for c in cases if c["required"]]
    if out["status"] == "pass" and any(c["status"] == "FAIL" for c in required):
        out["status"] = "fail"
        out["verdict"] = "FAIL"
        out.setdefault("problems", []).append("required acceptance case(s) failed: " + ", ".join(
            c["id"] for c in required if c["status"] == "FAIL"))
    elif out["status"] == "pass" and any(c["status"] == "BLOCKED" for c in required):
        out["status"] = "blocked"
        out["verdict"] = "BLOCKED"
    steps = out.get("steps") or []
    out["coverage"] = {"steps": len(steps), "replayed": sum(1 for x in steps if x["status"] in ("pass", "fail")),
                       "compared": sum(1 for x in steps if x["status"] in ("pass", "fail")),
                       "failed": sum(1 for x in steps if x["status"] == "fail"),
                       "skipped": sum(1 for x in steps if x["status"] == "skipped")}
    out["categories"] = {"replay": {"status": "FAIL" if any(x["status"] == "fail" for x in steps) else "PASS",
                                    "note": "each window's controls compared with the recorded tree"}}
    return out


def replay(recording: Path, exe: str) -> dict:
    """Verify's desktop replay: the values from bindings.yaml, the acceptance cases from
    acceptance.yaml, `verify` on `exe`. DesktopUnavailable where it may not run."""
    folder = Path(recording)
    decl = [c for c in (_yaml(folder / FILE_ACCEPTANCE).get("cases") or []) if isinstance(c, dict)]
    after = set()
    for c in decl:
        try:
            after.add(int(c.get("after")))
        except (TypeError, ValueError):
            pass
    observe = {"after": after}
    report = verify(exe, folder, values=values_of(folder), observe=observe)
    out = finish(report, judge_cases(decl, observe.get("trees") or {}))
    if framed(folder):
        out["categories"]["replay"]["note"] += (
            "; a modern (UWP / WinUI) application's window: the frame host's layers and the "
            "controls' toolkit classes (ClassName, FrameworkId) are not compared")
    return out


def framed(folder: Path) -> bool:
    """Whether a desktop recording's windows are a frame host's (a modern application)."""
    rec = Recording(Path(folder))
    return any((rec.tree(s.get("tree")) or {}).get("framed") for s in rec.steps[:5])
