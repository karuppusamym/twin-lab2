# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""The client of a lab agent's admin API (/_lab/*), from Twin Lab's `lab` module (`Agent`,
`LabError`). Verify uses it to reset a deployment's lab and compare its data with the
baseline (`twinlab2.independent`). Running labs is Twin Lab's job, not part of TwinLab2."""

from __future__ import annotations

import httpx


class LabError(Exception):
    pass


class Agent:
    """Client for one app's lab agent admin API (/_lab/*)."""

    def __init__(
        self, base_url: str, token: str = "", timeout: float = 60, holder: str | None = None
    ):
        headers: dict[str, str | bytes] = {"X-Lab-Token": token} if token else {}
        if holder:
            headers["X-Lab-Holder"] = holder.strip().encode("utf-8")  # names need not be ASCII
        self.holder = holder
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, headers=headers, timeout=timeout)

    def _call(self, method: str, path: str, **kwargs):
        try:
            r = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LabError(
                f"lab agent at {self.base} is not reachable ({exc.__class__.__name__})"
            ) from exc
        if r.status_code >= 400 and not (path == "/_lab/health" and r.status_code == 503):
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:500]
            if r.status_code in (404, 405) and detail in ({"detail": "Not Found"}, "Not Found",
                                                          {"detail": "Method Not Allowed"}):  # fmt: skip
                raise LabError(
                    "this lab runs an older lab agent that cannot do that yet: restart it "
                    "(Lab > Rebuild and restart, or Twin Lab's `lab up <app>`)"
                )
            if isinstance(detail, dict) and isinstance(detail.get("detail"), dict):
                inner = detail["detail"]
                message = inner.get("message")
                if message:  # the agent's own words, without the HTTP plumbing
                    problems = inner.get("problems") or inner.get("errors") or []
                    shown = "; ".join(
                        str(p.get("message", p)) if isinstance(p, dict) else str(p)
                        for p in problems[:10]
                    )
                    code = inner.get("code")
                    raise LabError(
                        message + (f": {shown}" if shown else "") + (f" [{code}]" if code else "")
                    )
            raise LabError(f"{method} {path} -> {r.status_code}: {detail}")
        return r.json()

    def health(self):
        return self._call("GET", "/_lab/health")

    def info(self):
        return self._call("GET", "/_lab/info")

    def data(self):
        return self._call("GET", "/_lab/data")

    def load(self, rows: dict, label: str, pack: str | None = None):
        body = {"rows": rows, "label": label, **({"pack": pack} if pack else {})}
        return self._call("POST", "/_lab/data/load", json=body)

    def baseline(self, label: str | None = None, pack: str | None = None):
        body = {k: v for k, v in (("label", label), ("pack", pack)) if v}
        return self._call("POST", "/_lab/data/baseline", json=body)

    def baselines(self):
        return self._call("GET", "/_lab/baselines")

    def baseline_rows(self) -> dict | None:
        """The rows of the current baseline ({table: rows}), or None when there is none."""
        return self._call("GET", "/_lab/data/baseline").get("tables")

    def restore_baseline(self, baseline_id: str):
        return self._call("POST", f"/_lab/baselines/{baseline_id}/restore")

    def pin_baseline(self, baseline_id: str, pinned: bool = True):
        return self._call("POST", f"/_lab/baselines/{baseline_id}/pin", json={"pinned": pinned})

    def reset(self):
        return self._call("POST", "/_lab/reset")

    def condition(self):
        return self._call("GET", "/_lab/condition")

    def set_condition(self, condition: dict | None):
        return self._call("PUT", "/_lab/condition", json=condition or {})

    def start_session(self, automation: str | None = None, label: str | None = None):
        return self._call(
            "POST", "/_lab/session/start", json={"automation": automation, "label": label}
        )

    def stop_session(self):
        return self._call("POST", "/_lab/session/stop")

    def sessions(self):
        return self._call("GET", "/_lab/sessions")

    def session(self, session_id: str):
        return self._call("GET", f"/_lab/sessions/{session_id}")

    def session_detail(self, session_id: str):
        return self._call("GET", f"/_lab/sessions/{session_id}/detail")

    def lease(self):
        return self._call("GET", "/_lab/lease").get("lease")

    def take_lease(self, holder: str, minutes: int = 60, purpose: str | None = None):
        body = {"holder": holder, "minutes": minutes, "purpose": purpose}
        return self._call("POST", "/_lab/lease", json=body)["lease"]

    def release_lease(self, force: bool = False):
        return self._call("DELETE", "/_lab/lease", json={"force": force})

    def quarantine_baseline(self, baseline_id: str, reason: str | None, release: bool = False):
        body = {"release": True} if release else {"reason": reason}
        return self._call("POST", f"/_lab/baselines/{baseline_id}/quarantine", json=body)
