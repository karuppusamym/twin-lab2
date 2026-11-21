"""The owner's package readiness of each application, kept between requests.

A recording's workboard shows, per application, whether its current draft package would pass
(`pack_validator`) and whether it equals the latest frozen revision. Working that out builds the
whole draft (`buildpack.draft`: evidence, scenarios, attachments, the demo target, redaction,
validation, the archive), and a workboard of 50 applications over a 2,000-event recording would
do it 50 times on every load. This module keeps each application's result: the draft's sha256
and its critical and important gap counts, or the error that stopped the draft.

An entry is dropped when something that goes into the draft changes:

- explicitly, by the writes that change it (`forget(db, app_id)`, `forget_recording(db, id)`,
  `forget_all(db)`): an assignment, a scenario, an attachment, the application's setup or scope,
  a recording arriving or being resent, a merge. Called inside the write's transaction, the
  entry is dropped at once and again when the transaction commits, so a workboard that read the
  old rows meanwhile cannot keep them;
- and, as a safety net for writes this process does not see (the worker, another portal
  process, a script on the database), by a stamp of the rows a draft reads (`stamps`): the
  application's row, its assignments' revisions and sizes, its scenarios' revisions, its
  attachments, identities and recorded flows, and the size of its recordings. A stamp that
  differs from the one the entry was made with is a miss.

The frozen revision it is compared with is read on every request (cheap); only the draft is
cached. Entries are per database (`Connection.target`) and per process.
"""

from __future__ import annotations

import hashlib
import json
import threading

_lock = threading.Lock()
_entries: dict[tuple[str, int], tuple[str, int, dict]] = {}  # (target, app_id) -> (stamp, generation, value)
_generation: dict[tuple[str, int], int] = {}
LIMIT = 10_000  # entries kept; past it the cache starts again (a catalog is far smaller)
stats = {"hits": 0, "misses": 0}


def _drop(target, app_ids):
    with _lock:
        for app_id in app_ids:
            key = (target, int(app_id))
            _entries.pop(key, None)
            _generation[key] = _generation.get(key, 0) + 1


def _drop_all(target):
    with _lock:
        for key in [k for k in _entries if k[0] == target]:
            del _entries[key]
        for key in [k for k in _generation if k[0] == target]:
            _generation[key] += 1
        _generation[(target, -1)] = _generation.get((target, -1), 0) + 1


def forget(db, *app_ids):
    """The applications' drafts changed (or may have): recompute them next time."""
    app_ids = [a for a in app_ids if a is not None]
    target = getattr(db, "target", None)
    if not app_ids or target is None:  # not a portal Connection (a script's sqlite3): nothing cached
        return
    _drop(target, app_ids)
    db.after_commit(lambda: _drop(target, app_ids))


def forget_recording(db, recording_id):
    """A recording changed: every application assigned to it or recorded in it."""
    ids = [r[0] for r in db.execute("SELECT app_id FROM evidence WHERE recording_id = ? UNION "
                                    "SELECT app_id FROM recording_apps WHERE recording_id = ?",
                                    (recording_id, recording_id))]
    forget(db, *ids)


def forget_all(db):
    target = getattr(db, "target", None)
    if target is None:
        return
    _drop_all(target)
    db.after_commit(lambda: _drop_all(target))


def _generation_of(key):
    return _generation.get(key, 0), _generation.get((key[0], -1), 0)


def _put(rows, stamps, column, digest_of):
    for row in rows:
        stamps.setdefault(row[column], []).append(digest_of(row))


def stamps(db, app_ids):
    """{app_id: a digest of the rows its draft is made from}, in a few queries for all of them."""
    app_ids = sorted({int(a) for a in app_ids})
    if not app_ids:
        return {}
    marks = ",".join("?" * len(app_ids))
    parts: dict[int, list] = {}
    args = tuple(app_ids)
    _put(db.execute(f"SELECT * FROM apps WHERE id IN ({marks})", args), parts, "id",
         lambda r: ["app", *[str(v) for v in r]])
    _put(db.execute(f"""SELECT app_id, recording_id, revision, reviewed, length(selection), length(notes)
                        FROM evidence WHERE app_id IN ({marks}) ORDER BY recording_id""", args), parts, "app_id",
         lambda r: ["evidence", *r[1:]])
    _put(db.execute(f"""SELECT evidence.app_id, recordings.id, length(recordings.events)
                        FROM evidence JOIN recordings ON recordings.id = evidence.recording_id
                        WHERE evidence.app_id IN ({marks}) ORDER BY recordings.id""", args), parts, "app_id",
         lambda r: ["recording", *r[1:]])
    _put(db.execute(f"""SELECT app_id, recording_id, key, length(flow), length(screens) FROM recording_apps
                        WHERE app_id IN ({marks}) ORDER BY recording_id, key""", args), parts, "app_id",
         lambda r: ["flow", *r[1:]])
    _put(db.execute(f"""SELECT app_id, id, revision, position, length(events) FROM scenarios
                        WHERE app_id IN ({marks}) ORDER BY id""", args), parts, "app_id",
         lambda r: ["scenario", *r[1:]])
    _put(db.execute(f"""SELECT app_id, id, share, kind, extension, title, artifact_id FROM attachments
                        WHERE app_id IN ({marks}) ORDER BY id""", args), parts, "app_id",
         lambda r: ["attachment", *r[1:]])
    _put(db.execute(f"""SELECT app_id, key, surface, facts FROM app_identities
                        WHERE app_id IN ({marks}) ORDER BY key""", args), parts, "app_id",
         lambda r: ["identity", *r[1:]])
    return {app_id: hashlib.sha256(json.dumps(parts.get(app_id, []), default=str).encode()).hexdigest()
            for app_id in app_ids}


def get(db, app_id, stamp, compute, keep=lambda value: True):
    """The cached value of `app_id` made with `stamp`, else `compute()`: kept when `keep(value)`
    and the application was not forgotten while it ran."""
    key = (db.target, int(app_id))
    with _lock:
        generation = _generation_of(key)
        entry = _entries.get(key)
        if entry is not None and entry[0] == stamp and entry[1] == generation:
            stats["hits"] += 1
            return entry[2]
        stats["misses"] += 1
    value = compute()
    if not keep(value):
        return value
    with _lock:
        if _generation_of(key) == generation:
            if len(_entries) >= LIMIT:
                _entries.clear()
            _entries[key] = (stamp, generation, value)
    return value


def clear():
    """Everything (tests)."""
    with _lock:
        _entries.clear()
        _generation.clear()
        stats.update(hits=0, misses=0)
