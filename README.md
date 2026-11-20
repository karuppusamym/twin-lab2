# TwinLab2 design proposal

## Run TwinLab2

```powershell
cd twin-lab2
python -m pip install -r requirements.txt
python portal.py
```

Open `http://127.0.0.1:8765`. Create a demo app, import a Twin Lab `steps.jsonl`, and assign events and notes from the recording page. On the app page, add one business scenario per outcome the copy must reproduce (starting conditions, actions linked to selected events, expected result, failure cases, synthetic example values, unresolved questions), preview the build pack file by file, and download it. Existing free-text requirements are moved into a scenario's notes. For an existing multi-app example, use `../rebuild-trial/judge/recording/order-to-invoice/steps.jsonl`.

The portal is local and writes its catalog to `.state/catalog.db`. Use `--database` and `--port` to override those defaults. Run `python -m unittest discover -s tests -v` to check the catalog, recording recovery and release workflows. [Verification results](VERIFICATION.md)

## TwinLabRecorder: a portable recorder that tells the portal what it recorded

`TwinLabRecorder.exe` is one folder: copy it to a Windows PC and run it, with no install and no administrator rights. It records one session across the applications a person or a robot uses. The session's **target** applications are recorded in full. Every other application is recorded only as having been used, and Windows itself, Adobe, Outlook, Teams and password managers always stay that way. **The recording stays on the PC.** The portal receives only references: the applications recorded and each one's steps as labels (control ids and the application's own words, never a typed value). It maps the recording to its applications and registers an application it has not seen as *discovered*. Design: `twin-lab/docs/11-session-recorder.md`.

1. In the portal, **Machines → Make a pairing code**. On the PC, run `TwinLabRecorder.exe` and choose **Pair with portal…** (or `TwinLabRecorder.exe pair --portal https://… --code ABCDE-FGHJK --name "Workstation 7"`).
2. Give each application an identity on its page (its executable, such as `CaseDesk.exe`, or its web address), then **Sessions → Offer a session**: name, machine, target applications (full or steps only), and optionally the automation it is for. Or start a session on the recorder itself (**My own session**).
3. On the PC, choose the offered session and **Start**. The recorder opens its own Edge window for web targets; use the applications as the process does. **Stop recording** when done.
4. Review what will be sent (**Recordings on this PC**) and **Send references**. The portal's **Recordings** page shows the session, its applications and the timeline across them; an application's page shows its recorded flows and the automations that touch it.

Command line: `sessions`, `record --session N` (or `--exe X.exe --origin https://…`), `show <id>`, `send <id>`, `recover`. Add `--portable-data` to keep everything in `data\` beside the program, `--ca-bundle` for a private certificate authority. Recordings are in `%LOCALAPPDATA%\TwinLabRecorder\recordings\`.

Build the folder from a Twin Lab checkout that has `labctl.workstation`:

```powershell
$env:TWINLAB_PATH = "..\twin-lab"
python agent\build.py
```

The build is not signed. Sign it before it runs on any machine other than a developer's: an unsigned program with keyboard hooks is likely to be stopped by endpoint protection.

**The portal on OpenShift.** `Dockerfile` and `deploy/openshift-portal.yaml` run the portal behind the OpenShift OAuth proxy (people sign in with OpenShift; recorders use their machine token on `/api/agent/`). Locally it serves only this machine, or anyone with `TWINLAB2_PASSWORD`. Settings: `TWINLAB2_SECRET_KEY`, `TWINLAB2_HOSTS`, `TWINLAB2_DATABASE`, `TWINLAB2_TRUST_PROXY_USER`.

## Capture with the workstation recorder

Install the recorder dependencies from a clean Twin Lab checkout and provision its browser.

```powershell
python -m pip install -e "../twin-lab[desktop]"
python -m playwright install chromium
python capture.py start .state/captures/order-demo --web http://127.0.0.1:8080
```

For automation, start the browser with a local CDP endpoint and add `--cdp http://127.0.0.1:9222` in place of `--web`. Add `--desktop MyApp.exe` or `--desktop-pid 1234` to attach to a running Windows application in the same session. A desktop-only capture uses just a desktop option. Close the manual browser to finish, or stop with Ctrl+C. After the collectors have stopped, `python capture.py recover .state/captures/order-demo` rebuilds the import file from recoverable channel logs. Check `session.json` and the channel logs for capture status and failures.

Import the session's top-level `steps.jsonl`. Recovered imports retain previously linked event IDs. Review scope and attach approved API contracts, synthetic database fixtures, utility examples, visual references, or acceptance cases directly to the app. The adapter records the collector commit for every capture. [Twin Lab baseline and update procedure](UPSTREAM.md)

## Build, package, and register a deployment

1. Download the app's build pack. Give the ZIP to a coding agent along with access to the demo's Git repo. Include launch, seed, reset, and smoke-check instructions in the committed demo source; add a Dockerfile when a server release will use OpenShift.
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

4. Add the environment and route URL on the release page. You can include a demo username and the reference to its password in your secret manager.

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
