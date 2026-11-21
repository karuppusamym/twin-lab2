# Package completeness

A builder package is complete when a coding agent can rebuild the application from it alone, so
that the recorded scenarios replay against the copy and the release checks can judge it. This page
is the spec; [`server/twinlab2/pack_validator.py`](../server/twinlab2/pack_validator.py) holds the
same items as code (`SPEC`), and `tests/server/test_pack_validator.py` checks that this table and
the code agree.

## How it is used

- **Preview** (`GET /api/v1/apps/{id}/package-draft`, the *Builder package preview* page): the
  draft is checked and the page shows the checklist: a score per section, every gap with why it
  matters and where the owner fixes it.
- **Freeze** (`POST /api/v1/apps/{id}/packages`): a draft with a **critical** gap is refused with
  409 `incomplete_package`, one `errors` entry per critical gap. The owner can freeze it anyway by
  writing a reason (`override_reason`, at least 10 characters). The reason and the gaps it covers
  are audited (`package.freeze_override`), kept on the revision (`known_gaps`, also in its change
  summary) and carried by the package: "Known gaps" in `BUILD_TASK.md`, `known_gaps` in
  `evidence.json`. The approval still names the draft's SHA-256; the stored package is that draft
  plus only the owner's reason and the list of gaps.
- **The package**: `BUILD_TASK.md` has a "Completeness" section (what is present and what is
  missing, per section) and `evidence.json` has `completeness` (the scores and the missing items),
  so the coding agent knows what it has to assume and records it in BUILD-NOTES.md.

## Severity

| Severity | Meaning | Weight in the score |
|---|---|---|
| critical | Without it the copy cannot pass the replay or the release checks. Blocks the freeze unless overridden with a reason. | 3 |
| important | The builder has to guess, and the copy may differ from the original. | 2 |
| nice | Helps the builder; never decides a result. | 1 |

A section's score is the weight of its present items over the weight of its applicable items; an
item that does not apply (no grid on the screens, no sign-in seen, no menu step) is not counted.

## What the replay checks, and where it is in the spec

Everything the fidelity replay (`server/twinlab2/replay/`) and the release checks
(`independent.py`) depend on is critical:

| The replay / release check | Spec item |
|---|---|
| After every step, the interactive elements by tag, id and classes, and how many of each (`fidelity.compare_dom`) | `screens.structure`, `screens.referenced`, `screens.controls` |
| Visibility, enabled state, option lists and value format of each element after each step | `screens.states` |
| Each step's target resolves to exactly one element (fail-closed locator) | `screens.targets`, `web.routes` |
| A step that types or selects needs a synthetic binding (`bindings.yaml`), else it is blocked | `data.examples` |
| The replay opens the first step's page; tabs and origins through the origin map | `navigation.entry`, `web.routes` |
| Acceptance cases judge the business outcome (`acceptance.py`, `judging.py`) | `scenario.expected` |
| A scenario's recordings are replayed; a scenario with no recording is not-run | `scenario.events`, `scenario.links_current` |
| The deployment reports the built commit at `/.twinlab/version` | `deploy.version` |
| The reset check: the lab agent's data equals its baseline after a reset | `data.backend` (with `scenario.starting` for the baseline) |
| Mainframe screens are fixed grids; transactions move on AID keys | `mainframe.layout`, `mainframe.aid` |
| A 3270 screen is named by all the protected text of its first non-empty row (the title line), compared exactly; then its input fields by row, column, length and attributes | `mainframe.title`, `mainframe.layout` |

## The items

