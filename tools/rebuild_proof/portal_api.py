"""A portal started by the proof, and the owner's calls on /api/v1.

Each case gets its own catalog (`<evidence>/<case>/portal/`), started with
``python -m twinlab2.portal --port 8796 --database ...`` from this checkout. The portal serves only
this machine (no password, no local users), so the owner is the portal's `local` admin.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

import httpx

from common import EVIDENCE, PORTAL_PORT, port_free, spawn, wait_http

URL = f"http://127.0.0.1:{PORTAL_PORT}"


class Portal:
    def __init__(self, case: str) -> None:
        self.case = case
        self.folder = EVIDENCE / case / "portal"
        self.proc: subprocess.Popen | None = None
        self.http = httpx.Client(base_url=URL, timeout=120)

    def start(self) -> "Portal":
        if not port_free(PORTAL_PORT):
            raise SystemExit(f"port {PORTAL_PORT} is taken: stop the other portal first")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.proc = spawn(["python", "-m", "twinlab2.portal", "--port", str(PORTAL_PORT),
                           "--database", str(self.folder / "catalog.db")],
                          self.folder / "portal.log",
                          extra_env={"TWINLAB2_STATE": str(self.folder / "state"),
                                     "TWINLAB2_DEV_WORKER": "0", "TWINLAB2_PASSWORD": "",
                                     "DATABASE_URL": ""})
        wait_http(URL + "/api/v1/session", 90)
        # the web app's form token (a session cookie keeps it), sent on every change
        token = self.http.get("/api/v1/session").json().get("csrf_token")
        self.http.headers["X-CSRF-Token"] = token or ""
        return self

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.http.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- calls ------------------------------------------------------------------------------------

    def call(self, method: str, path: str, ok=(200, 201), **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            raise SystemExit(f"{method} {path} -> {r.status_code} {r.text[:1500]}")
        if r.content and "json" in r.headers.get("content-type", ""):
            return r.json()
        return r.content

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)

    def patch(self, path, **kw):
        return self.call("PATCH", path, **kw)

    # -- the owner's setup ------------------------------------------------------------------------

    def app(self, name: str, purpose: str, identity: str) -> int:
        app_id = self.post("/api/v1/apps", json={"name": name, "purpose": purpose})["id"]
        self.post(f"/api/v1/apps/{app_id}/identities", json={"key": identity})
        return app_id

    def pairing_code(self) -> str:
        return self.post("/api/v1/machines/pairing-codes")["code"]

    def request_recording(self, name: str, app_ids: list[int]) -> dict:
        machine = self.get("/api/v1/machines")["machines"][0]["id"]
        return self.post("/api/v1/capture-sessions", json={
            "name": name, "machine_id": machine, "targets": [{"app_id": a, "level": "full"} for a in app_ids]})

    def attach(self, app_id: int, path: Path, title: str, kind: str, share: str = "reviewed") -> dict:
        with open(path, "rb") as f:
            return self.post(f"/api/v1/apps/{app_id}/attachments",
                             files={"file": (path.name, f.read(), "application/octet-stream")},
                             data={"title": title, "kind": kind, "share": share})

    def wait_recording(self, timeout=60) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            page = self.get("/api/v1/recordings")
            rows = page.get("recordings") if isinstance(page, dict) else page
            if rows:
                return rows[0]
            time.sleep(1)
        raise SystemExit("no recording arrived at the portal")
