"""Durable artifact storage: evidence, build packs, verification reports and release downloads.

An artifact is an immutable stored object. Its identity is an `ArtifactRef`: the backend, the
bucket, the object key and the object's version, with its SHA-256, size, content type and kind.
The portal database keeps refs (table `artifacts`), never the bytes and never an expiring URL;
`presign_get` makes a short-lived download URL from a ref when one is needed.

    ref = artifacts.put(data_or_stream, kind="builder_package", app_id=3, content_type="application/zip")
    ref = artifacts.save(db, ref)           # the row in `artifacts`; ref.id is its id
    with artifacts.open(ref) as stream: ...
    url = artifacts.presign_get(ref, 300)

Backends, chosen by environment variables (shown under Setup / Advanced, without credentials):

- `local` (the default, for development and tests): files under `TWINLAB2_ARTIFACT_DIR`, else
  an `artifacts` folder beside the catalog database. Every put is a new version file that is
  never overwritten. A presigned URL is a portal route with a signed, expiring token.
- `s3` (`TWINLAB2_ARTIFACT_BACKEND=s3`): boto3, for AWS or an S3-compatible store such as
  MinIO. `TWINLAB2_S3_BUCKET` (required), `TWINLAB2_S3_PREFIX`, `TWINLAB2_S3_REGION`,
  `TWINLAB2_S3_ENDPOINT_URL` (MinIO and other S3-compatible stores). Credentials come from
  boto3's usual chain (environment, profile, instance or pod role) and are never shown. The
  bucket should have versioning enabled: then a ref names one version that later writes cannot
  change.

Keys are content addressed: `<prefix><kind>/<app id or _>/<sha256>`.

Blobs that SQLite stored before this module (attachments, brief packages, release archives) are
copied here by `migrate_blobs`, which checks every copy's SHA-256 before the row points at it.
Rows not yet copied keep working from their blob (`content`); `purge_blobs` empties the blobs of
rows whose artifact verifies.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import logging
import mimetypes
import os
from pathlib import Path
import re

from twinlab2.paths import STATE
import secrets
import tempfile
import time

from twinlab2.db import state_dir  # where local files go for a database

log = logging.getLogger("twinlab2.artifacts")

KINDS = ("restricted_evidence", "builder_package", "verification_report", "release_download")
CHUNK = 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The table is created by migrations.py (5: artifacts and uploads).

# The tables whose blobs move here: (table, primary key, blob column, kind, content type,
# the query of rows not yet copied with their app id, a stored SHA-256 column to check or None)
BLOB_TABLES = (
    ("attachments", "id", "content", "restricted_evidence", None,
     "SELECT id AS pk, app_id, extension, content FROM attachments WHERE artifact_id IS NULL", None),
    ("brief_packages", "revision", "archive", "builder_package", "application/zip",
     "SELECT brief_packages.revision AS pk, briefs.app_id, archive FROM brief_packages "
     "JOIN briefs ON briefs.revision = brief_packages.revision WHERE brief_packages.artifact_id IS NULL",
     None),
    ("releases", "id", "archive", "release_download", "application/zip",
     "SELECT id AS pk, app_id, archive, archive_sha FROM releases WHERE artifact_id IS NULL", "archive_sha"),
)


class ArtifactError(Exception):
    pass


class ChecksumMismatch(ArtifactError):
    def __init__(self, expected, actual):
        super().__init__(f"checksum mismatch: expected sha256 {expected}, got {actual}")
        self.expected, self.actual = expected, actual


@dataclass(frozen=True)
class ArtifactRef:
    """One immutable stored object. `id` is its row in `artifacts` once saved."""

    backend: str
    bucket: str
    key: str
    version_id: str
    sha256: str
    size: int
    content_type: str
    kind: str
    created_at: str
    app_id: int | None = None
    id: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def object_key(kind: str, app_id, sha256: str, prefix: str = "") -> str:
    return f"{prefix}{kind}/{app_id if app_id is not None else '_'}/{sha256}"


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")


# --- backends ---------------------------------------------------------------------------------


class LocalStore:
    """Files under `root/objects/<key>/<version>`; a version file is written once and never
    overwritten. Uploads in progress are staged under `root/uploads/`."""

    backend = "local"

    def __init__(self, root, prefix: str = ""):
        self.root = Path(root)
        self.bucket = "local"
        self.prefix = prefix
        for name in ("objects", "tmp", "uploads"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        return {"backend": "local", "location": str(self.root), "versioning": "every put is a new version file",
                "direct_upload": False}

    def ping(self) -> None:
        """Raises unless the store's folder is there and writable (the portal's /readyz)."""
        objects = self.root / "objects"
        if not objects.is_dir() or not os.access(objects, os.W_OK):
            raise ArtifactError(f"{objects} is missing or not writable")

    def temp_dir(self) -> Path:
        return self.root / "tmp"

    def _path(self, key, version_id) -> Path:
        if ".." in key.split("/") or not re.fullmatch(r"[A-Za-z0-9._/-]+", key) or not re.fullmatch(r"[0-9a-f-]+", version_id):
            raise ArtifactError("invalid object key or version")
        return self.root / "objects" / key / version_id

    def commit(self, tmp_path: Path, key: str, content_type: str, sha256: str, size: int) -> str:
        version_id = f"{time.time_ns():020d}-{secrets.token_hex(4)}"
        final = self._path(key, version_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final)
        return version_id

    def open(self, ref: ArtifactRef):
        return builtins.open(self._path(ref.key, ref.version_id), "rb")

    def exists(self, ref: ArtifactRef) -> bool:
        return self._path(ref.key, ref.version_id).is_file()

    def staging_path(self, upload_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", upload_id):
            raise ArtifactError("invalid upload id")
        return self.root / "uploads" / (upload_id + ".part")


class S3Store:
    """boto3 against AWS S3 or an S3-compatible store. Expects bucket versioning."""

    backend = "s3"
    SINGLE_PUT_LIMIT = 64 * 1024 * 1024

    def __init__(self, bucket, prefix="", region=None, endpoint_url=None, client=None,
                 direct_upload=True, temp_dir=None):
        if not bucket:
            raise ArtifactError("TWINLAB2_S3_BUCKET is required for the s3 artifact backend")
        if client is None:
            import boto3
            from botocore.config import Config

            # SigV4 presigned URLs: required by current AWS regions, MinIO and other S3 stores
            client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url or None,
                                  config=Config(signature_version="s3v4"))
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.region, self.endpoint_url = region, endpoint_url
        self.direct_upload = direct_upload
        self._temp = Path(temp_dir) if temp_dir else None

    def describe(self) -> dict:
        try:
            status = self.client.get_bucket_versioning(Bucket=self.bucket).get("Status") or "not enabled"
        except Exception as error:  # noqa: BLE001 - shown on the Setup page, never raised there
            status = f"unknown ({type(error).__name__})"
        return {"backend": "s3", "bucket": self.bucket, "prefix": self.prefix or "(none)",
                "region": self.region or "(default)", "endpoint": self.endpoint_url or "AWS S3",
                "versioning": status, "direct_upload": self.direct_upload}

    def ping(self) -> None:
        """Raises unless the bucket answers (the portal's /readyz)."""
        self.client.head_bucket(Bucket=self.bucket)

    def temp_dir(self) -> Path:
        path = self._temp or Path(tempfile.gettempdir()) / "twinlab2-artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _version(value) -> str:
        return value or "null"

    def _vargs(self, ref_or_version) -> dict:
        version = ref_or_version.version_id if isinstance(ref_or_version, ArtifactRef) else ref_or_version
        return {} if version in (None, "", "null") else {"VersionId": version}

    def commit(self, tmp_path: Path, key: str, content_type: str, sha256: str, size: int) -> str:
        try:
            meta = {"sha256": sha256}
            if size <= self.SINGLE_PUT_LIMIT:
                with builtins.open(tmp_path, "rb") as body:
                    answer = self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                                    ContentType=content_type, Metadata=meta)
                return self._version(answer.get("VersionId"))
            upload = self.create_multipart(key, content_type, meta)
            parts = []
            with builtins.open(tmp_path, "rb") as body:
                number = 1
                while chunk := body.read(self.SINGLE_PUT_LIMIT):
                    parts.append({"PartNumber": number, "ETag": self.upload_part(key, upload, number, chunk)})
                    number += 1
            return self.complete_multipart(key, upload, parts)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def open(self, ref: ArtifactRef):
        return self.client.get_object(Bucket=ref.bucket, Key=ref.key, **self._vargs(ref))["Body"]

    def exists(self, ref: ArtifactRef) -> bool:
        try:
            self.client.head_object(Bucket=ref.bucket, Key=ref.key, **self._vargs(ref))
            return True
        except Exception:  # noqa: BLE001
            return False

    def presign_get(self, ref: ArtifactRef, ttl_seconds: int, filename=None) -> str:
        params = {"Bucket": ref.bucket, "Key": ref.key, "ResponseContentType": ref.content_type,
                  **self._vargs(ref)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{_safe_name(filename)}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=int(ttl_seconds))

    # multipart uploads (the resumable upload API)
    def create_multipart(self, key, content_type, metadata=None) -> str:
        return self.client.create_multipart_upload(Bucket=self.bucket, Key=key, ContentType=content_type,
                                                   Metadata=metadata or {})["UploadId"]

    def upload_part(self, key, upload_id, number, data) -> str:
        return self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                       PartNumber=number, Body=data)["ETag"]

    def presign_part(self, key, upload_id, number, ttl_seconds) -> str:
        return self.client.generate_presigned_url(
            "upload_part", Params={"Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": number},
            ExpiresIn=int(ttl_seconds))

    def list_parts(self, key, upload_id) -> list[dict]:
        parts, marker = [], 0
        while True:
            answer = self.client.list_parts(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker)
            parts += [{"PartNumber": p["PartNumber"], "ETag": p["ETag"], "Size": p["Size"]}
                      for p in answer.get("Parts", [])]
            if not answer.get("IsTruncated"):
                return parts
            marker = answer["NextPartNumberMarker"]

    def complete_multipart(self, key, upload_id, parts) -> str:
        answer = self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]})
        return self._version(answer.get("VersionId"))

    def abort_multipart(self, key, upload_id) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except Exception:  # noqa: BLE001 - already completed or aborted
            pass

    def delete_version(self, key, version_id) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key, **self._vargs(version_id))


