"""Desktop Verify of a modern app, second pass (wp/desktop-fidelity, from the live Calculator
proof): the frame's title bar is Windows' chrome, unnamed layout layers and caption texts are not
controls, a window the user closed is closed by the replay, a selected item is found by its own
AutomationId, and a judge-side `value` case checks a computed result. No window is opened."""

import unittest

from twinlab2.replay import desktop
from test_verify_modern_window import FRAMED, WPF, copy


class Chrome(unittest.TestCase):
    def test_the_frames_title_bar_and_the_rebuilds_own_are_not_compared(self):
        recorded = desktop.unframe(copy(FRAMED))
        self.assertEqual(recorded["children"][0]["type"], "TitleBar")
        live = copy(WPF)
        live["children"][0]["children"].append({"type": "MenuItem", "name": "System", "fw": "WPF"})
        self.assertEqual(desktop.compare_trees(recorded, live), [])

    def test_unnamed_layout_layers_of_a_modern_app_are_not_controls(self):
        framed = copy(FRAMED)
        core = framed["children"][1]
        core["children"] = [{"type": "Group", "cls": "LandmarkTarget", "fw": "XAML", "children": core["children"]}]
        self.assertEqual(desktop.compare_trees(desktop.unframe(framed), copy(WPF)), [])

    def test_a_caption_text_inside_a_tab_is_part_of_the_tab(self):
        live = copy(WPF)
        live["children"].append({"type": "Tab", "aid": "DockPivot", "children": [
            {"type": "TabItem", "aid": "HistoryLabel", "name": "History", "children": [
                {"type": "Text", "name": "History", "name_is": "structure"}]}]})
        recorded = desktop.unframe(copy(FRAMED))
        recorded["children"].append({"type": "Tab", "aid": "DockPivot", "children": [
            {"type": "TabItem", "aid": "HistoryLabel", "name": "History"}]})
        self.assertEqual(desktop.compare_trees(recorded, live), [])
        other = copy(live)
        other["children"][-1]["children"][0]["children"][0]["name"] = "Something else"
        self.assertTrue(desktop.compare_trees(recorded, other))


class Steps(unittest.TestCase):
    def test_a_close_long_after_the_last_step_was_the_users(self):
        prev = {"action": "click", "ts": "2026-11-21T15:01:33.550Z"}
        self.assertTrue(desktop.closed_by_user({"ts": "2026-11-21T15:01:40.000Z"}, prev))
        self.assertFalse(desktop.closed_by_user({"ts": "2026-11-21T15:01:34.000Z"}, prev))  # Save closed it
        self.assertTrue(desktop.closed_by_user({"ts": "2026-11-21T15:01:34.000Z"}, None))
        self.assertFalse(desktop.closed_by_user({"ts": "?"}, prev))

    def test_an_item_with_its_own_id_is_selected_by_it(self):
        self.assertTrue(desktop._own_id({"selector": "uia:AutomationId=Scientific;ControlType=ListItem"}))
        self.assertFalse(desktop._own_id({"selector": "uia:ControlType=ListItem;ClassName=X;Index=2"}))
        self.assertFalse(desktop._own_id({"selector": None}))


SEL = "uia:AutomationId=CalculatorResults;ControlType=Text"


class ValueCases(unittest.TestCase):
    def judge(self, values, **case):
        return desktop.judge_cases([{"id": "c", "after": 7, "check": "value", "selector": SEL, **case}],
                                   {7: [copy(WPF)]}, {7: values})[0]

    def test_the_computed_result_is_checked(self):
        self.assertEqual(self.judge({SEL: ["Display is 19"]}, contains="19")["status"], "PASS")
        got = self.judge({SEL: ["Display is 18"]}, contains="19")
        self.assertEqual(got["status"], "FAIL")
        self.assertNotIn("18", got["note"])  # the live text is not repeated in the report

    def test_none_or_several_controls(self):
        self.assertEqual(self.judge({SEL: []}, contains="19")["status"], "FAIL")
        self.assertEqual(self.judge({SEL: ["19", "19"]}, contains="19")["status"], "BLOCKED")
        self.assertEqual(self.judge({SEL: ["19"]})["status"], "BLOCKED")  # no expected text

    def test_a_step_that_did_not_run(self):
        got = desktop.judge_cases([{"id": "c", "after": 9, "check": "value", "selector": SEL, "contains": "1"}],
                                  {}, {})[0]
        self.assertEqual(got["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()


class NamedPath(unittest.TestCase):
    def test_a_class_name_does_not_name_a_container(self):
        path = ["uia:Name=Calculator;ControlType=Window", "uia:ControlType=Group;ClassName=LandmarkTarget;Index=0",
                "uia:AutomationId=NumberPad;ControlType=Group"]
        self.assertEqual(desktop._named(path), [path[0], path[2]])


class NestingInTheBrief(unittest.TestCase):
    def test_build_task_names_each_controls_container(self):
        from twinlab2 import buildpack

        line = buildpack._control_line({"kind": "Button", "id": "num1Button", "label": "One", "in": "NumberPad"})
        self.assertEqual(line, '- `Button#num1Button` (Button) "One" in `NumberPad`')
        self.assertEqual(buildpack._region({"kind": "Group", "id": "NumberPad", "in": "NavView"}),
                         "`Group#NumberPad` (in `NavView`)")
        self.assertEqual(buildpack._region({"kind": "Custom", "id": "NavView"}), "`Custom#NavView`")


class PackageBytesPerRevision(unittest.TestCase):
    """The same brief revision (manifest hash) packed by a newer portal is other bytes: freezing it
    again must not serve the earlier revision's zip (the live rerun's rev 2 download failed its
    SHA-256 check)."""

    def test_other_bytes_of_one_revision_are_kept_apart(self):
        import tempfile

        from twinlab2 import artifacts, packages
        from twinlab2.portal import create_app
        from support import connect, new_database

        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)  # last: after the catalog is closed (Windows)
        database = new_database(work.name)
        app = create_app(database)
        bound = artifacts.bind(artifacts.Binding(app.state.artifacts))
        self.addCleanup(artifacts.unbind, bound)
        db = connect(database)
        self.addCleanup(db.close)
        with db:
            app_id = db.execute("INSERT INTO apps(name, git_url, requirements, status, surface) "
                                "VALUES ('Calculator', '', '', 'known', 'desktop') RETURNING id").fetchone()[0]
            db.execute("INSERT INTO briefs(revision, app_id, manifest) VALUES ('r1', ?, '{}')", (app_id,))
            first = packages.put_package(db, app_id, "r1", b"old bytes", "{}")
            again = packages.put_package(db, app_id, "r1", b"old bytes", "{}")
            other = packages.put_package(db, app_id, "r1", b"new bytes", "{}")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(packages.open_package(db, first), b"old bytes")
        self.assertEqual(packages.open_package(db, other), b"new bytes")
