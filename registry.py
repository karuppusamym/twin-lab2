"""Machines, recording sessions, applications found by recordings, and automations.

The workstation recorder (agent/) never uploads a recording. It sends **references** at the end
of a session: the session, the applications it recorded (identity and level), and per
application a flow of filtered step labels (Twin Lab's labctl.workstation.flow). This module:

- pairs machines: a signed-in user makes a one-time code (10 characters from a 32-symbol
  alphabet, valid 10 minutes, single use); the recorder exchanges it for a machine token, of
  which only the SHA-256 is stored. Machines can be revoked;
- serves the agent API under `/api/agent/`: bearer token only, JSON with a fixed schema;
- offers sessions to a machine (made in the portal: name, target applications, automation):
  recording starts only when the person at the PC presses Start;
- maps references to applications by identity key: one not in the catalog is registered with
  status `discovered`. The recording is linked to every application it recorded, with that
  application's flow and the selected steps (the catalog's evidence links), and a session made
  for an automation links the automation to those applications.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlsplit

from flask import abort, g, jsonify, redirect, render_template, request, session, url_for
import yaml

import audit
import auth
from auth import require_role
import completeness as compl
import event_ids
import recorder_contract as contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_identities (
    key TEXT PRIMARY KEY, app_id INTEGER NOT NULL REFERENCES apps(id), surface TEXT NOT NULL,
    facts TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_sha TEXT NOT NULL UNIQUE,
    paired_by TEXT NOT NULL, paired_at TEXT NOT NULL, last_seen TEXT, revoked_at TEXT,
    recorder TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_sha TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, used_at TEXT, machine_id INTEGER
);
CREATE TABLE IF NOT EXISTS pairing_failures (at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS automation_apps (
    automation_id INTEGER NOT NULL REFERENCES automations(id),
    app_id INTEGER NOT NULL REFERENCES apps(id), PRIMARY KEY (automation_id, app_id)
);
CREATE TABLE IF NOT EXISTS capture_sessions (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, machine_id INTEGER NOT NULL REFERENCES machines(id),
    targets TEXT NOT NULL, automation_id INTEGER REFERENCES automations(id),
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
    recording_id TEXT
);
CREATE TABLE IF NOT EXISTS session_refs (
    recording_id TEXT PRIMARY KEY REFERENCES recordings(id), session_key TEXT NOT NULL UNIQUE,
    machine_id INTEGER REFERENCES machines(id), capture_session_id INTEGER,
    automation_id INTEGER REFERENCES automations(id), payload TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_apps (
    recording_id TEXT NOT NULL REFERENCES recordings(id), app_id INTEGER NOT NULL REFERENCES apps(id),
    key TEXT NOT NULL, level TEXT NOT NULL, flow TEXT NOT NULL,
    PRIMARY KEY (recording_id, app_id, key)
);
CREATE TABLE IF NOT EXISTS recording_contracts (
    recording_id TEXT PRIMARY KEY REFERENCES recordings(id), contract_version TEXT NOT NULL,
    recorder TEXT NOT NULL, recorder_version TEXT NOT NULL, twinlab_commit TEXT,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_completeness (
    recording_id TEXT PRIMARY KEY REFERENCES recordings(id), completeness TEXT, preflight TEXT,
    accepted_by TEXT, accepted_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS machine_contracts (
    machine_id INTEGER PRIMARY KEY REFERENCES machines(id), contract_version TEXT NOT NULL,
    recorder TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O or 1/I
CODE_LENGTH = 10
CODE_MINUTES = 10
FAILURES_PER_10_MIN = 20
STATUSES = ("known", "discovered", "not_replicated")
LEVELS = ("full", "steps")
KEY = re.compile(r"^(desktop:[^\x00-\x1f]{1,200}|exe:[\w .()+-]{1,80}\.exe"
                 r"|web:https?://[\w.\-\[\]:]{1,200}(/[^\s\x00-\x1f?#]{1,300})?"
                 r"|mainframe:[\w.\-\[\]:]{1,200}:\d{1,5})$",
                 re.I)  # fmt: skip
SESSION_KEY = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
FLOW_FIELDS = {"n": int, "action": str, "control": str, "label": str, "screen": str, "key": str,
               "channel": str, "captured_at": str, "seq": int}  # and event_id, checked on its own


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def migrate(db):
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(apps)")}
    if "status" not in columns:
        db.execute("ALTER TABLE apps ADD COLUMN status TEXT NOT NULL DEFAULT 'known'")
    if "surface" not in columns:
        db.execute("ALTER TABLE apps ADD COLUMN surface TEXT NOT NULL DEFAULT ''")
    if "url_match" not in columns:
        # how the application's web addresses match: `path` (longest path prefix wins) or
        # `host` (the owner's override: the addresses stand for their whole host). Identities
        # stored before paths were kept are host-only, so they match exactly as they did.
        db.execute("ALTER TABLE apps ADD COLUMN url_match TEXT NOT NULL DEFAULT 'path'")
    columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
    if "reviewed" not in columns:
        # links stored before this column existed are treated as reviewed: a resend then only
        # suggests events, it never changes their selection.
        db.execute("ALTER TABLE evidence ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 1")
    if "suggested" not in columns:
        db.execute("ALTER TABLE evidence ADD COLUMN suggested TEXT NOT NULL DEFAULT '[]'")
    db.commit()


def web_parts(key):
    """`web:https://host/orders` -> (`web:https://host`, `/orders`); a host-only key has path ``."""
    rest = key[4:]
    cut = rest.find("/", rest.find("//") + 2)
    return ("web:" + rest, "") if cut < 0 else ("web:" + rest[:cut], rest[cut:])


def path_matches(prefix, path):
    return not prefix or path == prefix or path.startswith(prefix + "/")


def normalise_key(text):
    """An identity key from what a person types: `desktop:Company/Product`, an executable
    name (`exe:CaseDesk.exe`), or a web address (`web:https://host[:port][/path]`). A web
    address keeps its path (without query, fragment or trailing slash): applications that
    share a host are told apart by path, the longest matching path prefix winning."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        text = "web:" + text
    if text.lower().startswith(("tn3270://", "tn3270s://")):
        text = "mainframe:" + text.split("://", 1)[1].rstrip("/")
    if text.startswith("mainframe:"):
        rest = text[10:].lower()
        text = "mainframe:" + (rest if re.search(r":\d{1,5}$", rest) else rest + ":23")
    if text.lower().endswith(".exe") and ":" not in text:
        text = "exe:" + text
    if text.startswith("web:"):
        try:
            parts = urlsplit(text[4:])
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname or parts.username:
            return None
        default = {"http": 80, "https": 443}[scheme]
        host = parts.hostname.lower()
        path = re.sub(r"/{2,}", "/", parts.path or "").rstrip("/")
        text = f"web:{scheme}://{host}" + (f":{port}" if port and port != default else "") + path
    if text.startswith("exe:"):
        text = "exe:" + text[4:].lower()
    return text if KEY.match(text) else None


