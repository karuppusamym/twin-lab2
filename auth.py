"""Who is signed in, and what they may do: server-enforced roles per application.

Roles
-----
Per application a user has at most one role:

- `viewer`  — read-only access to the application's non-restricted pages (name, owners,
  purpose, stage, releases);
- `builder` — a viewer who may also open and download the application's *builder-approved
  packages* (frozen revisions) and start builds;
- `owner`   — a builder who may also review restricted evidence (recordings, attachments),
  write scenarios, freeze packages, record test results, publish releases, change setup and
  assign roles.

The roles are ranked (viewer < builder < owner): `require_role("builder")` admits builders and
owners. A **portal admin** (`users.is_admin`, or a name in `TWINLAB2_ADMINS`, or the machine
user `local` when the portal serves only this machine) manages users and role assignments of
every application and may read every application's non-restricted pages. An admin is *not* an
owner: to review an application's restricted evidence an admin assigns themself the owner role,
which is written to the audit history like every other role change.

Every route needs a policy. `require_role(...)` (or `public`, `signed_in`, `agent_token`) marks
the view; `install` refuses (403) any request to an endpoint without a mark, so a route added
later without a decision is closed rather than open.

Sign-in
-------
`identify()` decides who the request is, in this order:

1. `TWINLAB2_TRUST_PROXY_USER=1`: the user the reverse proxy (OpenShift OAuth proxy, or any SSO
   gateway) puts in `X-Forwarded-User` / `X-Forwarded-Preferred-Username`. This is the hook for a
   real SSO: put the portal behind the gateway, set the variable, and name admins in
   `TWINLAB2_ADMINS` or with `python portal.py user NAME --admin`. Groups are read from
   `X-Forwarded-Groups` (comma separated); members of `TWINLAB2_ADMIN_GROUP` are admins. To
   take identity from somewhere else (a signed JWT, mTLS), replace `proxy_identity`.
2. A session made by `/login`: a user of the local user store (`users`, password hashed with
   PBKDF2-SHA256) signs in with their own password; with `TWINLAB2_PASSWORD` set, any other
   name signs in with that shared portal password, as before.
3. Otherwise only this machine: the user `local`, a portal admin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import hashlib
import hmac
import os
import secrets

from flask import abort, current_app, g, redirect, request, session, url_for

ROLES = ("viewer", "builder", "owner")
RANK = {role: rank for rank, role in enumerate(ROLES, 1)}
ADMIN = "admin"
LOCAL_USER = "local"
LOCAL = {"127.0.0.1", "::1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY, password_hash TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS app_roles (
    app_id INTEGER NOT NULL REFERENCES apps(id), user_name TEXT NOT NULL, role TEXT NOT NULL,
    granted_by TEXT NOT NULL, granted_at TEXT NOT NULL,
    PRIMARY KEY (app_id, user_name)
);
"""

ITERATIONS = 240_000


# -- the local user store ---------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        found = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(found.hex(), digest)


def valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 120 and not any(ord(c) < 32 for c in name)


def save_user(db, name, *, password=None, is_admin=None, by="cli", now=""):
    """Create or update a local user. `password` None keeps the stored one."""
    if not valid_name(name):
        raise ValueError("Give a user name of 1 to 120 printable characters.")
    row = db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    if row is None:
        db.execute("INSERT INTO users(name, password_hash, is_admin, created_by, created_at) VALUES (?,?,?,?,?)",
                   (name, hash_password(password) if password else "", 1 if is_admin else 0, by, now))
    else:
        if password is not None:
            db.execute("UPDATE users SET password_hash = ? WHERE name = ?",
                       (hash_password(password) if password else "", name))
        if is_admin is not None:
            db.execute("UPDATE users SET is_admin = ? WHERE name = ?", (1 if is_admin else 0, name))


def has_local_users(db) -> bool:
    return db.execute("SELECT 1 FROM users WHERE password_hash != '' LIMIT 1").fetchone() is not None


# -- roles --------------------------------------------------------------------------------------


