"""The owner-facing overviews of the /api/v1 API: a recording's workboard (one row per
application found, with its scope, review, scenarios, package readiness, build and release, and
one next action), the scope triage of discovered applications (with ranked "looks like"
suggestions and bulk decisions), the role-aware home, and registering a release from a commit or
a build session (what the README's release command did).

Each answers in one call what the pages would otherwise fetch per application. The rules stay
where they are (journey.py: the next action; pack_validator: readiness; packages: freezing;
verification.package_commit: a release of a commit); these routes only gather them.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re

from twinlab2 import api_models as m
from twinlab2 import auth
from twinlab2 import buildpack
from twinlab2 import builds
from twinlab2 import journey
from twinlab2 import packages
from twinlab2 import registry
from twinlab2 import scenarios
from twinlab2 import shapes
from twinlab2 import verification as commit_checks
from twinlab2.auth import require_role
from twinlab2.portal import VERSION_PATH
from twinlab2.rest import Routes, db, fail, g
from twinlab2.views import ensure_reviewer, get_app, get_recording, record, reviewer_of_recording, visible

V1 = "/api/v1"
routes = Routes()

# What the triage's decisions set (the API's AppStatus values; journey.STATUS_LABELS names them).
DECISIONS = {"replicate": "known", "not_replicated": "not_replicated"}
SAMPLES = 5


# -- identity -------------------------------------------------------------------------------------


def identity_rows(app_id):
    return db().execute("SELECT key, surface, facts FROM app_identities WHERE app_id = ? ORDER BY key",
                        (app_id,)).fetchall()


def identity_view(key, surface="", facts=None):
    """An identity key as people read it: host and path, the program file, or the host."""
    facts = facts or {}
    if key.startswith("web:"):
        origin, path = registry.web_parts(key)
        display = origin[4:].split("://", 1)[-1] + path
    elif key.startswith("mainframe:"):
        display = key.split(":", 1)[1]
    elif key.startswith("exe:"):
        display = key[4:]
    else:  # desktop:Company/Product
        display = facts.get("exe") or key.split(":", 1)[-1]
    surface = surface or (key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop")
    return {"key": key, "display": display, "surface": surface,
            "facts": {k: str(v) for k, v in facts.items() if k in ("exe", "company", "product", "version", "toolkit")}}


def identity_of(app_id, keys=()):
    """The application's identity: the key a recording used, else its first registered one."""
    rows = identity_rows(app_id)
    by_key = {r["key"]: r for r in rows}
    for key in [*keys, *by_key]:
        row = by_key.get(key)
        if row is not None:
            return identity_view(key, row["surface"] or "", json.loads(row["facts"] or "{}"))
        if key:
            return identity_view(key)
    return None


# -- the recording workboard -----------------------------------------------------------------------


def readiness(app_id, owner):
    """The package state of an application: {state: none|draft|frozen, rev, blockers, warnings,
    up_to_date, reason}. An owner's readiness is that of the current draft (pack_validator, as
    the preview shows it); a builder sees the latest frozen revision's stored result."""
    latest = packages.latest(db(), app_id)
    out = {"state": "frozen" if latest else "none", "rev": latest["rev"] if latest else None,
           "blockers": None, "warnings": None, "up_to_date": None, "reason": ""}
    if latest is not None:
        validation, _known = packages.completeness_of(latest)
        if validation:
            out["blockers"] = validation["counts"].get("critical", 0)
            out["warnings"] = validation["counts"].get("important", 0)
    if not owner:
        return out
    if not scenarios.load(db(), app_id):
        out.update(reason="No business scenario yet: a package needs one.")
        if latest is None:
            out["blockers"] = None
        return out
    try:
        draft = buildpack.draft(db(), app_id, VERSION_PATH)
    except Exception as error:  # noqa: BLE001 - one application's broken draft must not hide the board
        out.update(reason=f"The draft could not be built: {error}")
        return out
    counts = draft.validation["counts"]
    up_to_date = latest is not None and latest["sha256"] == draft.sha256
    out.update(up_to_date=up_to_date, blockers=counts.get("critical", 0), warnings=counts.get("important", 0))
    if not up_to_date:
        out["state"] = "draft"
        out["reason"] = ("Changes since revision {} are not frozen yet.".format(latest["rev"]) if latest
                         else "Not frozen yet.")
    return out


