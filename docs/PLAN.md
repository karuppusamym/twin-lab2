# TwinLab2 implementation plan

The first delivery follows one demo from evidence import to a coding agent's inbox. The portal runs on the workstation and stores its catalog in SQLite. See the [design proposal](README.md) for the full workflow and recorder priorities.

Status labels (**Implemented**, **Locally tested**, **Externally validated**) are defined in the README's *What works today*. Nothing is externally validated yet.

| Stage | Outcome | Acceptance | Status |
|---|---|---|---|
| 1. Catalog and handoff | Create demo apps, import Twin Lab event logs, assign selected events, and download a build brief per app. | Import one Orders/Billing session twice, link different slices to both apps, add a second Billing recording, and verify both exports. | Locally tested |
| 2. Recorder | Wrap existing web and Windows collectors with a shared session ID, a recoverable journal, and visible channel status. Add reviewed API, database and utility evidence imports. | Interrupt a recording during a handoff and recover the earlier events with stable IDs. Run a mixed Windows/web session and a headless example. | Locally tested (recovery from fixture logs; mixed session on one developer PC); API, database and utility evidence only as reviewed attachments |
| 3. Build and release | Export approved evidence files, fixtures and acceptance examples. Build from the pack in Git and register a runnable release against its commit. | Download both demo apps on a clean machine, reset them, and use a new order number in Billing. Publish an updated Billing release and verify both versions can still be downloaded. | Locally tested for packaging and download. Acceptance not met: no fresh agent rebuild from a pack (the replay used previously built fixture apps) and no clean-machine install |
| 4. Deployment | Register a release URL and configure any demo credentials through a secret reference. | Deploy a server release manually on OpenShift and confirm its registered URL serves the expected version. | Registration locally tested; OpenShift deployment Implemented only, never run on a cluster |
| 5. Onboarding journey | Each application shows owner, purpose, its stage in *Choose application → Add examples → Review required behaviour → Build → Verify → Open or download*, missing information and one next action. Technical settings move to Setup / Advanced. | A new user can tell from the catalog what to do next for every application. | Locally tested (`tests/test_journey.py`, local render); not yet tried with users |

## Implemented data flow

- A demo app contains a name, owner, purpose, Git URL and owner-authored business scenarios. Its scope decision is *Confirmed application* (`known`), *Discovered: decide scope* (`discovered`) or *Not replicated* (`not_replicated`).
- Workstation recordings retain their session ID and per-channel event identity across recoveries. Standalone Twin Lab logs are identified by a digest of their input lines. Reimporting returns the same catalog entry.
- An evidence link joins an app and recording. It stores the selected event numbers and the owner's account of the business outcome. Saving the same link again updates that selection.
- The catalog stores a projection of each recording with action names and source labels for assignment. Build packs include the selected events with their reviewed screen, control and label references, the owner's structured scenarios (expected results, synthetic example values, unresolved questions), notes and approved visual or contract attachments; the portal previews a pack's exact contents before download. Raw capture artifacts stay in the recorder's source folder.
- A ZIP contains Markdown instructions and a machine-readable evidence manifest. Its content hash identifies the exact exported brief. The app owner supplies the behavior and acceptance details needed to turn the event summary into an implementable task.
- The release command archives a committed Git tree and associates it with the stored brief. The portal keeps previous release downloads and registers deployment URLs against a specific release.

## Workstation recorder and portal references (2026-11-19)

Design: `twin-lab/docs/11-session-recorder.md`. Built and checked locally (`VERIFICATION.md`):

| Item | State |
|---|---|
| Portable recorder (`agent/`, PyInstaller one folder): pair, requested sessions, own sessions, record, review, send | Locally tested: end to end on one developer PC with the built program. No managed-PC trial; unsigned |
| Capture levels (full, steps, presence) and deny list, one timeline, references only | Locally tested (Twin Lab `labctl.workstation`) |
| Portal: sign-in, machines and pairing, agent API, recording requests, discovered applications, flows on recording and application pages, automations | Locally tested (`registry.py`, `tests/test_registry.py`) |
| Portal on OpenShift behind the OAuth proxy | Implemented (`Dockerfile`, `deploy/openshift-portal.yaml`); the image ran locally, the manifest has not run on a cluster |
| Next | Sign the build and try it on a managed PC; deploy on OpenShift; rebuild a demo from a fresh build pack and install it on a clean machine; upload recordings to GitHub or S3; clipboard hand-offs; split and build packs from a session |
