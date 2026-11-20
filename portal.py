"""Local TwinLab2 catalog and build pack export."""

import argparse
from contextlib import closing
import os
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import urllib.request

from flask import Flask, abort, g, redirect, render_template, request, send_file, session, url_for

import artifacts
import buildpack
import independent
import journey
import builds
import builds_web
import registry
import scenarios
import uploads
import verification as commit_checks

LOCAL = {"127.0.0.1", "::1"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, git_url TEXT NOT NULL,
    requirements TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, events TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    app_id INTEGER NOT NULL REFERENCES apps(id),
    recording_id TEXT NOT NULL REFERENCES recordings(id),
    selection TEXT NOT NULL, notes TEXT NOT NULL,
    PRIMARY KEY (app_id, recording_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
    title TEXT NOT NULL, kind TEXT NOT NULL, extension TEXT NOT NULL, content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS briefs (
    revision TEXT PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
    manifest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brief_packages (
    revision TEXT PRIMARY KEY REFERENCES briefs(revision), archive BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id),
    version TEXT NOT NULL, revision TEXT NOT NULL REFERENCES briefs(revision),
    commit_sha TEXT NOT NULL, archive BLOB NOT NULL, archive_sha TEXT NOT NULL,
    UNIQUE (app_id, version)
);
CREATE TABLE IF NOT EXISTS deployments (
    release_id INTEGER NOT NULL REFERENCES releases(id), environment TEXT NOT NULL,
    url TEXT NOT NULL, username TEXT NOT NULL, secret_ref TEXT NOT NULL,
    PRIMARY KEY (release_id, environment)
);
CREATE TABLE IF NOT EXISTS release_tests (
    release_id INTEGER PRIMARY KEY REFERENCES releases(id), commit_sha TEXT NOT NULL,
    platform TEXT NOT NULL, launch TEXT NOT NULL, reset_result TEXT NOT NULL,
    reset_notes TEXT NOT NULL, results TEXT NOT NULL, tested_by TEXT NOT NULL, tested_at TEXT NOT NULL
);
"""

# A release is Packaged when its committed tree is archived, Tested once its acceptance evidence
# is stored (release_tests), and Published only after it was Tested.
STATES = ("packaged", "tested", "published")
RESULTS = ("pass", "fail", "not-run")
# A deployment is Verified when GET <deployment URL>/.twinlab/version answers 200 with JSON
# {"commit": "<40-hex Git commit>"} equal to the release's commit. Anything else leaves it a
# registered URL that is not verified.
VERSION_PATH = ".twinlab/version"


def migrate_releases(connection):
    """Release states and deployment verification for catalogs made before them: an existing
    release becomes Packaged and an existing deployment an unverified registered URL."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(releases)")}
    if "state" not in columns:
        connection.execute("ALTER TABLE releases ADD COLUMN state TEXT NOT NULL DEFAULT 'packaged'")
    if "published_at" not in columns:
        connection.execute("ALTER TABLE releases ADD COLUMN published_at TEXT")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(deployments)")}
    for name, definition in (("verification", "TEXT NOT NULL DEFAULT 'unverified'"),
                             ("observed_commit", "TEXT NOT NULL DEFAULT ''"), ("checked_at", "TEXT")):
        if name not in columns:
            connection.execute(f"ALTER TABLE deployments ADD COLUMN {name} {definition}")
    connection.commit()


def brief_scenarios(manifest):
    """The agreed business scenarios of a build brief, each {"id", "name"}: the brief's own
    `scenarios` list when it has one (strings, or objects with an id and a name or title),
    else one per linked recording, named by its reviewed notes."""
    found, seen = [], set()
    for number, item in enumerate(manifest.get("scenarios") or [], 1):
        if isinstance(item, str):
            key, name = f"scenario-{number}", item
        elif isinstance(item, dict):
            key = item.get("id") or item.get("key") or f"scenario-{number}"
            name = item.get("name") or item.get("title") or item.get("outcome") or key
        else:
            continue
        found.append({"id": str(key)[:120], "name": str(name).strip()[:300] or str(key)})
    if not found:
        for item in manifest.get("evidence") or []:
            notes = (item.get("notes") or "").strip().splitlines()
            found.append({"id": f"recording:{item['recording_id']}",
                          "name": notes[0][:300] if notes else f"Recording {item['recording_id'][:12]}"})
    unique = []
    for item in found:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