| Item | What | Surfaces | Severity | Dimension | Why it matters |
|---|---|---|---|---|---|
| `scenario.present` | At least one business scenario | all | critical | behavioural | The builder builds the scenarios and the release checks replay them; without one there is nothing to build or check. |
| `scenario.events` | Each scenario links recorded events | all | critical | behavioural | A scenario is checked by replaying its recorded steps; one without events can never pass the replay. |
| `scenario.links_current` | Every linked event is still selected | all | critical | behavioural | A linked event that is no longer selected is not in the pack, so its step cannot be rebuilt or replayed. |
| `scenario.expected` | Expected result of each scenario | all | critical | behavioural | The acceptance cases judge the business outcome against it: a structure-only copy can show a wrong count, title or message and still look right. |
| `scenario.starting` | Starting conditions of each scenario | all | important | behavioural | The replay starts from a declared baseline (signed in, which records exist); without it the builder guesses the seed data. |
| `scenario.actions` | Actions written by the owner | all | important | behavioural | Recorded events say what was clicked, not why; the owner's actions tell the builder the intent of each step. |
| `scenario.failures` | Failure cases | all | important | behavioural | What the app does with wrong input is not in a happy-path recording; without it the copy accepts everything. |
| `scenario.messages` | Validation and error messages the app shows | all | important | behavioural | Required and numeric fields reject input with a message; the replay compares dialogs' messages word by word, so the builder needs the exact texts. |
| `behaviour.invalid_input` | Invalid input | all | important | behavioural | A recording shows valid entries; what the app refuses (empty, wrong format, out of range) is invisible unless written down. |
| `behaviour.permissions` | Permissions | all | important | behavioural | Who may see or change what is not in a recording made by one person. |
| `behaviour.empty_results` | Empty results | all | important | behavioural | Lists and searches were recorded with rows; the copy needs to know what an empty result shows. |
| `behaviour.retries` | Retries and unavailable services | all | important | behavioural | What happens when a save times out or a service is down decides whether a retry duplicates data. |
| `behaviour.duplicates` | Duplicates | all | important | behavioural | Creating the same record twice is refused, merged or allowed; the recording shows one creation. |
| `behaviour.cancellation` | Cancellation | all | important | behavioural | The screens have Cancel or Close; whether cancelling discards, keeps a draft or asks is not recorded. |
| `behaviour.persistence` | Persistence | all | important | behavioural | What was saved must still be there after a reload or a restart; acceptance cases can check a reload. |
| `behaviour.unrecorded` | Failure cases or precondition rules | all | important | behavioural | Off the recorded path the builder follows the pack's defaults (a direct URL to a later page returns to the start, keys not listed do nothing); without the owner's failure cases or precondition rules the copy behaves however the builder guessed. |
| `screens.structure` | Screen structure (recorder contract 1.3) | all | critical | structural | The replay compares every recorded screen's interactive elements by tag, id and classes after every step; without the structure the builder cannot know them. |
| `screens.referenced` | Every screen the scenarios touch | all | critical | structural | Each screen a scenario's step lands on is compared with the rebuilt one; a missing screen fails that step. |
| `screens.controls` | Controls of each screen: kind, identifier or label, order | all | critical | structural | The replay finds each step's target among the controls and counts every interactive element; a control without kind or identifier cannot be rebuilt to match. |
| `screens.states` | Each referenced event resolves to a screen state | all | critical | structural | After every step the replay compares the screen state the recording shows (enabled, read-only, required, visible, checked); an event with no state has nothing to compare against. |
| `screens.targets` | Each step's control or label | all | critical | structural | The replay locates each step's target and it must match exactly one element; a click or entry without a control or label cannot be located. |
| `screens.grids` | Grid columns | all | important | structural | Tables and lists are rebuilt with their column headers; a grid without headers is guessed. |
| `screens.withheld` | Identifiers withheld as possible data | all | important | structural | Identifiers, classes and labels that could be data (with a digit, an @, too long) are left out of the structure; the replay compares by them, so a stable id such as `button1` or `addressLine2` is lost. |
| `navigation.entry` | Entry screen | all | critical | structural | The replay opens the app at the first step's screen; without it the copy has no known start. |
| `navigation.transitions` | Navigation between screens | web, desktop | important | structural | The scenarios move between screens; the transitions say which action leads where. |
| `data.examples` | Synthetic values for the steps that type or select | all | critical | behavioural | A recording keeps no typed value: the replay types the bound synthetic values, and a step that needs one without it blocks the replay. |
| `data.shapes` | The shape of each example field | all | important | behavioural | "Field: value" lines tell the builder which field takes which format; the replay compares value formats. |
| `data.backend` | Data backend | all | critical | deployment | The demo creates its tables and seed data on this backend and resets to that seed. |
| `data.model` | Data model (DDL) | all | important | deployment | Without DDL the builder infers tables from the screens, and the names will differ from the original. |
| `access.signin` | Roles and sign-in | all | important | behavioural | The recording shows a sign-in; the builder needs which roles exist and which one each scenario starts as. |
| `web.routes` | Routes of the web screens | web | critical | structural | The replay maps each recorded page to the copy by its route; a screen without a route cannot be reached. |
| `web.api` | API calls (HAR or API description) | web | important | structural | The front end calls an API; method, path template and field shapes come from a reviewed HAR or an API description. |
| `desktop.toolkit` | Desktop toolkit (WinForms, WPF, Java, …) | desktop | important | structural | Controls, their ControlTypes and keyboard behaviour depend on the toolkit; the builder should use the same one. |
| `desktop.windows` | Window and dialog hierarchy | desktop | important | structural | Which dialogs are modal and which window owns them changes how the copy behaves. |
| `desktop.menus` | Menus used by the scenarios | desktop | important | structural | A menu step needs its menu path; without menu controls the builder guesses the menu bar. |
| `desktop.shortcuts` | Keyboard shortcuts | desktop | important | structural | A key step without its key cannot be rebuilt or replayed. |
| `mainframe.layout` | Field positions and lengths | mainframe | critical | structural | A 3270 screen is a fixed grid: each field's row, column and length is the screen. |
| `mainframe.title` | Title line of each host screen | mainframe | critical | structural | The replay names a 3270 screen by all the protected text of its first non-empty row and compares it exactly; a screen known only by its layout id gives the builder no title to put there. |
| `mainframe.attributes` | Field attributes (protected, hidden, numeric) | mainframe | important | structural | Protected, non-display and numeric fields behave differently on the terminal. |
| `mainframe.protected` | Protected text positions (labels, messages, key line) | mainframe | important | structural | Where labels, output fields, the message line and the key line sit is the look of a 3270 screen; without them the builder places them and the copy looks different. |
| `mainframe.aid` | AID keys (Enter, PF keys, Clear) | mainframe | critical | structural | A host transaction moves on an AID key; a step without it cannot be replayed. |
| `mainframe.flow` | Transaction flow between host screens | mainframe | important | structural | Which AID key on which screen leads to which next screen is the transaction. |
| `visuals.screens` | Screenshots or layout notes | all | important | visual | Structure says what is on a screen, not where; visual checks compare the copy's picture. |
| `visuals.per_screen` | An approved screenshot of every screen (visual baseline) | all | important | visual | The visual comparison (replay/visual.py) needs a baseline per compared state; a screen without one cannot be judged on appearance. |
| `visuals.states` | Screenshots of special states (disabled, selected, error) | all | important | visual | A disabled button, a selected row or an error message looks different; one picture per screen shows only one state. |
| `visuals.style` | Fonts, colours and spacing | all | nice | visual | Without them the copy looks generic even when its structure matches. |
| `deploy.target` | Deployment target | all | critical | deployment | The builder commits the deployment files of the target (OpenShift, or a local install for desktop). |
| `deploy.version` | Version endpoint | web, mainframe | critical | deployment | The release checks run only against a deployment that reports the built commit at the version endpoint. |
| `deploy.repository` | Demo repository | all | nice | deployment | The deployment files name the demo repository; without it the builder fills it in. |