def _safe_name(name) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name))[:120] or "download"


def from_env(database=None, environ=None):
    """The store the environment configures. `database` places the local default beside it."""
    env = os.environ if environ is None else environ
    backend = (env.get("TWINLAB2_ARTIFACT_BACKEND") or "local").strip().lower()
    prefix = (env.get("TWINLAB2_S3_PREFIX") or "").strip().strip("/")
    prefix = prefix + "/" if prefix else ""
    if backend == "local":
        root = env.get("TWINLAB2_ARTIFACT_DIR") or (
            state_dir(database) / "artifacts" if database else STATE / "artifacts")
        return LocalStore(root)
    if backend == "s3":
        return S3Store(env.get("TWINLAB2_S3_BUCKET", "").strip(), prefix=prefix,
                       region=env.get("TWINLAB2_S3_REGION") or env.get("AWS_REGION") or None,
                       endpoint_url=env.get("TWINLAB2_S3_ENDPOINT_URL") or None,
                       direct_upload=env.get("TWINLAB2_S3_DIRECT_UPLOAD", "1") != "0",
                       temp_dir=env.get("TWINLAB2_ARTIFACT_TMP") or None)
    raise ArtifactError(f"TWINLAB2_ARTIFACT_BACKEND must be local or s3, not {backend!r}")