def target_of(key, level, app_id, name):
    target = {"level": level, "app_id": app_id, "name": name}
    if key.startswith("web:"):
        # the recorder records the whole origin; the portal assigns steps to an application
        # by path when the references arrive
        origin, path = web_parts(key)
        target["origin"] = origin[4:]
        if path:
            target["path"] = path
    elif key.startswith("exe:"):
        target["exe"] = key[4:]
    elif key.startswith("mainframe:"):
        target["tn3270"] = key[10:]
    else:
        target["key"] = key
    return target


# --- validation of what a recorder sends ---------------------------------------------------


def _text(value, limit, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or re.search(r"[\x00-\x1f]", value):
        raise ValueError("invalid text")
    return value


def clean_references(doc):
    """The references in the fixed schema, or ValueError. Anything not in the schema is dropped."""
    if not isinstance(doc, dict) or doc.get("format") != "twinlab-references/1":
        raise ValueError("not twinlab-references/1")
    s = doc.get("session")
    if not isinstance(s, dict) or not SESSION_KEY.match(str(s.get("id") or "")):
        raise ValueError("session.id")
    out_session = {"id": s["id"], "name": _text(s.get("name") or s["id"], 120, True)}
    for k in ("started", "ended"):
        out_session[k] = _text(s.get(k), 40)
    out_session["state"] = s.get("state") if s.get("state") in ("stopped", "recovered") else "stopped"
    for k in ("automation_id", "portal_session_id"):
        if isinstance(s.get(k), int) and not isinstance(s.get(k), bool):
            out_session[k] = s[k]
    commit = s.get("twinlab_commit")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit):
        out_session["twinlab_commit"] = commit
    apps = []
    for a in (doc.get("apps") or [])[:50]:
        if not isinstance(a, dict):
            raise ValueError("apps")
        key = normalise_key(a.get("key"))
        if key is None or key.startswith("exe:"):
            raise ValueError("app key")
        if a.get("level") not in LEVELS:
            raise ValueError("app level")
        surface = key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop"
        item = {"key": key, "level": a["level"], "surface": surface}
        for k, limit in (("display", 120), ("exe", 120), ("company", 120), ("product", 120),
                         ("version", 40), ("toolkit", 40)):  # fmt: skip
            value = a.get(k)
            if isinstance(value, str) and value and len(value) <= limit and not re.search(r"[\x00-\x1f]", value):
                item[k] = value
        if isinstance(a.get("app_id"), int) and not isinstance(a.get("app_id"), bool):
            item["app_id"] = a["app_id"]
        apps.append(item)
    keys = {a["key"] for a in apps}
    flows = {}
    raw_flows = doc.get("flows") if isinstance(doc.get("flows"), dict) else {}
    seen_ids = set()
    for key, steps in raw_flows.items():
        nkey = normalise_key(key)
        if nkey not in keys or not isinstance(steps, list):
            continue
        clean = []
        if len(steps) > 50000:
            raise ValueError("flow longer than 50000 steps")
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("n"), int):
                raise ValueError("flow step")
            row = {}
            for field, kind in FLOW_FIELDS.items():
                value = step.get(field)
                if isinstance(value, kind) and not isinstance(value, bool) and (
                    kind is int or (0 < len(value) <= 160 and not re.search(r"[\x00-\x1f]", value))
                ):
                    row[field] = value
            if row.get("channel") not in (None, *compl.CHANNELS):
                del row["channel"]
            if step.get("event_id") is not None:
                event_id = event_ids.clean(step["event_id"])
                if event_id is None:
                    raise ValueError("event_id")
                if event_id in seen_ids:
                    raise ValueError(f"event_id {event_id} appears twice")
                seen_ids.add(event_id)
                row["event_id"] = event_id
            clean.append(row)
        flows[nkey] = clean
    timeline = []
    for run in (doc.get("timeline") or [])[:20000]:
        if not isinstance(run, dict) or not isinstance(run.get("steps"), int):
            raise ValueError("timeline")
        app = run.get("app")
        app = "presence" if app == "presence" else normalise_key(app)
        if app is None or (app != "presence" and app not in keys):
            raise ValueError("timeline app")
        timeline.append({"app": app, "steps": run["steps"]})
    out = {"format": "twinlab-references/1", "session": out_session, "apps": apps,
           "flows": flows, "timeline": timeline}  # fmt: skip
    for name, cleaner in (("completeness", compl.clean), ("preflight", compl.clean_preflight)):
        value = cleaner(doc.get(name))
        if value is not None:
            out[name] = value
    return out


def recording_status(db, recording_id):
    """What the pages say about a recording's completeness: the recorder's report (None from an
    older recorder or an imported event log), its preflight, the owner's acceptance, and the
    sentence and label to show. `attention` is True for a partial or failed recording nobody has
    accepted: a stopped recording is not complete by itself."""
    row = db.execute("SELECT * FROM recording_completeness WHERE recording_id = ?", (recording_id,)).fetchone()
    workstation = db.execute("SELECT 1 FROM session_refs WHERE recording_id = ?", (recording_id,)).fetchone()
    data = json.loads(row["completeness"]) if row is not None and row["completeness"] else None
    accepted = bool(row is not None and row["accepted_at"])
    if data is None:
        text = compl.UNKNOWN_OLDER if workstation else compl.UNKNOWN_IMPORTED
    else:
        text = compl.summary(data)
    return {"completeness": data, "accepted": accepted,
            "accepted_by": row["accepted_by"] if accepted else None, "accepted_at": row["accepted_at"] if accepted else None,
            "preflight": json.loads(row["preflight"]) if row is not None and row["preflight"] else None,
            "summary": text, "label": compl.label(data, accepted), "attention": compl.needs_attention(data, accepted),
            "workstation": bool(workstation)}


def contract_refusal(error):
    """The answer to a recorder outside the contract: what to update, and the versions.
    426 when the version is not accepted (update the recorder or the portal), 400 for a
    message of an accepted version that is not in the contract's shape."""
    body = {"error": str(error), "update": error.update,
            "contract": {"portal": contract.CONTRACT_VERSION, "supported": contract.supported()}}
    if error.errors:
        body["errors"] = error.errors[:20]
    return jsonify(body), 400 if error.errors else 426


def recorder_of(doc):
    """The sending recorder's name and version, from the message (else the request header)."""
    rec = doc.get("recorder") if isinstance(doc.get("recorder"), dict) else {}
    if rec.get("name") and rec.get("version"):
        return str(rec["name"])[:60], str(rec["version"])[:40]
    name, _, version = request.headers.get("X-TwinLab-Recorder", "").strip().partition(" ")
    return name[:60] or "unknown", version[:40] or "unknown"


