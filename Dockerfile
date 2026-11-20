# The TwinLab2 portal for OpenShift: gunicorn on 127.0.0.1:8080 behind the OAuth proxy sidecar
# (deploy/openshift-portal.yaml), the catalog on a volume at /data. Runs as any non-root UID.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TWINLAB2_DATABASE=/data/catalog.db TWINLAB2_TRUST_PROXY_USER=1 TWINLAB2_SECURE_COOKIES=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "gunicorn>=23,<24"
COPY portal.py registry.py openshift.py buildpack.py scenarios.py journey.py recorder_contract.py \
     event_ids.py completeness.py independent.py verification.py \
     builds.py builds_web.py devin.py github.py jobqueue.py worker.py twinlab.lock.json ./
COPY contract contract
COPY agent/__init__.py agent/twinlab.py agent/
# The Devin client is Twin Lab's, from the pinned commit, checked against the lock's digest.
RUN python -m agent.twinlab fetch
COPY templates templates
COPY static static
RUN mkdir -p /data && chgrp -R 0 /data /app && chmod -R g=u /data /app

USER 1001
EXPOSE 8080
# Bound to the pod's loopback: only the OAuth proxy in the same pod reaches it, so the user it
# names (X-Forwarded-User) can be trusted. The agent API (/api/agent/) has its own token.
# The build worker runs as a second container from this image: ["python", "worker.py"].
CMD ["gunicorn", "--bind", "127.0.0.1:8080", "--workers", "1", "--threads", "8", "portal:wsgi()"]
