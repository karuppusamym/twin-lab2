# Local verification

| Check | Result |
|---|---|
| Live Chromium recorder | Attached to a local Chromium session with CDP and recorded nine events across tabs, including interactions. The session retained the upstream commit and stable event IDs. |
| Mixed-source recovery | Automated fixture combined web and Windows channel logs, recovered an incomplete final line, reimported without deleting later events, and kept the original event linked. |
| Catalog and Git releases | Automated tests verified many-to-many recording assignments, reviewed attachments, immutable older build packs, packaging from the committed tree, running the extracted ZIP, earlier release downloads and deployment registration. |
| Orders/Billing release replay | Imported the existing 108-event recording, exported separate briefs, packaged the previously built fixture apps from temporary Git commits, downloaded both ZIPs and launched both extracted apps. The existing judge replayed all 107 executable steps. All 16 acceptance cases passed within the declared scope. |
| OpenShift | Manifest wiring was checked locally. Cluster rollout, image registry access, and route behavior need a target OpenShift project. |

The live capture used a browser. The mixed desktop test used recorded input fixtures. A fresh enterprise desktop capture, fresh agent rebuild, and clean-machine dependency installation were outside this test run. Interface evidence for direct API, database and utility calls is supplied through reviewed attachments.

Run `python -m unittest discover -s tests -v` from the project directory to repeat the automated catalog, recovery and release checks.
