# TwinLab2 design proposal

## Run TwinLab2

```powershell
cd twin-lab2
python -m pip install -r requirements.txt
python portal.py
```

Open `http://127.0.0.1:8765`. The **Applications** page follows each application through one journey: **Choose application → Add examples → Review required behaviour → Build → Verify → Open or download**. Each card shows the application's owner, purpose, current stage, missing information and the one thing to do next; the application page shows the same journey with its steps in order. Create a demo app, import a Twin Lab `steps.jsonl` (or request a recording), and assign events and notes from the recording page. On the app page, add one business scenario per outcome the copy must reproduce (starting conditions, actions linked to selected events, expected result, failure cases, synthetic example values, unresolved questions), preview the build pack file by file, and download it. Existing free-text requirements are moved into a scenario's notes. Recorder machines, pairing, application identities, Git details and deployment configuration are under **Setup / Advanced**.

A recording can also register applications it finds as *Discovered: decide scope*. On the application page, the **Scope decision** makes it a **Confirmed application** (replicate it), merges it into the application it is the same as, or marks it **Not replicated** (later recordings note it only as used). In the database these are the statuses `known`, `discovered` and `not_replicated`. For an existing multi-app example, use `../rebuild-trial/judge/recording/order-to-invoice/steps.jsonl`.

The portal is local and writes its catalog to `.state/catalog.db`. Use `--database` and `--port` to override those defaults. Install `requirements-test.txt` (adds `moto` for S3 without AWS) and run `python -m unittest discover -s tests -v` to check the catalog, recording recovery, artifact storage and release workflows. [Verification results](VERIFICATION.md)

