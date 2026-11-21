"""Adapters whose agent runs in a runner process per session (providers/runner.py): on this
machine (local mode) or as an OpenShift Job (TWINLAB2_RUNNER_MODE=openshift, openshift_job.py).

The adapter prepares the session folder (spec.json, pack.zip, and repo/: a clone made here, with
the portal's credentials, that the runner gets without any remote or token), grants the runner a
lease generation (lease.json) and starts it with an allowlisted environment (sandbox.py) and its
secrets on stdin. From then on it only reads what the runner writes (state.json, events.jsonl,
out/g<N>.bundle) and writes replies into its inbox.

Leases: every runner process holds one generation, and everything it writes carries it. Lines
and state from any other generation are rejected (counted in `Snapshot.rejected`). A runner
whose heartbeat stopped is not proof it is dead (a paused VM, a stopped process, a partitioned
node): the worker grants generation N+1 in the database (a guarded update, so one worker wins),
rewrites lease.json, and only then starts the new runner (`relaunch`). The old one, when it wakes,
finds the lease taken and stops before writing; anything it writes anyway is rejected here.

Publication: the runner pushes nothing. When it reports a commit (its bundle, for its own
generation), this adapter checks it (the bundle's checksum, the commit it names, that it descends
from the starting commit, no change to .github/workflows) and only then pushes the session branch
and opens the pull request with the portal's GitHub token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from twinlab2 import paths
from twinlab2.providers import base, sandbox

STALE_SECONDS = float(os.environ.get("TWINLAB2_RUNNER_STALE_SECONDS", 60))
MAX_RESTARTS = 2
DEFAULT_REMOTE = "https://github.com/{repo}.git"
BUNDLE = re.compile(r"^out/g(\d+)\.bundle$")


class PublishRefused(Exception):
    """The runner's commit failed a check before publication."""


def runner_mode(env=None):
    env = os.environ if env is None else env
    return "openshift" if (env.get("TWINLAB2_RUNNER_MODE") or "").strip().lower() == "openshift" else "local"


def publish_dir(workspace):
    """The portal's own clone of the session's repository: beside the session folder, never in it
    (an OpenShift runner mounts only its session folder)."""
    workspace = Path(workspace)
    root = os.environ.get("TWINLAB2_PUBLISH_ROOT")
    return (Path(root) if root else workspace.parent / ".publish") / workspace.name


