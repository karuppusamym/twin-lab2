"""Bad acknowledgements must not hang the durable queue or delete its evidence."""

import pytest

from twinlab2_recorder.client import PortalError
from twinlab2_recorder.upload_queue import UploadQueue, pack_session
from unittest import mock
from zipfile import ZipFile


class Portal:
    def __init__(self, offsets=(), chunk=4, mode="portal", conflict=False):
        self.offsets = iter(offsets)
        self.chunk, self.mode, self.conflict = chunk, mode, conflict
        self.calls = 0

    def upload_init(self, *args):
        return {"upload_id": "upload", "upload_token": "token", "expires_at": "2099-01-01T00:00:00+00:00",
                "mode": self.mode, "chunk_size": self.chunk, "received_bytes": 0, "state": "uploading"}

    def upload_status(self, *args):
        return {"state": "uploading", "received_bytes": 0}

    def upload_chunk(self, *args):
        self.calls += 1
        value = next(self.offsets)
        if self.conflict:
            raise PortalError("conflict", 409, {"received_bytes": value})
        return {"received_bytes": value}


def queued(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"0123456789")
    queue = UploadQueue(tmp_path)
    return queue, queue.add_file(source, "session-123")


@pytest.mark.parametrize("value", [-1, 11, True, None])
def test_invalid_acknowledgement_keeps_file_and_fails_job(tmp_path, value):
    queue, job = queued(tmp_path)
    portal = Portal([value])
    result = queue.process(portal, job)
    assert result["state"] == "failed"
    assert queue.file_of(job).exists()
    assert portal.calls == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_no_progress_returns_to_retry_queue(tmp_path, conflict):
    queue, job = queued(tmp_path)
    portal = Portal([0], conflict=conflict)
    result = queue.process(portal, job)
    assert result["state"] == "uploading"
    assert result["next_try"] > queue.clock()
    assert queue.file_of(job).exists()
    assert portal.calls == 1


def test_oscillating_conflicts_are_bounded(tmp_path):
    queue, job = queued(tmp_path)
    portal = Portal([4, 0, 4, 0, 4, 0], conflict=True)
    assert queue.process(portal, job)["next_try"] > queue.clock()
    assert portal.calls == 6


@pytest.mark.parametrize("chunk", [0, -1, True, 32 * 1024 * 1024])
def test_invalid_chunk_size_is_rejected(tmp_path, chunk):
    queue, job = queued(tmp_path)
    assert queue.process(Portal(chunk=chunk), job)["state"] == "failed"


def test_pack_streams_files_and_remains_deterministic(tmp_path):
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "steps.jsonl").write_bytes(b"event\n" * 1000)
    with mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("must stream")):
        pack_session(folder, tmp_path / "first.zip")
        pack_session(folder, tmp_path / "second.zip")
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    with ZipFile(tmp_path / "first.zip") as z:
        assert z.read("steps.jsonl") == b"event\n" * 1000


def test_pack_rejects_links_to_unrelated_files(tmp_path):
    folder = tmp_path / "recording"
    folder.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("not recording evidence")
    try:
        (folder / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks requires Windows privilege")
    with pytest.raises(OSError, match="linked path"):
        pack_session(folder, tmp_path / "pack.zip")
