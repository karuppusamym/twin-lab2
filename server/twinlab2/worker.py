"""The TwinLab2 build worker: works the job queue in the catalog database.

    python -m twinlab2.worker                         the catalog at TWINLAB2_DATABASE or .state/catalog.db
    python -m twinlab2.worker --database <path> [--once] [--lease 60]

Run one. Each job is leased; a heartbeat thread extends the lease while the job runs. When this
process dies mid-job its lease runs out and the next worker (this one restarted, or another)
takes the job over; build jobs are written so that taking one over never repeats a Devin
session create (see builds.py). Verify runs here too (`verify` jobs, verifications.py): a
verification taken over starts again from the start. Setup, Build worker shows the workers seen and the queue.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import threading
import traceback

from twinlab2 import builds
from twinlab2 import db as database
from twinlab2 import jobqueue
from twinlab2 import verifications

TYPES = ("build", "build.cancel", "build.message", "build.interrupt", verifications.JOB)
log = logging.getLogger("twinlab2.worker")


class Heartbeat(threading.Thread):
    """Extends a job's lease every third of it, on its own connection, until stopped."""

    def __init__(self, database, job_id, owner, lease):
        super().__init__(daemon=True)
        self.database, self.job_id, self.owner, self.lease = database, job_id, owner, lease
        self.halt = threading.Event()
        self.lost = False

    def run(self):
        db = builds.connect(self.database)
        try:
            while not self.halt.wait(max(0.05, self.lease / 3)):
                if not jobqueue.heartbeat(db, self.job_id, self.owner, self.lease):
                    self.lost = True
                    return
                jobqueue.worker_seen(db, self.owner, current_job=self.job_id)
        finally:
            db.close()


def work_one(db, database, job, owner, lease, ctx):
    if job["attempts"] > jobqueue.MAX_ATTEMPTS:
        error = job.get("error") or "its lease kept running out"
        if job["type"] == verifications.JOB:
            verifications.give_up(db, job, error)
        else:
            builds.give_up(db, job, error)
        jobqueue.fail(db, job["id"], owner, f"gave up after {job['attempts'] - 1} attempts: {error}")
        return
    beat = Heartbeat(database, job["id"], owner, lease)
    beat.start()
    try:
        if job["type"] == verifications.JOB:
            outcome = verifications.handle(db, job, ctx)
        else:
            outcome = builds.handle(db, job, ctx)
    except Exception as exc:  # a bug or an unexpected failure: try later, counted
        log.error("job %s (%s) failed:\n%s", job["id"], job["type"], traceback.format_exc())
        db.rollback()
        jobqueue.reschedule(db, job["id"], owner, min(300, 5 * 2 ** job["attempts"]),
                            error=f"{type(exc).__name__}: {exc}", keep_attempts=True)
        return
    finally:
        beat.halt.set()
        beat.join()
    if beat.lost:
        log.warning("job %s was taken over by another worker while it ran", job["id"])
        return
    if outcome.done:
        if outcome.error:
            jobqueue.fail(db, job["id"], owner, outcome.error)
        else:
            jobqueue.finish(db, job["id"], owner, outcome.result)
    else:
        jobqueue.reschedule(db, job["id"], owner, outcome.delay, error=outcome.error, keep_attempts=outcome.count)


def run(database, once=False, lease=jobqueue.LEASE_SECONDS, idle=1.0, ctx=None, stop=None, max_jobs=None,
        owner=None, types=TYPES):
    """Work jobs until `stop` is set; with once=True, until nothing is due (or max_jobs ran).
    `types` limits the job types this worker takes."""
    db = builds.connect(database)
    builds.migrate(db)
    ctx = ctx or builds.Context()
    stop = stop or threading.Event()
    owner = owner or jobqueue.worker_id()
    jobqueue.worker_seen(db, owner, started=True)
    done = 0
    try:
        while not stop.is_set():
            job = jobqueue.claim(db, owner, lease, types)
            if job is None:
                if once:
                    break
                jobqueue.worker_seen(db, owner)
                stop.wait(idle)
                continue
            jobqueue.worker_seen(db, owner, current_job=job["id"])
            work_one(db, database, job, owner, lease, ctx)
            done += 1
            if max_jobs is not None and done >= max_jobs:
                break
    finally:
        jobqueue.worker_seen(db, owner, stopped=True)
        db.close()
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", default=database.from_env(),
                        help="a DATABASE_URL (sqlite:///…, postgresql://…) or a SQLite file (default: DATABASE_URL)")
    parser.add_argument("--once", action="store_true", help="work what is due, then exit")
    parser.add_argument("--lease", type=float, default=jobqueue.LEASE_SECONDS, help="lease seconds per job")
    parser.add_argument("--idle", type=float, default=1.0, help="seconds between looks at an empty queue")
    parser.add_argument("--types", default=",".join(TYPES),
                        help="job types to take, comma-separated (e.g. verify: a worker for Verify only)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("worker on %s", database.redacted(args.database))
    try:
        types = tuple(t.strip() for t in args.types.split(",") if t.strip()) or TYPES
        run(args.database, once=args.once, lease=args.lease, idle=args.idle, types=types)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