class LocalRunnerProvider(base.Provider):
    backend = ""
    clarify = False

    def __init__(self, secrets, remote_template=None, github_api=None, anthropic_base_url=None, env=None,
                 launcher=None):
        self.secrets = dict(secrets)
        self.github_token = self.secrets.pop("github_token", None)  # the portal's: never given to the runner
        self.remote_template = remote_template or os.environ.get("TWINLAB2_GIT_REMOTE") or DEFAULT_REMOTE
        self.github_api = github_api
        self.anthropic_base_url = anthropic_base_url
        self.env = env
        self.mode = "openshift" if launcher is not None else runner_mode()
        self._launcher = launcher

    @property
    def isolation(self):
        return sandbox.isolation(self.mode)

    def launcher(self):
        if self._launcher is None and self.mode == "openshift":
            from twinlab2.providers.openshift_job import JobLauncher

            self._launcher = JobLauncher.from_env()
        return self._launcher

    # -- the session folder -----------------------------------------------------------------------

    @staticmethod
    def _read(path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def grant(folder, generation):
        """Write lease.json: from now on only `generation` may write the session."""
        folder = Path(folder)
        tmp = folder / f".lease.g{generation}.tmp"
        tmp.write_text(json.dumps({"generation": int(generation), "granted_at": time.time()}), encoding="utf-8")
        from twinlab2.providers.runner import replace

        replace(tmp, folder / "lease.json")

    def runner_env(self, folder):
        parent = self.env if self.env is not None else os.environ
        extra = [p for p in parent.get("PYTHONPATH", "").split(os.pathsep) if p]
        env = sandbox.child_env(parent, home=Path(folder) / "home", temp=Path(folder) / "tmp")
        env.pop("PYTHONPATH", None)
        env = paths.child_env(env)
        if extra:
            env["PYTHONPATH"] = os.pathsep.join([env["PYTHONPATH"]] + [p for p in extra if p not in env["PYTHONPATH"]])
        return env

    def _spawn(self, folder, resume=False, generation=1, build_id=None):
        folder = Path(folder)
        self.grant(folder, generation)
        args = ["--workspace", str(folder), "--backend", self.backend, "--generation", str(generation)]
        args += ["--resume"] if resume else []
        if self.mode == "openshift":
            try:
                return self.launcher().launch(build_id, generation, self.backend, self.secrets, resume=resume)
            except OSError as exc:
                raise base.ProviderError(f"Could not start the {self.label} runner Job: {exc}", transient=True) from exc
        log = open(folder / "runner.log", "ab")
        try:
            proc = subprocess.Popen([sys.executable, "-m", "twinlab2.providers.runner", *args],
                                    cwd=paths.SOURCE, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                    env=self.runner_env(folder), **sandbox.popen_options())
            proc.stdin.write((json.dumps(self.secrets) + "\n").encode())
            proc.stdin.close()
        except OSError as exc:
            raise base.ProviderError(f"Could not start the {self.label} runner: {exc}") from exc
        finally:
            log.close()
        return proc.pid

    def _git(self, *args, cwd, auth=False):
        empty = Path(cwd).parent / ".no-hooks"
        empty.mkdir(parents=True, exist_ok=True)
        command = ["git", "-c", f"core.hooksPath={empty}", "-c", "credential.helper=", "-c", "protocol.file.allow=always"]
        if auth and self.github_token and self.remote_template.startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{self.github_token}".encode()).decode()
            command += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        env = sandbox.child_env(self.env if self.env is not None else os.environ)
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        done = subprocess.run(command + list(args), cwd=cwd, env=env, capture_output=True, text=True, timeout=600,
                              **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}))
        if done.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[:500]}")
        return done.stdout.strip()

    def prepare(self, spec, state):
        folder = sandbox.fresh_workspace(Path(spec.workspace), spec.tag)
        (folder / "inbox").mkdir(parents=True, exist_ok=True)
        (folder / "pack.zip").write_bytes(spec.archive)
        remote = self.remote_template.format(repo=spec.repo)
        pub = publish_dir(folder)
        try:
            if not (pub / ".git").exists() and not (pub / "HEAD").exists():
                if pub.exists():
                    shutil.rmtree(pub)
                pub.parent.mkdir(parents=True, exist_ok=True)
                self._git("clone", "--bare", "--quiet", remote, str(pub), cwd=pub.parent, auth=True)
            if not (folder / "repo" / ".git").exists():
                if (folder / "repo").exists():
                    shutil.rmtree(folder / "repo")
                self._git("clone", "--no-hardlinks", "--no-checkout", "--quiet", str(pub), str(folder / "repo"),
                          cwd=folder)
                self._git("remote", "remove", "origin", cwd=folder / "repo")
        except RuntimeError as exc:
            raise base.ProviderError(f"Could not clone {spec.repo}: {exc}", transient=True) from exc
        doc = {"build_id": spec.build_id, "app_id": spec.app_id, "tag": spec.tag, "prompt": spec.prompt,
               "pack_sha256": spec.pack_sha256, "revision": spec.revision, "repo": spec.repo,
               "base_branch": spec.base_branch, "base_sha": spec.base_sha, "branch": spec.branch,
               "model": spec.model or self.model, "budget": spec.budget, "budget_unit": spec.budget_unit,
               "title": spec.title, "clarify": self.clarify, "anthropic_base_url": self.anthropic_base_url}
        (folder / "spec.json").write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        return {"workspace": str(folder), "isolation": self.isolation}

    def start(self, spec, state):
        folder = Path(spec.workspace)
        if (self._read(folder / "state.json") or {}).get("tag") == spec.tag:
            return self._started(spec)  # it is running already
        self._spawn(folder, generation=getattr(spec, "generation", 1), build_id=spec.build_id)
        return self._started(spec)

    def relaunch(self, build, generation):
        """Start generation `generation` (already granted in the database) with --resume."""
        if self.mode == "openshift":
            self.launcher().stop(build["id"], generation - 1)
        self._spawn(Path(build["workspace"]), resume=True, generation=generation, build_id=build["id"])

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
        generation = int(build.get("runner_generation") or 1)
        state = json.loads(build["provider_state"] or "{}")
        doc = self._read(folder / "state.json")
        if doc is None:
            raise base.ProviderError(f"The {self.label} runner has not written its state yet.", transient=True)
        cursor = int(build["event_cursor"] or 0)
        events, lines, rejected = [], [], 0
        if (folder / "events.jsonl").exists():
            lines = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
        for line in lines[cursor:]:
            try:
                e = json.loads(line)
            except ValueError:
                break  # a line being written: read it next time
            cursor += 1
            if int(e.get("gen") or 1) != generation:
                rejected += 1  # written by a runner that no longer holds the session
                continue
            events.append(base.Event(event_id=e["id"], kind=e["kind"], text=e.get("text", ""), data=dict(e.get("data") or {})))
        current = int(doc.get("generation") or 1) == generation
        snap = base.Snapshot(state=base.RUNNING, events=events, cursor=str(cursor), rejected=rejected)
        if not current:
            snap.status = "starting"
            snap.note = f"Waiting for runner generation {generation} to take over the session."
        else:
            phase = doc.get("phase")
            snap.status, snap.detail, snap.cost = phase, doc.get("waiting_for") or None, doc.get("cost")
            if phase == "done":
                return self._published(build, doc, snap, state, generation)
            if phase == "failed":
                snap.state, snap.note = base.FAILED, doc.get("error") or f"The {self.label} runner failed."
            elif phase == "cancelled":
                snap.state = "cancelled"
            elif doc.get("waiting"):
                snap.state = base.WAITING
                snap.questions = [doc.get("waiting_for") or "an answer"]
        last = float(doc.get("heartbeat") or 0) if current else 0.0
        since = max(last, float(state.get("lease_at") or 0))
        stale = time.time() - since > STALE_SECONDS
        if snap.state in (base.RUNNING, base.WAITING) and stale and not (folder / "inbox" / "cancel").exists():
            restarts = int(state.get("restarts", 0))
            if restarts >= MAX_RESTARTS:
                snap.state, snap.note = base.FAILED, (f"The {self.label} runner stopped {restarts + 1} times; "
                                                      "see runner.log in the session's workspace.")
            else:
                snap.restart = True
                snap.note = (f"The {self.label} runner stopped answering; generation {generation + 1} takes over "
                             "and continues where it was.")
        return snap

    # -- publication (the portal's, never the runner's) -------------------------------------------

    def _published(self, build, doc, snap, state, generation):
        head = doc.get("head_sha")
        if state.get("published") == head and head:
            snap.state, snap.head_sha = base.READY, head
            snap.pr_number, snap.pr_url = state.get("pr_number"), state.get("pr_url")
            return snap
        try:
            number, url = self.publish(build, doc, generation)
        except PublishRefused as exc:
            snap.state, snap.note = base.FAILED, f"Not published: {exc}"
            return snap
        except (RuntimeError, OSError, urllib.error.URLError) as exc:
            raise base.ProviderError(f"Publishing the session's branch: {exc}", transient=True) from exc
        snap.state, snap.head_sha, snap.pr_number, snap.pr_url = base.READY, head, number, url
        snap.state_update = {"published": head, "pr_number": number, "pr_url": url}
        snap.events.append(base.Event(event_id=f"published-{head[:12]}", kind="status",
                                      text=f"Checked and published {head[:12]} to {build['branch']}"
                                           + (f"; pull request #{number}." if number else ".")))
        return snap

    def publish(self, build, doc, generation):
        """Check the runner's commit report and publish it: (pull request number, url)."""
        folder = Path(build["workspace"])
        name, head = str(doc.get("bundle") or ""), str(doc.get("head_sha") or "")
        found = BUNDLE.match(name)
        if not found or int(found.group(1)) != generation:
            raise PublishRefused(f"the commit report is not from runner generation {generation}.")
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise PublishRefused("the runner reported no commit.")
        bundle = folder / name
        try:
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        except OSError:
            raise PublishRefused(f"{name} is missing.") from None
        if digest != doc.get("bundle_sha256"):
            raise PublishRefused(f"{name} does not match the checksum the runner reported.")
        spec = self._read(folder / "spec.json") or {}
        pub = publish_dir(folder)
        base_sha, branch = spec.get("base_sha") or build["base_sha"], build["branch"] or spec.get("branch")
        self._git("bundle", "verify", "--quiet", str(bundle), cwd=pub)
        ref = f"refs/twinlab2/g{generation}"
        self._git("fetch", "--quiet", "--no-tags", str(bundle), f"+refs/heads/{branch}:{ref}", cwd=pub)
        if self._git("rev-parse", ref, cwd=pub) != head:
            raise PublishRefused(f"the bundle holds another commit than the reported {head[:12]}.")
        try:
            self._git("merge-base", "--is-ancestor", base_sha, head, cwd=pub)
        except RuntimeError:
            raise PublishRefused(f"{head[:12]} does not descend from the starting commit {base_sha[:12]}.") from None
        changed = self._git("diff", "--name-only", base_sha, head, cwd=pub).splitlines()
        workflows = [p for p in changed if p.startswith(".github/workflows/")]
        if workflows and os.environ.get("TWINLAB2_BUILD_ALLOW_WORKFLOW_CHANGES") != "1":
            raise PublishRefused("the session changed the CI workflows that judge it (" + ", ".join(workflows[:5])
                                 + "); a portal admin can allow it with TWINLAB2_BUILD_ALLOW_WORKFLOW_CHANGES=1.")
        # the session branch is the session's own: a restarted session may replace its earlier commit
        self._git("push", "--quiet", "origin", f"+{head}:refs/heads/{branch}", cwd=pub, auth=True)
        return self.github_pull(spec, branch, head)

    def github_pull(self, spec, branch, head_sha):
        """Open (or find) the session's pull request; (number, url) or (None, None) without a token."""
        token, api, repo = self.github_token, self.github_api, spec.get("repo")
        if not token or not api or not repo:
            return None, None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "twinlab2",
                   "X-GitHub-Api-Version": "2022-11-28"}
        body = json.dumps({"title": spec.get("title"), "head": branch, "base": spec.get("base_branch"),
                           "body": f"TwinLab2 build session #{spec.get('build_id')}, package revision "
                                   f"{spec.get('revision')} (SHA-256 {spec.get('pack_sha256')}). Not tested: TwinLab2 "
                                   f"verifies commit {head_sha} independently.", "draft": False}).encode()
        request = urllib.request.Request(f"{api}/repos/{repo}/pulls", data=body, method="POST",
                                         headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                doc = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code != 422:  # 422: a pull request for this branch exists already
                raise RuntimeError(f"GitHub refused the pull request ({exc.code}).") from exc
            query = f"{api}/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch}"
            with urllib.request.urlopen(urllib.request.Request(query, headers=headers), timeout=30) as response:
                found = json.loads(response.read() or b"[]")
            doc = found[0] if found else {}
        return doc.get("number"), doc.get("html_url")

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
