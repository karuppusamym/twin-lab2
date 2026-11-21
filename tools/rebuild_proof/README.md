# Rebuild proof: recording → per-app package → blind rebuild → replay

A harness that proves the whole flow end to end on the Twin Lab reference apps, and finds the gaps. It runs
the real recorder, a real portal and the portal's own replay. The builders are Claude subagents given only
an unzipped package. Nothing here opens a visible window, except `desktop_batch.ps1`, which waits for
"go desktop".

| Case | Recording | Apps (one package each) |
|---|---|---|
| a | one app | Orders (web) |
| b | one app | Account Inquiry (synthetic TN3270 host) |
| c | many apps | Orders and Billing on **one host**, told apart by path; Account Inquiry (mainframe); CaseDesk (desktop) |
| d | one app | CaseDesk (desktop) |
| e | one app, live desktop | CaseDesk, recorded live: `desktop_batch.ps1 -GoDesktop` only |

Evidence goes outside the repository, to `../rebuild-proof/` (`REBUILD_PROOF_EVIDENCE`). The Twin Lab checkout
`../twin-lab` (`TWINLAB_CHECKOUT`) is read, never written.

## Steps

```powershell
python tools\rebuild_proof\record.py a        # b, c, d: reference apps + portal + recorder, references and upload
python tools\rebuild_proof\owner.py a         # assign events, scenarios, HAR/seed attachments, freeze, keep the ZIP
# blind builds: one fresh Claude subagent per <evidence>\builds\<case>-<app>\ (the unzipped ZIP only)
python tools\rebuild_proof\audit.py           # did each builder stay in its folder (transcripts in builds\agents.json)
python tools\rebuild_proof\replay.py control-web a     # positive control: the judge against the reference app
python tools\rebuild_proof\replay.py web a             # the rebuilt app, strict (case c: two apps behind one host)
python tools\rebuild_proof\replay.py tn3270 b          # mainframe (control-tn3270 for the control)
python tools\rebuild_proof\replay.py control-visual a  # visual: the reference against its own pictures
python tools\rebuild_proof\replay.py visual a          # visual: the rebuild (portal's visual.py, via visual_run.py)
python tools\rebuild_proof\probes.py web a             # non-happy path: reference vs rebuild (tn3270 b|c too)
python tools\rebuild_proof\verify_portal.py a          # the portal's own Verify (a job) takes the rebuild to Tested: a, b, c
```

- **visual_run.py**: the portal's `visual.py` at the recorded pictures' size (attach-mode pictures are not
  1280x720). **probes.py**: probes off the recorded path (invalid input, empty results, permission), with
  the reference app as the oracle.

- **record.py**: the recorder's own command line (`pair`, `record --session N --cdp …`, `send`, `upload`)
  against a headless Chromium with a DevTools port, and Twin Lab's scripted 3270 terminal against the
  recorder's TN3270 proxy. Desktop steps for c and d are Twin Lab's earlier real CaseDesk recording,
  spliced in (no desktop collector runs, because it hooks this PC's keyboard and mouse). The module
  docstring lists every difference from a person's recording.
- **owner.py**: only `/api/v1`, with scenarios written from what was seen while recording.
- **replay.py**: web through `twinlab2.independent.TwinLabChecks` (`python -m twinlab2.replay`), with
  judge-side `bindings.yaml`, `acceptance.yaml`, `replicas.yaml` and `scope.yaml` that no builder sees.
  Its mainframe replay was the harness's own; the portal now has one (`twinlab2.replay.tn3270`).
- **verify_portal.py**: the portal's real Verify, end to end: the case's catalog, the rebuild registered
  as a release (`python -m twinlab2.portal release`), the rebuild started with a lab agent stand-in
  (reset = restart on its seed), the deployment registered, `POST .../verify` (202) worked by a real
  `python -m twinlab2.worker --types verify`, progress polled, the release Tested. Case c runs Orders
  and Billing on two separate servers (no path proxy, no replicas.yaml): Verify maps each
  application's steps to its own deployment. Results: `<evidence>/<case>/verify-portal/`.
- **desktop_live.py / desktop_batch.ps1**: the live CaseDesk recording and Twin Lab's `labctl desktop verify`
  for the reference exe and both rebuilds. **Opens windows; run only on "go desktop".**

The results and the gap table are in `review-notes/rebuild-gaps.md` beside the checkout.
