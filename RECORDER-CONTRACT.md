# Recorder contract 1.0.0

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
{"error": "this recorder speaks contract 2.0.0, newer than this portal's 1.0.0 (supported >=1.0.0 <2.0.0). Update the portal, or use a recorder for contract 1.x.",
 "update": "portal", "contract": {"portal": "1.0.0", "supported": ">=1.0.0 <2.0.0"}}
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
{"contract_version": "1.0.0", "code": "ABCDE-FGHJK", "name": "Workstation 7",
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

### Recording uploads: `/api/uploads`

A recording file (TwinLabRecorder sends the session folder as a ZIP) goes up only when the person
uploads it, through a resumable upload. This API is an addition within contract 1.x: it changes
no message above, and a portal without it answers `404`, which leaves the upload queued on the PC.
The portal checks `contract_version` at init exactly as for pairing (see Version).

1. **Init**: `POST /api/uploads` with the machine token.

   ```json
   {"contract_version": "1.0.0", "session_id": "20261119T100000Z-0a1b2c3d", "size": 5242887,
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

## Who implements it

- **TwinLabRecorder** (`agent/`, version 0.2.0 or later) speaks contract 1.0.0. The references are
  built by Twin Lab's collectors (`labctl.workstation.timeline.outbox`) at the commit pinned in
  `twinlab.lock.json`. That outbox does not carry `contract_version` yet. The recorder stamps the
  version and itself on the outbox, then validates it, before it sends (`agent.main.contract_references`).
- **The portal** (`registry.py`) checks every pairing and upload with `recorder_contract.validate`.
  `uploads.py` serves recording uploads and stores them with `artifacts.py`.
- **The recorder's upload queue** (`agent/upload_queue.py`, commands `upload` and `uploads`) keeps
  each upload on disk, resumes it after a restart or a network failure, and deletes its copy only
  once the portal confirms.

## Changing the contract

1. Change the schema and the table above. Set `x-contract-version` in the schema,
   `CONTRACT_VERSION` in `recorder_contract.py` and `contract_version` in `twinlab.lock.json` to the
   new version. A test checks that they agree.
2. For a MAJOR change, decide whether the portal still accepts the old MAJOR for a while. If it
   does, handle both versions explicitly. If it does not, raise `OLDEST_SUPPORTED`.
3. Add samples to `tests/test_contract.py`.
4. When the change comes from Twin Lab's collectors, move the pin (`UPSTREAM.md`) and regenerate
   `tests/fixtures/references-from-pinned-twinlab.json` in the same commit.