_default = None


@dataclass
class Binding:
    """The store a running portal uses, with what signs its local download links: `secret` signs
    the token, `link(token)` is the URL of the portal's /artifacts/<token> route."""
    store: "LocalStore | S3Store"
    secret: str = ""
    link: Callable[[str], str] | None = None


_bound: ContextVar[Binding | None] = ContextVar("twinlab2_artifacts", default=None)


def bind(binding: Binding):
    """Use `binding` for the current request (or task); returns the token for `unbind`."""
    return _bound.set(binding)


def unbind(token) -> None:
    _bound.reset(token)


def configure(store) -> None:
    """The store used outside a portal request (scripts, tests)."""
    global _default
    _default = store


def store():
    found = _bound.get()
    if found is not None:
        return found.store
    global _default
    if _default is None:
        _default = from_env()
    return _default


def read_link(token: str, secret: str, target=None):
    """(ref, download name) of a signed local download link; PermissionError when it is forged or
    expired, LookupError when the object is gone."""
    from itsdangerous import BadSignature

    try:
        payload = _signer(secret).loads(token)
    except BadSignature:
        payload = None
    if not isinstance(payload, dict) or payload.get("e", 0) < time.time():
        raise PermissionError("This download link has expired: open the download again from the portal.")
    ref = ArtifactRef(**payload["r"])
    current = target or store()
    if current.backend != ref.backend or not current.exists(ref):
        raise LookupError(ref.object_key)
    return ref, payload.get("n") or None


# --- the interface ----------------------------------------------------------------------------