def role_of(db, app_id, user):
    row = db.execute("SELECT role FROM app_roles WHERE app_id = ? AND user_name = ?", (app_id, user)).fetchone()
    return row[0] if row else None


def roles_of(db, app_id):
    return db.execute("SELECT * FROM app_roles WHERE app_id = ? ORDER BY role = 'owner' DESC, user_name",
                      (app_id,)).fetchall()


def owners(db, app_id):
    return [row[0] for row in db.execute(
        "SELECT user_name FROM app_roles WHERE app_id = ? AND role = 'owner' ORDER BY user_name", (app_id,))]


def sync_owner_column(db, app_id):
    """`apps.owner` (shown on cards and read by the journey) is the list of assigned owners."""
    db.execute("UPDATE apps SET owner = ? WHERE id = ?", (", ".join(owners(db, app_id)), app_id))


def assign(db, app_id, user, role, by, now):
    """Give `user` `role` on the application (replacing any other role); returns the old role."""
    if role not in ROLES:
        raise ValueError("Choose owner, builder or viewer.")
    if not valid_name(user):
        raise ValueError("Give the user's sign-in name.")
    old = role_of(db, app_id, user)
    if old == role:
        return old
    if old == "owner" and len(owners(db, app_id)) == 1:
        raise ValueError("An application keeps at least one owner: assign another owner first.")
    db.execute("DELETE FROM app_roles WHERE app_id = ? AND user_name = ?", (app_id, user))
    db.execute("INSERT INTO app_roles VALUES (?, ?, ?, ?, ?)", (app_id, user, role, by, now))
    sync_owner_column(db, app_id)
    return old


def unassign(db, app_id, user):
    old = role_of(db, app_id, user)
    if old is None:
        return None
    if old == "owner" and len(owners(db, app_id)) == 1:
        raise ValueError("An application keeps at least one owner: assign another owner first.")
    db.execute("DELETE FROM app_roles WHERE app_id = ? AND user_name = ?", (app_id, user))
    sync_owner_column(db, app_id)
    return old


def visible_app_ids(db, user, admin):
    if admin:
        return None  # every application
    return {row[0] for row in db.execute("SELECT app_id FROM app_roles WHERE user_name = ?", (user,))}


# -- identity -----------------------------------------------------------------------------------


@dataclass
class Settings:
    trust_proxy: bool = False
    password: str = ""
    admins: set = field(default_factory=set)
    admin_group: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            trust_proxy=os.environ.get("TWINLAB2_TRUST_PROXY_USER") == "1",
            password=os.environ.get("TWINLAB2_PASSWORD", ""),
            admins={n.strip() for n in os.environ.get("TWINLAB2_ADMINS", "").split(",") if n.strip()},
            admin_group=os.environ.get("TWINLAB2_ADMIN_GROUP", "").strip(),
        )


def proxy_identity(req):
    """(user, groups) from a trusted SSO gateway's headers, or (None, set())."""
    user = req.headers.get("X-Forwarded-User") or req.headers.get("X-Forwarded-Preferred-Username")
    groups = {x.strip() for x in req.headers.get("X-Forwarded-Groups", "").split(",") if x.strip()}
    return (user[:120] if user else None), groups


def _settings() -> Settings:
    return current_app.extensions["twinlab_auth"]["settings"]


def _db():
    return current_app.extensions["twinlab_auth"]["db"]()


def identify():
    """Set g.user and g.is_admin for the request, or return the response that signs in."""
    settings, db = _settings(), _db()
    g.is_admin = False
    if settings.trust_proxy:
        user, groups = proxy_identity(request)
        if not user:
            abort(401, "Sign in through the SSO / OAuth proxy.")
        g.user = user
        g.is_admin = bool(settings.admin_group and settings.admin_group in groups)
    elif session.get("user"):
        g.user = session["user"]
    elif settings.password or request.remote_addr not in LOCAL:
        if settings.password or has_local_users(db):
            return redirect(url_for("login", next=request.path))
        abort(403, "This portal serves only this machine: set TWINLAB2_PASSWORD, add local users, "
                   "or run it behind the SSO proxy.")
    else:
        g.user = LOCAL_USER
        g.is_admin = True  # the machine's own user, when the portal serves only this machine
    if g.user in settings.admins:
        g.is_admin = True
    row = db.execute("SELECT is_admin FROM users WHERE name = ?", (g.user,)).fetchone()
    if row is not None and row[0]:
        g.is_admin = True
    return None


