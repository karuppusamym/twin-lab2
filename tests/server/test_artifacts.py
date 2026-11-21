"""Artifact storage (local and S3 via moto), the blob migration, resumable uploads and the
recorder's offline upload queue. No real AWS: S3 is moto, in process or as a local server."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from contextlib import closing
import tempfile
import time
import unittest
from unittest import mock
from zipfile import ZipFile

os.environ.setdefault("S3_UPLOAD_PART_MIN_SIZE", "256")  # moto's multipart minimum, before moto loads
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402

from twinlab2 import artifacts  # noqa: E402
from twinlab2_recorder.client import Portal, PortalError  # noqa: E402
from twinlab2_recorder.upload_queue import UploadQueue  # noqa: E402
from twinlab2.portal import create_app  # noqa: E402
from support import Client, Server, freeze  # noqa: E402
from test_registry import CV, references  # noqa: E402
from twinlab2.db import state_dir
from support import connect, new_database

SESSION = "20261119T100000Z-0a1b2c3d"
CHUNK = 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.store = artifacts.LocalStore(self.root / "artifacts")

    def test_put_open_and_verify(self):
        ref = artifacts.put(b"hello", kind="builder_package", app_id=3, content_type="application/zip", target=self.store)
        self.assertEqual((ref.backend, ref.sha256, ref.size, ref.kind), ("local", digest(b"hello"), 5, "builder_package"))
        self.assertEqual(ref.key, f"builder_package/3/{digest(b'hello')}")
        with artifacts.open(ref, self.store) as stream:
            self.assertEqual(stream.read(), b"hello")
        self.assertTrue(artifacts.verify(ref, self.store))
        again = artifacts.put(io.BytesIO(b"hello"), kind="builder_package", app_id=3, content_type="application/zip",
                              target=self.store)
        self.assertEqual(again.key, ref.key)
        self.assertNotEqual(again.version_id, ref.version_id)  # a new version, the old one unchanged
        self.assertEqual(artifacts.read(ref, target=self.store), b"hello")

    def test_a_wrong_checksum_stores_nothing(self):
        with self.assertRaises(artifacts.ChecksumMismatch):
            artifacts.put(b"hello", kind="restricted_evidence", app_id=None, content_type="text/plain",
                          sha256="0" * 64, target=self.store)
        self.assertEqual([p for p in (self.root / "artifacts").rglob("*") if p.is_file()], [])
        with self.assertRaises(ValueError):
            artifacts.put(b"x", kind="secrets", app_id=None, content_type="text/plain", target=self.store)

    def test_a_changed_object_does_not_verify(self):
        ref = artifacts.put(b"hello", kind="verification_report", app_id=1, content_type="text/plain", target=self.store)
        path = self.root / "artifacts" / "objects" / ref.key / ref.version_id
        path.write_bytes(b"jello")
        self.assertFalse(artifacts.verify(ref, self.store))
        with self.assertRaises(artifacts.ChecksumMismatch):
            artifacts.read(ref, target=self.store)

    def test_refs_round_trip_through_the_database(self):
        with closing(connect(":memory:")) as db:
            db.executescript("CREATE TABLE attachments (id INTEGER PRIMARY KEY, content BLOB);"
                             "CREATE TABLE brief_packages (revision TEXT PRIMARY KEY, archive BLOB);"
                             "CREATE TABLE releases (id INTEGER PRIMARY KEY, archive BLOB);")
            artifacts.migrate(db)
            ref = artifacts.save(db, artifacts.put(b"zip", kind="release_download", app_id=2,
                                                   content_type="application/zip", target=self.store))
            self.assertEqual(artifacts.save(db, ref).id, ref.id)
            self.assertEqual(artifacts.load(db, ref.id), ref)

    def test_presigned_local_url_expires(self):
        app = create_app(new_database(self.root))
        # what a portal request binds: the app's store, its secret and its /artifacts/<token> route
        bound = artifacts.bind(artifacts.Binding(app.state.artifacts, app.config["SECRET_KEY"],
                                                 lambda token: f"http://localhost/artifacts/{token}"))
        try:
            ref = artifacts.put(b"pack", kind="builder_package", app_id=1, content_type="application/zip")
            url = artifacts.presign_get(ref, 60, filename="pack.zip")
        finally:
            artifacts.unbind(bound)
        client = Client(app).http
        path = url.split("localhost", 1)[-1]
        response = client.get(path)
        self.assertEqual((response.status_code, response.content), (200, b"pack"))
        self.assertIn("pack.zip", response.headers["Content-Disposition"])
        self.assertEqual(client.get(path[:-3] + "xyz").status_code, 403)
        with mock.patch("twinlab2.artifacts.time.time", return_value=time.time() + 120):
            self.assertEqual(client.get(path).status_code, 403)


@mock_aws
class S3StoreTest(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        self.client.create_bucket(Bucket="evidence")
        self.client.put_bucket_versioning(Bucket="evidence", VersioningConfiguration={"Status": "Enabled"})
        self.store = artifacts.S3Store("evidence", prefix="twinlab2/", client=self.client)

    def test_put_open_presign_and_versions(self):
        ref = artifacts.put(b"v1", kind="restricted_evidence", app_id=7, content_type="text/plain", target=self.store)
        self.assertEqual((ref.backend, ref.bucket), ("s3", "evidence"))
        self.assertTrue(ref.key.startswith("twinlab2/restricted_evidence/7/"))
        self.assertNotIn(ref.version_id, ("", "null"))
        self.client.put_object(Bucket="evidence", Key=ref.key, Body=b"overwritten")  # a later write to the key
        self.assertEqual(artifacts.read(ref, target=self.store), b"v1")  # the ref names its own version
        url = artifacts.presign_get(ref, 300, filename="note.txt", target=self.store)
        self.assertIn("versionId=", url)
        self.assertIn("X-Amz-Expires=300", url)
        self.assertEqual(self.store.describe()["versioning"], "Enabled")
        self.assertNotIn("testing", str(self.store.describe()))  # no credentials on the Setup page

    def test_large_objects_go_up_in_parts(self):
        with mock.patch.object(artifacts.S3Store, "SINGLE_PUT_LIMIT", 300):
            data = os.urandom(1000)
            ref = artifacts.put(data, kind="release_download", app_id=1, content_type="application/zip",
                                target=self.store)
        self.assertEqual(artifacts.read(ref, target=self.store), data)

    def test_portal_on_s3_redirects_downloads_to_presigned_urls(self):
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(
                os.environ, {"TWINLAB2_ARTIFACT_BACKEND": "s3", "TWINLAB2_S3_BUCKET": "evidence"}):
            app = create_app(new_database(work), store=self.store)
            client = Client(app)
            self.assertEqual(client.post("/api/v1/apps", json={"name": "Orders"}).status_code, 201)
            self.assertEqual(client.post("/api/v1/apps/1/scenarios", json={"name": "Find", "expected": "Found."})
                             .status_code, 201)
            response = freeze(client, 1)
            self.assertEqual(response.status_code, 302, response.text[:500])
            self.assertIn("X-Amz-Signature", response.headers["Location"])
            storage = client.get("/api/v1/setup").json()["storage"]
            self.assertIn("evidence", str(storage))
            self.assertNotIn("testing", str(storage))


class MigrationTest(unittest.TestCase):
    """Blobs in an existing catalog are copied to artifacts, checked, and keep working."""

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.database = new_database(work.name)
        self.pack, self.release = b"PK-pack", b"PK-release"
        create_app(self.database)  # today's schema, then rows as an older portal stored them
        with closing(connect(self.database)) as db, db:
            db.execute("INSERT INTO apps(id, name, git_url, requirements) VALUES (1, 'Orders', '', '')")
            db.execute("INSERT INTO attachments(app_id, title, kind, extension, content) VALUES (1, 'API', 'API', '.json', ?)",
                       (b'{"a": 1}',))
            db.execute("INSERT INTO briefs VALUES ('r1', 1, '{\"evidence\": []}')")
            db.execute("INSERT INTO brief_packages(revision, archive) VALUES ('r1', ?)", (self.pack,))
            db.execute("INSERT INTO releases(app_id, version, revision, commit_sha, archive, archive_sha) "
                       "VALUES (1, '1.0', 'r1', ?, ?, ?)", ("a" * 40, self.release, digest(self.release)))
            db.execute("INSERT INTO releases(app_id, version, revision, commit_sha, archive, archive_sha) "
                       "VALUES (1, '1.1', 'r1', ?, ?, 'not-its-digest')", ("b" * 40, b"PK-bad"))

    def rows(self):
        with closing(connect(self.database)) as db:
            return {"attachments": db.execute("SELECT artifact_id, length(content) FROM attachments").fetchall(),
                    "packs": db.execute("SELECT artifact_id, length(archive) FROM brief_packages").fetchall(),
                    "releases": db.execute("SELECT version, artifact_id, length(archive) FROM releases ORDER BY id").fetchall()}

    def test_old_rows_work_before_during_and_after_the_migration(self):
        with mock.patch.dict(os.environ, {"TWINLAB2_ARTIFACT_MIGRATE": "0"}):
            client = Client(create_app(self.database))
        self.assertEqual(client.get("/api/v1/apps/1/briefs/r1/download").content, self.pack)  # not migrated yet: from the blob
        self.assertEqual(client.get("/api/v1/releases/1/download").content, self.release)

        client = Client(create_app(self.database))  # the start copies and checks
        rows = self.rows()
        self.assertIsNotNone(rows["attachments"][0][0])
        self.assertIsNotNone(rows["packs"][0][0])
        self.assertEqual([(v, a is not None) for v, a, _ in rows["releases"]], [("1.0", True), ("1.1", False)])
        self.assertEqual(client.get("/api/v1/apps/1/briefs/r1/download").content, self.pack)
        self.assertEqual(client.get("/api/v1/releases/1/download").content, self.release)
        self.assertEqual(client.get("/api/v1/releases/2/download").content, b"PK-bad")  # not copied: still its blob
        with closing(connect(self.database)) as db:
            store = artifacts.from_env(self.database)
            self.assertEqual(artifacts.migrate_blobs(db, store)["copied"], 0)  # runs again, copies nothing twice
            self.assertEqual(artifacts.purge_blobs(db, store)["purged"], 3)
        rows = self.rows()
        self.assertEqual(rows["packs"][0][1], 0)
        self.assertEqual(client.get("/api/v1/apps/1/briefs/r1/download").content, self.pack)  # now from the artifact
        self.assertEqual(client.get("/api/v1/releases/1/download").content, self.release)
        self.assertEqual(client.post("/api/v1/apps/1/scenarios", json={"name": "Find", "expected": "Found."})
                         .status_code, 201)
        # an attachment stored before roles is restricted evidence until the owner shares it
        self.assertEqual(client.patch("/api/v1/apps/1/attachments/1", json={"share": "reviewed"}).status_code, 200)
        with ZipFile(io.BytesIO(freeze(client, 1).content)) as pack:
            content = pack.read([n for n in pack.namelist() if n.startswith("attachments/")][0])
            self.assertEqual(json.loads(content), {"a": 1})

    def test_a_copy_that_does_not_verify_is_not_used(self):
        real_put = artifacts.put

        def corrupting_put(data, **kwargs):
            ref = real_put(data, **kwargs)
            store = kwargs["target"]
            (store.root / "objects" / ref.key / ref.version_id).write_bytes(b"corrupt")
            return ref

        with mock.patch("twinlab2.artifacts.put", corrupting_put):
            create_app(self.database)
        self.assertEqual(self.rows()["packs"][0][0], None)
        self.assertEqual(Client(create_app(self.database)).get("/api/v1/apps/1/briefs/r1/download").content, self.pack)


class UploadApiTest(unittest.TestCase):
    """The resumable upload API against the local backend, through the ASGI test client."""

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.database = new_database(work.name)
        env = mock.patch.dict(os.environ, {"TWINLAB2_UPLOAD_CHUNK": str(CHUNK)})
        env.start()
        self.addCleanup(env.stop)
        self.start_portal()
        code = Client(self.app).post("/api/v1/machines/pairing-codes").json()["code"]
        self.machine = self.client.post("/api/agent/pair", json={"contract_version": CV, "code": code, "name": "PC"}).json()["token"]
        self.data = os.urandom(CHUNK * 3 + 100)

    def start_portal(self):
        self.app = create_app(self.database)
        self.client = Client(self.app).http  # the recorder's API: no CSRF, not under /api/v1

    def init(self, sha=None, size=None, token=None):
        return self.client.post("/api/uploads", headers={"Authorization": f"Bearer {token or self.machine}"},
                                json={"contract_version": CV, "session_id": SESSION, "size": size or len(self.data),
                                      "sha256": sha or digest(self.data)})

    def put(self, up, offset):
        return self.client.put(f"/api/uploads/{up['upload_id']}?offset={offset}", content=self.data[offset:offset + CHUNK],
                               headers={"Authorization": f"Bearer {up['upload_token']}"})

    def call(self, method, up, suffix=""):
        return getattr(self.client, method)(f"/api/uploads/{up['upload_id']}{suffix}",
                                            headers={"Authorization": f"Bearer {up['upload_token']}"})

    def progress(self):
        return self.client.get(f"/api/recordings/ws-{SESSION}/upload").json()

    def test_interrupted_upload_resumes_across_a_portal_restart(self):
        self.assertEqual(self.client.get(f"/api/recordings/ws-{SESSION}/upload").status_code, 404)
        up = self.init().json()
        self.assertEqual((up["state"], up["received_bytes"], up["mode"], up["chunk_size"]), ("receiving", 0, "portal", CHUNK))
        self.assertEqual(self.put(up, 0).status_code, 200)
        self.assertEqual(self.put(up, CHUNK).status_code, 200)
        self.assertEqual(self.progress(), {"state": "receiving", "received_bytes": 2 * CHUNK,
                                           "total_bytes": len(self.data), "last_error": None})
        self.assertEqual(self.call("post", up, "/complete").status_code, 409)  # not all there yet

        self.start_portal()  # the portal restarts in the middle of the upload
        status = self.call("get", up)
        self.assertEqual((status.status_code, status.json()["received_bytes"]), (200, 2 * CHUNK))
        self.assertEqual(status.headers["Upload-Offset"], str(2 * CHUNK))
        wrong = self.put(up, 0)  # a chunk at the wrong offset is refused with the right one
        self.assertEqual((wrong.status_code, wrong.json()["received_bytes"]), (409, 2 * CHUNK))
        again = self.init().json()  # the recorder restarted too: init resumes the same upload
        self.assertEqual((again["upload_id"], again["received_bytes"]), (up["upload_id"], 2 * CHUNK))
        self.assertEqual(self.call("get", up).status_code, 401)  # the old authorisation was replaced
        for offset in (2 * CHUNK, 3 * CHUNK):
            self.assertEqual(self.put(again, offset).status_code, 200)
        done = self.call("post", again, "/complete")
        self.assertEqual((done.status_code, done.json()["state"]), (200, "confirmed"))
        self.assertEqual(done.json()["artifact"]["sha256"], digest(self.data))
        self.assertEqual(self.progress()["state"], "confirmed")
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT artifact_id FROM uploads").fetchone()
            ref = artifacts.load(db, row["artifact_id"])
            self.assertEqual((ref.kind, artifacts.read(ref, target=self.app.state.artifacts)),
                             ("restricted_evidence", self.data))
        self.assertEqual(list((state_dir(self.database) / "artifacts" / "uploads").iterdir()), [])  # staging gone
        self.assertEqual(self.init().json()["state"], "confirmed")  # sending it again changes nothing

    def test_a_checksum_mismatch_is_rejected(self):
        up = self.init(sha="1" * 64).json()
        for offset in range(0, len(self.data), CHUNK):
            self.put(up, offset)
        done = self.call("post", up, "/complete")
        self.assertEqual(done.status_code, 422)
        self.assertIn("checksum mismatch", done.json()["error"])
        self.assertEqual(self.progress()["state"], "rejected")
        self.assertIn("checksum mismatch", self.progress()["last_error"])
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        retry = self.init(sha="1" * 64).json()  # a new upload, from the beginning
        self.assertNotEqual(retry["upload_id"], up["upload_id"])
        self.assertEqual(retry["received_bytes"], 0)

    def test_refusals(self):
        self.assertEqual(self.init(token="x" * 50).status_code, 401)
        old = self.client.post("/api/uploads", headers={"Authorization": f"Bearer {self.machine}"},
                               json={"session_id": SESSION, "size": 1, "sha256": "0" * 64})
        self.assertEqual((old.status_code, old.json()["update"]), (426, "recorder"))
        self.assertEqual(self.init(size=-1).status_code, 400)
        up = self.init().json()
        short = self.client.put(f"/api/uploads/{up['upload_id']}?offset=0", content=b"short",
                                headers={"Authorization": f"Bearer {up['upload_token']}"})
        self.assertEqual(short.status_code, 400)  # only the last chunk may be short
        with mock.patch("twinlab2.uploads._now", return_value=datetime.now(timezone.utc) + timedelta(hours=2)):
            expired = self.put(up, 0)
        self.assertEqual((expired.status_code, expired.json()["expired"]), (401, True))
        self.assertEqual(self.client.get("/api/uploads/nope", headers={"Authorization": "Bearer x"}).status_code, 404)


class FlakyPortal:
    """The recorder's Portal, with the network failing after `ok` chunk or part PUTs."""

    def __init__(self, portal, ok):
        self.portal, self.ok, self.sent = portal, ok, []

    def __getattr__(self, name):
        return getattr(self.portal, name)

    def _maybe_fail(self, what):
        if len(self.sent) >= self.ok:
            raise PortalError("the portal cannot be reached: network down")
        self.sent.append(what)

    def upload_chunk(self, upload_id, token, offset, data):
        self._maybe_fail(offset)
        return self.portal.upload_chunk(upload_id, token, offset, data)

    def put_part(self, url, data):
        self._maybe_fail(url)
        return self.portal.put_part(url, data)


