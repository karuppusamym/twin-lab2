"""A build runner as an OpenShift (Kubernetes) Job: one disposable pod per runner generation.

TWINLAB2_RUNNER_MODE=openshift makes the Claude and Copilot adapters (local_runner.py) start
their runner through `JobLauncher` instead of a local process. The worker's service account
creates, for generation N of build B:

- a Secret `twinlab2-build-B-gN` holding only that runner's provider key (secrets.json), owned by
  the Job so it goes when the Job does;
- a Job `twinlab2-build-B-gN` (`job_manifest`): `backoffLimit: 0` (a restart is the portal's
  decision, made as a new generation), a deadline and a TTL; its pod runs as a non-root user
  with no service account token mounted, a read-only root filesystem, no privilege escalation,
  every capability dropped and the RuntimeDefault seccomp profile. It sees the shared workspaces
  volume only at its own session folder (subPath), so neither other sessions nor the portal's
  publish clones are visible to it, and /tmp is an emptyDir.

Its network is limited by the `twinlab2-build-runner` NetworkPolicy in deploy/openshift-builder.yaml:
no ingress, and egress only to DNS and the provider's addresses. It needs no git host: the portal
clones the repository into the session folder before the Job starts and publishes the result.

The Kubernetes API is reached with the worker's in-cluster credentials (the standard service
account files), or TWINLAB2_K8S_API / TWINLAB2_K8S_TOKEN_FILE / TWINLAB2_K8S_NAMESPACE.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.request

SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
COMPONENT = "build-runner"


def job_name(build_id, generation):
    return f"twinlab2-build-{int(build_id)}-g{int(generation)}"


@dataclass
class JobSettings:
    namespace: str
    image: str
    claim: str = "twinlab2-build-workspaces"
    workspaces_mount: str = "/workspaces"
    service_account: str = "twinlab2-build-runner"
    deadline_seconds: int = 6 * 3600
    ttl_seconds: int = 3600
    cpu: str = "2"
    memory: str = "4Gi"

    @classmethod
    def from_env(cls, env=None, namespace=None):
        env = os.environ if env is None else env
        return cls(namespace=namespace or env.get("TWINLAB2_K8S_NAMESPACE") or "default",
                   image=env.get("TWINLAB2_RUNNER_IMAGE") or "twinlab2-portal:latest",
                   claim=env.get("TWINLAB2_RUNNER_CLAIM") or cls.claim,
                   workspaces_mount=env.get("TWINLAB2_WORKSPACES") or cls.workspaces_mount,
                   deadline_seconds=int(env.get("TWINLAB2_RUNNER_DEADLINE_SECONDS") or cls.deadline_seconds),
                   cpu=env.get("TWINLAB2_RUNNER_CPU") or cls.cpu, memory=env.get("TWINLAB2_RUNNER_MEMORY") or cls.memory)


def job_manifest(settings: JobSettings, build_id, generation, backend, resume=False):
    """The Job of runner generation `generation` of build `build_id`."""
    name = job_name(build_id, generation)
    labels = {"app.kubernetes.io/name": "twinlab2", "app.kubernetes.io/component": COMPONENT,
              "twinlab2/build": str(int(build_id)), "twinlab2/generation": str(int(generation))}
    session = f"session-{int(build_id)}"
    args = ["-m", "twinlab2.providers.runner", "--workspace", "/workspace", "--backend", backend,
            "--generation", str(int(generation)), "--secrets-file", "/run/twinlab2/secrets.json"]
    args += ["--resume"] if resume else []
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": settings.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": settings.deadline_seconds,
            "ttlSecondsAfterFinished": settings.ttl_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": settings.service_account,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "runner",
                        "image": settings.image,
                        "command": ["python"],
                        "args": args,
                        "workingDir": "/workspace",
                        # the allowlisted environment (sandbox.py); keys come from the secret file
                        "env": [{"name": "HOME", "value": "/workspace/home"}, {"name": "TMPDIR", "value": "/tmp"},
                                {"name": "PYTHONUNBUFFERED", "value": "1"}],
                        "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                            "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                                      "limits": {"cpu": settings.cpu, "memory": settings.memory,
                                                 "ephemeral-storage": "2Gi"}},
                        "volumeMounts": [
                            {"name": "workspace", "mountPath": "/workspace", "subPath": session},
                            {"name": "secrets", "mountPath": "/run/twinlab2", "readOnly": True},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }],
                    "volumes": [
                        {"name": "workspace", "persistentVolumeClaim": {"claimName": settings.claim}},
                        {"name": "secrets", "secret": {"secretName": name, "defaultMode": 0o400}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                    ],
                },
            },
        },
    }


def secret_manifest(settings: JobSettings, build_id, generation, secrets, owner_uid=None):
    """The runner's Secret: only the provider key, never the GitHub token."""
    name = job_name(build_id, generation)
    kept = {k: v for k, v in (secrets or {}).items() if k in ("anthropic_api_key", "copilot_token") and v}
    doc = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": name, "namespace": settings.namespace,
                        "labels": {"app.kubernetes.io/component": COMPONENT, "twinlab2/build": str(int(build_id))}},
           "data": {"secrets.json": base64.b64encode(json.dumps(kept).encode()).decode()}}
    if owner_uid:
        doc["metadata"]["ownerReferences"] = [{"apiVersion": "batch/v1", "kind": "Job", "name": name,
                                               "uid": owner_uid, "blockOwnerDeletion": False}]
    return doc


