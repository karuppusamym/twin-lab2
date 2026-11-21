"""Resumable recording uploads from the workstation recorder (RECORDER-CONTRACT.md, Uploads).

    POST /api/uploads                    machine token: init (or resume) -> upload id + upload token
    GET  /api/uploads/<id>               upload token: state and the received offset
    PUT  /api/uploads/<id>?offset=N      upload token: one chunk through the portal
    POST /api/uploads/<id>/parts         upload token: presigned S3 part URLs (direct mode)
    POST /api/uploads/<id>/complete      upload token: verify the SHA-256, then confirm
    GET  /api/recordings/<id>/upload     signed-in user: {state, received_bytes, total_bytes, last_error}

What was received is always read back from durable storage, never only from memory: the staged
file's length (local backend) or the multipart upload's parts (S3). A portal restart in the
middle of an upload loses nothing, and the recorder resumes at the offset it is told.

On S3 the upload is a multipart upload to the artifact's final key. In `direct` mode (the
default when the recorder may reach the bucket) the recorder PUTs each part to a presigned URL;
in `portal` mode it PUTs chunks to the portal, which uploads them as parts. A chunk is exactly
`chunk_size` bytes except the last. Only after the whole object's SHA-256 matches the one given
at init is the upload confirmed and the artifact (kind `restricted_evidence`) recorded. A
mismatch rejects the upload and deletes what was received.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import secrets

from flask import abort, jsonify, request

from twinlab2 import artifacts
from twinlab2 import auth
from twinlab2_contract import recorder_contract as contract
from twinlab2 import registry

MACHINE_PATHS = ("/api/agent/", "/api/uploads")
STATES = ("receiving", "verifying", "confirmed", "rejected")
CONTENT_TYPES = {"application/zip", "application/x-ndjson", "application/json", "application/octet-stream"}

# The table is created by migrations.py (5: artifacts and uploads).


def migrate(db):
    """The `uploads` table (migration 5), with every other pending migration."""
    from twinlab2 import migrations
    migrations.run(db)


def settings():
    env = os.environ
    return {"chunk_size": int(env.get("TWINLAB2_UPLOAD_CHUNK", 8 * 1024 * 1024)),
            "token_ttl": int(env.get("TWINLAB2_UPLOAD_TOKEN_TTL", 3600)),
            "max_bytes": int(env.get("TWINLAB2_UPLOAD_MAX", 4 * 1024 ** 3))}


def _time(text):
    return datetime.fromisoformat(text)


def _stamp():
    """A creation time that orders uploads made within the same second."""
    return datetime.now(timezone.utc).isoformat()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def register(app, db):  # noqa: C901 - one place for the upload routes
    """Add the upload routes. `db()` is the request's connection."""

    def store():
        return artifacts.store()

    def agent_machine():
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not re.fullmatch(r"[A-Za-z0-9_\-]{40,100}", token):
            abort(401)
        row = db().execute("SELECT * FROM machines WHERE token_sha = ? AND revoked_at IS NULL",
                           (registry.sha(token),)).fetchone()
        if row is None:
            abort(401)
        return row

    def error(message, status, **extra):
        return jsonify(error=message, **extra), status

    def get_upload(upload_id):
        """The upload the request's upload token authorises (401/404 otherwise)."""
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        row = db().execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            abort(404)
        if not token or not secrets.compare_digest(registry.sha(token), row["token_sha"]):
            abort(401)
        if _time(row["token_expires"]) < _now():
            response = jsonify(error="the upload authorisation expired: init the upload again to resume",
                               expired=True)
            response.status_code = 401
            abort(response)
        machine = db().execute("SELECT 1 FROM machines WHERE id = ? AND revoked_at IS NULL",
                               (row["machine_id"],)).fetchone()
        if machine is None:
            abort(401)
        return row

    def update(upload_id, **fields):
        fields["updated_at"] = registry.now()
        names = ", ".join(f"{k} = ?" for k in fields)
        with db():
            db().execute(f"UPDATE uploads SET {names} WHERE id = ?", (*fields.values(), upload_id))

    # -- what has been received, from durable storage -------------------------------------------

    def s3_parts(row):
        return store().list_parts(row["object_key"], row["multipart_id"])

    def received(row):
        """(received bytes, parts) as storage has them now."""
        if row["state"] == "confirmed":
            return row["total_bytes"], []
        if row["state"] == "rejected":
            return row["received_bytes"], []
        if row["backend"] == "local":
            path = store().staging_path(row["id"])
            return (min(path.stat().st_size, row["total_bytes"]) if path.exists() else 0), []
        if row["object_version"]:  # the multipart upload is complete, verification pending
            return row["total_bytes"], []
        parts = s3_parts(row)
        sizes = {p["PartNumber"]: p["Size"] for p in parts}
        offset, number = 0, 1
        while number in sizes and (sizes[number] == row["chunk_size"] or offset + sizes[number] == row["total_bytes"]):
            offset += sizes[number]
            number += 1
        return offset, parts

    def status_of(row, live=True):
        got = received(row)[0] if live else row["received_bytes"]
        if live and got != row["received_bytes"] and row["state"] == "receiving":
            update(row["id"], received_bytes=got)
        return {"upload_id": row["id"], "recording_id": row["recording_id"], "state": row["state"],
                "received_bytes": got, "total_bytes": row["total_bytes"], "chunk_size": row["chunk_size"],
                "mode": row["mode"], "sha256": row["sha256"], "last_error": row["last_error"]}

    def fresh_token(upload_id):
        token = secrets.token_urlsafe(32)
        expires = (_now() + timedelta(seconds=settings()["token_ttl"])).isoformat()
        update(upload_id, token_sha=registry.sha(token), token_expires=expires)
        return token, expires

    def discard(row, reason):
        """Reject an upload and delete what it received."""
        if row["backend"] == "local":
            store().staging_path(row["id"]).unlink(missing_ok=True)
        elif row["object_version"]:
            store().delete_version(row["object_key"], row["object_version"])
        elif row["multipart_id"]:
            store().abort_multipart(row["object_key"], row["multipart_id"])
        update(row["id"], state="rejected", last_error=reason)

    # -- init -----------------------------------------------------------------------------------

    @app.post("/api/uploads")
    @auth.agent_token  # the machine token, then the upload's own token
    def upload_init():
        machine = agent_machine()
        doc = request.get_json(silent=True)
        if not isinstance(doc, dict):
            return error("send a JSON object", 400)
        try:
            contract.compatibility(doc.get("contract_version"))
        except contract.ContractError as refusal:
            return registry.contract_refusal(refusal)
        limits = settings()
        session_id, size, digest = doc.get("session_id"), doc.get("size"), doc.get("sha256")
        content_type = doc.get("content_type") or "application/zip"
        if not isinstance(session_id, str) or not registry.SESSION_KEY.match(session_id):
            return error("session_id must be a recorder session id (YYYYMMDDTHHMMSSZ-xxxxxxxx)", 400)
        if type(size) is not int or not 0 < size <= limits["max_bytes"]:
            return error(f"size must be an integer from 1 to {limits['max_bytes']} bytes", 400)
        if not isinstance(digest, str) or not artifacts.SHA256.match(digest):
            return error("sha256 must be 64 lowercase hex characters", 400)
        if content_type not in CONTENT_TYPES:
            return error(f"content_type must be one of {', '.join(sorted(CONTENT_TYPES))}", 400)
        recording_id = "ws-" + session_id
        owner = db().execute("SELECT machine_id FROM session_refs WHERE recording_id = ?", (recording_id,)).fetchone()
        other = db().execute("SELECT 1 FROM uploads WHERE recording_id = ? AND machine_id != ?",
                             (recording_id, machine["id"])).fetchone()
        if (owner is not None and owner["machine_id"] != machine["id"]) or other is not None:
            return error("this session belongs to another machine", 409)
        existing = db().execute(
            "SELECT * FROM uploads WHERE recording_id = ? AND machine_id = ? AND state IN ('receiving', 'verifying', 'confirmed') "
            "ORDER BY created_at DESC, id", (recording_id, machine["id"])).fetchall()
        for row in existing:
            if row["sha256"] == digest and row["total_bytes"] == size:
                token, expires = fresh_token(row["id"])
                row = db().execute("SELECT * FROM uploads WHERE id = ?", (row["id"],)).fetchone()
                return jsonify(upload_token=token, expires_at=expires, **status_of(row)), 200
        for row in existing:  # a different file for this recording replaces one still in progress
            if row["state"] != "confirmed":
                discard(row, "replaced by a newer upload of this recording")
        target = store()
        upload_id = secrets.token_urlsafe(18)
        key = artifacts.object_key("restricted_evidence", None, digest, getattr(target, "prefix", ""))
        mode, multipart = "portal", ""
        if target.backend == "s3":
            multipart = target.create_multipart(key, content_type, {"sha256": digest})
            mode = "direct" if target.direct_upload else "portal"
        token = secrets.token_urlsafe(32)
        expires = (_now() + timedelta(seconds=limits["token_ttl"])).isoformat()
        with db():
            db().execute(
                "INSERT INTO uploads(id, machine_id, recording_id, session_id, contract_version, total_bytes, sha256, "
                "content_type, mode, chunk_size, backend, object_key, multipart_id, state, token_sha, token_expires, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (upload_id, machine["id"], recording_id, session_id, doc["contract_version"], size, digest,
                 content_type, mode, limits["chunk_size"], target.backend, key, multipart, "receiving",
                 registry.sha(token), expires, _stamp(), registry.now()))
        if target.backend == "local":
            target.staging_path(upload_id).touch()
        row = db().execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return jsonify(upload_token=token, expires_at=expires, **status_of(row, live=False)), 201

    # -- progress, chunks, parts -----------------------------------------------------------------

    @app.get("/api/uploads/<upload_id>")
    @auth.agent_token  # the machine token, then the upload's own token
    def upload_status(upload_id):
        row = get_upload(upload_id)
        body = status_of(row)
        if row["mode"] == "direct" and row["state"] == "receiving":
            body["parts"] = [{"number": p["PartNumber"], "size": p["Size"]} for p in received(row)[1]]
        response = jsonify(body)
        response.headers["Upload-Offset"] = str(body["received_bytes"])
        return response

    @app.put("/api/uploads/<upload_id>")
    @auth.agent_token  # the machine token, then the upload's own token
    def upload_chunk(upload_id):
        row = get_upload(upload_id)
        if row["state"] != "receiving":
            return error(f"this upload is {row['state']}", 409, **status_of(row, live=False))
        if row["mode"] != "portal":
            return error("this upload goes straight to storage: ask for part URLs", 409)
        offset = request.args.get("offset", type=int)
        got = received(row)[0]
        if offset != got:
            return error(f"resume at offset {got}", 409, received_bytes=got, total_bytes=row["total_bytes"])
        data = request.get_data(cache=False)
        end = offset + len(data)
        if not data or end > row["total_bytes"] or (len(data) != row["chunk_size"] and end != row["total_bytes"]):
            return error(f"a chunk is {row['chunk_size']} bytes, the last one the rest", 400)
        if row["backend"] == "local":
            path = store().staging_path(row["id"])
            with path.open("r+b") as out:
                out.seek(offset)
                out.write(data)
                out.truncate()
                out.flush()
                os.fsync(out.fileno())
        else:
            store().upload_part(row["object_key"], row["multipart_id"], offset // row["chunk_size"] + 1, data)
        update(row["id"], received_bytes=end, last_error=None)
        response = jsonify(received_bytes=end, total_bytes=row["total_bytes"])
        response.headers["Upload-Offset"] = str(end)
        return response

    @app.post("/api/uploads/<upload_id>/parts")
    @auth.agent_token  # the machine token, then the upload's own token
    def upload_parts(upload_id):
        row = get_upload(upload_id)
        if row["mode"] != "direct" or row["state"] != "receiving":
            return error("part URLs are only for a direct upload still receiving", 409)
        numbers = (request.get_json(silent=True) or {}).get("parts")
        last = -(-row["total_bytes"] // row["chunk_size"])
        if not isinstance(numbers, list) or not 0 < len(numbers) <= 100 or not all(
                type(n) is int and 1 <= n <= last for n in numbers):
            return error(f"parts must list 1 to 100 part numbers from 1 to {last}", 400)
        remaining = (_time(row["token_expires"]) - _now()).total_seconds()
        ttl = int(max(60, min(900, remaining)))
        urls = {str(n): store().presign_part(row["object_key"], row["multipart_id"], n, ttl) for n in numbers}
        return jsonify(urls=urls, expires_in=ttl)

    # -- finish ---------------------------------------------------------------------------------

    @app.post("/api/uploads/<upload_id>/complete")
    @auth.agent_token  # the machine token, then the upload's own token
    def upload_complete(upload_id):
        row = get_upload(upload_id)
        if row["state"] == "confirmed":
            return jsonify(**status_of(row, live=False), artifact=artifact_body(row["artifact_id"]))
        if row["state"] == "rejected":
            return error(row["last_error"] or "this upload was rejected", 422, **status_of(row, live=False))
        got, parts = received(row)
        if got != row["total_bytes"]:
            return error(f"received {got} of {row['total_bytes']} bytes: resume at offset {got}", 409,
                         received_bytes=got, total_bytes=row["total_bytes"])
        update(row["id"], state="verifying", received_bytes=got)
        target = store()
        try:
            if row["backend"] == "local":
                path = target.staging_path(row["id"])
                with path.open("rb") as stream:
                    ref = artifacts.put(stream, kind="restricted_evidence", app_id=None,
                                        content_type=row["content_type"], sha256=row["sha256"], target=target)
            else:
                version = row["object_version"]
                if not version:
                    size = row["chunk_size"]
                    count = -(-row["total_bytes"] // size)
                    version = target.complete_multipart(row["object_key"], row["multipart_id"],
                                                        [p for p in parts if p["PartNumber"] <= count])
                    update(row["id"], object_version=version)
                ref = artifacts.ArtifactRef("s3", target.bucket, row["object_key"], version, row["sha256"],
                                            row["total_bytes"], row["content_type"], "restricted_evidence",
                                            artifacts.now())
                if not artifacts.verify(ref, target):
                    digest = hashlib.sha256()
                    with artifacts.open(ref, target) as stream:
                        while chunk := stream.read(artifacts.CHUNK):
                            digest.update(chunk)
                    raise artifacts.ChecksumMismatch(row["sha256"], digest.hexdigest())
        except artifacts.ChecksumMismatch as mismatch:
            row = db().execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
            discard(row, f"checksum mismatch: the received data has sha256 {mismatch.actual}, "
                         f"not {mismatch.expected}. Upload it again.")
            row = db().execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
            return error(row["last_error"], 422, **status_of(row, live=False))
        with db():
            ref = artifacts.save(db(), ref)
            db().execute("UPDATE uploads SET state = 'confirmed', artifact_id = ?, last_error = NULL, "
                         "received_bytes = total_bytes, updated_at = ? WHERE id = ?",
                         (ref.id, registry.now(), upload_id))
        if row["backend"] == "local":  # staged data goes only once the confirmation is stored
            target.staging_path(upload_id).unlink(missing_ok=True)
        row = db().execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return jsonify(**status_of(row, live=False), artifact=artifact_body(ref.id))

    def artifact_body(artifact_id):
        ref = artifacts.load(db(), artifact_id)
        return {"sha256": ref.sha256, "size": ref.size, "kind": ref.kind, "created_at": ref.created_at}

    # -- what the Recording details page reads ----------------------------------------------------

    @app.get("/api/recordings/<recording_id>/upload")
    @auth.signed_in
    def recording_upload(recording_id):
        known = db().execute("SELECT 1 FROM recordings WHERE id = ? UNION SELECT 1 FROM uploads WHERE recording_id = ?",
                             (recording_id, recording_id)).fetchone()
        if known is None:
            abort(404)
        if not app.extensions["twinlab_reviewer_of_recording"](recording_id):
            abort(403, "Recordings are restricted evidence.")
        row = db().execute("SELECT * FROM uploads WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1", (recording_id,)).fetchone()
        if row is None:
            if db().execute("SELECT 1 FROM recordings WHERE id = ?", (recording_id,)).fetchone() is None:
                abort(404)
            return jsonify(state="none", received_bytes=0, total_bytes=0, last_error=None)
        body = status_of(row)
        return jsonify({k: body[k] for k in ("state", "received_bytes", "total_bytes", "last_error")})
