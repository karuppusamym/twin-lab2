# The TwinLab2 portal for OpenShift: gunicorn on 127.0.0.1:8080 behind the OAuth proxy sidecar
# (deploy/openshift-portal.yaml). Runs as any non-root UID.
#
# The database is DATABASE_URL: postgresql://… for a shared deployment (pending migrations are
# applied at start). Without it the portal falls back to the SQLite file TWINLAB2_DATABASE on a
# volume at /data, which suits a single replica only.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TWINLAB2_DATABASE=/data/catalog.db TWINLAB2_TRUST_PROXY_USER=1 TWINLAB2_SECURE_COOKIES=1 \
    GUNICORN_CMD_ARGS="--workers 1 --threads 8"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "gunicorn>=23,<24"
COPY portal.py registry.py openshift.py buildpack.py scenarios.py journey.py recorder_contract.py \
     db.py migrations.py ./
COPY contract contract
COPY templates templates
COPY static static
RUN mkdir -p /data && chgrp -R 0 /data /app && chmod -R g=u /data /app

USER 1001
EXPOSE 8080
# Bound to the pod's loopback: only the OAuth proxy in the same pod reaches it, so the user it
# names (X-Forwarded-User) can be trusted. The agent API (/api/agent/) has its own token.
CMD ["gunicorn", "--bind", "127.0.0.1:8080", "portal:wsgi()"]
