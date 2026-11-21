# API versioning and conventions

TwinLab2 has two HTTP APIs with separate versions:

| API | Paths | Who calls it | Versioned by | Contract |
| --- | --- | --- | --- | --- |
| Portal API | `/api/v1/...` | the web app (`web/`) and scripts | the path (`v1`) and `info.version` of the spec | [api/openapi.yaml](../api/openapi.yaml) |
| Recorder agent API | `/api/agent/...`, `/api/uploads...` | the Windows recorder | the recorder contract version (`CONTRACT_VERSION`, now 1.4.0) | [RECORDER-CONTRACT.md](RECORDER-CONTRACT.md) |

## What `/api/v1` guarantees

Within `/api/v1` the API changes **only additively**. A client written against an earlier v1 keeps
working:

- **Added (allowed):** new operations; new optional request members, query parameters and headers;
  new response members; new values of `ErrorCode`; new values of response enums such as states and
  kinds. Clients ignore response members they don't know, and treat an unknown `code` by its HTTP
  status.
- **Not within v1:** removing or renaming an operation or member, making an optional request member
  required, narrowing what a request member accepts, changing a member's type or meaning, or
  changing a success status. Any of these needs `/api/v2`, served next to v1 for a transition that
  is announced in the README and in [design.md](design.md).
- **Relaxing is additive:** a required request member may become optional. For example, the build
  operations' `request_key` became optional when `Idempotency-Key` arrived.
- **Deprecation:** a member or operation that has a replacement is marked `deprecated: true` in the
  spec. Its description names the replacement, and a response that carries it sends
  `Deprecation: true`. It keeps working for all of v1. The first one is `RecordingSummary.apps`, a
  single string, replaced by `apps_list`: `[{app_id, name, mode}]`.
- `info.version` of the spec is semver for the document: minor for additions, patch for
  description fixes. The major version stays 1 while the path is `/api/v1`.

The generated clients and models (`web/src/api/schema.ts`, `server/twinlab2/api_models.py`) are
produced from the spec. CI fails when either is out of date, and every server test checks every
`/api/v1` response against the spec (`tests/contract.py`), so the server cannot drift from it.

## How the recorder contract relates

The recorder contract is versioned on its own because recorders are deployed to managed PCs and
updated slowly. It uses semver:

- A portal accepts recorders whose contract has the same major version and is no older than
  `OLDEST_SUPPORTED` (now 1.0.0). A newer minor or patch version is accepted, and the portal reads
  what it knows. A recorder whose contract the portal does not support is refused, and the refusal
  says which side to update.
- The recorder contract and `/api/v1` don't constrain each other. A new recorder contract minor
  (for example, the screen structure of 1.3.0) shows up in `/api/v1` only as additive response
  members. A breaking recorder contract change (2.0.0) does not imply `/api/v2`, and a v2 portal
  API does not imply a new recorder contract.
- Each recording keeps the recorder version and the contract version it was sent with, so evidence
  from older recorders stays readable after upgrades.

## Conventions every `/api/v1` operation follows

Details and examples are in the spec's description. In short:

- **Errors** are RFC 9457 `application/problem+json`, with a stable `code` (`ErrorCode`) and
  `type: /errors/<code>`. `GET /errors/<code>` describes a code. Clients act on `code`, never on
  `detail`.
- **`Request-Id`** is on every response and in every log line. A caller's own id, if it is a short
  token, is kept.
- **`Idempotency-Key`** works on the create-type POSTs (`x-twinlab-idempotent` in the spec). The same
  key with the same request is replayed with `Idempotent-Replayed: true`. The same key with a
  different request gets 422. A request that is still running gets 409. Keys are kept for 24 hours
  for each caller.
- **Conditional updates** use `revision` with `If-Match` or `expected_revision`, and answer 409
  `conflict` with `current` when stale.
- **Conditional GETs**: the build-chat polling endpoints send an `ETag` and answer 304 to a matching
  `If-None-Match`.
- **Pagination** uses `limit` and `offset`, with a `page` object in the response.