class QueueTest(unittest.TestCase):
    """The recorder's offline queue against a real portal server on this machine."""

    backend_env = {}

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.database = new_database(self.root / "portal")
        env = mock.patch.dict(os.environ, {"TWINLAB2_UPLOAD_CHUNK": str(CHUNK), **self.backend_env})
        env.start()
        self.addCleanup(env.stop)
        self.server = Server(create_app(self.database))
        self.addCleanup(lambda: self.server.stop())
        code = Client(self.server.app).post("/api/v1/machines/pairing-codes").json()["code"]
        self.token = Portal(self.server.url).pair(code, "PC")["token"]
        self.session = self.root / "recorder" / "recordings" / SESSION
        self.session.mkdir(parents=True)
        (self.session / "steps.jsonl").write_bytes(os.urandom(CHUNK * 4 + 7))  # does not compress
        (self.session / "stop").write_text("")

    def portal(self):
        return Portal(self.server.url, self.token, timeout=5)

    def dest(self):  # the same portal after a restart (a new port here)
        return {"portal": self.portal().url.rstrip("/"), "machine_id": 1}

    def restart_portal(self):
        self.server.stop()
        self.server = Server(create_app(self.database), port=self.server.port)

    def stored(self):
        with closing(connect(self.database)) as db:
            row = db.execute("SELECT artifact_id FROM uploads WHERE state = 'confirmed'").fetchone()
            return artifacts.read(artifacts.load(db, row["artifact_id"]), target=self.server.app.state.artifacts)

    def test_queue_survives_network_loss_and_restarts_and_resumes(self):
        queue = UploadQueue(self.root / "recorder", destination=self.dest)
        job = queue.add_session(self.session, SESSION)
        self.assertEqual(queue.add_session(self.session, SESSION)["id"], job["id"])  # same bytes, same job
        queued = queue.file_of(job)
        flaky = FlakyPortal(self.portal(), ok=2)
        job = queue.run(flaky)[0]
        self.assertEqual(job["state"], "uploading")
        self.assertIn("network down", job["last_error"])
        self.assertTrue(queued.is_file())  # kept until the portal confirms

        self.restart_portal()  # both sides restart: the portal, and the recorder's queue object
        queue = UploadQueue(self.root / "recorder", destination=self.dest)
        self.assertEqual(queue.run(self.portal())[0]["state"], "uploading")  # waiting to retry: not due yet
        resumed = FlakyPortal(self.portal(), ok=100)
        job = queue.run(resumed, force=True)[0]
        self.assertEqual(job["state"], "confirmed", job)
        if job["mode"] == "portal":
            self.assertEqual(resumed.sent[0], 2 * CHUNK)  # resumed, not sent again from the start
        else:
            self.assertEqual(len(resumed.sent), 3)  # the parts storage did not have yet
        self.assertFalse(queued.exists())
        with ZipFile(io.BytesIO(self.stored())) as archive:
            self.assertEqual(sorted(archive.namelist()), ["steps.jsonl"])
            self.assertEqual(archive.read("steps.jsonl"), (self.session / "steps.jsonl").read_bytes())
        status = Client(self.server.app).http.get(f"/api/recordings/ws-{SESSION}/upload").json()
        self.assertEqual(status["state"], "confirmed")

    def test_portal_restart_in_the_middle_of_a_chunked_upload(self):
        queue = UploadQueue(self.root / "recorder", destination=self.dest)
        job = queue.add_session(self.session, SESSION)
        portal = self.portal()
        answer = portal.upload_init(SESSION, job["size"], job["sha256"])
        data = queue.file_of(job).read_bytes()
        if answer["mode"] == "portal":
            portal.upload_chunk(answer["upload_id"], answer["upload_token"], 0, data[:CHUNK])
        else:
            url = portal.upload_part_urls(answer["upload_id"], answer["upload_token"], [1])["urls"]["1"]
            portal.put_part(url, data[:CHUNK])
        self.restart_portal()
        status = self.portal().upload_status(answer["upload_id"], answer["upload_token"])
        self.assertEqual(status["received_bytes"], CHUNK)
        self.assertEqual(queue.run(self.portal(), force=True)[0]["state"], "confirmed")
        self.assertEqual(self.stored(), data)

    def test_a_rejected_checksum_is_sent_again_and_a_changed_file_fails(self):
        queue = UploadQueue(self.root / "recorder", destination=self.dest)
        job = queue.add_session(self.session, SESSION)
        real_complete = Portal.upload_complete
        calls = []

        def corrupt_once(portal, upload_id, token):
            if not calls:  # storage got different bytes the first time
                calls.append(upload_id)
                with closing(connect(self.database)) as db:
                    db.execute("UPDATE uploads SET sha256 = ? WHERE id = ?", ("2" * 64, upload_id))
                    db.commit()
            return real_complete(portal, upload_id, token)

        with mock.patch.object(Portal, "upload_complete", corrupt_once):
            job = queue.run(self.portal())[0]
            self.assertEqual((job["state"], job["rejections"]), ("uploading", 1))
            self.assertIn("checksum mismatch", job["last_error"])
            job = queue.run(self.portal(), force=True)[0]
        self.assertEqual(job["state"], "confirmed")

        other = queue.add_file(self.session / "steps.jsonl", SESSION, "application/octet-stream")
        queue.file_of(other).write_bytes(b"changed")
        queue.run(self.portal(), force=True)
        self.assertEqual(queue.get(other["id"])["state"], "failed")
        self.assertTrue(queue.file_of(other).is_file())  # kept for a person to look at


