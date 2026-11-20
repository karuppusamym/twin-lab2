"""The recorder's calls to the portal: pair, list offered sessions, send references.

HTTPS only (plain HTTP only for this machine: localhost, 127.0.0.1, [::1]); the system's
certificate store, and `ca_bundle` for a private CA; the system's proxy settings. There is no
switch that ignores certificates. Every answer is read as data in a fixed shape: targets are
catalog ids and identities, never paths or commands.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from agent.settings import APP, VERSION
from recorder_contract import CONTRACT_VERSION

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class PortalError(Exception):
    """`status` is the HTTP status when the portal (or storage) answered, None when it could not
    be reached; `body` the JSON answer, when there was one."""

    def __init__(self, message: str, status: int | None = None, body: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}

    @property
    def unreachable(self) -> bool:
        return self.status is None


def check_portal(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query:
        raise PortalError("give the portal address, for example https://twinlab2.apps.example.test")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise PortalError("the portal must be reached over HTTPS (HTTP only on this machine)")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class Portal:
    def __init__(self, url: str, token: str | None = None, ca_bundle: str | None = None,
                 timeout: float = 20) -> None:  # fmt: skip
        self.url = check_portal(url)
        self.token = token
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def _call(self, method: str, path: str, body: dict | None = None, *, raw: bytes | None = None,
              token: str | None = None) -> dict:  # fmt: skip
        data = json.dumps(body).encode("utf-8") if body is not None else raw
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("X-TwinLab-Recorder", f"{APP} {VERSION}")
        req.add_header("X-TwinLab-Contract", CONTRACT_VERSION)
        if data is not None:
            req.add_header("Content-Type", "application/json" if body is not None else "application/octet-stream")
        if token or self.token:
            req.add_header("Authorization", f"Bearer {token or self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as r:
                raw = r.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                answer = json.loads(exc.read(64 * 1024).decode("utf-8"))
                answer = answer if isinstance(answer, dict) else {}
            except (ValueError, AttributeError, UnicodeDecodeError):
                answer = {}
            message = answer.get("error") if isinstance(answer.get("error"), str) else None
            if exc.code == 401 and token is None:
                message = message or "this machine is not paired, or was revoked: pair it again"
            raise PortalError(message or f"the portal answered {exc.code}", exc.code, answer) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PortalError(f"the portal cannot be reached: {getattr(exc, 'reason', exc)}") from exc
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PortalError("the portal's answer is not JSON") from exc
        if not isinstance(doc, dict):
            raise PortalError("unexpected answer from the portal")
        return doc

    def pair(self, code: str, name: str) -> dict:
        doc = self._call("POST", "/api/agent/pair", {"contract_version": CONTRACT_VERSION, "code": code,
                                                     "name": name, "recorder": {"name": APP, "version": VERSION}})
        if not isinstance(doc.get("token"), str) or not isinstance(doc.get("machine_id"), int):
            raise PortalError("unexpected answer from the portal")
        return doc

    def sessions(self) -> dict:
        doc = self._call("GET", "/api/agent/sessions")
        out = []
        for s in doc.get("sessions") or []:
            if not isinstance(s, dict) or not isinstance(s.get("id"), int):
                continue
            out.append({"id": s["id"], "name": str(s.get("name") or "")[:120],
                        "automation_id": s["automation_id"] if isinstance(s.get("automation_id"), int) else None,
                        "targets": clean_targets(s.get("targets"))})  # fmt: skip
        return {"sessions": out, "deny_keys": clean_keys(doc.get("deny_keys"))}

    def apps(self) -> dict:
        doc = self._call("GET", "/api/agent/apps")
        apps = []
        for a in doc.get("apps") or []:
            if isinstance(a, dict) and isinstance(a.get("app_id"), int):
                targets = clean_targets([a.get("target")])
                if targets:
                    apps.append({"app_id": a["app_id"], "name": str(a.get("name") or "")[:120],
                                 "target": targets[0]})  # fmt: skip
        return {"apps": apps, "deny_keys": clean_keys(doc.get("deny_keys"))}

    def send(self, references: dict) -> dict:
        return self._call("POST", "/api/agent/references", references)

    # -- resumable uploads (RECORDER-CONTRACT.md, Uploads) --------------------------------------

    def upload_init(self, session_id: str, size: int, sha256: str, content_type: str = "application/zip") -> dict:
        return self._call("POST", "/api/uploads", {"contract_version": CONTRACT_VERSION, "session_id": session_id,
                                                   "size": size, "sha256": sha256, "content_type": content_type})

    def upload_status(self, upload_id: str, token: str) -> dict:
        return self._call("GET", f"/api/uploads/{quote(upload_id)}", token=token)

    def upload_chunk(self, upload_id: str, token: str, offset: int, data: bytes) -> dict:
        return self._call("PUT", f"/api/uploads/{quote(upload_id)}?offset={int(offset)}", raw=data, token=token)

    def upload_part_urls(self, upload_id: str, token: str, numbers: list[int]) -> dict:
        return self._call("POST", f"/api/uploads/{quote(upload_id)}/parts", {"parts": numbers}, token=token)

    def upload_complete(self, upload_id: str, token: str) -> dict:
        return self._call("POST", f"/api/uploads/{quote(upload_id)}/complete", {}, token=token)

    def put_part(self, url: str, data: bytes) -> str:
        """PUT one part to a presigned storage URL (HTTPS, or HTTP on this machine); its ETag."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise PortalError("the portal gave an invalid upload URL", 0)
        if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
            raise PortalError("upload URLs must be HTTPS (HTTP only on this machine)", 0)
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as r:
                return r.headers.get("ETag", "")
        except urllib.error.HTTPError as exc:
            raise PortalError(f"storage answered {exc.code} to an upload part", exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PortalError(f"storage cannot be reached: {getattr(exc, 'reason', exc)}") from exc


def clean_targets(items) -> list[dict]:
    out = []
    for t in items or []:
        if not isinstance(t, dict):
            continue
        item = {"level": t.get("level") if t.get("level") in ("full", "steps") else "full"}
        for k in ("key", "exe", "origin", "name", "tn3270"):
            if isinstance(t.get(k), str) and 0 < len(t[k]) <= 200:
                item[k] = t[k]
        if isinstance(t.get("app_id"), int):
            item["app_id"] = t["app_id"]
        if any(k in item for k in ("key", "exe", "origin", "tn3270")):
            out.append(item)
    return out


def clean_keys(items) -> list[str]:
    return [k for k in items or [] if isinstance(k, str) and 0 < len(k) <= 220]
