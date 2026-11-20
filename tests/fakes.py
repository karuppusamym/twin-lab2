"""Local stand-ins for the Devin v3 API and the GitHub REST API, for the build tests.

Both listen on 127.0.0.1 only. Nothing here talks to the real services, and nothing a test does
can reach them: the tests point DEVIN_BASE_URL and TWINLAB2_GITHUB_API at these servers.

FakeDevin's sessions move only when a test says so (`work`, `ask`, `finish`), and its create
can be told what to do on each call (`create_plan`):

- "ok": create and answer.
- "slow": create, then answer only after `slow_seconds` (longer than the client's timeout).
- "fail_after_create": create, then answer 502 (the session exists).
- "fail": answer 502 without creating anything.
- "hold": create, then wait for `release` before answering (a worker can be killed meanwhile).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit


class _Server:
    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                outer.dispatch(self, "GET")

            def do_POST(self):
                outer.dispatch(self, "POST")

            def do_DELETE(self):
                outer.dispatch(self, "DELETE")

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that gave up (a timeout under test) is expected

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.lock = threading.RLock()
        self.requests = []

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeDevin(_Server):
    def __init__(self, key="cog_fake_key", org="org-test"):
        super().__init__()
        self.key, self.org = key, org
        self.sessions = {}
        self.attachments = {}
        self.create_plan = []
        self.create_calls = 0
        self.slow_seconds = 3.0
        self.release = threading.Event()
        self.received = threading.Event()
        self.answers_404_for_messages = False
        self.message_plan = []  # per message POST: "ok", "slow" (arrives, answers late), "fail_after" (arrives, 502)

    # --- what tests do ---

    def only_session(self):
        assert len(self.sessions) == 1, f"{len(self.sessions)} sessions"
        return next(iter(self.sessions.values()))

    def work(self, sid):
        with self.lock:
            self.sessions[sid].update(status="running", status_detail="working")

    def ask(self, sid, question):
        with self.lock:
            s = self.sessions[sid]
            s.update(status="running", status_detail="waiting_for_user")
            s["messages"].append({"source": "devin", "message": question})

    def finish(self, sid, pr=None):
        with self.lock:
            s = self.sessions[sid]
            s.update(status="running", status_detail="finished")
            if pr:
                s["pull_requests"] = [{"pr_url": f"https://github.com/{s['repos'][0]}/pull/{pr}", "pr_state": "open"}]

    # --- the API ---

    def view(self, s):
        return {k: s[k] for k in ("session_id", "url", "status", "status_detail", "tags", "created_at",
                                  "pull_requests", "title", "acus_consumed")}

    def dispatch(self, h, method):
        url = urlsplit(h.path)
        body = h._body()
        with self.lock:
            self.requests.append((method, url.path))
        if h.headers.get("Authorization") != f"Bearer {self.key}":
            return h._send(401, {"title": "Unauthorized", "detail": "Invalid API key"})
        base = f"/v3/organizations/{self.org}"
        if not url.path.startswith(base):
            return h._send(404, {"title": "Not Found"})
        path = url.path[len(base):]
        if method == "POST" and path == "/attachments":
            aid = secrets.token_hex(6)
            self.attachments[aid] = body
            return h._send(200, {"attachment_id": aid, "name": "pack.zip", "url": f"{self.url}/_att/{aid}"})
        if method == "POST" and path == "/sessions":
            return self.create(h, json.loads(body))
        if method == "GET" and path == "/sessions":
            query = parse_qs(url.query)
            tags = query.get("tags", [])
            with self.lock:
                items = [self.view(s) for s in self.sessions.values() if all(t in s["tags"] for t in tags)]
            return h._send(200, {"items": items, "has_next_page": False})
        match = re.fullmatch(r"/sessions/(devin-[0-9a-f]+)(/messages)?", path)
        if not match:
            return h._send(404, {"title": "Not Found"})
        s = self.sessions.get(match.group(1))
        if s is None:
            return h._send(404, {"title": "Not Found", "detail": "no such session"})
        if match.group(2):
            if method == "GET":
                if self.answers_404_for_messages:
                    return h._send(404, {"title": "Not Found"})
                return h._send(200, {"items": list(s["messages"])})
            text = json.loads(body).get("message")
            with self.lock:
                s["messages"].append({"source": "user", "message": text})
                if s["status_detail"] == "waiting_for_user":
                    s.update(status="running", status_detail="working")
                mode = self.message_plan.pop(0) if self.message_plan else "ok"
            self.received.set()
            if mode == "slow":
                threading.Event().wait(self.slow_seconds)
            if mode == "fail_after":
                return h._send(502, {"title": "Bad Gateway", "detail": "upstream timed out"})
            return h._send(200, self.view(s))
        if method == "GET":
            return h._send(200, self.view(s))
        if method == "DELETE":
            with self.lock:
                s.update(status="exit", status_detail=None, terminated=True)
            return h._send(200, self.view(s))
        return h._send(405, {"title": "Method Not Allowed"})

    def create(self, h, doc):
        with self.lock:
            self.create_calls += 1
            mode = self.create_plan.pop(0) if self.create_plan else "ok"
        if mode == "fail":
            return h._send(502, {"title": "Bad Gateway", "detail": "upstream timed out"})
        sid = secrets.token_hex(8)
        s = {"session_id": f"devin-{sid}", "url": f"https://app.devin.ai/sessions/{sid}", "status": "new",
             "status_detail": None, "tags": doc.get("tags") or [], "prompt": doc["prompt"], "repos": doc.get("repos") or [],
             "attachment_urls": doc.get("attachment_urls") or [], "max_acu_limit": doc.get("max_acu_limit"),
             "created_at": f"2026-11-20T00:00:{len(self.sessions):02d}Z", "pull_requests": [], "messages": [],
             "title": doc.get("title"), "acus_consumed": 0.0, "terminated": False}
        with self.lock:
            self.sessions[s["session_id"]] = s
        self.received.set()
        if mode == "fail_after_create":
            return h._send(502, {"title": "Bad Gateway", "detail": "upstream timed out"})
        if mode == "slow":
            threading.Event().wait(self.slow_seconds)
        if mode == "hold":
            self.release.wait(30)
        return h._send(200, self.view(s))


class FakeGitHub(_Server):
    def __init__(self, token="ghp_fake_token"):
        super().__init__()
        self.token = token
        self.repos = {}

    def add_repo(self, full, default="main", sha="a" * 40):
        self.repos[full] = {"default_branch": default, "branches": {default: sha}, "pulls": {}}

    def add_pull(self, full, number, head_sha):
        self.repos[full]["pulls"][number] = head_sha

    def dispatch(self, h, method):
        url = urlsplit(h.path)
        h._body()
        with self.lock:
            self.requests.append((method, url.path))
        if h.headers.get("Authorization") != f"Bearer {self.token}":
            return h._send(401, {"message": "Bad credentials"})
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)(?:/(branches|pulls|commits)/(.+))?", url.path)
        repo = self.repos.get(match.group(1)) if match else None
        if method != "GET" or repo is None:
            return h._send(404, {"message": "Not Found"})
        full = match.group(1)
        if match.group(2) is None:
            return h._send(200, {"full_name": full, "default_branch": repo["default_branch"], "private": True,
                                 "permissions": {"push": True}})
        if match.group(2) == "branches":
            name = match.group(3).replace("%2F", "/")
            if name not in repo["branches"]:
                return h._send(404, {"message": "Branch not found"})
            return h._send(200, {"name": name, "commit": {"sha": repo["branches"][name]}})
        if match.group(2) == "commits":
            sha = match.group(3)
            known = set(repo["branches"].values()) | set(repo["pulls"].values()) | repo.setdefault("commits", set())
            full = [c for c in known if c.startswith(sha)]
            if not full:
                return h._send(404, {"message": "No commit found for SHA"})
            return h._send(200, {"sha": full[0]})
        number = int(match.group(3))
        if number not in repo["pulls"]:
            return h._send(404, {"message": "Not Found"})
        return h._send(200, {"number": number, "html_url": f"https://github.com/{full}/pull/{number}", "state": "open",
                             "merged": False, "head": {"sha": repo["pulls"][number], "ref": "devin/build"},
                             "base": {"ref": repo["default_branch"]}})
