# The TwinLab2 portal for OpenShift: gunicorn on 127.0.0.1:8080 behind the OAuth proxy sidecar
# (deploy/openshift-portal.yaml). Runs as any non-root UID. Built from the repository root:
#
#   docker build -f deploy/Dockerfile -t twinlab2-portal .
#
# The database is DATABASE_URL: postgresql://… for a shared deployment (pending migrations are
# applied at start). Without it the portal falls back to the SQLite file TWINLAB2_DATABASE on a
# volume at /data, which suits a single replica only.
#
# The image has the portal and the build worker, not Verify's browser replay (server[replay] and
# Chromium): Verify reports the replay as not available here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TWINLAB2_DATABASE=/data/catalog.db TWINLAB2_STATE=/data TWINLAB2_TRUST_PROXY_USER=1 TWINLAB2_SECURE_COOKIES=1 \
    GUNICORN_CMD_ARGS="--workers 1 --threads 8"

WORKDIR /app
# the two packages the portal is made of: what it shares with the recorder, and the portal itself
COPY contract contract
COPY server server
RUN pip install --no-cache-dir ./contract "./server[serve]" && rm -rf contract server
RUN mkdir -p /data /artifacts && chgrp -R 0 /data /artifacts /app && chmod -R g=u /data /artifacts /app

USER 1001
EXPOSE 8080
# Bound to the pod's loopback: only the OAuth proxy in the same pod reaches it, so the user it
# names (X-Forwarded-User) can be trusted. The agent API (/api/agent/) has its own token.
# The build worker runs as a second container from this image: ["python", "-m", "twinlab2.worker"],
# with the same DATABASE_URL.
CMD ["gunicorn", "--bind", "127.0.0.1:8080", "twinlab2.portal:wsgi()"]
