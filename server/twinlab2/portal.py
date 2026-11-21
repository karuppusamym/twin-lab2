"""TwinLab2's releases (Packaged -> Tested -> Published), their tests, verification and publishing,
recording imports, and the command line: `python -m twinlab2.portal` serves the portal (app.py),
`... release`, `... user`, `... artifacts` manage it."""

import argparse
from contextlib import closing
import os
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
import urllib.request


from twinlab2 import audit
from twinlab2 import auth
from twinlab2 import artifacts
from twinlab2 import db as database
from twinlab2 import event_ids
from twinlab2 import independent
from twinlab2 import jobqueue
from twinlab2 import packages
from twinlab2 import registry
from twinlab2 import scenarios
from twinlab2 import verification as commit_checks

LOCAL = auth.LOCAL


# The tables are created and changed only by migrations.py.

# A release is Packaged when its committed tree is archived, Tested once its acceptance evidence
# is stored (release_tests), and Published only after it was Tested.
STATES = ("packaged", "tested", "published")
RESULTS = ("pass", "fail", "not-run")
# A deployment is Verified when GET <deployment URL>/.twinlab/version answers 200 with JSON
# {"commit": "<40-hex Git commit>"} equal to the release's commit. Anything else leaves it a
# registered URL that is not verified.
VERSION_PATH = ".twinlab/version"


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


def verified_scope(commit, platform, user, role, method, rows, deployment_url, tested_at):
    """What a release's evidence covers, so "Verified" / "Tested" never says more than was
    checked: the commit, the platform, who ran it with which role, how (by hand, or Twin Lab's
    replay), the scenarios run and passed, and the deployment they ran against."""
    total = len(rows)
    run = sum(1 for row in rows if row["result"] != "not-run")
    passed = sum(1 for row in rows if row["result"] == "pass")
    how = "Twin Lab's replay" if method == "twinlab" else "recorded by hand"
    label = (f"Commit {commit[:12]} on {platform or 'an unnamed platform'}: {passed} of {total} scenario"
             f"{'s' if total != 1 else ''} passed ({run} run), {how} by {user}"
             f"{f' ({role})' if role else ''}, " +
             (f"against {deployment_url}." if deployment_url else "with no verified deployment."))
    return {"commit_sha": commit, "platform": platform, "tested_by": user, "role": role, "method": method,
            "scenarios_total": total, "scenarios_run": run, "scenarios_passed": passed,
            "scenarios": [{"id": row.get("id"), "name": row["name"], "result": row["result"]} for row in rows],
            "deployment_url": deployment_url or None, "tested_at": tested_at, "label": label}


def verified_deployment(connection, release_id):
    row = connection.execute("SELECT url FROM deployments WHERE release_id = ? AND verification = 'verified' "
                             "ORDER BY environment", (release_id,)).fetchone()
    return row["url"] if row else None


def tester_role(connection, app_id, user):
    """The tester's role on the application (owner, builder, viewer), else `admin` for a portal
    admin, else None (`verification`, the automated run, has none)."""
    role = auth.role_of(connection, app_id, user)
    if role is None and user and auth.is_admin_name(auth.Settings.from_env(), connection, user):
        return auth.ADMIN
    return role


def record_test(connection, release_id, commit, platform, launch, reset_result, reset_notes, results, user,
                gate=None, github=None, tested=True, method="manual", deployment_url=None):
    """Store a release's acceptance evidence and make it Tested. ValueError when the evidence is
    incomplete, is for another commit, the release is already Published, or the commit's required
    checks (and Windows validation) are not green (`verification`). `gate` is a verification
    result just computed for this release; without one the checks are fetched now.
    `tested=False` stores the evidence only: the release is (or goes back to) Packaged. Twin Lab's
    automated verification uses it when a scenario or the reset check did not pass."""
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
    stamp = registry.now()
    scope = verified_scope(release["commit_sha"], platform[:500], user, tester_role(connection, release["app_id"], user),
                           method, rows, deployment_url or verified_deployment(connection, release_id), stamp)
    connection.execute("""
        INSERT INTO release_tests(release_id, commit_sha, platform, launch, reset_result, reset_notes,
                                  results, tested_by, tested_at, scope) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(release_id) DO UPDATE SET commit_sha = excluded.commit_sha,
        platform = excluded.platform, launch = excluded.launch, reset_result = excluded.reset_result,
        reset_notes = excluded.reset_notes, results = excluded.results, tested_by = excluded.tested_by,
        tested_at = excluded.tested_at, scope = excluded.scope
    """, (release_id, release["commit_sha"], platform[:500], launch[:8000], reset_result,
          (reset_notes or "").strip()[:2000], json.dumps(rows), user, stamp, json.dumps(scope)))
    connection.execute("UPDATE releases SET state = ? WHERE id = ?", ("tested" if tested else "packaged", release_id))
    return scope


