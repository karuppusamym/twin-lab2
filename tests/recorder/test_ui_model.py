"""What the recorder's window shows, without Tk (twinlab2_recorder.ui_model): the offered sessions
keep their selection, the upload queue's progress, retry and resume, the send review's summary,
and the recording indicator."""

import json
from pathlib import Path
import random
import tempfile
import time
import unittest

from twinlab2_recorder import ui_model as ui
from twinlab2_recorder.client import PortalError
from twinlab2_recorder.upload_queue import UploadQueue

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SESSION = "20261119T100000Z-0a1b2c3d"


class FakeListbox:
    """Tk's Listbox, as far as OfferedList uses it; counts the rows it rewrote."""

    def __init__(self):
        self.rows, self.sel, self.writes = [], set(), 0

    def size(self):
        return len(self.rows)

    def get(self, i):
        return self.rows[i]

    def insert(self, i, text):
        self.writes += 1
        if i == "end":
            self.rows.append(text)
        else:
            self.rows.insert(i, text)
            self.sel = {s + 1 if s >= i else s for s in self.sel}

    def delete(self, first, last=None):
        end = len(self.rows) if last == "end" else (first if last is None else last) + 1
        del self.rows[first:end]
        self.sel = {s if s < first else s - (end - first) for s in self.sel if not first <= s < end}

    def curselection(self):
        return tuple(sorted(self.sel))

    def selection_clear(self, first, last=None):
        self.sel.clear()

    def selection_set(self, i):
        self.sel.add(i)

    def see(self, i):
        pass


def offered(sid, name, *targets):
    return {"id": sid, "name": name, "automation_id": None, "targets": [{"name": t, "level": "full"} for t in targets]}


class OfferedListTest(unittest.TestCase):
    def test_the_selection_stays_on_its_session_across_polls(self):
        box, model = FakeListbox(), ui.OfferedList()
        model.sync(box, [offered(1, "Billing", "ERP"), offered(2, "Orders", "Shop"), offered(3, "Close", "Fin")])
        box.selection_set(1)
        self.assertEqual(model.selected(box)["id"], 2)
        writes = box.writes
        model.sync(box, [offered(1, "Billing", "ERP"), offered(2, "Orders", "Shop"), offered(3, "Close", "Fin")])
        self.assertEqual(box.writes, writes)  # nothing changed: nothing rewritten
        self.assertEqual(model.selected(box)["id"], 2)
        model.sync(box, [offered(2, "Orders", "Shop"), offered(3, "Close", "Fin")])  # the first one went
        self.assertEqual(model.selected(box)["id"], 2)
        self.assertEqual(box.curselection(), (0,))
        model.sync(box, [offered(4, "New", "CRM"), offered(2, "Orders", "Shop"), offered(3, "Close", "Fin")])
        self.assertEqual(model.selected(box)["id"], 2)

    def test_a_renamed_session_or_changed_targets_update_in_place(self):
        box, model = FakeListbox(), ui.OfferedList()
        model.sync(box, [offered(1, "Billing", "ERP"), offered(2, "Orders", "Shop")])
        box.selection_set(1)
        model.sync(box, [offered(1, "Billing", "ERP"), offered(2, "Orders (EU)", "Shop", "CaseDesk")])
        self.assertEqual(box.rows, ["Billing  ·  ERP", "Orders (EU)  ·  CaseDesk, Shop"])
        chosen = model.selected(box)  # what Start would record: the new targets
        self.assertEqual(chosen["name"], "Orders (EU)")
        self.assertEqual([t["name"] for t in chosen["targets"]], ["Shop", "CaseDesk"])

    def test_a_session_no_longer_offered_clears_the_selection(self):
        box, model = FakeListbox(), ui.OfferedList()
        model.sync(box, [offered(1, "Billing", "ERP")])
        box.selection_set(0)
        model.sync(box, [offered(2, "Orders", "Shop")])
        self.assertIsNone(model.selected(box))
        model.sync(box, [])
        self.assertEqual(box.rows, [])

    def test_catalog_check_boxes(self):
        a = [{"app_id": 1, "name": "ERP", "target": {"origin": "https://erp"}}]
        self.assertEqual(ui.app_choices(a, [dict(a[0])]), "keep")
        self.assertEqual(ui.app_choices(a, [dict(a[0], name="ERP 2")]), "relabel")
        self.assertEqual(ui.app_choices(a, [dict(a[0], target={"origin": "https://erp2"})]), "relabel")
        self.assertEqual(ui.app_choices(a, a + [{"app_id": 2, "name": "B", "target": {}}]), "rebuild")


