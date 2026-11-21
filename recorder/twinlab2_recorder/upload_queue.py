"""The recorder's offline upload queue: recordings go to the portal when it can be reached.

A job is one file to upload for one recorded session. The queue lives in the data folder
(`upload-queue\\`): per job a JSON file (`<job>.json`, written atomically) and the file itself
(`<job>.zip`, the queue's own copy, so a recording folder changed afterwards does not change
what is sent). Because everything is on disk, the queue survives the recorder being closed, the
PC restarting and the network going away: `run()` picks up where the last run stopped.

For each job, `run()`:

1. checks the queued file still has the SHA-256 recorded when it was queued;
2. inits the upload with the portal (POST /api/uploads), which resumes an upload of the same
   file already in progress, and keeps the short-lived upload authorisation it answers;
3. asks how much the portal has (GET /api/uploads/<id>) and sends the rest, chunk by chunk,
   either to the portal or to presigned storage URLs;
4. asks the portal to complete; the portal checks the SHA-256 and only then confirms.

Each job is bound to its destination when it is queued: the portal URL and the machine identity
(the machine id the portal gave at pairing). A job is only sent while the recorder is paired with
that same portal as that same machine. After re-pairing with another portal or as another machine
the job is held (`held` says why) and is not sent anywhere until a person transfers it to the new
destination in the window (`transfer`); each transfer is written to the job (`transfers`) and to
the queue's audit log (`transfers.jsonl`). Jobs queued before destinations were recorded are held
the same way.

The queued copy is deleted only after the portal confirms. A network failure leaves the job
queued with its error and a time to try again (backing off to 10 minutes). A rejected checksum
starts the upload again from the beginning, up to MAX_ATTEMPTS; a queued file that no longer
matches its checksum fails the job, which keeps its file for a person to look at.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import getpass
import re
import shutil
import time
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from twinlab2_recorder.client import PortalError

CHUNK = 1024 * 1024
MAX_ATTEMPTS = 5
SKIP = {"stop", "paused", "sent.json"}


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def destination_of(values: dict) -> dict | None:
    """Where this recorder sends now: `{"portal", "machine_id", "machine"}` from the pairing
    settings, or None when it is not paired."""
    from twinlab2_recorder.client import check_portal

    portal, machine_id = values.get("portal"), values.get("machine_id")
    if not portal or type(machine_id) is not int:
        return None
    try:
        portal = check_portal(portal)
    except PortalError:
        return None
    return {"portal": portal.rstrip("/"), "machine_id": machine_id, "machine": values.get("machine")}


def same_destination(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and a.get("portal") == b.get("portal") and a.get("machine_id") == b.get("machine_id")


def where(dest: dict | None) -> str:
    if not dest:
        return "no recorded destination"
    name = f" ({dest['machine']})" if dest.get("machine") else ""
    return f"{dest.get('portal')} as machine {dest.get('machine_id')}{name}"


def pack_session(folder: Path, out: Path) -> Path:
    """The session folder as a deterministic ZIP (sorted names, fixed times): the same
    recording always gives the same bytes, so queueing it again resumes the same upload."""
    folder = Path(folder)
    files = []
    for p in sorted(folder.rglob("*")):
        if p.is_symlink() or p.is_junction() or not p.resolve().is_relative_to(folder.resolve()):
            raise OSError(f"recording contains a linked path: {p.relative_to(folder)}")
        if p.is_file() and p.name not in SKIP and p.suffix != ".tmp":
            files.append(p)
    tmp = out.with_suffix(".tmp")
    with ZipFile(tmp, "w", ZIP_DEFLATED) as archive:
        for path in files:
            info = ZipInfo(path.relative_to(folder).as_posix(), (2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, CHUNK)
    os.replace(tmp, out)
    return out


class UploadQueue:
    def __init__(self, data_folder: Path, clock=time.time, on_change=None, destination=None) -> None:
        self.folder = Path(data_folder) / "upload-queue"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.on_change = on_change  # called with a copy of each job saved (the window's progress)
        # the current destination (destination_of the settings), asked each time: pairing changes it
        self.destination = destination if callable(destination) else (lambda: destination)

    # -- jobs on disk ---------------------------------------------------------------------------

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z_-]{8,80}", job_id):
            raise ValueError("invalid job id")
        return self.folder / f"{job_id}.json"

    def file_of(self, job: dict) -> Path:
        return self.folder / f"{job['id']}.zip"

    def save(self, job: dict) -> dict:
        job["updated_at"] = _stamp()
        path = self._job_path(job["id"])
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(job, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if self.on_change is not None:
            self.on_change(dict(job))
        return job

    def get(self, job_id: str) -> dict | None:
        try:
            return json.loads(self._job_path(job_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def jobs(self) -> list[dict]:
        found = []
        for path in sorted(self.folder.glob("*.json")):
            job = self.get(path.stem)
            if job:
                found.append(job)
        return sorted(found, key=lambda j: j.get("queued_at", ""))

    # -- queueing ---------------------------------------------------------------------------------

    def add_file(self, source: Path, session_id: str, content_type: str = "application/zip") -> dict:
        """Queue a copy of `source` for the session. Queueing the same bytes again returns the
        job already queued (or confirmed)."""
        tmp = self.folder / f"incoming-{os.getpid()}-{time.time_ns()}.tmp"
        shutil.copyfile(source, tmp)
        return self._adopt(tmp, session_id, content_type)

    def add_session(self, folder: Path, session_id: str) -> dict:
        """Queue the recorded session's folder as a ZIP."""
        tmp = self.folder / f"incoming-{os.getpid()}-{time.time_ns()}.zip"
        pack_session(folder, tmp)
        return self._adopt(tmp, session_id, "application/zip")

    def _adopt(self, tmp: Path, session_id: str, content_type: str) -> dict:
        digest, size = sha256_of(tmp), tmp.stat().st_size
        job_id = f"{session_id}-{digest[:12]}"
        dest = self.destination()
        if dest is None:
            tmp.unlink(missing_ok=True)
            raise OSError("this recorder is not paired with a portal: pair it before uploading")
        existing = self.get(job_id)
        if existing and existing["state"] != "failed":
            tmp.unlink(missing_ok=True)
            return existing
        os.replace(tmp, self.folder / f"{job_id}.zip")
        return self.save({
            "id": job_id, "session_id": session_id, "sha256": digest, "size": size, "content_type": content_type,
            "state": "queued", "upload_id": None, "upload_token": None, "token_expires": None, "mode": None,
            "chunk_size": None, "received_bytes": 0, "attempts": 0, "rejections": 0, "last_error": None,
            "next_try": 0, "queued_at": _stamp(), "confirmed_at": None, "recording_id": None,
            "destination": dest, "held": None, "transfers": [],
        })

    # -- destinations -------------------------------------------------------------------------------

    def hold_reason(self, job: dict, current: dict | None = None) -> str | None:
        """Why the job must not be sent to the current destination, or None."""
        if job.get("state") == "confirmed":
            return None
        current = self.destination() if current is None else current
        bound = job.get("destination")
        if current is None:
            return None  # not paired: nothing is sent anyway (the run cannot make a portal)
        if not bound:
            return (f"queued before uploads were bound to a portal; transfer it to {where(current)} "
                    "to send it there")  # fmt: skip
        if not same_destination(bound, current):
            return (f"queued for {where(bound)}, but this recorder is now paired with {where(current)}: "
                    "it is not sent until you transfer it or pair back")  # fmt: skip
        return None

    def check_holds(self) -> list[dict]:
        """Mark each job held (or no longer held) against the current destination."""
        current = self.destination()
        for job in self.jobs():
            reason = self.hold_reason(job, current) if current is not None else job.get("held")
            if reason != job.get("held"):
                job["held"] = reason
                self.save(job)
        return self.jobs()

    def transfer(self, job_id: str, by: str | None = None, note: str | None = None) -> dict:
        """Send a held job to the current destination instead: a person's explicit decision,
        kept on the job and in the audit log. The upload starts again at the new portal."""
        job = self.get(job_id)
        current = self.destination()
        if job is None:
            raise ValueError("no such upload")
        if job["state"] == "confirmed":
            raise ValueError("this upload is already confirmed")
        if current is None:
            raise ValueError("this recorder is not paired: pair it first")
        if same_destination(job.get("destination"), current):
            raise ValueError("this upload already goes to the current portal")
        entry = {"at": _stamp(), "job": job_id, "session_id": job["session_id"], "sha256": job["sha256"],
                 "from": job.get("destination"), "to": current, "by": by or _who(), "note": note}  # fmt: skip
        with (self.folder / "transfers.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        job.setdefault("transfers", []).append(entry)
        job.update(destination=current, held=None, upload_id=None, upload_token=None, token_expires=None,
                   mode=None, chunk_size=None, received_bytes=0, next_try=0, last_error=None, rejections=0)
        if job["state"] == "uploading" or (job["state"] == "failed" and self.file_of(job).is_file()):
            job["state"] = "queued"
        return self.save(job)

    def audit(self) -> list[dict]:
        """The transfer log, oldest first."""
        path = self.folder / "transfers.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def retry(self, job_id: str) -> dict | None:
        """Make a job due now: a failed one whose file is still queued goes back to the queue
        (it resumes where the portal stopped; a changed file fails again), a waiting one is tried
        at the next run. A confirmed job, or one without its file, is left as it is."""
        job = self.get(job_id)
        if job is None or job["state"] == "confirmed" or self.hold_reason(job):
            return job
        if job["state"] == "failed":
            if not self.file_of(job).is_file():
                return job
            job.update(state="uploading" if job.get("upload_id") else "queued", rejections=0)
        job["next_try"] = 0
        return self.save(job)

    # -- running ------------------------------------------------------------------------------------

    def run(self, portal, only: str | None = None, force: bool = False) -> list[dict]:
        """Work through the due jobs (all of them with `force`). Returns every job afterwards.
        A held job (bound to another portal or machine) is never sent."""
        self.check_holds()
        for job in self.jobs():
            if job.get("held"):
                continue
            if job["state"] in ("confirmed", "failed") or (only and job["id"] != only and job["session_id"] != only):
                continue
            if not force and job.get("next_try", 0) > self.clock():
                continue
            self.process(portal, job)
        return self.jobs()

    def _retry_later(self, job: dict, error: Exception) -> dict:
        job["attempts"] = job.get("attempts", 0) + 1
        job["last_error"] = str(error)
        job["next_try"] = self.clock() + min(600, 5 * 2 ** min(job["attempts"], 7))
        return self.save(job)

    def _fail(self, job: dict, message: str) -> dict:
        job.update(state="failed", last_error=message)
        return self.save(job)

    def process(self, portal, job: dict) -> dict:
        bound, url = job.get("destination") or {}, getattr(portal, "url", None)
        if url is not None and bound.get("portal") != str(url).rstrip("/"):
            # held jobs never get here; this guards a caller that skips run()
            return self.save(dict(job, held=f"not sent: queued for {where(bound)}, not {getattr(portal, 'url', '?')}"))
        path = self.file_of(job)
        if not path.is_file():
            return self._fail(job, "the queued file is missing")
        if sha256_of(path) != job["sha256"]:
            return self._fail(job, "the queued file changed after it was queued: queue the recording again")
        try:
            return self._send(portal, job, path)
        except PortalError as error:
            if error.status == 422:  # the portal's checksum differs: send it all again
                job["rejections"] = job.get("rejections", 0) + 1
                job.update(upload_id=None, upload_token=None, token_expires=None, received_bytes=0)
                if job["rejections"] >= MAX_ATTEMPTS:
                    return self._fail(job, f"the portal rejected the upload {job['rejections']} times: {error}")
            elif error.status in (400, 403, 413, 426) or (
                    error.status == 409 and "received_bytes" not in error.body):  # fmt: skip
                return self._fail(job, str(error))  # a person has to act: re-pair, update, or look
            return self._retry_later(job, error)

    def _init(self, portal, job: dict) -> dict:
        answer = portal.upload_init(job["session_id"], job["size"], job["sha256"], job["content_type"])
        job.update(upload_id=answer["upload_id"], upload_token=answer["upload_token"],
                   token_expires=answer["expires_at"], mode=answer["mode"], chunk_size=answer["chunk_size"],
                   received_bytes=answer["received_bytes"], state="uploading")
        self.save(job)
        return answer

    def _token_fresh(self, job: dict) -> bool:
        if not job.get("upload_token") or not job.get("token_expires"):
            return False
        try:
            expires = datetime.fromisoformat(job["token_expires"]).timestamp()
        except ValueError:
            return False
        return expires - 60 > self.clock()

    def _send(self, portal, job: dict, path: Path) -> dict:
        if not self._token_fresh(job):
            answer = self._init(portal, job)
            if answer["state"] == "confirmed":
                return self._confirmed(job, answer, path)
        try:
            status = portal.upload_status(job["upload_id"], job["upload_token"])
        except PortalError as error:
            if error.status not in (401, 404):
                raise
            self._init(portal, job)  # expired, or the portal no longer has it: resume or start again
            status = portal.upload_status(job["upload_id"], job["upload_token"])
        if status["state"] == "confirmed":
            return self._confirmed(job, status, path)
        if status["state"] == "rejected":
            raise PortalError(status.get("last_error") or "the portal rejected the upload", 422, status)
        offset, size, chunk = status["received_bytes"], job["size"], job["chunk_size"]
        def checked_offset(value):
            if type(value) is not int or not 0 <= value <= size:
                raise PortalError("the portal returned an invalid upload offset", 400)
            return value

        checked_offset(offset)
        if type(chunk) is not int or not 0 < chunk <= 16 * 1024 * 1024:
            raise PortalError("the portal returned an invalid upload chunk size", 400)
        if job["mode"] not in ("direct", "portal"):
            raise PortalError("the portal returned an unknown upload mode", 400)
        job["received_bytes"] = offset
        with path.open("rb") as f:
            if job["mode"] == "direct":
                have = {p["number"] for p in status.get("parts", []) if p["size"] == min(chunk, size - (p["number"] - 1) * chunk)}
                missing = [n for n in range(1, -(-size // chunk) + 1) if n not in have]
                for start in range(0, len(missing), 20):
                    batch = missing[start:start + 20]
                    urls = portal.upload_part_urls(job["upload_id"], job["upload_token"], batch)["urls"]
                    for number in batch:
                        f.seek((number - 1) * chunk)
                        portal.put_part(urls[str(number)], f.read(chunk))
                        job["received_bytes"] = min(size, job.get("received_bytes", 0) + chunk)
                        self.save(job)
            else:
                conflicts = 0
                while offset < size:
                    f.seek(offset)
                    data = f.read(chunk)
                    try:
                        answer = portal.upload_chunk(job["upload_id"], job["upload_token"], offset, data)
                    except PortalError as error:
                        if error.status == 409 and isinstance(error.body.get("received_bytes"), int):
                            next_offset = checked_offset(error.body["received_bytes"])
                            conflicts += 1
                            if next_offset == offset or conflicts > MAX_ATTEMPTS:
                                raise PortalError("upload progress did not settle; retrying later") from error
                            offset = next_offset  # the portal has more or less: go there
                            continue
                        raise
                    next_offset = checked_offset(answer.get("received_bytes"))
                    if next_offset <= offset:
                        raise PortalError("the portal acknowledged no upload progress; retrying later")
                    offset = next_offset
                    conflicts = 0
                    job.update(received_bytes=offset, last_error=None)
                    self.save(job)
        answer = portal.upload_complete(job["upload_id"], job["upload_token"])
        if answer.get("state") != "confirmed":
            raise PortalError(f"the portal did not confirm the upload ({answer.get('state')})", None, answer)
        return self._confirmed(job, answer, path)

    def _confirmed(self, job: dict, answer: dict, path: Path) -> dict:
        job.update(state="confirmed", received_bytes=job["size"], last_error=None, confirmed_at=_stamp(),
                   recording_id=answer.get("recording_id"), upload_token=None)
        self.save(job)
        path.unlink(missing_ok=True)  # the portal has it, checked: the queue's copy can go
        return job


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - only for the audit line
        return "unknown"


def describe(job: dict) -> str:
    done = f"{job.get('received_bytes', 0)}/{job['size']} bytes"
    line = f"{job['session_id']}  {'held' if job.get('held') else job['state']}  {done}"
    if job.get("held"):
        return line + f"  ({job['held']})"
    if job.get("last_error"):
        line += f"  ({job['last_error']})"
    return line