**Artifact storage** (`artifacts.py`). Evidence files, frozen build packs, verification reports, release downloads and uploaded recordings are stored as immutable objects; the catalog keeps only references (backend, bucket, key, version, SHA-256, size, type, kind), never an expiring URL. By default they are files beside the catalog (`.state/artifacts`, or `TWINLAB2_ARTIFACT_DIR`). For S3 or an S3-compatible store such as MinIO set `TWINLAB2_ARTIFACT_BACKEND=s3`, `TWINLAB2_S3_BUCKET`, and as needed `TWINLAB2_S3_PREFIX`, `TWINLAB2_S3_REGION`, `TWINLAB2_S3_ENDPOINT_URL` and `TWINLAB2_S3_DIRECT_UPLOAD=0` (recorders cannot reach the bucket: uploads go through the portal). Enable versioning on the bucket. Credentials come from boto3's usual environment, profile or role and are never shown; **Setup / Advanced** shows the rest. Downloads from S3 are presigned URLs valid for five minutes. Catalogs from before this keep their SQLite blobs working: every start copies blobs not yet copied and checks each copy's SHA-256 before using it, and `python portal.py artifacts --purge` then empties the blobs whose copy verifies. Recordings are uploaded with the resumable API in [RECORDER-CONTRACT.md](RECORDER-CONTRACT.md#recording-uploads-apiuploads) (`TwinLabRecorder upload <session-id>`).

## What works today

Each capability carries one of three labels:

- **Implemented**: the code is written and has unit tests where noted, but nobody has exercised it end to end.
- **Locally tested**: exercised end to end on the developer's own Windows PC, by the automated tests or a manual run.
- **Externally validated**: confirmed in a target environment by its users (a managed PC, an OpenShift cluster, a clean machine, a fresh agent build).

**Nothing is externally validated yet.** No recorder trial on a live managed PC, no OpenShift deployment, no fresh coding-agent rebuild from a build pack and no clean-machine install have been done. The Orders/Billing release replay packaged previously built fixture apps.

| Capability | Status | Evidence and limits |
|---|---|---|
| Portal journey: stage, missing information and one next action per application; Setup / Advanced for machines, identities, Git and deployment settings | Locally tested | `tests/test_journey.py`; pages rendered from a local run |
| Catalog: applications, Twin Lab recording import, evidence links, reviewed attachments | Locally tested | `tests/test_workflow.py` |
| Build pack export | Locally tested | Automated tests check its contents and immutable revisions. No coding agent has built an app from a pack in this project |
| Release registration from a committed Git tree, release download | Locally tested | Automated tests; the Orders/Billing replay used previously built fixture apps, not a fresh rebuild |
| Release states Packaged → Tested → Published; deployment version check | Locally tested | `tests/test_release_states.py`; the journey's Verify step is done once the latest release is Tested, Open or download once it is Published. No real deployment has answered the version check |
| Tested bound to the built commit: required GitHub checks, Windows validation, Twin Lab replay/outcome/reset, verification report | Locally tested | `tests/test_verification.py` against a fake GitHub API and a fake Twin Lab runner; the pinned labctl runner was smoke-run locally (replay and reset reach labctl). Never run against the real GitHub API or a real built app |
| Deployment registration (URL, demo username, secret reference) | Locally tested | Automated tests register a URL; no demo has been deployed |
| OpenShift manifest for a demo (`openshift.py`) | Implemented | Manifest wiring checked locally; never applied to a cluster |
| Portal on OpenShift (`Dockerfile`, `deploy/openshift-portal.yaml`) | Locally tested (image), Implemented (manifest) | The image ran locally as an arbitrary UID; the manifest, OAuth proxy, route and volume have not run on a cluster |
| Machine pairing, agent API, references mapped to applications | Locally tested | `tests/test_registry.py`, `tests/test_agent.py` |
| Evidence integrity: a resend keeps the reviewed selection, merge with a preview, web identity by path | Locally tested | `tests/test_integrity.py` |
| Business scenarios and build-pack preview | Locally tested | `tests/test_buildpack.py`; no coding agent has built from these packs yet |
| Portable recorder (built program): web, desktop and synthetic mainframe | Locally tested | One developer PC, unsigned build; no managed PC, endpoint protection or AppLocker; no real mainframe or emulator |
| UiPath: targets from a project, the robot's log | Implemented | Unit tests in Twin Lab; no real UiPath robot has been recorded |
| Recording recovery after an interrupted capture | Locally tested | Fixture logs (`tests/test_workflow.py`) |

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

Build the folder from the pinned Twin Lab commit (`twinlab.lock.json`). `fetch` downloads it and checks its digest, so no twin-lab checkout is needed:

```powershell
python -m pip install -r requirements-recorder.txt
python -m agent.twinlab fetch
python agent\build.py
```

The recorder and the portal speak the versioned [recorder contract](RECORDER-CONTRACT.md), now 1.0.0. The portal refuses a recorder whose contract it does not support, and it says which side to update. Recorders built before 0.2.0 declare no version: rebuild and redeploy them. Each recording keeps the recorder version and the contract version it was sent with. [How the Twin Lab pin is kept and moved](UPSTREAM.md)

The build is not signed. Sign it before it runs on any machine other than a developer's: an unsigned program with keyboard hooks is likely to be stopped by endpoint protection.

**The portal on OpenShift.** `Dockerfile` and `deploy/openshift-portal.yaml` run the portal behind the OpenShift OAuth proxy (people sign in with OpenShift; recorders use their machine token on `/api/agent/`). Locally it serves only this machine, or anyone with `TWINLAB2_PASSWORD`. Settings: `TWINLAB2_SECRET_KEY`, `TWINLAB2_HOSTS`, `TWINLAB2_DATABASE`, `TWINLAB2_TRUST_PROXY_USER`.

## Capture with the workstation recorder

`capture.py` runs Twin Lab's full command line (`labctl.cli`), which imports all of twin-lab's dependencies. Install them from the pinned commit. Then fetch the pinned collectors, which `capture.py` puts first on the path, and provision the browser.

```powershell
python -m pip install "twin-lab[desktop] @ git+https://github.com/karuppusamym/twin-lab@430dfe538965b46ccbe824f8f2b4f415c5576483"
python -m agent.twinlab fetch
python -m playwright install chromium
python capture.py start .state/captures/order-demo --web http://127.0.0.1:8080
```

For automation, start the browser with a local CDP endpoint and add `--cdp http://127.0.0.1:9222` in place of `--web`. Add `--desktop MyApp.exe` or `--desktop-pid 1234` to attach to a running Windows application in the same session. A desktop-only capture uses just a desktop option. Close the manual browser to finish, or stop with Ctrl+C. After the collectors have stopped, `python capture.py recover .state/captures/order-demo` rebuilds the import file from recoverable channel logs. Check `session.json` and the channel logs for capture status and failures.

Import the session's top-level `steps.jsonl`. Recovered imports retain previously linked event IDs. Review scope and attach approved API contracts, synthetic database fixtures, utility examples, visual references, or acceptance cases directly to the app. The adapter records the collector commit for every capture: the pinned one, or a clean checkout's with `--twin-lab`. [Twin Lab baseline and update procedure](UPSTREAM.md)

## Build with Devin

On the application page, **Build with Devin** opens the **Build** page: the exact build pack (the same file-by-file preview as the download), a **Freeze this pack** button, the target repository and base branch, what changed since the previous build, and the ACU budget. Only a frozen pack is built, and the build sends exactly the frozen bytes (their SHA-256 is checked again). The branch is resolved to a commit when the build is queued; Devin starts from that commit. A later build of a changed pack must say what changed; Devin gets the new pack, the previous revision and its pull request, the summary and the list of files that differ.

**Build progress** shows the state, Devin's questions with an answer box (the answer is sent to the session), the Devin session link, the pull request and its head commit, and a placeholder for the independent checks, with **Cancel** and **Retry**. Devin finishing is **Ready for verification**, never Tested.

| State | Meaning |
|---|---|
| Queued | Waiting for the worker |
| Starting | Uploading the pack and creating the Devin session |
| Checking whether the build started | The create got no clear answer (timeout, 5xx, or a worker that stopped mid-call). Nothing is sent again until the worker has looked for the session by the build's tag: found, it is followed; not found, it is created once more |
| Running | Devin is working |
| Waiting for your answer | Devin asked something; answer on the page |
| Ready for verification | Devin finished; the pull request awaits the independent checks |
| Failed, Cancelled | Final; **Retry** makes a new attempt of the same frozen pack, repository and base commit |

One build per application is active at a time (a unique column, plus a check), so a second click finds the first build. Builds run in one worker process beside the portal:

```powershell
python worker.py --database .state/catalog.db
```

Jobs are leased and kept alive by a heartbeat; a job held by a worker that died is taken over when its lease runs out. Under **Setup / Advanced: Build with Devin** an owner sets the Devin key and organisation and the GitHub token (environment variables `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `DEVIN_BASE_URL`, `TWINLAB2_GITHUB_TOKEN`, `TWINLAB2_GITHUB_API` win over saved values; secrets are never shown), connects repositories, sets the default ACU budget, and sees the workers and the job queue. For development the worker can be started from there. The Devin client is Twin Lab's own, imported from the pinned commit ([UPSTREAM.md](UPSTREAM.md)); run `python -m agent.twinlab fetch` once.

## Build, package, and register a deployment

1. Download the app's build pack (or build it with Devin, above). Give the ZIP to a coding agent along with access to the demo's Git repo. Include launch, seed, reset, and smoke-check instructions in the committed demo source; add a Dockerfile when a server release will use OpenShift.
2. Test the committed demo on the intended local platform. Register that committed tree using the app number in the portal URL and full brief revision in `BUILD_TASK.md`.

   ```powershell
   python portal.py release 1 1.0.0 <brief-revision> ../my-demo --ref HEAD
   ```

   Use `--subdirectory` for an app inside a shared repo. A release freezes the archive and points to its Git commit and immutable build pack. The release page provides the ZIP, its checksum, and earlier versions. Dependencies and launch steps come from the files committed by the builder.
3. To deploy a server manually, build and publish its image from that commit. Render the manifest for that image and apply it in the intended project. The container should listen on all interfaces on the configured port and support the target project's runtime requirements.

   ```powershell
   python openshift.py billing registry.example/demo/billing:1.0.0 --health-path /api/health --out billing-ocp.json
   oc apply -f billing-ocp.json
   oc rollout status deployment/billing
   oc get route billing
   ```

4. Add the environment and route URL on the release page, under **Setup / Advanced: deployment configuration**. You can include a demo username and the reference to its password in your secret manager. The portal then asks the deployment for `GET <URL>/.twinlab/version`; when it answers HTTP 200 with JSON `{"commit": "<full 40-character Git commit>"}` equal to the release's commit, the deployment is **Verified**. Otherwise (no endpoint, another commit, unreachable) every page shows it as **Registered URL (not verified)**. **Check version again** on the release page repeats the check. The build pack asks the builder for this endpoint.

### Release states: Packaged → Tested → Published

A registered release is **Packaged**: its committed tree is archived, nothing more is known. On the release page, **Record test results** makes it **Tested** and stores the evidence: the tested commit (it must be the packaged commit), a pass, fail or not-run result for every scenario of the release's build brief (the brief's `scenarios`, else one per linked recording, named by its notes), the supported platform, launch instructions and the reset check (pass or fail). It can be tested again until it is published. **Publish** is refused unless the release is Tested, and a Published release is final. The catalog shows each app's latest Tested or Published release with its results, **Open demo** (a verified deployment first, else a registered URL labelled as not verified) and **Download tested release**. Releases registered before these states existed are Packaged.

**Tested is bound to the exact built commit** (`verification.py`). Before a release becomes Tested, the portal reads GitHub's check runs and commit statuses for exactly the release's SHA (`TWINLAB2_GITHUB_TOKEN`, API at `TWINLAB2_GITHUB_API`) and stores each check's name, conclusion, URL and completion time against that SHA. Every **required check** must be green: per application in *Setup / Advanced → Release verification*, one check name or glob per line, or, when empty, every check reported on the commit (at least one) for a GitHub repository. An application whose **target platform** is Windows desktop also needs a green check from a Windows validation environment (a name matching `*windows*`, such as a `windows-latest` matrix job, or the configured pattern); without one the page says *Windows validation missing*. A result GitHub attributes to another SHA never counts, and a release made from a pull request or branch is refused, and goes back from Tested to Packaged, once that head moves (a new push or a force-push).

**Verify this build** (`portal.verify_commit(connection, app_id, repo, sha, …)`, `POST /apps/<id>/verify-commit` with `repo`, `sha` and optionally `pr_number`/`ref`; on a release page, `POST /releases/<id>/verify`) packages the commit from GitHub as a Packaged release, reads its checks, and runs Twin Lab's own checks with the pinned labctl (`independent.py`) against the deployment that reports that commit at `/.twinlab/version`: each scenario's recordings are replayed (`labctl behaviour fidelity`; folders from `TWINLAB2_RECORDINGS`), the required acceptance cases decide the business outcome, and the lab agent (the application's *Lab agent URL*, `TWINLAB2_LAB_TOKEN`) is reset and must equal its baseline. A scenario passes only when every replay passed and its outcome was met. When all of it holds, with the application's platform and launch instructions, the release becomes Tested; either way a verification report (JSON) is stored for the release and linked on its page.

TwinLab2 helps a demo owner turn examples of an application into a runnable, versioned demo. The owner opens an app in the catalog, adds recordings and requirements, reviews what the demo should do, and gives the resulting build brief to a coding agent. A customer of the catalog can download a release or open a deployed demo.

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

- The recorded Orders/Billing trial rebuilt two web apps and replayed 107 steps across them within its declared scope. It provides an existing starting fixture. [Trial report](../rebuild-trial/judge/report-blind-rebuild-2.md)
- The public storefront trial failed its official run. Diagnostic variations exposed timing, URL fragment and visual-state assumptions. That history supports recording settled states and checking business outcomes explicitly. [Storefront report](../colour-trial/blind2/judge/REPORT.md)
- The existing audit lists interrupted sessions, production authentication and independently rebuilt real API/database combinations among its outstanding coverage limits. These remain separate validation tasks. [Replication audit](../twin-lab/docs/extensive-replication-audit-2026-11-18.md)
