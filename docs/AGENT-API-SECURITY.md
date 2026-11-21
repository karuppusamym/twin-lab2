# Agent API security: credentials, scopes and rate limits

The recorder's agent API (`/api/agent/*`, `/api/uploads*`) is the part of the portal that is
reachable without a person's sign-in: on OpenShift the OAuth proxy lets these paths through
(`--skip-auth-regex`), and the portal checks machine credentials itself
(`server/twinlab2/agentauth.py`, `routes_registry.agent_machine`, `uploads.get_upload`).

## Three credentials

| Credential | Made by | Used for | Lifetime | Stored as |
| --- | --- | --- | --- | --- |
| Pairing code (10 characters, `XXXXX-XXXXX`) | A signed-in user, *Machines → Make a pairing code* (`POST /api/v1/machines/pairing-codes`) | Only `POST /api/agent/pair`, in the body. Never accepted as a bearer token. | One use, 10 minutes | SHA-256 |
| Machine token | Pairing | `Authorization: Bearer` on the agent API | Until the machine is revoked (*Machines → Revoke*) | SHA-256 |
| Upload token | `POST /api/uploads` (needs a machine token with `upload`) | The chunks, parts, status and completion of that one upload | Hours (the recorder initialises again to resume) | SHA-256 |

Wrong pairing codes are limited per portal: after `registry.FAILURES_PER_10_MIN` wrong codes in
ten minutes, `/api/agent/pair` answers 429 until the window passes.

## Token scopes

A pairing code carries the scopes its machine gets. The user who makes the code chooses them
(both by default):

| Scope | Allows |
| --- | --- |
| `record` | `GET /api/agent/sessions` (sessions offered to the machine, keys not to record), `GET /api/agent/apps`, `POST /api/agent/references` (a finished recording's applications, steps, timeline and completeness) |
| `upload` | `POST /api/uploads` (start an evidence upload), and for that upload's token: `GET/PUT /api/uploads/{id}`, `POST /api/uploads/{id}/parts`, `POST /api/uploads/{id}/complete` |

A machine paired before scopes existed (migration 15) holds both. The *Machines* page and
`GET /api/v1/machines` show each machine's `scopes`.

A call without the scope answers **403** in the agent API's shape:

```json
{"error": "this machine's token may not upload evidence: pair the machine again with a code that allows 'upload'", "scope": "upload"}
```

The recorder's upload queue treats 403 as "a person has to act" and stops retrying that job. To
change a machine's scopes, revoke it and pair it again with a new code.

When to narrow them: a machine that only re-sends recordings captured elsewhere needs `upload`
only; a machine that records but whose evidence must never leave it (references only) needs
`record` only.

## Per-machine rate limit

Every authenticated agent call (machine token or upload token) counts against its machine:
`TWINLAB2_AGENT_RATE` requests a minute (default **600**; `0` turns the limit off). The count is a
fixed one-minute window kept in the database (`agent_rate`), so every replica and worker shares
it. A request over the limit answers **429** with `Retry-After` (seconds to the next minute):

```json
{"error": "too many requests from this machine (600 a minute): retry in 17 s", "retry_after": 17}
```

The recorder's upload queue retries a 429 later with its usual backoff (5 s, doubling, at most
10 minutes). Requests with an unknown or revoked token are refused with 401 before they are
counted, so they cannot use up a paired machine's allowance. 600 a minute leaves room for an
8 MiB-chunk upload at full speed (one request per chunk) while stopping a runaway loop or a
copied token from flooding the portal.

What it does not cover: a flood of requests with made-up tokens (401s) is not limited by the
portal; put the route's or the proxy's connection and request limits in front of it for that.
`/api/agent/pair` without a valid code is limited by the wrong-code counter above.

## Related settings

- `TWINLAB2_AGENT_RATE`: requests per machine per minute (default 600, 0 = off).
- Pairing code lifetime and wrong-code limit: `registry.CODE_MINUTES`, `registry.FAILURES_PER_10_MIN`.
- The listening address and auth mode check at start (`auth.bind_check`): see the README's
  *Sign-in* section and [deploying-on-openshift.md](deploying-on-openshift.md).