def record_test(connection, release_id, commit, platform, launch, reset_result, reset_notes, results, user,
                gate=None, github=None):
    """Store a release's acceptance evidence and make it Tested. ValueError when the evidence is
    incomplete, is for another commit, the release is already Published, or the commit's required
    checks (and Windows validation) are not green (`verification`). `gate` is a verification
    result just computed for this release; without one the checks are fetched now."""
    release = connection.execute(
        "SELECT releases.*, briefs.manifest FROM releases JOIN briefs USING (revision) WHERE releases.id = ?",
        (release_id,),
    ).fetchone()
    if release is None:
        raise LookupError("No such release.")
    if release["state"] == "published":
        raise ValueError("A published release is final: package a new version to test again.")
    if (commit or "").strip().lower() != release["commit_sha"]:
        raise ValueError("The tested commit must be the packaged commit " + release["commit_sha"] + ".")
    platform, launch = (platform or "").strip(), (launch or "").strip()
    if not platform or not launch:
        raise ValueError("Give the supported platform and the launch instructions.")
    if reset_result not in ("pass", "fail"):
        raise ValueError("Record whether the reset check passed or failed.")
    scenarios = brief_scenarios(json.loads(release["manifest"]))
    if not scenarios:
        raise ValueError("The build brief names no scenarios to test: link a recording or add scenarios.")
    if set(results) - {item["id"] for item in scenarios}:
        raise ValueError("A result names a scenario that is not in the build brief.")
    rows = []
    for item in scenarios:
        result = results.get(item["id"])
        if result not in RESULTS:
            raise ValueError(f"Record pass, fail or not-run for “{item['name']}”.")
        rows.append({"id": item["id"], "name": item["name"], "result": result})
    gate = gate or commit_checks.refresh(connection, release_id, github)
    if not gate["ok"]:
        raise ValueError("Not Tested: " + " ".join(gate["reasons"]))
    connection.execute("""
        INSERT INTO release_tests VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(release_id) DO UPDATE SET commit_sha = excluded.commit_sha,
        platform = excluded.platform, launch = excluded.launch, reset_result = excluded.reset_result,
        reset_notes = excluded.reset_notes, results = excluded.results, tested_by = excluded.tested_by,
        tested_at = excluded.tested_at
    """, (release_id, release["commit_sha"], platform[:500], launch[:8000], reset_result,
          (reset_notes or "").strip()[:2000], json.dumps(rows), user, registry.now()))
    connection.execute("UPDATE releases SET state = 'tested' WHERE id = ?", (release_id,))


def store_brief(connection, app_id, revision, contents, package):
    """Keep a frozen build brief and its pack (as a `builder_package` artifact) under its
    revision, once: the same revision is the same bytes. No commit."""
    connection.execute("INSERT INTO briefs VALUES (?, ?, ?) ON CONFLICT (revision) DO NOTHING",
                       (revision, app_id, contents))
    if connection.execute("SELECT 1 FROM brief_packages WHERE revision = ?", (revision,)).fetchone() is None:
        ref = artifacts.save(connection, artifacts.put(package, kind="builder_package", app_id=app_id,
                                                       content_type="application/zip"))
        connection.execute("INSERT INTO brief_packages(revision, archive, artifact_id) VALUES (?, ?, ?)",
                           (revision, b"", ref.id))


def verify_commit(connection, app_id, repo, sha, github=None, runner=None, pr_number=None, ref="",
                  base_url=None, user="verification"):
    """“Verify this build”: a Packaged release of exactly `sha` (from GitHub), its checks, Twin
    Lab's independent checks against a deployment reporting that commit, and the Tested
    transition when all of it holds. The release stays Packaged otherwise. The verification
    report is stored either way. Returns {release_id, tested, reasons, report, report_sha}."""
    repo, sha = commit_checks.check_commit_args(repo, sha)
    github = github or commit_checks.GitHub()
    if not scenarios.load(connection, app_id):
        raise ValueError("Add a business scenario before verifying a build.")
    # The brief the build answers: the app's current one, stored as an export would store it.
    revision, contents, _, package = buildpack.build(connection, app_id, VERSION_PATH)
    store_brief(connection, app_id, revision, contents, package)
    release_id = commit_checks.package_commit(connection, app_id, repo, sha, github, revision, pr_number, ref)
    return verify_release(connection, release_id, github, runner, base_url, user)


def verify_release(connection, release_id, github=None, runner=None, base_url=None, user="verification"):
    """The verification of an existing release (see verify_commit)."""
    release = connection.execute(
        "SELECT releases.*, briefs.manifest FROM releases JOIN briefs USING (revision) WHERE releases.id = ?",
        (release_id,)).fetchone()
    if release is None:
        raise LookupError("No such release.")
    app = connection.execute("SELECT * FROM apps WHERE id = ?", (release["app_id"],)).fetchone()
    sha, manifest = release["commit_sha"], json.loads(release["manifest"])
    gate = commit_checks.refresh(connection, release_id, github)
    reasons = list(gate["reasons"])
    if base_url is None:
        row = connection.execute(
            "SELECT url FROM deployments WHERE release_id = ? AND verification = 'verified' ORDER BY environment",
            (release_id,)).fetchone()
        base_url = row["url"] if row else None
    twin = None
    if not base_url:
        reasons.append(f"No deployment reports this release's commit at /{VERSION_PATH}, "
                       "so Twin Lab's checks did not run.")
    else:
        observed = deployed_commit(base_url)
        if observed != sha:
            reasons.append(f"{base_url} reports " + (f"commit {observed[:12]}" if observed else "no commit") +
                           f", not {sha[:12]}: Twin Lab's checks need the built commit.")
        else:
            twin = independent.run(runner or independent.PinnedTwinLab(), brief_scenarios(manifest), manifest,
                                   base_url, app["lab_url"] or "")
            reasons += twin.get("problems") or []
    tested = False
    if twin is not None and not twin.get("problems"):
        reset = twin["reset"] or {}
        platform = commit_checks.PLATFORMS[app["target_platform"]] if app["target_platform"] else ""
        try:
            record_test(connection, release_id, sha, platform, app["launch"], reset.get("result"),
                        reset.get("notes"), {s["id"]: s["result"] for s in twin["scenarios"]}, user, gate=gate)
            tested = True
        except ValueError as error:
            if gate["ok"]:  # else its reasons are already listed
                reasons.append(str(error))
    report = {
        "format": "twinlab2-verification-report/1", "release_id": release_id, "app_id": release["app_id"],
        "version": release["version"], "repository": gate["repository"], "commit": sha,
        "pull_request": release["pr_number"], "ref": release["source_ref"], "created_at": registry.now(),
        "github": {"checks": commit_checks.stored_checks(connection, gate["repository"], sha)
                   if gate["repository"] else [],
                   "required": gate["required"], "windows": gate["windows"], "green": gate["ok"]},
        "twinlab": twin, "tested": tested, "reasons": reasons,
    }
    digest = commit_checks.put_report(connection, release_id, report)
    return {"release_id": release_id, "tested": tested, "reasons": reasons, "report": report, "report_sha": digest}


