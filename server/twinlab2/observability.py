"""Metrics, structured logs and (opt-in) tracing for the portal and the worker.

Metrics (Prometheus text format, `GET /metrics`):

- per process: `twinlab2_http_requests_total{method,route,status}`,
  `twinlab2_http_request_duration_seconds{method,route}` (a histogram), `twinlab2_upload_bytes_total`
  (bytes received by the portal's upload routes). `route` is the route *template*
  (`/api/v1/apps/{app_id}`), never the raw path: no ids or tokens in label values.
- from the catalog at scrape time (the same for every pod, whichever answers): job queue depth
  and the age of the oldest due job per type (`twinlab2_jobs{type,state}`,
  `twinlab2_job_oldest_due_seconds{type}`), worker job outcomes (`twinlab2_job_outcomes{type,state}`:
  jobs done or failed), workers seen recently, Verify results
  (`twinlab2_verifications{state,tested}`), stored bytes per artifact kind, and retention runs.

`/metrics` is never public. It answers on the internal metrics port only
(`TWINLAB2_METRICS_PORT`, default 9090, which gunicorn binds next to the portal's loopback port;
the Route never reaches it), or on any port with `Authorization: Bearer <TWINLAB2_METRICS_TOKEN>`.
Anything else gets 404, as if there were no such page.

Under gunicorn with several workers, set `PROMETHEUS_MULTIPROC_DIR` (the image does): every
worker process writes its counters there and a scrape adds them up (deploy/gunicorn.conf.py
removes the files of a worker that exits).

Logs: `TWINLAB2_LOG_FORMAT=json` (the image's default) writes one JSON object per line with `ts`,
`level`, `logger`, `msg`, `request_id`, `user` and `app_id` (when the request names one), and for
the request line `method`, `route`, `status`, `ms`. Messages go through `scrub`: bearer tokens,
passwords in URLs, signed download tokens and `token=`/`secret=` query values are replaced. The
portal never logs evidence content, request bodies or headers.

Tracing: off unless `TWINLAB2_OTEL=1` (or `OTEL_EXPORTER_OTLP_ENDPOINT` is set) and the
`opentelemetry-sdk` package is installed (`pip install "./server[otel]"`). Each request is a
server span named `METHOD route-template` (W3C `traceparent` from the caller is continued), each
worker job a span `job <type>`. Spans go to the OTLP/HTTP exporter (`OTEL_EXPORTER_OTLP_*`
variables), or to the console with `TWINLAB2_OTEL_EXPORTER=console`.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import re
import time

log = logging.getLogger("twinlab2.observability")

# -- request context (logs) -----------------------------------------------------------------------

user_var: ContextVar[str | None] = ContextVar("twinlab2_log_user", default=None)
app_var: ContextVar[int | None] = ContextVar("twinlab2_log_app", default=None)

_SCRUB = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(token|secret|password|passwd|sig|signature|x-amz-signature|x-amz-credential)=([^&\s]+)"),
     r"\1=[redacted]"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[redacted]@"),
    (re.compile(r"/artifacts/[A-Za-z0-9._-]{20,}"), "/artifacts/[token]"),
    (re.compile(r"/api/uploads/[A-Za-z0-9_-]{16,}"), "/api/uploads/[id]"),
]


def scrub(text: str) -> str:
    """`text` with credentials and signed tokens replaced (log messages, paths)."""
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields: request_id, user, app_id, and the request line's
    method, route, status, ms (RequestLog passes them)."""

    FIELDS = ("method", "route", "status", "ms", "job_id", "job_type", "outcome")

    def format(self, record):
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": scrub(record.getMessage())}
        rid = getattr(record, "request_id", None)
        if rid and rid != "-":
            out["request_id"] = rid
        from twinlab2 import rest

        in_user, in_app = rest.log_context()
        user = getattr(record, "user", None) or in_user or user_var.get()
        if user:
            out["user"] = user
        app_id = getattr(record, "app_id", None) or in_app or app_var.get()
        if app_id is not None:
            out["app_id"] = app_id
        for name in self.FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                out[name] = value
        if record.exc_info:
            out["error"] = scrub(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False, default=str)


class TextScrubFilter(logging.Filter):
    """For the plain text format: the same scrubbing of the message."""

    def filter(self, record):
        message = record.getMessage()
        cleaned = scrub(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def configure_logging(level=None, fmt=None):
    """The root logger for the portal or the worker: JSON (TWINLAB2_LOG_FORMAT=json) or text."""
    from twinlab2 import rest

    level = level or os.environ.get("TWINLAB2_LOG_LEVEL", "INFO")
    fmt = (fmt or os.environ.get("TWINLAB2_LOG_FORMAT", "text")).strip().lower()
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(rest.RequestIdFilter())
        if fmt == "json":
            handler.setFormatter(JsonFormatter())
        else:
            handler.addFilter(TextScrubFilter())
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))


# -- metrics ------------------------------------------------------------------------------------

_metrics = None