**Business behaviour beyond the happy path** (`behaviour.*`): invalid input, permissions, empty
results, retries, duplicates, cancellation and persistence. A recording shows what one person did
once; these are what the copy must also do. Each applies only when the scenarios make it relevant
(typing or saving for invalid input, retries, duplicates and persistence; a sign-in or a role for
permissions; a grid or search for empty results; a Cancel or Close control for cancellation) and is
present when a scenario's text covers it. They are important, not critical: the replay checks a
reload or a fresh context only where an acceptance case declares one, never unconditionally.

**Verification dimensions.** Every item serves one dimension of release verification, and the
validator scores each (`dimensions`): behavioural (scenarios, outcomes, failure paths, examples,
roles), structural (screens, controls, states, navigation, surface specifics), visual (approved
screenshots per screen and per special state, style) and deployment (data backend and model,
target, version endpoint, repository). A release's verified scope can then be stated per dimension.

Each gap the validator reports also names the page that fixes it (Scenarios, Recorded flows,
Uploaded evidence, Recordings, Data and deployment, Git repository) and the action to take there,
and lists the scenarios, screens or events that lack the item.

## How each item is checked

- **Scenarios**: per scenario, its fields in `evidence.json` (`starting_conditions`, `actions`,
  `expected_result`, `failure_cases`, `example_values`, `events`). `scenario.links_current` reads
  the "Linked event … is no longer selected" question the pack builder adds. `scenario.messages`
  passes when a failure case quotes or names a message; it does not apply when no failure case is
  written and no required or numeric field is on the scenarios' screens.
