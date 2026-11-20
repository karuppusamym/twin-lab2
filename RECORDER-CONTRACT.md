# Recorder contract 1.1.0

What a workstation recorder sends to the TwinLab2 portal, and how the two sides agree on a version. The
machine-checkable form is [`contract/recorder-contract.schema.json`](contract/recorder-contract.schema.json)
(JSON Schema 2020-12). [`recorder_contract.py`](recorder_contract.py) holds the version rules and a
small validator for that schema. The portal and this repository's recorder both run it.

## Version

`contract_version` is a semantic version, `MAJOR.MINOR.PATCH`.

| Change | Version | Example |
|---|---|---|
| A field is removed or renamed, a field's meaning or type changes, an optional field becomes required, a limit is tightened | MAJOR | `2.0.0` |
| An optional field is added. Older portals ignore it. | MINOR | `1.1.0` |
| Wording or documentation only, with no change to what is valid | PATCH | `1.0.1` |

The portal accepts a recorder when the recorder's version is in the portal's supported range: the
same MAJOR and not older than `OLDEST_SUPPORTED`. That range is `>=1.0.0 <2.0.0` today. A newer
MINOR is accepted because the portal ignores fields it does not know.

| The recorder sends | The portal answers | What the person is told |
|---|---|---|
| no `contract_version` (recorders before 0.2.0) | `426` with `"update": "recorder"` | update TwinLabRecorder to 0.2.0 or later |
| an older MAJOR, or older than `OLDEST_SUPPORTED` (`0.9.0`) | `426` with `"update": "recorder"` | update TwinLabRecorder to a build for the portal's contract |
| a newer MAJOR (`2.0.0`) | `426` with `"update": "portal"` | update the portal, or use a recorder for contract 1.x |
| a supported version, wrong shape | `400` with `errors` (paths such as `$.apps[0].level`) | the recorder's collectors do not match the contract |
| a supported version, right shape | as before (`200`) | |

A refusal answers with this JSON:

```json
{"error": "this recorder speaks contract 2.0.0, newer than this portal's 1.1.0 (supported >=1.0.0 <2.0.0). Update the portal, or use a recorder for contract 1.x.",
 "update": "portal", "contract": {"portal": "1.1.0", "supported": ">=1.0.0 <2.0.0"}}
```

The portal checks the version before anything else. An outdated recorder hears what to update,
not a list of field errors, and a refused pairing does not use up the code or count as a wrong
code.

The recorder also checks its own references against the schema before sending them. If the Twin
Lab collectors it was built from no longer match the contract, the person is told on the PC and
nothing leaves it.

## Messages

Every request also carries the headers `X-TwinLab-Recorder: <name> <version>` (for example
`TwinLabRecorder 0.2.0`) and `X-TwinLab-Contract: <contract_version>`. Agent calls after pairing use
`Authorization: Bearer <machine token>`.

### Pairing: `POST /api/agent/pair`

```json
{"contract_version": "1.1.0", "code": "ABCDE-FGHJK", "name": "Workstation 7",
 "recorder": {"name": "TwinLabRecorder", "version": "0.2.0"}}
```

| Field | Required | Rule |
|---|---|---|
| `contract_version` | yes | see Version |
| `code` | yes | the one-time code from the portal's Machines page. Dashes, spaces and case are ignored. |
| `name` | yes | 1 to 80 characters, no control characters |
| `recorder` | no | `name` (1 to 60 characters) and `version` (1 to 40), optional `twinlab_commit` (7 to 40 hex) |

The answer is `{"token", "machine_id", "name", "contract_version", "contract_supported"}`. The
portal keeps the machine's contract version and recorder and shows them on the Machines page.

### References of a recorded session: `POST /api/agent/references`

This is the only recording upload. The recording itself stays on the PC. The recorder sends the
applications it recorded and, for each one, the steps as labels.

