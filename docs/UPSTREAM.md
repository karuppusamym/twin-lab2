# Twin Lab: the code TwinLab2 copied

TwinLab2 depends on no other repository. The recorder's collectors, the recording format, Verify's
scenario replay, the lab-agent client and the Devin client came from Twin Lab,
[karuppusamym/twin-lab](https://github.com/karuppusamym/twin-lab) (Apache-2.0), at commit
`90bdd917414a24a4da7f09fb66e8edb49ba3bfee` (twin-lab `main`, 2026-11-20). Until then TwinLab2
downloaded that commit's `lab/labctl/labctl` tree at build and test time (`twinlab.lock.json`,
`agent/twinlab.py`); that pin is gone.

Only what TwinLab2 runs was copied: the transitive imports of the modules it used, and inside them
only the paths it can reach. Twin Lab's command line, pack builder, data tools and lab runner were
left out. Each copied file starts with a header naming the commit; its imports were renamed to
this repository's packages. Nothing else in a copied module was changed except where listed under
*Changes*.

`twinlab2_contract.upstream` holds the commit (`UPSTREAM_COMMIT`), and the recorder still reports
it as `twinlab_commit` (in `session.json`, the references and `_build.json`): it is the commit
the collectors' code is from.

## What came from where

About 21,100 lines in 50 files (with their headers), from 47 Twin Lab modules out of 172.

| TwinLab2 module | Twin Lab source (`lab/labctl/labctl/...`) | Used by |
|---|---|---|
| `twinlab2_contract.evidence.sanitize`, `.typedvalues`, `.dom`, `.locate`, `.recording`, `.publicdata` | `sanitize.py`, `typedvalues.py`, `behaviour/dom.py`, `locate.py`, `botlog/recording.py`, `publicdata.py` | the recorder (writes recordings) and the replay (reads them) |
| `twinlab2_recorder.workstation.*` (13 modules) | `workstation/` (`session`, `levels`, `preflight`, `propose`, `timeline`, `completeness`, `recovery`, `robot`, `supervisor`, `tn3270`, `flow`, `journal`, `__init__`) | the recorder (`twinlab2_recorder.main`) |
| `twinlab2_recorder.desktop.*` (8 modules) | `desktop/` (`__init__`, `detect`, `locator`, `privacy`, `recorder`, `win`, `winlook`, `pictures`) | desktop recording |
| `twinlab2_recorder.web.recorder` | `recorder.py` | browser recording |
| `twinlab2_recorder.ocr` | `ocr.py` | the desktop picture check and the web recorder |
| `twinlab2_recorder.botlog.robotlog`, `.models` | `botlog/robotlog.py`, `botlog/models.py` | the UiPath robot log read at the end of a session |
| `twinlab2_recorder.botlog.uipath` | `to_css`, `_wild`, `TEXT_ONLY_ATTRS` from `analyse/uipath.py` | `robotlog` |
| `twinlab2_recorder.botlog.base` | `Contract` from `models.py` | `botlog.models` |
| `twinlab2.replay.*` (9 modules) | `behaviour/` (`__init__`, `fidelity`, `acceptance`, `baseline`, `bindings`, `durable`, `judging`, `scope`, `visual`) | Verify (`twinlab2.independent`, through `python -m twinlab2.replay`) |
| `twinlab2.replay.extracts` | `normalise_origin`, `DEFAULT_PORTS` from `split.py`; `BUSY_JS` from `jsfviews.py` | the replay |
| `twinlab2.replay.selectors` | `_PART`, `_compound`, `_parse`, `_fits`, `_matches` from `behaviour/drafts.py` | `replay.scope` (element exclusions) |
| `twinlab2.labagent` | `LabError`, `Agent` from `lab.py` | Verify's reset check |
| `twinlab2.devinapi.client`, `.config` | `devin/client.py`, `devin/config.py` | Build with Devin (`twinlab2.devin`) |
| `twinlab2.devinapi.jobs` | `_MONEY_WORDS`, `map_status` from `devin/jobs.py` | `twinlab2.devin` |
| `tests/recorder/desktop_drive.py` (test only) | `desktop/drive.py` | the CaseDesk end to end |

## What was cut, and why

A replay of a TwinLab2 pack, a recording and the portal never reach these paths. Each import was
replaced by `raise NotAbsorbed(...)` (`twinlab2_contract.upstream`) and the dead code after it
removed, so a caller that reaches one anyway gets a plain error. Verify turns that into a scenario
that was **not run** (with the reason), never a pass.

