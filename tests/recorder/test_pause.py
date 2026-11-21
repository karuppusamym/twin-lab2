"""Pausing a recording (twinlab2_recorder.workstation.pause): while the session folder holds the
file `paused`, no collector records anything, the journal says when, and the references carry the
periods (recorder contract 1.7 `completeness.paused`).

Plain Python on fakes: no window is opened or read, no browser or mainframe is reached."""

import json
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from twinlab2_recorder import main as agent_main
from twinlab2_recorder.web import recorder as web
from twinlab2_recorder.workstation import completeness
from twinlab2_recorder.workstation import levels as lv
from twinlab2_recorder.workstation import pause
from twinlab2_recorder.workstation import session as ses
from twinlab2_recorder.workstation import tn3270

from test_exclude import WINDOWS, FakeWin


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class FlagTest(Folder):
    def test_the_flag_follows_the_file(self):
        flag = pause.PauseFlag(self.tmp, start=False)
        heard = []
        flag.on_change(heard.append)
        self.assertFalse(flag())
        (self.tmp / pause.FILE).touch()
        flag.check()
        self.assertTrue(flag())
        (self.tmp / pause.FILE).unlink()
        flag.check()
        self.assertEqual(heard, [True, False])

    def test_the_flag_reads_on_its_own_thread(self):
        flag = pause.PauseFlag(self.tmp, interval=0.02)
        self.addCleanup(flag.stop)
        (self.tmp / pause.FILE).touch()
        end = time.monotonic() + 3
        while not flag() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(flag())

    def test_periods_rows_and_the_summary(self):
        rows = []
        p = pause.Pauses(rows.append)
        p.changed(True)
        p.changed(True)  # still the same pause
        p.changed(False)
        p.changed(True)
        periods = p.close()  # stopped while paused: the pause ends now
        self.assertEqual(
            [r["kind"] for r in rows], ["pause", "resume", "pause", "resume"]
        )
        self.assertEqual(len(periods), 2)
        self.assertTrue(all(x["from"] and x["to"] for x in periods))
        got = pause.summary([{"from": "2026-11-21T10:00:00Z", "to": "2026-11-21T10:02:30Z"},
                             {"from": "2026-11-21T10:05:00Z", "to": "2026-11-21T10:05:10Z"}])  # fmt: skip
        self.assertEqual((got["count"], got["seconds"]), (2, 160.0))
        self.assertEqual(got["periods"][0]["seconds"], 150.0)
        self.assertIsNone(pause.summary([]))

    def test_periods_from_a_journal_left_open(self):
        rows = [{"kind": "pause", "ts": "2026-11-21T10:00:00Z"}, {"kind": "resume", "ts": "2026-11-21T10:01:00Z"},
                {"kind": "pause", "ts": "2026-11-21T10:02:00Z"}, {"kind": "note", "ts": "2026-11-21T10:03:00Z"}]  # fmt: skip
        self.assertEqual(pause.paused_periods(rows), [
            {"from": "2026-11-21T10:00:00Z", "to": "2026-11-21T10:01:00Z"},
            {"from": "2026-11-21T10:02:00Z", "to": "2026-11-21T10:03:00Z"}])  # fmt: skip


class Paused:
    """A pause switch for the collectors."""

    def __init__(self):
        self.on = False

    def __call__(self):
        return self.on


