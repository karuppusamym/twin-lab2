"""Local TwinLab2 catalog and build pack export."""

import argparse
from contextlib import closing
import os
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit
from zipfile import ZipFile

from flask import Flask, abort, g, redirect, render_template, request, send_file, session, url_for

import registry

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
"""

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


def add_release(database, app_id, version, revision, repository, ref, subdirectory=None):
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
        connection.execute(
            """INSERT INTO releases(app_id,version,revision,commit_sha,archive,archive_sha)
               VALUES (?,?,?,?,?,?)""",
            (app_id, version, revision, commit, archive, hashlib.sha256(archive).hexdigest()),
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
        registry.migrate(db())

    @app.before_request
    def sign_in():
        """Every page needs a signed-in user: the OpenShift OAuth proxy's user when the portal
        runs behind it, a password when one is set, else only this machine. The agent API has
        its own bearer token (registry)."""
        if request.path.startswith(("/api/agent/", "/static/")) or request.endpoint in ("login", "health"):
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
        if request.path.startswith("/api/agent/"):
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
            "SELECT id, version, revision, commit_sha, archive_sha FROM releases WHERE app_id = ? ORDER BY id DESC",
            (app_id,),
        ).fetchall()

    @app.get("/")
    def home():
        return render_template(
            "catalog.html", apps=db().execute("SELECT * FROM apps ORDER BY status = 'not_replicated', name").fetchall(),
            recordings=db().execute("SELECT id, name FROM recordings ORDER BY name").fetchall(),
        )

    @app.post("/apps")
    def add_app():
        name = request.form.get("name", "").strip()
        git_url = request.form.get("git_url", "").strip()
        requirements = request.form.get("requirements", "").strip()
        if not name:
            abort(400, "Enter a demo app name.")
        with db():
            result = db().execute(
                "INSERT INTO apps(name, git_url, requirements) VALUES (?, ?, ?)",
                (name, git_url, requirements),
            )
        return redirect(url_for("demo", app_id=result.lastrowid))

    @app.route("/apps/<int:app_id>", methods=["GET", "POST"])
    def demo(app_id):
        item = get_app(app_id)
        if request.method == "POST":
            with db():
                db().execute(
                    "UPDATE apps SET git_url = ?, requirements = ? WHERE id = ?",
                    (request.form.get("git_url", "").strip(),
                     request.form.get("requirements", "").strip(), app_id),
                )
            return redirect(url_for("demo", app_id=app_id))
        return render_template(
            "demo.html", item=item, links=links(app_id), loads=json.loads,
            attachments=attachments(app_id), kinds=ATTACHMENT_TYPES, releases=releases(app_id),
            others=db().execute("SELECT id, name FROM apps WHERE id != ? ORDER BY name", (app_id,)).fetchall(),
        )

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
        with db():
            db().execute(
                "INSERT INTO attachments(app_id,title,kind,extension,content) VALUES (?,?,?,?,?)",
                (app_id, title, kind, extension, content),
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

    @app.get("/apps/<int:app_id>/build-pack")
    def build_pack(app_id):
        item = get_app(app_id)
        if not item["requirements"]:
            abort(400, "Add build requirements before exporting.")
        manifest = {"format": 1, "app": dict(item), "evidence": []}
        for link in links(app_id):
            selection = set(json.loads(link["selection"]))
            manifest["evidence"].append({
                "recording_id": link["id"], "notes": link["notes"],
                "events": [{"seq": event["seq"], "action": event["action"]}
                           for event in json.loads(link["events"]) if event["seq"] in selection],
            })
        files = {}
        manifest["attachments"] = []
        for attachment in attachments(app_id):
            path = f"attachments/{attachment['id']}{attachment['extension']}"
            content = attachment["content"]
            files[path] = content
            manifest["attachments"].append({
                "path": path, "title": attachment["title"], "kind": attachment["kind"],
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        contents = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
        revision = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        task = (
            f"# Build {item['name']}\n\nBrief revision `{revision}`\n\n"
            f"## Build requirements\n\n{item['requirements']}\n\n"
            "Use evidence.json for the reviewed recording notes, event references and attachments. "
            "Record assumptions and unresolved behavior in BUILD-NOTES.md. "
            "Provide a local launcher, synthetic seed/reset steps, and smoke checks for the "
            "requested business outcomes. Commit the source and run instructions to the demo repo.\n"
        )
        package = io.BytesIO()
        with ZipFile(package, "w") as archive:
            archive.writestr("BUILD_TASK.md", task)
            archive.writestr("evidence.json", contents)
            for path, content in files.items():
                archive.writestr(path, content)
        with db():
            db().execute("INSERT OR IGNORE INTO briefs VALUES (?, ?, ?)", (revision, app_id, contents))
            db().execute("INSERT OR IGNORE INTO brief_packages VALUES (?, ?)", (revision, package.getvalue()))
        return download_brief(app_id, revision)

    @app.get("/apps/<int:app_id>/briefs/<revision>")
    def download_brief(app_id, revision):
        item = get_app(app_id)
        row = db().execute("""
            SELECT archive FROM brief_packages JOIN briefs USING (revision)
            WHERE app_id = ? AND revision = ?
        """, (app_id, revision)).fetchone()
        if row is None:
            abort(404)
        slug = re.sub(r"[^a-z0-9]+", "-", item["name"].lower()).strip("-") or "demo"
        return send_file(io.BytesIO(row["archive"]), mimetype="application/zip", as_attachment=True,
                         download_name=f"{slug}-{revision[:12]}.zip")

    @app.route("/releases/<int:release_id>", methods=["GET", "POST"])
    def release(release_id):
        item = db().execute(
            "SELECT id, app_id, version, revision, commit_sha, archive_sha FROM releases WHERE id = ?",
            (release_id,),
        ).fetchone()
        if item is None:
            abort(404)
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
                    INSERT INTO deployments VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(release_id, environment) DO UPDATE SET
                    url = excluded.url, username = excluded.username, secret_ref = excluded.secret_ref
                """, (release_id, environment, url, request.form.get("username", "").strip(), secret_ref))
            return redirect(url_for("release", release_id=release_id))
        deployments = db().execute(
            "SELECT * FROM deployments WHERE release_id = ? ORDER BY environment", (release_id,),
        ).fetchall()
        return render_template("release.html", release=item, item=get_app(item["app_id"]),
                               deployments=deployments)

    @app.get("/releases/<int:release_id>/download")
    def download_release(release_id):
        item = db().execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        if item is None:
            abort(404)
        return send_file(io.BytesIO(item["archive"]), mimetype="application/zip",
                         as_attachment=True, download_name=f"demo-{item['app_id']}-{item['version']}.zip")

    registry.register(app, db)
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
    else:
        app.run(host=args.host, port=args.port)