class Metrics:
    """The process's Prometheus metrics (prometheus_client)."""

    BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

    def __init__(self):
        from prometheus_client import Counter, Histogram

        self.requests = Counter("twinlab2_http_requests", "HTTP requests answered",
                                ["method", "route", "status"])
        self.duration = Histogram("twinlab2_http_request_duration_seconds", "Time to answer an HTTP request",
                                  ["method", "route"], buckets=self.BUCKETS)
        self.upload_bytes = Counter("twinlab2_upload_bytes", "Recording upload bytes received by the portal")


def metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        folder = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        if folder:  # gunicorn.conf.py makes it; a portal started another way still counts
            os.makedirs(folder, exist_ok=True)
        _metrics = Metrics()
    return _metrics


def observe_request(method, route, status, seconds):
    try:
        m = metrics()
        m.requests.labels(method, route, str(status)).inc()
        m.duration.labels(method, route).observe(seconds)
    except Exception:  # noqa: BLE001 - metrics never fail a request
        log.debug("metrics", exc_info=True)


def count_upload_bytes(n):
    try:
        metrics().upload_bytes.inc(max(0, int(n)))
    except Exception:  # noqa: BLE001
        log.debug("metrics", exc_info=True)


WORKER_FRESH = 180  # seconds: a worker seen within this is alive (as the worker probe)


class CatalogCollector:
    """Gauges read from the catalog at scrape time: the queue, job outcomes, workers, Verify,
    stored bytes, retention. A database that does not answer gives `twinlab2_catalog_up 0`."""

    def __init__(self, target):
        self.target = target

    def collect(self):
        from prometheus_client.core import GaugeMetricFamily

        from twinlab2 import db as database
        from twinlab2 import jobqueue

        up = GaugeMetricFamily("twinlab2_catalog_up", "Whether the catalog database answered this scrape")
        try:
            connection = database.connect(self.target)
        except Exception:  # noqa: BLE001
            up.add_metric([], 0)
            yield up
            return
        try:
            now = datetime.now(timezone.utc)
            jobs = GaugeMetricFamily("twinlab2_jobs", "Jobs in the queue by type and state", labels=["type", "state"])
            outcomes = GaugeMetricFamily("twinlab2_job_outcomes", "Jobs that ended, by type and outcome (done, failed)",
                                         labels=["type", "state"])
            for kind, state, count in connection.execute("SELECT type, state, COUNT(*) FROM jobs GROUP BY type, state"):
                jobs.add_metric([kind, state], count)
                if state in ("done", "failed"):
                    outcomes.add_metric([kind, state], count)
            oldest = GaugeMetricFamily("twinlab2_job_oldest_due_seconds",
                                       "Age of the oldest queued job that is due, by type (0 when none)", labels=["type"])
            due = jobqueue.stamp(now)
            for kind, first in connection.execute(
                    "SELECT type, MIN(run_after) FROM jobs WHERE state = 'queued' AND run_after <= ? GROUP BY type", (due,)):
                oldest.add_metric([kind], max(0.0, (now - _parse(first)).total_seconds()))
            stuck = GaugeMetricFamily("twinlab2_jobs_lease_expired", "Running jobs whose lease ran out (a worker died)")
            stuck.add_metric([], connection.execute("SELECT COUNT(*) FROM jobs WHERE state = 'running' AND lease_until < ?",
                                                    (due,)).fetchone()[0])
            workers = GaugeMetricFamily("twinlab2_workers_alive", f"Workers seen within {WORKER_FRESH} s")
            fresh = jobqueue.stamp(datetime.fromtimestamp(now.timestamp() - WORKER_FRESH, timezone.utc))
            workers.add_metric([], connection.execute(
                "SELECT COUNT(*) FROM workers WHERE seen_at >= ? AND stopped_at IS NULL", (fresh,)).fetchone()[0])
            verify = GaugeMetricFamily("twinlab2_verifications", "Verify requests by state and result",
                                       labels=["state", "tested"])
            for state, tested, count in connection.execute(
                    "SELECT state, tested, COUNT(*) FROM verifications GROUP BY state, tested"):
                verify.add_metric([state, "none" if tested is None else str(bool(tested)).lower()], count)
            stored = GaugeMetricFamily("twinlab2_artifact_bytes", "Bytes of stored artifacts by kind", labels=["kind"])
            objects = GaugeMetricFamily("twinlab2_artifacts", "Stored artifacts by kind", labels=["kind"])
            for kind, count, size in connection.execute(
                    "SELECT kind, COUNT(*), COALESCE(SUM(size), 0) FROM artifacts WHERE deleted_at IS NULL GROUP BY kind"):
                objects.add_metric([kind], count)
                stored.add_metric([kind], size)
            uploads = GaugeMetricFamily("twinlab2_uploads_confirmed_bytes", "Bytes of confirmed recording uploads")
            uploads.add_metric([], connection.execute(
                "SELECT COALESCE(SUM(total_bytes), 0) FROM uploads WHERE state = 'confirmed'").fetchone()[0])
            retention = GaugeMetricFamily("twinlab2_retention_last_run_timestamp_seconds",
                                          "When the last applied (not dry-run) retention run finished")
            last = connection.execute("SELECT MAX(finished_at) FROM retention_runs WHERE dry_run = 0 AND state = 'done'"
                                      ).fetchone()[0]
            retention.add_metric([], _parse(last).timestamp() if last else 0)
            connection.commit()
            up.add_metric([], 1)
            yield from (up, jobs, outcomes, oldest, stuck, workers, verify, objects, stored, uploads, retention)
        except Exception:  # noqa: BLE001 - a scrape answers, it does not crash
            log.warning("metrics: reading the catalog failed", exc_info=True)
            up.add_metric([], 0)
            yield up
        finally:
            connection.close()