def login_user(name, password):
    """True when `name` may sign in with `password`: a local user with their own password, or
    (with TWINLAB2_PASSWORD) any other name with the shared password."""
    settings, db = _settings(), _db()
    row = db.execute("SELECT password_hash FROM users WHERE name = ?", (name,)).fetchone()
    if row is not None and row[0]:
        return check_password(row[0], password)
    return bool(settings.password) and secrets.compare_digest(password, settings.password)


def login_available():
    settings = _settings()
    return not settings.trust_proxy and (bool(settings.password) or has_local_users(_db()))


# -- policies -----------------------------------------------------------------------------------


def app_from_args(kwargs):
    """The default resolver: the route's `app_id`."""
    return kwargs.get("app_id")


def allowed(roles, app_id, user=None, admin=None):
    """Whether the current user holds one of `roles` (ranked) on the application, or is an
    admin where `admin` is among the roles. Admins also read every application (`viewer`)."""
    user = g.user if user is None else user
    admin = g.get("is_admin", False) if admin is None else admin
    if admin and (ADMIN in roles or "viewer" in roles):
        return True
    app_roles = [r for r in roles if r in RANK]
    if not app_roles or app_id is None:
        return False
    held = role_of(_db(), app_id, user)
    return held is not None and RANK[held] >= min(RANK[r] for r in app_roles)


def ensure(*roles, app_id=None):
    """Abort 403 unless the current user holds one of `roles` on `app_id`."""
    if not allowed(roles, app_id):
        names = " or ".join(r if r != ADMIN else "portal admin" for r in roles)
        abort(403, f"This needs the {names} role" + (" on this application." if app_id is not None else "."))


def can(*roles, app_id=None):
    return allowed(roles, app_id)


def require_role(*roles, resolve=app_from_args):
    """Mark and guard a view: the user needs one of `roles` (owner, builder, viewer — ranked —
    on the application `resolve(view kwargs)` returns, or `admin`). `resolve` may return one
    application id, a list of them (all needed), or None (then only `admin` can pass)."""
    for role in roles:
        if role not in RANK and role != ADMIN:
            raise ValueError(f"unknown role {role!r}")

    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            target = resolve(kwargs) if resolve else None
            targets = target if isinstance(target, (list, tuple, set)) else [target]
            for app_id in targets:
                ensure(*roles, app_id=app_id)
            return view(*args, **kwargs)

        guarded.twinlab_policy = ("roles", roles)
        return guarded

    return decorate


def _mark(kind):
    def decorate(view):
        view.twinlab_policy = (kind, ())
        return view
    return decorate


public = _mark("public")  # no sign-in (login page, health check)
signed_in = _mark("signed_in")  # any signed-in user; the view filters what it shows
agent_token = _mark("agent")  # the recorder's bearer token (registry.agent_machine)


def policy_of(view):
    return getattr(view, "twinlab_policy", None)


def install(app, db):
    """Sign every request in and refuse endpoints without a policy."""
    app.extensions["twinlab_auth"] = {"settings": Settings.from_env(), "db": db}

    @app.before_request
    def authorise():
        view = app.view_functions.get(request.endpoint)
        if request.endpoint == "static":
            return None
        if view is None:
            return None  # 404 / 405 follow
        policy = policy_of(view)
        if policy is None:
            abort(403, "This page has no access policy.")
        if policy[0] in ("public", "agent"):
            return None
        return identify()

    app.jinja_env.globals.update(can=can, is_admin=lambda: g.get("is_admin", False),
                                 login_available=login_available)