def build_of(app_id):
    row = db().execute("SELECT * FROM builds WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "state": row["state"], "state_label": builds.label(row["state"]),
            "outstanding": len(builds.outstanding(db(), row["id"])), "head_sha": row["head_sha"]}


def release_of(app_id):
    row = db().execute("SELECT id, version, state FROM releases WHERE app_id = ? ORDER BY id DESC LIMIT 1",
                       (app_id,)).fetchone()
    return {"id": row["id"], "version": row["version"], "state": row["state"]} if row else None


def row_action(app, recording_id, link, j, role):
    """The one primary action of a workboard row: {label, href, blocked}, or None."""
    app_id = app["id"]
    if role == "owner":
        if app["status"] == "discovered":
            return {"label": "Decide scope", "href": f"/recordings/{recording_id}/triage#app-{app_id}", "blocked": ""}
        if app["status"] == "not_replicated":
            return {"label": "Change scope", "href": f"/recordings/{recording_id}/triage#app-{app_id}", "blocked": ""}
        if link is not None and not link["reviewed"]:
            return {"label": "Review steps", "href": f"/recordings/{recording_id}?app_id={app_id}#assign", "blocked": ""}
        step = j.next
        if step is None:
            return {"label": "Open release", "href": j.steps[-1].href, "blocked": ""}
        return {"label": step.action, "href": step.href, "blocked": step.blocked}
    if role == "builder":
        if packages.latest(db(), app_id) is not None:
            return {"label": "Open build sessions", "href": f"/apps/{app_id}/build", "blocked": ""}
        return {"label": "Waiting for a package", "href": f"/apps/{app_id}",
                "blocked": "No package is approved for building yet."}
    if role == "viewer":
        return {"label": "Open", "href": f"/apps/{app_id}", "blocked": ""}
    return None


def workboard_row(app, recording_id, keys, steps):
    app_id = app["id"]
    role = auth.role_of(db(), app_id, g.user)
    owner = role == "owner"
    link = db().execute("SELECT selection, reviewed FROM evidence WHERE app_id = ? AND recording_id = ?",
                        (app_id, recording_id)).fetchone() if owner else None
    j = journey.for_app(db(), app)
    scenario_count = db().execute("SELECT count(*) FROM scenarios WHERE app_id = ?", (app_id,)).fetchone()[0]
    builder = role in ("owner", "builder")
    return {
        "app_id": app_id, "name": app["name"], "my_role": role,
        "identity": identity_of(app_id, keys),
        "scope": {"status": app["status"], "label": shapes.status_label(app["status"])},
        "steps": steps,
        "selected": len(json.loads(link["selection"])) if link else None,
        "reviewed": bool(link["reviewed"]) if link else None,
        "scenarios": scenario_count if owner else None,
        "package": readiness(app_id, owner) if builder and app["status"] == "known" else None,
        "build": build_of(app_id) if builder else None,
        "release": release_of(app_id) if role else None,
        "stage": j.stage,
        "action": row_action(app, recording_id, link, j, role),
    }


def flows_of(recording_id):
    """{app_id: [(key, steps)]} of the recording, from its references (a recorder's recording) or
    its evidence links (an imported event log)."""
    found = {}
    for row in db().execute("SELECT app_id, key, flow FROM recording_apps WHERE recording_id = ? ORDER BY key",
                            (recording_id,)).fetchall():
        found.setdefault(row["app_id"], []).append((row["key"], json.loads(row["flow"])))
    return found


