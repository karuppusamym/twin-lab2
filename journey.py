"""Where an application is in the journey, and the one thing to do next.

Choose application -> Add examples -> Review required behaviour -> Build -> Verify -> Open or download

The approach (and much of the wording) follows Twin Lab's `labctl.portal.guide`: pure functions
over facts the portal already has, so the catalog, the application page and the tests agree.
The first required step not done is the current stage; its hint is the single next action.

`facts(db, app)` gathers the facts from the catalog. To change what a step needs (for example
what counts as reviewed behaviour, or which release states count as verified), change the fact
here; the pages only read `stage`, `next` and `missing`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

JOURNEY = ("Choose application", "Add examples", "Review required behaviour", "Build", "Verify",
           "Open or download")  # fmt: skip

# Release states (portal.STATES: packaged, tested, published) that count as verified, and as
# ready to open or download. A catalog without a release state column has neither.
VERIFIED_STATES = {"tested", "published"}
OPEN_STATES = {"published"}

# What a scope decision is called on the pages. The stored values stay as they are.
STATUS_LABELS = {
    "known": "Confirmed application",
    "discovered": "Discovered: decide scope",
    "not_replicated": "Not replicated",
}


@dataclass
class Step:
    id: str
    title: str
    done: bool
    hint: str  # what to do, in one plain sentence
    href: str
    action: str = ""  # the button's words
    state: str = "todo"  # done | next | todo


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
) -> Journey:
    """The journey of one application. `releases` is newest first."""
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

    if status == "discovered":
        choose_hint = ("A recording found this application: confirm it to replicate it, merge it "
                       "into the application it is the same as, or mark it as not replicated.")
        choose_action = "Decide scope"
    else:
        absent = [m.lower() for m in ("Owner", "Purpose") if m in missing]
        choose_hint = f"Say who owns this application and what the demo is for: add its {' and '.join(absent)}."
        choose_action = "Add " + " and ".join(absent)
    if not identities:
        examples_hint = ("Add the application's identity (its executable, web address or mainframe "
                         "host) so a recording can target it.")
        examples_href, examples_action = setup + "#identity", "Add identity"
    elif not machines:
        examples_hint = "Pair a recorder machine, then request a recording of the process."
        examples_href, examples_action = "/machines", "Pair a machine"
    else:
        examples_hint = ("Request a recording of the process on a paired machine, or import a "
                         "Twin Lab steps.jsonl.")
        examples_href, examples_action = "/sessions", "Request a recording"
    if briefs:
        build_hint = ("Have the app built from the approved builder package, then register the "
                      "committed build as a release (README: Releases).")
    else:
        build_hint = ("Preview the builder package, check its pre-freeze report, approve and "
                      "freeze it, and have the app built from it.")
    if latest:
        verify_hint = (f"Replay the recorded examples against release {latest['version']} and mark "
                       "it tested.")
    else:
        verify_hint = "Replay the recorded examples against the built release and mark it tested."

    steps = [
        Step("choose", JOURNEY[0], status == "known" and bool(owner) and bool(purpose),
             choose_hint, f"{base}#choose", choose_action),
        Step("examples", JOURNEY[1], examples > 0, examples_hint, examples_href, examples_action),
        Step("behaviour", JOURNEY[2], behaviour,
             "Review the recorded examples and add a business scenario for each outcome the "
             "demo must reproduce: starting conditions, actions, expected result, failure cases.",
             f"{base}#behaviour", "Add a scenario"),
        Step("build", JOURNEY[3], bool(releases), build_hint,
             f"{base}#build" if briefs else f"{base}/build-pack/preview",
             "Register a release" if briefs else "Freeze builder package"),
        Step("verify", JOURNEY[4], state in VERIFIED_STATES, verify_hint, release_href,
             "Verify release"),
        Step("open", JOURNEY[5], state in OPEN_STATES,
             "Publish the tested release, so that others can open the demo or download it.",
             release_href, "Publish release"),
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
    columns = {row[1] for row in db.execute("PRAGMA table_info(releases)")}
    wanted = ["id", "version"] + [c for c in ("state", "status") if c in columns]
    releases = [dict(zip(wanted, row)) for row in db.execute(
        f"SELECT {', '.join(wanted)} FROM releases WHERE app_id = ? ORDER BY id DESC", (app_id,))]
    keys = app.keys()
    return {
        "status": app["status"],
        "owner": (app["owner"] if "owner" in keys else "") or "",
        "purpose": (app["purpose"] if "purpose" in keys else "") or "",
        "identities": count("SELECT count(*) FROM app_identities WHERE app_id = ?"),
        "machines": db.execute("SELECT count(*) FROM machines WHERE revoked_at IS NULL").fetchone()[0],
        "examples": examples,
        "behaviour": behaviour_reviewed(db, app),
        # frozen builder packages (briefs of catalogs from before freezing count too)
        "briefs": count("SELECT count(*) FROM builder_packages WHERE app_id = ?") if "builder_packages" in tables(db)
        else count("SELECT count(*) FROM briefs WHERE app_id = ?"),
        "releases": releases,
    }


def tables(db) -> set:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def behaviour_reviewed(db, app) -> bool:
    """Required behaviour is written down once the app has a business scenario (the build pack
    needs one); a catalog from before scenarios counts its free-text requirements."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "scenarios" in tables and db.execute(
            "SELECT 1 FROM scenarios WHERE app_id = ? LIMIT 1", (app["id"],)).fetchone():
        return True
    return bool((app["requirements"] or "").strip())


def for_app(db, app) -> Journey:
    return app_journey(app["id"], **facts(db, app))
