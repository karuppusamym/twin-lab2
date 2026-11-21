"""The recorder's window never waits on its own thread, and its deferred callbacks keep their
values (twinlab2_recorder.uithread). Headless: no Tk, no window."""

import ast
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from twinlab2_recorder import uithread
from twinlab2_recorder.client import Portal, PortalError
from twinlab2_recorder.uithread import Dispatcher, Jobs, UIThreadViolation

RECORDER = Path(__file__).resolve().parents[2] / "recorder" / "twinlab2_recorder"


def except_name_closures(source: str, filename: str = "<source>") -> list[str]:
    """Lambdas and nested functions inside an `except ... as NAME` handler that use NAME. Python
    deletes NAME when the handler ends, so such a callable run later (root.after, a thread, a
    queue) raises NameError: the recorder's 'portal offline' status never showed for that."""
    found = []
    for handler in ast.walk(ast.parse(source, filename)):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for stmt in handler.body:
            for node in ast.walk(stmt):
                if not isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
                if handler.name in params:
                    continue
                inner = node.body if isinstance(node.body, list) else [node.body]
                if any(isinstance(n, ast.Name) and n.id == handler.name and isinstance(n.ctx, ast.Load)
                       for part in inner for n in ast.walk(part)):  # fmt: skip
                    found.append(f"{filename}:{node.lineno} uses `{handler.name}` of the except at line "
                                 f"{handler.lineno} after the handler ends")  # fmt: skip
    return found


# The code as it was (main.py, poll_portal), kept here so the check is shown to catch it.
OLD_POLL = '''
def poll_portal():
    def work():
        try:
            p = _portal(cfg, ca_bundle)
            offered, apps = p.sessions(), p.apps()
            root.after(0, lambda: apply(offered, apps, None))
        except PortalError as exc:
            root.after(0, lambda: apply(None, None, str(exc)))
'''


class DeferredCallbackTest(unittest.TestCase):
    def test_the_old_pattern_loses_the_exception_when_the_callback_runs_later(self):
        queued, shown = [], []

        def poll():  # the old poll_portal's worker, with root.after(0, f) as "queue f"
            try:
                raise PortalError("the portal cannot be reached: refused")
            except PortalError as exc:
                queued.append(lambda: shown.append(str(exc)))

        poll()  # the handler has returned; now the window runs what was queued
        with self.assertRaises(NameError):
            queued[0]()
        self.assertEqual(except_name_closures(OLD_POLL), [
            "<source>:9 uses `exc` of the except at line 8 after the handler ends"])

    def test_no_callback_in_the_recorder_uses_a_finished_except_name(self):
        found = []
        for path in sorted(RECORDER.rglob("*.py")):
            found += except_name_closures(path.read_text(encoding="utf-8"), str(path.relative_to(RECORDER)))
        self.assertEqual(found, [])

    def test_a_failed_poll_reaches_the_window_after_the_worker_is_done(self):
        dispatcher = Dispatcher()
        jobs = Jobs(dispatcher)
        shown = []

        def work(_report):
            raise PortalError("the portal cannot be reached: [WinError 10061] refused")

        jobs.run("poll", work, on_failed=lambda reason: shown.append(reason))
        jobs.join()  # the worker and its except handler are long finished...
        dispatcher.drain()  # ...when the window runs the callback
        self.assertEqual(shown, ["the portal cannot be reached: [WinError 10061] refused"])