DEST = {"portal": "https://portal.test", "machine_id": 1}


class FakeUploads:
    """The portal's resumable uploads (portal mode), in memory; `down` makes it unreachable."""

    def __init__(self, chunk=1024):
        self.chunk, self.uploads, self.down, self.puts = chunk, {}, False, 0

    def _check(self):
        if self.down:
            raise PortalError("the portal cannot be reached: network down")

    def upload_init(self, session_id, size, sha256, content_type="application/zip"):
        self._check()
        up = self.uploads.setdefault(sha256, {"id": "up-" + sha256[:8], "size": size, "data": b"", "state": "open"})
        return {"upload_id": up["id"], "upload_token": "tok", "expires_at": "2999-01-01T00:00:00+00:00",
                "mode": "portal", "chunk_size": self.chunk, "received_bytes": len(up["data"]), "state": up["state"]}

    def _get(self, upload_id):
        return next(u for u in self.uploads.values() if u["id"] == upload_id)

    def upload_status(self, upload_id, token):
        self._check()
        up = self._get(upload_id)
        return {"state": up["state"], "received_bytes": len(up["data"])}

    def upload_chunk(self, upload_id, token, offset, data):
        self._check()
        up = self._get(upload_id)
        up["data"] = up["data"][:offset] + data
        self.puts += 1
        return {"received_bytes": len(up["data"])}

    def upload_complete(self, upload_id, token):
        self._check()
        up = self._get(upload_id)
        up["state"] = "confirmed"
        return {"state": "confirmed", "recording_id": 42}


class UploadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.session = self.tmp / "recordings" / SESSION
        self.session.mkdir(parents=True)
        (self.session / "session.json").write_text(json.dumps({"id": SESSION}), encoding="utf-8")
        (self.session / "big.bin").write_bytes(random.Random(1).randbytes(10_000))  # 10 chunks: not compressible
        self.portal = FakeUploads()
        self.published = []
        self.clock = [1000.0]

    def worker(self, make_portal=None):
        w = ui.UploadWorker(lambda on_change: UploadQueue(self.tmp / "data", clock=lambda: self.clock[0],
                                                          on_change=on_change, destination=DEST),
                            make_portal or (lambda: self.portal), lambda fn, *a: fn(*a),
                            lambda jobs, active, error: self.published.append((jobs, active, error)),
                            clock=lambda: self.clock[0])  # fmt: skip
        w.queue = w.make_queue(w._changed)
        return w

    def test_progress_is_published_per_chunk_and_the_job_ends_done(self):
        w = self.worker()
        w.step(("add", self.session, SESSION))
        sending = [ui.upload_row(j[0], 0, a) for j, a, _ in self.published if j and a]
        self.assertGreater(len(sending), 3)
        percents = [r["percent"] for r in sending if r["state"] == "sending"]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(any(0 < p < 100 for p in percents))
        jobs, active, error = self.published[-1]
        row = ui.upload_row(jobs[0], 0, active)
        self.assertEqual((row["state"], row["percent"], error), ("done", 100, None))

    def test_offline_then_retry_resumes_where_it_stopped(self):
        w = self.worker()
        sent, lost = [], []
        real = self.portal.upload_chunk

        def flaky(*a):
            if len(sent) == 3 and not lost:
                lost.append(1)
                self.portal.down = True
            self.portal._check()
            sent.append(a[2])
            return real(*a)

        self.portal.upload_chunk = flaky
        w.step(("add", self.session, SESSION))
        job = w.queue.jobs()[0]
        row = ui.upload_row(job, self.clock[0], None)
        self.assertEqual(row["state"], "retrying")
        self.assertIn("network down", row["detail"])
        self.assertIn("next try in", row["detail"])
        self.assertTrue(0 < row["percent"] < 100)
        self.portal.down = False
        w.step(("retry", job["id"]))  # the person presses Retry: due now
        self.assertEqual(w.queue.jobs()[0]["state"], "confirmed")
        self.assertEqual(sent[3], 3 * 1024)  # resumed at the chunk it lost, not from the start

    def test_a_restart_resumes_the_queue(self):
        w = self.worker()
        self.portal.down = True
        w.step(("add", self.session, SESSION))
        self.assertEqual(w.queue.jobs()[0]["state"], "queued")
        self.portal.down = False
        self.clock[0] += 3600  # the PC was off; the recorder starts again
        again = self.worker()
        again.step(("run", False))
        self.assertEqual(again.queue.jobs()[0]["state"], "confirmed")

    def test_a_failed_job_can_be_retried(self):
        w = self.worker()
        w.queue.add_session(self.session, SESSION)
        job = w.queue.jobs()[0]
        w.queue._fail(job, "the portal answered 403")
        self.assertEqual(ui.upload_row(w.queue.jobs()[0], 0)["state"], "failed")
        self.assertTrue(ui.upload_row(w.queue.jobs()[0], 0)["can_retry"])
        w.step(("retry", job["id"]))
        self.assertEqual(w.queue.jobs()[0]["state"], "confirmed")

    def test_not_paired_is_shown_without_losing_the_job(self):
        def not_paired():
            raise PortalError("this machine is not paired: run `TwinLabRecorder pair` first")

        w = self.worker(not_paired)
        w.step(("add", self.session, SESSION))
        jobs, _active, error = self.published[-1]
        self.assertIn("not paired", error)
        self.assertEqual(jobs[0]["state"], "queued")

    def test_the_worker_thread_resumes_and_stops(self):
        w = self.worker()
        w.queue.add_session(self.session, SESSION)  # queued by an earlier run
        w.queue = None
        w.start()
        end = time.monotonic() + 10
        while time.monotonic() < end and not (self.published and self.published[-1][0]
                                              and self.published[-1][0][0]["state"] == "confirmed"):  # fmt: skip
            time.sleep(0.02)
        w.stop()
        w.thread.join(5)
        self.assertEqual(self.published[-1][0][0]["state"], "confirmed")
        self.assertFalse(w.thread.is_alive())


class ReviewTest(unittest.TestCase):
    def test_summary_names_apps_evidence_withheld_and_destination(self):
        box = json.loads((FIXTURES / "references-ten-apps.json").read_text(encoding="utf-8"))
        text = ui.review_summary(box, ["web: a note"], "https://twinlab2.apps.example.test", "Workstation 7")
        self.assertIn("Destination: https://twinlab2.apps.example.test  (as Workstation 7)", text)
        self.assertIn(f"Applications ({len(box['apps'])}):", text)
        steps = sum(len(v) for v in box["flows"].values())
        self.assertIn(f"• steps: {steps} (", text)
        self.assertIn("screen structure:", text)
        self.assertIn("Withheld (stays on this PC):", text)
        self.assertIn("other applications: used", text)
        self.assertIn("steps-only applications", text)  # the mainframe app is steps only
        self.assertIn("checks before recording:", text)
        self.assertIn("• web: a note", text)
        self.assertNotIn('"flows"', text)  # the JSON is under Details, not here
        self.assertLess(text.index("Destination"), text.index("Applications"))

    def test_nothing_recorded_and_not_paired(self):
        box = {"session": {"id": "x", "name": "n", "targets": [{"exe": "a.exe"}]}, "apps": [], "flows": {},
               "timeline": [{"app": "presence", "steps": 4}]}
        text = ui.review_summary(box)
        self.assertIn("not paired", text)
        self.assertIn("none: no target application was recorded", text)
        self.assertIn("used 4 time(s), not named", text)
        self.assertIn("1 target(s) with nothing recorded", text)