def publish(connection, release_id):
    """Make a Tested release Published; ValueError for any other state."""
    row = connection.execute("SELECT state FROM releases WHERE id = ?", (release_id,)).fetchone()
    if row is None:
        raise LookupError("No such release.")
    if row["state"] != "tested":
        raise ValueError("Only a tested release can be published: record its test results first."
                         if row["state"] == "packaged" else "This release is already published.")
    connection.execute("UPDATE releases SET state = 'published', published_at = ? WHERE id = ?",
                       (registry.now(), release_id))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def deployed_commit(url, timeout=5):
    """The commit a deployment reports at <url>/.twinlab/version, or None."""
    parts = urlsplit(url)
    target = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/" + VERSION_PATH, "", ""))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(target, headers={"Accept": "application/json"}),
                         timeout=timeout) as response:
            if response.status != 200:
                return None
            commit = json.loads(response.read(4096)).get("commit")
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return None
    return commit.lower() if isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit) else None


def verification(url, commit):
    """(verification, observed commit) for a deployment of a release at `commit`."""
    observed = deployed_commit(url)
    if observed is None:
        return "unverified", ""
    return ("verified" if observed == commit else "mismatch"), observed


def deployment_label(deployment):
    """How every page names a deployment: Verified, or a registered URL that is not verified."""
    if deployment["verification"] == "verified":
        return "Verified"
    return "Registered URL (not verified)"


def deployment_reason(deployment):
    if deployment["verification"] == "verified":
        return f"{VERSION_PATH} reports this release's commit."
    if deployment["verification"] == "mismatch":
        return f"{VERSION_PATH} reports commit {deployment['observed_commit'][:12]}, not this release's."
    return f"No commit at {VERSION_PATH}."

ATTACHMENT_TYPES = {
    "Screen": {".png", ".jpg", ".jpeg"},
    "API": {".json", ".yaml", ".yml", ".md", ".txt"},
    "Database": {".sql", ".json", ".csv", ".md", ".txt"},
    "Utility": {".json", ".csv", ".md", ".txt"},
    "Acceptance": {".md", ".txt", ".json"},
}

ACTIONS = set("""
click dblclick double_click right_click type input select change toggle expand collapse menu
key keydown keypress fill submit navigate goto navigation tab-open tab-switch tab-close
window_open window_close menu_open focus check uncheck scroll setup download load hover
""".split())

def ordered(events):
    return sorted(events, key=lambda event: (event.get("time") or "", event["seq"]))


def recording_projection(log):
    events = {}
    session_ids = set()
    lines = log.decode("utf-8-sig").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Line {number} must contain an event object.")
        seq = row.get("seq")
        if type(seq) is not int or seq < 0 or seq in events:
            raise ValueError(f"Line {number} must have a unique nonnegative integer seq.")
        url = urlsplit(str(row.get("url") or ""))
        label = url.netloc.rsplit("@", 1)[-1] if url.scheme in {"http", "https"} else ""
        if not label and row.get("surface") == "desktop":
            label = str(row.get("process") or "Windows desktop")[:150]
        action = row.get("action")
        session_id = row.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id)
        ):
            raise ValueError(f"Line {number} has an invalid TwinLab2 session ID.")
        session_ids.add(session_id)
        events[seq] = {
            "seq": seq, "source": label or "Unknown source",
            "action": action if isinstance(action, str) and action in ACTIONS else "other",
        }
        if isinstance(row.get("ts"), str):
            try:
                at = datetime.fromisoformat(row["ts"])
                if at.tzinfo is not None:
                    events[seq]["time"] = at.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass
    if not events:
        raise ValueError("The event log is empty.")
    if len(session_ids) != 1:
        raise ValueError("An upload must contain events from a single recording session.")
    session_id = session_ids.pop()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return session_id or digest, ordered(events.values())


