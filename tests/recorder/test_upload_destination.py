"""Queued uploads are bound to the portal and machine they were queued for (upload_queue)."""

import json

import pytest

from twinlab2_recorder import ui_model as ui
from twinlab2_recorder.upload_queue import UploadQueue, destination_of, same_destination

A = {"portal": "https://a.example", "machine_id": 1, "machine": "PC-A"}
B = {"portal": "https://b.example", "machine_id": 7, "machine": "PC-B"}


class Portal:
    def __init__(self, url):
        self.url = url
        self.calls = []

    def upload_init(self, session_id, size, sha256, content_type):
        self.calls.append("init")
        return {"upload_id": "u1", "upload_token": "t", "expires_at": "2999-01-01T00:00:00+00:00",
                "mode": "portal", "chunk_size": 1024, "received_bytes": 0, "state": "uploading"}

    def upload_status(self, upload_id, token):
        return {"state": "uploading", "received_bytes": 0}

    def upload_chunk(self, upload_id, token, offset, data):
        self.calls.append("chunk")
        return {"received_bytes": offset + len(data)}

    def upload_complete(self, upload_id, token):
        return {"state": "confirmed", "recording_id": 5}


@pytest.fixture
def setup(tmp_path):
    current = {"dest": dict(A)}
    queue = UploadQueue(tmp_path, destination=lambda: current["dest"])
    source = tmp_path / "rec.zip"
    source.write_bytes(b"x" * 3000)
    job = queue.add_file(source, "session-123")
    return queue, job, current


def test_a_job_records_its_destination(setup):
    queue, job, _ = setup
    assert job["destination"] == A
    assert job["transfers"] == [] and job["held"] is None


def test_same_destination_sends(setup):
    queue, job, _ = setup
    portal = Portal(A["portal"])
    assert queue.run(portal, force=True)[0]["state"] == "confirmed"


@pytest.mark.parametrize("other", [B, dict(A, machine_id=2), dict(B, machine_id=1)])
def test_repairing_holds_the_job_and_sends_nothing(setup, other):
    queue, job, current = setup
    current["dest"] = other
    portal = Portal(other["portal"])
    after = queue.run(portal, force=True)[0]
    assert after["state"] == "queued" and after["held"]
    assert "transfer" in after["held"]
    assert portal.calls == []  # nothing went to the new portal
    assert queue.retry(job["id"])["held"]  # retry does not release it
    assert queue.run(portal, force=True)[0]["held"] and portal.calls == []
    assert ui.upload_row(after, 0)["state"] == "held"
    assert ui.upload_row(after, 0)["can_transfer"]


def test_pairing_back_releases_the_hold(setup):
    queue, job, current = setup
    current["dest"] = B
    queue.run(Portal(B["portal"]), force=True)
    current["dest"] = dict(A, machine="renamed")  # the same portal and machine id
    assert queue.run(Portal(A["portal"]), force=True)[0]["state"] == "confirmed"


def test_transfer_is_explicit_and_audited(setup):
    queue, job, current = setup
    current["dest"] = B
    queue.run(Portal(B["portal"]), force=True)
    moved = queue.transfer(job["id"], by="tester", note="office moved portals")
    assert moved["destination"] == B and moved["held"] is None
    assert moved["upload_id"] is None and moved["received_bytes"] == 0
    [entry] = moved["transfers"]
    assert entry["from"] == A and entry["to"] == B and entry["by"] == "tester"
    assert queue.audit() == [entry]
    line = json.loads((queue.folder / "transfers.jsonl").read_text(encoding="utf-8"))
    assert line["sha256"] == job["sha256"]
    portal = Portal(B["portal"])
    assert queue.run(portal, force=True)[0]["state"] == "confirmed"
    with pytest.raises(ValueError):
        queue.transfer(job["id"])  # confirmed


def test_transfer_refuses_without_a_change_or_pairing(setup):
    queue, job, current = setup
    with pytest.raises(ValueError):
        queue.transfer(job["id"])  # already goes there
    current["dest"] = None
    with pytest.raises(ValueError):
        queue.transfer(job["id"])


def test_a_legacy_job_without_destination_is_held(setup):
    queue, job, _ = setup
    path = queue.folder / f"{job['id']}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.pop("destination")
    path.write_text(json.dumps(doc), encoding="utf-8")
    portal = Portal(A["portal"])
    after = queue.run(portal, force=True)[0]
    assert after["held"] and "before uploads were bound" in after["held"]
    assert portal.calls == []
    queue.transfer(job["id"], by="tester")
    assert queue.run(portal, force=True)[0]["state"] == "confirmed"


def test_process_refuses_a_portal_other_than_the_bound_one(setup):
    queue, job, _ = setup
    portal = Portal(B["portal"])
    after = queue.process(portal, job)
    assert after["held"] and portal.calls == []


def test_queueing_needs_a_pairing(tmp_path):
    queue = UploadQueue(tmp_path)
    source = tmp_path / "rec.zip"
    source.write_bytes(b"x")
    with pytest.raises(OSError):
        queue.add_file(source, "session-123")


def test_destination_of_settings():
    assert destination_of({}) is None
    assert destination_of({"portal": "https://a.example/", "machine_id": "1"}) is None
    got = destination_of({"portal": "https://a.example/", "machine_id": 1, "machine": "PC-A"})
    assert same_destination(got, A)


def test_the_worker_transfers_on_request(setup):
    queue, job, current = setup
    current["dest"] = B
    portal = Portal(B["portal"])
    published = []
    w = ui.UploadWorker(lambda on_change: queue, lambda: portal, lambda fn, *a: fn(*a),
                        lambda jobs, active, error: published.append((jobs, error)))
    w.queue = queue
    w.step(("run", True))
    assert published[-1][0][0]["held"] and portal.calls == []
    w.step(("transfer", job["id"]))
    assert published[-1][0][0]["state"] == "confirmed"
    w.step(("transfer", job["id"]))
    assert "not transferred" in published[-1][1]
