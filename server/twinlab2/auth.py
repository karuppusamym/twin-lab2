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

Every route needs a policy. `require_role(...)` (or `public`, `signed_in`, `agent_token`) is
given when the route is added (`rest.Routes`), and runs as its FastAPI dependency; a route added
without one answers 403 (`rest.PolicyRoute`), so a route added later without a decision is
closed rather than open, and a test lists every route's policy.

Sign-in: the auth mode
---------------------
`TWINLAB2_AUTH_MODE` says how people sign in; the portal logs the mode at start and
`GET /api/v1/session` reports it (`access_mode`, shown on the Users page):

- `local-dev`: only this machine. Requests from loopback are the admin user `local`; any other
  request is refused (403), and there is no sign-in form. Refused (at start, and per request)
  when local users with passwords exist, unless `TWINLAB2_LOCAL_DEV_FORCE=1`: once people have
  their own passwords, the loopback admin would bypass them.
- `individual`: everyone signs in with their own password (the local user store: `users`,
  PBKDF2-SHA256; the web app's sign-in page, `POST /api/v1/session`). No loopback admin, and
  proxy headers are ignored. `TWINLAB2_PASSWORD` (the shared demo password) signs in only as
  the one demo identity `TWINLAB2_SHARED_USER` (default `demo`): never an admin (that session
  is never admin, and a start with the demo identity named an admin is refused), never a name
  with its own password. The demo identity holds only the roles an owner assigns to it.
  Named owners, builders and admins use their own credentials (or the SSO proxy).
- `trusted-proxy`: identity only from the SSO / OAuth proxy's headers (`X-Forwarded-User` /
  `X-Forwarded-Preferred-Username`, groups from `X-Forwarded-Groups`; members of
  `TWINLAB2_ADMIN_GROUP` are admins), and only for requests that come from the proxy: the
  peer address must be in `TWINLAB2_TRUSTED_PROXIES` (comma-separated addresses or networks,
  default loopback: the sidecar proxy in the same pod). Requests from anywhere else are 403.
  No sign-in form and no loopback admin. To take identity from somewhere else (a signed JWT,
  mTLS), replace `proxy_identity`.

Without `TWINLAB2_AUTH_MODE` the mode is derived: `trusted-proxy` with the older
`TWINLAB2_TRUST_PROXY_USER=1`; `individual` when `TWINLAB2_PASSWORD` is set or a local user has
a password; else `local-dev`. Outside `local-dev` the portal refuses to start without
`TWINLAB2_SECRET_KEY` (app.py).

Admins come from `TWINLAB2_ADMINS` (comma separated), `users.is_admin` (`python -m
twinlab2.portal user NAME --admin`) or the proxy's admin group.

Sessions (individual mode) are server-side rows (`auth_sessions`): each sign-in makes one,
the signed cookie carries its id, and it ends after `TWINLAB2_SESSION_HOURS` (default 12), at
sign-out, or when the user's password changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import os
import secrets

from fastapi import Request

from twinlab2.rest import check_csrf, db, fail, form_token, g, is_api

ROLES = ("viewer", "builder", "owner")
RANK = {role: rank for rank, role in enumerate(ROLES, 1)}
ADMIN = "admin"
LOCAL_USER = "local"
LOCAL = {"127.0.0.1", "::1"}

# The tables are created by migrations.py (6: roles and audit).

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
            revoke_user_sessions(db, name)  # a new (or removed) password ends the user's sessions
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
    db.execute("INSERT INTO app_roles(app_id, user_name, role, granted_by, granted_at) VALUES (?, ?, ?, ?, ?)",
               (app_id, user, role, by, now))
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

LOCAL_DEV, INDIVIDUAL, TRUSTED_PROXY = "local-dev", "individual", "trusted-proxy"
MODES = (LOCAL_DEV, INDIVIDUAL, TRUSTED_PROXY)
SHARED_USER = "demo"
SESSION_HOURS = 12.0
# Who may add an application (TWINLAB2_APP_CREATORS): `owners` (default: a portal admin, or anyone
# who owns an application already), `admins` (portal admins only) or `anyone` signed in. Whoever
# adds an application becomes its owner, so `anyone` lets a viewer make themselves an owner.
APP_CREATORS = ("owners", "admins", "anyone")


class ConfigError(RuntimeError):
    """The portal's auth settings are unsafe or contradictory: it does not start."""


