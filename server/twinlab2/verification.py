"""Release verification bound to the exact built commit.

A release is Packaged from one Git commit. It becomes Tested only when, for that exact SHA:

- every **required check** reported by GitHub (check runs and commit statuses) is green. Each
  application says which checks are required (`apps.required_checks`, one name or glob per
  line). Empty means: every check GitHub reports on the commit, and at least one, when the
  application has a GitHub repository; no CI gate when it has none (the release page says so);
- when the application targets **Windows desktop** (`apps.target_platform`), a green check from a
  Windows validation environment: a check whose name matches `apps.windows_check_pattern`
  (default `*windows*`, which matches a `windows-latest` matrix job). Without one the page says
  "Windows validation missing";
- the commit is still the head the release was built from: a release made from a pull request
  (or a branch) is refused, and a Tested one goes back to Packaged, once that head has moved
  (a new push or a force-push);
- and, as before, the scenario results, platform, launch instructions and the reset check are
  recorded (`portal.record_test`), now also by Twin Lab's own replay (`independent.py`).

Check results are stored per repository and SHA in `commit_checks`. A check whose `head_sha`
(check run) or `sha` (combined status) is not the release's commit is never stored, so a result
for another commit cannot make a release Tested.

The GitHub API is `TWINLAB2_GITHUB_API` (default https://api.github.com) with the token
`TWINLAB2_GITHUB_TOKEN` (or `GITHUB_TOKEN`). Tests point it at a fake server.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from twinlab2 import registry

API = "https://api.github.com"
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
PLATFORMS = {"": "Not set", "web": "Web", "windows-desktop": "Windows desktop", "mainframe": "Mainframe (3270)"}
WINDOWS_PATTERN = "*windows*"
ALL = "*"
GREEN = "success"

# The tables and columns are created by migrations.py (2: release verification).


class GitHubError(Exception):
    pass


class GitHub:
    """The few GitHub REST calls verification needs."""

    def __init__(self, token=None, api=None, timeout=20):
        self.token = token if token is not None else (
            os.environ.get("TWINLAB2_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "")
        self.api = (api or os.environ.get("TWINLAB2_GITHUB_API") or API).rstrip("/")
        self.timeout = timeout

    def _open(self, path, accept="application/vnd.github+json"):
        url = path if path.startswith(("http://", "https://")) else self.api + path
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "twinlab2"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as error:
            raise GitHubError(f"GitHub answered {error.code} for {urllib.parse.urlsplit(url).path}") from error
        except OSError as error:
            raise GitHubError(f"GitHub is not reachable: {error}") from error

    def get(self, path):
        with self._open(path) as response:
            return json.loads(response.read())

    def pages(self, path, key):
        """Every item of a paginated list (`key` names the list in an object answer)."""
        items, url = [], path + ("&" if "?" in path else "?") + "per_page=100"
        for _ in range(20):
            with self._open(url) as response:
                body = json.loads(response.read())
                link = response.headers.get("Link") or ""
            items += body.get(key, []) if key else body
            found = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not found:
                break
            url = found.group(1)
        return items

    def check_runs(self, repo, sha):
        return self.pages(f"/repos/{repo}/commits/{sha}/check-runs", "check_runs")

    def status(self, repo, sha):
        return self.get(f"/repos/{repo}/commits/{sha}/status?per_page=100")

    def pr_head(self, repo, number):
        return self.get(f"/repos/{repo}/pulls/{int(number)}")["head"]["sha"].lower()

    def branch_head(self, repo, ref):
        return self.get(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")["sha"].lower()

    def zipball(self, repo, sha):
        with self._open(f"/repos/{repo}/zipball/{sha}", accept="application/vnd.github+json") as response:
            return response.read(200 * 1024 * 1024 + 1)


def github_repo(git_url):
    """owner/name of a GitHub repository URL (https or SSH), or ''."""
    text = (git_url or "").strip()
    found = re.match(r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$", text)
    if found and REPO.fullmatch(found.group(1)):
        return found.group(1)
    return text if REPO.fullmatch(text) and "." not in text.split("/")[0] else ""


def _conclusion_of_run(run):
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion") or ""
    if conclusion == "success":
        return GREEN
    if conclusion in ("neutral", "skipped"):
        return conclusion
    return "failure" if conclusion else "pending"


def _conclusion_of_status(state):
    return {"success": GREEN, "pending": "pending"}.get(state, "failure")


def fetch_checks(github, repo, sha):
    """The check runs and commit statuses GitHub reports for exactly `sha`, each
    {kind, name, conclusion, url, completed_at}. Results that GitHub attributes to another SHA
    are dropped."""
    found = {}
    for run in github.check_runs(repo, sha):
        if (run.get("head_sha") or "").lower() != sha:
            continue
        name = str(run.get("name") or "")[:300]
        item = {"kind": "check_run", "name": name, "conclusion": _conclusion_of_run(run),
                "url": str(run.get("html_url") or run.get("details_url") or "")[:1000],
                "completed_at": run.get("completed_at")}
        # A re-run is a second run with the same name: the latest one decides.
        previous = found.get(("check_run", name))
        if previous is None or (run.get("id") or 0) > previous[0]:
            found[("check_run", name)] = (run.get("id") or 0, item)
    combined = github.status(repo, sha)
    if (combined.get("sha") or "").lower() == sha:
        for status in combined.get("statuses") or []:
            name = str(status.get("context") or "")[:300]
            if ("status", name) in found:
                continue  # the combined status lists the newest status per context first
            found[("status", name)] = (0, {
                "kind": "status", "name": name, "conclusion": _conclusion_of_status(status.get("state")),
                "url": str(status.get("target_url") or "")[:1000],
                "completed_at": status.get("updated_at") if status.get("state") != "pending" else None})
    return sorted((item for _, item in found.values()), key=lambda c: (c["name"].lower(), c["kind"]))


def store_checks(connection, repo, sha, checks):
    connection.execute("DELETE FROM commit_checks WHERE repository = ? AND commit_sha = ?", (repo, sha))
    stamp = registry.now()
    for check in checks:
        connection.execute(
            "INSERT INTO commit_checks(repository, commit_sha, kind, name, conclusion, url, completed_at, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (repo, sha, check["kind"], check["name"], check["conclusion"], check["url"],
             check["completed_at"], stamp))


def stored_checks(connection, repo, sha):
    return [dict(row) for row in connection.execute(
        "SELECT kind, name, conclusion, url, completed_at, fetched_at FROM commit_checks "
        "WHERE repository = ? AND commit_sha = ? ORDER BY lower(name), kind", (repo, sha))]


def required_patterns(app, repo):
    """The application's required checks, one name or glob per line. Empty means every reported
    check when there is a GitHub repository, none otherwise."""
    lines = [line.strip() for line in (app["required_checks"] or "").splitlines() if line.strip()]
    if lines:
        return lines
    return [ALL] if repo else []


def windows_required(app):
    return app["target_platform"] == "windows-desktop"


def windows_pattern(app):
    return (app["windows_check_pattern"] or "").strip() or WINDOWS_PATTERN


def release_repository(app, release):
    repo = release["repository"] if release is not None and release["repository"] else ""
    return repo or github_repo(app["git_url"])


def _matches(pattern, name):
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def evaluate(app, checks, head_ok=True, head_reason=None, repo=""):
    """The CI part of the Tested gate: {ok, reasons, required: [{pattern, checks, state}],
    windows: {required, pattern, state, checks}}."""
    reasons, required = [], []
    patterns = required_patterns(app, repo)
    if not head_ok:
        reasons.append(head_reason)
    for pattern in patterns:
        matching = checks if pattern == ALL else [c for c in checks if _matches(pattern, c["name"])]
        if not matching:
            state = "missing"
            reasons.append("No checks are reported on this commit." if pattern == ALL
                           else f"Required check “{pattern}” is missing on this commit.")
        elif all(c["conclusion"] == GREEN for c in matching):
            state = GREEN
        elif any(c["conclusion"] == "pending" for c in matching):
            state = "pending"
        else:
            state = "failure"
        if state in ("pending", "failure"):
            for c in matching:
                if c["conclusion"] != GREEN:
                    reasons.append(f"Required check “{c['name']}” is {c['conclusion']}.")
        required.append({"pattern": pattern, "checks": [c["name"] for c in matching], "state": state})
    windows = {"required": windows_required(app), "pattern": windows_pattern(app), "state": "not-required",
               "checks": []}
    if windows["required"]:
        matching = [c for c in checks if _matches(windows["pattern"], c["name"])]
        windows["checks"] = [c["name"] for c in matching]
        if not matching:
            windows["state"] = "missing"
            reasons.append("Windows validation missing: no check from a Windows validation environment "
                           f"(name matching “{windows['pattern']}”) on this commit.")
        elif all(c["conclusion"] == GREEN for c in matching):
            windows["state"] = GREEN
        else:
            windows["state"] = "failure"
            reasons.append("Windows validation is not green: " + ", ".join(
                f"{c['name']} {c['conclusion']}" for c in matching if c["conclusion"] != GREEN) + ".")
    return {"ok": not reasons, "reasons": reasons, "required": required, "windows": windows,
            "repository": repo}


def _load(connection, release_id):
    release = connection.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
    if release is None:
        raise LookupError("No such release.")
    app = connection.execute("SELECT * FROM apps WHERE id = ?", (release["app_id"],)).fetchone()
    return release, app


def refresh(connection, release_id, github=None):
    """Fetch the checks of the release's commit and its current head from GitHub, store them and
    the gate's outcome. A Tested (not yet Published) release whose head moved goes back to
    Packaged. Returns the gate (see `evaluate`)."""
    release, app = _load(connection, release_id)
    repo, sha = release_repository(app, release), release["commit_sha"]
    observed = sha
    if repo:
        github = github or GitHub()
        try:
            if release["pr_number"]:
                observed = github.pr_head(repo, release["pr_number"])
            elif release["source_ref"]:
                observed = github.branch_head(repo, release["source_ref"])
            checks = fetch_checks(github, repo, sha)
        except GitHubError as error:
            gate = evaluate(app, stored_checks(connection, repo, sha), repo=repo)
            gate["ok"] = False
            gate["reasons"].insert(0, f"Could not read checks from GitHub: {error}")
            _save_gate(connection, release, repo, "", gate)
            return gate
        store_checks(connection, repo, sha, checks)
    else:
        checks = []
    head_reason = moved(release, observed)
    gate = evaluate(app, checks, head_reason is None, head_reason, repo)
    _save_gate(connection, release, repo, observed, gate)
    if head_reason and release["state"] == "tested":
        connection.execute("UPDATE releases SET state = 'packaged' WHERE id = ?", (release_id,))
    return gate


def moved(release, observed):
    """Why the release's commit is no longer the head it was built from, or None."""
    if not observed or observed == release["commit_sha"]:
        return None
    where = (f"Pull request #{release['pr_number']}" if release["pr_number"]
             else f"Branch {release['source_ref']}" if release["source_ref"] else "The release's head")
    return (f"{where} now points at {observed[:12]}, not this release's commit "
            f"{release['commit_sha'][:12]}: verify the new head as a new release.")


