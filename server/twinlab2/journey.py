"""Where an application is in the journey, and the one thing to do next.

Choose application -> Add examples -> Review required behaviour -> Connect repository -> Build ->
Verification settings -> Verify -> Open or download

The repository and the verification settings are real steps (a build commits to the demo
repository; verification reads it and needs the target platform and launch instructions), so no
required setting hides under Setup. A step that cannot be done yet carries `blocked`: the reason,
in one sentence, with the step's `href` pointing at the fix.

The approach (and much of the wording) follows Twin Lab's portal guide (`portal/guide.py`): pure functions
over facts the portal already has, so the catalog, the application page and the tests agree.
The first required step not done is the current stage; its hint is the single next action.

`facts(db, app)` gathers the facts from the catalog. To change what a step needs (for example
what counts as reviewed behaviour, or which release states count as verified), change the fact
here; the pages only read `stage`, `next` and `missing`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from twinlab2 import completeness as compl

JOURNEY = ("Choose application", "Add examples", "Review required behaviour", "Connect repository", "Build",
           "Verification settings", "Verify", "Open or download")  # fmt: skip

# Release states (portal.STATES: packaged, tested, published) that count as verified, and as
# ready to open or download. A catalog without a release state column has neither.
VERIFIED_STATES = {"tested", "published"}
OPEN_STATES = {"published"}

# The one mapping of scope statuses (the API's `AppStatus`) to what the pages call them. The stored
# values stay as they are; api/openapi.yaml (`AppStatus`) and web/src/lib/scope.ts repeat this table.
#   known          -> "Replicate"                    an owner chose to build a copy
#   discovered     -> "Needs a scope decision"       a recording found it, nobody decided yet (system state)
#   not_replicated -> "Not replicated (used only)"   recordings note it only as having been used
STATUS_LABELS = {
    "known": "Replicate",
    "discovered": "Needs a scope decision",
    "not_replicated": "Not replicated (used only)",
}
# What an owner may choose: "discovered" is the state before a decision, never a choice.
OWNER_CHOICES = ("known", "not_replicated")

# The catalog's groups (the API's `AppFamily`), in the order the catalog shows them: the scope
# decision first, then how far a replicated application got, out of scope last.
FAMILY_LABELS = {
    "decide": "Needs a scope decision",
    "build": "In build",
    "tested": "Tested",
    "published": "Published",
    "not_replicated": "Not replicated (used only)",
}


def family(status: str, release_states=()) -> str:
    """The catalog group of an application with scope `status` and these release states."""
    if status == "discovered":
        return "decide"
    if status == "not_replicated":
        return "not_replicated"
    states = set(release_states)
    if "published" in states:
        return "published"
    return "tested" if "tested" in states else "build"

# Build session states (builds.py) in which a session is still working on the package.
BUILD_ACTIVE = {"queued", "starting", "running", "waiting_for_answer"}


@dataclass
class Step:
    id: str
    title: str
    done: bool
    hint: str  # what to do, in one plain sentence
    href: str
    action: str = ""  # the button's words
    state: str = "todo"  # done | next | todo
    blocked: str = ""  # why the step cannot be done yet ("" = it can)


@dataclass
class Journey:
    steps: list[Step]
    missing: list[str] = field(default_factory=list)
    out_of_scope: bool = False

    @property
    def next(self) -> Step | None:
        return next((s for s in self.steps if s.state == "next"), None)

    @property
    def stage(self) -> str:
        if self.out_of_scope:
            return "Not replicated"
        step = self.next
        return step.title if step else "Ready to open or download"


def mark(steps: list[Step]) -> list[Step]:
    """The first step not done is `next`; the rest keep done or todo."""
    seen_next = False
    for s in steps:
        if s.done:
            s.state = "done"
        elif not seen_next:
            s.state, seen_next = "next", True
        else:
            s.state = "todo"
    return steps


def release_state(release: dict) -> str:
    """A release's state when the catalog keeps one (`state` or `status`), else ''."""
    return str(release.get("state") or release.get("status") or "").lower()