def _networks(text):
    found = []
    for item in (x.strip() for x in text.split(",")):
        if item:
            try:
                found.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise ConfigError(f"TWINLAB2_TRUSTED_PROXIES: {item!r} is not an address or network.") from None
    return found


@dataclass
class Settings:
    mode: str = ""  # TWINLAB2_AUTH_MODE as given ("" = derived, see `mode_of`)
    trust_proxy: bool = False  # the older TWINLAB2_TRUST_PROXY_USER=1 (derives trusted-proxy)
    password: str = ""
    shared_user: str = SHARED_USER
    admins: set = field(default_factory=set)
    admin_group: str = ""
    trusted_proxies: list = field(default_factory=lambda: _networks("127.0.0.1,::1"))
    force_local_dev: bool = False
    session_hours: float = SESSION_HOURS
    app_creators: str = "owners"

    @classmethod
    def from_env(cls):
        env = os.environ.get
        mode = env("TWINLAB2_AUTH_MODE", "").strip().lower()
        if mode and mode not in MODES:
            raise ConfigError(f"TWINLAB2_AUTH_MODE must be one of {', '.join(MODES)}, not {mode!r}.")
        try:
            hours = float(env("TWINLAB2_SESSION_HOURS", "") or SESSION_HOURS)
        except ValueError:
            raise ConfigError("TWINLAB2_SESSION_HOURS must be a number of hours.") from None
        if not 0 < hours <= 24 * 30:
            raise ConfigError("TWINLAB2_SESSION_HOURS must be more than 0 and at most 720.")
        creators = (env("TWINLAB2_APP_CREATORS", "") or "owners").strip().lower()
        if creators not in APP_CREATORS:
            raise ConfigError(f"TWINLAB2_APP_CREATORS must be one of {', '.join(APP_CREATORS)}, not {creators!r}.")
        return cls(
            mode=mode,
            trust_proxy=env("TWINLAB2_TRUST_PROXY_USER") == "1",
            password=env("TWINLAB2_PASSWORD", ""),
            shared_user=(env("TWINLAB2_SHARED_USER", "") or SHARED_USER).strip(),
            admins={n.strip() for n in env("TWINLAB2_ADMINS", "").split(",") if n.strip()},
            admin_group=env("TWINLAB2_ADMIN_GROUP", "").strip(),
            trusted_proxies=_networks(env("TWINLAB2_TRUSTED_PROXIES", "") or "127.0.0.1,::1"),
            force_local_dev=env("TWINLAB2_LOCAL_DEV_FORCE") == "1",
            session_hours=hours,
            app_creators=creators,
        )

    @property
    def proxy(self):
        """Whether identity comes from the proxy (fixed for the process: never derived from data)."""
        return self.mode == TRUSTED_PROXY or (not self.mode and self.trust_proxy)


def mode_of(settings, connection):
    """The auth mode in force: the configured one, else derived (see the module's docstring)."""
    if settings.mode:
        return settings.mode
    if settings.trust_proxy:
        return TRUSTED_PROXY
    if settings.password or has_local_users(connection):
        return INDIVIDUAL
    return LOCAL_DEV


def is_admin_name(settings, connection, user):
    if user in settings.admins:
        return True
    row = connection.execute("SELECT is_admin FROM users WHERE name = ?", (user,)).fetchone()
    return row is not None and bool(row[0])


def check_settings(settings, connection, secret_set):
    """ConfigError when the portal must not start with these settings; else the mode's log line."""
    mode = mode_of(settings, connection)
    if settings.mode == LOCAL_DEV and has_local_users(connection) and not settings.force_local_dev:
        raise ConfigError("TWINLAB2_AUTH_MODE=local-dev, but local users with passwords exist: this machine's "
                          "admin `local` would bypass them. Use TWINLAB2_AUTH_MODE=individual, or set "
                          "TWINLAB2_LOCAL_DEV_FORCE=1 to serve this machine anyway.")
    if mode != LOCAL_DEV and not secret_set:
        raise ConfigError(f"The {mode} auth mode needs TWINLAB2_SECRET_KEY (a long random value, the same "
                          "for every replica): sessions and download links are signed with it.")
    if settings.password:
        if not valid_name(settings.shared_user):
            raise ConfigError("TWINLAB2_SHARED_USER must be a user name of 1 to 120 printable characters.")
        if is_admin_name(settings, connection, settings.shared_user):
            raise ConfigError(f"TWINLAB2_SHARED_USER {settings.shared_user!r} is a portal admin: the shared "
                              "password must never sign in as an admin. Name a separate demo identity.")
    how = {LOCAL_DEV: "only this machine, as the admin user `local`" + (" (forced)" if settings.force_local_dev else ""),
           INDIVIDUAL: "everyone signs in with their own password" + (
               f"; the shared password signs in only as {settings.shared_user!r}" if settings.password else ""),
           TRUSTED_PROXY: "identity from the SSO proxy's headers, accepted from "
                          + ", ".join(str(n) for n in settings.trusted_proxies)}[mode]
    derived = "" if settings.mode else " (derived: set TWINLAB2_AUTH_MODE to fix it)"
    return f"auth mode {mode}{derived}: {how}"


