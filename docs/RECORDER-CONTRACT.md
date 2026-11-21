# Recorder contract 1.2.0

What a workstation recorder sends to the TwinLab2 portal, and how the two sides agree on a version. The
machine-checkable form is [`contract/twinlab2_contract/recorder-contract.schema.json`](contract/twinlab2_contract/recorder-contract.schema.json)
(JSON Schema 2020-12). [`twinlab2_contract.recorder_contract`](recorder_contract.py) holds the version rules and a
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
{"error": "this recorder speaks contract 2.0.0, newer than this portal's 1.2.0 (supported >=1.0.0 <2.0.0). Update the portal, or use a recorder for contract 1.x.",
 "update": "portal", "contract": {"portal": "1.2.0", "supported": ">=1.0.0 <2.0.0"}}
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

This message holds no recording: the recorder sends the applications it recorded and, for each
one, the steps as labels. The recording itself stays on the PC unless the person uploads it
(Recording uploads, below).

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
| `seq` | should be sent | integer ≥ 0, the display order. It may be recomputed. **It is never used as a reference.** `n` stays required for 1.0 portals and equals `seq`; a step without `seq` is ordered by `n`. |

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

#### Contract 1.2: application path prefixes

1.2.0 is a minor version with no new requirement: a 1.2 message is checked against the same
`#/$defs/references_1_1`, and the portal still accepts 1.0.x and 1.1.x.

Several applications can share one host and be told apart by path: `https://erp.example.test/finance`,
`/finance/billing` and `/finance/payroll` are three catalog applications. Up to 1.1 a web step's
`screen` kept only the first path segment (`https://erp.example.test/finance/{id}/{id}/{id}`), so
the portal could never put a step on `/finance/billing`. From 1.2:

| Field | Rule |
|---|---|
| `session.targets[].path` | optional: a web target's path prefix as the portal's catalog names it (`/finance/billing`, whole segments, no query or fragment, up to 120 characters) |
| `screen` (web) | when the step's URL is under one of the session's target prefixes on its origin, that prefix is kept whole, the longest one winning, by whole segments (`/finance/billing-archive` is not under `/finance/billing`); after it, only route words are kept and every other segment is `{id}`. A step under no prefix keeps the 1.1 rule: the first segment when it is an identifier. |

The prefix is the target's own text, given by the portal, never read from the URL: a segment in
that place that no target names is still `{id}`, and no query, fragment or typed value is kept.

The portal assigns a web step to the identity with the longest path prefix its screen is under
(`url_match: path`), whatever the version: a 1.1 screen such as `/finance/{id}/{id}` is under
`/finance` and under no deeper prefix, so a 1.1 recording is assigned exactly as before.

#### Event ids in the portal

Everything the portal keeps about an event refers to its `event_id`: an application's evidence
selection and suggestions, a scenario's linked events (`<recording id>#<event_id>`) and the
build pack's events (`evidence.json` events carry `event_id`; `seq` only orders them). A resend
of the same recording, even one whose `seq` values were recomputed by recovery, is matched by
`event_id`: the owner's reviewed selection still means the same events, and only events with a
new `event_id` are suggested.

A send adds to the recording and never removes from it. Applications are kept by identity key
and steps by `event_id`. A recorder may send a session in parts (a recovered first half, then the
rest) or send a batch again. The latest send's copy of a step wins. The session, completeness and
preflight are the latest send's. So is the timeline, unless that send leaves out applications an
earlier send had: then the earlier timeline is kept and the new one appended to it.