def add_release(database, app_id, version, revision, repository, ref, subdirectory=None, store=None):
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Use a version such as 1.0.0.")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        brief = connection.execute(
            "SELECT 1 FROM briefs WHERE revision = ? AND app_id = ?", (revision, app_id),
        ).fetchone()
        if brief is None:
            raise ValueError("Export the build brief for this app before registering its release.")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repository), *args], check=True, capture_output=True,
            ).stdout

        commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
        tree = commit
        if subdirectory:
            tree = git("rev-parse", "--verify", f"{commit}:{subdirectory}").decode().strip()
            if git("cat-file", "-t", tree).strip() != b"tree":
                raise ValueError("The release subdirectory must be a directory in the commit.")
        entries = git("ls-tree", "-r", "-z", tree).split(b"\0")
        if any(entry.startswith((b"120000 ", b"160000 ")) for entry in entries):
            raise ValueError("Package a release directory without symlinks or Git submodules.")
        archive = git("archive", "--format=zip", tree)
        if not archive or not any(entries):
            raise ValueError("The selected Git tree is empty.")
        if connection.execute("SELECT 1 FROM releases WHERE app_id = ? AND version = ?", (app_id, version)).fetchone():
            raise sqlite3.IntegrityError("UNIQUE constraint failed: releases.app_id, releases.version")
        artifacts.migrate(connection)
        ref = artifacts.save(connection, artifacts.put(
            archive, kind="release_download", app_id=app_id, content_type="application/zip",
            target=store or artifacts.from_env(database)))
        connection.execute(
            """INSERT INTO releases(app_id,version,revision,commit_sha,archive,archive_sha,artifact_id)
               VALUES (?,?,?,?,?,?,?)""",
            (app_id, version, revision, commit, b"", ref.sha256, ref.id),
        )
    return commit