def proxy_identity(req):
    """(user, groups) from a trusted SSO gateway's headers, or (None, set())."""
    user = req.headers.get("X-Forwarded-User") or req.headers.get("X-Forwarded-Preferred-Username")
    groups = {x.strip() for x in req.headers.get("X-Forwarded-Groups", "").split(",") if x.strip()}
    return (user[:120] if user else None), groups


def _settings() -> Settings:
    return g.settings


def _db():
    return db()


def client_host(req):
    return req.client.host if req.client else ""


def _address_in(host, networks):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def from_proxy(req, settings):
    """Whether the request's peer is one of the trusted proxies."""
    return _address_in(client_host(req), settings.trusted_proxies)


ONLY_THIS_MACHINE = ("This portal serves only this machine (auth mode local-dev): set TWINLAB2_AUTH_MODE=individual "
                     "and add users, or run it behind the SSO proxy.")
LOCAL_DEV_REFUSED = ("Local users with passwords exist, so this machine's admin is not signed in automatically: "
                     "set TWINLAB2_AUTH_MODE=individual and sign in.")
NOT_FROM_PROXY = "Requests must come through the SSO / OAuth proxy."
REFUSALS = (ONLY_THIS_MACHINE, LOCAL_DEV_REFUSED, NOT_FROM_PROXY)


# -- server-side sessions (individual mode) ------------------------------------------------------


def _stamp(when=None):
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def start_session(connection, user, via, hours):
    """A new session row for `user` (via "password" or "shared"); returns its id. No commit."""
    sid = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    connection.execute("INSERT INTO auth_sessions(id, user_name, via, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                       (sid, user, via, _stamp(now), _stamp(now + timedelta(hours=hours))))
    return sid


def session_row(connection, sid, user):
    """The live session `sid` of `user`, or None (unknown, expired, revoked, another user)."""
    if not sid or not isinstance(sid, str):
        return None
    row = connection.execute("SELECT * FROM auth_sessions WHERE id = ?", (sid,)).fetchone()
    if row is None or row["user_name"] != user or row["revoked_at"] or row["expires_at"] <= _stamp():
        return None
    return row


def revoke_session(connection, sid):
    connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_stamp(), sid))


def revoke_user_sessions(connection, user):
    """End every session of `user` (a password change, or an admin's reset). No commit."""
    connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_name = ? AND revoked_at IS NULL",
                       (_stamp(), user))


def resolve(req=None):
    """(user, is_admin) of the request, or (None, the reason nobody is signed in). Never raises."""
    req = req or g.request
    settings, connection = _settings(), _db()
    mode = mode_of(settings, connection)
    admin = False
    if mode == TRUSTED_PROXY:
        if not from_proxy(req, settings):
            return None, NOT_FROM_PROXY
        user, groups = proxy_identity(req)
        if not user:
            return None, "Sign in through the SSO / OAuth proxy."
        admin = bool(settings.admin_group and settings.admin_group in groups)
    elif mode == INDIVIDUAL:
        user = req.session.get("user")
        row = session_row(connection, req.session.get("sid"), user) if user else None
        if row is None:
            return None, "Sign in first."
        if row["via"] == "shared":
            return user, False  # the shared demo identity is never an admin
    else:  # local-dev
        if client_host(req) not in LOCAL:
            return None, ONLY_THIS_MACHINE
        if has_local_users(connection) and not settings.force_local_dev:
            return None, LOCAL_DEV_REFUSED
        return LOCAL_USER, True  # the machine's own user
    return user, admin or is_admin_name(settings, connection, user)


