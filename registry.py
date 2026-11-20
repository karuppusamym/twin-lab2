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
"""

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O or 1/I
CODE_LENGTH = 10
CODE_MINUTES = 10
FAILURES_PER_10_MIN = 20
STATUSES = ("known", "discovered", "not_replicated")
LEVELS = ("full", "steps")
KEY = re.compile(r"^(desktop:[^\x00-\x1f]{1,200}|exe:[\w .()+-]{1,80}\.exe|web:https?://[\w.\-\[\]:]{1,200}"
                 r"|mainframe:[\w.\-\[\]:]{1,200}:\d{1,5})$",
                 re.I)  # fmt: skip
SESSION_KEY = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
FLOW_FIELDS = {"n": int, "action": str, "control": str, "label": str, "screen": str, "key": str}


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
    db.commit()


def normalise_key(text):
    """An identity key from what a person types: `desktop:Company/Product`, an executable
    name (`exe:CaseDesk.exe`), or a web address (`web:https://host[:port]`)."""
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
        parts = urlsplit(text[4:])
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            return None
        port = parts.port
        default = {"http": 80, "https": 443}[parts.scheme]
        host = parts.hostname.lower()
        text = f"web:{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")
    if text.startswith("exe:"):
        text = "exe:" + text[4:].lower()
    return text if KEY.match(text) else None


def target_of(key, level, app_id, name):
    target = {"level": level, "app_id": app_id, "name": name}
    if key.startswith("web:"):
        target["origin"] = key[4:]
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
    return {"format": "twinlab-references/1", "session": out_session, "apps": apps,
            "flows": flows, "timeline": timeline}  # fmt: skip


def register(app, db):  # noqa: C901 - one place for the routes of this module
    """Add the routes to the Flask app; `db()` returns the request's SQLite connection."""

    # -- sign-in and the agent's bearer token -----------------------------------------------

    def current_user():
        return g.get("user")

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
    def machines():
        rows = db().execute("SELECT * FROM machines ORDER BY revoked_at IS NOT NULL, name").fetchall()
        return render_template("machines.html", machines=rows, code=session.pop("pair_code", None),
                               minutes=CODE_MINUTES)  # fmt: skip

    @app.post("/machines/pair")
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
    def revoke(machine_id):
        with db():
            db().execute("UPDATE machines SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (now(), machine_id))  # fmt: skip
        return redirect(url_for("machines"))

    @app.post("/api/agent/pair")
    def agent_pair():
        body = request.get_json(silent=True) or {}
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
        return jsonify(token=token, machine_id=machine, name=name)

    # -- sessions offered to a machine ---------------------------------------------------------

    def identities(app_id):
        return db().execute("SELECT * FROM app_identities WHERE app_id = ? ORDER BY key", (app_id,)).fetchall()

    def deny_keys():
        return [r["key"] for r in db().execute("""
            SELECT key FROM app_identities JOIN apps ON apps.id = app_identities.app_id
            WHERE apps.status = 'not_replicated' AND key NOT LIKE 'exe:%'
        """)]  # fmt: skip

    @app.get("/sessions")
    def sessions():
        rows = db().execute("""
            SELECT capture_sessions.*, machines.name AS machine, automations.name AS automation
            FROM capture_sessions JOIN machines ON machines.id = capture_sessions.machine_id
            LEFT JOIN automations ON automations.id = capture_sessions.automation_id
            ORDER BY capture_sessions.id DESC
        """).fetchall()
        apps = db().execute("""
            SELECT apps.*, (SELECT COUNT(*) FROM app_identities WHERE app_id = apps.id) AS ids
            FROM apps WHERE status != 'not_replicated' ORDER BY name
        """).fetchall()
        return render_template(
            "sessions.html", sessions=rows, apps=apps, loads=json.loads,
            machines=db().execute("SELECT * FROM machines WHERE revoked_at IS NULL ORDER BY name").fetchall(),
            automations=db().execute("SELECT * FROM automations ORDER BY name").fetchall(),
        )

    @app.post("/sessions")
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
    def withdraw_session(session_id):
        with db():
            db().execute("UPDATE capture_sessions SET status = 'withdrawn' WHERE id = ? AND status = 'offered'",
                         (session_id,))  # fmt: skip
        return redirect(url_for("sessions"))

    @app.get("/api/agent/sessions")
    def agent_sessions():
        machine = agent_machine()
        rows = db().execute("SELECT * FROM capture_sessions WHERE machine_id = ? AND status = 'offered' ORDER BY id",
                            (machine["id"],)).fetchall()  # fmt: skip
        return jsonify(machine={"id": machine["id"], "name": machine["name"]}, deny_keys=deny_keys(),
                       sessions=[{"id": r["id"], "name": r["name"], "automation_id": r["automation_id"],
                                  "targets": json.loads(r["targets"])} for r in rows])  # fmt: skip

    @app.get("/api/agent/apps")
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

    @app.post("/api/agent/references")
    def agent_references():
        machine = agent_machine()
        try:
            refs = clean_references(request.get_json(silent=True))
        except (ValueError, TypeError) as error:
            return jsonify(error=f"invalid references: {error}"), 400
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
        names = {a["key"]: a.get("display") or a["key"] for a in refs["apps"]}
        events = []
        for key, steps in refs["flows"].items():
            for step in steps:
                events.append({"seq": step["n"], "source": names.get(key, key)[:150],
                               "action": step.get("action", "other")})  # fmt: skip
        events.sort(key=lambda e: e["seq"])
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
            db().execute("DELETE FROM recording_apps WHERE recording_id = ?", (recording_id,))
            for item in refs["apps"]:
                app_id, created = app_for(item)
                steps = refs["flows"].get(item["key"], [])
                db().execute("INSERT OR REPLACE INTO recording_apps VALUES (?, ?, ?, ?, ?)",
                             (recording_id, app_id, item["key"], item["level"], json.dumps(steps)))  # fmt: skip
                selection = sorted({st["n"] for st in steps})
                if selection:
                    db().execute("""INSERT INTO evidence VALUES (?, ?, ?, '')
                                    ON CONFLICT(app_id, recording_id) DO UPDATE SET selection = excluded.selection""",
                                 (app_id, recording_id, json.dumps(selection)))  # fmt: skip
                if automation_id is not None:
                    db().execute("INSERT OR IGNORE INTO automation_apps VALUES (?, ?)", (automation_id, app_id))
                (discovered if created else mapped).append(app_id)
            if capture is not None:
                db().execute("UPDATE capture_sessions SET status = 'recorded', recording_id = ? WHERE id = ?",
                             (recording_id, capture["id"]))  # fmt: skip
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
        names = {a["key"]: a["name"] for a in apps}
        timeline = [dict(run, name=names.get(run["app"], "Other applications" if run["app"] == "presence"
                                             else run["app"])) for run in payload["timeline"]]  # fmt: skip
        return {"ref": ref, "session": payload["session"], "apps": apps, "timeline": timeline,
                "flows": {a["app_id"]: json.loads(a["flow"]) for a in apps}}  # fmt: skip

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
                                 app_automations=app_automations, app_identities=identities)  # fmt: skip

    @app.get("/recordings")
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
        return render_template("recordings.html", recordings=rows)

    @app.post("/apps/<int:app_id>/identity")
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
        surface = key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop"
        with db():
            db().execute("INSERT OR IGNORE INTO app_identities VALUES (?, ?, ?, '{}')", (key, app_id, surface))
            db().execute("UPDATE apps SET surface = ? WHERE id = ? AND surface = ''", (surface, app_id))
        return redirect(url_for("app_setup", app_id=app_id))

    @app.post("/apps/<int:app_id>/status")
    def set_status(app_id):
        status = request.form.get("status")
        name = request.form.get("name", "").strip()[:120]
        if status not in STATUSES:
            abort(400, "Unknown status.")
        with db():
            db().execute("UPDATE apps SET status = ? WHERE id = ?", (status, app_id))
            if name:
                db().execute("UPDATE apps SET name = ? WHERE id = ?", (name, app_id))
        return redirect(url_for("demo", app_id=app_id))

    @app.post("/apps/<int:app_id>/merge")
    def merge_app(app_id):
        into = request.form.get("into", type=int)
        if into is None or into == app_id or db().execute("SELECT 1 FROM apps WHERE id = ?", (into,)).fetchone() is None:
            abort(400, "Choose the application to merge into.")
        if db().execute("SELECT 1 FROM releases WHERE app_id = ?", (app_id,)).fetchone() or \
                db().execute("SELECT 1 FROM attachments WHERE app_id = ?", (app_id,)).fetchone():
            abort(400, "Only an application without releases or attachments can be merged.")
        with db():
            db().execute("UPDATE app_identities SET app_id = ? WHERE app_id = ?", (into, app_id))
            db().execute("UPDATE OR IGNORE recording_apps SET app_id = ? WHERE app_id = ?", (into, app_id))
            db().execute("DELETE FROM recording_apps WHERE app_id = ?", (app_id,))
            db().execute("UPDATE OR IGNORE evidence SET app_id = ? WHERE app_id = ?", (into, app_id))
            db().execute("DELETE FROM evidence WHERE app_id = ?", (app_id,))
            db().execute("UPDATE OR IGNORE automation_apps SET app_id = ? WHERE app_id = ?", (into, app_id))
            db().execute("DELETE FROM automation_apps WHERE app_id = ?", (app_id,))
            db().execute("DELETE FROM briefs WHERE app_id = ? AND revision NOT IN (SELECT revision FROM releases)", (app_id,))
            db().execute("DELETE FROM apps WHERE id = ?", (app_id,))
        return redirect(url_for("demo", app_id=into))

    # -- automations ------------------------------------------------------------------------------

    @app.get("/automations")
    def automations():
        rows = db().execute("""
            SELECT automations.*, (SELECT group_concat(apps.name, ', ') FROM automation_apps
                JOIN apps ON apps.id = automation_apps.app_id WHERE automation_id = automations.id) AS apps
            FROM automations ORDER BY name
        """).fetchall()
        return render_template("automations.html", automations=rows,
                               apps=db().execute("SELECT * FROM apps ORDER BY name").fetchall())  # fmt: skip

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
        automation_id = save_automation(name, request.form.get("kind", "").strip()[:40],
                                        request.form.get("notes", "").strip()[:2000], ids)  # fmt: skip
        return redirect(url_for("automation", automation_id=automation_id))

    @app.post("/automations/import")
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
        by_name = {r["name"].lower(): r["id"] for r in db().execute("SELECT id, name FROM apps")}
        ids = [by_name[n.lower()] for n in names if n.lower() in by_name]
        missing = [n for n in names if n.lower() not in by_name]
        notes = "Imported from automation.yaml." + (f" Applications not in the catalog: {', '.join(missing)}." if missing else "")
        automation_id = save_automation(str(doc.get("name") or doc.get("id"))[:120], str(doc.get("type") or "")[:40],
                                        notes, ids)  # fmt: skip
        return redirect(url_for("automation", automation_id=automation_id))

    @app.get("/automations/<int:automation_id>")
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
        return render_template("automation.html", item=item, apps=apps, recordings=recs)

    return app
