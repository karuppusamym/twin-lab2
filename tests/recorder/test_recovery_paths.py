"""Recovery writes stay inside the session folder, whatever the capture journal says."""

import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from twinlab2_recorder.workstation import journal as capture
from twinlab2_recorder.workstation import recovery

BAD_NAMES = [
    "..", "../escape", "..\\escape", "web/../../escape", "web\\..\\..\\escape", "/abs", "\\abs",
    "C:\\Windows\\Temp\\x", "C:escape", "\\\\server\\share\\x", "web/sub", "web\\sub", "web-0", "web-01",
    "web-x", "web-2/../..", "CON", "NUL", "web.", "web ", " web", "web-profile", "journal", "desktop:ads",
    "mainframe::$DATA", "web\x00", "", ".", "web-99999", "Web", 1, 2.5, None, True, [], {}, ["web"],
    {"a": 1}, "é", "web-2\n",
]


def snapshot(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def write_journal(folder: Path, channel: str, rows) -> None:
    path = capture.path_of(folder, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


@pytest.fixture
def session(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = tmp_path / "box" / "session"
    folder.mkdir(parents=True)
    return tmp_path, folder


NAMED = [n for n in BAD_NAMES if n]  # no name at all is the channel's own recording (below)


@pytest.mark.parametrize("name", NAMED, ids=repr)
@pytest.mark.parametrize("channel", ["web", "desktop", "mainframe"])
def test_a_bad_recording_name_is_not_written(session, channel, name):
    tmp_path, folder = session
    write_journal(folder, channel, [
        {"type": "event", "event_id": "e1", "seq": 1, "action": "click", "recording": name},
        {"type": "step", "event_id": "e1", "recording": name, "step": {"seq": 1, "event_id": "e1"}},
    ])
    before = snapshot(tmp_path)
    done = recovery.rebuild(folder)
    assert snapshot(tmp_path) == before  # nothing anywhere: not inside, not outside
    assert done[channel]["recordings"] == [] and done[channel]["rejected"] == 1


@pytest.mark.parametrize("name", NAMED, ids=repr)
def test_a_bad_har_recording_name_is_not_written(session, name):
    tmp_path, folder = session
    write_journal(folder, "network", [{"type": "har", "recording": name, "entry": {"request": {}}}])
    before = snapshot(tmp_path)
    done = recovery.rebuild(folder)
    assert snapshot(tmp_path) == before
    assert done["network"]["rebuilt"] == [] and done["network"]["rejected"] == 1


def test_good_names_are_written_where_they_belong(session):
    tmp_path, folder = session
    write_journal(folder, "web", [{"type": "event", "event_id": "a", "seq": 1, "recording": "web-2"}])
    write_journal(folder, "desktop", [{"type": "event", "event_id": "b", "seq": 1}])
    write_journal(folder, "network", [{"type": "har", "recording": "web-2", "entry": {}}])
    recovery.rebuild(folder)
    assert (folder / "web-2" / "steps.jsonl").is_file()
    assert (folder / "desktop" / "steps.jsonl").is_file()
    assert (folder / "web-2" / "recording.har").is_file()


@pytest.mark.parametrize("name", [None, "", [], {}, 0], ids=repr)
def test_no_name_is_the_channels_own_recording(session, name):
    tmp_path, folder = session
    write_journal(folder, "desktop", [{"type": "event", "event_id": "b", "recording": name}])
    assert recovery.rebuild(folder)["desktop"]["recordings"] == ["desktop"]


def test_a_channel_cannot_write_another_channels_folder(session):
    tmp_path, folder = session
    write_journal(folder, "desktop", [{"type": "event", "event_id": "b", "recording": "web"}])
    assert recovery.rebuild(folder)["desktop"]["rejected"] == 1
    assert not (folder / "web").exists()


def _link(link: Path, to: Path) -> bool:
    try:
        os.symlink(to, link, target_is_directory=True)
        return True
    except OSError:
        if sys.platform != "win32":
            return False
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(to)], capture_output=True)
        return made.returncode == 0


def test_a_linked_recording_folder_is_not_followed(session):
    tmp_path, folder = session
    outside = tmp_path / "outside"
    if not _link(folder / "web", outside):
        pytest.skip("cannot make a link or junction here")
    try:
        write_journal(folder, "web", [{"type": "event", "event_id": "a", "seq": 1}])
        write_journal(folder, "network", [{"type": "har", "entry": {}}])
        done = recovery.rebuild(folder)
        assert list(outside.iterdir()) == []
        assert done["web"]["rejected"] == 1 and done["network"]["rejected"] == 1
    finally:
        os.rmdir(folder / "web")  # removes the link only


def _junk(rng: random.Random, depth: int = 0):
    pick = rng.randrange(9 if depth < 3 else 6)
    if pick == 0:
        return rng.choice(BAD_NAMES + ["web", "web-2", "desktop", "mainframe"])
    if pick == 1:
        return rng.randint(-5, 5)
    if pick == 2:
        return None
    if pick == 3:
        return rng.random()
    if pick == 4:
        return "".join(rng.choice("ab./\\:..-") for _ in range(rng.randrange(8)))
    if pick == 5:
        return rng.choice([True, False])
    if pick == 6:
        return [_junk(rng, depth + 1) for _ in range(rng.randrange(3))]
    keys = ["type", "event_id", "recording", "step", "seq", "entry", "event_ids", "origin", "tab",
            "caused_by", "locators", "ts"]  # fmt: skip
    return {rng.choice(keys): _junk(rng, depth + 1) for _ in range(rng.randrange(5))}


def _row(rng: random.Random):
    if rng.random() < 0.1:
        return rng.choice(["{", "not json", "[1, 2", "null", "7", '"text"', "[]"])
    row = {"type": rng.choice(["event", "step", "drop", "har", "other", None, 3]),
           "event_id": rng.choice(["e1", "e2", "e3", None, 4, ["e1"], ""]),
           "recording": rng.choice(BAD_NAMES + ["web", "web-2", "desktop", "mainframe"] * 3)}  # fmt: skip
    if rng.random() < 0.6:
        row["step"] = _junk(rng) if rng.random() < 0.3 else {"seq": _junk(rng), "event_id": row["event_id"],
                                                               "locators": _junk(rng), "caused_by": _junk(rng)}
    if rng.random() < 0.3:
        row["event_ids"] = _junk(rng)
    if rng.random() < 0.3:
        row["entry"] = _junk(rng)
    for key in ("seq", "origin", "tab"):
        if rng.random() < 0.3:
            row[key] = _junk(rng)
    return row


ALLOWED = {"web", "desktop", "mainframe"} | {f"web-{n}" for n in range(1, 10000)}


@pytest.mark.parametrize("seed", range(60))
def test_fuzzed_journals_never_write_outside_the_recording_folders(session, seed):
    tmp_path, folder = session
    rng = random.Random(seed)
    for channel in ("web", "desktop", "mainframe", "network"):
        write_journal(folder, channel, [_row(rng) for _ in range(rng.randrange(1, 30))])
    before = snapshot(tmp_path)
    recovery.rebuild(folder)  # must not raise
    for path in snapshot(tmp_path) - before:
        rel = Path(path)
        assert rel.parts[:2] == ("box", "session"), path
        inside = rel.parts[2:]
        assert inside[0] in ALLOWED, path
        assert len(inside) == 1 or inside[1] in ("steps.jsonl", "recording.har"), path
    assert list((tmp_path / "outside").iterdir()) == []