def _save_gate(connection, release, repo, observed, gate):
    connection.execute("""
        INSERT INTO release_verifications(release_id, commit_sha, repository, observed_head, status, reasons,
                                          checked_at) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(release_id) DO UPDATE SET commit_sha = excluded.commit_sha,
        repository = excluded.repository, observed_head = excluded.observed_head,
        status = excluded.status, reasons = excluded.reasons, checked_at = excluded.checked_at
    """, (release["id"], release["commit_sha"], repo, observed, "green" if gate["ok"] else "refused",
          json.dumps(gate["reasons"]), registry.now()))


def require_green(connection, release_id, github=None):
    """ValueError naming what is not green when the release's commit may not become Tested."""
    gate = refresh(connection, release_id, github)
    if not gate["ok"]:
        raise ValueError(" ".join(gate["reasons"]))
    return gate


def summary(connection, release_id):
    """What the release page shows, from stored results only (no GitHub call)."""
    release, app = _load(connection, release_id)
    repo = release_repository(app, release)
    checks = stored_checks(connection, repo, release["commit_sha"]) if repo else []
    saved = connection.execute("SELECT * FROM release_verifications WHERE release_id = ?",
                               (release_id,)).fetchone()
    reason = moved(release, saved["observed_head"]) if saved else None
    gate = evaluate(app, checks, reason is None, reason, repo)
    extra = [r for r in json.loads(saved["reasons"]) if r not in gate["reasons"]] if saved else []
    if extra:  # e.g. GitHub could not be read at the last check
        gate["ok"], gate["reasons"] = False, extra + gate["reasons"]
    gate.update(checks=checks, checked_at=saved["checked_at"] if saved else None,
                platform=PLATFORMS.get(app["target_platform"], app["target_platform"]),
                pr_number=release["pr_number"], source_ref=release["source_ref"],
                reports=[dict(row) for row in connection.execute(
                    "SELECT id, kind, sha256, created_at FROM release_artifacts WHERE release_id = ? "
                    "ORDER BY id DESC", (release_id,))])
    return gate