class StatusTest(unittest.TestCase):
    def test_indicator_elapsed_targets_and_channel_health(self):
        rec = ui.RecordingStatus("Month-end", [{"origin": "https://erp"}, {"exe": "billing.exe"}], started=100)
        self.assertEqual(rec.lines(100)[:3], ["● RECORDING  00:00:00  Month-end",
                                              "Targets: billing.exe, https://erp", "Channels: starting…"])  # fmt: skip
        self.assertEqual(rec.health(), "starting")
        for row in ({"kind": "channel", "channel": "web", "event": "started"},
                    {"kind": "channel", "channel": "web", "event": "ready"},
                    {"kind": "channel", "channel": "desktop", "event": "ready"}, {"kind": "browser"}):
            rec.feed(row)
        self.assertEqual(rec.health(), "ok")
        lines = rec.lines(100 + 3725)
        self.assertEqual(lines[0], "● RECORDING  01:02:05  Month-end")
        self.assertEqual(lines[2], "Channels: desktop ok · web ok")
        self.assertIn("The browser is ready: start the process now.", lines)
        rec.feed({"kind": "channel", "channel": "desktop", "event": "failed", "reason": "no heartbeat for 10 s"})
        rec.feed({"kind": "channel", "channel": "desktop", "event": "restarted"})
        self.assertEqual(rec.health(), "trouble")
        rec.feed({"kind": "channel", "channel": "desktop", "event": "ready"})
        rec.feed({"kind": "channel", "channel": "web", "event": "gap", "reason": "x"})
        self.assertEqual(rec.lines(200)[2], "Channels: desktop ok (restarted 1×) · web ok (1 gap)")
        rec.feed({"kind": "channel", "channel": "web", "event": "cut_off", "reason": "did not stop in 10 s"})
        self.assertIn("web cut off (1 gap): did not stop in 10 s", rec.lines(200)[2])
        rec.stopping = True
        self.assertTrue(rec.lines(200)[0].startswith("■ STOPPING"))

    def test_journal_tail_reads_only_new_complete_lines(self):
        path = Path(tempfile.mkdtemp()) / "journal.jsonl"
        tail = ui.JournalTail(path)
        self.assertEqual(tail.read(), [])  # not there yet
        with path.open("ab") as f:
            f.write(b'{"kind": "app"}\n{"kind": "chan')
        self.assertEqual(tail.read(), [{"kind": "app"}])
        with path.open("ab") as f:
            f.write(b'nel", "channel": "web", "event": "ready"}\nnot json\n')
        self.assertEqual(tail.read(), [{"kind": "channel", "channel": "web", "event": "ready"}])
        self.assertEqual(tail.read(), [])

    def test_journal_watcher_posts_rows_from_its_thread(self):
        path = Path(tempfile.mkdtemp()) / "journal.jsonl"
        got = []
        watcher = ui.JournalWatcher(path, lambda fn, *a: fn(*a), got.extend, interval=0.02).start()
        path.write_text('{"kind": "browser"}\n', encoding="utf-8")
        end = time.monotonic() + 5
        while not got and time.monotonic() < end:
            time.sleep(0.02)
        watcher.stop()
        self.assertEqual(got, [{"kind": "browser"}])

    def test_close_messages(self):
        self.assertIsNone(ui.close_message(0, 120))
        self.assertIn("did not finish within 120 seconds", ui.close_message(None, 120))
        self.assertIn("recover", ui.close_message(None, 120))
        self.assertIn("exit code 3", ui.close_message(3, 120))


if __name__ == "__main__":
    unittest.main()