def identify():
    """Set g.user and g.is_admin for the request, or answer 401 `signed_out` (403 when the
    request may not sign in here at all: not this machine, or not through the proxy)."""
    user, admin = resolve()
    if user is None:
        if admin in REFUSALS:
            fail(403, admin)
        fail(401, admin, "signed_out")
    g.user, g.is_admin = user, admin


def login_user(name, password):
    """How `name` signs in with `password`: "password" (a local user with their own password),
    "shared" (the shared demo password, only as the demo identity), or None."""
    settings, connection = _settings(), _db()
    row = connection.execute("SELECT password_hash FROM users WHERE name = ?", (name,)).fetchone()
    if row is not None and row[0]:
        return "password" if check_password(row[0], password) else None
    if (settings.password and name == settings.shared_user and password
            and secrets.compare_digest(password.encode("utf-8"), settings.password.encode("utf-8"))
            and not is_admin_name(settings, connection, name)):
        return "shared"
    return None


def access_mode():
    """local-dev, individual or trusted-proxy: the mode in force for this request."""
    return mode_of(_settings(), _db())


def login_available():
    return access_mode() == INDIVIDUAL


def auth_mode():
    """proxy (the OAuth proxy names the user), local (a sign-in form), or open (this machine)."""
    return {TRUSTED_PROXY: "proxy", INDIVIDUAL: "local", LOCAL_DEV: "open"}[access_mode()]


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
    """403 (`role`) unless the current user holds one of `roles` on `app_id`."""
    if not allowed(roles, app_id):
        names = " or ".join(r if r != ADMIN else "portal admin" for r in roles)
        fail(403, f"This needs the {names} role" + (" on this application." if app_id is not None else "."), "role")


def can(*roles, app_id=None):
    return allowed(roles, app_id)


def may_create_apps(user=None, admin=None) -> bool:
    """Whether the current user may add an application (Settings.app_creators)."""
    user = g.user if user is None else user
    admin = g.get("is_admin", False) if admin is None else admin
    if user is None:
        return False
    rule = getattr(_settings(), "app_creators", "owners")
    if admin or rule == "anyone":
        return True
    if rule == "admins":
        return False
    return _db().execute("SELECT 1 FROM app_roles WHERE user_name = ? AND role = 'owner' LIMIT 1",
                         (user,)).fetchone() is not None


def home_role(user=None, admin=None) -> str:
    """The highest role the user holds anywhere: admin, owner, builder, viewer, or none."""
    user = g.user if user is None else user
    admin = g.get("is_admin", False) if admin is None else admin
    if admin:
        return ADMIN
    held = [row[0] for row in _db().execute("SELECT role FROM app_roles WHERE user_name = ?", (user,))]
    return max(held, key=lambda r: RANK.get(r, 0)) if held else "none"


class Policy:
    """Who may call a route. `dependency` is the FastAPI dependency that enforces it: it signs
    the request in (unless public or the agent's token, which the view checks), checks the
    roles on the application(s) `resolve(path parameters)` names, then the CSRF header."""

    def __init__(self, kind, roles=(), resolve=None):
        self.kind, self.roles, self.resolve = kind, tuple(roles), resolve
        self.mark = (kind, self.roles)

        def dependency(request: Request):
            g.request = request
            if is_api(request.url.path):
                form_token()
            if self.kind == "roles":
                identify()
                target = self.resolve(dict(request.path_params)) if self.resolve else None
                targets = target if isinstance(target, (list, tuple, set)) else [target]
                for app_id in targets:
                    ensure(*self.roles, app_id=app_id)
            elif self.kind == "signed_in":
                identify()
            check_csrf(request)

        self.dependency = dependency


def require_role(*roles, resolve=app_from_args):
    """The user needs one of `roles` (owner, builder, viewer — ranked — on the application
    `resolve(path parameters)` returns, or `admin`). `resolve` may return one application id, a
    list of them (all needed), or None (then only `admin` can pass)."""
    for role in roles:
        if role not in RANK and role != ADMIN:
            raise ValueError(f"unknown role {role!r}")
    return Policy("roles", roles, resolve)


public = Policy("public")  # no sign-in (the session endpoint, the web app's files, health check)
signed_in = Policy("signed_in")  # any signed-in user; the view filters what it shows
agent_token = Policy("agent")  # the recorder's bearer token (registry routes check it)


def policy_of(view):
    return getattr(view, "twinlab_policy", None)