def register(app, db):  # noqa: C901 - one place for the routes of this module
    """Add the routes to the Flask app; `db()` returns the request's SQLite connection."""

    # -- sign-in and the agent's bearer token -----------------------------------------------

    def current_user():
        return g.get("user")

    def record(action, object_type, object_id, app_id=None, **details):
        audit.record(db(), current_user(), action, object_type, object_id, app_id=app_id, **details)

    def reviewer_of_recording(recording_id):
        return app.extensions["twinlab_reviewer_of_recording"](recording_id)

    def owned(app_ids):
        """Abort 403 unless the user owns every application in `app_ids`."""
        for app_id in app_ids:
            auth.ensure("owner", app_id=app_id)

    def agent_machine():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            abort(401)
        token = header[7:].strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{40,100}", token):
            abort(401)
        row = db().execute("SELECT * FROM machines WHERE token_sha = ? AND revoked_at IS NULL",
                           (sha(token),)).fetchone()  # fmt: skip
        if row is None:
            abort(401)
        with db():
            db().execute("UPDATE machines SET last_seen = ?, recorder = ? WHERE id = ?",
                         (now(), request.headers.get("X-TwinLab-Recorder", "")[:60], row["id"]))  # fmt: skip
        return row

    app.jinja_env.globals["current_user"] = current_user
    app.jinja_env.globals["statuses"] = STATUSES

    # -- machines and pairing -----------------------------------------------------------------

    @app.get("/machines")
    @auth.signed_in
    def machines():
        """A user sees the machines they paired; a portal admin sees all."""
        rows = [row for row in db().execute("""
            SELECT machines.*, machine_contracts.contract_version FROM machines
            LEFT JOIN machine_contracts ON machine_contracts.machine_id = machines.id
            ORDER BY revoked_at IS NOT NULL, name
        """).fetchall() if g.is_admin or row["paired_by"] == current_user()]
        return render_template("machines.html", machines=rows, code=session.pop("pair_code", None),
                               minutes=CODE_MINUTES)  # fmt: skip

    @app.post("/machines/pair")
    @auth.signed_in
    def make_code():
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        created = datetime.now(timezone.utc)
        with db():
            db().execute("INSERT INTO pairing_codes VALUES (?, ?, ?, ?, NULL, NULL)",
                         (sha(code), current_user(), created.isoformat(),
                          (created + timedelta(minutes=CODE_MINUTES)).isoformat()))  # fmt: skip
        session["pair_code"] = code
        return redirect(url_for("machines"))

    @app.post("/machines/<int:machine_id>/revoke")
    @auth.signed_in
    def revoke(machine_id):
        row = db().execute("SELECT paired_by FROM machines WHERE id = ?", (machine_id,)).fetchone()
        if row is None:
            abort(404)
        if not g.is_admin and row["paired_by"] != current_user():
            abort(403, "Only the user who paired a machine, or a portal admin, can revoke it.")
        with db():
            db().execute("UPDATE machines SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (now(), machine_id))  # fmt: skip
        return redirect(url_for("machines"))

    @app.post("/api/agent/pair")
    @auth.agent_token
    def agent_pair():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            body = {}
        try:  # before the code is looked at: an outdated recorder does not use up the code
            contract.validate(body, "pair")
        except contract.ContractError as error:
            return contract_refusal(error)
        code = str(body.get("code") or "").strip().upper().replace("-", "").replace(" ", "")
        name = str(body.get("name") or "").strip()[:80]
        if not name or re.search(r"[\x00-\x1f]", name):
            return jsonify(error="give the machine a name"), 400
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        failures = db().execute("SELECT COUNT(*) FROM pairing_failures WHERE at > ?", (cutoff,)).fetchone()[0]
        if failures >= FAILURES_PER_10_MIN:
            return jsonify(error="too many wrong codes: wait ten minutes"), 429
        row = None
        if len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET):
            row = db().execute("SELECT * FROM pairing_codes WHERE code_sha = ? AND used_at IS NULL",
                               (sha(code),)).fetchone()  # fmt: skip
        if row is None or row["expires_at"] < datetime.now(timezone.utc).isoformat():
            with db():
                db().execute("INSERT INTO pairing_failures VALUES (?)", (now(),))
            return jsonify(error="the code is wrong, used or expired"), 403
        token = secrets.token_urlsafe(32)
        with db():
            used = db().execute("UPDATE pairing_codes SET used_at = ? WHERE code_sha = ? AND used_at IS NULL",
                                (now(), row["code_sha"])).rowcount  # fmt: skip
            if used != 1:
                abort(403)
            machine = db().execute(
                "INSERT INTO machines(name, token_sha, paired_by, paired_at, recorder) VALUES (?,?,?,?,?)",
                (name, sha(token), row["created_by"], now(),
                 request.headers.get("X-TwinLab-Recorder", "")[:60]),
            ).lastrowid  # fmt: skip
            db().execute("UPDATE pairing_codes SET machine_id = ? WHERE code_sha = ?", (machine, row["code_sha"]))
            rec_name, rec_version = recorder_of(body)
            db().execute("INSERT OR REPLACE INTO machine_contracts VALUES (?, ?, ?, ?)",
                         (machine, body["contract_version"], f"{rec_name} {rec_version}", now()))  # fmt: skip
        return jsonify(token=token, machine_id=machine, name=name, contract_version=contract.CONTRACT_VERSION,
                       contract_supported=contract.supported())  # fmt: skip

    # -- sessions offered to a machine ---------------------------------------------------------

    def identities(app_id):
        return db().execute("SELECT * FROM app_identities WHERE app_id = ? ORDER BY key", (app_id,)).fetchall()

    def deny_keys():
        """Keys the recorder must not record in full. The recorder knows origins, not paths, so a
        path identity is not sent: denying it would deny the applications sharing its host."""
        return [r["key"] for r in db().execute("""
            SELECT key FROM app_identities JOIN apps ON apps.id = app_identities.app_id
            WHERE apps.status = 'not_replicated' AND key NOT LIKE 'exe:%'
        """) if not r["key"].startswith("web:") or not web_parts(r["key"])[1]]  # fmt: skip

    def web_identities(origin):
        """The identities on one origin as (key, app_id, prefix): a `host` application's prefix is
        empty, so its addresses stand for the whole host."""
        rows = db().execute("""
            SELECT app_identities.key, app_identities.app_id, apps.url_match FROM app_identities
            JOIN apps ON apps.id = app_identities.app_id
            WHERE app_identities.key = ? OR substr(app_identities.key, 1, ?) = ?
        """, (origin, len(origin) + 1, origin + "/")).fetchall()
        return [(r["key"], r["app_id"], "" if r["url_match"] == "host" else web_parts(r["key"])[1]) for r in rows]

    def resolve_web(key):
        """The identity a web address belongs to: the longest matching path prefix wins; a
        host-only identity (or a `host` application) matches every path on its host."""
        origin, path = web_parts(key)
        best = None
        for ident, app_id, prefix in web_identities(origin):
            if path_matches(prefix, path):
                rank = (len(prefix), ident == key, -app_id)
                if best is None or rank > best[0]:
                    best = (rank, ident, app_id)
        return (best[1], best[2]) if best else None

    def web_clash(app_id, key, url_match):
        """Another application that already claims the same host and path as `key` would."""
        origin, path = web_parts(key)
        prefix = "" if url_match == "host" else path
        for ident, other, other_prefix in web_identities(origin):
            if other != app_id and other_prefix == prefix:
                return other
        return None

    @app.get("/sessions")
    @auth.signed_in
    def sessions():
        """Sessions the user requested (all for an admin), for applications the user owns."""
        rows = [row for row in db().execute("""
            SELECT capture_sessions.*, machines.name AS machine, automations.name AS automation
            FROM capture_sessions JOIN machines ON machines.id = capture_sessions.machine_id
            LEFT JOIN automations ON automations.id = capture_sessions.automation_id
            ORDER BY capture_sessions.id DESC
        """).fetchall() if g.is_admin or row["created_by"] == current_user()]
        apps = [row for row in db().execute("""
            SELECT apps.*, (SELECT COUNT(*) FROM app_identities WHERE app_id = apps.id) AS ids
            FROM apps WHERE status != 'not_replicated' ORDER BY name
        """).fetchall() if auth.can("owner", app_id=row["id"])]
        return render_template(
            "sessions.html", sessions=rows, apps=apps, loads=json.loads,
            machines=[m for m in db().execute("SELECT * FROM machines WHERE revoked_at IS NULL ORDER BY name")
                      .fetchall() if g.is_admin or m["paired_by"] == current_user()],
            automations=db().execute("SELECT * FROM automations ORDER BY name").fetchall(),
        )

    @app.post("/sessions")
    @auth.signed_in
    def add_session():
        name = request.form.get("name", "").strip()[:120]
        machine_id = request.form.get("machine_id", type=int)
        automation_id = request.form.get("automation_id", type=int) or None
        if not name or db().execute("SELECT 1 FROM machines WHERE id = ? AND revoked_at IS NULL",
                                    (machine_id,)).fetchone() is None:  # fmt: skip
            abort(400, "Name the session and choose a paired machine.")
        targets = []
        for value in request.form.getlist("app"):
            try:
                app_id = int(value)
            except ValueError:
                abort(400, "Invalid application.")
            level = request.form.get(f"level_{app_id}", "full")
            if level not in LEVELS:
                abort(400, "Choose full or steps.")
            item = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
            if item is not None:
                owned([app_id])
            ids = identities(app_id) if item else []
            if item is None or not ids:
                abort(400, "Each target application needs an identity (an executable or a web address).")
            for row in ids:
                targets.append(target_of(row["key"], level, app_id, item["name"]))
        if not targets:
            abort(400, "Choose at least one target application.")
        if automation_id is not None and db().execute("SELECT 1 FROM automations WHERE id = ?",
                                                      (automation_id,)).fetchone() is None:  # fmt: skip
            abort(400, "Unknown automation.")
        with db():
            db().execute("""INSERT INTO capture_sessions(name, machine_id, targets, automation_id,
                            created_by, created_at, status) VALUES (?,?,?,?,?,?, 'offered')""",
                         (name, machine_id, json.dumps(targets), automation_id, current_user(), now()))  # fmt: skip
        return redirect(url_for("sessions"))

    @app.post("/sessions/<int:session_id>/withdraw")
    @auth.signed_in
    def withdraw_session(session_id):
        row = db().execute("SELECT created_by FROM capture_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            abort(404)
        if not g.is_admin and row["created_by"] != current_user():
            abort(403, "Only who requested a recording session, or a portal admin, can withdraw it.")
        with db():
            db().execute("UPDATE capture_sessions SET status = 'withdrawn' WHERE id = ? AND status = 'offered'",
                         (session_id,))  # fmt: skip
        return redirect(url_for("sessions"))

    @app.get("/api/agent/sessions")
    @auth.agent_token
    def agent_sessions():
        machine = agent_machine()
        rows = db().execute("SELECT * FROM capture_sessions WHERE machine_id = ? AND status = 'offered' ORDER BY id",
                            (machine["id"],)).fetchall()  # fmt: skip
        return jsonify(machine={"id": machine["id"], "name": machine["name"]}, deny_keys=deny_keys(),
                       sessions=[{"id": r["id"], "name": r["name"], "automation_id": r["automation_id"],
                                  "targets": json.loads(r["targets"])} for r in rows])  # fmt: skip

    @app.get("/api/agent/apps")
    @auth.agent_token
    def agent_apps():
        """Catalog applications with an identity, for a session started on the recorder."""
        agent_machine()
        rows = db().execute("""
            SELECT apps.id, apps.name, app_identities.key FROM apps
            JOIN app_identities ON app_identities.app_id = apps.id
            WHERE apps.status != 'not_replicated' ORDER BY apps.name
        """).fetchall()
        return jsonify(deny_keys=deny_keys(), apps=[
            {"app_id": r["id"], "name": r["name"], "target": target_of(r["key"], "full", r["id"], r["name"])}
            for r in rows])  # fmt: skip

    # -- references -------------------------------------------------------------------------------

    def app_for(item):
        """The catalog application of a recorded one: by identity key, by the target's catalog id,
        or by its executable name; else a new `discovered` one."""
        row = db().execute("SELECT app_id FROM app_identities WHERE key = ?", (item["key"],)).fetchone()
        if row is not None:
            return row["app_id"], False
        app_id = None
        if item.get("app_id") and db().execute("SELECT 1 FROM apps WHERE id = ?", (item["app_id"],)).fetchone():
            app_id = item["app_id"]
            if item["key"].startswith("web:"):
                # a target known by path does not claim the rest of its host
                origin = web_parts(item["key"])[0]
                if any(other == app_id for _, other, prefix in web_identities(origin) if prefix):
                    app_id = None
        if app_id is None and item.get("exe"):
            row = db().execute("SELECT app_id FROM app_identities WHERE key = ?",
                               ("exe:" + item["exe"].lower(),)).fetchone()  # fmt: skip
            app_id = row["app_id"] if row else None
        created = False
        if app_id is None:
            name = item.get("display") or item.get("product") or item["key"].split(":", 1)[-1]
            app_id = db().execute(
                "INSERT INTO apps(name, git_url, requirements, status, surface) VALUES (?, '', '', 'discovered', ?)",
                (name[:120], item["surface"]),
            ).lastrowid
            created = True
        facts = {k: item[k] for k in ("exe", "company", "product", "version", "toolkit") if k in item}
        db().execute("INSERT OR IGNORE INTO app_identities VALUES (?, ?, ?, ?)",
                     (item["key"], app_id, item["surface"], json.dumps(facts)))  # fmt: skip
        return app_id, created

    def assign_steps(item, steps):
        """The recorded application's steps per catalog application, as (app_id, identity key,
        steps, created). A web origin's steps go by the path of their screen to the identity with
        the longest matching path prefix; steps no identity claims go to `app_for(item)`."""
        groups, rest = {}, []
        if item["key"].startswith("web:"):
            origin = web_parts(item["key"])[0]
            for step in steps:
                url = normalise_key(step.get("screen") or "") if str(step.get("screen", "")).startswith("http") else None
                url = url if url and web_parts(url)[0] == origin else item["key"]
                found = resolve_web(url)
                if found is None:
                    rest.append(step)
                else:
                    groups.setdefault(found, []).append(step)
            if not steps:
                found = resolve_web(item["key"])
                if found is not None:
                    groups[found] = []
        else:
            rest = list(steps)
        out = [(app_id, key, group, False) for (key, app_id), group in groups.items()]
        if rest or not groups:
            app_id, created = app_for(item)
            out.append((app_id, item["key"], rest, created))
        return out

    def link_evidence(app_id, recording_id, numbers, seen):  # numbers: the event ids received
        """The application's evidence link to the recording. A first receipt (or one the owner
        has not reviewed yet) selects every step, as before. Once the owner has reviewed the
        selection it is never changed by a resend: events that are new in the resend are kept as
        suggestions, shown as new and not yet reviewed until the owner saves the assignment."""
        row = db().execute("SELECT * FROM evidence WHERE app_id = ? AND recording_id = ?",
                           (app_id, recording_id)).fetchone()  # fmt: skip
        if row is None:
            if numbers:
                db().execute("""INSERT INTO evidence(app_id, recording_id, selection, notes, reviewed, suggested)
                                VALUES (?, ?, ?, '', 0, '[]')""", (app_id, recording_id, json.dumps(sorted(numbers))))
        elif not row["reviewed"]:
            if numbers:
                db().execute("UPDATE evidence SET selection = ? WHERE app_id = ? AND recording_id = ?",
                             (json.dumps(sorted(numbers)), app_id, recording_id))  # fmt: skip
        else:
            selection = set(json.loads(row["selection"]))
            suggested = (set(json.loads(row["suggested"])) | (numbers - seen)) & numbers
            db().execute("UPDATE evidence SET suggested = ? WHERE app_id = ? AND recording_id = ?",
                         (json.dumps(sorted(suggested - selection)), app_id, recording_id))  # fmt: skip

    @app.post("/api/agent/references")
    @auth.agent_token
    def agent_references():
        machine = agent_machine()
        doc = request.get_json(silent=True)
        try:
            contract.validate(doc, "references")
        except contract.ContractError as error:
            return contract_refusal(error)
        try:
            refs = clean_references(doc)
        except (ValueError, TypeError) as error:
            return jsonify(error=f"invalid references: {error}"), 400
        rec_name, rec_version = recorder_of(doc)
        sender = {"contract_version": doc["contract_version"], "recorder": rec_name,
                  "recorder_version": rec_version,
                  "twinlab_commit": (doc.get("recorder") or {}).get("twinlab_commit")
                  or refs["session"].get("twinlab_commit")}  # fmt: skip
        s = refs["session"]
        recording_id = "ws-" + s["id"]
        capture = None
        if s.get("portal_session_id"):
            capture = db().execute("SELECT * FROM capture_sessions WHERE id = ? AND machine_id = ?",
                                   (s["portal_session_id"], machine["id"])).fetchone()  # fmt: skip
        automation_id = capture["automation_id"] if capture else None
        if automation_id is None and s.get("automation_id"):
            if db().execute("SELECT 1 FROM automations WHERE id = ?", (s["automation_id"],)).fetchone():
                automation_id = s["automation_id"]
        owner = db().execute("SELECT machine_id FROM session_refs WHERE recording_id = ?",
                             (recording_id,)).fetchone()  # fmt: skip
        if owner is not None and owner["machine_id"] != machine["id"]:
            return jsonify(error="this session belongs to another machine"), 409
        # an older recorder's steps get the id stored for them by an earlier send, else one
        # derived now (event_ids): every reference below is to event_id, never to n or seq
        known = event_ids.stored_ids(db(), recording_id)
        for steps in refs["flows"].values():
            event_ids.assign(steps, recording_id, known)
        names = {a["key"]: a.get("display") or a["key"] for a in refs["apps"]}
        events = []
        for key, steps in refs["flows"].items():
            for step in steps:
                event = {"event_id": step["event_id"], "seq": step.get("seq", step["n"]),
                         "source": names.get(key, key)[:150], "action": step.get("action", "other")}
                if step.get("captured_at"):
                    event["time"] = step["captured_at"]
                if step.get("channel"):
                    event["channel"] = step["channel"]
                events.append(event)
        events.sort(key=lambda e: (e["seq"], e.get("time") or "", e["event_id"]))
        mapped, discovered = [], []
        with db():
            db().execute("""INSERT INTO recordings VALUES (?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET name = excluded.name, events = excluded.events""",
                         (recording_id, s["name"], json.dumps(events)))  # fmt: skip
            db().execute("""INSERT INTO session_refs VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(recording_id) DO UPDATE SET payload = excluded.payload,
                            received_at = excluded.received_at, automation_id = excluded.automation_id""",
                         (recording_id, s["id"], machine["id"], capture["id"] if capture else None,
                          automation_id, json.dumps(refs), now()))  # fmt: skip
            seen = {}  # what earlier sends of this recording had, per application
            for row in db().execute("SELECT app_id, flow FROM recording_apps WHERE recording_id = ?", (recording_id,)):
                seen.setdefault(row["app_id"], set()).update(st["event_id"] for st in json.loads(row["flow"]))
            db().execute("DELETE FROM recording_apps WHERE recording_id = ?", (recording_id,))
            received = {}  # app_id -> event ids in this send
            for item in refs["apps"]:
                for app_id, key, steps, created in assign_steps(item, refs["flows"].get(item["key"], [])):
                    old = db().execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                                       (recording_id, app_id, key)).fetchone()  # fmt: skip
                    if old is not None:  # two references resolved to one identity
                        steps = json.loads(old["flow"]) + steps
                    db().execute("INSERT OR REPLACE INTO recording_apps VALUES (?, ?, ?, ?, ?)",
                                 (recording_id, app_id, key, item["level"], json.dumps(steps)))  # fmt: skip
                    received.setdefault(app_id, set()).update(st["event_id"] for st in steps)
                    if automation_id is not None:
                        db().execute("INSERT OR IGNORE INTO automation_apps VALUES (?, ?)", (automation_id, app_id))
                    if app_id not in mapped and app_id not in discovered:
                        (discovered if created else mapped).append(app_id)
            for app_id, numbers in received.items():
                link_evidence(app_id, recording_id, numbers, seen.get(app_id, set()))
            db().execute("INSERT OR IGNORE INTO recording_uploads VALUES (?, ?, ?)",
                         (recording_id, machine["paired_by"], now()))
            for app_id in discovered:
                if auth.valid_name(machine["paired_by"]) and not auth.owners(db(), app_id):
                    auth.assign(db(), app_id, machine["paired_by"], "owner", f"machine:{machine['id']}", now())
                    audit.record(db(), f"machine:{machine['id']}", "role.assign", "role",
                                 f"{app_id}/{machine['paired_by']}", app_id=app_id, user=machine["paired_by"],
                                 role="owner", previous=None, reason="paired the machine that discovered it")
            report = refs.get("completeness")
            old = db().execute("SELECT * FROM recording_completeness WHERE recording_id = ?", (recording_id,)).fetchone()
            text = json.dumps(report, sort_keys=True) if report else None
            # an owner's acceptance of a partial recording holds only for what they accepted
            accepted = (old["accepted_by"], old["accepted_at"]) if old is not None and old["completeness"] == text else (None, None)
            db().execute("""INSERT INTO recording_completeness VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(recording_id) DO UPDATE SET completeness = excluded.completeness,
                            preflight = excluded.preflight, accepted_by = excluded.accepted_by,
                            accepted_at = excluded.accepted_at, updated_at = excluded.updated_at""",
                         (recording_id, text, json.dumps(refs["preflight"], sort_keys=True) if refs.get("preflight") else None,
                          *accepted, now()))  # fmt: skip
            if capture is not None:
                db().execute("UPDATE capture_sessions SET status = 'recorded', recording_id = ? WHERE id = ?",
                             (recording_id, capture["id"]))  # fmt: skip
            db().execute("INSERT OR REPLACE INTO recording_contracts VALUES (?, ?, ?, ?, ?, ?)",
                         (recording_id, sender["contract_version"], sender["recorder"],
                          sender["recorder_version"], sender["twinlab_commit"], now()))  # fmt: skip
            db().execute("INSERT OR REPLACE INTO machine_contracts VALUES (?, ?, ?, ?)",
                         (machine["id"], sender["contract_version"],
                          f"{sender['recorder']} {sender['recorder_version']}", now()))  # fmt: skip
        return jsonify(recording_id=recording_id, mapped=mapped, discovered=discovered,
                       url=url_for("recording", recording_id=recording_id, _external=True))  # fmt: skip

    # -- what a recording and an application show ----------------------------------------------

    def recording_refs(recording_id):
        ref = db().execute("""
            SELECT session_refs.*, machines.name AS machine, automations.name AS automation
            FROM session_refs LEFT JOIN machines ON machines.id = session_refs.machine_id
            LEFT JOIN automations ON automations.id = session_refs.automation_id
            WHERE recording_id = ?
        """, (recording_id,)).fetchone()
        if ref is None:
            return None
        apps = db().execute("""
            SELECT recording_apps.*, apps.name, apps.status FROM recording_apps
            JOIN apps ON apps.id = recording_apps.app_id WHERE recording_id = ? ORDER BY apps.name
        """, (recording_id,)).fetchall()
        payload = json.loads(ref["payload"])
        def name_of(key):  # a web origin's steps may have gone to several applications by path
            if key == "presence":
                return "Other applications"
            found = [a["name"] for a in apps if a["key"] == key or a["key"].startswith(key + "/")]
            return ", ".join(dict.fromkeys(found)) or key

        timeline = [dict(run, name=name_of(run["app"])) for run in payload["timeline"]]
        sender = db().execute("SELECT * FROM recording_contracts WHERE recording_id = ?",
                              (recording_id,)).fetchone()  # fmt: skip
        status = recording_status(db(), recording_id)
        return {"ref": ref, "session": payload["session"], "timeline": timeline, "contract": sender,
                "status": status,
                "ranges": compl.ranges(status["completeness"], payload["session"].get("started"),
                                       payload["session"].get("ended")),
                "apps": [dict(a, steps=json.loads(a["flow"])) for a in apps]}  # fmt: skip

    def app_recordings(app_id):
        rows = db().execute("""
            SELECT recording_apps.*, recordings.name AS recording_name, session_refs.received_at,
                   machines.name AS machine
            FROM recording_apps JOIN recordings ON recordings.id = recording_apps.recording_id
            LEFT JOIN session_refs ON session_refs.recording_id = recording_apps.recording_id
            LEFT JOIN machines ON machines.id = session_refs.machine_id
            WHERE recording_apps.app_id = ? ORDER BY session_refs.received_at DESC
        """, (app_id,)).fetchall()
        return [dict(r, steps=json.loads(r["flow"])) for r in rows]

    def app_automations(app_id):
        return db().execute("""
            SELECT automations.* FROM automations JOIN automation_apps ON automation_apps.automation_id = automations.id
            WHERE automation_apps.app_id = ? ORDER BY automations.name
        """, (app_id,)).fetchall()

    app.jinja_env.globals.update(recording_refs=recording_refs, app_recordings=app_recordings,
                                 recording_status=lambda rid: recording_status(db(), rid),
                                 app_automations=app_automations, app_identities=identities)  # fmt: skip

    @app.get("/recordings")
    @auth.signed_in
    def recordings():
        rows = db().execute("""
            SELECT recordings.id, recordings.name, session_refs.received_at, machines.name AS machine,
                   automations.name AS automation,
                   (SELECT group_concat(apps.name || ' (' || recording_apps.level || ')', ', ')
                    FROM recording_apps JOIN apps ON apps.id = recording_apps.app_id
                    WHERE recording_apps.recording_id = recordings.id) AS apps
            FROM recordings LEFT JOIN session_refs ON session_refs.recording_id = recordings.id
            LEFT JOIN machines ON machines.id = session_refs.machine_id
            LEFT JOIN automations ON automations.id = session_refs.automation_id
            ORDER BY session_refs.received_at DESC, recordings.name
        """).fetchall()
        return render_template("recordings.html", recordings=[dict(r, status=recording_status(db(), r["id"]))
                                                              for r in rows if reviewer_of_recording(r["id"])])

    @app.post("/recordings/<recording_id>/completeness/accept")
    @auth.signed_in
    def accept_recording(recording_id):
        """The owner accepts a partial or failed recording as it is: it no longer holds up the
        journey of the applications it recorded. A resend with other completeness undoes it.
        Restricted evidence: its uploader and the owners of the applications it is linked to."""
        if not reviewer_of_recording(recording_id):
            abort(403, "Only the recording's uploader and the owners of its applications can accept it.")
        status = recording_status(db(), recording_id)
        if status["completeness"] is None:
            abort(404)
        with db():
            db().execute("UPDATE recording_completeness SET accepted_by = ?, accepted_at = ? WHERE recording_id = ?",
                         (current_user(), now(), recording_id))  # fmt: skip
            linked = [r[0] for r in db().execute(
                "SELECT app_id FROM evidence WHERE recording_id = ? UNION SELECT app_id FROM recording_apps "
                "WHERE recording_id = ?", (recording_id, recording_id))]
            for app_id in linked or [None]:
                record("recording.accept", "recording", recording_id, app_id=app_id,
                       overall=status.get("overall") if isinstance(status, dict) else None)
        return redirect(url_for("recording", recording_id=recording_id))

    @app.post("/apps/<int:app_id>/identity")
    @require_role("owner")
    def add_identity(app_id):
        if db().execute("SELECT 1 FROM apps WHERE id = ?", (app_id,)).fetchone() is None:
            abort(404)
        key = normalise_key(request.form.get("key"))
        if key is None:
            abort(400, "Enter an executable name (CaseDesk.exe), a web address, a mainframe host "
                       "(tn3270://host:port), or desktop:Company/Product.")
        other = db().execute("SELECT app_id FROM app_identities WHERE key = ?", (key,)).fetchone()
        if other is not None and other["app_id"] != app_id:
            abort(409, "Another application already has this identity: merge them instead.")
        if key.startswith("web:"):
            mode = db().execute("SELECT url_match FROM apps WHERE id = ?", (app_id,)).fetchone()[0]
            if web_clash(app_id, key, mode) is not None:
                abort(409, "Another application already matches this host and path: add a longer path, "
                           "or merge them if they are the same application.")
        surface = key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop"
        with db():
            db().execute("INSERT OR IGNORE INTO app_identities VALUES (?, ?, ?, '{}')", (key, app_id, surface))
            db().execute("UPDATE apps SET surface = ? WHERE id = ? AND surface = ''", (surface, app_id))
        return redirect(url_for("app_setup", app_id=app_id))

    @app.post("/apps/<int:app_id>/status")
    @require_role("owner")
    def set_status(app_id):
        status = request.form.get("status")
        name = request.form.get("name", "").strip()[:120]
        if status not in STATUSES:
            abort(400, "Unknown status.")
        with db():
            db().execute("UPDATE apps SET status = ? WHERE id = ?", (status, app_id))
            if name:
                db().execute("UPDATE apps SET name = ? WHERE id = ?", (name, app_id))
            record("app.status", "app", app_id, app_id=app_id, status=status, name=name or None)
        return redirect(url_for("demo", app_id=app_id))

    @app.post("/apps/<int:app_id>/url-match")
    @require_role("owner")
    def set_url_match(app_id):
        """The owner's override of how the application's web addresses match."""
        mode = request.form.get("url_match")
        if mode not in ("path", "host") or db().execute("SELECT 1 FROM apps WHERE id = ?", (app_id,)).fetchone() is None:
            abort(400, "Choose path or host.")
        for row in identities(app_id):
            if row["key"].startswith("web:") and web_clash(app_id, row["key"], mode) is not None:
                abort(409, "Another application already matches this whole host.")
        with db():
            db().execute("UPDATE apps SET url_match = ? WHERE id = ?", (mode, app_id))
        return redirect(url_for("app_setup", app_id=app_id))

    # -- merging two applications ------------------------------------------------------------------
    # Moved to the surviving application: identities, recorded flows, evidence links,
    # automations, scenarios and historical build briefs with their packages. An evidence link both
    # applications have to the same recording is combined (shown in the preview first); nothing
    # is dropped. Rows in any other table that refers to the application block the merge.

    # app_roles: the target's assignments stay; the source's are dropped (merging never widens
    # who may see the target). Merging needs the owner role on both applications.
    MOVED = {"app_identities", "recording_apps", "evidence", "automation_apps", "briefs", "scenarios", "app_roles"}

    def combine_links(keep, gone, gone_name):
        """One evidence link from two links to the same recording. A reviewed selection is never
        widened by an unreviewed one: those events become suggestions for the owner instead."""
        def sel(row):
            return set(json.loads(row["selection"]))

        def sug(row):
            return set(json.loads(row["suggested"]))

        if keep["reviewed"] and gone["reviewed"] or not keep["reviewed"] and not gone["reviewed"]:
            selection = sel(keep) | sel(gone)
            suggested = sug(keep) | sug(gone)
        else:
            reviewed, other = (keep, gone) if keep["reviewed"] else (gone, keep)
            selection = sel(reviewed)
            suggested = sug(keep) | sug(gone) | sel(other)
        notes = [n for n in (keep["notes"].strip(), gone["notes"].strip()) if n]
        if len(notes) == 2 and notes[0] != notes[1]:
            notes = [notes[0], f"Merged from {gone_name}: {notes[1]}"]
        return {"selection": sorted(selection), "suggested": sorted(suggested - selection),
                "notes": "\n\n".join(dict.fromkeys(notes)),
                "reviewed": 1 if keep["reviewed"] or gone["reviewed"] else 0}

    def merge_plan(app_id, into):
        source = db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        target = db().execute("SELECT * FROM apps WHERE id = ?", (into,)).fetchone()
        if source is None:
            abort(404)
        if target is None or into == app_id:
            abort(400, "Choose the application to merge into.")
        blockers = []
        for (table,) in db().execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            if table in MOVED:
                continue
            for fk in db().execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                if fk["table"] == "apps":
                    count = db().execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{fk["from"]}" = ?',
                                         (app_id,)).fetchone()[0]  # fmt: skip
                    if count:
                        blockers.append(f"{count} {table.replace('_', ' ')}")
        target_links = {r["recording_id"]: r for r in db().execute(
            "SELECT * FROM evidence WHERE app_id = ?", (into,))}  # fmt: skip
        moving, combined = [], []
        for row in db().execute("""SELECT evidence.*, recordings.name FROM evidence
                                   JOIN recordings ON recordings.id = evidence.recording_id
                                   WHERE app_id = ? ORDER BY recordings.name""", (app_id,)).fetchall():  # fmt: skip
            keep = target_links.get(row["recording_id"])
            if keep is None:
                moving.append({"recording_id": row["recording_id"], "name": row["name"],
                               "selected": len(json.loads(row["selection"]))})  # fmt: skip
            else:
                order = {e["event_id"]: e["seq"] for e in json.loads(db().execute(
                    "SELECT events FROM recordings WHERE id = ?", (row["recording_id"],)).fetchone()[0])}
                link = combine_links(keep, row, source["name"])
                combined.append({"recording_id": row["recording_id"], "name": row["name"],
                                 # what the page shows: the events' display numbers, in order
                                 "shown": sorted(order.get(i, 0) for i in link["selection"]),
                                 "shown_suggested": sorted(order.get(i, 0) for i in link["suggested"]),
                                 "source": json.loads(row["selection"]), "target": json.loads(keep["selection"]),
                                 "source_notes": row["notes"], "target_notes": keep["notes"],
                                 **link})  # fmt: skip
        flows = db().execute("""SELECT recording_apps.recording_id, recording_apps.key, recordings.name,
                                       EXISTS (SELECT 1 FROM recording_apps t WHERE t.app_id = ?
                                               AND t.recording_id = recording_apps.recording_id
                                               AND t.key = recording_apps.key) AS shared
                                FROM recording_apps JOIN recordings ON recordings.id = recording_apps.recording_id
                                WHERE app_id = ? ORDER BY recordings.name""", (into, app_id)).fetchall()  # fmt: skip
        plan = {
            "source": {"id": source["id"], "name": source["name"], "url_match": source["url_match"]},
            "target": {"id": target["id"], "name": target["name"], "url_match": target["url_match"]},
            "identities": [r["key"] for r in identities(app_id)],
            "flows": [{"recording_id": r["recording_id"], "name": r["name"], "key": r["key"],
                       "shared": bool(r["shared"])} for r in flows],  # fmt: skip
            "links": moving, "combined": combined,
            "automations": [r["name"] for r in app_automations(app_id)],
            "briefs": [{"revision": r["revision"], "package": bool(r["package"])} for r in db().execute(
                """SELECT revision, EXISTS (SELECT 1 FROM brief_packages p WHERE p.revision = briefs.revision)
                   AS package FROM briefs WHERE app_id = ? ORDER BY revision""", (app_id,))],  # fmt: skip
            "scenarios": [r["name"] for r in db().execute(
                "SELECT name FROM scenarios WHERE app_id = ? ORDER BY position, id", (app_id,))],
            "blockers": blockers,
        }
        plan["fingerprint"] = sha(json.dumps(plan, sort_keys=True))[:16]
        return plan

    @app.get("/apps/<int:app_id>/merge")
    @require_role("owner", resolve=lambda kw: [kw["app_id"], request.args.get("into", type=int)])
    def merge_preview(app_id):
        plan = merge_plan(app_id, request.args.get("into", type=int))
        return render_template("merge.html", plan=plan)

    @app.post("/apps/<int:app_id>/merge")
    @require_role("owner", resolve=lambda kw: [kw["app_id"], request.form.get("into", type=int)])
    def merge_app(app_id):
        into = request.form.get("into", type=int)
        plan = merge_plan(app_id, into)
        if request.form.get("confirm") != plan["fingerprint"]:
            # not confirmed, or the applications changed since the preview: show it (again)
            return redirect(url_for("merge_preview", app_id=app_id, into=into))
        if plan["blockers"]:
            abort(400, "This application cannot be merged: " + ", ".join(plan["blockers"]) + ".")
        with db():
            db().execute("UPDATE app_identities SET app_id = ? WHERE app_id = ?", (into, app_id))
            for row in db().execute("SELECT * FROM recording_apps WHERE app_id = ?", (app_id,)).fetchall():
                keep = db().execute("SELECT flow FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                                    (row["recording_id"], into, row["key"])).fetchone()  # fmt: skip
                if keep is None:
                    db().execute("UPDATE recording_apps SET app_id = ? WHERE recording_id = ? AND app_id = ? AND key = ?",
                                 (into, row["recording_id"], app_id, row["key"]))  # fmt: skip
                    continue
                steps = {st["event_id"]: st for st in json.loads(row["flow"])}
                steps.update({st["event_id"]: st for st in json.loads(keep["flow"])})
                merged = sorted(steps.values(), key=lambda st: (st.get("seq", st["n"]), st["event_id"]))
                db().execute("UPDATE recording_apps SET flow = ? WHERE recording_id = ? AND app_id = ? AND key = ?",
                             (json.dumps(merged), row["recording_id"], into, row["key"]))
                db().execute("DELETE FROM recording_apps WHERE recording_id = ? AND app_id = ? AND key = ?",
                             (row["recording_id"], app_id, row["key"]))  # fmt: skip
            for link in plan["combined"]:
                db().execute("""UPDATE evidence SET selection = ?, suggested = ?, notes = ?, reviewed = ?
                                WHERE app_id = ? AND recording_id = ?""",
                             (json.dumps(link["selection"]), json.dumps(link["suggested"]), link["notes"],
                              link["reviewed"], into, link["recording_id"]))  # fmt: skip
                db().execute("DELETE FROM evidence WHERE app_id = ? AND recording_id = ?", (app_id, link["recording_id"]))
            db().execute("UPDATE evidence SET app_id = ? WHERE app_id = ?", (into, app_id))
            db().execute("INSERT OR IGNORE INTO automation_apps SELECT automation_id, ? FROM automation_apps "
                         "WHERE app_id = ?", (into, app_id))  # fmt: skip
            db().execute("DELETE FROM automation_apps WHERE app_id = ?", (app_id,))
            db().execute("UPDATE briefs SET app_id = ? WHERE app_id = ?", (into, app_id))
            # scenarios follow the target's own, in their order
            db().execute("""UPDATE scenarios SET app_id = ?, position = position +
                            (SELECT COALESCE(MAX(position), 0) FROM scenarios WHERE app_id = ?) WHERE app_id = ?""",
                         (into, into, app_id))  # fmt: skip
            db().execute("DELETE FROM app_roles WHERE app_id = ?", (app_id,))
            details = {k: plan[k] for k in ("identities", "links", "combined", "automations", "briefs", "scenarios")}
            for logged in (into, app_id):
                record("app.merge", "app", app_id, app_id=logged, source=plan["source"], target=plan["target"],
                       fingerprint=plan["fingerprint"], **details)
            db().execute("DELETE FROM apps WHERE id = ?", (app_id,))
        return redirect(url_for("demo", app_id=into))

    # -- automations ------------------------------------------------------------------------------

    @app.get("/automations")
    @auth.signed_in
    def automations():
        rows = db().execute("""
            SELECT automations.*, (SELECT group_concat(apps.name, ', ') FROM automation_apps
                JOIN apps ON apps.id = automation_apps.app_id WHERE automation_id = automations.id) AS apps
            FROM automations ORDER BY name
        """).fetchall()
        return render_template("automations.html", automations=rows,
                               apps=[a for a in db().execute("SELECT * FROM apps ORDER BY name").fetchall()
                                     if auth.can("owner", app_id=a["id"])])  # fmt: skip

    def save_automation(name, kind, notes, app_ids):
        with db():
            row = db().execute("SELECT id FROM automations WHERE name = ?", (name,)).fetchone()
            if row is None:
                automation_id = db().execute("INSERT INTO automations(name, kind, notes) VALUES (?, ?, ?)",
                                             (name, kind, notes)).lastrowid  # fmt: skip
            else:
                automation_id = row["id"]
                db().execute("UPDATE automations SET kind = ?, notes = ? WHERE id = ?", (kind, notes, automation_id))
            for app_id in app_ids:
                db().execute("INSERT OR IGNORE INTO automation_apps VALUES (?, ?)", (automation_id, app_id))
        return automation_id

    @app.post("/automations")
    @auth.signed_in
    def add_automation():
        name = request.form.get("name", "").strip()[:120]
        if not name:
            abort(400, "Name the automation.")
        ids = []
        for value in request.form.getlist("app"):
            try:
                ids.append(int(value))
            except ValueError:
                abort(400, "Invalid application.")
        known = {r["id"] for r in db().execute("SELECT id FROM apps")}
        if not set(ids) <= known:
            abort(400, "Unknown application.")
        owned(ids)
        automation_id = save_automation(name, request.form.get("kind", "").strip()[:40],
                                        request.form.get("notes", "").strip()[:2000], ids)  # fmt: skip
        return redirect(url_for("automation", automation_id=automation_id))

    @app.post("/automations/import")
    @auth.signed_in
    def import_automation():
        """A Twin Lab automation.yaml: its name, type, and the applications it `touches`, matched
        to catalog applications by name (case-insensitive). Unmatched names are listed in notes."""
        upload = request.files.get("file")
        if upload is None:
            abort(400, "Choose an automation.yaml.")
        try:
            doc = yaml.safe_load(upload.read(256 * 1024).decode("utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            abort(400, "Not a YAML file.")
        if not isinstance(doc, dict) or not isinstance(doc.get("id") or doc.get("name"), str):
            abort(400, "The file has no id or name.")
        names = [str(t.get("app")) for t in doc.get("touches") or [] if isinstance(t, dict) and t.get("app")]
        # only applications the user owns are linked: an import never reaches other applications
        by_name = {r["name"].lower(): r["id"] for r in db().execute("SELECT id, name FROM apps")
                   if auth.can("owner", app_id=r["id"])}
        ids = [by_name[n.lower()] for n in names if n.lower() in by_name]
        missing = [n for n in names if n.lower() not in by_name]
        notes = "Imported from automation.yaml." + (f" Applications not in the catalog: {', '.join(missing)}." if missing else "")
        automation_id = save_automation(str(doc.get("name") or doc.get("id"))[:120], str(doc.get("type") or "")[:40],
                                        notes, ids)  # fmt: skip
        return redirect(url_for("automation", automation_id=automation_id))

    @app.get("/automations/<int:automation_id>")
    @auth.signed_in
    def automation(automation_id):
        item = db().execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
        if item is None:
            abort(404)
        apps = db().execute("""SELECT apps.* FROM apps JOIN automation_apps ON automation_apps.app_id = apps.id
                               WHERE automation_id = ? ORDER BY name""", (automation_id,)).fetchall()  # fmt: skip
        recs = db().execute("""SELECT recordings.id, recordings.name, session_refs.received_at FROM session_refs
                               JOIN recordings ON recordings.id = session_refs.recording_id
                               WHERE session_refs.automation_id = ? ORDER BY received_at DESC""",
                            (automation_id,)).fetchall()  # fmt: skip
        apps = [a for a in apps if auth.can("viewer", app_id=a["id"])]
        recs = [r for r in recs if reviewer_of_recording(r["id"])]
        return render_template("automation.html", item=item, apps=apps, recordings=recs)

    return app