class GuardTest(unittest.TestCase):
    def setUp(self):
        uithread.mark_ui_thread()
        self.addCleanup(uithread.clear_ui_thread)

    def test_a_portal_call_on_the_window_thread_fails_before_any_network(self):
        portal = Portal("http://127.0.0.1:9", token="t", timeout=0.5)
        for call in (portal.sessions, portal.apps, lambda: portal.send({}), lambda: portal.pair("c", "n"),
                     lambda: portal.upload_status("u", "t"), lambda: portal.put_part("http://127.0.0.1:9/x", b"")):
            with self.assertRaises(UIThreadViolation):
                call()

    def test_the_same_call_on_a_worker_runs_and_reports_the_portal_offline(self):
        portal = Portal("http://127.0.0.1:9", token="t", timeout=0.5)
        got = []

        def work():
            try:
                portal.sessions()
            except PortalError as exc:
                got.append(exc.unreachable)

        t = threading.Thread(target=work)
        t.start()
        t.join(10)
        self.assertEqual(got, [True])

    def test_waiting_for_a_process_on_the_window_thread_fails(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(proc.wait)
        with self.assertRaises(UIThreadViolation):
            uithread.wait_process(proc, 120)

    def test_the_command_line_marks_no_thread(self):
        uithread.clear_ui_thread()
        uithread.assert_off_ui_thread("anything")  # no window: nothing to protect


class JobsTest(unittest.TestCase):
    def run_job(self, work, **kw):
        dispatcher, states, ends = Dispatcher(), [], []
        jobs = Jobs(dispatcher, lambda job: states.append((job.state, job.reason, job.attempt)), sleep=lambda s: None)
        jobs.run("send", work, on_done=lambda r: ends.append(("done", r)),
                 on_failed=lambda r: ends.append(("failed", r)), **kw)  # fmt: skip
        jobs.join()
        dispatcher.drain()
        return states, ends

    def test_states_from_sending_to_done(self):
        states, ends = self.run_job(lambda _r: {"recording_id": 7}, state="sending")
        self.assertEqual([s[0] for s in states], ["sending", "done"])
        self.assertEqual(ends, [("done", {"recording_id": 7})])

    def test_an_unreachable_portal_is_retried_then_fails_with_the_reason(self):
        calls = []

        def work(_r):
            calls.append(1)
            raise PortalError("the portal cannot be reached: timed out")

        states, ends = self.run_job(work, state="sending", retries=2)
        self.assertEqual(len(calls), 3)
        self.assertEqual([s[0] for s in states], ["sending", "retrying", "sending", "retrying", "sending", "failed"])
        self.assertEqual(states[1], ("retrying", "the portal cannot be reached: timed out", 2))
        self.assertEqual(ends, [("failed", "the portal cannot be reached: timed out")])

    def test_a_refusal_is_not_retried(self):
        def work(_r):
            raise PortalError("this machine is not paired, or was revoked: pair it again", 401)

        states, ends = self.run_job(work, retries=3)
        self.assertEqual([s[0] for s in states], ["connecting", "failed"])
        self.assertEqual(ends[0][0], "failed")

    def test_a_missing_outbox_fails_the_job_instead_of_killing_the_thread(self):
        def work(_r):
            raise SystemExit("no references for session x")

        _states, ends = self.run_job(work)
        self.assertEqual(ends, [("failed", "no references for session x")])

    def test_a_retry_after_a_transient_failure_succeeds(self):
        calls = []

        def work(_r):
            calls.append(1)
            if len(calls) == 1:
                raise PortalError("the portal cannot be reached: reset")
            return "ok"

        states, ends = self.run_job(work, retries=1)
        self.assertEqual([s[0] for s in states], ["connecting", "retrying", "connecting", "done"])
        self.assertEqual(ends, [("done", "ok")])

    def test_describe(self):
        job = uithread.Job(1, "send", "retrying", "references of Month-end", "timed out", 2)
        self.assertEqual(job.describe(), "Retrying: references of Month-end (attempt 2) — timed out")


# --- the window's code: nothing slow outside a background job ----------------------------------------

SLOW_NAMES = {"send", "_portal", "preflight_checks", "wait_process", "sleep", "Portal", "make_portal"}
SLOW_ATTRS = {"pair", "sessions", "apps", "send", "wait", "join", "sleep", "run", "upload_init", "add_session",
              "make_portal", "_portal", "preflight_checks"}
BACKGROUND = {"run", "Thread", "UploadWorker"}  # jobs.run(...), threading.Thread(target=...), ui.UploadWorker(...)


def slow_calls_on_the_window_thread(source: str) -> list[str]:
    """Calls in app.RecorderApp (the window's code) that reach the portal or wait, but are not
    inside a callable handed to a background job (jobs.run, a Thread's target, the upload worker)."""
    tree = ast.parse(source)
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RecorderApp")
    parent = {}
    for node in ast.walk(window):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    background_defs, background_nodes = set(), set()
    for node in ast.walk(window):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in BACKGROUND:
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(arg, ast.Lambda):
                        background_nodes.add(arg)
                    elif isinstance(arg, ast.Name):
                        background_defs.add(arg.id)
    for node in ast.walk(window):
        if isinstance(node, ast.FunctionDef) and node.name in background_defs:
            background_nodes.add(node)
    out = []
    for node in ast.walk(window):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        slow = (isinstance(fn, ast.Name) and fn.id in SLOW_NAMES) or (
            isinstance(fn, ast.Attribute) and fn.attr in SLOW_ATTRS
            and not (fn.attr == "run" and "jobs" in (getattr(fn.value, "id", None), getattr(fn.value, "attr", None)))
            and not isinstance(fn.value, ast.Constant))  # fmt: skip - a string's join is no thread's
        if not slow:
            continue
        up = node
        while up in parent and up not in background_nodes:
            up = parent[up]
        if up not in background_nodes:
            out.append(f"app.py:{node.lineno} {ast.unparse(node)[:80]}")
    return out


class WindowStructureTest(unittest.TestCase):
    def test_the_window_runs_portal_calls_and_waits_only_in_background_jobs(self):
        self.assertEqual(slow_calls_on_the_window_thread((RECORDER / "app.py").read_text(encoding="utf-8")), [])

    def test_the_check_catches_a_send_and_a_wait_on_the_window_thread(self):
        old = '''
class RecorderApp:
    def on_send(self):
        result = self.m.send(self.cfg, s["id"], self.ca_bundle)
    def on_close(self):
        self.state["proc"].wait(120)
    def ok(self):
        self.jobs.run("send", lambda _r: self.m.send(self.cfg, sid, self.ca_bundle))
'''
        self.assertEqual(slow_calls_on_the_window_thread(old), [
            "app.py:4 self.m.send(self.cfg, s['id'], self.ca_bundle)", "app.py:6 self.state['proc'].wait(120)"])


if __name__ == "__main__":
    unittest.main()
