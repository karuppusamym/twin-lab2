# TwinLab2 design

How TwinLab2 works and why: the portal, the recorder, storage, access, build sessions, releases
and what has been tested. To run the demo and use the portal, start with the [README](../README.md).

TwinLab2 helps a demo owner turn examples of an application into a runnable, versioned demo. The owner opens an app in the catalog, adds recordings and requirements, reviews what the demo should do, and gives the resulting build brief to a coding agent. A customer of the catalog can download a release or open a deployed demo.

## The portal

The portal is one ASGI application (FastAPI, `server/twinlab2/app.py`) that serves the JSON API
`/api/v1` ([api/openapi.yaml](../api/openapi.yaml)), the recorder's agent API (`/api/agent/…`,
`/api/uploads…`), the downloads and the built React app (`web/dist`; every other page address
answers `index.html`, so `/apps/1` or `/login` open the app's own page). `python -m twinlab2.portal`
runs it with uvicorn (`--port`, `--host`, `--database`, `--reload`); a server runs
`gunicorn -k uvicorn_worker.UvicornWorker "twinlab2.app:asgi()"` (or `uvicorn --factory
twinlab2.app:asgi`). The build worker is its own process: `python -m twinlab2.worker`.

**Working on the web app.** Start the portal (`python -m twinlab2.portal`), then in `web/` run
`npm run dev` and open `http://localhost:5173`: Vite serves the app with hot reload and forwards
`/api` and `/artifacts` to the portal on 127.0.0.1:8765 (`TWINLAB2_PORTAL` changes it). A change to
`api/openapi.yaml` is followed by `npm run gen` (the web types) and
`python tools/gen_api_models.py` (the server's Pydantic models); CI fails when either is stale.

`requirements.txt` installs the two packages the portal is made of, editable: `contract/` (what the
portal and the recorder share) and `server/` (the portal). It is the same as
`python -m pip install -e ./contract -e ./server`. Nothing else is fetched: TwinLab2 carries the
Twin Lab code it runs ([docs/UPSTREAM.md](UPSTREAM.md)).

| Folder | What it is |
|---|---|
| `server/twinlab2/` | the portal: the ASGI app (`app`, `rest`, `auth`, `routes_*`, `api_models` generated from the spec), the domain modules (`portal`, `db`, `migrations`, `registry`, `packages`, `buildpack`, `builds`, `verification`, `artifacts`, `audit`, `twinlab2/providers/`, `worker`, ...) and Verify's replay (`replay/`) |
| `recorder/twinlab2_recorder/` | TwinLabRecorder and its collectors (`workstation/`, `desktop/`, `web/`, `botlog/`); `recorder/build.py` builds the exe |
| `contract/twinlab2_contract/` | what both sides share: the recorder contract and its schema, and the recording format (`evidence/`) |
| `api/`, `web/` | the `/api/v1` contract (`api/openapi.yaml`) and the React web app the portal serves |
| `tools/` | `gen_api_models.py`: the server's request and response models from the contract |
| `deploy/` | the Dockerfile, Docker Compose and OpenShift files |
| `docs/` | the guides: recorder contract, recording guide, verification results, Twin Lab provenance, plan, [API versioning](api-versioning.md), [deploying on OpenShift](deploying-on-openshift.md) (not yet run on a cluster) |
| `tests/server`, `tests/recorder` | the test suite (`python -m pip install -r requirements-test.txt`, then `python -m pytest`) |


**A recording's workboard.** A recording's page is one table with a row per application it found: identity (surface, host and path or program file), scope, steps, review, scenarios, package readiness (blockers and warnings), build and release, and one next action; then a timeline with one swim lane per application over the recording's time, and a note on how applications that share a host were told apart (the longest path prefix). Preflight, upload, channels and the raw step tables are under **Details**; **Download all approved packages** gives one ZIP.

**Who may add applications** is `TWINLAB2_APP_CREATORS`: `owners` (default: portal admins and anyone who already owns an application), `admins`, or `anyone`. A release is registered from a build session's commit, or any commit of the demo repository, with **Register a release** on the Build page (`POST /api/v1/apps/{id}/releases`); the `release` command below stays for scripted use. Build providers that are not ready are listed only to admins, and the stand-in provider only on a demo portal (`TWINLAB2_DEMO=1`).

A recording can also register applications it finds; they *need a scope decision*. **Scope triage** (from Home, the navigation or a workboard) shows one card per such application with its identity, steps, sample screens and controls and what it looks like (same host, same or similar name, same vendor, similar program file), and three choices: **Replicate**, **Same as …** (a merge, previewed before anything changes) or **Not replicated (used only)**, for one card or all selected ones; replicate and not replicated can be reversed. The statuses are `known` (Replicate), `discovered` (Needs a scope decision; never an owner's choice) and `not_replicated` (Not replicated, used only): the mapping is in `journey.STATUS_LABELS`, `web/src/lib/scope.ts` and `AppStatus` in the API spec. For an existing multi-app example, use `../rebuild-trial/judge/recording/order-to-invoice/steps.jsonl`.

The portal is local and writes its catalog to `.state/catalog.db` (SQLite). Use `--database` (a file or a `DATABASE_URL`) and `--port` to override those defaults. A shared deployment uses PostgreSQL instead: see [The portal database](#the-portal-database). Install `requirements-test.txt` (adds `moto` for S3 without AWS) and run `python -m pytest` to check the catalog, recording recovery, artifact storage and release workflows. [Verification results](VERIFICATION.md)

**Artifact storage** (`twinlab2.artifacts`). Evidence files, frozen build packs, verification reports, release downloads and uploaded recordings are stored as immutable objects; the catalog keeps only references (backend, bucket, key, version, SHA-256, size, type, kind), never an expiring URL. By default they are files beside the catalog (`.state/artifacts`, or `TWINLAB2_ARTIFACT_DIR`); a portal on PostgreSQL keeps them under `.state/pg-<id of the database>/artifacts`, so a shared deployment sets S3 (or `TWINLAB2_ARTIFACT_DIR` on a volume). For S3 or an S3-compatible store such as MinIO set `TWINLAB2_ARTIFACT_BACKEND=s3`, `TWINLAB2_S3_BUCKET`, and as needed `TWINLAB2_S3_PREFIX`, `TWINLAB2_S3_REGION`, `TWINLAB2_S3_ENDPOINT_URL` and `TWINLAB2_S3_DIRECT_UPLOAD=0` (recorders cannot reach the bucket: uploads go through the portal). Enable versioning on the bucket. Credentials come from boto3's usual environment, profile or role and are never shown; **Setup / Advanced** shows the rest. Downloads from S3 are presigned URLs valid for five minutes. Catalogs from before this keep their SQLite blobs working: every start copies blobs not yet copied and checks each copy's SHA-256 before using it, and `python -m twinlab2.portal artifacts --purge` then empties the blobs whose copy verifies. Recordings are uploaded with the resumable API in [RECORDER-CONTRACT.md](RECORDER-CONTRACT.md#recording-uploads-apiuploads) (`TwinLabRecorder upload <session-id>`).

## What works today

Each capability carries one of three labels:

- **Implemented**: the code is written and has unit tests where noted, but nobody has exercised it end to end.
- **Locally tested**: exercised end to end on the developer's own Windows PC, by the automated tests or a manual run.
- **Externally validated**: confirmed in a target environment by its users (a managed PC, an OpenShift cluster, a clean machine, a fresh agent build).

**Nothing is externally validated yet.** No recorder trial on a live managed PC and no OpenShift deployment have been done. A blind agent rebuild from exported packs and a clean-container install were done once, locally, on fixture apps (the Orders-to-Billing milestone in [VERIFICATION.md](VERIFICATION.md)); the earlier Orders/Billing release replay packaged previously built fixture apps.

| Capability | Status | Evidence and limits |
|---|---|---|
| Portal journey: stage, missing information and one next action per application; Setup / Advanced for machines, identities, Git and deployment settings | Locally tested | `tests/server/test_journey.py`; pages rendered from a local run |
| Catalog: applications, Twin Lab recording import, evidence links, reviewed attachments | Locally tested | `tests/server/test_workflow.py` |
| Build pack export | Locally tested | Automated tests check its contents and immutable revisions. In the Orders-to-Billing milestone, blind Claude subagents built both apps from exported packs alone; the v1 pack lacked interface structure the replay compares, which an owner note closed by v3 |
| Release registration from a committed Git tree, release download | Locally tested | Automated tests; the Orders/Billing replay used previously built fixture apps, not a fresh rebuild |
| Release states Packaged → Tested → Published; deployment version check | Locally tested | `tests/server/test_release_states.py`; the journey's Verify step is done once the latest release is Tested, Open or download once it is Published. No real deployment has answered the version check |
| Tested bound to the built commit: required GitHub checks, Windows validation, Twin Lab replay/outcome/reset, verification report | Locally tested | `tests/server/test_verification.py` against a fake GitHub API and a fake replay runner; the copied replay and reset check were run locally on the Orders-to-Billing v3 build (PASS WITHIN DECLARED SCOPE, 8/8 cases, reset pass). Never run against the real GitHub API or a real built app |
| Deployment registration (URL, demo username, secret reference) | Locally tested | Automated tests register a URL; no demo has been deployed |
| OpenShift manifest for a demo (`twinlab2.openshift`) | Implemented | Manifest wiring checked locally; never applied to a cluster |
| Portal on OpenShift (`Dockerfile`, `deploy/openshift-portal.yaml`) | Locally tested (image), Implemented (manifest) | The image ran locally as an arbitrary UID; the manifest, OAuth proxy, route and volume have not run on a cluster |
| Machine pairing, agent API, references mapped to applications | Locally tested | `tests/server/test_registry.py`, `tests/recorder/test_agent.py` |
| Evidence integrity: a resend keeps the reviewed selection, merge with a preview, web identity by path | Locally tested | `tests/server/test_integrity.py` |
| Business scenarios and build-pack preview | Locally tested | `tests/server/test_buildpack.py`; no coding agent has built from these packs yet |
| Portable recorder (built program): web, desktop and synthetic mainframe | Locally tested | One developer PC, unsigned build; no managed PC, endpoint protection or AppLocker; no real mainframe or emulator |
| UiPath: targets from a project, the robot's log | Implemented | Unit tests in Twin Lab; no real UiPath robot has been recorded |
| Recording recovery after an interrupted capture | Locally tested | Fixture logs (`tests/server/test_workflow.py`) |

## TwinLabRecorder: a portable recorder that tells the portal what it recorded

`TwinLabRecorder.exe` is one folder: copy it to a Windows PC and run it, with no install and no administrator rights. It records one session across the applications a person or a robot uses. The session's **target** applications are recorded in full. Every other application is recorded only as having been used, and Windows itself, Adobe, Outlook, Teams and password managers always stay that way. **The recording stays on the PC.** The portal receives only references: the applications recorded and each one's steps as labels (control ids and the application's own words, never a typed value). It maps the recording to its applications and registers an application it has not seen as *discovered*. Design: `twin-lab/docs/11-session-recorder.md`.

1. In the portal, **Setup / Advanced → Machines and pairing → Make a pairing code**. On the PC, run `TwinLabRecorder.exe` and choose **Pair with portal…** (or `TwinLabRecorder.exe pair --portal https://… --code ABCDE-FGHJK --name "Workstation 7"`).
2. Give each application an identity on its **Setup / Advanced** page (its executable, such as `CaseDesk.exe`, or its web address), then **Request a recording**: name, machine, target applications (full or steps only), and optionally the automation it is for. Or start a session on the recorder itself (**My own session**).
3. On the PC, choose the requested session and **Start**. The recorder opens its own Edge window for web targets; use the applications as the process does. **Stop recording** when done.
4. Review what will be sent (**Recordings on this PC**) and **Send references**. The portal's **Recordings** page shows the session, its applications and the timeline across them; an application's page shows its recorded flows and the automations that touch it.

Applications that share a host are told apart by path: give each one its address with its path (`https://example.test/orders`, `https://example.test/billing`) and the portal assigns each recorded web step by the path of its page, the longest matching path winning; a host-only address (`https://example.test`) takes the steps no path claims. The recorder still records the whole origin. On an application's page **Web addresses match → the whole host** is the owner's override for an application that is its whole host.

The first time a recording arrives, each application's link selects all of its steps (*not yet reviewed*). Once the owner saves the assignment on the recording's page, sending the recording again never changes that selection: events new in the resend are shown as *new, not yet reviewed* and stay out of the build pack until the owner selects them.

**Merge** on an application's page opens a preview first: the identities, recorded flows, linked recordings, automations, scenarios and build briefs that move, and every recording both applications link, with the combined selection and notes. A reviewed selection is not widened by an unreviewed one; those events become suggestions. Earlier build briefs and their downloads move to the surviving application. An application with releases or attachments cannot be merged.

Command line: `sessions`, `record --session N` (or `--exe X.exe --origin https://…`), `show <id>`, `send <id>`, `recover`. Add `--portable-data` to keep everything in `data\` beside the program, `--ca-bundle` for a private certificate authority. Recordings are in `%LOCALAPPDATA%\TwinLabRecorder\recordings\`.

Build the folder from `recorder/` (its collectors are this repository's; no twin-lab checkout or download is needed):

```powershell
python -m pip install -r requirements-recorder.txt
python recorder\build.py
```

The recorder and the portal speak the versioned [recorder contract](RECORDER-CONTRACT.md), now 1.4.0: events carry ids assigned at capture time, each recording reports how complete it is, a web step keeps the path prefix of the catalog application it is under, so applications at `/finance/billing` and `/finance/payroll` on one host are told apart, and each application's screens come with their structure (controls with their ids, classes and enabled/read-only/required/visible states, regions shown or hidden, grids with column headers and row counts as shapes, transitions), which the build package carries as "Screens and controls" (1.0.x to 1.3.x recorders are still accepted; see [RECORDER-CONTRACT.md](RECORDER-CONTRACT.md) for what 1.4 added). The portal refuses a recorder whose contract it does not support, and it says which side to update. Recorders built before 0.2.0 declare no version: rebuild and redeploy them. Each recording keeps the recorder version and the contract version it was sent with. [Where the collectors came from](UPSTREAM.md)

The build is not signed. Sign it before it runs on any machine other than a developer's: an unsigned program with keyboard hooks is likely to be stopped by endpoint protection.

**The portal on OpenShift.** `deploy/Dockerfile` (built from the repository root: `docker build -f deploy/Dockerfile .`) and `deploy/openshift-portal.yaml` run the portal behind the OpenShift OAuth proxy (people sign in with OpenShift; recorders use their machine token on `/api/agent/`). The image runs in the `trusted-proxy` auth mode (see **Sign-in** below) and refuses to start without `TWINLAB2_SECRET_KEY`. Settings: `DATABASE_URL` (PostgreSQL, see [Shared deployment](#shared-deployment)), `TWINLAB2_SECRET_KEY`, `TWINLAB2_HOSTS`, `TWINLAB2_DATABASE` (the SQLite file when there is no `DATABASE_URL`), `TWINLAB2_AUTH_MODE`, `TWINLAB2_TRUSTED_PROXIES`, `TWINLAB2_SESSION_HOURS`.

## The portal database

One setting chooses it: `DATABASE_URL`.

- **Local development and tests: SQLite.** Without `DATABASE_URL` the portal uses the file `TWINLAB2_DATABASE` (default `.state/catalog.db`). `--database` takes a file path or a URL. Nothing to install.
- **Shared deployment: PostgreSQL.** `DATABASE_URL=postgresql://user:password@host:5432/twinlab2` (add `?sslmode=require` for a remote server). `psycopg` is in `requirements.txt`. The portal keeps no other state, so several workers or replicas can share one database.

`twinlab2.db` is the only module that talks to a driver. Its connection works like `sqlite3`'s on both engines: `?` placeholders, rows by index or name, `with connection:` as one transaction, and `db.IntegrityError` for either engine. `twinlab2.migrations` holds the schema as numbered migrations. They are recorded in `schema_migrations`, and the portal applies pending ones at every start (on PostgreSQL under an advisory lock). Migration 1 is the baseline, and it also upgrades a catalog made by an older portal.

```powershell
python -m twinlab2.migrations status --database postgresql://twinlab2:…@host/twinlab2
python -m twinlab2.migrations migrate
```

### Shared deployment

1. Create a PostgreSQL 13+ database and an owner role for TwinLab2. Use `C` collation (`createdb --locale=C --template=template0 twinlab2`) so lists sort the way they do on SQLite.
2. Give the portal `DATABASE_URL`. On OpenShift, put it in the `twinlab2` secret as `database-url`; [deploy/openshift-portal.yaml](../deploy/openshift-portal.yaml) reads it, so the pod no longer needs a volume. [deploy/docker-compose.yaml](../deploy/docker-compose.yaml) runs the portal and PostgreSQL together on one machine.
3. Store artifacts in S3 (`TWINLAB2_ARTIFACT_BACKEND=s3`, `TWINLAB2_S3_BUCKET`, see **Artifact storage** above) or in a folder every replica and the worker share (`TWINLAB2_ARTIFACT_DIR`). The database holds only their refs.
4. To move an existing portal, copy its SQLite catalog into the new, empty database before the first start. The copy migrates both databases, copies every table in foreign-key order, moves the identity counters past the copied ids and checks the row counts. It refuses a database that already has rows. Artifact files are not copied: the copy names the folder that holds them. Point `TWINLAB2_ARTIFACT_DIR` at it, or at a copy.

```powershell
python -m twinlab2.migrations copy .state/catalog.db "postgresql://twinlab2:…@host:5432/twinlab2"
```

### Tests on both engines

`python -m pytest` runs on SQLite. Set `TWINLAB2_TEST_DATABASE_URL` to run the same suite on PostgreSQL. Every test gets a new database on that server, and the databases are dropped at the end of the run:

```powershell
docker run -d --name twinlab2-pg -e POSTGRES_PASSWORD=twinlab2 -e POSTGRES_INITDB_ARGS="--locale=C --encoding=UTF8" -p 55432:5432 postgres:16
```

```powershell
$env:TWINLAB2_TEST_DATABASE_URL = "postgresql://postgres:twinlab2@localhost:55432/postgres"; python -m pytest
```

CI ([.github/workflows/test.yml](../.github/workflows/test.yml)) runs the suite on both.

### Portable SQL rules

Follow these rules in every new query and table so they work on SQLite and PostgreSQL. CI on both engines catches what they miss.

- **Schema changes go in `twinlab2.migrations`**: append a new numbered migration and never edit one that has shipped. Don't use `CREATE TABLE` or `ALTER TABLE` anywhere else, and don't put `SCHEMA` strings in modules.
- **DDL types:** `{id}` for an integer primary key the database assigns, `{blob}` for bytes, `TEXT` for strings and JSON, `INTEGER` for counts and flags (0/1, not `BOOLEAN`). Timestamps are `TEXT` in UTC ISO-8601 from `registry.now()`, so they compare as strings on both engines. Every table with a foreign key to `apps(id)` names it with `REFERENCES`, because the application merge checks for such keys.
- **Placeholders:** always `?`. Don't build SQL from values with f-strings; f-strings are only for table and column names from code.
- **Inserts name their columns** (`INSERT INTO t(a, b) VALUES (?, ?)`). Never use `INSERT INTO t VALUES (…)`, because column order differs between a migrated catalog and a new one.
- **New ids:** `INSERT … RETURNING id`, then `.fetchone()[0]`. Never use `cursor.lastrowid`, which is SQLite only.
- **Upserts:** `ON CONFLICT DO NOTHING`, or `ON CONFLICT(key columns) DO UPDATE SET c = excluded.c`. Never use `INSERT OR IGNORE`, `INSERT OR REPLACE` or `REPLACE INTO`.
- **No engine-specific functions or catalog queries:** use `string_agg(x, ', ')` (not `group_concat`), `COALESCE`, `substr`, `||`, `count(*)`. Use `connection.tables()`, `columns(t)` and `foreign_keys(t)` instead of `PRAGMA` or `sqlite_master`. Don't use `strftime`, `datetime('now')`, `now()`, `ILIKE` or `::` casts.
- **NULL ordering differs between engines**, so order a nullable column with `NULLS FIRST` or `NULLS LAST`.
- **Types are strict on PostgreSQL:** compare a column with a value of its own type (an `INTEGER` id with an `int`, not `'3'`). `LIKE` is case-sensitive on PostgreSQL. `EXISTS(…)` and comparisons return `True`/`False` there and `1`/`0` on SQLite, so use them as truth values only.
- **Transactions:** wrap each change in `with db():`. On PostgreSQL a failed statement aborts the rest of the transaction, so don't catch an error and keep using the same transaction. Roll back first, or check before you write.
- **Large files** (packs, release archives, attachments) are `{blob}` columns for now. They move to object storage with artifact references.

## Access, evidence and builder packages

**Roles are enforced by the server** (`twinlab2.auth`). Per application a user is an `owner` (reviews restricted evidence, writes scenarios, freezes packages, records tests, verifies and publishes releases, assigns roles), a `builder` (an owner's read access minus evidence, plus the frozen packages, starting builds and re-checking deployments) or a `viewer` (the application page without evidence, its releases, published downloads). A **portal admin** manages users and every application's roles but does not read evidence unless they assign themself owner (audited). Whoever creates an application owns it; an application a recorder discovers is owned by whoever paired that machine; the old free-text Owner field became an owner assignment. Every route is added with its policy, `require_role(...)` (or `public` / `signed_in` / `agent_token`), which runs as the route's FastAPI dependency; a route without one answers 403, and a test lists every route's policy. Every `/api/v1` request that changes something sends the session's `X-CSRF-Token`.

**Sign-in.** `TWINLAB2_AUTH_MODE` names one of three modes; the portal logs it at start, and `GET /api/v1/session` (`access_mode`) and the *Users* page show it:

| Mode | Who is signed in |
|---|---|
| `local-dev` | Only this machine: loopback requests are the admin user `local`, every other request is refused. Refused (at start and per request) once local users with passwords exist, unless `TWINLAB2_LOCAL_DEV_FORCE=1`. |
| `individual` | Everyone with their own password: the local user store (`python -m twinlab2.portal user NAME --password`, or *Users* in the portal), the web app's sign-in page. No loopback admin, proxy headers ignored. `TWINLAB2_PASSWORD`, the shared demo password, signs in only as the one demo identity `TWINLAB2_SHARED_USER` (default `demo`): never an admin (a start with it named an admin is refused), never a name that has its own password, and only with the roles an owner assigns to it. Named owners, builders and admins use their own credentials or SSO. |
| `trusted-proxy` | Identity only from the SSO / OAuth proxy's `X-Forwarded-User` (or `X-Forwarded-Preferred-Username`; groups from `X-Forwarded-Groups`), and only on requests whose peer is in `TWINLAB2_TRUSTED_PROXIES` (addresses or networks, default `127.0.0.1,::1`: the sidecar in the same pod). Anything else is 403. |

Without `TWINLAB2_AUTH_MODE` the mode is derived: `trusted-proxy` with the older `TWINLAB2_TRUST_PROXY_USER=1`, `individual` when `TWINLAB2_PASSWORD` is set or a local user has a password, else `local-dev`. Outside `local-dev` the portal refuses to start without `TWINLAB2_SECRET_KEY` (the same value on every replica). A sign-in is a server-side session (`auth_sessions`) that ends after `TWINLAB2_SESSION_HOURS` (default 12), at sign-out, and when the user's password changes: a copied cookie stops working then too. Admins come from `TWINLAB2_ADMINS` (comma separated), `TWINLAB2_ADMIN_GROUP` (matched against `X-Forwarded-Groups`) or `python -m twinlab2.portal user NAME --admin`. To take identity from elsewhere (JWT, mTLS), replace `auth.proxy_identity`.

**Listening beyond this machine.** At start the portal checks the address it listens on (`TWINLAB2_BIND_HOST`, set by `python -m twinlab2.portal` and `tools/demo_portal.py`, else gunicorn's `--bind` / uvicorn's `--host`) against the auth mode (`auth.bind_check`). On a loopback address nothing is checked. Elsewhere: `trusted-proxy` that trusts a non-loopback `TWINLAB2_TRUSTED_PROXIES` network without `TWINLAB2_PROXY_SECRET` **refuses to start** (anyone on that network could send `X-Forwarded-User`); set `TWINLAB2_PROXY_SECRET` and have the proxy send it as `X-TwinLab-Proxy-Secret`, trust only the loopback sidecar, or listen on 127.0.0.1 (`TWINLAB2_ALLOW_UNSAFE_BIND=1` turns the refusal into a warning). The shared password (`TWINLAB2_PASSWORD`), `local-dev`, and `trusted-proxy` with only the loopback sidecar trusted log a warning. **CSRF**: the form token (`X-CSRF-Token`, from `GET /api/v1/session`) is new at every sign-in and sign-out; the web app answers a stale-token 403 by reading the session again and sending the change once more. **Access requests**: a 403 for want of a role offers *Request access*; owners approve or decline on their Home page ([ACCESS-REQUESTS.md](ACCESS-REQUESTS.md)). **Recorder machines**: token scopes (`record`, `upload`) and a per-machine rate limit (`TWINLAB2_AGENT_RATE`, default 600 a minute): [AGENT-API-SECURITY.md](AGENT-API-SECURITY.md).

**Two artifact classes.** Recordings and uploads are *restricted evidence*: only the uploader and the application's owners see them, and uploading never shares anything. A *builder-approved package* is the only thing a builder gets: the owner previews the draft with its **pre-freeze report** ("N items excluded, reasons"; what was redacted), approves that exact SHA-256, and freezes it as revision 1, 2, … Each revision records who approved it, when, the SHA-256, the report, a change summary and a diff against the previous revision; revisions are never changed (SQLite triggers refuse it). A build session sends only a frozen revision, and releases and build verification answer one.

**Filtering beyond typed values** (`twinlab2.redact`). Attachments stay out until the owner marks them *reviewed* or *synthetic*. Shared ones are filtered: HAR files lose cookies, credential headers, query and form values, binary bodies, and sensitive fields in JSON bodies; JSON loses fields named like passwords, tokens, e-mail, card, SSN, IBAN, phone, address, birth date; images lose their metadata (PNG text/EXIF chunks, JPEG APP1–15 and comments); all text in the pack (flow labels, notes, scenarios, text files) loses e-mail addresses outside example domains, card numbers (Luhn), IBANs, SSNs, phone numbers, JWTs, bearer tokens, API keys and `password=…` values. A file that cannot be parsed is excluded.

**Audit history** (`twinlab2.audit`, *Audit history* on each application): an append-only record of who did what and when — evidence uploads, reviews and sharing, package freezes and downloads, build starts, test results, verification, publication, deployments, role and user changes, status changes and merges.

## Capture with the workstation recorder

Record with TwinLabRecorder (above): it runs the browser, desktop and mainframe collectors in one session and sends the references. The earlier `capture.py start`, which ran Twin Lab's whole command line, is gone. For a capture folder it wrote, `python -m twinlab2_recorder.capture recover .state/captures/order-demo` still rebuilds the import file from the channel logs.

Import the session's top-level `steps.jsonl`. Recovered imports retain previously linked event IDs. Review scope and attach approved API contracts, synthetic database fixtures, utility examples, visual references, or acceptance cases directly to the app. [Where the collectors came from](UPSTREAM.md)

## Build sessions (Devin, Claude, Copilot)

Three things are kept apart. A **recording** is evidence and can feed several applications. An **approved package revision** is the immutable builder package an owner froze (above). A **build session** is one provider's attempt at one package revision, from one repository and starting commit, with its own branch (`twinlab2/app-<id>/session-<n>-<provider>`), workspace, budget, conversation, outputs (pull request, head commit, cost) and verification results. One revision can have many sessions: a Devin first implementation, a Claude alternative, a Claude improvement that starts from the commit another session made.

On the application page, **Build** opens the sessions page: the exact package (the same file-by-file preview as the download), **New build session** (provider, model, package revision, repository, base branch or a previous session's commit, budget in the provider's own unit, what changed since the previous build), and the session list (state, branch, cost against budget, outstanding questions, pull request) with **Compare results**. Only a frozen revision is built, and a session sends exactly its bytes (the SHA-256 is checked again).

A session's page is its **chat**: the provider's questions and tool requests, your answers with a delivery status each (Queued, Sending, Sent, Acknowledged, **Delivery uncertain**, Not delivered, Held for package review), the provider session link, the branch, pull request and head commit, and **Interrupt** (when the provider supports it), **Cancel**, **Retry** and **New session**. Tool approvals are their own Approve / Deny control, never a chat answer. A reply that got no clear answer (a timeout, a 5xx, a worker that died mid-send, or a provider that never confirmed it) is *Delivery uncertain* and is **not resent**: check the session, then answer again. An answer marked **This changes agreed behaviour** is not sent: it becomes a proposed scenario, so the draft package changes and an owner reviews and approves it as a new revision; the session's own revision is untouched. A provider finishing is **Ready for verification**, never Tested.

| State | Meaning |
|---|---|
| Queued | Waiting for the worker |
| Starting | Preparing (Devin: uploading the pack) and starting the provider session |
| Checking whether the session started | The start got no clear answer (timeout, 5xx, or a worker that stopped mid-call). Nothing is started again until the worker has looked for the session by its tag: found, it is followed; not found, it is started once more. A provider that cannot look a start up fails the session instead of repeating it |
| Running | The provider is working |
| Waiting for your answer | The provider asked something or wants a tool approved |
| Ready for verification | Finished; the commit awaits the independent checks |
| Failed, Cancelled | Final; **Retry** makes a new session of the same provider, package, repository and starting commit |

**Idempotency and isolation.** Each New build session form carries a request key: a repeated click returns the session that request started (the key is unique in the database); **New session** deliberately starts another attempt. There is no "one active build per application" rule any more: sessions run side by side, each on its own branch and workspace. **Publishing stays coordinated**: only one release publish per application runs at a time (a lock taken before the publish; 409 while another holds it; taken over after 2 minutes if its holder died), and a release belongs to the session that first packaged its commit, so another session cannot take it over.

**Compare results** runs the same acceptance checks as release verification (the commit's required GitHub checks, and Twin Lab's independent scenario checks against a deployment reporting that commit) against each selected session's commit, under that session's package revision, and shows them side by side. Nothing is merged or published from there.

**Providers.** A provider is an integration; the model is configuration inside it. Each adapter (`twinlab2/providers/`) declares its capabilities (start, read events, reply, interrupt, cancel, collect results, find a lost start, tool approvals, acknowledgements) and the page shows only the controls it supports.

| Provider | How it runs | Budget unit | Status |
|---|---|---|---|
| Devin | Twin Lab's Devin client, copied (`twinlab2.devin`): the pack as an attachment, the session tagged, an uncertain create looked up by tag | ACU | Implemented; locally tested against a fake Devin API |
| Claude | A clarification conversation on the Messages API (official `anthropic` SDK; only BUILD_TASK.md and evidence.json), then the Claude Agent SDK editing and running code in the session's own clone, in an isolated runner process per session; TwinLab2 commits, pushes the session's branch and opens the pull request. Model from `CLAUDE_MODEL` (default `claude-opus-5-5`) | USD (`max_budget_usd`) | Implemented; locally tested with a fake agent, a fake Messages API and a local git remote; not externally validated |
| GitHub Copilot | The Copilot SDK (sessions, events, permission requests, resume) in the same runner, behind `TWINLAB2_ENABLE_COPILOT=1`. **Authentication and deployment are not validated** | premium requests (recorded, not enforced) | Implemented behind a feature flag; locally tested with a fake agent only |

**Claude and Copilot runners.** Each session's folder (`.state/…/workspaces/session-<n>`, or `TWINLAB2_WORKSPACES`) holds the approved package (checked against its SHA-256), a clone at the starting commit on the session's branch, the runner's `state.json` and `events.jsonl`, and an inbox for replies. The runner gets its secrets on stdin; the agent works only in the clone and never gets the GitHub token. Reads and edits inside the clone run; any other tool (a shell command, web access) is a tool approval in the session chat. A reply is *Acknowledged* once the runner handed it to the model; one whose Messages API call got no answer is *Delivery uncertain* and is not resent. A runner whose heartbeat stops (the machine restarted) is started again with `--resume` and continues from its state (at most twice). The worker machine needs git and `pip install -r requirements-providers.txt` (`claude-agent-sdk`, `github-copilot-sdk`); the portal needs only `anthropic`. Settings: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `ANTHROPIC_BASE_URL`, `COPILOT_GITHUB_TOKEN`, `COPILOT_MODEL`, `TWINLAB2_GIT_REMOTE` (default `https://github.com/{repo}.git`).

**Tests never call a real provider.** Devin, GitHub and the Messages API are local fakes; agents are `tests/server/fake_agents.py` (`TWINLAB2_CLAUDE_AGENT` / `TWINLAB2_COPILOT_AGENT`). `tests/server/test_live_providers.py` calls the real Claude APIs only with `TWINLAB2_LIVE_CLAUDE=1` (or `TWINLAB2_LIVE_CLAUDE_AGENT=1`) and `ANTHROPIC_API_KEY`, and is skipped otherwise: it spends money.

Sessions run in one worker process beside the portal:

```powershell
python -m twinlab2.worker --database .state/catalog.db   # or DATABASE_URL, as the portal
```

Jobs are leased and kept alive by a heartbeat; a job held by a worker that died is taken over when its lease runs out. Under **Setup / Advanced: Build sessions** an admin sees each provider's status and sets the provider credentials and the GitHub token (environment variables `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `DEVIN_BASE_URL`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `COPILOT_GITHUB_TOKEN`, `TWINLAB2_GITHUB_TOKEN`, `TWINLAB2_GITHUB_API` win over saved values; secrets are never shown), connects repositories, sets default budgets, and sees the workers and the job queue. For development the worker can be started from there. The Devin client is Twin Lab's, copied into `twinlab2.devinapi` ([docs/UPSTREAM.md](UPSTREAM.md)).

## Build, package, and register a deployment

1. Download the app's build pack (or build it in a build session, above). Give the ZIP to a coding agent along with access to the demo's Git repo. Include launch, seed, reset, and smoke-check instructions in the committed demo source; add a Dockerfile when a server release will use OpenShift.
2. Test the committed demo on the intended local platform. Register that committed tree using the app number in the portal URL and full brief revision in `BUILD_TASK.md`.

   ```powershell
   python -m twinlab2.portal release 1 1.0.0 <brief-revision> ../my-demo --ref HEAD
   ```

   Use `--subdirectory` for an app inside a shared repo. A release freezes the archive and points to its Git commit and immutable build pack. The release page provides the ZIP, its checksum, and earlier versions. Dependencies and launch steps come from the files committed by the builder.
3. To deploy a server manually, build and publish its image from that commit. Render the manifest for that image and apply it in the intended project. The container should listen on all interfaces on the configured port and support the target project's runtime requirements.

   ```powershell
   python -m twinlab2.openshift billing registry.example/demo/billing:1.0.0 --health-path /api/health --out billing-ocp.json
   oc apply -f billing-ocp.json
   oc rollout status deployment/billing
   oc get route billing
   ```

4. Add the environment and route URL on the release page, under **Setup / Advanced: deployment configuration**. You can include a demo username and the reference to its password in your secret manager. The portal then asks the deployment for `GET <URL>/.twinlab/version`; when it answers HTTP 200 with JSON `{"commit": "<full 40-character Git commit>"}` equal to the release's commit, the deployment is **Verified** (it serves that commit; nothing more). Otherwise (no endpoint, another commit, unreachable) every page shows it as **Registered URL (not verified)**. **Check version again** on the release page repeats the check. The build pack asks the builder for this endpoint. What a Tested release's evidence covers is shown as its **scope** on the release page and in the verification report: the scenarios run and passed, the platform, who ran them with which role, how (by hand or Twin Lab's replay), the commit and the deployment URL.

**Release manifests.** The builder package's `deploy/ocp/<name>/openshift.yaml` is the *development* manifest: it builds `main` and follows `:latest`, so a pod can run newer code than any verified commit. For a release, download **OpenShift manifest (release)** from the release page (`GET /api/v1/releases/<id>/openshift.yaml`): its BuildConfig builds exactly the release's commit (git ref = the SHA) into `<name>:<commit>`, nothing follows a tag, and the Deployment runs the built image by digest (`<name>@sha256:...`). Build first, read the digest (`oc get istag <name>:<commit>`), then download it again with `?image_digest=sha256:...` and apply it.

### Release states: Packaged → Tested → Published

A registered release is **Packaged**: its committed tree is archived, nothing more is known. On the release page, **Record test results** makes it **Tested** and stores the evidence: the tested commit (it must be the packaged commit), a pass, fail or not-run result for every scenario of the release's build brief (the brief's `scenarios`, else one per linked recording, named by its notes), the supported platform, launch instructions and the reset check (pass or fail). It can be tested again until it is published. **Publish** is refused unless the release is Tested, and a Published release is final. The catalog shows each app's latest Tested or Published release with its results, **Open demo** (a verified deployment first, else a registered URL labelled as not verified) and **Download tested release**. Releases registered before these states existed are Packaged.

**Tested is bound to the exact built commit** (`twinlab2.verification`). Before a release becomes Tested, the portal reads GitHub's check runs and commit statuses for exactly the release's SHA (`TWINLAB2_GITHUB_TOKEN`, API at `TWINLAB2_GITHUB_API`) and stores each check's name, conclusion, URL and completion time against that SHA. Every **required check** must be green: per application in *Setup / Advanced → Release verification*, one check name or glob per line, or, when empty, every check reported on the commit (at least one) for a GitHub repository. An application whose **target platform** is Windows desktop also needs a green check from a Windows validation environment (a name matching `*windows*`, such as a `windows-latest` matrix job, or the configured pattern); without one the page says *Windows validation missing*. A result GitHub attributes to another SHA never counts, and a release made from a pull request or branch is refused, and goes back from Tested to Packaged, once that head moves (a new push or a force-push).

**Verify this build** (`portal.verify_commit(connection, app_id, repo, sha, …)`, `POST /api/v1/apps/<id>/verify-commit` with `repo`, `sha` and optionally `pr_number`/`ref`; on a release page, `POST /api/v1/releases/<id>/verify`) packages the commit from GitHub as a Packaged release, reads its checks, and runs Twin Lab's checks, copied into TwinLab2 (`twinlab2.independent`), against the deployment that reports that commit at `/.twinlab/version`: each scenario's recordings are replayed (`python -m twinlab2.replay`, which needs `pip install -e "server[replay]"` and `python -m playwright install chromium`; folders from `TWINLAB2_RECORDINGS`), the required acceptance cases decide the business outcome, and the lab agent (the application's *Lab agent URL*, `TWINLAB2_LAB_TOKEN`) is reset and must equal its baseline. A scenario passes only when every replay passed and its outcome was met; a replay that reaches a Twin Lab path TwinLab2 did not copy makes it not run, never passed. When all of it holds, with the application's platform and launch instructions, the release becomes Tested; either way a verification report (JSON) is stored for the release and linked on its page.

## What the owner sees

1. **Record or import.** Start a session before navigating manually or running an office automation. Import an existing session, API contract, schema, file example or owner note when available. Automation source is optional input for discovering application requirements.
2. **Assign evidence.** Review suggested applications and components. Select an existing catalog app, create one, or ignore an incidental service. Keep unassigned evidence visible. The same session can contribute to several apps.
3. **Review the brief.** For each app, show screens, operations, sample data, expected outcomes and unanswered questions. Let the owner correct an inference, supply an example or narrow the demo. Preserve the evidence behind each decision.
4. **Build in Git.** Export a self-contained brief for a coding agent. On the first build the agent creates the app. On later builds it receives the previous source revision and changes to the approved brief, and proposes a reviewable change in the same repo.
5. **Publish a release.** Link a tested source commit, downloadable runnable package and instructions. Optionally register a deployment and demo sign-in information. A deployed URL belongs to a release and environment so the portal can show which version is live.

## A small implementation

Start with one portal service, SQLite for the catalog, a restricted directory for source captures, and a local recorder CLI on the workstation. The portal can import completed sessions. Git holds demo source. Use the existing Git host's release attachments or an internal artifact store for downloads. An ordinary ZIP and a Markdown build task are enough to hand work to an external agent. An integrated agent service, worker fleet and cluster credentials can wait.

| Record | Identity and relationships |
|---|---|
| Demo app | Stable ID, name, owner, purpose, repo URL and optional repo subdirectory. Can be created before recording. |
| Component | A named interface owned by an app, such as a Windows client, HTTP endpoint, database view or file exchange. Several components may use the same underlying state. |
| Recording | Stable session ID, capture format, source artifacts, channel health and completion status. Importing the same session again preserves its identity. |
| Evidence link | Joins an app or component to a recording and selected event IDs or artifact references. Supports many-to-many assignment and later correction without moving the original. |
| Build brief revision | Immutable selection of app evidence, owner decisions, fixtures, acceptance examples and known gaps. An updated recording or decision creates a new revision when approved. |
| Release and deployment | A release points to a brief revision, Git commit, package and check results. A deployment points to a release, environment, URL, optional demo username and access-controlled secret reference. |

**Example.** One recording opens Orders, creates an order and uses the returned order number in Billing. Another recording shows Billing rejecting an invalid amount. Orders receives evidence from the first recording. Billing receives evidence from both. The shared contract says Billing accepts the order number that Orders creates, using a synthetic example. Each app gets its own build brief and release. For a combined demo, a small launch manifest pins compatible releases and configures their connection. If Billing must read an Orders API, test the actual request between the demos. If a person copies the number, test that the copied value is accepted. Record the distinction in the brief.

## Recorder changes in priority order

| Priority | Change | How to tell it works |
|---|---|---|
| 1 | Give the web and Windows collectors one session ID, stable event IDs, channel IDs and a recoverable local journal. Show when each collector is attached, interrupted or stopped. | Kill the recorder during a tab-to-desktop handoff, recover earlier events and import twice. Earlier links still resolve, events appear once and the interruption is visible. |
| 2 | Suggest app assignments from page origin and path, tab, process and window. Allow merge, split and reassignment. Treat shared authentication, embedded widgets and shared hosts as review decisions. | Two apps behind one host and a desktop client with the same business identity can be mapped correctly. Corrections leave the original session intact. |
| 3 | Capture the state around a meaningful interaction, including the stable result or a timeout. Keep DOM or accessibility structure, relevant visual evidence and accessible control identifiers when required for the demo. | A delayed modal, a rejected input and a successful save each produce distinguishable evidence. Unsupported surfaces and missing observations appear in the brief. |
| 4 | Accept independent evidence for direct API, SQL and utility calls. Start with contract and sample import. Add a live collector only for a demonstrated gap. Record source, time and any supplied correlation ID. | An API-only job can supply a request and response, a database-only job can supply a schema and before/after fixture, and a utility can supply sample input/output and exit status. None requires a browser step. |
| 5 | Preview the exact export. Keep raw screenshots, payloads and traces restricted. Export reviewed visuals, sanitized examples and synthetic values that preserve relationships across apps. | Inspect an exported pack for seeded test secrets and verify that one synthetic order number stays consistent across both apps. |

A screen recording can show that clicking Save displayed an order number. It cannot establish a database transaction, retention rule or every validation branch. Keep each requirement tagged as observed, owner-specified or unresolved. When recordings conflict, show both observations with their version, role or scenario and ask the owner which behavior the demo needs.

## The two packages

| Build pack for the developer or agent | Release package for the demo user |
|---|---|
| Approved scope, evidence index, sanitized screens and contracts, synthetic fixtures, handoff examples, target runtime and executable acceptance examples where available. | Versioned application, local launcher, dependencies or image instructions, demo seed/reset, configuration template and a smoke check. Include a Windows launcher or installer for native components. |
| Identifies its app and brief revision. Can be consumed offline by any agent with the necessary build tools. | Identifies its source commit and supported platform. For a server component, include the container build and manual OpenShift manifests when requested. A native Windows UI runs on a Windows machine and can connect to services in OpenShift. |

## Reuse and scope

| Existing code | TwinLab2 decision |
|---|---|
| Twin Lab web and desktop recorders, web split proposals and desktop timeline support | Wrap the existing collectors and normalize their outputs at import. Preserve global event references. Use per-app exports as views of a session. |
| Twin Lab model, brief, synthetic bindings and agent package generation | Adapt the portable artifacts behind a provider-neutral export command. Review dependencies on the existing app directory layout and automation declarations. |
| Twin Lab behavior checks and manual OpenShift renderer | Reuse checks that exercise the application's demo scenarios. Adapt deployment files to the generated repo and pinned release. |
| Prod Echo capture, masking and generation pipeline | Consult individual data-handling modules if a specific fixture requires them. Its primary pipeline starts from a process and includes bot tracing and replay. |
| Application Twin Studio shared-state runtime | Reuse the shared-state design where a demo exposes the same business record through UI, API and database. Its automation onboarding and A/B comparison belong outside the catalog workflow. |

## First delivery and acceptance

Build one vertical slice using the existing Orders/Billing recording. Import it once, assign it to two apps, add a second Billing recording, export both briefs, and have a coding agent build both from the packs. Pin the resulting source revisions. On a clean machine, download both releases, launch and reset them, create an order and use its new number in Billing. Change one Billing requirement and release it again while the earlier release remains downloadable. This exercises the catalog's central relationships, the agent handoff and the user's actual delivery path.

Next, run one mixed Windows/web scenario and one headless API/database or file scenario. Check the business result, persisted state, interface characteristics the consuming automation needs, and reset behavior. Record actual operating systems, dependencies and omissions with the release. Validate manual OpenShift deployment separately on an available cluster.

## Evidence behind the recommendation

- The recorded Orders/Billing trial rebuilt two web apps and replayed 107 steps across them within its declared scope. It provides an existing starting fixture. [Trial report](../../rebuild-trial/judge/report-blind-rebuild-2.md)
- The public storefront trial failed its official run. Diagnostic variations exposed timing, URL fragment and visual-state assumptions. That history supports recording settled states and checking business outcomes explicitly. [Storefront report](../../colour-trial/blind2/judge/REPORT.md)
- The existing audit lists interrupted sessions, production authentication and independently rebuilt real API/database combinations among its outstanding coverage limits. These remain separate validation tasks. [Replication audit](../../twin-lab/docs/extensive-replication-audit-2026-11-18.md)
