"""gunicorn settings of the image (read from /app/gunicorn.conf.py, gunicorn's default config file).

Prometheus metrics across gunicorn's worker processes (observability.py): with
PROMETHEUS_MULTIPROC_DIR set, each worker writes its counters to files there and a scrape of
/metrics adds them up. The folder is emptied when gunicorn starts, and a worker's live-gauge files
are removed when it exits.
"""

import os
import shutil


def on_starting(server):
    folder = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)


def child_exit(server, worker):
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