| Field | Required | Rule |
|---|---|---|
| `contract_version` | yes | see Version |
| `format` | yes | `"twinlab-references/1"` |
| `recorder` | no | as in pairing. Stored with the recording. Without it, the `X-TwinLab-Recorder` header is used. |
| `session.id` | yes | `YYYYMMDDTHHMMSSZ-xxxxxxxx` (8 hex characters) |
| `session.name`, `started`, `ended` | no | text, at most 120, 40 and 40 characters |
| `session.state` | no | `stopped` or `recovered` |
| `session.twinlab_commit` | no | 7 to 40 hex: the Twin Lab commit of the collectors |
| `session.automation_id`, `portal_session_id` | no | catalog ids (integers ≥ 1). `portal_session_id` is the offered session that was recorded. |
| `session.recorder`, `location`, `targets` | no | informational. The portal does not store them. |
| `apps[]` | yes (at most 50) | the applications recorded at level `full` or `steps` |
| `apps[].key` | yes | the identity: `desktop:<company>/<product>`, `web:<scheme>://<host>[:<port>]` or `mainframe:<host>:<port>`. `exe:` is a target and never a recorded key. |
| `apps[].level` | yes | `full` or `steps` |
| `apps[].surface` | no | `web`, `desktop` or `mainframe` |
| `apps[].display`, `exe`, `company`, `product`, `version`, `origin`, `toolkit` | no | text. The portal keeps up to 120 characters (40 for `version` and `toolkit`) and drops longer values. |
| `apps[].app_id` | no | the catalog id from the offered target |
| `flows` | yes | an object: for each application key in `apps`, its steps (at most 50 000) |
| `timeline[]` | yes (at most 20 000) | `{"app": <key in apps> or "presence", "steps": <count>}`: runs of consecutive steps. `presence` stands for applications that were not targets, which are never named. |
| `robot` | no | counts from a UiPath robot's log: `activities`, `ui_activities`, `covered`, `gaps` |

#### Events: one step of a flow

```json
{"n": 16, "action": "type", "control": "field", "label": "ACCOUNT NO", "screen": "ACCOUNT INQUIRY"}
```

| Field | Required | Meaning |
|---|---|---|
| `n` | yes | the step's position in the whole session (integer ≥ 0). Steps of different applications share one sequence. |
| `action` | no | what the step did: `click`, `type`, `select`, `check`, `submit`, `goto`, `window_open`, `screen`, `aid` (a 3270 key), …, else `other` |
| `control` | no | the **control reference**: the control's type, such as `Button`, `Edit`, `input`, `select`, `field` (a 3270 input field), `key` or `screen` |
| `label` | no | the control's **label**: its automation id or name (`btnSave`, `txtSearch`), a web element's id or name, a 3270 field's label (`USERID`) or a key's name (`PF3`) |
| `screen` | no | the **screen reference**: a desktop window's automation id or title, a web route with ids replaced by `{id}` (`https://host/cases/{id}`), or a 3270 screen's title (or its layout id when there is none) |
| `key` | no | a named key pressed in the step (`Enter`, `Tab`, `F1`…`F12`) |

A step never holds a typed value, the content of a screen or field, or a picture. The portal stores
only these six fields and drops any other field. It drops text over 160 characters or with control
characters.

#### Contract 1.1: event ids, capture times and completeness

1.1.0 is a minor version: everything above still holds, and the portal still accepts 1.0.x. A
message's rules are those of the version it declares. A 1.1.0 (or later 1.x) message is checked
against `#/$defs/references_1_1`, which adds these requirements.

Each step also carries:

| Field | Required in 1.1 | Rule |
|---|---|---|
| `event_id` | yes | a UUIDv7, lowercase canonical form, assigned **when the event is captured**. Finalisation, crash recovery and reordering never change it. Unique within the recording. |
| `channel` | yes | `web`, `desktop`, `mainframe` or `network` |
| `captured_at` | yes | ISO-8601 UTC with milliseconds: `2026-11-20T10:02:00.123Z` (`+00:00` and microseconds are accepted) |
| `seq` | yes | integer ≥ 0, the display order. It may be recomputed. **It is never used as a reference.** `n` stays required for 1.0 portals and equals `seq`. |

The references also carry the recording's `completeness` (required in 1.1) and may carry the
recorder's `preflight`:

```json
{"completeness": {
   "overall": "partial",
   "channels": {
     "web": {"status": "complete", "gaps": [], "notes": []},
     "desktop": {"status": "interrupted",
                 "gaps": [{"from": "2026-11-20T10:02:00.000Z", "to": "2026-11-20T10:05:00.000Z", "reason": "collector restarted"}],
                 "notes": ["UI Automation stopped answering"]},
     "mainframe": {"status": "not_requested"}},
   "network_evidence": "incomplete", "recovered": true, "loss_window_seconds": 4},
 "preflight": {"checks": [{"name": "UI Automation", "ok": true, "detail": "available"}], "ready": true}}
```

