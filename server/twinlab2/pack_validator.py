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

SPEC_VERSION = 2  # 2: bindings, per-state baselines, interfaces, Twin App label, evidence levels
LEVELS = ("documented", "observed", "owner-confirmed", "replay-tested")
COVERED = ("observed", "owner-confirmed", "replay-tested")
SURFACES = ("web", "desktop", "mainframe")
SEVERITIES = ("critical", "important", "nice")
WEIGHT = {"critical": 3, "important": 2, "nice": 1}

SECTIONS = {
    "scenarios": "Scenarios",
    "behaviour": "Business behaviour beyond the happy path",
    "screens": "Screens and controls",
    "navigation": "Navigation",
    "data": "Data",
    "access": "Roles and sign-in",
    "surface": "Surface specifics",
    "interfaces": "Interfaces with other applications",
    "visuals": "Visuals and deployment",
}

# how a release is verified, per dimension: each item counts toward one (see `dimension_of`)
DIMENSIONS = {
    "behavioural": "Behavioural (scenarios, outcomes, failure paths)",
    "structural": "Structural (screens, controls, states, navigation)",
    "visual": "Visual (appearance against approved baselines)",
    "deployment": "Deployment (data, target, version endpoint)",
}

# what an owner can do about a gap: the page (and its section) and the action
PAGES = {
    "scenarios": ("Scenarios", "/apps/{app}#scenarios"),
    "evidence": ("Uploaded evidence", "/apps/{app}#examples"),
    "flows": ("Recorded flows", "/apps/{app}#examples"),
    "recordings": ("Recordings", "/recordings"),
    "setup": ("Data and deployment", "/apps/{app}/setup#demo-target"),
    "git": ("Git repository", "/apps/{app}/setup#git"),
    "interfaces": ("Interfaces", "/apps/{app}/build-pack/preview#interfaces"),
    "baselines": ("Visual baselines", "/apps/{app}/build-pack/preview#baselines"),
    "package": ("Package preview", "/apps/{app}/build-pack/preview"),
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
    # -- business behaviour beyond the happy path ----------------------------------------------------
    # None of these is checked by the replay unconditionally (acceptance cases such as a reload or a
    # fresh context are declared per recording), so they are important, not critical.
    Item("behaviour.invalid_input", "behaviour", "Invalid input", "important", ALL,
         "A recording shows valid entries; what the app refuses (empty, wrong format, out of range) is invisible "
         "unless written down.", "scenarios", "Add failure cases for invalid input, with what the app shows."),
    Item("behaviour.permissions", "behaviour", "Permissions", "important", ALL,
         "Who may see or change what is not in a recording made by one person.", "scenarios",
         "Say which roles may do each scenario and what others see (read-only, hidden, refused)."),
    Item("behaviour.empty_results", "behaviour", "Empty results", "important", ALL,
         "Lists and searches were recorded with rows; the copy needs to know what an empty result shows.",
         "scenarios", "Add a failure case or scenario for a search or list with no results."),
    Item("behaviour.retries", "behaviour", "Retries and unavailable services", "important", ALL,
         "What happens when a save times out or a service is down decides whether a retry duplicates data.",
         "scenarios", "Say what the app does when saving fails or times out, and whether it can be retried."),
    Item("behaviour.duplicates", "behaviour", "Duplicates", "important", ALL,
         "Creating the same record twice is refused, merged or allowed; the recording shows one creation.",
         "scenarios", "Add a failure case for creating a record that already exists."),
    Item("behaviour.cancellation", "behaviour", "Cancellation", "important", ALL,
         "The screens have Cancel or Close; whether cancelling discards, keeps a draft or asks is not recorded.",
         "scenarios", "Say what Cancel or Close does on the scenario's screens."),
    Item("behaviour.persistence", "behaviour", "Persistence", "important", ALL,
         "What was saved must still be there after a reload or a restart; acceptance cases can check a reload.",
         "scenarios", "Say in the expected result what is still shown after a reload or restart."),
    Item("behaviour.unrecorded", "behaviour", "Failure cases or precondition rules", "important", ALL,
         "Off the recorded path the builder follows the pack's defaults (a direct URL to a later page returns to "
         "the start, keys not listed do nothing); without the owner's failure cases or precondition rules the "
         "copy behaves however the builder guessed (a blind rebuild showed a form the original never shows "
         "without a lookup).", "scenarios",
         "Write the failure cases, and the precondition rules: which pages or screens are reached only from "
         "another (for example \"the invoice form is only reached from a found order\"), what a direct URL "
         "shows, which keys do something."),
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
    Item("screens.withheld", "screens", "Identifiers withheld as possible data", "important", ALL,
         "Identifiers, classes and labels that could be data (with a digit, an @, too long) are left out of the "
         "structure; the replay compares by them, so a stable id such as `button1` or `addressLine2` is lost.",
         "evidence", "Check those screens in a screenshot, and name the withheld controls in layout notes "
                     "(kind Utility).",
         capture_gap="The recorder and the portal drop every identifier with a digit and keep only a count "
                     "(`withheld`), never the names: twinlab2_contract/structure.py `ident()`/`plain()`, "
                     "twinlab2_recorder/workstation/flow.py `ident()`/`plain()`, workstation/structure.py."),
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
    Item("data.bindings", "data", "Each typing or selecting step bound to a fixture field", "important", ALL,
         "One example line does not say which step types it: the replay types the value bound to each step, and a "
         "step without a binding is guessed or blocks the replay.", "scenarios",
         "Bind each typing or selecting step to one of the scenario's example fields (or a shared identifier)."),
    Item("data.binding_types", "data", "Bound values fit their controls", "critical", ALL,
         "The replay types the bound value into the recorded control: a number in an email field, or an option "
         "that is not in the list, fails the step on the original as well as on the copy.", "scenarios",
         "Change the example value (or bind another field) so that it fits the control."),
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
    Item("mainframe.title", "surface", "Title line of each host screen", "critical", ("mainframe",),
         "The replay names a 3270 screen by all the protected text of its first non-empty row and compares it "
         "exactly; a screen known only by its layout id gives the builder no title to put there.", "recordings",
         "Record the host screens again so each has a title line (the first row with protected text)."),
    Item("mainframe.attributes", "surface", "Field attributes (protected, hidden, numeric)", "important",
         ("mainframe",), "Protected, non-display and numeric fields behave differently on the terminal.",
         "recordings", "Record the host screens again at level full."),
    Item("mainframe.protected", "surface", "Protected text positions (labels, messages, key line)", "important",
         ("mainframe",), "Where labels, output fields, the message line and the key line sit is the look of a "
         "3270 screen; without them the builder places them and the copy looks different.", "recordings",
         "Record the host screens again with a recorder that sends protected fields (contract 1.4).",
         capture_gap="Recorder contract 1.3 sends only unprotected fields; protected output fields appear only as "
                     "the label of the field after them. From contract 1.4 the recorder sends every protected "
                     "field (the state's `texts`: row, column, length, text or shape) and the pack renders a "
                     "layout table."),
    Item("mainframe.aid", "surface", "AID keys (Enter, PF keys, Clear)", "critical", ("mainframe",),
         "A host transaction moves on an AID key; a step without it cannot be replayed.", "recordings",
         "Record the AID steps again."),
    Item("mainframe.flow", "surface", "Transaction flow between host screens", "important", ("mainframe",),
         "Which AID key on which screen leads to which next screen is the transaction.", "recordings",
         "Record the transaction again at level full."),
    # -- interfaces with other applications -----------------------------------------------------
    Item("interfaces.identifiers", "interfaces", "Shared identifiers come from a producer", "critical", ALL,
         "A value one application types that another produced (an order number typed in Billing) must be one the "
         "producer makes, with the same synthetic value in both packages, or the two copies never meet.",
         "interfaces", "Name the producer of the identifier, add it to the producer's scenario, or unbind it."),
    Item("interfaces.order", "interfaces", "Launch and reset order", "important", ALL,
         "Applications recorded together are started producers first and reset consumers first; the replay of a "
         "cross-application recording needs every peer up.", "interfaces",
         "Check the launch and reset order of the interface contract."),
    Item("interfaces.peer_down", "interfaces", "What happens when a peer is down", "important", ALL,
         "A consumer whose producer does not answer must fail in a known way (message, retry, queued work), not "
         "however the builder guessed.", "interfaces",
         "Write, for each relationship, what the consumer does when the producer is down."),
    Item("interfaces.schemas", "interfaces", "API and file schemas between the applications", "important", ALL,
         "An API call or a file between two applications needs its method and path, or its columns, on both sides.",
         "interfaces", "Give the method, path and fields of each API relationship and the columns of each file."),
    # -- visuals and deployment ---------------------------------------------------------------------
    Item("deploy.twin_label", "visuals", "The \"AI generated Twin App\" label", "critical", ALL,
         "Every rebuilt application shows that it is an AI generated twin, on every screen, so nobody takes it for "
         "the original; Verify refuses a release without it.", "package",
         "Freeze the package again: the package builder adds the label requirement (contract 0.3)."),
    Item("visuals.screens", "visuals", "Screenshots or layout notes", "important", ALL,
         "Structure says what is on a screen, not where; visual checks compare the copy's picture.", "evidence",
         "Attach screenshots (kind Screen) and mark them reviewed or synthetic."),
    Item("visuals.per_screen", "visuals", "An approved baseline of every screen state the scenarios reach", "important",
         ALL, "The visual comparison (replay/visual.py) needs a baseline per compared state, at a known viewport, theme "
         "and scale; pictures of one screen say nothing about the others.", "baselines",
         "Approve a baseline for each screen state (from the recorder's pictures or a Screen attachment)."),
    Item("visuals.states", "visuals", "Baselines of special states (disabled, selected, error)", "important", ALL,
         "A disabled button, a selected row or an error message looks different; a baseline of another state of the "
         "same screen does not show it.", "baselines",
         "Approve a baseline for each recorded state that shows disabled, selected or error controls."),
    Item("visuals.style", "visuals", "Fonts, colours and spacing", "nice", ALL,
         "Without them the copy looks generic even when its structure matches.", "evidence",
         "Attach style notes (kind Utility, title with \"style\"), or name fonts and colours in a scenario's notes."),
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

# What shows an item when it is present, before the owner confirms it or Verify tests it:
# "recording" items are observed in the recording, "prose" items are only documented (a keyword
# or a written line), "setting" and "approval" items are the owner's own choice or approval.
# Items not listed are "prose".
BASIS = {
    "scenario.events": "recording", "scenario.links_current": "recording", "screens.structure": "recording",
    "screens.referenced": "recording", "screens.controls": "recording", "screens.states": "recording",
    "screens.targets": "recording", "screens.grids": "recording", "screens.withheld": "recording",
    "navigation.entry": "recording", "navigation.transitions": "recording", "web.routes": "recording",
    "desktop.toolkit": "recording", "desktop.menus": "recording", "desktop.shortcuts": "recording",
    "mainframe.layout": "recording", "mainframe.title": "recording", "mainframe.attributes": "recording",
    "mainframe.protected": "recording", "mainframe.aid": "recording", "mainframe.flow": "recording",
    "data.bindings": "approval", "data.binding_types": "approval", "data.model": "approval", "web.api": "approval",
    "desktop.windows": "approval", "visuals.screens": "approval", "visuals.per_screen": "approval",
    "visuals.states": "approval", "data.backend": "setting", "deploy.target": "setting",
    "deploy.version": "setting", "deploy.repository": "setting", "deploy.twin_label": "setting",
    "interfaces.identifiers": "recording", "interfaces.order": "recording", "interfaces.schemas": "recording",
}
START = {"recording": "observed", "prose": "documented", "setting": "owner-confirmed", "approval": "owner-confirmed"}
# what a passed verification dimension tests of a present item (twinlab2.independent.dimensions)
TESTED_BY = {
    "behavioural": {"scenario.events", "scenario.expected", "data.examples", "data.bindings", "data.binding_types",
                    "interfaces.identifiers"},
    "structural": {"screens.structure", "screens.referenced", "screens.controls", "screens.states", "screens.targets",
                   "navigation.entry", "navigation.transitions", "web.routes", "mainframe.layout", "mainframe.title",
                   "mainframe.aid", "desktop.menus", "desktop.shortcuts", "deploy.twin_label"},
    "visual": {"visuals.per_screen", "visuals.states"},
    "deployment": {"deploy.target", "deploy.version", "data.backend"},
}

TYPING = {"type", "input", "fill", "select", "change"}
TARGETED = TYPING | {"click", "dblclick", "double_click", "right_click", "check", "uncheck", "toggle", "submit"}
KEY_ACTIONS = {"key", "keydown", "keypress", "aid"}
MENU_ACTIONS = {"menu", "menu_open"}
SIGNIN = re.compile(r"\b(password|passwort|sign[ -]?in|log[ -]?in|logon|anmelden)\b", re.I)
WHO = re.compile(r"\b(role|signed in|logged in|sign in|log in|as (?:an? )?\w+|user|admin|clerk|manager)\b", re.I)
MESSAGE = re.compile(r"[\"“”'‘’«»]|\b(message|error|shows?|says?|warn\w*)\b", re.I)
FIELD_VALUE = re.compile(r"^[^:=]{1,60}\s*[:=]\s*\S")
STYLE = re.compile(r"\b(style|font|colou?r|spacing|theme|palette|css)\b", re.I)
CANCEL = re.compile(r"\b(cancel|close|abbrechen|back)\b", re.I)
BEHAVIOUR = {  # spec item -> words that show a scenario covers it
    "behaviour.invalid_input": r"invalid|wrong|empty|missing|required|refus|reject|format|too (long|short|large|small)"
                               r"|out of range|not a (number|date)|without",
    "behaviour.permissions": r"permission|not allowed|forbidden|denied|unauthori[sz]|\brole|read-only|only (an? |the )?"
                             r"\w+s? (can|may)",
    "behaviour.empty_results": r"no results?|empty|nothing (found|shown)|none found|no matches|zero rows|no \w+ (found|shown)",
    "behaviour.retries": r"retry|retries|again|time ?out|timed out|unavailable|offline|try later|resubmit|connection",
    "behaviour.duplicates": r"duplicate|already exists|twice|unique|same (number|name|id)",
    "behaviour.cancellation": r"cancel|abort|discard|close without|back without",
    "behaviour.persistence": r"reload|refresh|persist|restart|reopen|still (there|shown|listed)|after (a )?(log ?out|sign ?out)",
}
# words that state a precondition or what happens off the recorded path
PRECONDITION = re.compile(r"only (be )?(reached|opened|shown|available)|direct(ly)? (url|link|address)|by url"
                          r"|without (a |an |the |first )?\w+ (first|before)|precondition"
                          r"|unless|requires? (a |an |the )?\w+ first|(key|pf\d+|pa\d|clear)s? .{0,30}"
                          r"(nothing|ignored|no effect)|sends? you back|returns? to|redirect", re.I)
TITLE_ID = re.compile(r"^scr:[0-9a-f]+$")
VIEWPORT = re.compile(r"^\d{2,5}x\d{2,5}$")
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
        self.levels = {}  # item -> the evidence level a check decided itself (else from BASIS)
        self.interfaces = [c for c in data.get("interfaces") or [] if isinstance(c, dict)]
        self.baselines = [b for b in data.get("visual_baselines") or [] if isinstance(b, dict)]
        self.controls_of = {}
        for sc in self.screens.values():
            for st in sc.get("states") or []:
                for e in st.get("events") or []:
                    self.controls_of[e] = st.get("controls") or []

    # each check returns None (present), a list of details (missing), or "n/a"
    def gap(self, item_id, details):
        item = BY_ID[item_id]
        label, href = PAGES[item.page]
        out = {"item": item.id, "title": item.title, "section": item.section, "dimension": dimension_of(item),
               "severity": item.severity,
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

    # -- business behaviour ------------------------------------------------------------------------
    def text(self):
        return " ".join(" ".join([s.get("name") or "", s.get("starting_conditions") or "", s.get("expected_result") or "",
                                  s.get("notes") or "", *(s.get("actions") or []), *(s.get("failure_cases") or [])])
                        for s in self.scenarios)

    def typed(self):
        return any(e.get("action") in TYPING for e in self.events)

    def behaviour(self, item, applies, what):
        if not applies:
            return "n/a"
        if re.search(BEHAVIOUR[item], self.text(), re.I):
            return []
        return [f"No scenario or failure case says {what}."]

    def check_behaviour_invalid_input(self):
        return self.behaviour("behaviour.invalid_input", self.typed(), "what happens with invalid input")

    def check_behaviour_permissions(self):
        return self.behaviour("behaviour.permissions", self.signin_seen() or re.search(r"\brole", self.text(), re.I),
                              "who may do it and what others see")

    def check_behaviour_empty_results(self):
        grids = any(st.get("grids") for n, sc in self.screens.items() if n in self.touched for st in sc.get("states") or [])
        search = any(re.search(r"search|find|filter|such", str(e.get("label") or ""), re.I) for e in self.events)
        return self.behaviour("behaviour.empty_results", grids or search, "what an empty list or search shows")

    def check_behaviour_retries(self):
        return self.behaviour("behaviour.retries", self.typed(), "what happens when saving fails or times out")

    def check_behaviour_duplicates(self):
        return self.behaviour("behaviour.duplicates", self.typed(), "what happens with a record that already exists")

    def check_behaviour_cancellation(self):
        controls = any(CANCEL.search(str(c.get("label") or c.get("id") or "")) for _, c in _controls(self.screens, self.touched))
        return self.behaviour("behaviour.cancellation", controls, "what Cancel or Close does")

    def check_behaviour_persistence(self):
        return self.behaviour("behaviour.persistence", self.typed(), "what is still there after a reload or restart")

    def check_behaviour_unrecorded(self):
        if not self.scenarios:
            return "n/a"
        if any(s.get("failure_cases") for s in self.scenarios) or PRECONDITION.search(self.text()):
            return []
        return ["No scenario has failure cases or a precondition rule (which page or screen is reached only from "
                "another, what a direct URL or an unlisted key does)."]

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

    def check_screens_withheld(self):
        out = []
        for name in sorted(self.touched & set(self.screens)):
            for i, st in enumerate(self.screens[name].get("states") or [], 1):
                if st.get("withheld"):
                    out.append(f"Screen `{name}`, state {i}: {st['withheld']} identifier(s), class(es) or label(s) "
                               "withheld as possible data.")
        return out

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

    def typed_events(self, s):
        return [e for e in s.get("events") or [] if e.get("action") in TYPING]

    def check_data_bindings(self):
        if not any(self.typed_events(s) for s in self.scenarios):
            return "n/a"
        out = []
        for s in self.scenarios:
            bound = {b.get("event_id") for b in s.get("bindings") or []}
            fields = {f.get("field") for f in s.get("fixture_fields") or []}
            for e in self.typed_events(s):
                if e.get("event_id") not in bound:
                    out.append(f"Scenario \"{s.get('name')}\": event {e.get('recording_id')} #{e.get('seq')} "
                               f"({e.get('action')}{' ' + e['label'] if e.get('label') else ''}) is bound to no "
                               "fixture field.")
            for b in s.get("bindings") or []:
                if b.get("field") and b["field"] not in fields:
                    out.append(f"Scenario \"{s.get('name')}\": the binding of event {b.get('event_id')} names field "
                               f"\"{b['field']}\", which no example line gives.")
        return out

    def check_data_binding_types(self):
        from twinlab2_contract import package

        bound = [(s, b) for s in self.scenarios for b in s.get("bindings") or [] if b.get("field") and "value" in b]
        if not bound:
            return "n/a"
        out = []
        for s, b in bound:
            event = next((e for e in s.get("events") or [] if e.get("event_id") == b.get("event_id")), {})
            target = event.get("label") or ""
            control = next((c for c in self.controls_of.get(b.get("event_id")) or []
                            if target and target in (c.get("id"), c.get("name"), c.get("label"))), None)
            if control is None:
                continue
            why = package.incompatible({"value": b["value"]}, control)
            if why:
                out.append(f"Scenario \"{s.get('name')}\": field {b['field']} at event #{event.get('seq')}: {why}.")
        return out

    # -- interfaces ------------------------------------------------------------------------------
    def contract_level(self):
        return "owner-confirmed" if any(c.get("edited") for c in self.interfaces) else "observed"

    def check_interfaces_identifiers(self):
        from twinlab2_contract import package

        idents = {i.get("name"): (c, i) for c in self.interfaces for i in c.get("identifiers") or []}
        used = [(s, b) for s in self.scenarios for b in s.get("bindings") or []
                if b.get("identifier") or package.slug(b.get("field")) in idents]
        if not self.interfaces and not used:
            return "n/a"
        self.levels["interfaces.identifiers"] = self.contract_level()
        out = []
        for s, b in used:
            name = b.get("identifier") or package.slug(b.get("field"))
            if name not in idents:
                out.append(f"Scenario \"{s.get('name')}\" binds shared identifier `{name}`, which no interface "
                           "contract lists.")
                continue
            c, ident = idents[name]
            producer = {a.get("id"): a.get("name") for a in c.get("apps") or []}.get(ident.get("producer"), "no application")
            if ident.get("producer") is None:
                out.append(f"Shared identifier `{name}` has no producer.")
            elif ident["producer"] != self.app_id and not ident.get("produced"):
                out.append(f"Scenario \"{s.get('name')}\" types `{name}`, but {producer} never produces it (no "
                           f"scenario of {producer} names it).")
            elif b.get("value") and ident.get("value") and b["value"] != ident["value"]:
                out.append(f"Scenario \"{s.get('name')}\" types {b.get('field')} `{b['value']}`, but the shared "
                           f"value produced by {producer} is `{ident['value']}`.")
        return sorted(set(out))

    def check_interfaces_order(self):
        if not self.interfaces:
            return "n/a"
        self.levels["interfaces.order"] = self.contract_level()
        out = []
        for c in self.interfaces:
            ids = {a.get("id") for a in c.get("apps") or []}
            for key in ("launch_order", "reset_order"):
                if set(c.get(key) or []) != ids:
                    out.append(f"The {key.replace('_', ' ')} of recording {c.get('recording_id')} does not name every "
                               "application recorded with this one.")
        return out

    def check_interfaces_peer_down(self):
        rels = [r for c in self.interfaces for r in c.get("relationships") or [] if r.get("consumer") == self.app_id]
        if not rels:
            return "n/a"
        if any(c.get("edited") for c in self.interfaces):
            self.levels["interfaces.peer_down"] = "owner-confirmed"
        names = {a.get("id"): a.get("name") for c in self.interfaces for a in c.get("apps") or []}
        return [f"What this application does when {names.get(r.get('producer'), 'its producer')} is down is not "
                f"written ({r.get('kind')} relationship)." for r in rels if not (r.get("when_down") or "").strip()]

    def check_interfaces_schemas(self):
        rels = [r for c in self.interfaces for r in c.get("relationships") or [] if r.get("kind") in ("api", "file")]
        if not rels:
            return "n/a"
        self.levels["interfaces.schemas"] = self.contract_level()
        out = []
        for r in rels:
            schema = r.get("detail") or {}
            if r["kind"] == "api" and not (schema.get("method") and schema.get("path")):
                out.append("An API relationship has no method and path.")
            if r["kind"] == "file" and not schema.get("columns"):
                out.append("A file relationship has no columns.")
        return out

    def check_deploy_twin_label(self):
        from twinlab2_contract import package

        label = (self.data.get("target") or {}).get("twin_label") or {}
        if label.get("text") != package.TWIN_LABEL or self.surface not in (label.get("surfaces") or {}):
            return [f"The package does not require the \"{package.TWIN_LABEL}\" label on every screen."]
        return []

    def check_data_backend(self):
        b = (self.data.get("target") or {}).get("data_backend") or {}
        return [] if b.get("engine") else ["The package names no data backend."]

    def check_data_model(self):
        model = (self.data.get("target") or {}).get("data_model") or {}
        return [] if model.get("tables") else ["No reviewed DDL is attached: the builder infers the tables."]

    # -- access --------------------------------------------------------------------------------
    def signin_seen(self):
        seen = any(c.get("type") == "password" or SIGNIN.search(str(c.get("label") or c.get("id") or ""))
                   for _, c in _controls(self.screens))
        return seen or any(SIGNIN.search(str(e.get("label") or e.get("screen") or "")) for e in self.events)

    def check_access_signin(self):
        if not self.signin_seen():
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

    def host_screens(self):
        """The host screens the scenarios touch, by name, with their structure when known."""
        return sorted(self.touched | {n for n, sc in self.screens.items()
                                      if sc.get("surface", "mainframe") == "mainframe"})

    def check_mainframe_title(self):
        names = self.host_screens()
        if not names:
            return "n/a"
        return [f"Host screen `{n}` is known only by its layout id: no title line was recorded."
                for n in names if TITLE_ID.match(n) or not n.strip()]

    def check_mainframe_protected(self):
        if not self.screens:
            return ["The package has no host screen structure, so no protected field positions (labels, messages, "
                    "key line)."]
        from twinlab2.buildpack import host_fields  # buildpack imports this module

        out = []
        for name in sorted(self.touched & set(self.screens)):
            states = self.screens[name].get("states") or []
            if not any(c.get("protected") for st in states for c in host_fields(st)):
                out.append(f"Host screen `{name}` has no protected field with its position (labels, messages, "
                           "key line).")
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

    def screenshots(self):
        return [a for a in self.attachments if a.get("kind") == "Screen"]

    def needed_states(self):
        """[(screen, state id or None, label)] the scenarios reach: each recorded state a linked
        event lands on; a touched screen without structure counts as one state."""
        out = []
        linked = {e.get("event_id") for e in self.events}
        for name, sc in self.screens.items():
            for i, st in enumerate(sc.get("states") or [], 1):
                if linked & set(st.get("events") or []):
                    out.append((name, st.get("id"), f"`{name}` state {i}"))
        reached = {n for n, _s, _l in out}
        out += [(n, None, f"`{n}`") for n in sorted(self.touched - reached)]
        return out

    def baseline_for(self, screen, state):
        return any(b.get("screen") == screen and (state is None or b.get("state") == state)
                   and VIEWPORT.match(str(b.get("viewport") or "")) and b.get("theme") in ("light", "dark")
                   and b.get("scale") for b in self.baselines)

    def check_visuals_per_screen(self):
        need = self.needed_states()
        if not need:
            return "n/a"
        missing = [label for screen, state, label in need if not self.baseline_for(screen, state)]
        if not missing:
            return []
        return [f"{len(need) - len(missing)} of {len(need)} screen state(s) have an approved baseline."] + \
            [f"{label} has no approved baseline (viewport, theme and scale)." for label in missing]

    def check_visuals_states(self):
        special = []
        linked = {e.get("event_id") for e in self.events}
        for name, sc in self.screens.items():
            for i, st in enumerate(sc.get("states") or [], 1):
                if not linked & set(st.get("events") or []):
                    continue
                if any(c.get("enabled") is False or c.get("selected") or c.get("checked") for c in st.get("controls") or []) \
                        or any(re.search(r"error|alert|message|invalid", str(r.get("id") or r.get("name") or ""), re.I)
                               and r.get("visible", True) for r in st.get("regions") or []):
                    special.append((name, st.get("id"), f"`{name}` state {i}"))
        if not special:
            return "n/a"
        return [f"{label} shows disabled, selected or error controls but has no approved baseline."
                for screen, state, label in special if not self.baseline_for(screen, state)]

    def check_visuals_style(self):
        noted = any(STYLE.search(str(a.get("title") or "")) for a in self.attachments) or \
            any(STYLE.search(s.get("notes") or "") for s in self.scenarios)
        return [] if noted else ["No style notes (fonts, colours, spacing)."]

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
    dimensions = []
    for did, title in DIMENSIONS.items():
        items = [i for i in SPEC if dimension_of(i) == did and checker.surface in i.surfaces and i.id not in checker.na]
        have = sum(WEIGHT[i.severity] for i in items if i.id not in missing)
        weight = sum(WEIGHT[i.severity] for i in items)
        dimensions.append({"id": did, "title": title, "score": round(100 * have / weight) if weight else 100,
                           "present": [i.id for i in items if i.id not in missing],
                           "missing": [i.id for i in items if i.id in missing],
                           "not_applicable": [i for i in checker.na if dimension_of(BY_ID[i]) == did]})
    order = {s: n for n, s in enumerate(SEVERITIES)}
    gaps = sorted(checker.gaps, key=lambda g: (order[g["severity"]], list(BY_ID).index(g["item"])))
    counts = {s: sum(1 for g in gaps if g["severity"] == s) for s in SEVERITIES}
    confirmed = {c.get("item") for c in (data or {}).get("confirmations") or [] if isinstance(c, dict)}
    items = []
    for i in SPEC:
        if checker.surface not in i.surfaces:
            continue
        if i.id in checker.na:
            status, level = "not_applicable", None
        elif i.id in missing:
            status, level = "missing", None
        else:
            status = "present"
            level = checker.levels.get(i.id) or START[BASIS.get(i.id, "prose")]
            if i.id in confirmed and LEVELS.index(level) < LEVELS.index("owner-confirmed"):
                level = "owner-confirmed"
        items.append({"item": i.id, "title": i.title, "section": i.section, "dimension": dimension_of(i),
                      "severity": i.severity, "status": status, "level": level, "confirmed": i.id in confirmed})
    result = {"spec_version": SPEC_VERSION, "surface": checker.surface, "passed": counts["critical"] == 0,
              "score": round(100 * total_have / total_all) if total_all else 100, "sections": sections,
              "dimensions": dimensions, "gaps": gaps, "counts": counts, "items": items}
    return _levels(result)


def _levels(result):
    """`coverage` (applicable items at an evidence level of observed or above, weighted by
    severity: documented-only items do not count) and `levels` (how many items at each level)."""
    applicable = [i for i in result["items"] if i["status"] != "not_applicable"]
    weight = sum(WEIGHT[i["severity"]] for i in applicable)
    covered = sum(WEIGHT[i["severity"]] for i in applicable if i["level"] in COVERED)
    result["coverage"] = round(100 * covered / weight) if weight else 100
    result["levels"] = {"missing": sum(1 for i in applicable if i["status"] == "missing"),
                        **{level: sum(1 for i in applicable if i["level"] == level) for level in LEVELS}}
    return result


def with_tested(result, dimensions):
    """The result with the present items a passed verification dimension exercised raised to
    replay-tested (`dimensions`: {behavioural: {status}, ...} of the latest verification of a
    release built from this package). Pack bytes never carry this: it is shown, not frozen."""
    passed = {d for d, v in (dimensions or {}).items() if isinstance(v, dict) and v.get("status") == "pass"}
    tested = set().union(*(TESTED_BY.get(d, set()) for d in passed)) if passed else set()
    out = dict(result, items=[dict(i, level="replay-tested") if i["status"] == "present" and i["item"] in tested
                              else i for i in result.get("items") or []])
    return _levels(out)


def dimension_of(item) -> str:
    """The verification dimension an item serves: behavioural, structural, visual or deployment."""
    if item.section == "visuals":
        return "deployment" if item.id.startswith("deploy.") else "visual"
    if item.section in ("scenarios", "behaviour", "access", "interfaces") or item.id in (
            "data.examples", "data.shapes", "data.bindings", "data.binding_types"):
        return "behavioural"
    if item.id in ("data.backend", "data.model"):
        return "deployment"
    return "structural"


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
           "Where an item is missing, say in BUILD-NOTES.md what you assumed instead.", "",
           "By verification dimension: " + "; ".join(f"{d['id']} {d['score']}%" for d in result["dimensions"]) + ".",
           "",
           f"Evidence: {result.get('coverage', 0)}% observed in the recording, confirmed by the owner or tested by "
           "replay; items only documented (a written line or keyword) do not count as covered. "
           + "; ".join(f"{n} {k}" for k, n in (result.get("levels") or {}).items()) + ".",
           ""]
    level = {i["item"]: i["level"] for i in result.get("items") or []}
    for section in result["sections"]:
        if not section["present"] and not section["missing"]:
            continue
        out.append(f"**{SECTIONS[section['id']]}** ({section['score']}%)")
        out.append("")
        out += [f"- present ({level.get(i) or 'documented'}): {title_of(i)}" for i in section["present"]]
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
            "score": result["score"], "coverage": result.get("coverage"), "levels": result.get("levels"),
            "evidence": {i["item"]: i["level"] for i in result.get("items") or [] if i["level"]},
            "sections": {s["id"]: s["score"] for s in result["sections"]},
            "dimensions": {d["id"]: d["score"] for d in result["dimensions"]},
            "missing": [{"item": g["item"], "severity": g["severity"], "details": g["details"]}
                        for g in result["gaps"]]}