def put_report(connection, release_id, report):
    """Store a verification report for a release; returns its SHA-256. The one place reports are
    written, so moving them to object storage (artifacts.put(kind="verification_report")) is a
    change here only."""
    content = json.dumps(report, indent=1, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    connection.execute(
        "INSERT INTO release_artifacts(release_id, kind, sha256, content, created_at) VALUES (?,?,?,?,?)",
        (release_id, "verification_report", digest, content, registry.now()))
    return digest


def get_report(connection, release_id, report_id):
    row = connection.execute(
        "SELECT * FROM release_artifacts WHERE id = ? AND release_id = ? AND kind = 'verification_report'",
        (report_id, release_id)).fetchone()
    return None if row is None else bytes(row["content"])


def package_commit(connection, app_id, repo, sha, github, revision, pr_number=None, ref="", build_id=None):
    """A Packaged release of `sha` from the GitHub archive of that commit, under the build brief
    `revision`; an existing release of the same commit is reused. Returns its id.

    `build_id` is the build session the commit came from. A release belongs to the first session
    that packaged it: another session cannot take it over (re-point its pull request or ref)."""
    existing = connection.execute(
        "SELECT id, build_id FROM releases WHERE app_id = ? AND commit_sha = ? ORDER BY id DESC", (app_id, sha)).fetchone()
    if existing is not None:
        owner = existing["build_id"]
        if owner is not None and build_id is not None and owner != build_id:
            raise ValueError(f"Commit {sha[:12]} is already release {existing['id']} of build session #{owner}: "
                             "a session cannot take over another session's release.")
        if owner is not None and build_id is None:
            return existing["id"]  # a plain verification of a session's commit leaves its binding alone
        connection.execute("UPDATE releases SET repository = ?, pr_number = ?, source_ref = ?, "
                           "build_id = COALESCE(build_id, ?) WHERE id = ?",
                           (repo, pr_number, ref or "", build_id, existing["id"]))
        return existing["id"]
    try:
        archive = github.zipball(repo, sha)
    except GitHubError as error:
        raise ValueError(f"Could not download commit {sha[:12]} from GitHub: {error}") from error
    if len(archive) > 200 * 1024 * 1024:
        raise ValueError("The commit's archive is larger than 200 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            if not any(not name.endswith("/") for name in zipped.namelist()):
                raise ValueError("The commit's tree is empty.")
    except zipfile.BadZipFile as error:
        raise ValueError("GitHub did not return a ZIP archive of the commit.") from error
    cursor = connection.execute(
        """INSERT INTO releases(app_id, version, revision, commit_sha, archive, archive_sha,
           repository, pr_number, source_ref, build_id) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (app_id, sha[:12], revision, sha, archive, hashlib.sha256(archive).hexdigest(),
         repo, pr_number, ref or "", build_id))
    return cursor.fetchone()[0]


def check_commit_args(repo, sha):
    repo, sha = (repo or "").strip(), (sha or "").strip().lower()
    if not REPO.fullmatch(repo):
        raise ValueError("Name the GitHub repository as owner/name.")
    if not SHA.fullmatch(sha):
        raise ValueError("Give the full 40-character commit SHA.")
    return repo, sha
