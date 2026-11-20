"""The few GitHub REST reads a build needs: a repository, a branch's commit, a pull request.

The token comes from TWINLAB2_GITHUB_TOKEN or the secret saved under Setup; it is sent only to
the configured API (TWINLAB2_GITHUB_API, default https://api.github.com) and never shown.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API = "https://api.github.com"
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<!/)$")


class GitHubError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def settings(secrets=None, env=None):
    env = os.environ if env is None else env
    secrets = secrets or {}
    token, source = env.get("TWINLAB2_GITHUB_TOKEN", "").strip(), "environment"
    if not token and secrets.get("GITHUB_TOKEN"):
        token, source = secrets["GITHUB_TOKEN"], "Setup"
    api = (env.get("TWINLAB2_GITHUB_API", "").strip() or secrets.get("GITHUB_API") or DEFAULT_API).rstrip("/")
    return {"token": token or None, "api": api, "source": source if token else None}


def normalize_repo(text):
    text = (text or "").strip()
    text = re.sub(r"^https://github\.com/", "", text).removesuffix(".git").strip("/")
    if not REPO.fullmatch(text):
        raise ValueError("Name the repository as owner/name.")
    return text


class GitHub:
    def __init__(self, values, timeout=15):
        if not values.get("token"):
            raise GitHubError(0, "GitHub is not connected: set TWINLAB2_GITHUB_TOKEN or save a token under Setup.")
        self._token = values["token"]
        self.api = values["api"]
        self.timeout = timeout

    def __repr__(self):
        return f"GitHub(api={self.api!r})"

    def _get(self, path):
        request = urllib.request.Request(self.api + path, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "twinlab2"})
        context = ssl.create_default_context() if self.api.startswith("https://") else None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and exc.headers.get("X-RateLimit-Remaining") != "0":
                raise GitHubError(exc.code, f"GitHub refused the token ({exc.code}): it may have expired or been "
                                  "revoked, or it lacks access. Update it under Setup.") from exc
            if exc.code == 404:
                raise GitHubError(404, "GitHub: not found, or the token cannot see it.") from exc
            raise GitHubError(exc.code, f"GitHub answered {exc.code} {exc.reason}.") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GitHubError(0, f"Cannot reach GitHub at {self.api}: {exc}") from exc

    def repository(self, repo):
        doc = self._get(f"/repos/{normalize_repo(repo)}")
        return {"full_name": doc.get("full_name") or repo, "default_branch": doc.get("default_branch") or "main",
                "private": bool(doc.get("private")), "push": bool((doc.get("permissions") or {}).get("push"))}

    def branch_commit(self, repo, branch):
        if not BRANCH.fullmatch(branch or ""):
            raise ValueError("That is not a branch name.")
        doc = self._get(f"/repos/{normalize_repo(repo)}/branches/{urllib.parse.quote(branch, safe='')}")
        sha = str((doc.get("commit") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError(0, f"GitHub gave no commit for {repo}@{branch}.")
        return sha

    def commit(self, repo, sha):
        """The full SHA of `sha` in `repo` (GitHubError 404 when the repository has no such commit)."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
            raise ValueError("That is not a commit SHA.")
        doc = self._get(f"/repos/{normalize_repo(repo)}/commits/{sha}")
        found = str(doc.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", found):
            raise GitHubError(0, f"GitHub gave no commit for {repo}@{sha}.")
        return found

    def pull(self, repo, number):
        doc = self._get(f"/repos/{normalize_repo(repo)}/pulls/{int(number)}")
        return {"number": int(doc.get("number") or number), "url": doc.get("html_url"),
                "state": doc.get("state"), "merged": bool(doc.get("merged")),
                "head_sha": (doc.get("head") or {}).get("sha"), "head_ref": (doc.get("head") or {}).get("ref"),
                "base_ref": (doc.get("base") or {}).get("ref")}