A 1.0.x recording has no event ids. The portal derives one per step **once**, when it first
receives it (a UUIDv5 of the recording and the step's `n`), stores it with the step, and reuses
the stored id on a resend. Its recording page shows "Completeness unknown (older recorder)". An
imported `steps.jsonl` keeps the `event_id` of a row that has one; other rows get a derived id.

Catalogs from before 1.1 are migrated at start-up (`event_ids.migrate`): stored events and flows
get their derived ids, and selections, suggestions and scenario links by number become links by
`event_id`. Build briefs already exported are kept as they were.

### Recording uploads: `/api/uploads`

A recording file (TwinLabRecorder sends the session folder as a ZIP) goes up only when the person
uploads it, through a resumable upload. This API is an addition within contract 1.x: it changes
no message above, and a portal without it answers `404`, which leaves the upload queued on the PC.
The portal checks `contract_version` at init exactly as for pairing (see Version).

1. **Init**: `POST /api/uploads` with the machine token.

   ```json
   {"contract_version": "1.1.0", "session_id": "20261119T100000Z-0a1b2c3d", "size": 5242887,
    "sha256": "<64 lowercase hex>", "content_type": "application/zip"}
   ```

   | Field | Required | Rule |
   |---|---|---|
   | `contract_version` | yes | see Version |
   | `session_id` | yes | the recorded session (`session.id` of its references). The recording is `ws-<session_id>`. |
   | `size` | yes | bytes, at least 1, at most the portal's limit (`TWINLAB2_UPLOAD_MAX`, 4 GiB by default) |
   | `sha256` | yes | of the whole file, 64 lowercase hex characters |
   | `content_type` | no | `application/zip` (the default), `application/x-ndjson`, `application/json` or `application/octet-stream` |

   The answer (`201` for a new upload, `200` for one already under way or confirmed):

   ```json
   {"upload_id": "…", "upload_token": "…", "expires_at": "2026-11-20T10:00:00+00:00",
    "state": "receiving", "received_bytes": 0, "total_bytes": 5242887, "chunk_size": 8388608,
    "mode": "portal", "recording_id": "ws-20261119T100000Z-0a1b2c3d", "sha256": "…", "last_error": null}
   ```

   `upload_token` is the **upload authorisation**: it is valid for this upload only, until
   `expires_at` (an hour by default), and goes in `Authorization: Bearer <upload_token>` on the
   calls below. Init again with the same `session_id`, `size` and `sha256` to resume: the answer
   is the same `upload_id` with a new token (the old one stops working) and the offset received
   so far. Init with another `sha256` replaces an upload of that session still in progress. A
   session recorded by another machine is refused with `409`.

2. **Where to resume**: `GET /api/uploads/<upload_id>` answers the same fields (and the header
   `Upload-Offset`). `received_bytes` is read from storage, so it is right after a portal restart.
   In `direct` mode, `parts` lists the parts storage has: `{"number", "size"}`.

3. **Send the rest**, in chunks of exactly `chunk_size` bytes except the last:
   - `mode: "portal"`: `PUT /api/uploads/<upload_id>?offset=<received_bytes>` with the chunk as
     the body (`application/octet-stream`). The answer is `{"received_bytes", "total_bytes"}`. A
     chunk at another offset is refused with `409` and the `received_bytes` to resume at.
   - `mode: "direct"` (S3 storage the PC may reach): `POST /api/uploads/<upload_id>/parts` with
     `{"parts": [1, 2, …]}` (at most 100) answers `{"urls": {"1": "https://…"}, "expires_in": 900}`.
     PUT part *n* (bytes `(n-1)·chunk_size` onwards) to its URL, without the portal's headers.
     The URLs are HTTPS (HTTP only for storage on the same machine).

4. **Finish**: `POST /api/uploads/<upload_id>/complete`. The portal reads the whole file back and
   checks its SHA-256. Only when it matches is the upload `confirmed` (`200`, with
   `"artifact": {"sha256", "size", "kind": "restricted_evidence", "created_at"}`); the recorder
   may then delete its copy. A mismatch answers `422`, the upload becomes `rejected` with
   `last_error`, and what was received is deleted: init again to send the file again. Complete
   before everything arrived answers `409` with `received_bytes`.

States: `receiving`, `verifying`, `confirmed`, `rejected`. An expired upload token answers `401`
with `"expired": true`: init again to resume.

The portal's own pages read an upload's progress at `GET /api/recordings/<recording_id>/upload`
(signed-in users, not the recorder): `{"state", "received_bytes", "total_bytes", "last_error"}`,
`state` `none` when the recording has no upload.

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

- **TwinLabRecorder** (`recorder/`, version 0.2.0 or later) speaks contract 1.x. The references are
  built by the collectors' timeline (`twinlab2_recorder.workstation.timeline.outbox`, copied from
  Twin Lab 90bdd91, [UPSTREAM.md](UPSTREAM.md)). Since Twin Lab `4385949` that outbox declares `contract_version` 1.1.0 itself, with
  event ids assigned at capture time and a completeness manifest. The recorder stamps itself on
  the outbox, then validates it, before it sends (`twinlab2_recorder.main.contract_references`). An outbox
  without a version (an older Twin Lab) gets the version its shape is (1.1.0 when it reports
  `completeness`, else 1.0.0). Twin Lab also writes 1.1.0 when it rebuilds a session recorded
  before event ids: if no step has an `event_id`, the recorder sends it as 1.0.0 without
  `completeness` (that manifest was computed without a capture journal), and the portal derives
  stable ids at ingest. A recording in which only some steps have ids is refused.
- **The portal** (`twinlab2.registry`) checks every pairing and upload with `recorder_contract.validate`.
  `twinlab2.uploads` serves recording uploads and stores them with `twinlab2.artifacts`.
- **The recorder's upload queue** (`twinlab2_recorder.upload_queue`, commands `upload` and `uploads`) keeps
  each upload on disk, resumes it after a restart or a network failure, and deletes its copy only
  once the portal confirms.

## Changing the contract

1. Change the schema and the tables above. Set `x-contract-version` in the schema and
   `CONTRACT_VERSION` in `twinlab2_contract.recorder_contract` to the new version (a test checks that they
   agree), and `CONTRACT_VERSION` in `twinlab2_recorder.workstation.timeline`, the version the
   collectors write on every outbox (a test checks that one too). A minor version whose new fields are required of its senders
   gets its own definition (as `references_1_1`), chosen by `recorder_contract.definition`.
2. For a MAJOR change, decide whether the portal still accepts the old MAJOR for a while. If it
   does, handle both versions explicitly. If it does not, raise `OLDEST_SUPPORTED`.
3. Add samples to `tests/recorder/test_contract.py`.
4. When the change comes from Twin Lab's collectors, move the pin (`UPSTREAM.md`) and regenerate
   `tests/fixtures/references-from-the-collectors.json` in the same commit.
