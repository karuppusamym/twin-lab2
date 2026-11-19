# TwinLab2 implementation plan

The first delivery follows one demo from evidence import to a coding agent's inbox. The portal runs on the workstation and stores its catalog in SQLite. See the [design proposal](README.md) for the full workflow and recorder priorities.

| Stage | Outcome | Acceptance |
|---|---|---|
| 1. Catalog and handoff | Create demo apps, import Twin Lab event logs, assign selected events, and download a build brief per app. | Import one Orders/Billing session twice, link different slices to both apps, add a second Billing recording, and verify both exports. |
| 2. Recorder | Wrap existing web and Windows collectors with a shared session ID, a recoverable journal, and visible channel status. Add reviewed API, database and utility evidence imports. | Interrupt a recording during a handoff and recover the earlier events with stable IDs. Run a mixed Windows/web session and a headless example. |
| 3. Build and release | Export approved evidence files, fixtures and acceptance examples. Build from the pack in Git and register a runnable release against its commit. | Download both demo apps on a clean machine, reset them, and use a new order number in Billing. Publish an updated Billing release and verify both versions can still be downloaded. |
| 4. Deployment | Register a release URL and configure any demo credentials through a secret reference. | Deploy a server release manually on OpenShift and confirm its registered URL serves the expected version. |

## Implemented data flow

- A demo app contains a name, Git URL and owner-authored build requirements.
- Workstation recordings retain their session ID and per-channel event identity across recoveries. Standalone Twin Lab logs are identified by a digest of their input lines. Reimporting returns the same catalog entry.
- An evidence link joins an app and recording. It stores the selected event numbers and the owner's account of the business outcome. Saving the same link again updates that selection.
- The catalog stores a projection of each recording with action names and source labels for assignment. Build packs include selected events, owner requirements, notes and approved visual or contract attachments. Raw capture artifacts stay in the recorder's source folder.
- A ZIP contains Markdown instructions and a machine-readable evidence manifest. Its content hash identifies the exact exported brief. The app owner supplies the behavior and acceptance details needed to turn the event summary into an implementable task.
- The release command archives a committed Git tree and associates it with the stored brief. The portal keeps previous release downloads and registers deployment URLs against a specific release.