def app_journey(
    app_id: int,
    *,
    status: str,
    owner: str,
    purpose: str,
    identities: int,
    machines: int,
    examples: int,
    behaviour: bool,
    briefs: int,
    releases: list[dict],
    attention: list[dict] = (),
    repository: bool = True,
    verification: bool = True,
    build_sessions: list[dict] = (),
    connected_repos: int = 1,
) -> Journey:
    """The journey of one application. `releases` is newest first. `attention` lists the
    recordings of the application that are partial or failed and not accepted as they are, each
    {"id", "name", "overall", "summary"}: a stopped recording is not complete by itself, so
    examples are not done while one needs attention. `repository`: the demo repository is set;
    `verification`: the target platform and launch instructions are; `build_sessions` (newest
    first, {"id", "state", "head_sha"}); `connected_repos`: GitHub repositories an admin connected
    for build sessions."""
    base = f"/apps/{app_id}"
    setup = f"{base}/setup"
    latest = releases[0] if releases else None
    state = release_state(latest) if latest else ""
    release_href = f"/releases/{latest['id']}" if latest else f"{base}#build"

    missing = []
    if not owner:
        missing.append("Owner")
    if not purpose:
        missing.append("Purpose")
    if not identities:
        missing.append("Identity (to request a recording)")
    if not examples:
        missing.append("Examples")
    if not behaviour:
        missing.append("Required behaviour")
    for rec in attention:
        missing.append(f"Complete recording ({rec['name']} is {rec['overall']})")

    if status == "discovered":
        choose_hint = ("A recording found this application: replicate it, mark it as the same as an "
                       "application you have, or record it as not replicated (used only).")
        choose_action = "Decide scope"
        choose_href = f"/triage#app-{app_id}"
    else:
        absent = [m.lower() for m in ("Owner", "Purpose") if m in missing]
        choose_hint = f"Say who owns this application and what the demo is for: add its {' and '.join(absent)}."
        choose_action = "Add " + " and ".join(absent)
        choose_href = f"{base}#choose"
    if not identities:
        examples_hint = ("Add the application's identity (its executable, web address or mainframe "
                         "host) so a recording can target it.")
        examples_href, examples_action = setup + "#identity", "Add identity"
    elif not machines:
        examples_hint = "Pair a recorder machine, then request a recording of the process."
        examples_href, examples_action = "/machines", "Pair a machine"
    else:
        examples_hint = ("Request a recording of the process on a paired machine, or import a "
                         "recording file (steps.jsonl).")
        examples_href, examples_action = "/sessions", "Request a recording"
    if attention:
        rec = attention[0]
        examples_hint = (f"Recording “{rec['name']}” stopped but is {rec['overall']}: {rec['summary']}. "
                         "Re-record the missing part, or review it and accept it as it is.")
        examples_href, examples_action = f"/recordings/{rec['id']}", f"Review {rec['overall']} recording"
    build_href, build_action, build_blocked = f"{base}/build-pack/preview", "Freeze builder package", ""
    build_hint = ("Preview the builder package, check its readiness report, approve and freeze it, "
                  "then start a build session from it.")
    session = build_sessions[0] if build_sessions else None
    if briefs:
        build_href, build_action = f"{base}/build", "Start a build"
        build_hint = "Start a build session from the approved builder package."
        if session and session.get("state") == "ready_for_verification" and session.get("head_sha"):
            build_href, build_action = f"/builds/{session['id']}", "Verify this build"
            build_hint = (f"Build session #{session['id']} has a commit ready: verify it, which registers it "
                          "as a release of this application.")
        elif session and session.get("state") == "waiting_for_answer":
            build_href, build_action = f"/builds/{session['id']}", "Answer the provider"
            build_hint = f"Build session #{session['id']} is waiting for your answer to a question."
        elif session and session.get("state") in BUILD_ACTIVE:
            build_href, build_action = f"/builds/{session['id']}", "Open build session"
            build_hint = f"Build session #{session['id']} is working on the package."
        elif not connected_repos:
            build_blocked = ("No GitHub repository is connected for build sessions: a portal admin connects one "
                             "under Setup, Build sessions.")
    if not repository:
        build_blocked = "No demo repository is set: connect one first."
    if latest:
        verify_hint = (f"Replay the recorded examples against release {latest['version']} and mark "
                       "it tested.")
    else:
        verify_hint = "Replay the recorded examples against the built release and mark it tested."

    steps = [
        Step("choose", JOURNEY[0], status == "known" and bool(owner) and bool(purpose),
             choose_hint, choose_href, choose_action),
        Step("examples", JOURNEY[1], examples > 0 and not attention, examples_hint, examples_href,
             examples_action),
        Step("behaviour", JOURNEY[2], behaviour,
             "Review the recorded examples and add a business scenario for each outcome the "
             "demo must reproduce: starting conditions, actions, expected result, failure cases.",
             f"{base}#behaviour", "Add a scenario"),
        Step("repository", JOURNEY[3], repository,
             "Set the demo repository: the GitHub repository the build commits to, which "
             "verification reads.", setup + "#git", "Set repository"),
        Step("build", JOURNEY[4], bool(releases), build_hint, build_href, build_action, blocked=build_blocked),
        Step("verification", JOURNEY[5], verification,
             "Say how the built demo is checked: its target platform and launch instructions.",
             setup + "#verification", "Set verification",
             blocked="" if repository else "Verification reads the demo repository: connect one first."),
        Step("verify", JOURNEY[6], state in VERIFIED_STATES, verify_hint, release_href,
             "Verify release", blocked="" if releases else "There is no release to verify yet: build one first."),
        Step("open", JOURNEY[7], state in OPEN_STATES,
             "Publish the tested release, so that others can open the demo or download it.",
             release_href, "Publish release",
             blocked="" if state in VERIFIED_STATES else "Only a tested release can be published."),
    ]  # fmt: skip
    if status == "not_replicated":
        for s in steps:
            s.state = "todo"
        return Journey(steps, [], out_of_scope=True)
    return Journey(mark(steps), missing)