class MainframeTest(Folder):
    def test_no_step_screen_or_typed_value_while_paused(self):
        paused = Paused()
        rec = tn3270.Recorder(self.tmp, "mainframe:host:23", paused=paused)
        self.addCleanup(rec.close)
        conn = types.SimpleNamespace(number=1, screen=tn3270.Screen())
        field = {"start": 0, "len": 8, "hidden": False, "protected": False}
        with mock.patch.object(rec, "_screen_info", lambda c: {"title": "MENU"}), \
                mock.patch.object(rec, "_label", lambda c, f: "User"):  # fmt: skip
            paused.on = True
            rec.screen_step(conn)
            rec.field_step(conn, field, 0, "jdoe1234")
            rec.aid_step(conn, 0x7D, None)
            self.assertEqual(rec.seq, 0)
            self.assertEqual(rec.typed, set())
            self.assertEqual(list((self.tmp / "screens").iterdir()), [])
            paused.on = False
            rec.aid_step(conn, 0x7D, None)
        steps = [
            json.loads(line)
            for line in (self.tmp / "steps.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([s["action"] for s in steps], ["aid"])


class WebTest(Folder):
    def test_no_step_or_network_entry_while_paused_and_nothing_reported_outside(self):
        paused, outside = Paused(), []
        steps = web._Steps(
            self.tmp,
            False,
            origins={"https://erp.example.test"},
            outside=lambda o, ts: outside.append(o),
        )
        self.addCleanup(steps.file.close)
        har = web._Har({"https://erp.example.test"})
        steps.paused = har.paused = paused
        request = types.SimpleNamespace(url="https://erp.example.test/api/1",
                                        frame=types.SimpleNamespace(page=types.SimpleNamespace(url="https://erp.example.test/")))  # fmt: skip
        self.assertTrue(steps.recorded("https://erp.example.test/orders"))
        self.assertTrue(har._wanted(request))
        paused.on = True
        self.assertFalse(steps.recorded("https://erp.example.test/orders"))
        self.assertFalse(steps.recorded("https://news.example.test/"))
        self.assertFalse(har._wanted(request))
        self.assertEqual(outside, [])  # paused: not even another site is reported

    def test_the_session_hands_the_switch_to_every_collector(self):
        targets = lv.Targets.from_list([{"origin": "https://erp.example.test"}])
        s = ses.Session(self.tmp, targets, name="t")
        self.assertIs(s._web_journals()["paused"].__func__, ses.Session.paused)
        self.assertIs(s.desktop_exclusions()["paused"].__func__, ses.Session.paused)
        self.assertFalse(s.paused())


class DesktopTest(unittest.TestCase):
    def recorder(self, paused):
        from twinlab2_recorder.desktop.recorder import Recorder

        rec = Recorder.__new__(Recorder)  # no Windows needed: only the pause logic
        rec.paused_fn, rec.q = paused, queue.Queue()
        rec.pids, rec.admit, rec.refused = {42}, None, set()
        return rec

    def test_the_hooks_and_events_take_nothing_while_paused(self):
        paused = Paused()
        rec = self.recorder(paused)
        self.assertTrue(rec._wanted_now(42))
        paused.on = True
        self.assertFalse(rec._wanted_now(42))
        self.assertFalse(rec._ours(types.SimpleNamespace(CurrentProcessId=42)))
        rec.q.put({"kind": "key", "key": "Ctrl+S", "t": 1.0})
        rec._discard()
        self.assertTrue(rec.q.empty())


class WatcherTest(Folder):
    def test_no_presence_row_while_paused(self):
        win = FakeWin(WINDOWS)
        import twinlab2_recorder.desktop as desktop

        for patcher in (mock.patch.dict(sys.modules, {"twinlab2_recorder.desktop.win": win}),
                        mock.patch.object(desktop, "win", win, create=True),
                        mock.patch.object(ses, "_window_pids", lambda _w: set()),
                        mock.patch.object(ses, "_elevated", lambda _pid: None)):  # fmt: skip
            patcher.start()
            self.addCleanup(patcher.stop)
        targets = lv.Targets.from_list([{"exe": "CaseDesk.exe", "name": "CaseDesk"}])
        s = ses.Session(self.tmp, targets, name="t")
        s.rows = []
        s._journal_row = s.rows.append
        s.pause_flag = paused = Paused()
        paused.on = True
        win.fg = [2, 2]

        def sleep(_s):  # paused: one round, then the session stops
            s.stop()

        with mock.patch.object(ses.time, "sleep", sleep):
            s._watch()
        self.assertEqual(s.rows, [])
        self.assertEqual(win.fg, [2, 2])  # the window in front was not even read
        paused.on = False
        s._stop.clear()
        with mock.patch.object(
            ses.time, "sleep", lambda _s: None if win.fg else s.stop()
        ):
            s._watch()
        self.assertEqual(
            [r["exe"] for r in s.rows if r.get("kind") == "presence"], ["notepad.exe"]
        )


class SessionRunTest(Folder):
    def test_a_session_paused_and_resumed_journals_it_and_the_references_carry_it(self):
        targets = lv.Targets.from_list([])

        def script(s):
            (s.folder / pause.FILE).touch()
            time.sleep(0.6)
            self.assertTrue(s.paused())
            (s.folder / pause.FILE).unlink()
            time.sleep(0.6)
            (s.folder / pause.FILE).touch()  # stopped while paused
            time.sleep(0.6)

        with mock.patch.dict("os.environ", {"TWINLAB_COLLECTORS": "threads"}), \
                mock.patch.object(ses.Session, "_watch", lambda self: None):  # fmt: skip
            s = ses.Session(self.tmp, targets, name="paused", robot=False)
            folder = s.run(script=script)
        rows = [
            json.loads(x)
            for x in (folder / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        kinds = [r.get("kind") for r in rows if r.get("kind") in ("pause", "resume")]
        self.assertEqual(kinds, ["pause", "resume", "pause", "resume"])
        meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(len(meta["pauses"]), 2)
        self.assertFalse((folder / pause.FILE).exists())
        block = completeness.manifest(folder, meta)["paused"]
        self.assertEqual(block["count"], 2)
        self.assertGreater(block["seconds"], 0.5)
        box = json.loads((folder / "portal-outbox.json").read_text(encoding="utf-8"))
        self.assertEqual(box["contract_version"], "1.8.0")
        self.assertEqual(box["completeness"]["paused"]["count"], 2)
        doc = agent_main.contract_references(
            box
        )  # checked against the 1.7 schema before sending
        self.assertEqual(doc["completeness"]["paused"]["count"], 2)

    def test_a_recovered_session_reads_its_pauses_from_the_journal(self):
        folder = self.tmp / "s"
        folder.mkdir()
        rows = [
            {"kind": "pause", "ts": "2026-11-21T10:00:00Z"},
            {"kind": "resume", "ts": "2026-11-21T10:00:30Z"},
        ]
        (folder / "journal.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        self.assertEqual(completeness.paused_summary(folder, {})["seconds"], 30.0)


if __name__ == "__main__":
    unittest.main()