| Field | Required | Rule |
|---|---|---|
| `completeness.overall` | yes | `complete`, `partial` or `failed`. A stopped recording is not complete by itself: only `complete` is. |
| `completeness.channels` | yes | an object with any of `web`, `desktop`, `mainframe`, `network` (no other names). Each has `status` (`complete`, `interrupted`, `missing`, `not_requested`), optional `gaps[]` (`from`, `to`: ISO-8601 with offset; `reason`: text up to 200) and optional `notes[]` (text up to 300). |
| `completeness.network_evidence` | yes | `complete`, `incomplete` or `none` |
| `completeness.recovered` | yes | `true` when the recording was rebuilt from its journal after the recorder stopped unexpectedly |
| `completeness.loss_window_seconds` | no | a number ≥ 0, or `null`: at most this many seconds before the interruption may be lost |
| `preflight.checks[]` | with `preflight` | `name` (text up to 80), `ok` (boolean), optional `detail` (up to 300) |
| `preflight.ready` | with `preflight` | whether the recorder was ready to record |

Both objects go at the **top level** of the references message, next to `session`, not inside it.

#### Event ids in the portal

Everything the portal keeps about an event refers to its `event_id`: an application's evidence
selection and suggestions, a scenario's linked events (`<recording id>#<event_id>`) and the
build pack's events (`evidence.json` events carry `event_id`; `seq` only orders them). A resend
of the same recording, even one whose `seq` values were recomputed by recovery, is matched by
`event_id`: the owner's reviewed selection still means the same events, and only events with a
new `event_id` are suggested.

A 1.0.x recording has no event ids. The portal derives one per step **once**, when it first
receives it (a UUIDv5 of the recording and the step's `n`), stores it with the step, and reuses
the stored id on a resend. Its recording page shows "Completeness unknown (older recorder)". An
imported `steps.jsonl` keeps the `event_id` of a row that has one; other rows get a derived id.

Catalogs from before 1.1 are migrated at start-up (`event_ids.migrate`): stored events and flows
get their derived ids, and selections, suggestions and scenario links by number become links by
`event_id`. Build briefs already exported are kept as they were.

## What the portal records

For each recording, the portal keeps the table `recording_contracts`: the contract version, the
recorder's name and version, the Twin Lab commit and the time received. The recording's page shows
them ("Sent by TwinLabRecorder 0.2.0 · recorder contract 1.0.0 · Twin Lab 430dfe538965"). For each
machine, it keeps the last contract version and recorder in `machine_contracts`.

From 1.1 it also keeps, in `recording_completeness`, the completeness and preflight as received
and an owner's acceptance of a partial or failed recording. The recording page shows per channel
what was complete or interrupted ("Web complete; desktop interrupted 10:02–10:05; network
evidence incomplete"), the affected time ranges on a timeline, a warning when the recording was
recovered, the preflight results, the upload progress (`GET /api/recordings/<id>/upload`, "not
available" when the portal has no artifact storage) and a preview of the evidence. A partial or
failed recording **needs attention**: its applications' journey stays at Add examples, with
"Review partial recording" as the next action, until it is re-recorded or its owner accepts it as
it is. A resend with different completeness withdraws that acceptance.

## Who implements it

- **TwinLabRecorder** (`agent/`, version 0.2.0 or later) speaks contract 1.x. The references are
  built by Twin Lab's collectors (`labctl.workstation.timeline.outbox`) at the commit pinned in
  `twinlab.lock.json`. That outbox does not carry `contract_version` yet. The recorder stamps the
  version its shape is (1.1.0 when the outbox reports `completeness`, else 1.0.0) and itself on
  the outbox, then validates it, before it sends (`agent.main.contract_references`). The pinned
  commit's collectors emit 1.0.0, which is the `contract_version` in `twinlab.lock.json`; the pin
  moves to collectors that assign event ids at capture time once Twin Lab has them.
- **The portal** (`registry.py`) checks every pairing and upload with `recorder_contract.validate`.

## Changing the contract

1. Change the schema and the tables above. Set `x-contract-version` in the schema and
   `CONTRACT_VERSION` in `recorder_contract.py` to the new version (a test checks that they
   agree). `contract_version` in `twinlab.lock.json` is the version the pinned collectors emit:
   change it when the pin moves. A minor version whose new fields are required of its senders
   gets its own definition (as `references_1_1`), chosen by `recorder_contract.definition`.
2. For a MAJOR change, decide whether the portal still accepts the old MAJOR for a while. If it
   does, handle both versions explicitly. If it does not, raise `OLDEST_SUPPORTED`.
3. Add samples to `tests/test_contract.py`.
4. When the change comes from Twin Lab's collectors, move the pin (`UPSTREAM.md`) and regenerate
   `tests/fixtures/references-from-pinned-twinlab.json` in the same commit.