def lanes(recording_id, apps, flows):
    """A swim lane per application over the recording's time axis: runs of consecutive steps of
    one application, each with its first and last time and a few of its steps. The axis is time
    when every step has its capture time, else the steps' order."""
    names = {a["id"]: a["name"] for a in apps}
    every = []
    for app_id, parts in flows.items():
        for _key, steps in parts:
            for st in steps:
                every.append((st.get("captured_at") or "", st.get("seq", st.get("n", 0)), app_id, st))
    timed = bool(every) and all(item[0] for item in every)
    every.sort(key=lambda item: (item[0], item[1]) if timed else (item[1], item[0]))
    runs = []
    for index, (at, _seq, app_id, st) in enumerate(every):
        if runs and runs[-1]["app_id"] == app_id:
            run = runs[-1]
            run.update(to_index=index, end=at or None, steps=run["steps"] + 1)
        else:
            run = {"app_id": app_id, "from_index": index, "to_index": index, "start": at or None,
                   "end": at or None, "steps": 1, "samples": []}
            runs.append(run)
        if len(run["samples"]) < SAMPLES:
            words = " ".join(str(st.get(k)) for k in ("action", "label", "control") if st.get(k))
            screen = st.get("screen")
            run["samples"].append(f"{at[11:19] + ' ' if at else ''}{words}{f' · {screen}' if screen else ''}".strip())
    by_app = {}
    for run in runs:
        by_app.setdefault(run.pop("app_id"), []).append(run)
    surface = {}
    for app_id, parts in flows.items():
        identity = identity_of(app_id, [k for k, _ in parts])
        surface[app_id] = identity["surface"] if identity else ""
    return {
        "axis": "time" if timed else "order",
        "start": every[0][0] or None if every else None,
        "end": every[-1][0] or None if every else None,
        "total": len(every),
        "lanes": [{"app_id": app_id, "name": names.get(app_id, str(app_id)), "surface": surface.get(app_id, ""),
                   "steps": sum(r["steps"] for r in by_app.get(app_id, [])), "runs": by_app.get(app_id, [])}
                  for app_id in sorted(flows, key=lambda i: min((r["from_index"] for r in by_app.get(i, [])),
                                                                default=0))],
    }


def shared_hosts(apps, flows):
    """Web applications of the recording that share one host, told apart by path."""
    names = {a["id"]: a["name"] for a in apps}
    hosts = {}
    for app_id, parts in flows.items():
        for key, _steps in parts:
            if key.startswith("web:"):
                origin, path = registry.web_parts(key)
                hosts.setdefault(origin, []).append({"app_id": app_id, "name": names.get(app_id, ""), "path": path or "/"})
    out = []
    for origin, members in sorted(hosts.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda a: (-len(a["path"]), a["name"]))
        host = origin[4:].split("://", 1)[-1]
        out.append({"host": host, "apps": members,
                    "text": (f"{', '.join(a['name'] + ' (' + a['path'] + ')' for a in members)} share {host}: "
                             "each step went to the application matched by the longest path prefix of the "
                             "step's address; a step outside every path stays with the host.")})
    return out


@routes.get(V1 + "/recordings/{recording_id}/workboard", auth.signed_in, response_model=m.Workboard)
def workboard(recording_id: str):
    """A reviewer of the recording: one row per application the recording holds."""
    item = get_recording(recording_id)
    ensure_reviewer(recording_id)
    flows = flows_of(recording_id)
    linked = [r[0] for r in db().execute("SELECT app_id FROM evidence WHERE recording_id = ?", (recording_id,))]
    ids = list(dict.fromkeys([*flows, *linked]))
    apps = [db().execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone() for app_id in ids]
    apps = sorted((a for a in apps if a is not None),
                  key=lambda a: (a["status"] != "discovered", a["status"] == "not_replicated", a["name"].casefold()))
    rows = [workboard_row(a, recording_id, [k for k, _ in flows.get(a["id"], [])],
                          sum(len(s) for _k, s in flows.get(a["id"], []))) for a in apps]
    status = registry.recording_status(db(), recording_id)
    downloadable = [r for r in rows if r["package"] and r["package"]["rev"] is not None
                    and r["my_role"] in ("owner", "builder")]
    return {
        "recording": {"id": item["id"], "name": item["name"], "status_label": shapes.recording_status(status)["label"]},
        "apps": rows,
        "counts": {"apps": len(rows), "needs_decision": sum(r["scope"]["status"] == "discovered" for r in rows),
                   "replicate": sum(r["scope"]["status"] == "known" for r in rows),
                   "not_replicated": sum(r["scope"]["status"] == "not_replicated" for r in rows),
                   "approved_packages": len(downloadable)},
        "download_href": (f"/api/v1/recordings/{item['id']}/packages/download" if downloadable else None),
        "timeline": lanes(recording_id, apps, flows),
        "shared_hosts": shared_hosts(apps, flows),
        "labels": scope_labels(),
    }


def scope_labels():
    return [{"value": value, "label": label, "decision": next((d for d, v in DECISIONS.items() if v == value), None)}
            for value, label in journey.STATUS_LABELS.items()]


# -- scope triage ---------------------------------------------------------------------------------


QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")


def base_name(name):
    return re.sub(r"[^0-9a-zÀ-￿]+", " ", QUALIFIER.sub("", name).casefold()).strip()


def word_overlap(a, b):
    """The share of words two names have in common (Jaccard): "billing admin" and "billing" 0.5;
    one shared word among five ("Kasse Filiale Nord", "Zählerstand Portal Nord") is not similar."""
    x, y = set(a.split()), set(b.split())
    return len(x & y) / len(x | y) if x | y else 0.0


