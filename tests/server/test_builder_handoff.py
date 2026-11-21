"""The implementation agent must receive verified inputs with bounded file access."""

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile, ZipInfo

import pytest

from twinlab2 import packages
from twinlab2.providers import pack_input, runner


def archive(tmp_path, extra=None):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        for name, value in {"BUILD_TASK.md": b"Build the reviewed scenario",
                            "evidence.json": b'{"scenarios": []}',
                            "attachments/layout.txt": b"Approved layout", **(extra or {})}.items():
            info = ZipInfo()
            info.filename = name  # retain malicious Windows separators for the reader test
            z.writestr(info, value)
    content = buffer.getvalue()
    (tmp_path / "pack.zip").write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_materialized_pack_contains_attachments_outside_repo(tmp_path):
    digest = archive(tmp_path)
    first = pack_input.materialize(tmp_path, digest)
    assert (first / "attachments/layout.txt").read_text() == "Approved layout"
    assert not first.is_relative_to(tmp_path / "repo")
    (first / "stale.txt").write_text("not approved")
    second = pack_input.materialize(tmp_path, digest)
    assert not (second / "stale.txt").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "a\\escape", "a/../escape",
                                      "EVIDENCE.JSON", "file:stream", "trailing.", "NUL.txt", "bad?.txt"])
def test_unsafe_package_paths_are_rejected_before_extraction(tmp_path, name):
    digest = archive(tmp_path, {name: b"bad"})
    with pytest.raises(ValueError):
        pack_input.materialize(tmp_path, digest)
    assert not list(tmp_path.glob("approved-input-*"))


def test_duplicate_members_are_rejected(tmp_path):
    content = io.BytesIO()
    with ZipFile(content, "w") as z:
        z.writestr("BUILD_TASK.md", "task")
        z.writestr("evidence.json", "{}")
        with pytest.warns(UserWarning):
            z.writestr("evidence.json", "{}")
    (tmp_path / "pack.zip").write_bytes(content.getvalue())
    with pytest.raises(ValueError):
        pack_input.materialize(tmp_path, hashlib.sha256(content.getvalue()).hexdigest())


def test_extraction_limit_is_checked_before_writing(tmp_path):
    digest = archive(tmp_path)
    with mock.patch.object(pack_input, "MAX_BYTES", 1), pytest.raises(ValueError, match="limit"):
        pack_input.materialize(tmp_path, digest)
    assert not list(tmp_path.glob("approved-input-*"))


@pytest.mark.parametrize("phase", [None, "implement", "publish"])
def test_corrupt_pack_fails_before_any_provider_or_git_operation(tmp_path, phase):
    digest = archive(tmp_path)
    (tmp_path / "spec.json").write_text(json.dumps({"tag": "test", "pack_sha256": digest}))
    session = runner.Session(tmp_path, {})
    session.save(phase=phase)
    (tmp_path / "pack.zip").write_bytes(b"corrupt")
    with mock.patch.object(runner, "setup") as setup, mock.patch.object(runner, "implement") as implement, \
            mock.patch.object(runner, "publish") as publish:
        asyncio.run(runner.run(session, "copilot"))
    assert session.state["phase"] == "failed"
    assert "checksum" in session.state["error"]
    setup.assert_not_called()
    implement.assert_not_called()
    publish.assert_not_called()


def test_tool_access_scopes_relative_paths_and_inputs(tmp_path):
    repo = tmp_path / "repo"
    inputs = tmp_path / "inputs"
    repo.mkdir()
    inputs.mkdir()
    session = SimpleNamespace(repo=repo, input_dir=inputs)
    allows = lambda tool, args: runner.Session.allows(session, tool, args)
    assert allows("Write", {"file_path": "src/app.py"})
    assert allows("Read", {"file_path": str(inputs / "BUILD_TASK.md")})
    assert not allows("Write", {"file_path": str(inputs / "BUILD_TASK.md")})
    assert not allows("Read", {"file_path": "../spec.json"})
    assert not allows("Grep", {"path": str(tmp_path), "pattern": "secret"})
    assert not allows("Glob", {"pattern": "../**/*"})
    assert allows("Glob", {"pattern": "**/*.py"})
    assert not allows("Read", {})


def test_package_summary_uses_event_identity_not_display_order():
    def manifest(eid, seq):
        return {"evidence": [{"recording_id": "rec", "events": [{"event_id": eid, "seq": seq}]}]}
    summary, _ = packages.changes({}, {}, manifest("same", 1), manifest("same", 2))
    assert not any("recorded event" in line for line in summary)
    summary, _ = packages.changes({}, {}, manifest("old", 1), manifest("new", 1))
    assert "1 recorded event added" in summary
    assert "1 recorded event removed" in summary


def test_git_credentials_are_not_in_process_arguments(tmp_path):
    session = SimpleNamespace(repo=tmp_path, secrets={"github_token": "private-github-token"},
                              spec={"remote": "https://github.com/example/repo.git"})
    with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")) as call:
        assert runner._git(session, "fetch") == "ok"
    args, kwargs = call.call_args
    assert args[0] == ["git", "fetch"]
    assert kwargs["env"]["GIT_CONFIG_COUNT"] == "1"
    assert kwargs["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