def facts(db, app) -> dict:
    """The facts `app_journey` needs, read from the catalog for one `apps` row."""
    app_id = app["id"]

    def count(sql):
        return db.execute(sql, (app_id,)).fetchone()[0]

    examples = (count("SELECT count(*) FROM recording_apps WHERE app_id = ?")
                + count("SELECT count(*) FROM evidence WHERE app_id = ?")
                + count("SELECT count(*) FROM attachments WHERE app_id = ?"))  # fmt: skip
    columns = set(db.columns("releases"))
    wanted = ["id", "version"] + [c for c in ("state", "status") if c in columns]
    releases = [dict(zip(wanted, row)) for row in db.execute(
        f"SELECT {', '.join(wanted)} FROM releases WHERE app_id = ? ORDER BY id DESC", (app_id,))]
    keys = app.keys()
    tables = db.tables()
    return {
        "status": app["status"],
        "owner": (app["owner"] if "owner" in keys else "") or "",
        "purpose": (app["purpose"] if "purpose" in keys else "") or "",
        "identities": count("SELECT count(*) FROM app_identities WHERE app_id = ?"),
        "machines": db.execute("SELECT count(*) FROM machines WHERE revoked_at IS NULL").fetchone()[0],
        "examples": examples,
        "behaviour": behaviour_reviewed(db, app),
        # frozen builder packages (briefs of catalogs from before freezing count too)
        "briefs": count("SELECT count(*) FROM builder_packages WHERE app_id = ?"),
        "releases": releases,
        "attention": recordings_needing_attention(db, app_id),
        "repository": bool(((app["git_url"] if "git_url" in keys else "") or "").strip()),
        "verification": all(((app[c] if c in keys else "") or "").strip() for c in ("target_platform", "launch")),
        "build_sessions": [dict(zip(("id", "state", "head_sha"), row)) for row in db.execute(
            "SELECT id, state, head_sha FROM builds WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,))]
        if "builds" in tables else [],
        "connected_repos": db.execute("SELECT count(*) FROM github_repos").fetchone()[0]
        if "github_repos" in tables else 0,
    }


def recordings_needing_attention(db, app_id) -> list[dict]:
    """The application's recordings that are partial or failed and that nobody has accepted."""
    rows = db.execute("""
        SELECT recordings.id, recordings.name, recording_completeness.completeness
        FROM recording_completeness JOIN recordings ON recordings.id = recording_completeness.recording_id
        WHERE recording_completeness.completeness IS NOT NULL AND recording_completeness.accepted_at IS NULL
          AND (recordings.id IN (SELECT recording_id FROM recording_apps WHERE app_id = ?)
               OR recordings.id IN (SELECT recording_id FROM evidence WHERE app_id = ?))
        ORDER BY recordings.id
    """, (app_id, app_id)).fetchall()
    out = []
    for rec_id, name, text in rows:
        data = json.loads(text)
        if compl.needs_attention(data):
            out.append({"id": rec_id, "name": name, "overall": data["overall"], "summary": compl.summary(data)})
    return out


def behaviour_reviewed(db, app) -> bool:
    """Required behaviour is written down once the app has a business scenario (the build pack
    needs one); a catalog from before scenarios counts its free-text requirements."""
    tables = db.tables()
    if "scenarios" in tables and db.execute(
            "SELECT 1 FROM scenarios WHERE app_id = ? LIMIT 1", (app["id"],)).fetchone():
        return True
    return bool((app["requirements"] or "").strip())


def for_app(db, app) -> Journey:
    return app_journey(app["id"], **facts(db, app))
