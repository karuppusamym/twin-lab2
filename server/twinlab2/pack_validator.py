"""Package completeness: does a draft build pack carry what a coding agent needs to rebuild the
application so that its recorded scenarios replay against the copy?

`SPEC` is the machine-readable completeness spec (docs/PACKAGE-COMPLETENESS.md is the same in
prose): one `Item` per thing a builder needs, with the surfaces it applies to (web, desktop,
mainframe), its severity and why it matters. Severity:

- **critical**: without it the rebuilt app cannot pass the replay or the release checks
  (twinlab2.replay.fidelity compares, after every step, the interactive elements by tag, id and
  classes, their visibility, enabled state, option lists and value format; each step's target must
  resolve to exactly one element; a step that types or selects needs a synthetic value; the
  acceptance cases judge the expected result; independent.py needs the version endpoint). A freeze
  with a critical gap is refused unless the owner overrides it with a written reason;
- **important**: the builder has to guess (it says so in BUILD-NOTES.md) and the copy may differ;
- **nice**: helps the builder, never decides a result.

`validate(data, files)` takes what `buildpack.manifest` produces (the evidence.json document and
the pack's files) and returns the result: a score per section, every gap (item, severity, why,
and the page and action that fix it), what is present, and `passed` (no critical gap). It is pure:
no database, no web framework, no clock. Keys the pack adds after validation (`completeness`,
`known_gaps`) are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

SPEC_VERSION = 1
SURFACES = ("web", "desktop", "mainframe")
SEVERITIES = ("critical", "important", "nice")
WEIGHT = {"critical": 3, "important": 2, "nice": 1}

SECTIONS = {
    "scenarios": "Scenarios",
    "screens": "Screens and controls",
    "navigation": "Navigation",
    "data": "Data",
    "access": "Roles and sign-in",
    "surface": "Surface specifics",
    "visuals": "Visuals and deployment",
}

# what an owner can do about a gap: the page (and its section) and the action
PAGES = {
    "scenarios": ("Scenarios", "/apps/{app}#scenarios"),
    "evidence": ("Uploaded evidence", "/apps/{app}#examples"),
    "flows": ("Recorded flows", "/apps/{app}#examples"),
    "recordings": ("Recordings", "/recordings"),
    "setup": ("Data and deployment", "/apps/{app}/setup#demo-target"),
    "git": ("Git repository", "/apps/{app}/setup#git"),
}


@dataclass(frozen=True)
class Item:
    id: str
    section: str
    title: str
    severity: str
    surfaces: tuple
    why: str
    page: str
    action: str
    capture_gap: str = ""  # set when the recorder or the pack builder cannot produce it yet


ALL = SURFACES

SPEC: tuple[Item, ...] = (
    # -- scenarios ------------------------------------------------------------------------------
    Item("scenario.present", "scenarios", "At least one business scenario", "critical", ALL,
         "The builder builds the scenarios and the release checks replay them; without one there is nothing to build "
         "or check.", "scenarios", "Add a business scenario."),
    Item("scenario.events", "scenarios", "Each scenario links recorded events", "critical", ALL,
         "A scenario is checked by replaying its recorded steps; one without events can never pass the replay.",
         "scenarios", "Link the recorded events that show this scenario."),
    Item("scenario.links_current", "scenarios", "Every linked event is still selected", "critical", ALL,
         "A linked event that is no longer selected is not in the pack, so its step cannot be rebuilt or replayed.",
         "flows", "Select the event again in the recorded flow, or unlink it from the scenario."),
    Item("scenario.expected", "scenarios", "Expected result of each scenario", "critical", ALL,
         "The acceptance cases judge the business outcome against it: a structure-only copy can show a wrong "
         "count, title or message and still look right.", "scenarios", "Write the expected result."),
    Item("scenario.starting", "scenarios", "Starting conditions of each scenario", "important", ALL,
         "The replay starts from a declared baseline (signed in, which records exist); without it the builder "
         "guesses the seed data.", "scenarios", "Write the starting conditions."),
    Item("scenario.actions", "scenarios", "Actions written by the owner", "important", ALL,
         "Recorded events say what was clicked, not why; the owner's actions tell the builder the intent of each "
         "step.", "scenarios", "Write the actions, one per line."),
    Item("scenario.failures", "scenarios", "Failure cases", "important", ALL,
         "What the app does with wrong input is not in a happy-path recording; without it the copy accepts "
         "everything.", "scenarios", "Write the failure cases, one per line."),
    Item("scenario.messages", "scenarios", "Validation and error messages the app shows", "important", ALL,
         "Required and numeric fields reject input with a message; the replay compares dialogs' messages word by "
         "word, so the builder needs the exact texts.", "scenarios",
         "Quote the messages in the failure cases (for example: Amount empty → \"Enter an amount\")."),
    # -- screens --------------------------------------------------------------------------------
    Item("screens.structure", "screens", "Screen structure (recorder contract 1.3)", "critical", ALL,
         "The replay compares every recorded screen's interactive elements by tag, id and classes after every step; "
         "without the structure the builder cannot know them.", "recordings",
         "Record the application again with recorder 1.3 or later at level full, and select its events."),
    Item("screens.referenced", "screens", "Every screen the scenarios touch", "critical", ALL,
         "Each screen a scenario's step lands on is compared with the rebuilt one; a missing screen fails that "
         "step.", "flows", "Select the events on that screen (or record it again at level full)."),
    Item("screens.controls", "screens", "Controls of each screen: kind, identifier or label, order", "critical",
         ALL, "The replay finds each step's target among the controls and counts every interactive element; a "
         "control without kind or identifier cannot be rebuilt to match.", "recordings",
         "Record the screen again at level full: the controls came without an identifier or label."),
    Item("screens.states", "screens", "Each referenced event resolves to a screen state", "critical", ALL,
         "After every step the replay compares the screen state the recording shows (enabled, read-only, required, "
         "visible, checked); an event with no state has nothing to compare against.", "flows",
         "Select the event's screen state, or record the step again at level full."),
    Item("screens.targets", "screens", "Each step's control or label", "critical", ALL,
         "The replay locates each step's target and it must match exactly one element; a click or entry without a "
         "control or label cannot be located.", "recordings",
         "Record the step again so it names its control, or link another event."),
    Item("screens.grids", "screens", "Grid columns", "important", ALL,
         "Tables and lists are rebuilt with their column headers; a grid without headers is guessed.", "evidence",
         "Attach a screenshot or layout notes of the grid (kind Screen or Utility)."),
    # -- navigation -----------------------------------------------------------------------------
    Item("navigation.entry", "navigation", "Entry screen", "critical", ALL,
         "The replay opens the app at the first step's screen; without it the copy has no known start.", "flows",
         "Select the first event of the scenario with its screen (the page or window it starts on)."),
    Item("navigation.transitions", "navigation", "Navigation between screens", "important", ("web", "desktop"),
         "The scenarios move between screens; the transitions say which action leads where.", "recordings",
         "Record the scenario again at level full, so the moves between screens are captured."),
    # -- data -----------------------------------------------------------------------------------
    Item("data.examples", "data", "Synthetic values for the steps that type or select", "critical", ALL,
         "A recording keeps no typed value: the replay types the bound synthetic values, and a step that needs "
         "one without it blocks the replay.", "scenarios",
         "Write synthetic example values, one per field (Field: value)."),
    Item("data.shapes", "data", "The shape of each example field", "important", ALL,
         "\"Field: value\" lines tell the builder which field takes which format; the replay compares value "
         "formats.", "scenarios", "Write each example as Field: value."),
    Item("data.backend", "data", "Data backend", "critical", ALL,
         "The demo creates its tables and seed data on this backend and resets to that seed.", "setup",
         "Choose the data backend."),
    Item("data.model", "data", "Data model (DDL)", "important", ALL,
         "Without DDL the builder infers tables from the screens, and the names will differ from the original.",
         "evidence", "Attach the DDL (.sql, kind Database) and mark it reviewed or synthetic."),
    # -- access ---------------------------------------------------------------------------------
    Item("access.signin", "access", "Roles and sign-in", "important", ALL,
         "The recording shows a sign-in; the builder needs which roles exist and which one each scenario starts "
         "as.", "scenarios", "Say in the starting conditions who is signed in, with which role."),
    # -- surface specifics ----------------------------------------------------------------------
    Item("web.routes", "surface", "Routes of the web screens", "critical", ("web",),
         "The replay maps each recorded page to the copy by its route; a screen without a route cannot be "
         "reached.", "recordings", "Record the web application again so each step carries its page."),
    Item("web.api", "surface", "API calls (HAR or API description)", "important", ("web",),
         "The front end calls an API; method, path template and field shapes come from a reviewed HAR or an API "
         "description.", "evidence", "Attach a HAR or OpenAPI file (kind API) and mark it reviewed.",
         capture_gap="The recorder keeps network evidence on the recording machine; the pack carries API calls "
                     "only from an attached HAR (redacted)."),
    Item("desktop.toolkit", "surface", "Desktop toolkit (WinForms, WPF, Java, …)", "important", ("desktop",),
         "Controls, their ControlTypes and keyboard behaviour depend on the toolkit; the builder should use the "
         "same one.", "recordings", "Record the application again with a recorder that reports its toolkit."),
    Item("desktop.windows", "surface", "Window and dialog hierarchy", "important", ("desktop",),
         "Which dialogs are modal and which window owns them changes how the copy behaves.", "evidence",
         "Attach layout notes (kind Utility) listing the windows and dialogs and which one opens which.",
         capture_gap="Recorder 1.3 sends one screen per window without its owner window or modality."),
    Item("desktop.menus", "surface", "Menus used by the scenarios", "important", ("desktop",),
         "A menu step needs its menu path; without menu controls the builder guesses the menu bar.", "recordings",
         "Record the menu steps again at level full."),
    Item("desktop.shortcuts", "surface", "Keyboard shortcuts", "important", ("desktop",),
         "A key step without its key cannot be rebuilt or replayed.", "recordings",
         "Record the key steps again (the key was not a named key)."),
    Item("mainframe.layout", "surface", "Field positions and lengths", "critical", ("mainframe",),
         "A 3270 screen is a fixed grid: each field's row, column and length is the screen.", "recordings",
         "Record the host screens again at level full."),
    Item("mainframe.attributes", "surface", "Field attributes (protected, hidden, numeric)", "important",
         ("mainframe",), "Protected, non-display and numeric fields behave differently on the terminal.",
         "recordings", "Record the host screens again at level full.",
         capture_gap="Recorder 1.3 sends only unprotected fields; protected output fields appear only as the "
                     "label of the field after them."),
    Item("mainframe.aid", "surface", "AID keys (Enter, PF keys, Clear)", "critical", ("mainframe",),
         "A host transaction moves on an AID key; a step without it cannot be replayed.", "recordings",
         "Record the AID steps again."),
    Item("mainframe.flow", "surface", "Transaction flow between host screens", "important", ("mainframe",),
         "Which AID key on which screen leads to which next screen is the transaction.", "recordings",
         "Record the transaction again at level full."),
    # -- visuals and deployment ---------------------------------------------------------------------
    Item("visuals.screens", "visuals", "Screenshots or layout notes", "important", ALL,
         "Structure says what is on a screen, not where; visual checks compare the copy's picture.", "evidence",
         "Attach screenshots (kind Screen) and mark them reviewed or synthetic."),
    Item("deploy.target", "visuals", "Deployment target", "critical", ALL,
         "The builder commits the deployment files of the target (OpenShift, or a local install for desktop).",
         "setup", "Set the target platform."),
    Item("deploy.version", "visuals", "Version endpoint", "critical", ("web", "mainframe"),
         "The release checks run only against a deployment that reports the built commit at the version "
         "endpoint.", "setup", "Set the target platform to web so the pack carries the version endpoint."),
    Item("deploy.repository", "visuals", "Demo repository", "nice", ALL,
         "The deployment files name the demo repository; without it the builder fills it in.", "git",
         "Connect the demo's Git repository."),
)
BY_ID = {item.id: item for item in SPEC}

TYPING = {"type", "input", "fill", "select", "change"}
TARGETED = TYPING | {"click", "dblclick", "double_click", "right_click", "check", "uncheck", "toggle", "submit"}
KEY_ACTIONS = {"key", "keydown", "keypress", "aid"}
MENU_ACTIONS = {"menu", "menu_open"}
SIGNIN = re.compile(r"\b(password|passwort|sign[ -]?in|log[ -]?in|logon|anmelden)\b", re.I)
WHO = re.compile(r"\b(role|signed in|logged in|sign in|log in|as (?:an? )?\w+|user|admin|clerk|manager)\b", re.I)
MESSAGE = re.compile(r"[\"“”'‘’«»]|\b(message|error|shows?|says?|warn\w*)\b", re.I)
FIELD_VALUE = re.compile(r"^[^:=]{1,60}\s*[:=]\s*\S")
LINK_GONE = re.compile(r"^Linked event (\S+) is no longer selected")


def _screens(data):
    return {sc["screen"]: sc for sc in data.get("screens") or [] if isinstance(sc, dict) and sc.get("screen")}


def _state_events(screens):
    return {e: name for name, sc in screens.items() for st in sc.get("states") or [] for e in st.get("events") or []}


def _controls(screens, names=None):
    for name, sc in screens.items():
        if names is not None and name not in names:
            continue
        for st in sc.get("states") or []:
            for c in st.get("controls") or []:
                yield sc, c


def _is_route(screen):
    return isinstance(screen, str) and (screen.startswith("/") or re.match(r"^https?://", screen) is not None)


class _Checker:
    def __init__(self, data, files):
        self.data, self.files = data, files or {}
        target = data.get("target") or {}
        self.surface = target.get("kind") if target.get("kind") in SURFACES else (data.get("app") or {}).get("surface")
        if self.surface not in SURFACES:
            self.surface = "web"
        self.app_id = (data.get("app") or {}).get("id")
        self.scenarios = [s for s in data.get("scenarios") or [] if isinstance(s, dict)]
        self.screens = _screens(data)
        self.state_of = _state_events(self.screens)
        self.events = [e for s in self.scenarios for e in s.get("events") or []]
        self.touched = {e["screen"] for e in self.events if e.get("screen")}
        self.attachments = [a for a in data.get("attachments") or [] if isinstance(a, dict)]
        self.gaps, self.present, self.na = [], [], []

    # each check returns None (present), a list of details (missing), or "n/a"
    def gap(self, item_id, details):
        item = BY_ID[item_id]
        label, href = PAGES[item.page]
        out = {"item": item.id, "title": item.title, "section": item.section, "severity": item.severity,
               "why": item.why, "fix": {"page": label, "href": href.format(app=self.app_id or ""),
                                        "action": item.action},
               "details": [d for d in details if d][:20]}
        if item.capture_gap:
            out["capture_gap"] = item.capture_gap
        self.gaps.append(out)

    def run(self):
        for item in SPEC:
            if self.surface not in item.surfaces:
                continue
            result = getattr(self, "check_" + item.id.replace(".", "_"))()
            if result == "n/a":
                self.na.append(item.id)
            elif result:
                self.gap(item.id, result)
            else:
                self.present.append(item.id)

    def each(self, test, text):
        return [text.format(name=s.get("name") or "(unnamed)") for s in self.scenarios if not test(s)]

    # -- scenarios -----------------------------------------------------------------------------
    def check_scenario_present(self):
        return [] if self.scenarios else ["The package has no business scenario."]

    def check_scenario_events(self):
        return self.each(lambda s: s.get("events"), "Scenario \"{name}\" links no recorded event.")

    def check_scenario_links_current(self):
        out = []
        for s in self.scenarios:
            for q in s.get("unresolved_questions") or []:
                m = LINK_GONE.match(q) if isinstance(q, str) else None
                if m:
                    out.append(f"Scenario \"{s.get('name')}\": linked event {m[1]} is no longer selected.")
        return out

    def check_scenario_expected(self):
        return self.each(lambda s: (s.get("expected_result") or "").strip(),
                         "Scenario \"{name}\" does not state its expected result.")

    def check_scenario_starting(self):
        return self.each(lambda s: (s.get("starting_conditions") or "").strip(),
                         "Scenario \"{name}\" has no starting conditions.")

    def check_scenario_actions(self):
        return self.each(lambda s: s.get("actions"), "Scenario \"{name}\" has no written actions.")

    def check_scenario_failures(self):
        return self.each(lambda s: s.get("failure_cases"), "Scenario \"{name}\" has no failure cases.")

    def check_scenario_messages(self):
        lines = [f for s in self.scenarios for f in s.get("failure_cases") or []]
        validated = any(c.get("required") or c.get("numeric") for _, c in _controls(self.screens, self.touched))
        if any(MESSAGE.search(f) for f in lines):
            return None
        if not lines and not validated:
            return "n/a" if self.screens else ["No failure case says which message the app shows."]
        why = "Required or numeric fields are on the scenarios' screens, but " if validated else ""
        return [why + "no failure case quotes the message the app shows."]

    # -- screens -------------------------------------------------------------------------------
    def check_screens_structure(self):
        return [] if self.screens else ["The package has no screen structure: the recording was made before "
                                        "contract 1.3, or not at level full."]

    def check_screens_referenced(self):
        if not self.screens:
            return "n/a" if not self.touched else [f"Screen `{s}` has no structure." for s in sorted(self.touched)]
        return [f"Screen `{s}` is touched by a scenario but has no structure in the package."
                for s in sorted(self.touched) if s not in self.screens]

    def check_screens_controls(self):
        out = []
        for name in sorted(self.touched & set(self.screens)):
            sc = self.screens[name]
            if not any(st.get("controls") for st in sc.get("states") or []):
                out.append(f"Screen `{name}` has no controls in any state.")
                continue
            for _, c in _controls({name: sc}):
                if not c.get("kind") or not (any(c.get(k) for k in ("tag", "id", "name", "label", "classes", "type"))
                                             or "row" in c or c["kind"] not in ("control", "")):
                    out.append(f"A control on `{name}` has no kind or identifier.")
                    break
        return out

    def check_screens_states(self):
        if not self.screens:
            return "n/a"
        return [f"Event {e.get('recording_id')} #{e.get('seq')} ({e.get('action')}) resolves to no screen state."
                for e in self.events if e.get("event_id") not in self.state_of]

    def check_screens_targets(self):
        out = []
        for e in self.events:
            if e.get("action") in TARGETED and not (e.get("control") or e.get("label")):
                out.append(f"Event {e.get('recording_id')} #{e.get('seq')} ({e['action']}) names no control or label.")
            elif not (e.get("screen") or e.get("control")):
                out.append(f"Event {e.get('recording_id')} #{e.get('seq')} has no recorded screen or control.")
        return out

    def check_screens_grids(self):
        grids = [(name, g) for name, sc in self.screens.items() if name in self.touched
                 for st in sc.get("states") or [] for g in st.get("grids") or []]
        if not grids:
            return "n/a"
        return sorted({f"A grid on `{name}` has no column headers." for name, g in grids
                       if not any(g.get("columns") or [])})

    # -- navigation ----------------------------------------------------------------------------
    def check_navigation_entry(self):
        return self.each(lambda s: not s.get("events") or s["events"][0].get("screen"),
                         "The first event of scenario \"{name}\" has no screen to start on.")

    def check_navigation_transitions(self):
        if len(self.touched) < 2:
            return "n/a"
        if not self.screens:
            return ["The scenarios move between screens, but no transition was recorded."]
        out = []
        for s in self.scenarios:
            seq = [e.get("screen") for e in s.get("events") or [] if e.get("screen")]
            for a, b in zip(seq, seq[1:]):
                if a != b and a in self.screens and not any(
                        t.get("to") == b for t in self.screens[a].get("transitions") or []):
                    out.append(f"Scenario \"{s.get('name')}\" moves from `{a}` to `{b}`, but no transition shows how.")
        return sorted(set(out))

    # -- data ----------------------------------------------------------------------------------
    def check_data_examples(self):
        out = []
        for s in self.scenarios:
            typed = [e for e in s.get("events") or [] if e.get("action") in TYPING]
            if typed and not s.get("example_values"):
                out.append(f"Scenario \"{s.get('name')}\" types or selects in {len(typed)} step"
                           f"{'s' if len(typed) != 1 else ''} but gives no synthetic value.")
        return out

    def check_data_shapes(self):
        lines = [(s.get("name"), v) for s in self.scenarios for v in s.get("example_values") or []]
        if not lines:
            return "n/a"
        return sorted({f"Scenario \"{n}\": example \"{v[:40]}\" does not name its field (Field: value)."
                       for n, v in lines if not FIELD_VALUE.match(v)})

    def check_data_backend(self):
        b = (self.data.get("target") or {}).get("data_backend") or {}
        return [] if b.get("engine") else ["The package names no data backend."]

    def check_data_model(self):
        model = (self.data.get("target") or {}).get("data_model") or {}
        return [] if model.get("tables") else ["No reviewed DDL is attached: the builder infers the tables."]

    # -- access --------------------------------------------------------------------------------
    def check_access_signin(self):
        seen = any(c.get("type") == "password" or SIGNIN.search(str(c.get("label") or c.get("id") or ""))
                   for _, c in _controls(self.screens))
        seen = seen or any(SIGNIN.search(str(e.get("label") or e.get("screen") or "")) for e in self.events)
        if not seen:
            return "n/a"
        told = any(WHO.search(s.get("starting_conditions") or "") for s in self.scenarios)
        return [] if told else ["The recording shows a sign-in, but no scenario says who is signed in."]

    # -- surface -------------------------------------------------------------------------------
    def check_web_routes(self):
        return sorted({f"Screen `{e['screen']}` is not a route." for e in self.events
                       if e.get("screen") and not _is_route(e["screen"])} |
                      {f"Event {e.get('recording_id')} #{e.get('seq')} has no page." for e in self.events
                       if not e.get("screen")})

    def check_web_api(self):
        found = any(a.get("kind") == "API" or str(a.get("path", "")).endswith(".har") for a in self.attachments)
        return [] if found else ["No reviewed HAR or API description is attached."]

    def check_desktop_toolkit(self):
        return [] if (self.data.get("app") or {}).get("toolkit") else ["The package does not name the toolkit."]

    def check_desktop_windows(self):
        notes = [a for a in self.attachments if a.get("kind") in ("Utility", "Screen")]
        return [] if notes else ["No layout notes or screenshots describe the windows and dialogs."]

    def check_desktop_menus(self):
        menu_steps = [e for e in self.events if e.get("action") in MENU_ACTIONS]
        if not menu_steps:
            return "n/a"
        has = any("menu" in str(c.get("kind", "")).lower() for _, c in _controls(self.screens))
        return [] if has else [f"{len(menu_steps)} menu step(s), but no menu control in the screen structure."]

    def check_desktop_shortcuts(self):
        keys = [e for e in self.events if e.get("action") in KEY_ACTIONS]
        if not keys:
            return "n/a"
        return [f"Event {e.get('recording_id')} #{e.get('seq')} is a key step without its key."
                for e in keys if not e.get("key")]

    def mainframe_fields(self):
        return [c for sc, c in _controls(self.screens, self.touched) if sc.get("surface", "mainframe") == "mainframe"]

    def check_mainframe_layout(self):
        if not self.screens:
            return ["The package has no host screen layout."]
        out = []
        for name in sorted(self.touched & set(self.screens)):
            fields = [c for _, c in _controls({name: self.screens[name]})]
            if not fields or any(not all(k in c for k in ("row", "col", "len")) for c in fields):
                out.append(f"Host screen `{name}` has fields without row, column and length.")
        return out

    def check_mainframe_attributes(self):
        # the recorder sends numeric, non-display and intensified with every input field it sends;
        # protected fields are not sent (only their text as the next field's label): a capture gap
        fields = self.mainframe_fields()
        return [] if fields else ["No host field is recorded."]

    def check_mainframe_aid(self):
        keys = [e for e in self.events if e.get("action") in KEY_ACTIONS]
        if not keys:
            return ["No scenario step presses an AID key." if self.events else "No recorded step."]
        return [f"Event {e.get('recording_id')} #{e.get('seq')} sends an AID without its key."
                for e in keys if not e.get("key")]

    def check_mainframe_flow(self):
        return self.check_navigation_transitions()

    # -- visuals and deployment ------------------------------------------------------------------
    def check_visuals_screens(self):
        found = any(a.get("kind") == "Screen" for a in self.attachments)
        return [] if found else ["No reviewed screenshot or layout note is attached."]

    def check_deploy_target(self):
        deploy = (self.data.get("target") or {}).get("deploy") or {}
        if deploy.get("kind") == "local":
            return []
        if deploy.get("kind") == "openshift":
            missing = [p for p in deploy.get("files") or [] if p not in self.files]
            return [f"Deployment file {p} is not in the package." for p in missing] if deploy.get("files") \
                else ["The OpenShift deployment lists no files."]
        return ["The package names no deployment target."]

    def check_deploy_version(self):
        path = (self.data.get("target") or {}).get("version_endpoint")
        return [] if path else ["The package does not name the version endpoint."]

    def check_deploy_repository(self):
        return [] if (self.data.get("app") or {}).get("git_url") else ["The demo repository is not set."]


def validate(data: dict, files: dict | None = None) -> dict:
    """The completeness of a draft pack: {"spec_version", "surface", "passed", "score",
    "sections": [{"id", "title", "score", "present", "missing", "not_applicable"}], "gaps": [...],
    "counts": {severity: n}}. `passed` is False when any critical item is missing."""
    checker = _Checker(data or {}, files)
    checker.run()
    missing = {g["item"] for g in checker.gaps}
    sections = []
    total_have = total_all = 0
    for sid, title in SECTIONS.items():
        items = [i for i in SPEC if i.section == sid and checker.surface in i.surfaces and i.id not in checker.na]
        have = sum(WEIGHT[i.severity] for i in items if i.id not in missing)
        weight = sum(WEIGHT[i.severity] for i in items)
        total_have, total_all = total_have + have, total_all + weight
        sections.append({"id": sid, "title": title, "score": round(100 * have / weight) if weight else 100,
                         "present": [i.id for i in items if i.id not in missing],
                         "missing": [i.id for i in items if i.id in missing],
                         "not_applicable": [i for i in checker.na if BY_ID[i].section == sid]})
    order = {s: n for n, s in enumerate(SEVERITIES)}
    gaps = sorted(checker.gaps, key=lambda g: (order[g["severity"]], list(BY_ID).index(g["item"])))
    counts = {s: sum(1 for g in gaps if g["severity"] == s) for s in SEVERITIES}
    return {"spec_version": SPEC_VERSION, "surface": checker.surface, "passed": counts["critical"] == 0,
            "score": round(100 * total_have / total_all) if total_all else 100, "sections": sections,
            "gaps": gaps, "counts": counts}


def critical(result) -> list[dict]:
    return [g for g in result["gaps"] if g["severity"] == "critical"]


def title_of(item_id) -> str:
    return BY_ID[item_id].title if item_id in BY_ID else item_id


def markdown(result, known_gaps=None) -> list[str]:
    """The "Completeness" section of BUILD_TASK.md (and "Known gaps" when the owner froze the
    package over critical gaps)."""
    out = ["## Completeness", "",
           f"Checked against the package completeness spec v{result['spec_version']} for a {result['surface']} "
           f"application: {result['score']}% "
           + ("(every critical item is present)." if result["passed"] else "(critical items are missing)."),
           "Where an item is missing, say in BUILD-NOTES.md what you assumed instead.", ""]
    for section in result["sections"]:
        if not section["present"] and not section["missing"]:
            continue
        out.append(f"**{SECTIONS[section['id']]}** ({section['score']}%)")
        out.append("")
        out += [f"- present: {title_of(i)}" for i in section["present"]]
        for g in (g for g in result["gaps"] if g["section"] == section["id"]):
            out.append(f"- missing ({g['severity']}): {g['title']}. {g['why']}")
            out += [f"  - {d}" for d in g["details"][:5]]
        out.append("")
    if known_gaps:
        out += ["## Known gaps", "",
                "The owner froze this package with critical items missing, for this reason:", "",
                "> " + known_gaps["reason"].replace("\n", "\n> "), ""]
        out += [f"- {g['title']}" for g in known_gaps["gaps"]] + [""]
    return out


def summary(result) -> dict:
    """What evidence.json carries of the result: compact, without the owner's fix actions."""
    return {"spec_version": result["spec_version"], "surface": result["surface"], "passed": result["passed"],
            "score": result["score"],
            "sections": {s["id"]: s["score"] for s in result["sections"]},
            "missing": [{"item": g["item"], "severity": g["severity"], "details": g["details"]}
                        for g in result["gaps"]]}