def exe_stem(facts):
    exe = (facts.get("exe") or "").casefold()
    return re.sub(r"\.(exe|jar|app)$", "", exe)


def host_of(key):
    if key.startswith("web:"):
        return registry.web_parts(key)[0][4:].split("://", 1)[-1]
    if key.startswith("mainframe:"):
        return key.split(":", 2)[1]
    return None


def looks_like(app, idents, other, other_idents):
    """(score, reasons) of how much `other` looks like the same application as `app`."""
    score, reasons = 0, []
    hosts = {host_of(i["key"]) for i in idents} - {None}
    other_hosts = {host_of(i["key"]) for i in other_idents} - {None}
    if hosts & other_hosts:
        score += 3
        reasons.append(f"same host ({sorted(hosts & other_hosts)[0]})")
    mine, theirs = base_name(app["name"]), base_name(other["name"])
    if mine and mine == theirs:
        score += 3
        reasons.append("same name")
    elif mine and theirs and (SequenceMatcher(None, mine, theirs).ratio() >= 0.75
                              or word_overlap(mine, theirs) >= 0.5):
        score += 2
        reasons.append("similar name")
    vendors = {(i["facts"].get("company") or "").casefold() for i in idents} - {""}
    other_vendors = {(i["facts"].get("company") or "").casefold() for i in other_idents} - {""}
    if vendors & other_vendors:
        score += 2
        company = next(i["facts"]["company"] for i in idents if (i["facts"].get("company") or "").casefold() in other_vendors)
        reasons.append(f"same vendor ({company})")
    stems = {exe_stem(i["facts"]) for i in idents} - {""}
    other_stems = {exe_stem(i["facts"]) for i in other_idents} - {""}
    if stems and other_stems and any(a == b or a.endswith(b) or b.endswith(a) for a in stems for b in other_stems):
        score += 2
        reasons.append("similar program file")
    return score, reasons


def identities_view(app_id):
    return [identity_view(r["key"], r["surface"] or "", json.loads(r["facts"] or "{}")) for r in identity_rows(app_id)]


def triage_card(app, candidates, idents_of):
    app_id = app["id"]
    rows = db().execute("""SELECT recording_apps.recording_id, recordings.name, recording_apps.flow
                           FROM recording_apps JOIN recordings ON recordings.id = recording_apps.recording_id
                           WHERE app_id = ? ORDER BY recordings.name""", (app_id,)).fetchall()
    screens, controls, steps, seen = [], [], 0, {}
    for row in rows:
        flow = json.loads(row["flow"])
        steps += len(flow)
        seen[row["recording_id"]] = {"id": row["recording_id"], "name": row["name"],
                                     "steps": seen.get(row["recording_id"], {}).get("steps", 0) + len(flow)}
        for st in flow:
            if st.get("screen") and st["screen"] not in screens and len(screens) < SAMPLES:
                screens.append(st["screen"])
            control = " ".join(str(st[k]) for k in ("label", "control") if st.get(k))
            if control and control not in controls and len(controls) < SAMPLES:
                controls.append(control)
    mine = idents_of(app_id)
    suggestions = []
    for other in candidates:
        if other["id"] == app_id:
            continue
        score, reasons = looks_like(app, mine, other, idents_of(other["id"]))
        if score:
            suggestions.append({"app_id": other["id"], "name": other["name"], "status": other["status"],
                                "status_label": shapes.status_label(other["status"]), "score": score,
                                "reasons": reasons, "can_merge": auth.can("owner", app_id=other["id"])})
    suggestions.sort(key=lambda s: (-s["score"], s["status"] != "known", s["name"].casefold()))
    return {"app_id": app_id, "name": app["name"], "revision": app["revision"],
            "status": app["status"], "status_label": shapes.status_label(app["status"]),
            "surface": (mine[0]["surface"] if mine else app["surface"] or ""),
            "identities": mine, "recordings": list(seen.values()), "steps": steps,
            "screens": screens, "controls": controls, "suggestions": suggestions[:3]}


