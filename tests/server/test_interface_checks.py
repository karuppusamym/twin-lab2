"""Verify's checks of the contracts between applications, against real local servers: a producer
(JSON, CSV and a POST route), a consumer whose page shows a failure message while the producer's
lab agent holds the peer-down condition, and a fake lab agent. Nothing is `pass` that was not
read; what cannot be read stays `not-checked` with the reason."""

import json
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from twinlab2 import interfaces

STATE = {"down": False, "posts": 0, "condition": {}}


class Producer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/orders":
            self.reply(200, json.dumps([{"number": 1, "total": 5.0, "customer": "x"}]))
        elif self.path == "/export.csv":
            self.reply(200, "number,total\n1,5\n", "text/csv")
        elif self.path == "/secure":
            self.reply(401, "{}")
        else:
            self.reply(404, "{}")

    def do_POST(self):
        STATE["posts"] += 1
        self.reply(200, "{}")


class Consumer(Producer):
    def do_GET(self):
        if self.path == "/lookup":
            self.reply(200, "Orders is unavailable, try again" if STATE["down"] else "Order found", "text/html")
        else:
            self.reply(404, "{}")


class LabAgent(Producer):
    def do_GET(self):
        self.reply(200, json.dumps(STATE["condition"]))

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        STATE["condition"] = body
        STATE["down"] = bool(body.get("faults"))
        self.reply(200, json.dumps(body))


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = [serve(h) for h in (Producer, Consumer, LabAgent)]
        (_a, cls.producer), (_b, cls.consumer), (_c, cls.lab) = cls.servers
        cls.db = sqlite3.connect(":memory:")
        cls.db.row_factory = sqlite3.Row
        cls.db.executescript(
            "CREATE TABLE apps(id INTEGER, lab_url TEXT); CREATE TABLE releases(id INTEGER, app_id INTEGER);"
            "CREATE TABLE deployments(release_id INTEGER, url TEXT, verification TEXT);")
        cls.db.execute("INSERT INTO apps VALUES (1, ?), (2, '')", (cls.lab,))
        cls.db.execute("INSERT INTO releases VALUES (10, 1), (20, 2)")
        cls.db.execute("INSERT INTO deployments VALUES (10, ?, 'verified'), (20, ?, 'verified')",
                       (cls.producer, cls.consumer))

    @classmethod
    def tearDownClass(cls):
        for server, _url in cls.servers:
            server.shutdown()

    def manifest(self, *relationships):
        return {"interfaces": [{"apps": [{"id": 1, "name": "Orders"}, {"id": 2, "name": "Billing"}],
                                "identifiers": [], "launch_order": [1, 2],
                                "relationships": [dict({"producer": 1, "consumer": 2}, **r) for r in relationships]}]}

    def run_checks(self, *relationships):
        STATE.update(down=False, posts=0, condition={})
        return interfaces.verify(self.db, self.manifest(*relationships), 2, lambda url: "a" * 40)

    def by(self, checks, prefix):
        return next(c for c in checks if c["what"].startswith(prefix))

    def test_a_get_api_is_read_and_its_response_fields_compared(self):
        ok = {"kind": "api", "detail": {"method": "GET", "path": "/api/orders", "response": ["number", "total"]}}
        self.assertEqual(self.by(self.run_checks(ok), "api schema")["status"], "pass")
        bad = {"kind": "api", "detail": {"method": "GET", "path": "/api/orders", "response": ["number", "status"]}}
        got = self.by(self.run_checks(bad), "api schema")
        self.assertEqual(got["status"], "fail")
        self.assertIn("status", got["notes"])

    def test_a_call_that_changes_data_is_never_sent(self):
        post = {"kind": "api", "detail": {"method": "POST", "path": "/api/orders", "response": ["number"]}}
        got = self.by(self.run_checks(post), "api schema")
        self.assertEqual((got["status"], STATE["posts"]), ("not-checked", 0))
        self.assertIn("changes the producer's data", got["notes"])

    def test_a_page_behind_sign_in_or_a_record_path_is_not_checked(self):
        for path, why in (("/secure", "401"), ("/api/orders/17", "one record")):
            rel = {"kind": "api", "detail": {"method": "GET", "path": path, "response": ["number"]}}
            got = self.by(self.run_checks(rel), "api schema")
            self.assertEqual(got["status"], "not-checked")
            self.assertIn(why, got["notes"])

    def test_a_file_with_an_address_has_its_columns_compared(self):
        ok = self.by(self.run_checks({"kind": "file", "detail": {"path": "/export.csv", "columns": ["number", "total"]}}),
                     "file schema")
        self.assertEqual(ok["status"], "pass")
        bad = self.by(self.run_checks({"kind": "file", "detail": {"path": "/export.csv", "columns": ["number", "tax"]}}),
                      "file schema")
        self.assertEqual(bad["status"], "fail")
        none = self.by(self.run_checks({"kind": "file", "detail": {"columns": ["number"]}}), "file schema")
        self.assertEqual(none["status"], "not-checked")

    def test_peer_down_stops_the_peer_reads_the_message_and_puts_it_back(self):
        rel = {"kind": "identifier", "when_down": "Billing says Orders is unavailable",
               "down_check": {"path": "/lookup", "contains": "unavailable"}}
        got = self.by(self.run_checks(rel), "when Orders is down")
        self.assertEqual(got["status"], "pass", got)
        self.assertFalse(STATE["down"], "the peer is up again")
        self.assertEqual(STATE["condition"], {})

    def test_a_wrong_message_fails_and_the_peer_is_still_put_back(self):
        rel = {"kind": "identifier", "down_check": {"path": "/lookup", "contains": "maintenance window"}}
        got = self.by(self.run_checks(rel), "when Orders is down")
        self.assertEqual(got["status"], "fail")
        self.assertFalse(STATE["down"])

    def test_no_check_written_or_no_lab_agent_stays_not_checked(self):
        got = self.by(self.run_checks({"kind": "identifier", "when_down": "an error"}), "when Orders is down")
        self.assertEqual(got["status"], "not-checked")
        self.assertIn("no check written", got["notes"])
        self.db.execute("UPDATE apps SET lab_url = '' WHERE id = 1")
        try:
            rel = {"kind": "identifier", "down_check": {"path": "/lookup", "contains": "unavailable"}}
            got = self.by(self.run_checks(rel), "when Orders is down")
            self.assertEqual(got["status"], "not-checked")
            self.assertIn("no lab agent", got["notes"])
        finally:
            self.db.execute("UPDATE apps SET lab_url = ? WHERE id = 1", (self.lab,))

    def test_a_peer_that_already_runs_under_a_condition_is_left_alone(self):
        rel = {"kind": "identifier", "down_check": {"path": "/lookup", "contains": "unavailable"}}
        STATE["condition"] = {"faults": [{"match": "* *", "status": 500, "rate": 0.1}]}
        original = dict(STATE["condition"])
        got = interfaces._down_result(dict({"producer": 1, "consumer": 2}, **rel), {1: "Orders"},
                                      self.db.execute("SELECT 1 AS id, ? AS url, ? AS lab_url", (self.producer, self.lab)).fetchone(),
                                      self.consumer, interfaces.LiveProbe())
        self.assertEqual(got["status"], "not-checked")
        self.assertEqual(STATE["condition"], original)


if __name__ == "__main__":
    unittest.main()
