# Local verification

Every result below is **Locally tested** (on the developer's Windows PC) unless it says otherwise; labels as in the README's *What works today*. **Nothing here is externally validated**: there has been no recorder trial on a live managed PC, no OpenShift deployment, no fresh coding-agent rebuild from a build pack and no clean-machine install. The Orders/Billing release replay used previously built fixture apps.

Since 2026-11-20 the portal calls offering a session **Request a recording** (the stored session status is still `offered`) and a confirmed application **Confirmed application** (stored status `known`). The results below keep the wording of the day they were run.

| Check | Result |
|---|---|
| Live Chromium recorder | Attached to a local Chromium session with CDP and recorded nine events across tabs, including interactions. The session retained the upstream commit and stable event IDs. |
| Mixed-source recovery | Automated fixture combined web and Windows channel logs, recovered an incomplete final line, reimported without deleting later events, and kept the original event linked. |
| Catalog and Git releases | Automated tests verified many-to-many recording assignments, reviewed attachments, immutable older build packs, packaging from the committed tree, running the extracted ZIP, earlier release downloads and deployment registration. |
| Orders/Billing release replay | Imported the existing 108-event recording, exported separate briefs, packaged the previously built fixture apps from temporary Git commits, downloaded both ZIPs and launched both extracted apps. The existing judge replayed all 107 executable steps. All 16 acceptance cases passed within the declared scope. |
| OpenShift | Implemented only. Manifest wiring was checked locally. Cluster rollout, image registry access, and route behavior need a target OpenShift project. |

The live capture used a browser. The mixed desktop test used recorded input fixtures. A fresh enterprise desktop capture, fresh agent rebuild, and clean-machine dependency installation were outside this test run. Interface evidence for direct API, database and utility calls is supplied through reviewed attachments.

Run `python -m unittest discover -s tests -v` from the project directory to repeat the automated catalog, recovery and release checks.

## Workstation recorder and portal references (2026-11-19)

Windows 11, Edge 142, .NET 6 SDK, Python 3.13, Twin Lab `labctl.workstation` (twin-lab branch `claude/new-session-6114b2`).

| Check | Result |
|---|---|
| Portal: pairing, tokens, agent API, mapping (`tests/test_registry.py`) | Pass. Codes are single use, expire, and are refused after 20 failures in 10 minutes; tokens and codes are stored as SHA-256 only; a revoked machine gets 401; references create `discovered` applications, map by identity on a resend, link the automation of an offered session, and refuse unknown keys, a timeline naming an application not sent, and another machine's session. |
| Portal sign-in (`tests/test_registry.py`) | Pass. Without a password or the proxy only this machine is served; password sign-in; `X-Forwarded-User` only with `TWINLAB2_TRUST_PROXY_USER=1`. |
| Recorder client (`tests/test_agent.py`) | Pass. HTTPS required except for this machine; targets from the portal kept to a fixed shape; the token is DPAPI-protected in `settings.json`; pair, list and send against a real portal server. |
| End to end with Python (`tests/test_agent_e2e.py`, `TWINLAB_AGENT_E2E=1`) | Pass. Portal on 127.0.0.1; pairing code; an offered session targeting a web application with an automation; the recorder records it with CaseDesk added as its own target, in a real Edge and the real CaseDesk (WinForms), with Notepad used in between; references sent. The portal shows the recording mapped to the web application and to CaseDesk (registered as discovered), both linked to the automation, Notepad only as "Other applications", and no typed value. |
| End to end with the built program | Pass. `python agent/build.py` (PyInstaller, one folder, 248 MB); the folder copied to another directory and run with `--portable-data`, with no Python directory on `PATH` and no `PYTHONPATH`/`TWINLAB_PATH`: `version`, `pair`, `sessions`, `record --session 1 --exe CaseDesk.exe` (stopped with the `stop` file), `send --yes`, against `portal.py` running as its own process. The portal mapped the web application, registered CaseDesk as discovered, showed other applications unnamed, held no typed value; the session browser's profile was deleted. |
| The window | Opened from the built program; shows the pairing state and the three tabs. Its actions call the same functions as the command line; they were not clicked through. |

| Portal image | `docker build` of `Dockerfile`, run as an arbitrary UID in group 0 (as OpenShift runs it): `/healthz` answers, a page without `X-Forwarded-User` is refused (401) and served with it, the agent API refuses a wrong pairing code (403). |

Not verified: a second PC or clean VM; a signed build; endpoint protection or AppLocker on a managed PC; `deploy/openshift-portal.yaml` on a cluster (the OAuth proxy sidecar, the route, the volume).

## UiPath readiness: mainframe, browser options and the second review (2026-11-19)

| Check | Result |
|---|---|
| Built program, with the portal, all three surfaces | Pass. A copy of the folder, no Python on `PATH`: paired, offered session for the web application, `record --session 1 --exe CaseDesk.exe --tn3270 127.0.0.1:<demo host>`; real Edge, real CaseDesk, the synthetic mainframe through the proxy. The portal mapped the web application and registered CaseDesk and the mainframe as discovered, with labels such as `USERID`; no typed value, no account number. CaseDesk: 37 steps (typing through UI Automation). |
| Built program, without a portal | Pass. A fresh copy, never paired: `record --exe CaseDesk.exe --tn3270 …`, CaseDesk started outside the session and recorded from its first step, `show` lists both applications. |
| Mainframe proxy (`test_workstation_mainframe.py`) | Pass. Sign-on with a wrong then a right password, two account inquiries, PF3: screens, fields by label, keys; password fields keep nothing; no value in steps or screen files; the same map with other data is one screen id. Synthetic host only: no real mainframe or emulator was used. |
| Browser modes (`test_workstation_gui.py`) | Pass. Attached to a browser started elsewhere (left running); a kept profile used by two sessions in a row. |
| Fresh process hears UI Automation | Pass (`test_a_fresh_process_hears_ui_automation_events`). |
| Twin Lab suite | 1584 passed, 27 skipped, 1 failed: `test_pagerules.py::test_no_typed_value_and_no_script_is_in_the_package`, which types the date 2026-11-20 and finds it in the package's own timestamp on that date (not related to this work). The recorder tests were run again after the review fixes. |

Not checked: a real UiPath robot, a real mainframe or emulator, Chrome with UiPath's extension.

## A real recording through the portal (2026-11-20)

The built recorder, paired with the portal on this PC, recorded an offered session for the automation "Demo: form, calculator and mainframe":

| Surface | Application | Recorded | Sent to the portal |
|---|---|---|---|
| Web | the public Selenium test form, https://www.selenium.dev/selenium/web/web-form.html (synthetic text, a select, a check box, a radio button, submitted) | 11 steps, HAR of that origin only | `my-text-id`, `my-textarea`, `my-select`, submit; route `/selenium/{id}/{id}` |
| Desktop | Windows Calculator (a UWP app behind ApplicationFrameHost), 128 × 4 = | 8 steps | `clearButton`, `One`, `Two`, `Eight`, `multiplyButton`, `Four`, `equalButton` |
| Mainframe | the synthetic TN3270 host through the proxy | 16 steps | `USERID`, `PASSWORD`, `ACCOUNT NO`, `Enter`, `PF3`, screens by title |

example.com, visited in the session's browser, left no step and no HAR entry; the other applications in front appear as "Other applications". No typed text, result or account number reached the portal. The first run lost the web steps (the built recorder announced its browser before Playwright had attached its listeners); the session now announces the browser only once it listens, and the second run recorded them.

Not checked: a real UiPath robot, a real mainframe or emulator, a second PC, a signed build.

## The onboarding journey (2026-11-20)

| Check | Result |
|---|---|
| Journey stage, missing information and one next action (`tests/test_journey.py`) | Pass. The first step not done is the stage; a discovered application asks for a scope decision; examples need an identity, then a paired machine; Verify waits for a release state that says tested; a not-replicated application is out of the journey. |
| Pages (`tests/test_journey.py`) | Pass. Each catalog card shows owner, purpose, stage, missing information and exactly one action; identities and the Git repository are on the Setup / Advanced page; machines are reached from Setup / Advanced, not the main navigation; the application page's forms no longer overwrite each other's fields. |
| Local render | The catalog, an application page, its Setup / Advanced page, Request a recording and Setup / Advanced rendered from `python portal.py --port 8791` with a sample catalog. |

Not checked: onboarding with real users. Release states (Packaged, Tested, Published) are not stored by this portal yet, so no application gets past Verify.