class S3QueueTest(QueueTest):
    """The same queue against a portal on S3 (a local moto server, like MinIO): the recorder
    sends parts straight to presigned storage URLs."""

    @classmethod
    def setUpClass(cls):
        cls.moto = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        cls.moto.start()
        host, port = cls.moto.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        s3 = boto3.client("s3", region_name="us-east-1", endpoint_url=endpoint)
        s3.create_bucket(Bucket="twinlab2")
        s3.put_bucket_versioning(Bucket="twinlab2", VersioningConfiguration={"Status": "Enabled"})
        cls.backend_env = {"TWINLAB2_ARTIFACT_BACKEND": "s3", "TWINLAB2_S3_BUCKET": "twinlab2",
                           "TWINLAB2_S3_ENDPOINT_URL": endpoint, "TWINLAB2_S3_REGION": "us-east-1"}

    @classmethod
    def tearDownClass(cls):
        cls.moto.stop()

    def test_proxy_mode_when_the_recorder_cannot_reach_storage(self):
        with mock.patch.dict(os.environ, {"TWINLAB2_S3_DIRECT_UPLOAD": "0"}):
            self.restart_portal()
            queue = UploadQueue(self.root / "recorder", destination=self.dest)
            job = queue.add_session(self.session, SESSION)
            flaky = FlakyPortal(self.portal(), ok=1)
            self.assertEqual(queue.run(flaky)[0]["mode"], "portal")
            self.restart_portal()
            self.assertEqual(queue.run(self.portal(), force=True)[0]["state"], "confirmed")
            with ZipFile(io.BytesIO(self.stored())) as archive:
                self.assertEqual(archive.read("steps.jsonl"), (self.session / "steps.jsonl").read_bytes())
            self.assertEqual(job["sha256"], digest(self.stored()))


if __name__ == "__main__":
    unittest.main()
