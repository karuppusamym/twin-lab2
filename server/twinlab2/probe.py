"""Exec probes for the containers of deploy/openshift-portal.yaml (exit 0: healthy, 1: not).

    python -m twinlab2.probe http http://127.0.0.1:8080/livez     the portal (bound to the pod's loopback)
    python -m twinlab2.probe worker [--max-age 180]               the build worker

The portal listens on 127.0.0.1 only (its OAuth proxy sidecar is the way in), so the kubelet
cannot reach it with an httpGet probe: its liveness and startup probes run this inside the
container. Its readiness probe is an httpGet of /readyz through the proxy (as users reach it).

The worker serves nothing: it is alive while its loop, or the heartbeat of the job it runs,
still writes `workers.seen_at` (jobqueue.worker_seen) for this host. A database that does not
answer is not the worker's fault: the probe passes then (restarting the worker would not help)
and says so.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import socket
import sys
import urllib.error
import urllib.request


def http(url, timeout=3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - a fixed loopback URL
            return 200 <= response.status < 300, f"{response.status}"
    except urllib.error.HTTPError as error:
        return False, f"{error.code}"
    except OSError as error:
        return False, f"unreachable ({error})"


def worker(target, max_age=180.0, host=None) -> tuple[bool, str]:
    from twinlab2 import db as database

    host = host or socket.gethostname()
    target = str(target)
    if target.startswith(("postgresql:", "postgres:")) and "connect_timeout=" not in target:
        target += ("&" if "?" in target else "?") + "connect_timeout=5"  # a probe never hangs
    try:
        connection = database.connect(target)
    except Exception as error:  # noqa: BLE001
        return True, f"database unreachable ({type(error).__name__}): not the worker's fault"
    try:
        row = connection.execute("SELECT MAX(seen_at) FROM workers WHERE host = ? AND stopped_at IS NULL",
                                 (host,)).fetchone()
    except Exception as error:  # noqa: BLE001
        return True, f"database unreachable ({type(error).__name__}): not the worker's fault"
    finally:
        connection.close()
    seen = row[0] if row else None
    if not seen:
        return False, f"no running worker on {host} yet"
    when = datetime.strptime(seen, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - when
    if age > timedelta(seconds=max_age):
        return False, f"the worker on {host} was last seen {age.total_seconds():.0f}s ago"
    return True, f"seen {age.total_seconds():.0f}s ago"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    one = sub.add_parser("http")
    one.add_argument("url")
    one.add_argument("--timeout", type=float, default=3.0)
    two = sub.add_parser("worker")
    two.add_argument("--max-age", type=float, default=180.0)
    two.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    if args.kind == "http":
        ok, why = http(args.url, args.timeout)
    else:
        from twinlab2 import db as database

        ok, why = worker(args.database or database.from_env(), args.max_age)
    print(why, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