def _spool(data_or_stream, directory: Path):
    """Write the data to a temporary file in `directory`: (path, sha256, size)."""
    digest, size = hashlib.sha256(), 0
    handle, name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            if isinstance(data_or_stream, (bytes, bytearray, memoryview)):
                data_or_stream = io.BytesIO(bytes(data_or_stream))
            while chunk := data_or_stream.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name), digest.hexdigest(), size


def put(data_or_stream, *, kind, app_id, content_type, sha256=None, target=None) -> ArtifactRef:
    """Store bytes or a binary stream as a new immutable object. With `sha256`, the data must
    have that digest (ChecksumMismatch otherwise, and nothing is stored)."""
    _check_kind(kind)
    if sha256 is not None and not SHA256.match(str(sha256).lower()):
        raise ValueError("sha256 must be 64 hex characters")
    target = target or store()
    tmp, digest, size = _spool(data_or_stream, target.temp_dir())
    try:
        if sha256 is not None and digest != sha256.lower():
            raise ChecksumMismatch(sha256.lower(), digest)
        key = object_key(kind, app_id, digest, getattr(target, "prefix", ""))
        version = target.commit(tmp, key, content_type, digest, size)
    finally:
        tmp.unlink(missing_ok=True)
    return ArtifactRef(target.backend, target.bucket, key, version, digest, size, content_type, kind, now(),
                       app_id=app_id)


def open(ref: ArtifactRef, target=None):  # noqa: A001 - the interface's name
    """A binary stream of the object's bytes. Close it (or use `with`)."""
    target = target or store()
    if ref.backend != target.backend:
        raise ArtifactError(f"this artifact is in the {ref.backend} backend; the portal is configured for {target.backend}")
    return target.open(ref)


def read(ref: ArtifactRef, verify=True, target=None) -> bytes:
    """All the object's bytes, checked against the ref's SHA-256 and size."""
    with open(ref, target) as stream:
        data = stream.read()
    if verify:
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.sha256 or len(data) != ref.size:
            raise ChecksumMismatch(ref.sha256, digest)
    return data


def stream(ref: ArtifactRef, target=None, chunk=CHUNK):
    """The object's bytes in chunks of at most `chunk` (local file or S3 body, never read
    whole), checked against the ref's SHA-256 and size as they pass: on a mismatch the last
    chunk is withheld and ChecksumMismatch ends the download (the client sees it cut short).
    The object is opened now (in the request's binding), read as the response is sent."""
    body = open(ref, target)
    return _chunks(ref, body, chunk)


def _chunks(ref, body, chunk):
    digest, size, pending = hashlib.sha256(), 0, b""
    with closing(body):
        while data := body.read(chunk):
            if pending:
                yield pending
            digest.update(data)
            size += len(data)
            pending = data
    if digest.hexdigest() != ref.sha256 or size != ref.size:
        log.error("artifact %s/%s does not match its SHA-256: download stopped", ref.key, ref.version_id)
        raise ChecksumMismatch(ref.sha256, digest.hexdigest())
    if pending:
        yield pending


def verify(ref: ArtifactRef, target=None) -> bool:
    """Whether the stored object still has the ref's SHA-256 and size (streamed)."""
    digest, size = hashlib.sha256(), 0
    try:
        with open(ref, target) as stream:
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except Exception:  # noqa: BLE001 - a missing or unreadable object does not verify
        return False
    return digest.hexdigest() == ref.sha256 and size == ref.size


def _signer(secret):
    from itsdangerous import URLSafeSerializer

    return URLSafeSerializer(secret, salt="twinlab2-artifact-download")


def presign_get(ref: ArtifactRef, ttl_seconds: int, filename=None, target=None) -> str:
    """A URL that downloads the object for `ttl_seconds`. Never store it: store the ref."""
    target = target or store()
    ttl = max(1, min(int(ttl_seconds), 7 * 24 * 3600))
    if target.backend == "s3":
        return target.presign_get(ref, ttl, filename)
    binding = _bound.get()
    if binding is None or binding.link is None:
        raise ArtifactError("A local download link needs a running portal.")
    fields = {k: v for k, v in ref.as_dict().items() if k != "id"}
    token = _signer(binding.secret).dumps({"r": fields, "e": int(time.time()) + ttl,
                                           "n": _safe_name(filename) if filename else None})
    return binding.link(token)


# --- refs in the database ---------------------------------------------------------------------

_COLUMNS = "id, backend, bucket, object_key, version_id, sha256, size, content_type, kind, app_id, created_at"