def store_brief(connection, app_id, revision, contents, package):
    """Keep a frozen build brief and its pack (as a `builder_package` artifact) under its
    revision, once: the same revision is the same bytes. No commit."""
    connection.execute("INSERT INTO briefs(revision, app_id, manifest) VALUES (?, ?, ?) ON CONFLICT (revision) DO NOTHING",
                       (revision, app_id, contents))
    if connection.execute("SELECT 1 FROM brief_packages WHERE revision = ?", (revision,)).fetchone() is None:
        ref = artifacts.save(connection, artifacts.put(package, kind="builder_package", app_id=app_id,
                                                       content_type="application/zip"))
        connection.execute("INSERT INTO brief_packages(revision, archive, artifact_id) VALUES (?, ?, ?)",
                           (revision, b"", ref.id))


def verify_commit(connection, app_id, repo, sha, github=None, runner=None, pr_number=None, ref="",
                  base_url=None, user="verification", build_id=None, revision=None):
    """“Verify this build”: a Packaged release of exactly `sha` (from GitHub), its checks, Twin
    Lab's independent checks against a deployment reporting that commit, and the Tested
    transition when all of it holds. The release stays Packaged otherwise. The verification
    report is stored either way. Returns {release_id, tested, reasons, report, report_sha}."""
    repo, sha = commit_checks.check_commit_args(repo, sha)
    github = github or commit_checks.GitHub()
    if not scenarios.load(connection, app_id):
        raise ValueError("Add a business scenario before verifying a build.")
    # The package the build answers: the latest builder-approved (frozen) revision, the only
    # artifact a builder was given.
    # A build session answers the revision it was given (`revision`).
    frozen = packages.by_revision(connection, app_id, revision) if revision else packages.latest(connection, app_id)
    if frozen is None:
        raise ValueError("Freeze a builder package before verifying a build: a build answers an approved package.")
    revision = frozen["revision"]
    release_id = commit_checks.package_commit(connection, app_id, repo, sha, github, revision, pr_number, ref,
                                              build_id=build_id)
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
                       "so the scenario checks did not run.")
    else:
        observed = deployed_commit(base_url)
        if observed != sha:
            reasons.append(f"{base_url} reports " + (f"commit {observed[:12]}" if observed else "no commit") +
                           f", not {sha[:12]}: the scenario checks need the built commit.")
        else:
            twin = independent.run(runner or independent.TwinLabChecks(), brief_scenarios(manifest), manifest,
                                   base_url, app["lab_url"] or "")
            reasons += twin.get("problems") or []
    tested, scope = False, None
    if twin is not None:  # what the replay covered, Tested or not
        role = tester_role(connection, release["app_id"], user)
        platform = commit_checks.PLATFORMS[app["target_platform"]] if app["target_platform"] else ""
        scope = verified_scope(sha, platform, user, role, "twinlab",
                               [{"id": s["id"], "name": s["name"], "result": s["result"]} for s in twin["scenarios"]],
                               base_url, registry.now())
    if twin is not None and not twin.get("problems"):
        reset = twin["reset"] or {}
        platform = commit_checks.PLATFORMS[app["target_platform"]] if app["target_platform"] else ""
        # Tested only when all of it holds: every scenario passed and the reset restored the
        # baseline. Otherwise the results are kept as evidence and the release stays Packaged.
        held = [f"Scenario “{s['name']}”: {s['result']}" + (f" ({s['why']})" if s.get("why") else "") + "."
                for s in twin["scenarios"] if s["result"] != "pass"]
        if reset.get("result") == "fail":
            held.append("The reset check failed: " + (reset.get("notes") or "no notes") + ".")
        try:
            scope = record_test(connection, release_id, sha, platform, app["launch"], reset.get("result"),
                                reset.get("notes"), {s["id"]: s["result"] for s in twin["scenarios"]}, user,
                                gate=gate, tested=not held, method="twinlab", deployment_url=base_url)
            tested = not held
            reasons += [f"Not Tested: {item}" for item in held]
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
        # what "Tested" covers: scenarios run and passed, platform, role, commit, deployment URL
        "scope": scope,
    }
    digest = commit_checks.put_report(connection, release_id, report)
    return {"release_id": release_id, "tested": tested, "reasons": reasons, "report": report, "report_sha": digest}


class PublishConflict(ValueError):
    """Another release of the application is being published right now."""


PUBLISH_LOCK_SECONDS = 120