@routes.get(V1 + "/triage", auth.signed_in, response_model=m.Triage)
def triage():
    """The applications waiting for a scope decision that the caller owns (one card each), or with
    `recording_id`, those of that recording (`status=all`: decided ones too, to change them)."""
    query = g.request.query_params
    recording_id = query.get("recording_id") or ""
    every = query.get("status") == "all"
    rows = db().execute("SELECT * FROM apps ORDER BY name").fetchall()
    if recording_id:
        get_recording(recording_id)
        ensure_reviewer(recording_id)
        ids = {r[0] for r in db().execute(
            "SELECT app_id FROM recording_apps WHERE recording_id = ? UNION "
            "SELECT app_id FROM evidence WHERE recording_id = ?", (recording_id, recording_id))}
        rows = [r for r in rows if r["id"] in ids]
    mine = [r for r in rows if auth.can("owner", app_id=r["id"]) and (every or r["status"] == "discovered")]
    mine.sort(key=lambda r: (r["status"] != "discovered", r["name"].casefold()))
    candidates = [r for r in visible(db().execute("SELECT * FROM apps WHERE status != 'not_replicated' "
                                                  "ORDER BY name").fetchall())]
    cache = {}

    def idents_of(app_id):
        if app_id not in cache:
            cache[app_id] = identities_view(app_id)
        return cache[app_id]

    return {"apps": [triage_card(r, candidates, idents_of) for r in mine],
            "same_as": [shapes.ref(r) for r in candidates if auth.can("owner", app_id=r["id"])],
            "labels": scope_labels(),
            "recording": ({"id": recording_id, "name": get_recording(recording_id)["name"]} if recording_id else None)}


@routes.post(V1 + "/triage/decisions", auth.signed_in, response_model=m.TriageOutcome)
def decide(body: m.TriageDecisions):
    """Replicate, or record as not replicated (used only), one or many applications at once: all
    of them or none. Each needs the owner role. Reversible: deciding again changes it back."""
    items = body.decisions
    if not items:
        fail(400, "Choose at least one application.", errors=[{"field": "decisions", "message": "empty"}])
    changed = []
    with db():
        for item in items:
            status = DECISIONS.get(item.decision)
            if status is None:
                fail(400, "Decide replicate or not_replicated.", errors=[{"field": "decision", "message": "unknown"}])
            auth.ensure("owner", app_id=item.app_id)
            app = get_app(item.app_id)
            sql, args = "UPDATE apps SET status = ?, revision = revision + 1 WHERE id = ?", [status, item.app_id]
            if item.expected_revision is not None:
                sql, args = sql + " AND revision = ?", args + [item.expected_revision]
            if db().execute(sql, tuple(args)).rowcount != 1:
                fail(409, f"{app['name']} changed since you loaded it: reload and decide again.", "conflict")
            record("app.status", "app", item.app_id, app_id=item.app_id, status=status, previous=app["status"],
                   via="triage")
            changed.append({"id": item.app_id, "name": app["name"], "status": status,
                            "status_label": shapes.status_label(status), "previous": app["status"]})
    replicate = [c["name"] for c in changed if c["status"] == "known"]
    skipped = [c["name"] for c in changed if c["status"] == "not_replicated"]
    parts = []
    if replicate:
        parts.append(f"{', '.join(replicate)}: replicate")
    if skipped:
        parts.append(f"{', '.join(skipped)}: not replicated (later recordings note it only as used)")
    return {"apps": changed, "message": "; ".join(parts) + ". You can reverse this at any time from the triage."}


# -- the role-aware home -------------------------------------------------------------------------