class StoreCollector:
    """Free space of a local artifact store's file system (the full-disk alert)."""

    def __init__(self, store):
        self.store = store

    def collect(self):
        import shutil

        from prometheus_client.core import GaugeMetricFamily

        root = getattr(self.store, "root", None)
        if root is None:
            return
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            return
        free = GaugeMetricFamily("twinlab2_artifact_store_free_bytes", "Free bytes where local artifacts are kept")
        free.add_metric([], usage.free)
        total = GaugeMetricFamily("twinlab2_artifact_store_size_bytes", "Size of the file system of local artifacts")
        total.add_metric([], usage.total)
        yield free
        yield total


def _parse(text):
    text = str(text).replace("Z", "+00:00")
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def exposition(target, store) -> tuple[bytes, str]:
    """(body, content type) of a scrape: this process's metrics (or every gunicorn worker's, in
    multiprocess mode) and the catalog's."""
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

    metrics()  # registered even before the first request
    registry = CollectorRegistry()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.MultiProcessCollector(registry)
    else:
        registry = _Union(REGISTRY, registry)
    registry.register(CatalogCollector(target))
    registry.register(StoreCollector(store))
    return generate_latest(registry), CONTENT_TYPE_LATEST


class _Union:
    """The process registry plus a scrape's own collectors, without registering those globally."""

    def __init__(self, base, extra):
        self.base, self.extra = base, extra

    def register(self, collector):
        self.extra.register(collector)

    def collect(self):
        yield from self.base.collect()
        yield from self.extra.collect()


METRICS_PATH = "/metrics"


def metrics_port() -> int | None:
    value = os.environ.get("TWINLAB2_METRICS_PORT", "9090").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def metrics_allowed(scope) -> bool:
    """A scrape on the internal metrics port, or with the metrics bearer token."""
    import hmac

    port = metrics_port()
    server = scope.get("server") or (None, None)
    if port is not None and server[1] == port:
        return True
    token = os.environ.get("TWINLAB2_METRICS_TOKEN", "").strip()
    if not token:
        return False
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            sent = value.decode("latin-1")
            return sent.startswith("Bearer ") and hmac.compare_digest(sent[7:].strip(), token)
    return False


class MetricsEndpoint:
    """ASGI middleware (outside the host check): `GET /metrics` for an allowed scrape; on the
    metrics port nothing else is served (404)."""

    def __init__(self, app, target, store):
        self.app, self.target, self.store = app, target, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        port = metrics_port()
        on_port = port is not None and (scope.get("server") or (None, None))[1] == port
        if scope.get("path") == METRICS_PATH and scope.get("method") in ("GET", "HEAD") and metrics_allowed(scope):
            import anyio

            body, ctype = await anyio.to_thread.run_sync(exposition, self.target, self.store)
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", ctype.encode()), (b"cache-control", b"no-store"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body if scope["method"] == "GET" else b""})
            return
        if on_port:
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"not found\n"})
            return
        return await self.app(scope, receive, send)


# -- tracing (opt-in) -----------------------------------------------------------------------------

_tracer = None
_tracing_checked = False


def tracing_enabled() -> bool:
    return os.environ.get("TWINLAB2_OTEL", "").strip() == "1" or bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def tracer(service="twinlab2-portal", exporter=None):
    """The OpenTelemetry tracer, set up once; None when tracing is off or not installed."""
    global _tracer, _tracing_checked
    if _tracing_checked and exporter is None:
        return _tracer
    _tracing_checked = True
    if not tracing_enabled() and exporter is None:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    except ImportError:
        log.warning("TWINLAB2_OTEL is set but opentelemetry-sdk is not installed: no traces "
                    "(pip install \"./server[otel]\")")
        _tracer = None
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service)}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("TWINLAB2_OTEL_EXPORTER", "otlp") == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            log.warning("opentelemetry-exporter-otlp-proto-http is not installed: no traces exported")
            return None
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    _tracer = provider.get_tracer("twinlab2")
    return _tracer


def reset_tracing():
    """For tests: look at the environment again on the next `tracer()`."""
    global _tracer, _tracing_checked
    _tracer, _tracing_checked = None, False


def extract_context(scope):
    try:
        from opentelemetry.propagate import extract
    except ImportError:
        return None
    carrier = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
    return extract(carrier)


class Timer:
    def __init__(self):
        self.started = time.monotonic()

    @property
    def seconds(self):
        return time.monotonic() - self.started