def acquire_publish(connection, app_id, release_id, holder):
    """Take the application's publish lock (committed at once, so a second publisher sees it).
    PublishConflict while another publish holds it; a lock older than PUBLISH_LOCK_SECONDS
    belongs to a publisher that died and is taken over."""
    stamp, until = registry.now(), jobqueue.later(PUBLISH_LOCK_SECONDS)
    cursor = connection.execute(
        """INSERT INTO publish_locks(app_id, release_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (app_id) DO UPDATE SET release_id = excluded.release_id, holder = excluded.holder,
           acquired_at = excluded.acquired_at, expires_at = excluded.expires_at
           WHERE publish_locks.expires_at < ?""", (app_id, release_id, holder, stamp, until, jobqueue.stamp()))
    connection.commit()
    if cursor.rowcount != 1:
        row = connection.execute("SELECT * FROM publish_locks WHERE app_id = ?", (app_id,)).fetchone()
        raise PublishConflict(f"Release {row['release_id']} of this application is being published by {row['holder']}: "
                              "try again when it has finished.")


def release_publish(connection, app_id, holder):
    connection.execute("DELETE FROM publish_locks WHERE app_id = ? AND holder = ?", (app_id, holder))
    connection.commit()


def publish(connection, release_id):
    """Make a Tested release Published; ValueError for any other state."""
    row = connection.execute("SELECT state FROM releases WHERE id = ?", (release_id,)).fetchone()
    if row is None:
        raise LookupError("No such release.")
    if row["state"] != "tested":
        raise ValueError("Only a tested release can be published: record its test results first."
                         if row["state"] == "packaged" else "This release is already published.")
    updated = connection.execute("UPDATE releases SET state = 'published', published_at = ? WHERE id = ? "
                                 "AND state = 'tested'", (registry.now(), release_id)).rowcount
    if updated != 1:
        raise ValueError("This release is already published.")


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
    "API": {".json", ".har", ".yaml", ".yml", ".md", ".txt"},
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
    return sorted(events, key=lambda event: (event.get("time") or "", event["seq"], event.get("event_id") or ""))


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
        event_id = row.get("event_id")
        if event_id is not None and event_ids.clean(event_id) is None:
            raise ValueError(f"Line {number} has an invalid event_id.")
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
        if event_id is not None:
            events[seq]["event_id"] = event_ids.clean(event_id)
    if not events:
        raise ValueError("The event log is empty.")
    if len(session_ids) != 1:
        raise ValueError("An upload must contain events from a single recording session.")
    session_id = session_ids.pop()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    recording_id = session_id or digest
    found = event_ids.assign(list(events.values()), recording_id)  # an event without one: derived from its seq
    if len({event["event_id"] for event in found}) != len(found):
        raise ValueError("Each event must have its own event_id.")
    return recording_id, ordered(found)


def upload_view(data):
    """What the recording page says about an upload, from the storage API's answer (None when
    there is none). The answer's shape is read leniently: a state, bytes or parts, a percentage."""
    if not isinstance(data, dict):
        return None

    def first(*names):
        for name in names:
            value = data.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    state = str(data.get("state") or data.get("status") or "").strip()[:40]
    if state.lower() == "none":  # the storage API: this recording has no upload
        return None
    done = first("received_bytes", "bytes_received", "uploaded_bytes", "uploaded", "received")
    total = first("total_bytes", "bytes_total", "size", "total")
    parts, of = first("parts_received", "received_parts", "parts_done"), first("parts_total", "total_parts")
    percent = first("percent", "progress")
    if percent is not None and percent <= 1 and "percent" not in data:
        percent *= 100
    if percent is None and done is not None and total:
        percent = done / total * 100
    if percent is None and parts is not None and of:
        percent = parts / of * 100
    percent = None if percent is None else max(0, min(100, round(percent)))
    complete = state.lower() in ("complete", "completed", "done", "uploaded", "stored", "confirmed") or percent == 100
    words = [state.replace("_", " ").capitalize() or ("Complete" if complete else "Uploading")]
    if percent is not None:
        words.append(f"{percent}%")
    if done is not None and total:
        words.append(f"({human(done)} of {human(total)})")
    elif parts is not None and of:
        words.append(f"({parts} of {of} parts)")
    if isinstance(data.get("last_error"), str) and data["last_error"]:
        words.append(f"· {data['last_error'][:300]}")
    return {"text": " ".join(words), "percent": percent, "complete": complete}