- **Screens**: `screens` of `evidence.json` (recorder contract 1.3). The screens the scenarios
  touch are the `screen` of their linked events; each event must be in a state's `events`; each
  click, entry or selection must name its `control` or `label`.
- **Navigation**: the first linked event of each scenario has a screen; for consecutive events on
  different screens, a transition from the first to the second.
- **Data**: a scenario with `type`/`select`/`input`/`fill`/`change` events needs example values;
  each value should read `Field: value`. The backend and the tables come from `target`.
- **Roles and sign-in**: applies when a control is a password field or a label or screen says
  sign in / log in; then a scenario's starting conditions must say who is signed in.
- **Surface**: web screens are routes (a path or URL); web API calls need an attachment of kind
  API or a `.har`. Desktop: `app.toolkit` (the toolkit the recorder detected), layout notes or
  screenshots for windows and dialogs, menu controls when a step uses a menu, a named key on every
  key step. Mainframe: every field of a touched host screen has row, column and length; every host
  screen has a title (not only a layout id `scr:…`); at least one protected field with its position
  per touched screen; every AID step names its key.
- **Behaviour not recorded**: `behaviour.unrecorded` is present when a scenario has failure cases or
  its text states a precondition rule ("only reached from", "a direct URL", "unless", "PF5 does
  nothing", "sends you back"). BUILD_TASK.md always has a "Behaviour not recorded" section with the
  defaults the builder follows where the owner said nothing: web pages are reached only as recorded
  (a direct URL to a later step returns to the start), keys not listed for a host screen do nothing,
  the required fields of the recorded forms, and the owner's failure cases first.
- **Visuals and deployment**: a reviewed screenshot (kind Screen); `target.deploy` with its files
  in the package; `target.version_endpoint` for web and mainframe; `app.git_url`.

## Capture gaps

Items that today's recorder or pack builder cannot produce at all, whatever the owner does on the
pages. The validator still reports them (with `capture_gap` set) so the builder knows:

| Item | What is missing | Where |
|---|---|---|
| `web.api` | The recorder keeps network evidence on the recording machine; API calls reach the pack only from an attached, reviewed HAR (redacted), never as a summary of methods, path templates and field shapes. | `recorder/twinlab2_recorder/workstation/` (network evidence), `server/twinlab2/buildpack.py` |
| `desktop.windows` | One screen per window, without its owner window or whether it is modal. | `recorder/twinlab2_recorder/workstation/structure.py` (`desktop_state`), `contract/twinlab2_contract/structure.py` |
| `screens.withheld` | Identifiers, classes and labels that could be data (any digit, an `@`, longer than 40 characters) are dropped, so stable ids such as `button1` or `addressLine2` are lost too. Only a count reaches the portal (`withheld` per state), never the names, so the preview shows how many per screen and state, not which. | `contract/twinlab2_contract/structure.py` (`ident()`, `plain()`, `clean_state`), `recorder/twinlab2_recorder/workstation/flow.py` (`ident()`, `plain()`), `recorder/twinlab2_recorder/workstation/structure.py` |
| `visuals.per_screen` | The recorder's screenshots (the replay's default visual baselines, `replay/visual.py`) stay on the recording machine; the pack has only screenshots the owner attaches and approves. | `recorder/twinlab2_recorder/workstation/` (pictures), `server/twinlab2/buildpack.py` |
| `mainframe.protected` | Recorder contract 1.3 sends only unprotected fields; protected output fields appear only as the label of the next field. Once a state's controls carry protected fields (`protected: true`, with `row`, `col`, `len` and `text`, or `shape` when the text may be data), the pack renders them as a layout table and the gap closes. | `recorder/twinlab2_recorder/workstation/structure.py` (`mainframe_state`), `contract/twinlab2_contract/structure.py` (`clean_control`) |

Fixed in the pack builder with this spec (it had them and did not copy them):

- the desktop toolkit the recorder detects (`app_identities.facts.toolkit`) is now `app.toolkit`;
- mainframe AID keys PF1–PF24, PA1–PA3 and Clear were dropped as "not a named key"
  (`buildpack.NAMED_KEY`); they are now kept;
- the version endpoint is now `target.version_endpoint` in `evidence.json`, not only prose in
  BUILD_TASK.md.