class JobLauncher:
    """Creates and deletes runner Jobs through the Kubernetes API."""

    def __init__(self, api, token, settings: JobSettings, ca_file=None):
        self.api, self.token, self.settings = api.rstrip("/"), token, settings
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        api = env.get("TWINLAB2_K8S_API")
        if not api and env.get("KUBERNETES_SERVICE_HOST"):
            api = f"https://{env['KUBERNETES_SERVICE_HOST']}:{env.get('KUBERNETES_SERVICE_PORT', '443')}"
        token_file = Path(env.get("TWINLAB2_K8S_TOKEN_FILE") or SA / "token")
        if not api or not token_file.exists():
            raise OSError("TWINLAB2_RUNNER_MODE=openshift, but no Kubernetes API is reachable from this worker "
                          "(no in-cluster service account and no TWINLAB2_K8S_API).")
        namespace_file = SA / "namespace"
        namespace = env.get("TWINLAB2_K8S_NAMESPACE") or (namespace_file.read_text().strip()
                                                           if namespace_file.exists() else None)
        ca = SA / "ca.crt"
        return cls(api, token_file.read_text().strip(), JobSettings.from_env(env, namespace),
                   ca_file=str(ca) if ca.exists() and not env.get("TWINLAB2_K8S_API") else None)

    def _call(self, method, path, body=None, ok=(200, 201, 202)):
        request = urllib.request.Request(self.api + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in ok:
                return exc.code, {}
            raise OSError(f"Kubernetes API {method} {path} -> {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"Kubernetes API not reachable: {exc.reason}") from exc

    def launch(self, build_id, generation, backend, secrets, resume=False):
        ns = self.settings.namespace
        status, job = self._call("POST", f"/apis/batch/v1/namespaces/{ns}/jobs",
                                 job_manifest(self.settings, build_id, generation, backend, resume), ok=(409,))
        uid = (job.get("metadata") or {}).get("uid")
        self._call("POST", f"/api/v1/namespaces/{ns}/secrets",
                   secret_manifest(self.settings, build_id, generation, secrets, uid), ok=(409,))
        return job_name(build_id, generation)

    def stop(self, build_id, generation):
        """Delete a generation's Job and its pod (a partitioned node may keep it running: the lease
        fences it either way)."""
        if generation < 1:
            return
        self._call("DELETE", f"/apis/batch/v1/namespaces/{self.settings.namespace}/jobs/{job_name(build_id, generation)}",
                   {"propagationPolicy": "Background"}, ok=(404,))