def human(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def add_release(target, app_id, version, revision, repository, ref, subdirectory=None, store=None):
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Use a version such as 1.0.0.")
    with closing(database.connect(target)) as connection, connection:
        brief = connection.execute(
            "SELECT 1 FROM briefs WHERE revision = ? AND app_id = ?", (revision, app_id),
        ).fetchone()
        if brief is None:
            raise ValueError("Freeze a builder package for this app before registering its release.")
        frozen = connection.execute("SELECT COUNT(*) FROM builder_packages WHERE app_id = ?", (app_id,)).fetchone()[0]
        if frozen and connection.execute("SELECT 1 FROM builder_packages WHERE app_id = ? AND revision = ?",
                                         (app_id, revision)).fetchone() is None:
            raise ValueError("Register a release against a frozen builder package revision.")

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
            raise ValueError(f"Version {version} is already registered for this app.")
        ref = artifacts.save(connection, artifacts.put(
            archive, kind="release_download", app_id=app_id, content_type="application/zip",
            target=store or artifacts.from_env(target)))
        connection.execute(
            """INSERT INTO releases(app_id,version,revision,commit_sha,archive,archive_sha,artifact_id)
               VALUES (?,?,?,?,?,?,?)""",
            (app_id, version, revision, commit, b"", ref.sha256, ref.id),
        )
    return commit


def migrate_owners(db, fallback=None):
    """The free-text owner of catalogs made before roles becomes an owner assignment. An
    application without one gets `fallback`: the machine user `local` when the portal serves
    only this machine (who owned everything before), else nobody until an admin assigns one."""
    rows = db.execute("""SELECT id, trim(owner) AS owner FROM apps
                         WHERE NOT EXISTS (SELECT 1 FROM app_roles WHERE app_roles.app_id = apps.id)""").fetchall()
    for app_id, owner in rows:
        owner = owner or fallback
        if owner and auth.valid_name(owner):
            auth.assign(db, app_id, owner, "owner", "migration", registry.now())
            audit.record(db, "migration", "role.assign", "role", f"{app_id}/{owner}", app_id=app_id,
                         user=owner, role="owner", previous=None, reason="owner text field before roles")


def create_app(target, store=None):
    """The portal (an ASGI app, app.py) on the database at `target`."""
    from twinlab2.app import create_app as make

    return make(target, store)


def asgi():
    """The portal for an ASGI server (see app.asgi)."""
    from twinlab2.app import asgi as make

    return make()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--database", default=database.from_env(),
                        help="a DATABASE_URL (sqlite:///…, postgresql://…) or a SQLite file")
    commands = parser.add_subparsers(dest="command")
    releasing = commands.add_parser("release", help="Register a ZIP from a committed Git tree.")
    releasing.add_argument("app_id", type=int)
    releasing.add_argument("version")
    releasing.add_argument("revision")
    releasing.add_argument("repository", type=Path)
    releasing.add_argument("--ref", default="HEAD")
    releasing.add_argument("--subdirectory")
    users = commands.add_parser("user", help="Add or change a local user (dev sign-in) or a portal admin.")
    users.add_argument("name")
    users.add_argument("--admin", action="store_true", help="make the user a portal admin")
    users.add_argument("--password", action="store_true", help="set a password (asked for)")
    moving = commands.add_parser("artifacts", help="Copy SQLite blobs to artifact storage and check them.")
    moving.add_argument("--purge", action="store_true", help="then empty the blobs whose copy verifies")
    args = parser.parse_args()
    if args.command == "release":
        from subprocess import CalledProcessError
        try:
            commit = add_release(args.database, args.app_id, args.version, args.revision,
                                 args.repository, args.ref, args.subdirectory)
        except (ValueError, *database.IntegrityError, CalledProcessError, OSError) as error:
            parser.exit(1, f"Release registration failed: {error}\n")
        print(f"Registered {args.version} from {commit}.")
    elif args.command == "user":
        import getpass
        secret = getpass.getpass(f"Password for {args.name}: ") if args.password else None
        with closing(database.connect(args.database)) as connection, connection:
            auth.save_user(connection, args.name, password=secret, is_admin=True if args.admin else None,
                           by="cli", now=registry.now())
            audit.record(connection, "cli", "user.save", "user", args.name, user=args.name,
                         is_admin=args.admin, password_changed=bool(secret))
        print(f"Saved user {args.name}.")
    elif args.command == "artifacts":
        create_app(args.database)  # migrations first
        with closing(database.connect(args.database)) as connection:
            result = artifacts.migrate_blobs(connection)
            print(f"Copied {result['copied']} blobs to {artifacts.store().backend} artifact storage.")
            for table, key, reason in result["failed"]:
                print(f"Not copied: {table} {key}: {reason}")
            if args.purge:
                purged = artifacts.purge_blobs(connection)
                print(f"Emptied {purged['purged']} blobs; kept {len(purged['kept'])} whose copy did not verify.")
    else:
        import uvicorn

        os.environ["DATABASE_URL"] = str(args.database)
        uvicorn.run("twinlab2.app:asgi", factory=True, host=args.host, port=args.port, reload=args.reload,
                    proxy_headers=False)