def create_app(database):
    app = Flask(__name__)
    hosts = [h.strip() for h in os.environ.get("TWINLAB2_HOSTS", "").split(",") if h.strip()]
    app.config.update(
        SECRET_KEY=os.environ.get("TWINLAB2_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]", *hosts],
        MAX_FORM_PARTS=20000,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("TWINLAB2_SECURE_COOKIES") == "1",
    )
    trust_proxy = os.environ.get("TWINLAB2_TRUST_PROXY_USER") == "1"
    password = os.environ.get("TWINLAB2_PASSWORD", "")
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    artifacts.init_app(app, database)

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(database)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    with app.app_context():
        db().executescript(SCHEMA)
        migrate_releases(db())
        commit_checks.migrate(db())
        registry.migrate(db())
        scenarios.migrate(db())
        builds.migrate(db())
        artifacts.migrate(db())
        uploads.migrate(db())
        if os.environ.get("TWINLAB2_ARTIFACT_MIGRATE", "1") != "0":
            moved = artifacts.migrate_blobs(db())
            for table, key, reason in moved["failed"]:
                app.logger.warning("blob of %s %s not moved to artifact storage: %s", table, key, reason)
        columns = {row[1] for row in db().execute("PRAGMA table_info(apps)")}
        for column in ("owner", "purpose"):
            if column not in columns:
                db().execute(f"ALTER TABLE apps ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        db().commit()

    app.jinja_env.filters["status_label"] = lambda status: journey.STATUS_LABELS.get(
        status, str(status).replace("_", " "))
    app.jinja_env.globals["journey_steps"] = journey.JOURNEY
    app.jinja_env.globals.update(deployment_label=deployment_label, deployment_reason=deployment_reason,
                                 version_path=VERSION_PATH)

    @app.before_request
    def sign_in():
        """Every page needs a signed-in user: the OpenShift OAuth proxy's user when the portal
        runs behind it, a password when one is set, else only this machine. The agent API has
        its own bearer token (registry)."""
        if request.path.startswith(("/static/", *uploads.MACHINE_PATHS)) or request.endpoint in (
                "login", "health", "artifact_download"):
            return None
        if trust_proxy:
            user = request.headers.get("X-Forwarded-User") or request.headers.get("X-Forwarded-Preferred-Username")
            if not user:
                abort(401, "Sign in through the OpenShift OAuth proxy.")
            g.user = user[:120]
        elif password:
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            g.user = session["user"]
        else:
            if request.remote_addr not in LOCAL:
                abort(403, "This portal serves only this machine: set TWINLAB2_PASSWORD or run it behind the OAuth proxy.")
            g.user = "local"
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password:
            return redirect(url_for("home"))
        error = None
        if request.method == "POST":
            name = request.form.get("user", "").strip()[:80]
            if name and secrets.compare_digest(request.form.get("password", ""), password):
                session.clear()
                session["user"] = name
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("home"))
            error = "Wrong password."
        return render_template("login.html", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login") if password else url_for("home"))

    @app.get("/healthz")
    def health():
        db().execute("SELECT 1")
        return {"ok": True}

    @app.before_request
    def protect_forms():
        if request.path.startswith(uploads.MACHINE_PATHS):
            return
        if "form_token" not in session:
            session["form_token"] = secrets.token_hex(32)
        if request.method == "POST" and request.form.get("token") != session["form_token"]:
            abort(400, "Reload the page before submitting the form.")

    def get_app(app_id):
        row = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def get_recording(recording_id):
        row = db().execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def links(app_id):
        return db().execute("""
            SELECT recordings.*, evidence.selection, evidence.notes, evidence.reviewed, evidence.suggested
            FROM evidence JOIN recordings ON recordings.id = evidence.recording_id
            WHERE evidence.app_id = ? ORDER BY recordings.id
        """, (app_id,)).fetchall()

    def attachments(app_id):
        return db().execute(
            "SELECT * FROM attachments WHERE app_id = ? ORDER BY id", (app_id,),
        ).fetchall()

    def releases(app_id):
        return db().execute(
            "SELECT id, version, revision, commit_sha, archive_sha, state FROM releases WHERE app_id = ? ORDER BY id DESC",
            (app_id,),
        ).fetchall()

    def tested_releases():
        """Per app, its latest Tested or Published release, the results of its test, and the
        deployment to open (a verified one first)."""
        rows = db().execute("""
            SELECT releases.id, releases.app_id, releases.version, releases.state, releases.commit_sha,
                   release_tests.results, release_tests.platform
            FROM releases JOIN release_tests ON release_tests.release_id = releases.id
            WHERE releases.id = (SELECT MAX(id) FROM releases AS later
                                 WHERE later.app_id = releases.app_id AND later.state IN ('tested', 'published'))
        """).fetchall()
        found = {}
        for row in rows:
            results = [item["result"] for item in json.loads(row["results"])]
            deployment = db().execute(
                "SELECT * FROM deployments WHERE release_id = ? ORDER BY verification = 'verified' DESC, environment",
                (row["id"],),
            ).fetchone()
            found[row["app_id"]] = dict(row, deployment=deployment,
                                        counts={result: results.count(result) for result in RESULTS})
        return found

    @app.get("/")
    def home():
        apps = db().execute("SELECT * FROM apps ORDER BY status = 'not_replicated', name").fetchall()
        return render_template(
            "catalog.html", apps=[(row, journey.for_app(db(), row)) for row in apps],
            recordings=db().execute("SELECT id, name FROM recordings ORDER BY name").fetchall(),
            tested=tested_releases(),
        )

    @app.post("/apps")
    def add_app():
        name = request.form.get("name", "").strip()
        git_url = request.form.get("git_url", "").strip()
        requirements = request.form.get("requirements", "").strip()
        owner = request.form.get("owner", "").strip()[:120]
        purpose = request.form.get("purpose", "").strip()[:500]
        if not name:
            abort(400, "Enter a demo app name.")
        with db():
            result = db().execute(
                "INSERT INTO apps(name, git_url, requirements, owner, purpose) VALUES (?, ?, ?, ?, ?)",
                (name, git_url, requirements, owner, purpose),
            )
        scenarios.migrate(db())
        return redirect(url_for("demo", app_id=result.lastrowid))

    def selected_events(app_id):
        """The events selected for the app, as `recording#seq` with their pack references."""
        return {scenarios.event_ref(rec["recording_id"], event["seq"]): dict(event, recording=rec["recording"])
                for rec in buildpack.evidence(db(), app_id) for event in rec["events"]}

    @app.route("/apps/<int:app_id>", methods=["GET", "POST"])
    def demo(app_id):
        item = get_app(app_id)
        if request.method == "POST":
            # Each form on the application and setup pages sends only its own fields.
            limits = {"owner": 120, "purpose": 500, "git_url": 500}
            with db():
                for column, limit in limits.items():
                    if column in request.form:
                        db().execute(f"UPDATE apps SET {column} = ? WHERE id = ?",
                                     (request.form[column].strip()[:limit], app_id))
            target = request.form.get("next")
            return redirect(url_for("app_setup", app_id=app_id) if target == "setup"
                            else url_for("demo", app_id=app_id))
        return render_template(
            "demo.html", item=item, links=links(app_id), loads=json.loads,
            journey=journey.for_app(db(), item),
            attachments=attachments(app_id), kinds=ATTACHMENT_TYPES, releases=releases(app_id),
            others=db().execute("SELECT id, name FROM apps WHERE id != ? ORDER BY name", (app_id,)).fetchall(),
            scenarios=scenarios.load(db(), app_id), choices=selected_events(app_id),
            fields=scenarios.FIELDS,
        )

    @app.get("/apps/<int:app_id>/setup")
    def app_setup(app_id):
        item = get_app(app_id)
        return render_template("app_setup.html", item=item, releases=releases(app_id),
                               platforms=commit_checks.PLATFORMS, github_repo=commit_checks.github_repo(item["git_url"]),
                               default_windows=commit_checks.WINDOWS_PATTERN,
                               github_token=bool(commit_checks.GitHub().token))

    @app.get("/setup")
    def setup():
        return render_template("setup.html", apps=db().execute("SELECT id, name FROM apps ORDER BY name").fetchall(),
                               storage=artifacts.store().describe())

    @app.post("/apps/<int:app_id>/scenarios", defaults={"scenario_id": None})
    @app.post("/apps/<int:app_id>/scenarios/<int:scenario_id>")
    def save_scenario(app_id, scenario_id):
        get_app(app_id)
        try:
            values = scenarios.from_form(request.form, set(selected_events(app_id)))
        except ValueError as error:
            abort(400, str(error))
        try:
            scenario_id = scenarios.save(db(), app_id, values, scenario_id)
        except LookupError:
            abort(404)
        return redirect(url_for("demo", app_id=app_id, _anchor=f"scenario-{scenario_id}"))

    @app.post("/apps/<int:app_id>/scenarios/<int:scenario_id>/remove")
    def remove_scenario(app_id, scenario_id):
        get_app(app_id)
        scenarios.remove(db(), app_id, scenario_id)
        return redirect(url_for("demo", app_id=app_id))

    @app.post("/apps/<int:app_id>/attachments")
    def add_attachment(app_id):
        get_app(app_id)
        upload = request.files.get("file")
        title = request.form.get("title", "").strip()
        kind = request.form.get("kind", "")
        if not upload or not title or request.form.get("reviewed") != "yes":
            abort(400, "Choose a file and confirm it is ready to share with the builder.")
        extension = Path(upload.filename).suffix.lower()
        if extension not in ATTACHMENT_TYPES.get(kind, set()):
            abort(400, "The file extension is unsupported for this evidence type.")
        content = upload.read()
        if not content or len(content) > 4 * 1024 * 1024:
            abort(400, "Evidence files must be between 1 byte and 4 MiB.")
        ref = artifacts.put(content, kind="restricted_evidence", app_id=app_id,
                            content_type=artifacts.content_type_for(extension))
        with db():
            ref = artifacts.save(db(), ref)
            db().execute(
                "INSERT INTO attachments(app_id,title,kind,extension,content,artifact_id) VALUES (?,?,?,?,?,?)",
                (app_id, title, kind, extension, b"", ref.id),
            )
        return redirect(url_for("demo", app_id=app_id))

    @app.post("/apps/<int:app_id>/attachments/<int:attachment_id>/remove")
    def remove_attachment(app_id, attachment_id):
        get_app(app_id)
        with db():
            db().execute("DELETE FROM attachments WHERE app_id = ? AND id = ?", (app_id, attachment_id))
        return redirect(url_for("demo", app_id=app_id))

    @app.post("/recordings")
    def import_recording():
        name = request.form.get("name", "").strip()
        upload = request.files.get("log")
        if not name or upload is None:
            abort(400, "Enter a recording name and select steps.jsonl.")
        try:
            digest, events = recording_projection(upload.read())
        except ValueError as error:
            abort(400, str(error))
        with db():
            inserted = db().execute(
                "INSERT OR IGNORE INTO recordings VALUES (?, ?, ?) RETURNING id",
                (digest, name, json.dumps(events)),
            ).fetchone()
            if inserted is None:
                old = {event["seq"]: event for event in json.loads(get_recording(digest)["events"])}
                for event in events:
                    existing = old.setdefault(event["seq"], event)
                    if any(existing[key] != event[key] for key in ("action", "source")) or (
                        existing.get("time") and event.get("time") and existing["time"] != event["time"]
                    ):
                        abort(409, "A recovered event conflicts with the existing catalog event.")
                    existing.update(event)
                db().execute("UPDATE recordings SET events = ? WHERE id = ?", (
                    json.dumps(ordered(old.values())), digest,
                ))
        return redirect(url_for("recording", recording_id=digest))

    @app.route("/recordings/<recording_id>", methods=["GET", "POST"])
    def recording(recording_id):
        item = get_recording(recording_id)
        events = json.loads(item["events"])
        if request.method == "POST":
            app_id = request.form.get("app_id", type=int)
            if app_id is None:
                abort(400, "Choose a demo app.")
            get_app(app_id)
            try:
                selected = {int(value) for value in request.form.getlist("event")}
            except ValueError:
                abort(400, "Invalid event number.")
            if not selected or not selected <= {event["seq"] for event in events}:
                abort(400, "Choose one or more events from this recording.")
            with db():
                # the owner's review: a resend never changes this selection (registry.link_evidence)
                db().execute("""
                    INSERT INTO evidence(app_id, recording_id, selection, notes, reviewed, suggested)
                    VALUES (?, ?, ?, ?, 1, '[]')
                    ON CONFLICT(app_id, recording_id) DO UPDATE
                    SET selection = excluded.selection, notes = excluded.notes, reviewed = 1, suggested = '[]'
                """, (app_id, recording_id, json.dumps(sorted(selected)),
                      request.form.get("notes", "").strip()))
            return redirect(url_for("demo", app_id=app_id))
        app_id = request.args.get("app_id", type=int)
        saved = db().execute(
            "SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
            (app_id, recording_id),
        ).fetchone()
        return render_template(
            "recording.html", item=item, events=events, app_id=app_id, saved=saved,
            selected=set(json.loads(saved["selection"])) if saved else set(),
            suggested=set(json.loads(saved["suggested"])) if saved else set(),
            apps=db().execute("SELECT * FROM apps ORDER BY name").fetchall(),
        )

    def store_pack(app_id):
        """Build the app's pack and store it under its revision: the bytes a preview shows and
        a download serves are the stored ones."""
        get_app(app_id)
        if not scenarios.load(db(), app_id):
            abort(400, "Add a business scenario before exporting.")
        revision, contents, _, package = buildpack.build(db(), app_id, VERSION_PATH)
        with db():
            store_brief(db(), app_id, revision, contents, package)
        return revision

    def pack_row(app_id, revision):
        row = db().execute("""
            SELECT brief_packages.archive, brief_packages.artifact_id FROM brief_packages
            JOIN briefs USING (revision) WHERE app_id = ? AND revision = ?
        """, (app_id, revision)).fetchone()
        if row is None:
            abort(404)
        return row

    def stored_pack(app_id, revision):
        return artifacts.content(db(), pack_row(app_id, revision), "archive")

    def send_stored(row, column, filename):
        """A download of a stored artifact: a short-lived presigned URL when the store is S3,
        else the bytes through the portal, checked against the ref's SHA-256."""
        ref = artifacts.ref_of(db(), row)
        if ref is not None and ref.backend == "s3":
            return redirect(artifacts.presign_get(ref, 300, filename))
        return send_file(io.BytesIO(artifacts.content(db(), row, column)), mimetype="application/zip",
                         as_attachment=True, download_name=filename)

    @app.get("/apps/<int:app_id>/build-pack")
    def build_pack(app_id):
        return download_brief(app_id, store_pack(app_id))

    def pack_view(app_id):
        """Exactly what the download contains: every file of the stored archive, its size and
        SHA-256, text files in full. The preview and the Build page both show this."""
        revision = store_pack(app_id)
        package = stored_pack(app_id, revision)
        files = [{"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                  "text": content.decode("utf-8") if buildpack.is_text(path) else None}
                 for path, content in buildpack.read(package).items()]
        return {"revision": revision, "files": files, "size": len(package),
                "sha256": hashlib.sha256(package).hexdigest()}

    @app.get("/apps/<int:app_id>/build-pack/preview")
    def preview_pack(app_id):
        return render_template("preview.html", item=get_app(app_id), pack=pack_view(app_id))

    @app.get("/apps/<int:app_id>/briefs/<revision>")
    def download_brief(app_id, revision):
        item = get_app(app_id)
        slug = re.sub(r"[^a-z0-9]+", "-", item["name"].lower()).strip("-") or "demo"
        return send_stored(pack_row(app_id, revision), "archive", f"{slug}-{revision[:12]}.zip")

    def get_release(release_id):
        item = db().execute(
            "SELECT id, app_id, version, revision, commit_sha, archive_sha, state, published_at, repository, "
            "pr_number, source_ref FROM releases WHERE id = ?",
            (release_id,),
        ).fetchone()
        if item is None:
            abort(404)
        return item

    def save_check(release, environment, url):
        status, observed = verification(url, release["commit_sha"])
        db().execute(
            "UPDATE deployments SET verification = ?, observed_commit = ?, checked_at = ? WHERE release_id = ? AND environment = ?",
            (status, observed, registry.now(), release["id"], environment),
        )

    @app.route("/releases/<int:release_id>", methods=["GET", "POST"])
    def release(release_id):
        item = get_release(release_id)
        if request.method == "POST":
            environment = request.form.get("environment", "").strip()
            url = request.form.get("url", "").strip()
            try:
                parsed = urlsplit(url)
            except ValueError:
                abort(400, "Enter a valid deployment URL.")
            if not environment or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
                abort(400, "Provide an environment and an HTTP or HTTPS URL without credentials.")
            secret_ref = request.form.get("secret_ref", "").strip()
            if secret_ref and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,255}", secret_ref):
                abort(400, "Enter a secret reference such as vault:demo/billing.")
            with db():
                db().execute("""
                    INSERT INTO deployments(release_id, environment, url, username, secret_ref) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(release_id, environment) DO UPDATE SET
                    url = excluded.url, username = excluded.username, secret_ref = excluded.secret_ref
                """, (release_id, environment, url, request.form.get("username", "").strip(), secret_ref))
                save_check(item, environment, url)
            return redirect(url_for("release", release_id=release_id))
        deployments = db().execute(
            "SELECT * FROM deployments WHERE release_id = ? ORDER BY environment", (release_id,),
        ).fetchall()
        test = db().execute("SELECT * FROM release_tests WHERE release_id = ?", (release_id,)).fetchone()
        brief = db().execute("SELECT manifest FROM briefs WHERE revision = ?", (item["revision"],)).fetchone()
        return render_template("release.html", release=item, item=get_app(item["app_id"]),
                               deployments=deployments, test=test, loads=json.loads, results=RESULTS,
                               scenarios=brief_scenarios(json.loads(brief["manifest"])),
                               checks=commit_checks.summary(db(), release_id))

    @app.post("/releases/<int:release_id>/deployments/check")
    def check_deployment(release_id):
        item = get_release(release_id)
        environment = request.form.get("environment", "")
        row = db().execute("SELECT url FROM deployments WHERE release_id = ? AND environment = ?",
                           (release_id, environment)).fetchone()
        if row is None:
            abort(404)
        with db():
            save_check(item, environment, row["url"])
        return redirect(url_for("release", release_id=release_id))

    @app.post("/releases/<int:release_id>/test")
    def test_release(release_id):
        get_release(release_id)
        results = {key[len("result:"):]: value for key, value in request.form.items() if key.startswith("result:")}
        with db():  # the fetched checks are kept even when the test is refused
            gate = commit_checks.refresh(db(), release_id, github())
        try:
            with db():
                record_test(db(), release_id, request.form.get("commit"), request.form.get("platform"),
                            request.form.get("launch"), request.form.get("reset_result"),
                            request.form.get("reset_notes"), results, g.user, gate=gate)
        except ValueError as error:
            abort(400, str(error))
        return redirect(url_for("release", release_id=release_id))

    def github():
        return app.config.get("GITHUB") or commit_checks.GitHub()

    @app.post("/releases/<int:release_id>/checks")
    def refresh_checks(release_id):
        # ROLE: owner
        get_release(release_id)
        with db():
            commit_checks.refresh(db(), release_id, github())
        return redirect(url_for("release", release_id=release_id, _anchor="verification"))

    @app.post("/releases/<int:release_id>/verify")
    def verify_this_release(release_id):
        # ROLE: owner
        get_release(release_id)
        with db():
            verify_release(db(), release_id, github(), app.config.get("TWINLAB_RUNNER"), user=g.user)
        return redirect(url_for("release", release_id=release_id, _anchor="verification"))

    @app.post("/apps/<int:app_id>/verify-commit")
    def verify_build(app_id):
        """The “Verify this build” hook: a build's repository, head commit and pull request."""
        # ROLE: owner
        item = get_app(app_id)
        try:
            with db():
                outcome = verify_commit(
                    db(), app_id, request.form.get("repo") or commit_checks.github_repo(item["git_url"]),
                    request.form.get("sha"), github(), app.config.get("TWINLAB_RUNNER"),
                    request.form.get("pr_number", type=int), request.form.get("ref", "").strip()[:200],
                    user=g.user)
        except ValueError as error:
            abort(400, str(error))
        return redirect(url_for("release", release_id=outcome["release_id"], _anchor="verification"))

    @app.post("/apps/<int:app_id>/verification")
    def verification_settings(app_id):
        # ROLE: owner
        get_app(app_id)
        platform = request.form.get("target_platform", "")
        if platform not in commit_checks.PLATFORMS:
            abort(400, "Choose a target platform.")
        checks = "\n".join(line.strip()[:200] for line in request.form.get("required_checks", "").splitlines()
                           if line.strip())[:4000]
        lab_url = request.form.get("lab_url", "").strip()[:500]
        if lab_url and urlsplit(lab_url).scheme not in ("http", "https"):
            abort(400, "The lab agent URL must be HTTP or HTTPS.")
        with db():
            db().execute("""UPDATE apps SET required_checks = ?, target_platform = ?, windows_check_pattern = ?,
                            lab_url = ?, launch = ? WHERE id = ?""",
                         (checks, platform, request.form.get("windows_check_pattern", "").strip()[:200],
                          lab_url, request.form.get("launch", "").strip()[:8000], app_id))
        return redirect(url_for("app_setup", app_id=app_id, _anchor="verification"))

    @app.get("/releases/<int:release_id>/reports/<int:report_id>")
    def verification_report(release_id, report_id):
        content = commit_checks.get_report(db(), release_id, report_id)
        if content is None:
            abort(404)
        return send_file(io.BytesIO(content), mimetype="application/json", as_attachment=True,
                         download_name=f"verification-{release_id}-{report_id}.json")

    @app.post("/releases/<int:release_id>/publish")
    def publish_release(release_id):
        get_release(release_id)
        try:
            with db():
                publish(db(), release_id)
        except ValueError as error:
            abort(400, str(error))
        return redirect(url_for("release", release_id=release_id))

    @app.get("/releases/<int:release_id>/download")
    def download_release(release_id):
        item = db().execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        if item is None:
            abort(404)
        return send_stored(item, "archive", f"demo-{item['app_id']}-{item['version']}.zip")

    registry.register(app, db)
    uploads.register(app, db)
    builds_web.register(app, db, get_app, pack_view, database,
                        dev_worker=not trust_proxy and os.environ.get("TWINLAB2_DEV_WORKER", "1") != "0")
    return app


def wsgi():
    """The portal for a WSGI server (gunicorn "portal:wsgi()"), its catalog at TWINLAB2_DATABASE."""
    return create_app(Path(os.environ.get("TWINLAB2_DATABASE", Path(__file__).parent / ".state/catalog.db")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--database", type=Path, default=Path(__file__).parent / ".state/catalog.db")
    commands = parser.add_subparsers(dest="command")
    publish = commands.add_parser("release", help="Register a ZIP from a committed Git tree.")
    publish.add_argument("app_id", type=int)
    publish.add_argument("version")
    publish.add_argument("revision")
    publish.add_argument("repository", type=Path)
    publish.add_argument("--ref", default="HEAD")
    publish.add_argument("--subdirectory")
    moving = commands.add_parser("artifacts", help="Copy SQLite blobs to artifact storage and check them.")
    moving.add_argument("--purge", action="store_true", help="then empty the blobs whose copy verifies")
    args = parser.parse_args()
    app = create_app(args.database)
    if args.command == "release":
        from subprocess import CalledProcessError
        try:
            commit = add_release(args.database, args.app_id, args.version, args.revision,
                                 args.repository, args.ref, args.subdirectory)
        except (ValueError, sqlite3.IntegrityError, CalledProcessError, OSError) as error:
            parser.exit(1, f"Release registration failed: {error}\n")
        print(f"Registered {args.version} from {commit}.")
    elif args.command == "artifacts":
        with app.app_context(), closing(sqlite3.connect(args.database)) as connection:
            connection.row_factory = sqlite3.Row
            result = artifacts.migrate_blobs(connection)
            print(f"Copied {result['copied']} blobs to {artifacts.store().backend} artifact storage.")
            for table, key, reason in result["failed"]:
                print(f"Not copied: {table} {key}: {reason}")
            if args.purge:
                purged = artifacts.purge_blobs(connection)
                print(f"Emptied {purged['purged']} blobs; kept {len(purged['kept'])} whose copy did not verify.")
    else:
        app.run(host=args.host, port=args.port)
