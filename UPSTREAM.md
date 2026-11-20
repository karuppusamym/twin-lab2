# Twin Lab: the pinned recorder dependency

TwinLab2 builds its recorder from Twin Lab's collectors, `labctl` in
[karuppusamym/twin-lab](https://github.com/karuppusamym/twin-lab) (Apache-2.0). It uses one pinned
commit, never a sibling checkout or a branch. The portal and the recorder agree on what crosses
between them through the versioned [recorder contract](RECORDER-CONTRACT.md).

## The pin

[`twinlab.lock.json`](twinlab.lock.json):

| Field | Value |
|---|---|
| Repository | `https://github.com/karuppusamym/twin-lab` |
| Commit | `430dfe538965b46ccbe824f8f2b4f415c5576483` (twin-lab `main`, 2026-11-19: merge of #59, which includes the workstation recorder, mainframe and UiPath support) |
| Tree | `lab/labctl/labctl` |
| Tree SHA-256 | `e763f0eeb11f8f1295d5c4383602dcc3dcee622e26250b4e1640402a23dbdde5` |
| Recorder contract | `1.0.0` |

```powershell
python -m agent.twinlab fetch
python -m agent.twinlab verify
```

`fetch` downloads that commit's archive from GitHub. It takes only `lab/labctl/labctl`, only regular
files, never links or paths outside the tree. It computes the tree digest and refuses the copy when
the digest is not the lock's. The copy goes to `vendor/twin-lab-430dfe538965/`, which Git ignores.
Without network access, use `--from-checkout <a twin-lab clone>`. It takes the same commit with
`git archive` and passes the same check.

The digest covers file paths and contents, with line endings normalised to LF and `__pycache__`
left out. It is therefore the same from GitHub's archive, from `git archive` and from a Windows
checkout with `autocrlf`. All three were checked for this pin. The archive's own bytes are not
pinned, because GitHub does not promise byte-stable archives.

## Why a pinned, digest-checked source and a built folder

Managed PCs never see Twin Lab. They get the built `TwinLabRecorder` folder. `python agent/build.py`
bundles Python, the pinned `labctl` and the contract schema into it. It writes `_build.json` with the
commit, the tree digest, `"pinned": true` and the contract version, and it signs the folder when a
certificate is given. A PC therefore needs no Python, no Git, no network access to GitHub and no
second repository. The build host checks the one thing that matters, that the collectors are
exactly the reviewed commit.

The alternatives considered:

- **A requirements pin.** twin-lab's root `pyproject.toml` packages `labctl` as `twin-lab`, so
  `twin-lab[desktop] @ git+https://github.com/karuppusamym/twin-lab@<commit>` would work. It is
  weaker on three counts. pip cannot hash-check a Git requirement. GitHub does not promise that
  archive bytes stay the same, so a hash of the archive file could break. And it installs all of
  twin-lab's dependencies on the build host (PostgreSQL, SQL Server and Oracle drivers, FastAPI…),
  where the recorder needs only a few. Those few are in `requirements-recorder.txt`.
- **Committing a vendored copy** (about 200 files). It is reproducible offline, but the copy can be
  edited in place without anyone noticing, and it bloats every diff that moves the pin. The digest
  check makes the copy fetched at build time just as trustworthy.

When twin-lab publishes a release of the recorder, either a wheel on an index or a release
asset with its checksum, switch to that version with pip's `--require-hashes`. The contract does
not change.

`TWINLAB_PATH=<checkout>` remains for development only, to try collectors that are not pinned yet.
The recorder run from source then prints a note. `agent/build.py` refuses it unless
`TWINLAB_ALLOW_UNPINNED=1` is set, and the resulting `_build.json` says `"pinned": false`.

## Moving the pin

1. Review what changed upstream in a separate twin-lab clone:

   ```powershell
   git -C ../twin-lab log --oneline 430dfe538965b46ccbe824f8f2b4f415c5576483..origin/main -- lab/labctl/labctl
   git -C ../twin-lab diff --stat 430dfe538965b46ccbe824f8f2b4f415c5576483..origin/main -- lab/labctl/labctl
   ```

   Read the changes to `workstation/timeline.py` (`outbox`) and `workstation/flow.py` (`label_step`)
   with particular care: they decide what a recorder sends. Also compare the dependencies in
   `pyproject.toml` with `requirements-recorder.txt`.
2. Put the new commit in `twinlab.lock.json`, both `commit` and the `archive` address, and set
   `tree_sha256` to the output of `python -m agent.twinlab digest <clone>/lab/labctl/labctl`, with
   that clone at the new commit and clean. Run `python -m agent.twinlab fetch`.
3. Run `python -m unittest discover -s tests`. `test_the_pinned_twinlab_still_builds_the_fixture`
   fails when the new collectors build different references from `tests/fixtures/workstation-session/`.
   Review the difference. If the new output is still contract 1.x (only optional fields added),
   regenerate `tests/fixtures/references-from-pinned-twinlab.json`. Otherwise the contract needs a
   new MAJOR version first ([Changing the contract](RECORDER-CONTRACT.md#changing-the-contract)).
4. Build, record a check run (RECORDING-GUIDE.md, *A check run*) and update this file's table in
   the same commit.

## Integration points

| Integration | Upstream entry point |
|---|---|
| Workstation recorder (`agent/`) | `labctl.workstation` (`session`, `levels`, `timeline`, `flow`, `tn3270`, `robot`, `propose`) |
| References sent to the portal | `labctl.workstation.timeline.outbox`, then stamped and validated by `agent.main.contract_references` |
| Browser launch or attach (`capture.py`) | `lab/labctl/labctl/evidence_cli.py` and `lab/labctl/labctl/recorder.py` |
| Windows desktop attach (`capture.py`) | `lab/labctl/labctl/desktop/cli.py` and `lab/labctl/labctl/desktop/recorder.py` |
| Manual OpenShift deployment reference | `lab/labctl/labctl/ocp_manual.py`. TwinLab2 renders its own smaller resource set for prebuilt images. |

`capture.py` uses the pinned copy by default. It records the lock's commit in `session.json`. It can
still be pointed at a clean checkout with `--twin-lab`.

## Follow-up for twin-lab

When twin-lab emits these itself, the recorder stops stamping them:

- `timeline.outbox` should write `"contract_version": "1.0.0"` at the top of `portal-outbox.json`.
- `timeline.outbox` should write a `recorder` object (`name`, `version`, `twinlab_commit`).

Separately, tagged releases with a published wheel or a checksummed asset would allow a hashed
requirements pin.
