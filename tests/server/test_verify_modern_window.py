"""Desktop Verify of a modern (UWP / WinUI) application, from the live Windows Calculator proof
(2026-11-21, rebuild-proof/live-calc): no window is opened.

The recorder sees Calculator's window as Windows' frame host draws it (ApplicationFrameWindow >
title bar, input sink, the app's CoreWindow). A blind WPF rebuild has none of those classes, so
the recorded window locator (`ClassName=ApplicationFrameWindow`) found no window and not one
step was replayed. Verify now compares the window as the application draws it.
"""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2.replay import desktop

FRAMED = {
    "type": "Window", "name": "Calculator", "cls": "ApplicationFrameWindow", "fw": "Win32",
    "children": [
        {"type": "Window", "aid": "TitleBar", "name": "Calculator", "cls": "ApplicationFrameTitleBarWindow",
         "fw": "Win32", "children": [
             {"type": "Button", "aid": "Close", "name": "Close Calculator", "cls": None, "fw": "Win32"}]},
        {"type": "Window", "name": "Calculator", "cls": "Windows.UI.Core.CoreWindow", "fw": "XAML", "children": [
            {"type": "Group", "aid": "NumberPad", "name": "Number pad", "name_is": "structure",
             "cls": "NamedContainerAutomationPeer", "fw": "XAML", "children": [
                 {"type": "Button", "aid": "num1Button", "name": "One", "name_is": "structure",
                  "cls": "Button", "fw": "XAML", "enabled": True, "visible": True}]}]},
        {"type": "Pane", "cls": "ApplicationFrameInputSinkWindow", "fw": "Win32"},
    ]}

WPF = {
    "type": "Window", "name": "Calculator", "cls": "Window", "fw": "WPF", "children": [
        {"type": "Window", "aid": "TitleBar", "name": "Calculator", "cls": "Border", "fw": "WPF", "children": [
            {"type": "Button", "aid": "Close", "name": "Close Calculator", "cls": "Button", "fw": "WPF"}]},
        {"type": "Group", "aid": "NumberPad", "name": "Number pad", "name_is": "structure", "cls": "Region",
         "fw": "WPF", "children": [
             {"type": "Button", "aid": "num1Button", "name": "One", "name_is": "structure", "cls": "Button",
              "fw": "WPF", "enabled": True, "visible": True}]}]}


def copy(tree):
    return json.loads(json.dumps(tree))


class ModernWindow(unittest.TestCase):
    def test_the_window_is_found_by_name_not_by_the_frame_hosts_class(self):
        sel = "uia:Name=Calculator;ControlType=Window;ClassName=ApplicationFrameWindow"
        self.assertEqual(desktop.window_wanted(sel), {"Name": "Calculator", "ControlType": "Window"})
        other = "uia:AutomationId=MainForm;ControlType=Window;ClassName=WindowsForms10.Window"
        self.assertIn("ClassName", desktop.window_wanted(other))

    def test_unframe_keeps_what_the_application_draws(self):
        tree = desktop.unframe(copy(FRAMED))
        self.assertTrue(tree["framed"])
        self.assertNotIn("cls", tree)
        self.assertEqual([c.get("aid") for c in tree["children"]], ["TitleBar", "NumberPad"])
        self.assertNotIn("cls", tree["children"][0])  # the title bar keeps its controls
        plain = copy(WPF)
        self.assertIs(desktop.unframe(plain), plain)  # any other window as it is

    def test_a_wpf_rebuild_matches_the_framed_recording(self):
        self.assertEqual(desktop.compare_trees(desktop.unframe(copy(FRAMED)), copy(WPF)), [])

    def test_a_missing_key_is_still_found(self):
        live = copy(WPF)
        live["children"][1]["children"] = []
        diffs = desktop.compare_trees(desktop.unframe(copy(FRAMED)), live)
        self.assertEqual([(d.check, d.element) for d in diffs],
                         [("missing", "uia:AutomationId=num1Button;ControlType=Button")])

    def test_without_unframe_the_rebuild_could_never_match(self):
        checks = {d.check for d in desktop.compare_trees(copy(FRAMED), copy(WPF))}
        self.assertIn("changed", checks)

    def test_a_recording_folder_says_it_is_framed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "desktop"
            (folder / "trees").mkdir(parents=True)
            (folder / "trees" / "0001.json").write_text(json.dumps(FRAMED), encoding="utf-8")
            (folder / "steps.jsonl").write_text(json.dumps({"seq": 1, "action": "window_open",
                                                            "tree": "trees/0001.json"}) + "\n", encoding="utf-8")
            self.assertTrue(desktop.framed(folder))
            (folder / "trees" / "0001.json").write_text(json.dumps(WPF), encoding="utf-8")
            self.assertFalse(desktop.framed(folder))


if __name__ == "__main__":
    unittest.main()
