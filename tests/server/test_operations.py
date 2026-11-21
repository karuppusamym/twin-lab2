"""Operating the portal in production: metrics, structured logs, tracing, encryption at rest,
retention, owner-requested deletion, purges, backup and restore.

The world (World): alice owns Billing Desk and CaseDesk; a paired recorder sent one recording
holding both and uploaded its bundle; Billing Desk has an attachment. carol has no role; root is a
portal admin. Everything runs on the test database engine (SQLite, or PostgreSQL in CI) with a
local artifact store; S3 is moto.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("S3_UPLOAD_PART_MIN_SIZE", "256")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

from support import Client, connect, new_database  # noqa: E402
from test_registry import CV, DESK, WEB, references  # noqa: E402
from twinlab2 import admin, artifacts, audit, crypto, lifecycle, migrations, observability, worker  # noqa: E402
from twinlab2.portal import create_app  # noqa: E402

SESSION = "20261119T100000Z-0a1b2c3d"
RECORDING = f"ws-{SESSION}"
CHUNK = 1024
OLD = (datetime.now(timezone.utc) - timedelta(days=400)).replace(microsecond=0).isoformat()
KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # base64 of 32 bytes, tests only
KEY2 = "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA="


class World(unittest.TestCase):
    """A portal behind the OAuth proxy with one uploaded recording and one attachment."""

    env = {}

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.store_dir = Path(self.work.name) / "store"
        patch = mock.patch.dict(os.environ, {"TWINLAB2_TRUST_PROXY_USER": "1", "TWINLAB2_ADMINS": "root",
                                             "TWINLAB2_DEV_WORKER": "0", "TWINLAB2_UPLOAD_CHUNK": str(CHUNK),
                                             "TWINLAB2_ARTIFACT_DIR": str(self.store_dir), **self.env})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("TWINLAB2_PASSWORD", "TWINLAB2_METRICS_TOKEN", "TWINLAB2_OTEL"):
            os.environ.pop(name, None)
        self.app = create_app(self.database)
        artifacts.configure(self.app.state.artifacts)  # outside requests (lifecycle calls in tests)
        self.client = Client(self.app, "alice")
        alice = self.client
        self.billing = self.ok(alice.post("/api/v1/apps", json={"name": "Billing Desk"}), 201)["id"]
        self.casedesk = self.ok(alice.post("/api/v1/apps", json={"name": "CaseDesk"}), 201)["id"]
        self.ok(alice.post(f"/api/v1/apps/{self.billing}/identities", json={"key": WEB.removeprefix("web:")}), 201)
        self.ok(alice.post(f"/api/v1/apps/{self.casedesk}/identities", json={"key": DESK}), 201)
        code = self.ok(alice.post("/api/v1/machines/pairing-codes"), 201)["code"]
        self.machine = alice.http.post("/api/agent/pair", json={"contract_version": CV, "code": code,
                                                                "name": "PC-1"}).json()["token"]
        sent = alice.http.post("/api/agent/references", json=references(),
                               headers={"Authorization": f"Bearer {self.machine}"})
        assert sent.status_code == 200, sent.text
        self.bundle = os.urandom(CHUNK * 2 + 10)
        self.upload(self.bundle)
        self.picture = b"\x89PNG\r\n\x1a\n" + os.urandom(300)
        self.attachment = self.ok(alice.post(f"/api/v1/apps/{self.billing}/attachments", data={
            "title": "Login screen", "kind": "Screen"}, files={"file": ("login.png", self.picture, "image/png")}), 201)["id"]

    def ok(self, response, status=200):
        assert response.status_code == status, (response.status_code, response.text[:1500])
        return response.json() if "json" in response.headers.get("content-type", "") else response

    def as_(self, user):
        return self.client.as_user(user)

    def upload(self, data):
        http, auth = self.client.http, {"Authorization": f"Bearer {self.machine}"}
        up = http.post("/api/uploads", headers=auth, json={"contract_version": CV, "session_id": SESSION,
                                                            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}).json()
        token = {"Authorization": f"Bearer {up['upload_token']}"}
        for offset in range(0, len(data), CHUNK):
            assert http.put(f"/api/uploads/{up['upload_id']}?offset={offset}", content=data[offset:offset + CHUNK],
                            headers=token).status_code == 200
        done = http.post(f"/api/uploads/{up['upload_id']}/complete", headers=token)
        assert done.status_code == 200, done.text

    def db(self):
        return closing(connect(self.database))

    def attachment_bytes(self, attachment_id, database=None, store=None):
        """The stored bytes of an attachment (None when its row is gone)."""
        with closing(connect(database or self.database)) as db:
            row = db.execute("SELECT artifact_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
            ref = artifacts.load(db, row["artifact_id"]) if row else None
            db.commit()
        return artifacts.read(ref, target=store or self.app.state.artifacts) if ref else None

    def artifact_files(self):
        return sorted(p for p in (self.store_dir / "objects").rglob("*") if p.is_file())


# -- metrics, logs, tracing ------------------------------------------------------------------------


class MetricsTest(World):
    def test_metrics_answer_on_the_internal_port_or_with_the_token_only(self):
        self.ok(self.client.get(f"/api/v1/apps/{self.billing}"))
        main = self.client.http
        self.assertEqual(main.get("/metrics").status_code, 404)  # the public port, no token
        with mock.patch.dict(os.environ, {"TWINLAB2_METRICS_TOKEN": "scrape-token-1"}):
            self.assertEqual(main.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code, 404)
            scraped = main.get("/metrics", headers={"Authorization": "Bearer scrape-token-1"})
        self.assertEqual(scraped.status_code, 200)
        internal = TestClient(self.app, base_url="http://10.1.2.3:9090")  # Prometheus, on the pod's IP
        body = internal.get("/metrics")
        self.assertEqual(body.status_code, 200)
        self.assertEqual(internal.get("/api/v1/session").status_code, 404)  # nothing else on that port
        text = body.text
        self.assertIn('twinlab2_http_requests_total{method="GET",route="/api/v1/apps/{app_id}",status="200"}', text)
        self.assertIn('twinlab2_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/api/v1/apps/{app_id}"}',
                      text)
        self.assertNotIn(f"/api/v1/apps/{self.billing}\"", text)  # templates, never raw paths
        self.assertIn("twinlab2_catalog_up 1.0", text)
        self.assertIn("twinlab2_workers_alive 0.0", text)
        self.assertRegex(text, r'twinlab2_artifacts\{kind="restricted_evidence"\} 2\.0')
        self.assertIn("twinlab2_upload_bytes_total", text)
        self.assertIn("twinlab2_artifact_store_free_bytes", text)

    def test_the_catalog_gauges_follow_the_queue(self):
        with self.db() as db:
            from twinlab2 import jobqueue

            jobqueue.enqueue(db, "verify", {}, delay=-120)
            jobqueue.enqueue(db, "build", {})
        text = TestClient(self.app, base_url="http://10.1.2.3:9090").get("/metrics").text
        self.assertIn('twinlab2_jobs{state="queued",type="verify"} 1.0', text)
        oldest = next(line for line in text.splitlines() if line.startswith('twinlab2_job_oldest_due_seconds{type="verify"}'))
        self.assertGreaterEqual(float(oldest.split()[-1]), 100)

    def test_every_metric_an_alert_uses_exists(self):
        import re
        import yaml

        text = TestClient(self.app, base_url="http://10.1.2.3:9090").get("/metrics").text
        exposed = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and not line.startswith("#")}
        exposed |= {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}  # no samples yet
        rules = yaml.safe_load((Path(__file__).resolve().parents[2] / "deploy" / "openshift-monitoring.yaml")
                               .read_text(encoding="utf-8"))["items"][1]["spec"]["groups"]
        used = {name for group in rules for rule in group["rules"]
                for name in re.findall(r"\b(twinlab2_[a-z_]+)", rule["expr"])}
        self.assertTrue(used)
        self.assertEqual(sorted(used - exposed), [])


class LogsTest(World):
    def capture(self, fmt):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        from twinlab2 import rest

        handler.addFilter(rest.RequestIdFilter())
        handler.setFormatter(observability.JsonFormatter() if fmt == "json" else logging.Formatter("%(message)s"))
        if fmt != "json":
            handler.addFilter(observability.TextScrubFilter())
        logger = logging.getLogger("twinlab2")
        logger.addHandler(handler)
        old = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(old)))
        return stream

    def test_a_request_line_is_one_json_object_with_its_ids(self):
        stream = self.capture("json")
        response = self.client.get(f"/api/v1/apps/{self.billing}", headers={"Request-Id": "trace-me-0001"})
        self.assertEqual(response.status_code, 200)
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        line = next(item for item in lines if item.get("route") == "/api/v1/apps/{app_id}")
        self.assertEqual((line["request_id"], line["user"], line["app_id"], line["status"], line["method"]),
                         ("trace-me-0001", "alice", self.billing, 200, "GET"))
        self.assertEqual(line["level"], "INFO")

    def test_secrets_and_signed_tokens_never_reach_a_log_line(self):
        cases = {
            "GET /artifacts/eyJyIjp7ImJhY2tlbmQiOiJsb2NhbCJ9fQ.abcdefghijklmnop 200": "/artifacts/[token]",
            "Authorization: Bearer abc.def-123": "Bearer [redacted]",
            "postgresql://twinlab2:hunter2@db:5432/x": "postgresql://twinlab2:[redacted]@db",
            "https://s3/x?X-Amz-Signature=deadbeef&token=t0k": "X-Amz-Signature=[redacted]&token=[redacted]",
        }
        for text, expected in cases.items():
            cleaned = observability.scrub(text)
            self.assertIn(expected, cleaned)
            self.assertNotIn("hunter2", cleaned)
        stream = self.capture("text")
        logging.getLogger("twinlab2.test").info("connecting to %s", "postgresql://u:hunter2@h/db")
        self.assertNotIn("hunter2", stream.getvalue())

    def test_configure_logging_picks_json(self):
        root = logging.getLogger()
        saved = [(h, h.formatter, list(h.filters)) for h in root.handlers]

        def restore():
            for handler, formatter, filters in saved:
                handler.setFormatter(formatter)
                handler.filters = filters
        self.addCleanup(restore)
        observability.configure_logging(level="INFO", fmt="json")
        self.assertTrue(all(isinstance(h.formatter, observability.JsonFormatter) for h in root.handlers))


class TracingTest(World):
    def test_tracing_is_off_unless_asked(self):
        observability.reset_tracing()
        self.addCleanup(observability.reset_tracing)
        self.assertIsNone(observability.tracer())

    def test_a_request_and_a_job_are_spans_named_by_template(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        observability.reset_tracing()
        self.addCleanup(observability.reset_tracing)
        observability.tracer(exporter=exporter)
        parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        self.ok(self.client.get(f"/api/v1/apps/{self.billing}", headers={"traceparent": parent}))
        span = next(s for s in exporter.get_finished_spans() if s.name == "GET /api/v1/apps/{app_id}")
        self.assertEqual(span.attributes["http.response.status_code"], 200)
        self.assertEqual(format(span.context.trace_id, "032x"), "0af7651916cd43dd8448eb211c80319c")  # continued
        with self.db() as db:
            run = lifecycle.request_run(db, "root", True)
            db.commit()
        worker.run(self.database, once=True, types=(lifecycle.JOB,))
        self.assertIn("job retention", [s.name for s in exporter.get_finished_spans()])
        self.assertEqual(self.ok(self.as_("root").get(f"/api/v1/admin/retention/runs/{run['id']}"))["state"], "done")


# -- encryption at rest ----------------------------------------------------------------------------


class LocalEncryptionTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.keyring = crypto.Keyring(crypto.parse_key(KEY))
        self.store = artifacts.LocalStore(self.work.name, keyring=self.keyring)

    def test_files_are_sealed_and_read_back(self):
        data = b"secret picture " * 100000  # several chunks
        ref = artifacts.put(data, kind="restricted_evidence", app_id=1, content_type="image/png", target=self.store)
        raw = self.store.file_of(ref).read_bytes()
        self.assertTrue(raw.startswith(crypto.MAGIC))
        self.assertNotIn(b"secret picture", raw)
        self.assertEqual(artifacts.read(ref, target=self.store), data)
        self.assertTrue(artifacts.verify(ref, self.store))
        self.assertIn("AES-256-GCM", self.store.describe()["encryption"])
        empty = artifacts.put(b"", kind="restricted_evidence", app_id=1, content_type="text/plain", target=self.store)
        self.assertEqual(artifacts.read(empty, target=self.store), b"")

    def test_a_changed_or_truncated_file_does_not_decrypt(self):
        ref = artifacts.put(os.urandom(5000), kind="restricted_evidence", app_id=1, content_type="x/y", target=self.store)
        path = self.store.file_of(ref)
        raw = bytearray(path.read_bytes())
        raw[-5] ^= 1
        path.write_bytes(bytes(raw))
        self.assertFalse(artifacts.verify(ref, self.store))
        path.write_bytes(bytes(raw[:-30]))
        with self.assertRaises(crypto.SealError):
            artifacts.read(ref, target=self.store)

    def test_rotation_previous_keys_and_rewrap(self):
        ref = artifacts.put(b"old key data", kind="restricted_evidence", app_id=1, content_type="x/y", target=self.store)
        plain = artifacts.put(b"before encryption", kind="restricted_evidence", app_id=1, content_type="x/y",
                              target=artifacts.LocalStore(self.work.name))
        new = crypto.Keyring(crypto.parse_key(KEY2))
        with self.assertRaises(crypto.SealError):  # the new key alone cannot read the old files
            artifacts.read(ref, target=artifacts.LocalStore(self.work.name, keyring=new))
        both = artifacts.LocalStore(self.work.name, keyring=crypto.Keyring(crypto.parse_key(KEY2), [crypto.parse_key(KEY)]))
        self.assertEqual(artifacts.read(ref, target=both), b"old key data")
        self.assertEqual(artifacts.read(plain, target=both), b"before encryption")  # plaintext files still read
        self.assertEqual(crypto.rewrap(both.file_of(ref), both.keyring), "rewrapped")
        self.assertEqual(crypto.rewrap(both.file_of(plain), both.keyring), "sealed")
        self.assertEqual(crypto.rewrap(both.file_of(ref), both.keyring), "current")
        only_new = artifacts.LocalStore(self.work.name, keyring=new)
        self.assertEqual(artifacts.read(ref, target=only_new), b"old key data")
        self.assertEqual(artifacts.read(plain, target=only_new), b"before encryption")

    def test_a_bad_key_is_refused_at_start(self):
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.from_env(environ={"TWINLAB2_ARTIFACT_DIR": self.work.name, "TWINLAB2_ARTIFACT_KEY": "short"})


@mock_aws
class S3EncryptionTest(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        self.client.create_bucket(Bucket="evidence")
        self.client.put_bucket_versioning(Bucket="evidence", VersioningConfiguration={"Status": "Enabled"})

    def test_every_write_asks_for_server_side_encryption(self):
        store = artifacts.S3Store("evidence", client=self.client, sse="AES256")
        ref = artifacts.put(b"one", kind="restricted_evidence", app_id=1, content_type="x/y", target=store)
        self.assertEqual(self.client.head_object(Bucket="evidence", Key=ref.key)["ServerSideEncryption"], "AES256")
        with mock.patch.object(artifacts.S3Store, "SINGLE_PUT_LIMIT", 300):
            big = artifacts.put(os.urandom(1000), kind="release_download", app_id=1, content_type="application/zip",
                                target=store)
        self.assertEqual(self.client.head_object(Bucket="evidence", Key=big.key)["ServerSideEncryption"], "AES256")
        self.assertIn("AES256 on every write", store.describe()["encryption"])
        kms = artifacts.S3Store("evidence", client=self.client, sse="aws:kms", kms_key_id="alias/twinlab2")
        self.assertEqual(kms._sse(), {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "alias/twinlab2"})
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.S3Store("evidence", client=self.client, sse="rot13")
        env = artifacts.from_env(environ={"TWINLAB2_ARTIFACT_BACKEND": "s3", "TWINLAB2_S3_BUCKET": "evidence",
                                          "TWINLAB2_S3_SSE": "AES256"})
        self.assertEqual(env.sse, "AES256")

    def test_delete_and_purge_remove_versions_for_good(self):
        store = artifacts.S3Store("evidence", client=self.client)
        first = artifacts.put(b"leak", kind="restricted_evidence", app_id=1, content_type="x/y", target=store)
        second = artifacts.put(b"leak", kind="restricted_evidence", app_id=1, content_type="x/y", target=store)
        self.assertEqual(first.key, second.key)  # content addressed: two versions of one key
        store.delete(first)
        self.assertFalse(store.exists(first))
        self.assertTrue(store.exists(second))
        self.assertEqual(store.purge_key(first.key), 1)
        versions = self.client.list_object_versions(Bucket="evidence", Prefix=first.key)
        self.assertEqual(versions.get("Versions", []) + versions.get("DeleteMarkers", []), [])


class PortalEncryptionTest(World):
    env = {"TWINLAB2_ARTIFACT_KEY": KEY}

    def test_uploads_and_attachments_are_sealed_on_disk(self):
        files = self.artifact_files()
        self.assertEqual(len(files), 2)
        for path in files:
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(crypto.MAGIC))
            self.assertNotIn(self.picture[8:40], raw)
        self.assertEqual(self.attachment_bytes(self.attachment), self.picture)
        self.assertIn("AES-256-GCM", self.ok(self.as_("root").get("/api/v1/admin/operations"))["storage"]["encryption"])


# -- retention, deletion, purge -------------------------------------------------------------------------


class RetentionTest(World):
    def age_everything(self):
        with self.db() as db:
            with db:
                db.execute("UPDATE artifacts SET created_at = ?", (OLD,))
                for n in range(3):
                    db.execute("INSERT INTO audit_log(at, actor, app_id, action, object_type, object_id, details) "
                               "VALUES (?, 'old', NULL, 'old.thing', 'x', ?, '{}')", (OLD, str(n)))

    def test_a_dry_run_counts_and_a_run_deletes_with_tombstones(self):
        self.age_everything()
        root = self.as_("root")
        self.ok(root.put("/api/v1/admin/retention", json={"raw_recordings": 30, "evidence": 30, "audit": 30}))
        before = self.artifact_files()
        plan = self.ok(root.get("/api/v1/admin/operations"))["plan"]
        self.assertEqual((plan["raw_recordings"]["count"], plan["evidence"]["count"], plan["audit"]["count"]), (1, 1, 3))
        self.assertEqual(plan["raw_recordings"]["bytes"], len(self.bundle))
        dry = self.ok(root.post("/api/v1/admin/retention/runs", json={"dry_run": True}), 202)
        worker.run(self.database, once=True, types=(lifecycle.JOB,))
        self.assertEqual(self.ok(root.get(f"/api/v1/admin/retention/runs/{dry['id']}"))["result"]["evidence"]["count"], 1)
        self.assertEqual(self.artifact_files(), before)  # a dry run deletes nothing

        real = self.ok(root.post("/api/v1/admin/retention/runs", json={"dry_run": False}), 202)
        worker.run(self.database, once=True, types=(lifecycle.JOB,))
        done = self.ok(root.get(f"/api/v1/admin/retention/runs/{real['id']}"))
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["result"]["raw_recordings"]["freed_bytes"], len(self.bundle))
        self.assertEqual(self.artifact_files(), [])
        self.assertIsNone(self.attachment_bytes(self.attachment))
        stones = self.ok(root.get("/api/v1/admin/tombstones"))["tombstones"]
        kinds = sorted(s["object_type"] for s in stones)
        self.assertEqual(kinds, ["attachment", "audit_log", "upload"])
        self.assertTrue(all(s["reason"] == "retention" for s in stones))
        self.assertIn("Login screen", [s["details"].get("title") for s in stones])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'old.thing'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'retention.apply'").fetchone()[0], 1)
            deleted = db.execute("SELECT deleted_reason, tombstone_id FROM artifacts").fetchall()
            self.assertTrue(all(r["deleted_reason"] == "retention" and r["tombstone_id"] for r in deleted))
            db.commit()
        self.assertEqual(TestClient(self.app, base_url="http://10.1.2.3:9090").get("/metrics").text.count(
            "twinlab2_retention_last_run_timestamp_seconds 0.0"), 0)

    def test_the_audit_log_stays_append_only_outside_retention(self):
        with self.db() as db:
            for sql in ("DELETE FROM audit_log", "UPDATE audit_log SET actor = 'x'"):
                with self.assertRaises(Exception):
                    with db:
                        db.execute(sql)
                db.rollback()
            # a window row lets old rows go, never newer ones, and UPDATE is still refused
            with db:
                db.execute("INSERT INTO audit_prune_window(cutoff) VALUES (?)", ("2000-01-01",))
            with self.assertRaises(Exception):
                with db:
                    db.execute("DELETE FROM audit_log")
            db.rollback()
            with db:
                lifecycle.tombstone(db, actor="t", reason="purge", object_type="file", object_id="x")
            with self.assertRaises(Exception):
                with db:
                    db.execute("DELETE FROM tombstones")
            db.rollback()

    def test_the_policy_is_checked_and_keeps_releases_and_the_latest_package(self):
        root = self.as_("root")
        self.assertEqual(root.put("/api/v1/admin/retention", json={"audit": 7}).status_code, 400)
        self.assertEqual(root.put("/api/v1/admin/retention", json={"evidence": 0}).status_code, 400)
        saved = self.ok(root.put("/api/v1/admin/retention", json={"evidence": 365, "schedule": "daily"}))
        self.assertEqual((saved["evidence"], saved["raw_recordings"], saved["schedule"]), (365, None, "daily"))
        with self.db() as db:
            jobs = db.execute("SELECT type, state FROM jobs WHERE dedupe_key = ?", (lifecycle.SCHEDULE_KEY,)).fetchall()
            self.assertEqual([tuple(j) for j in jobs], [("retention", "queued")])
            self.assertEqual(lifecycle.candidates(db, "frozen_packages", 1), [])
            db.commit()
        self.assertEqual(self.as_("alice").put("/api/v1/admin/retention", json={"evidence": 9}).status_code, 403)


class DeletionTest(World):
    def test_an_owner_deletes_a_recording_and_its_derived_data(self):
        self.assertEqual(self.as_("carol").post(f"/api/v1/recordings/{RECORDING}/deletion",
                                                json={"reason": "Not mine."}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/recordings/{RECORDING}/deletion", json={"reason": ""}).status_code, 400)
        deleted = self.ok(self.client.post(f"/api/v1/recordings/{RECORDING}/deletion",
                                           json={"reason": "Recorded on the production tenant."}))
        self.assertEqual(sorted(deleted["apps"]), sorted([self.billing, self.casedesk]))
        self.assertEqual((deleted["uploads_deleted"], deleted["freed_bytes"]), (1, len(self.bundle)))
        self.assertEqual(self.client.get(f"/api/v1/recordings/{RECORDING}").status_code, 404)
        self.assertEqual(len(self.artifact_files()), 1)  # only the attachment is left
        stones = self.ok(self.client.get(f"/api/v1/apps/{self.billing}/tombstones"))["tombstones"]
        self.assertEqual({s["object_type"] for s in stones}, {"recording"})
        self.assertEqual(stones[0]["details"]["note"], "Recorded on the production tenant.")
        self.assertEqual(self.as_("carol").get(f"/api/v1/apps/{self.billing}/tombstones").status_code, 403)
        with self.db() as db:
            for table in ("recording_apps", "evidence", "session_refs", "recording_contracts"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE recording_id = ?",
                                            (RECORDING,)).fetchone()[0], 0, table)
            actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE object_id = ?", (RECORDING,))]
            self.assertIn("recording.delete", actions)
            db.commit()
        again = self.client.post(f"/api/v1/recordings/{RECORDING}/deletion", json={"reason": "Again."})
        self.assertEqual(again.status_code, 403)  # it covers no application now: admins only
        self.assertEqual(self.as_("root").post(f"/api/v1/recordings/{RECORDING}/deletion",
                                               json={"reason": "Again."}).status_code, 404)

    def test_removing_an_attachment_removes_its_bytes(self):
        self.assertEqual(self.client.delete(f"/api/v1/apps/{self.billing}/attachments/{self.attachment}").status_code, 204)
        self.assertEqual(len(self.artifact_files()), 1)
        stones = self.ok(self.client.get(f"/api/v1/apps/{self.billing}/tombstones"))["tombstones"]
        self.assertEqual([(s["object_type"], s["sha256"]) for s in stones],
                         [("attachment", hashlib.sha256(self.picture).hexdigest())])


class PurgeTest(World):
    def test_a_leaked_picture_goes_from_every_copy(self):
        alice = self.client
        sha = hashlib.sha256(self.picture).hexdigest()
        copy = self.ok(alice.post(f"/api/v1/apps/{self.casedesk}/attachments", data={"title": "Again", "kind": "Screen"},
                                  files={"file": ("again.png", self.picture, "image/png")}), 201)["id"]
        # a stored ZIP (a release download) that holds it too
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("evidence/login.png", self.picture)
            archive.writestr("README.md", "other")
        with self.db() as db:
            ref = artifacts.save(db, artifacts.put(buffer.getvalue(), kind="release_download", app_id=self.billing,
                                                   content_type="application/zip"))
            db.commit()
        root = self.as_("root")
        found = self.ok(root.get(f"/api/v1/admin/files/{sha}"))
        self.assertEqual(len(found["artifacts"]), 2)
        self.assertEqual(found["inside"][0]["members"], ["evidence/login.png"])
        self.assertEqual(alice.post(f"/api/v1/admin/files/{sha}/purge", json={"note": "Leak."}).status_code, 403)
        purged = self.ok(root.post(f"/api/v1/admin/files/{sha}/purge", json={"note": "Card number visible (INC-1)."}))
        self.assertEqual((purged["artifacts_deleted"], [c["id"] for c in purged["containers_deleted"]]), (2, [ref.id]))
        for attachment in (self.attachment, copy):
            self.assertIsNone(self.attachment_bytes(attachment))
        self.assertEqual(len(self.artifact_files()), 1)  # only the recording bundle is left
        with self.db() as db:
            with self.assertRaises(artifacts.ArtifactDeleted):
                artifacts.read(artifacts.load(db, ref.id))
            db.commit()
        self.assertEqual(self.ok(root.get(f"/api/v1/admin/files/{sha}")), {"sha256": sha, "artifacts": [], "inside": []})
        self.assertEqual(root.get("/api/v1/admin/files/xyz").status_code, 400)


class AuditExportTest(World):
    def test_csv_and_json_lines_filtered_by_date(self):
        root = self.as_("root")
        with self.db() as db:
            with db:
                audit.record(db, "mallory", "=cmd|' /C calc'!A0", "x", "1")
                db.execute("INSERT INTO audit_log(at, actor, app_id, action, object_type, object_id, details) "
                           "VALUES ('2020-01-02T00:00:00+00:00', 'old', NULL, 'old.thing', 'x', '1', '{}')")
        csv_text = root.get("/api/v1/admin/audit-export", params={"since": "2021-01-01"}).text
        self.assertNotIn("old.thing", csv_text)
        self.assertIn("'=cmd", csv_text)  # a formula is not run by a spreadsheet
        self.assertIn("evidence.upload", csv_text)
        lines = root.get("/api/v1/admin/audit-export", params={"until": "2021-01-01", "format": "json"})
        self.assertEqual(lines.headers["content-type"], "application/x-ndjson")
        rows = [json.loads(line) for line in lines.text.splitlines()]
        self.assertEqual([r["action"] for r in rows], ["old.thing"])
        self.assertEqual(root.get("/api/v1/admin/audit-export", params={"since": "yesterday"}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/admin/audit-export").status_code, 403)
        exported = root.get("/api/v1/admin/audit-export", params={"format": "json"}).text
        self.assertIn("audit.export", exported)  # exports are audited


# -- backup and restore -------------------------------------------------------------------------------


class BackupRestoreTest(World):
    def backup(self, **kw):
        folder = Path(self.work.name) / "backup"
        admin.backup(self.database, folder, store=self.app.state.artifacts, out=lambda *_: None, **kw)
        return folder

    def test_restore_into_a_fresh_database_passes_the_integrity_checks(self):
        folder = self.backup()
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], migrations.latest())
        self.assertEqual(len(manifest["objects"]), 2)
        self.assertEqual(admin.verify(self.database, self.app.state.artifacts, backup_dir=folder), [])

        fresh = new_database(Path(self.work.name) / "fresh")
        new_store = Path(self.work.name) / "restored-store"
        with mock.patch.dict(os.environ, {"TWINLAB2_ARTIFACT_DIR": str(new_store)}):
            self.assertEqual(admin.main(["restore", str(folder), "--database", str(fresh)]), 0)
            self.assertEqual(admin.main(["verify-backup", str(folder), "--database", str(fresh)]), 0)
            self.assertEqual(admin.main(["restore", str(folder), "--database", str(fresh)]), 1)  # not empty
            app = create_app(fresh)
        client = Client(app, "alice")
        self.assertEqual(self.attachment_bytes(self.attachment, fresh, app.state.artifacts), self.picture)
        self.assertEqual(self.ok(client.get(f"/api/v1/recordings/{RECORDING}"))["id"], RECORDING)
        # new rows after the restore get new ids (identity counters moved past the copied ones)
        later = self.ok(client.post("/api/v1/apps", json={"name": "After restore"}), 201)["id"]
        self.assertGreater(later, self.casedesk)
        # damage is found: a lost object in the store, a changed backup file
        with closing(connect(fresh)) as db:
            ref = artifacts.load(db, db.execute("SELECT artifact_id FROM attachments").fetchone()[0])
            db.commit()
        artifacts.LocalStore(new_store).delete(ref)
        problems = admin.verify(fresh, artifacts.LocalStore(new_store))
        self.assertTrue(any("missing or does not match" in p for p in problems), problems)
        (folder / "catalog" / "apps.jsonl").write_text("[]\n", encoding="utf-8")
        self.assertTrue(any("apps.jsonl does not match" in p for p in admin.verify_backup_dir(folder)))
        with self.assertRaises(RuntimeError):
            admin.restore(folder, new_database(Path(self.work.name) / "third"), out=lambda *_: None)

    def test_a_backup_of_an_encrypted_store_stays_sealed(self):
        keyring = crypto.Keyring(crypto.parse_key(KEY))
        sealed_store = artifacts.LocalStore(self.store_dir, keyring=keyring)
        folder = Path(self.work.name) / "sealed-backup"
        admin.backup(self.database, folder, store=sealed_store, out=lambda *_: None)
        for path in (folder / "objects").iterdir():
            self.assertTrue(path.read_bytes().startswith(crypto.MAGIC))
        self.assertEqual(admin.verify_backup_dir(folder, sealed_store), [])
        problems = admin.verify_backup_dir(folder, artifacts.LocalStore(self.store_dir))  # without the key
        self.assertTrue(problems and all("cannot be read" in p for p in problems), problems)

    def test_a_deleted_file_is_not_backed_up_and_still_verifies(self):
        self.ok(self.client.post(f"/api/v1/recordings/{RECORDING}/deletion", json={"reason": "Mistake."}))
        folder = self.backup()
        self.assertEqual(len(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["objects"]), 1)
        self.assertEqual(admin.verify(self.database, self.app.state.artifacts, backup_dir=folder), [])


if __name__ == "__main__":
    unittest.main()