def migrate(db) -> None:
    """The `artifacts` table and the blob tables' `artifact_id` (migration 5), with every other
    pending migration."""
    from twinlab2 import migrations
    migrations.run(db)


def columns(db, table) -> set[str]:
    """A table's column names, portably (no PRAGMA)."""
    cursor = db.execute(f"SELECT * FROM {table} LIMIT 0")
    return {item[0] for item in cursor.description}


def save(db, ref: ArtifactRef) -> ArtifactRef:
    """Insert the ref into `artifacts` (no commit) and return it with its id. Saving the same
    object version again returns the existing row."""
    row = db.execute("SELECT id FROM artifacts WHERE backend = ? AND bucket = ? AND object_key = ? AND version_id = ?",
                     (ref.backend, ref.bucket, ref.key, ref.version_id)).fetchone()
    if row is None:
        row = db.execute(
            "INSERT INTO artifacts(backend, bucket, object_key, version_id, sha256, size, content_type, kind, "
            "app_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (ref.backend, ref.bucket, ref.key, ref.version_id, ref.sha256, ref.size, ref.content_type,
             ref.kind, ref.app_id, ref.created_at)).fetchone()
    return replace(ref, id=row[0])


def load(db, artifact_id) -> ArtifactRef:
    row = db.execute(f"SELECT {_COLUMNS} FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise LookupError(f"no artifact {artifact_id}")
    (id_, backend, bucket, key, version, sha, size, ctype, kind, app_id, created) = tuple(row)
    return ArtifactRef(backend, bucket, key, version, sha, size, ctype, kind, created, app_id=app_id, id=id_)


def content(db, row, column) -> bytes:
    """A row's bytes: from its artifact when it has one (checked), else its old blob."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "artifact_id" in keys and row["artifact_id"] is not None:
        return read(load(db, row["artifact_id"]))
    return bytes(row[column])


def ref_of(db, row):
    """The row's ArtifactRef, or None for a row still on its blob."""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "artifact_id" in keys and row["artifact_id"] is not None:
        return load(db, row["artifact_id"])
    return None


def content_type_for(extension) -> str:
    return mimetypes.types_map.get(str(extension).lower(), "application/octet-stream")


def migrate_blobs(db, target=None, batch=50) -> dict:
    """Copy every blob not yet copied into the store, check the copy's SHA-256 (and a stored
    archive_sha), and only then point the row at it. Rows that fail keep their blob and are
    reported. Returns {"copied": n, "failed": [(table, pk, reason)]}. Safe to run again."""
    target = target or store()
    copied, failed = 0, []
    for table, pk, column, kind, ctype, query, sha_column in BLOB_TABLES:
        seen = set()
        while True:
            rows = [r for r in db.execute(query).fetchall() if r["pk"] not in seen][:batch]
            if not rows:
                break
            for row in rows:
                seen.add(row["pk"])
                data = bytes(row[column] or b"")
                if not data:
                    failed.append((table, row["pk"], "empty blob"))
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if sha_column and row[sha_column] and row[sha_column] != digest:
                    failed.append((table, row["pk"], f"stored {sha_column} does not match the blob"))
                    continue
                content_type = ctype or content_type_for(row["extension"])
                try:
                    ref = put(data, kind=kind, app_id=row["app_id"], content_type=content_type,
                              sha256=digest, target=target)
                    read(ref, target=target)  # the copy as stored, checked against the blob's digest
                except (ArtifactError, OSError) as error:
                    failed.append((table, row["pk"], str(error)))
                    continue
                ref = save(db, ref)
                db.execute(f"UPDATE {table} SET artifact_id = ? WHERE {pk} = ? AND artifact_id IS NULL", (ref.id, row["pk"]))
                db.commit()
                copied += 1
    return {"copied": copied, "failed": failed}


def purge_blobs(db, target=None) -> dict:
    """Empty the blob of every row whose artifact verifies. Run after migrate_blobs."""
    target = target or store()
    purged, kept = 0, []
    for table, pk, column, *_ in BLOB_TABLES:
        rows = db.execute(f"SELECT {pk} AS pk, artifact_id FROM {table} WHERE artifact_id IS NOT NULL "
                          f"AND length({column}) > 0").fetchall()
        for row in rows:
            if verify(load(db, row["artifact_id"]), target):
                db.execute(f"UPDATE {table} SET {column} = ? WHERE {pk} = ?", (b"", row["pk"]))
                db.commit()
                purged += 1
            else:
                kept.append((table, row["pk"]))
    return {"purged": purged, "kept": kept}
