"""Adapters whose agent runs here, in an isolated runner process per session (providers/runner.py).

The adapter writes the session folder (spec.json, pack.zip), starts the runner detached with
the secrets on its stdin, and from then on only reads what the runner writes (state.json,
events.jsonl) and writes replies into its inbox. A start is found again by the session's tag in
state.json, so an uncertain start is never repeated; a runner whose heartbeat stopped is started
again with --resume (at most MAX_RESTARTS times), and it continues from its own state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from twinlab2 import paths
from twinlab2.providers import base

STALE_SECONDS = float(os.environ.get("TWINLAB2_RUNNER_STALE_SECONDS", 60))
MAX_RESTARTS = 2
DEFAULT_REMOTE = "https://github.com/{repo}.git"


class LocalRunnerProvider(base.Provider):
    backend = ""
    clarify = False

    def __init__(self, secrets, remote_template=None, github_api=None, anthropic_base_url=None, env=None):
        self.secrets = secrets
        self.remote_template = remote_template or os.environ.get("TWINLAB2_GIT_REMOTE") or DEFAULT_REMOTE
        self.github_api = github_api
        self.anthropic_base_url = anthropic_base_url
        self.env = env

    # -- the session folder -----------------------------------------------------------------------

    @staticmethod
    def _read(path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _spawn(self, folder, resume=False):
        log = open(folder / "runner.log", "ab")
        flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
                 if sys.platform == "win32" else {"start_new_session": True})
        env = dict(self.env if self.env is not None else os.environ)
        for name in list(env):
            if name.startswith(("DEVIN_", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "TWINLAB2_GITHUB_TOKEN",
                                "DATABASE_URL", "TWINLAB2_PASSWORD")):
                env.pop(name)
        env = paths.child_env(env)
        try:
            proc = subprocess.Popen([sys.executable, "-m", "twinlab2.providers.runner", "--workspace", str(folder),
                                     "--backend", self.backend] + (["--resume"] if resume else []),
                                    cwd=paths.SOURCE, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, **flags)
            proc.stdin.write((json.dumps(self.secrets) + "\n").encode())
            proc.stdin.close()
        except OSError as exc:
            raise base.ProviderError(f"Could not start the {self.label} runner: {exc}") from exc
        finally:
            log.close()
        return proc.pid

    def prepare(self, spec, state):
        folder = Path(spec.workspace)
        (folder / "inbox").mkdir(parents=True, exist_ok=True)
        (folder / "pack.zip").write_bytes(spec.archive)
        remote = self.remote_template.format(repo=spec.repo)
        doc = {"build_id": spec.build_id, "app_id": spec.app_id, "tag": spec.tag, "prompt": spec.prompt,
               "pack_sha256": spec.pack_sha256, "revision": spec.revision, "repo": spec.repo, "remote": remote,
               "base_branch": spec.base_branch, "base_sha": spec.base_sha, "branch": spec.branch,
               "model": spec.model or self.model, "budget": spec.budget, "budget_unit": spec.budget_unit,
               "title": spec.title, "clarify": self.clarify, "github_api": self.github_api,
               "anthropic_base_url": self.anthropic_base_url}
        (folder / "spec.json").write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        return {"workspace": str(folder)}

    def start(self, spec, state):
        folder = Path(spec.workspace)
        if (self._read(folder / "state.json") or {}).get("tag") == spec.tag:
            return self._started(spec)  # it is running already
        self._spawn(folder)
        return self._started(spec)

    def _started(self, spec):
        return base.Started(session_id=spec.tag, status="running", detail=f"{self.label} runner in {spec.workspace}")

    def find_started(self, spec, state):
        """A runner that started writes its tag in state.json at once."""
        for _ in range(10):
            doc = self._read(Path(spec.workspace) / "state.json")
            if doc and doc.get("tag") == spec.tag:
                return [self._started(spec)]
            time.sleep(0.2)
        return []

    # -- reading ----------------------------------------------------------------------------------

    def read(self, build):
        folder = Path(build["workspace"])
        doc = self._read(folder / "state.json")
        if doc is None:
            raise base.ProviderError(f"The {self.label} runner has not written its state yet.", transient=True)
        cursor = int(build["event_cursor"] or 0)
        events, lines = [], []
        if (folder / "events.jsonl").exists():
            lines = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
        for line in lines[cursor:]:
            try:
                e = json.loads(line)
            except ValueError:
                break  # a line being written: read it next time
            data = dict(e.get("data") or {})
            events.append(base.Event(event_id=e["id"], kind=e["kind"], text=e.get("text", ""), data=data))
            cursor += 1
        phase = doc.get("phase")
        snap = base.Snapshot(state=base.RUNNING, status=phase, detail=doc.get("waiting_for") or None,
                             events=events, cursor=str(cursor), cost=doc.get("cost"), branch=None,
                             head_sha=doc.get("head_sha"), pr_number=doc.get("pr_number"), pr_url=doc.get("pr_url"))
        if phase == "done":
            snap.state = base.READY
        elif phase == "failed":
            snap.state, snap.note = base.FAILED, doc.get("error") or f"The {self.label} runner failed."
        elif phase == "cancelled":
            snap.state = "cancelled"
        elif doc.get("waiting"):
            snap.state = base.WAITING
            snap.questions = [doc.get("waiting_for") or "an answer"]
        stale = time.time() - float(doc.get("heartbeat") or 0) > STALE_SECONDS
        if snap.state in (base.RUNNING, base.WAITING) and stale and not (folder / "inbox" / "cancel").exists():
            state = json.loads(build["provider_state"] or "{}")
            restarts = int(state.get("restarts", 0))
            if restarts >= MAX_RESTARTS:
                snap.state, snap.note = base.FAILED, (f"The {self.label} runner stopped {restarts + 1} times; "
                                                      "see runner.log in the session's workspace.")
            else:
                self._spawn(folder, resume=True)
                snap.state_update = {"restarts": restarts + 1}
                snap.note = f"The {self.label} runner had stopped; it was started again and continues where it was."
        return snap

    # -- writing to the runner ---------------------------------------------------------------------

    def reply(self, build, message):
        inbox = Path(build["workspace"]) / "inbox"
        if not inbox.is_dir():
            raise base.ProviderError(f"The {self.label} session has no workspace.", gone=True)
        doc = {"id": message["id"], "kind": message["kind"], "body": message["body"],
               "data": json.loads(message.get("data") or "{}")}
        tmp = inbox / f".{message['id']}.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        from twinlab2.providers.runner import replace
        replace(tmp, inbox / f"{message['id']}.json")  # the runner acknowledges it once it used it

    def interrupt(self, build):
        (Path(build["workspace"]) / "inbox" / "interrupt").write_text("1", encoding="utf-8")

    def cancel(self, build, spec=None):
        folder = Path(build["workspace"] or (spec.workspace if spec else ""))
        if not folder or not (folder / "inbox").is_dir():
            return []
        (folder / "inbox" / "cancel").write_text("1", encoding="utf-8")
        return [build["provider_session_id"] or build["idempotency_tag"]]
