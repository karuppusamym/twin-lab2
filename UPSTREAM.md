# Twin Lab source baseline

- Repository: `git@github.com:karuppusamym/twin-lab.git`
- Baseline: `c0c1fe15f19aed23caf51fd0eadee0854160c54c`
- Commit subject: Replay and judging: empty fragment, settle like the recorder, unsettled pictures, self-changing regions, scope subtrees (#51)
- Both local checkouts, `twin-lab` and `tl-carry`, resolved to this revision at implementation time.

TwinLab2 uses a checkout adapter. The collector modules stay in Twin Lab, and a session records the clean checkout's actual commit in `session.json`.

| Integration | Upstream entry point |
|---|---|
| Browser launch or attach | `lab/labctl/labctl/evidence_cli.py` and `lab/labctl/labctl/recorder.py` |
| Windows desktop attach | `lab/labctl/labctl/desktop/cli.py` and `lab/labctl/labctl/desktop/recorder.py` |
| Manual OpenShift deployment reference | `lab/labctl/labctl/ocp_manual.py`. TwinLab2 renders its own smaller resource set for prebuilt images. |

To evaluate upstream changes, compare a separate clean checkout with the baseline.

```powershell
git -C ../twin-lab log --oneline c0c1fe15f19aed23caf51fd0eadee0854160c54c..HEAD
git -C ../twin-lab diff --stat c0c1fe15f19aed23caf51fd0eadee0854160c54c..HEAD -- lab/labctl/labctl
python capture.py start .state/captures/upstream-trial --twin-lab ../twin-lab --web http://127.0.0.1:8080
```

Run the catalog checks and a representative capture with the candidate. Review any CLI, event format or sequence changes before updating this baseline. Keep historical session metadata for traceability.
