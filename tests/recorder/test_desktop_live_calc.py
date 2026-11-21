"""What the live Windows Calculator recording (2026-11-21, rebuild-proof/live-calc) found in the
desktop recorder, checked without opening a window:

- the toolkit the recorder detects (desktop.json) reaches the application's facts in the
  references, so the package names it (it did not: "The package does not name the toolkit");
- a selection a script makes through UI Automation a moment after its last action (Calculator's
  navigation: open the pane, then pick Scientific) is a step, not the application's own;
- one failed UI Automation call (Calculator's frame window read after it closed:
  EVENT_E_ALL_SUBSCRIBERS_FAILED) no longer ends the desktop channel; `win.capture` of a window
  that is gone is None (checked on Windows only).
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from twinlab2_recorder.desktop import recorder as drec
from twinlab2_recorder.workstation import timeline

CALC = "desktop:Microsoft Corporation/Microsoft Calculator"


def calc_session(folder: Path, processes: list[dict], facts: dict | None = None) -> None:
    (folder / "desktop").mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({
        "format": timeline.FORMAT, "id": folder.name, "name": "calc", "state": "stopped",
        "started": "2026-11-21T15:00:58.000Z", "ended": "2026-11-21T15:02:57.000Z",
        "targets": [{"key": CALC}], "channels": ["desktop"], "apps": {}, "notes": []}), encoding="utf-8")
    app = {"kind": "app", "pid": 3856, "key": CALC, "surface": "desktop", "exe": "CalculatorApp.exe",
           "company": "Microsoft Corporation", "product": "Microsoft Calculator", "display": "Calculator",
           "level": "full", "ts": "2026-11-21T15:01:03.880Z", **(facts or {})}
    (folder / "journal.jsonl").write_text(json.dumps(app) + "\n", encoding="utf-8")
    step = {"seq": 1, "ts": "2026-11-21T15:01:08.879Z", "surface": "desktop", "action": "click",
            "process": "CalculatorApp.exe", "pid": 3856,
            "window": {"selector": "uia:Name=Calculator;ControlType=Window;ClassName=ApplicationFrameWindow",
                       "title": "Calculator"},
            "selector": "uia:AutomationId=num1Button;ControlType=Button",
            "target": {"props": {"ControlType": "Button", "AutomationId": "num1Button", "Name": "One"}},
            "event_id": "01a1fea0-2a94-717d-b5a3-8a82f4fe2550"}
    (folder / "desktop" / "steps.jsonl").write_text(json.dumps(step) + "\n", encoding="utf-8")
    (folder / "desktop" / "desktop.json").write_text(json.dumps({"processes": processes}), encoding="utf-8")


def uwp(confidence="certain", process="CalculatorApp.exe", toolkit="UWP / WinUI"):
    return {"process": process, "pid": 3856,
            "technology": {"surface": "desktop", "toolkit": toolkit, "confidence": confidence}}


class ToolkitInReferences(unittest.TestCase):
    def build(self, processes, facts=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "20261121T150058Z-0f702b8f"
            calc_session(folder, processes, facts)
            box = timeline.build(folder)
        return next(a for a in box["apps"] if a["key"] == CALC)

    def test_the_detected_toolkit_is_an_application_fact(self):
        self.assertEqual(self.build([uwp()])["toolkit"], "UWP / WinUI")
        self.assertEqual(self.build([uwp("likely")])["toolkit"], "UWP / WinUI")

    def test_a_guess_or_two_toolkits_for_one_executable_name_none(self):
        self.assertNotIn("toolkit", self.build([uwp("guess")]))
        self.assertNotIn("toolkit", self.build([uwp(), uwp(toolkit="Win32")]))
        self.assertNotIn("toolkit", self.build([uwp(process="Other.exe")]))
        self.assertNotIn("toolkit", self.build([]))

    def test_a_toolkit_already_known_is_kept(self):
        self.assertEqual(self.build([uwp()], {"toolkit": "winforms"})["toolkit"], "winforms")


def item(rid, ancestors, types):
    return {"rid": rid, "ancestors": ancestors, "ancestor_types": types, "type": "ListItem",
            "window": 100}


# Calculator: the navigation button and the mode list share only the NavigationView
TOGGLE = {"rid": [42, 1], "ancestors": [[42, 9], [42, 0]], "ancestor_types": ["Custom", "Window"]}
SCIENTIFIC = item([42, 7], [[42, 5], [42, 4], [42, 9], [42, 0]], ["Group", "Window", "Custom", "Window"])


class ScriptedSelection(unittest.TestCase):
    def last(self, t, **action):
        return {"desc": TOGGLE, "t": t, "action": {"kind": "click", **action}}

    def test_the_rule_alone_gives_the_mode_choice_to_the_application(self):
        # what dropped Calculator's mode changes from the live recording
        self.assertTrue(drec.self_made_selection(SCIENTIFIC, TOGGLE))

    def test_a_selection_a_moment_after_a_scripted_action_is_the_scripts(self):
        self.assertTrue(drec.scripted_selection(11.2, self.last(10.0, via="uia-invoke")))
        self.assertTrue(drec.scripted_selection(11.0, self.last(10.0, via="uia-selection")))

    def test_at_once_or_after_a_mouse_click_it_stays_the_applications(self):
        self.assertFalse(drec.scripted_selection(10.3, self.last(10.0, via="uia-invoke")))
        self.assertFalse(drec.scripted_selection(12.0, self.last(10.0, via="uia-invoke", mouse=True)))
        self.assertFalse(drec.scripted_selection(12.0, self.last(10.0, via="mouse-hook")))
        self.assertFalse(drec.scripted_selection(12.0, {"desc": {}, "t": 10.0, "action": None}))
        self.assertFalse(drec.scripted_selection(12.0, None))

    def test_the_recorder_keeps_the_scripted_choice_as_a_step(self):
        rec = object.__new__(drec.Recorder)
        rec.popup_owner, rec.pending = {}, []
        rec.last_user = self.last(10.0, via="uia-invoke")
        self.assertFalse(rec._own_selection({"desc": SCIENTIFIC, "t": 11.8}))
        # the same selection at once: the application's own (an effect, or counted)
        self.assertTrue(rec._own_selection({"desc": SCIENTIFIC, "t": 10.2}))
        self.assertEqual(rec.effects_unplaced, 1)


class LoopGoesOn(unittest.TestCase):
    def test_a_failed_call_is_noted_once_and_counted(self):
        rec = object.__new__(drec.Recorder)
        rec.notes = []

        def gone():
            raise OSError(-2147220991, "An event was unable to invoke any of the subscribers")

        self.assertFalse(rec._guarded("reading the windows", gone))
        self.assertFalse(rec._guarded("reading the windows", gone))
        self.assertTrue(rec._guarded("reading the windows", lambda: None))
        self.assertEqual(len(rec.notes), 1)
        self.assertIn("reading the windows: OSError", rec.notes[0])
        self.assertEqual(rec.loop_failures, {"reading the windows: OSError": 2})

    def test_ctrl_c_still_ends_the_loop(self):
        rec = object.__new__(drec.Recorder)
        rec.notes = []

        def interrupt():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            rec._guarded("handling an event", interrupt)


def nav_tree() -> dict:
    """Calculator's window, cut down: the navigation's mode items (a Group, not a list) and the
    history list (a List, whose items are records)."""
    def li(aid, name, selected=False, name_is="data"):
        return {"type": "ListItem", "aid": aid, "name": name, "name_is": name_is, "selected": selected}

    return {"type": "Window", "name": "Calculator", "children": [
        {"type": "Button", "aid": "TogglePaneButton", "name": "Close Navigation", "name_is": "structure"},
        {"type": "Group", "aid": "MenuItemsHost", "children": [
            li("Standard", "Standard Calculator", True), li("Scientific", "Scientific Calculator"),
            li("Item2", "Two"), li("", "No id")]},
        {"type": "List", "aid": "HistoryListView", "children": [li("HistoryItem", "12 + 7 = 19")]},
    ]}


class NavigationItems(unittest.TestCase):
    def test_mode_items_with_word_ids_are_controls_history_rows_are_not(self):
        from twinlab2_recorder.workstation import structure

        state = structure.desktop_state(nav_tree())
        items = [c for c in state["controls"] if c["kind"] == "ListItem"]
        self.assertEqual([(c["id"], c.get("selected")) for c in items], [("Standard", True), ("Scientific", False)])
        self.assertTrue(all("label" not in c for c in items))  # their names were judged data
        self.assertEqual(state["grids"], [{"kind": "List", "rows": "1", "id": "HistoryListView"}])


@unittest.skipUnless(sys.platform == "win32", "UI Automation: Windows only")
class CaptureOfAGoneWindow(unittest.TestCase):
    def test_none_not_a_com_error(self):
        from twinlab2_recorder.desktop import win

        self.assertIsNone(win.capture(0x7FFFF0))  # no window has this handle


if __name__ == "__main__":
    unittest.main()


class Nesting(unittest.TestCase):
    """Contract 1.6 `in`: each control and region names the region it is inside (L6)."""

    def test_controls_name_their_container_not_the_window(self):
        from twinlab2_recorder.workstation import structure

        tree = {"type": "Window", "aid": "Main", "name": "Calculator", "children": [
            {"type": "Custom", "aid": "NavView", "children": [
                {"type": "Group", "name": "", "children": [  # an unnamed layer in between
                    {"type": "Group", "aid": "NumberPad", "children": [
                        {"type": "Button", "aid": "num1Button", "name": "One", "name_is": "structure"}]}]},
                {"type": "Button", "aid": "TogglePaneButton", "name": "Open Navigation", "name_is": "structure"}]},
            {"type": "Button", "aid": "Top", "name": "Top", "name_is": "structure"}]}
        state = structure.desktop_state(tree)
        where = {c["id"]: c.get("in") for c in state["controls"]}
        self.assertEqual(where, {"num1Button": "NumberPad", "TogglePaneButton": "NavView", "Top": None})
        regions = {r["id"]: r.get("in") for r in state["regions"]}
        self.assertEqual(regions, {"Main": None, "NavView": None, "NumberPad": "NavView"})

    def test_the_contract_keeps_in(self):
        from twinlab2_contract import structure as st

        state = st.clean_state({"controls": [{"kind": "Button", "id": "num1Button", "in": "NumberPad"}],
                                "regions": [{"kind": "Group", "id": "NumberPad", "in": "NavView"}],
                                "grids": [{"kind": "List", "rows": "few", "in": "DockPanel"}]})
        self.assertEqual(state["controls"][0]["in"], "NumberPad")
        self.assertEqual(state["regions"][0]["in"], "NavView")
        self.assertEqual(state["grids"][0]["in"], "DockPanel")
        bad = st.clean_state({"controls": [{"kind": "Button", "id": "b", "in": "C-1001 Ada Example"}]})
        self.assertNotIn("in", bad["controls"][0])


class RecorderWindowDpi(unittest.TestCase):
    def test_not_windows_is_scale_one(self):
        from unittest import mock

        from twinlab2_recorder import main as agent_main

        with mock.patch.object(agent_main.sys, "platform", "linux"):
            self.assertEqual(agent_main.dpi_aware(), 1.0)

    def test_windows_declares_per_monitor_awareness_and_reads_the_scale(self):
        import ctypes
        from unittest import mock

        from twinlab2_recorder import main as agent_main

        calls = []
        fake = mock.Mock()
        fake.shcore.SetProcessDpiAwareness.side_effect = lambda v: calls.append(v)
        fake.user32.GetDpiForSystem.return_value = 144
        with mock.patch.object(agent_main.sys, "platform", "win32"), \
                mock.patch.object(ctypes, "windll", fake, create=True):
            self.assertEqual(agent_main.dpi_aware(), 1.5)
        self.assertEqual(calls, [2])
