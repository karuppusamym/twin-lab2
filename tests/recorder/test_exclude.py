"""The PC's own exclude list (twinlab2_recorder.workstation.exclude): an excluded program, window
title or web address leaves nothing in a recording but the exclude entry's name.

Plain Python on fakes: a fake `desktop.win` stands in for Windows (no window is opened or read),
fake requests for Playwright's. The picture test that paints real pixels runs only on Windows,
off screen (GDI on an in-memory bitmap)."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from twinlab2_contract import recorder_contract as contract
from twinlab2_recorder import main as agent_main
from twinlab2_recorder import settings as st
from twinlab2_recorder.web import recorder as web
from twinlab2_recorder.workstation import completeness
from twinlab2_recorder.workstation import exclude
from twinlab2_recorder.workstation import levels as lv
from twinlab2_recorder.workstation import session as ses
from twinlab2_recorder.workstation import timeline

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def policy(*entries):
    return exclude.Policy.from_list([exclude.parse_entry(e) for e in entries])


class RulesTest(unittest.TestCase):
    def test_a_program_by_name_case_and_extension_aside(self):
        pol = policy("WINWORD.EXE", "process: excel", "adobe*.exe")
        self.assertEqual(pol.process("winword.exe").pattern, "WINWORD.EXE")
        self.assertEqual(pol.process(r"C:\Program Files\Microsoft Office\EXCEL.EXE").pattern, "excel")
        self.assertIsNotNone(pol.process("AdobeCollabSync.exe"))
        self.assertIsNone(pol.process("CaseDesk.exe"))
        self.assertIsNone(pol.process("WINWORD.EXE.bak"))

    def test_a_window_title_anywhere_or_whole_with_wildcards(self):
        pol = policy("title: Personal", "title: * - Excel")
        self.assertIsNotNone(pol.title("Personal budget 2026"))
        self.assertIsNotNone(pol.title("Q3   figures.xlsx - Excel"))
        self.assertIsNone(pol.title("Excel tips - CaseDesk"))  # `* - Excel` is the whole title
        self.assertIsNone(pol.title(""))

    def test_a_host_covers_its_subdomains_and_a_prefix_whole_segments(self):
        pol = policy("mail.google.com", "*.slack.com", "https://erp.example.test/hr/")
        self.assertIsNotNone(pol.url("https://mail.google.com/mail/u/0/#inbox"))
        self.assertIsNotNone(pol.url("https://eu.mail.google.com/"))
        self.assertIsNone(pol.url("https://google.com/"))
        self.assertIsNotNone(pol.url("https://acme.slack.com/messages"))
        self.assertIsNotNone(pol.url("https://erp.example.test/hr/payslips/2026"))
        self.assertIsNotNone(pol.url("https://ERP.example.test/HR"))
        self.assertIsNone(pol.url("https://erp.example.test/hrm/"))  # not the same segment
        self.assertIsNone(pol.url("https://erp.example.test/orders"))
        self.assertIsNone(pol.url("http://erp.example.test/hr/"))  # another scheme, another site

    def test_what_a_person_types_becomes_the_right_kind(self):
        self.assertEqual(exclude.parse_entry("EXCEL.EXE").kind, "process")
        self.assertEqual(exclude.parse_entry("mail.google.com").kind, "web")
        self.assertEqual(exclude.parse_entry("https://intranet/hr/").kind, "web")
        self.assertEqual(exclude.parse_entry("Personal budget").kind, "title")
        self.assertEqual(exclude.parse_entry("window: x.exe").kind, "title")
        self.assertIsNone(exclude.parse_entry("*"))  # would exclude everything
        self.assertIsNone(exclude.parse_entry("process: *.exe"))

    def test_the_local_list_wins_over_a_target_and_the_portal(self):
        targets = lv.Targets.from_list([{"exe": "WINWORD.EXE", "allow_denied": True}, {"exe": "CaseDesk.exe"}],
                                       deny_keys=["desktop:exe/notepad.exe"], exclude=policy("WINWORD.EXE"))  # fmt: skip
        word = lv.desktop_identity(r"C:\Office\WINWORD.EXE", {"company": "Microsoft Corporation",
                                                              "product": "Microsoft Office"})  # fmt: skip
        self.assertEqual(targets.level(word), lv.EXCLUDED)
        self.assertEqual(targets.level(lv.desktop_identity(r"C:\Apps\CaseDesk.exe")), lv.FULL)
        self.assertEqual(targets.level(lv.desktop_identity(r"C:\Apps\notepad.exe")), lv.PRESENCE)  # deny_keys
        web_id = lv.web_identity("https://mail.google.com/mail")
        self.assertEqual(lv.Targets.from_list([], exclude=policy("mail.google.com")).level(web_id), lv.EXCLUDED)

    def test_the_session_copy_keeps_each_entry_and_where_it_came_from(self):
        pol = exclude.Policy([exclude.Rule("process", "WINWORD.EXE", "Microsoft Word", "managed"),
                              exclude.Rule("web", "mail.google.com", "", "user")], managed=True)  # fmt: skip
        again = exclude.Policy.from_session(json.loads(json.dumps(pol.as_session())))
        self.assertEqual(again.rules, pol.rules)
        self.assertTrue(again.managed)


class FakeWin(types.ModuleType):
    """The few desktop.win calls the session's watcher makes, over a table of windows:
    {hwnd: (pid, exe, title, company, product)}."""

    def __init__(self, windows):
        super().__init__("twinlab2_recorder.desktop.win")
        self.windows = windows
        self.fg = []

    def foreground(self):
        return self.fg.pop(0) if self.fg else 0

    def app_pid(self, hwnd):
        return self.windows[hwnd][0]

    def window_pid(self, hwnd):
        return self.windows[hwnd][0]

    def _by_pid(self, pid):
        return next(w for w in self.windows.values() if w[0] == pid)

    def process_path(self, pid):
        return "C:\\Programs\\" + self._by_pid(pid)[1]

    def exe_info(self, path):
        w = next(w for w in self.windows.values() if path.endswith("\\" + w[1]))
        return {"company": w[3], "product": w[4]} if w[3] else {}

    def package_family(self, pid):
        return None

    def host_facts(self, pid):
        return {}

    def parent_pid(self, pid):
        return None


WINDOWS = {1: (10, "WINWORD.EXE", "Budget.docx - Word", "Microsoft Corporation", "Microsoft Office"),
           2: (20, "notepad.exe", "notes.txt - Notepad", "", ""),
           3: (30, "CalculatorApp.exe", "Personal budget", "", ""),
           4: (40, "CaseDesk.exe", "Case 1042 - CaseDesk", "Twin Lab reference", "CaseDesk")}  # fmt: skip


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.win = FakeWin(WINDOWS)
        import twinlab2_recorder.desktop as desktop

        for patcher in (mock.patch.dict(sys.modules, {"twinlab2_recorder.desktop.win": self.win}),
                        mock.patch.object(desktop, "win", self.win, create=True),
                        mock.patch.object(ses, "_title", lambda _w, h: WINDOWS[h][2] if h in WINDOWS else ""),
                        mock.patch.object(ses, "_window_pids", lambda _w: set()),
                        mock.patch.object(ses, "_elevated", lambda _pid: None)):  # fmt: skip
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, pol):
        targets = lv.Targets.from_list([{"exe": "CaseDesk.exe", "name": "CaseDesk"}], exclude=pol)
        s = ses.Session(self.tmp, targets, name="t")
        s.rows = []
        s._journal_row = s.rows.append
        return s

    def watch(self, s, foreground):
        self.win.fg = list(foreground)
        with mock.patch.object(ses.time, "sleep", lambda _s: None if self.win.fg else s.stop()):
            s._watch()

    def test_an_excluded_program_and_title_leave_no_presence_row(self):
        s = self.session(policy("WINWORD.EXE", "title: Personal"))
        self.watch(s, [1, 2, 3, 2])
        presence = [r for r in s.rows if r.get("kind") == "presence"]
        self.assertEqual([r["exe"] for r in presence], ["notepad.exe", "notepad.exe"])  # Notepad only
        excluded = [r for r in s.rows if r.get("kind") == "excluded"]
        self.assertEqual(excluded, [{"kind": "excluded", "name": "WINWORD.EXE"},
                                    {"kind": "excluded", "name": "Personal"}])  # no time, title or pid
        text = json.dumps(s.rows)
        for leak in ("Budget", "Word", "Calculator", "budget"):
            self.assertNotIn(leak, text.replace("WINWORD", ""))

    def test_a_target_this_pc_excludes_is_not_recorded_and_the_notes_say_so(self):
        s = self.session(policy("CaseDesk.exe"))
        self.watch(s, [4])
        self.assertFalse([r for r in s.rows if r.get("kind") in ("app", "presence")])
        self.assertEqual(s.apps, {})
        self.assertIn("CaseDesk is a target of this session, but this PC's exclude list names it: it is not "
                      "recorded", s.notes)  # fmt: skip

    def test_the_desktop_recorder_hears_which_windows_are_excluded(self):
        s = self.session(policy("WINWORD.EXE", "title: Personal"))
        self.assertEqual(s.excluded_window(1), "WINWORD.EXE")
        self.assertEqual(s.excluded_window(3), "Personal")
        self.assertIsNone(s.excluded_window(4))
        self.assertEqual(set(s.desktop_exclusions()), {"exclude_window", "exclusion_masks", "paused"})
        self.assertEqual(set(self.session(exclude.Policy()).desktop_exclusions()), {"paused"})

    def test_a_picture_is_masked_where_an_excluded_window_overlaps_it(self):
        s = self.session(policy("WINWORD.EXE"))
        rects = {4: (100, 100, 400, 300), 1: (400, 300, 300, 300), 2: (120, 120, 100, 100)}
        with mock.patch.object(ses, "_rect", lambda _w, h: rects.get(h)), \
                mock.patch.object(ses, "_above", lambda _w, h: [1, 2]), \
                mock.patch.object(ses, "_embedded", lambda _w, h, own: []):  # fmt: skip
            self.assertEqual(s.exclusion_masks(4), [[300, 200, 100, 100]])  # Word's corner, not Notepad
            rects[1] = (50, 50, 500, 400)  # Word now covers the whole window
            self.assertIsNone(s.exclusion_masks(4))  # the picture is not kept at all


class MasksTest(unittest.TestCase):
    def test_window_relative_rectangles_and_the_share_covered(self):
        rects, share = exclude.masks((100, 100, 400, 300), [(400, 300, 300, 300), (900, 900, 10, 10), None])
        self.assertEqual(rects, [[300, 200, 100, 100]])
        self.assertAlmostEqual(share, 10000 / 120000)
        places = exclude.mask_places(rects)
        self.assertEqual(places[0]["rect"], [300, 200, 100, 100])
        self.assertEqual(places[0]["fill"], exclude.MASK_FILL)
        self.assertIsNone(places[0]["secret"])

    @unittest.skipUnless(sys.platform == "win32", "paints with GDI on Windows (off screen)")
    def test_the_recorder_paints_the_excluded_part_of_a_picture_grey(self):
        from twinlab2_recorder.desktop import pictures
        from twinlab2_recorder.desktop import recorder as desk

        w, h = 60, 40
        px = bytearray()
        for y in range(h):
            for x in range(w):
                # what an excluded window would show if a compositor put it into the picture: red
                px += bytes((0, 0, 255, 255)) if x >= 30 and y >= 20 else bytes((255, 255, 255, 255))
        rec = desk.Recorder.__new__(desk.Recorder)
        rec.pending_pictures, rec.secrets, rec.kept_texts = [], set(), set()
        rec.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, rec.out, True)
        raw = {"type": "Window", "name": "Orders", "hwnd": 1, "rect": [0, 0, w, h], "children": [],
               "_pixels": (w, h, bytes(px)), "_exclusion": [[30, 20, 30, 20]]}  # fmt: skip
        self.assertEqual(rec._picture(1, (raw, raw), 1), "pictures/0001.png")
        rec._render_pictures()
        pw, ph, rgb = pictures.read_png((rec.out / "pictures" / "0001.png").read_bytes())
        at = lambda x, y: tuple(rgb[(y * pw + x) * 3:(y * pw + x) * 3 + 3])  # noqa: E731
        self.assertEqual(at(45, 30), (128, 128, 128))  # the excluded part: grey, not red
        self.assertEqual(at(5, 5), (255, 255, 255))  # the rest as the window printed it
        skipped = dict(raw, _exclusion=None)
        self.assertIsNone(rec._picture(1, (skipped, skipped), 2))


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        targets = lv.Targets.from_list([{"origin": "https://erp.example.test"}],
                                       exclude=policy("mail.google.com", "https://erp.example.test/hr/"))  # fmt: skip
        self.session = ses.Session(self.tmp, targets, name="t")
        self.session.rows = []
        self.session._journal_row = self.session.rows.append

    def test_an_excluded_address_is_not_recorded_nor_reported_outside(self):
        outside = []
        steps = web._Steps(self.tmp, False, origins={"https://erp.example.test"},
                           outside=lambda o, ts: outside.append(o), exclude=self.session.excluded_url)  # fmt: skip
        self.addCleanup(steps.file.close)
        self.assertFalse(steps.recorded("https://mail.google.com/mail/u/0/#inbox"))
        self.assertFalse(steps.recorded("https://erp.example.test/hr/payslips/7"))
        self.assertTrue(steps.recorded("https://erp.example.test/orders/7"))
        self.assertFalse(steps.recorded("https://news.example.test/"))  # not a target: outside
        self.assertEqual(outside, ["https://news.example.test"])
        self.assertEqual([r["name"] for r in self.session.rows], ["mail.google.com", "https://erp.example.test/hr/"])

    def test_the_har_keeps_no_request_of_or_to_an_excluded_address(self):
        har = web._Har({"https://erp.example.test"}, exclude=self.session.excluded_url)

        def request(url, page):
            return types.SimpleNamespace(url=url, frame=types.SimpleNamespace(page=types.SimpleNamespace(url=page)))

        self.assertTrue(har._wanted(request("https://cdn.example.test/app.js", "https://erp.example.test/orders")))
        self.assertFalse(har._wanted(request("https://erp.example.test/api/hr/1", "https://erp.example.test/hr/1")))
        self.assertFalse(har._wanted(request("https://mail.google.com/sync", "https://erp.example.test/orders")))
        self.assertIs(self.session._web_journals()["exclude"].__func__, ses.Session.excluded_url)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"TWINLAB_RECORDER_POLICY": str(self.tmp / "no-policy.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_defaults_first_then_the_persons_own_list_is_kept(self):
        cfg = st.Settings(self.tmp)
        first = cfg.exclude_policy()
        self.assertEqual(first.rules, list(exclude.DEFAULTS))
        self.assertTrue({"WINWORD.EXE", "EXCEL.EXE", "OUTLOOK.EXE", "ms-teams.exe", "KeePass.exe"}
                        <= {r.pattern for r in first.rules})  # fmt: skip
        mine = [r for r in first.rules if r.pattern != "EXCEL.EXE"] + [exclude.parse_entry("title: Personal")]
        cfg.set_exclude_rules(mine)
        again = st.Settings(self.tmp).exclude_policy()
        self.assertEqual([r.pattern for r in again.rules], [r.pattern for r in mine])
        self.assertIsNone(again.process("EXCEL.EXE"))  # removed stays removed
        self.assertEqual(again.title("Personal budget").source, "user")
        restored = exclude.restore_defaults(again.rules)
        self.assertIsNotNone(exclude.Policy(restored).process("EXCEL.EXE"))
        cfg.set_exclude_rules([])
        self.assertEqual(st.Settings(self.tmp).exclude_policy().rules, [])  # an empty list is a choice too

    def test_a_managed_policy_locks_its_entries_and_can_lock_the_list(self):
        managed = self.tmp / "policy.json"
        managed.write_text(json.dumps({"exclude": [{"kind": "process", "pattern": "Slack.exe", "name": "Slack"}]}))
        cfg = st.Settings(self.tmp)
        pol = cfg.exclude_policy(managed)
        self.assertEqual(pol.rules[0].label, "Slack")
        self.assertTrue(pol.rules[0].locked and pol.managed and pol.allow_user_changes)
        cfg.set_exclude_rules(pol.rules)  # managed entries are never stored as the person's
        self.assertNotIn("Slack.exe", json.dumps(json.loads(cfg.path.read_text(encoding="utf-8"))))
        managed.write_text(json.dumps({"exclude": [], "allow_user_changes": False}))
        self.assertFalse(cfg.exclude_policy(managed).allow_user_changes)
        managed.write_text("{not json")
        broken = cfg.exclude_policy(managed)
        self.assertFalse(broken.allow_user_changes)  # an unreadable office policy locks the list
        self.assertTrue(broken.process("WINWORD.EXE"))  # and the person's own list still applies

    def test_a_session_never_applies_fewer_than_the_pcs_own_list(self):
        cfg = st.Settings(self.tmp)
        conf = agent_main.session_config([{"exe": "CaseDesk.exe"}], [], "s", exclude={"rules": []})
        got = exclude.Policy.from_session(agent_main.session_exclude(cfg, conf))
        self.assertIsNotNone(got.process("WINWORD.EXE"))
        conf = agent_main.session_config([], [], "s", exclude=policy("title: Personal").as_session())
        got = exclude.Policy.from_session(agent_main.session_exclude(cfg, conf))
        self.assertIsNotNone(got.title("Personal budget"))
        self.assertIsNotNone(got.process("EXCEL.EXE"))


class CompletenessTest(unittest.TestCase):
    def test_the_references_name_the_entries_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "s"
            shutil.copytree(FIXTURES / "workstation-session", folder, ignore=shutil.ignore_patterns("README.md"))
            meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            meta["local_policy"] = {"entries": 24, "managed": False}
            (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
            with (folder / "journal.jsonl").open("a", encoding="utf-8") as f:
                for name in ("Microsoft Word", "Microsoft Excel", "Microsoft Word"):
                    f.write(json.dumps({"kind": "excluded", "name": name}) + "\n")
            before = completeness.manifest(folder, dict(meta, local_policy=None))
            box = timeline.build(folder)
        local = box["completeness"]["local_exclusions"]
        self.assertEqual(local, {"count": 2, "names": ["Microsoft Excel", "Microsoft Word"], "entries": 24,
                                 "managed": False})  # fmt: skip
        self.assertEqual(box["completeness"]["overall"], before["overall"])  # not a gap
        self.assertEqual(box["completeness"]["channels"], before["channels"])
        self.assertEqual(json.dumps(box).count("Microsoft Word"), 1)
        self.assertEqual(box["contract_version"], contract.CONTRACT_VERSION)
        doc = dict(box, recorder={"name": "TwinLabRecorder", "version": "0.2.0"})
        self.assertEqual([e for e in contract.errors(doc, "references") if "local_exclusions" in e], [])

    def test_no_list_no_block(self):
        self.assertIsNone(exclude.summary([], 0))
        self.assertEqual(exclude.summary([], 3), {"count": 0, "names": [], "entries": 3, "managed": False})


if __name__ == "__main__":
    unittest.main()
