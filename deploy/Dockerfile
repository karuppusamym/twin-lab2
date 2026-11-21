# The TwinLab2 portal for OpenShift: one ASGI app (FastAPI) under gunicorn with uvicorn workers on
# 127.0.0.1:8080, behind the OAuth proxy sidecar (deploy/openshift-portal.yaml). It serves the
# JSON API (/api/v1), the recorder's agent API (/api/agent, /api/uploads), the downloads and the
# web app (web/, built in the first stage). Runs as any non-root UID (OpenShift's restricted SCC).
# Built from the repository root:
#
#   docker build -f deploy/Dockerfile -t twinlab2-portal .
#
# The database is DATABASE_URL: postgresql://… for a shared deployment (pending migrations are
# applied at start). Without it the portal falls back to the SQLite file TWINLAB2_DATABASE on a
# volume at /data, which suits a single replica only.
#
# The image has the portal and the build worker, not Verify's browser replay (server[replay] and
# Chromium): Verify reports the replay as not available here.

# -- 1: the web app (web/dist) -------------------------------------------------------------------
# Base images pinned by digest (Dependabot proposes new digests: .github/dependabot.yml).
FROM node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json web/.npmrc* ./
RUN npm ci --no-audit --no-fund
COPY api /src/api
COPY web /src/web
RUN npm run build

# -- 2: the portal ----------------------------------------------------------------------------------
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TWINLAB2_DATABASE=/data/catalog.db TWINLAB2_STATE=/data TWINLAB2_AUTH_MODE=trusted-proxy TWINLAB2_SECURE_COOKIES=1 \
    TWINLAB2_WEB_DIST=/app/web/dist TWINLAB2_LOG_FORMAT=json PROMETHEUS_MULTIPROC_DIR=/tmp/twinlab2-metrics \
    GUNICORN_CMD_ARGS="--workers 2 --worker-class uvicorn_worker.UvicornWorker --timeout 120 --forwarded-allow-ips=0.0.0.0"
# trusted-proxy: the user comes only from the OAuth proxy's headers, and only on requests whose peer
# is in TWINLAB2_TRUSTED_PROXIES (default loopback: the sidecar). `--forwarded-allow-ips=0.0.0.0`
# (an address no peer has) keeps uvicorn from replacing that peer with the proxy's X-Forwarded-For.
# TWINLAB2_SECRET_KEY is required: the portal refuses to start without it.

# Debian's security fixes released since the base image was built (the supply-chain workflow's image
# scan fails on a fixable high or critical finding: docs/security-supply-chain.md)
RUN apt-get update && apt-get -y upgrade --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# the two packages the portal is made of: what it shares with the recorder, and the portal itself
COPY contract contract
COPY server server
RUN pip install --no-cache-dir ./contract "./server[serve,otel]" && rm -rf contract server
COPY --from=web /src/web/dist /app/web/dist
# gunicorn's default config file (cwd): multiprocess Prometheus metrics across its workers
COPY deploy/gunicorn.conf.py /app/gunicorn.conf.py
# any UID in group 0 (OpenShift runs the container as an arbitrary UID with GID 0) may write here
RUN mkdir -p /data /artifacts && chgrp -R 0 /data /artifacts /app && chmod -R g=u /data /artifacts /app

USER 1001
EXPOSE 8080 9090
# Bound to the pod's loopback: only the OAuth proxy in the same pod reaches it, so the user it
# names (X-Forwarded-User) can be trusted. The agent API has its own machine token.
# Probes: /livez (the process), /readyz (database at the current migration, artifact store), both
# answered whatever the Host; inside the container `python -m twinlab2.probe http <url>`.
# The build worker is its own Deployment from this image: ["python", "-m", "twinlab2.worker"], with
# the same DATABASE_URL and TWINLAB2_SECRET_KEY (probe: `python -m twinlab2.probe worker`).
# 9090 answers GET /metrics only (observability.py): Prometheus scrapes it on the pod's IP; the
# Route never reaches it. Logs are JSON lines (TWINLAB2_LOG_FORMAT); tracing is opt-in (TWINLAB2_OTEL=1).
CMD ["gunicorn", "--bind", "127.0.0.1:8080", "--bind", "0.0.0.0:9090", "twinlab2.app:asgi()"]