@routes.get(V1 + "/home", auth.signed_in, response_model=m.Home)
def home():
    """What the signed-in user's role needs first: viewers the published demos; builders the
    packages ready to build and questions waiting for them; owners their recordings' workboards
    and the triage queue; admins also Setup."""
    role = auth.home_role()
    apps = visible(db().execute("SELECT * FROM apps ORDER BY name").fetchall())
    published, ready, waiting = [], [], []
    for app in apps:
        app_id = app["id"]
        row = db().execute("SELECT id, version, state FROM releases WHERE app_id = ? AND state = 'published' "
                           "ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()
        if row is not None:
            deployment = db().execute("SELECT url, verification FROM deployments WHERE release_id = ? "
                                      "ORDER BY verification = 'verified' DESC, environment", (row["id"],)).fetchone()
            published.append({"app_id": app_id, "name": app["name"], "purpose": app["purpose"] or "",
                              "release_id": row["id"], "version": row["version"],
                              "url": deployment["url"] if deployment else None,
                              "verified": bool(deployment and deployment["verification"] == "verified")})
        if not auth.can("builder", app_id=app_id):
            continue
        latest = packages.latest(db(), app_id)
        if latest is None:
            continue
        build = build_of(app_id)
        ready.append({"app_id": app_id, "name": app["name"], "rev": latest["rev"],
                      "approved_at": latest["approved_at"], "build": build,
                      "release": release_of(app_id)})
        for b in db().execute("SELECT id, state FROM builds WHERE app_id = ? ORDER BY id DESC", (app_id,)).fetchall():
            count = len(builds.outstanding(db(), b["id"]))
            if count:
                waiting.append({"build_id": b["id"], "app_id": app_id, "name": app["name"], "questions": count})
    boards = []
    for rec in db().execute("""SELECT recordings.id, recordings.name, session_refs.received_at FROM recordings
                               LEFT JOIN session_refs ON session_refs.recording_id = recordings.id
                               ORDER BY session_refs.received_at DESC NULLS LAST, recordings.name""").fetchall():
        if len(boards) == 20:
            break
        if not reviewer_of_recording(rec["id"]):
            continue
        ids = {r[0] for r in db().execute(
            "SELECT app_id FROM recording_apps WHERE recording_id = ? UNION "
            "SELECT app_id FROM evidence WHERE recording_id = ?", (rec["id"], rec["id"]))}
        statuses = [r[0] for r in db().execute("SELECT status FROM apps WHERE id IN (%s)" % ",".join("?" * len(ids)),
                                               tuple(ids)).fetchall()] if ids else []
        boards.append({"id": rec["id"], "name": rec["name"], "received_at": rec["received_at"],
                       "apps": len(ids), "needs_decision": statuses.count("discovered")})
    triage_count = sum(1 for a in apps if a["status"] == "discovered" and auth.can("owner", app_id=a["id"]))
    return {"role": role, "user": g.user, "can_create_apps": auth.may_create_apps(),
            "published": published, "ready_to_build": ready, "waiting": waiting,
            "workboards": boards, "triage_count": triage_count, "is_admin": bool(g.is_admin)}


# -- a release from a commit or a build session ---------------------------------------------------


@routes.post(V1 + "/apps/{app_id:int}/releases", require_role("builder"), response_model=m.ReleaseRegistered,
             status_code=201)
def register_release(app_id: int, body: m.ReleaseInput):
    """A Packaged release of a commit: a build session's head commit (`build_id`), or `sha` in the
    demo repository (or `repo`). It answers the session's package revision, else the latest
    frozen one. Registering does not verify it: verification is the next step (an owner's)."""
    from twinlab2.routes_portal import github

    item = get_app(app_id)
    build_id = body.build_id
    if build_id:
        build = builds.get(db(), build_id)
        if build is None or build["app_id"] != app_id:
            fail(400, "That build session is not one of this application's.")
        if not build["head_sha"]:
            fail(400, "That build session has no commit yet.")
        repo, sha, pr, ref, revision = (build["repo"], build["head_sha"], build["pr_number"], build["branch"] or "",
                                        build["revision"])
    else:
        repo = (body.repo or "").strip() or commit_checks.github_repo(item["git_url"] or "")
        sha, pr, ref = (body.sha or "").strip(), body.pr_number, (body.ref or "").strip()
        if not repo:
            fail(400, "Set the demo repository first, or name the repository.",
                 errors=[{"field": "repo", "message": "required"}])
        frozen = packages.latest(db(), app_id)
        if frozen is None:
            fail(400, "Freeze a builder package first: a release answers an approved package.")
        revision = frozen["revision"]
    try:
        repo, sha = commit_checks.check_commit_args(repo, sha)
        with db():
            before = db().execute("SELECT id FROM releases WHERE app_id = ? AND commit_sha = ?", (app_id, sha)).fetchone()
            release_id = commit_checks.package_commit(db(), app_id, repo, sha, github(), revision, pr, ref,
                                                      build_id=build_id or None)
            if before is None:
                record("release.register", "release", release_id, app_id=app_id, commit=sha, repo=repo,
                       build_id=build_id, revision=revision)
    except ValueError as error:
        fail(400, str(error))
    row = db().execute("SELECT id, version, revision, commit_sha, archive_sha, state FROM releases WHERE id = ?",
                       (release_id,)).fetchone()
    return {"release": shapes.release_summary(row, registry.release_labels(db(), app_id)[release_id]), "created": before is None, "href": f"/releases/{release_id}",
            "next": "Verify the release: replay the recorded examples against its deployment."}