| Cut | Where | Why it is not reached |
|---|---|---|
| `fidelity.reports_dir`, `fidelity.save` (Twin Lab's `lab`, `conformance`) | `twinlab2.replay.fidelity` | Twin Lab's CLI keeps reports under its lab state (`--app`); TwinLab2 stores the report with the release |
| `durable.add_app`, `durable.assess` (`conformance`) | `twinlab2.replay.durable` | only `save` and Twin Lab's readiness gate call them |
| `visual.unsettled_pictures` (`recorder.next_action`) | `twinlab2.replay.visual` | only `visual.template`, which writes Twin Lab's `visual.yaml` template from the CLI; the server never imports the recorder package |
| `Public.collect`, `active_for`, `draft` (`har`, `factory`, `lookfeel`) | `twinlab2_contract.evidence.publicdata` | the owner's public-data declaration is drafted and collected by Twin Lab's pack builder; recording and replay only read the active declaration (`current`, `refused`) |
| the rest of `split.py` (1,260 lines), `jsfviews.py` (760), `behaviour/drafts.py` (650), `lab.py`, `devin/jobs.py`, `analyse/uipath.py`, `models.py` | (only the definitions above were taken) | they build packs, run labs, or analyse automation projects |

How this was decided: an AST walk over the pinned tree gave each module's imports, at module level
and inside functions; every import into a module outside the copied set was then checked against
its callers. The milestone v3 replay (`milestone-trial/judge-recordings/ws-20261120T173343Z-fa325a22`,
Orders 3.0.0 on its clean-machine image) was run with the pinned tree, tracing the modules it
loads (18 of labctl's), and again with the copy: both PASS WITHIN DECLARED SCOPE, 8/8 acceptance
cases, 65 steps, the same report but for artifact paths; Verify's `independent.run` with the copy
also passed the reset check ("reset restored the baseline (1 table(s))"), as Verify did on
2026-11-20.

## Changes

- Imports renamed (`labctl.workstation` to `twinlab2_recorder.workstation`, and so on); Twin Lab
  module names in comments and messages refer to "Twin Lab's ..." where the module was not copied.
- The supervisor's collector entry points (`"twinlab2_recorder.workstation.supervisor:run_web"`
  and the others) and the `recorder` names written into recordings
  (`twinlab2_recorder.desktop.recorder`, ...) follow the rename.
- `botlog.models` takes `Contract` from `botlog.base`; `KINDS` there is an empty dict (TwinLab2
  does not load Twin Lab's YAML contracts).
- `twinlab2.replay.__main__` is new: the entry Verify runs, writing a `not-run` report when the
  replay raises `NotAbsorbed`.

## Taking a later Twin Lab change

There is no pin to move. To take a fix from twin-lab, diff the upstream module against the
copied commit and apply the change here by hand:

```powershell
git -C ../twin-lab diff 90bdd917414a24a4da7f09fb66e8edb49ba3bfee..origin/main -- lab/labctl/labctl/workstation/timeline.py
```

Read changes to `workstation/timeline.py` (`outbox`) and `workstation/flow.py` (`label_step`)
with particular care: they decide what a recorder sends. Then run the tests:
`test_the_collectors_still_build_the_fixture` fails when the references built from
`tests/fixtures/workstation-session/` change. Review the difference; if it is still contract 1.x
(only optional fields added), regenerate `tests/fixtures/references-from-the-collectors.json`,
otherwise the contract needs a new MAJOR version first
([Changing the contract](RECORDER-CONTRACT.md#changing-the-contract)). Say in the commit which
upstream commit the change came from.

## Open notes from the time of the pin

- The outbox could write a `recorder` object (`name`, `version`, `twinlab_commit`) itself; the
  recorder stamps it (`twinlab2_recorder.main.contract_references`).
- The outbox writes 1.1.0 also for a session recorded before event ids; the recorder sends such a
  session as 1.0.0.
- Collectors run in "spawn" processes (`workstation/supervisor.py`), so the frozen recorder calls
  `multiprocessing.freeze_support()` first (`twinlab2_recorder/main.py`, `__main__.py`).
- `twinlab2.devin.BuildDevin` adds two reads to the Devin client: `find_sessions(tag)` (the
  sessions an uncertain create may have started) and `list_messages(session)` (Devin's questions).
