# Setting up on the office PC

For moving development and testing to a Windows PC at the office: clone from GitHub, install,
run the demo portal, run the tests, build the recorder, pair it, and run the on-PC checks.
Every command is PowerShell, from the repository root unless it says otherwise.

## 1. Prerequisites

| Need | Version | Notes |
|---|---|---|
| Windows | 10 or 11, x64 | |
| Git | 2.35+ | |
| Python | **3.13** (3.12+ works; CI runs 3.13) | `python --version`; tick *Add to PATH* |
| Node.js | **22 LTS** (20.19+ works; CI runs 22) | includes npm |
| Microsoft Edge WebView2 Runtime | any current | The recorder's window. Already present on Windows 11 and on PCs with current Edge. Without it the recorder still runs in an Edge app window |
| Microsoft Edge | any current | The recorder's web channel drives the PC's Edge; no browser is bundled |
| PostgreSQL 14+ | optional | Only to run the portal tests against PostgreSQL (`TWINLAB2_TEST_DATABASE_URL`, as in `.github/workflows/test.yml`). SQLite is the default |
| .NET SDK 8 | optional | Only to rebuild the desktop reference app (CaseDesk) for the desktop checks |
| WiX 5 (`dotnet tool install --global wix --version 5.0.2`) | optional | Only to build the MSI yourself (needs the .NET SDK) |

**Corporate proxy and TLS inspection** (for pip, npm and git; the recorder has its own settings,
see `docs/recorder-deployment.md`):

```powershell
git config --global http.proxy http://proxy.corp:8080
npm config set proxy http://proxy.corp:8080
npm config set https-proxy http://proxy.corp:8080
$env:PIP_PROXY = "http://proxy.corp:8080"
```

If the proxy inspects TLS, export the inspection root certificate as PEM (Base-64) and point the
tools at it, or they fail with certificate errors:

```powershell
git config --global http.sslCAInfo C:\certs\corp-root.pem
npm config set cafile C:\certs\corp-root.pem
$env:PIP_CERT = "C:\certs\corp-root.pem"
$env:REQUESTS_CA_BUNDLE = "C:\certs\corp-root.pem"
```

The portal itself reaches nothing outside this PC in the demo.

## 2. Clone and install

```powershell
git clone https://github.com/karuppusamym/twin-lab2.git
cd twin-lab2
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if scripts are blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install -r requirements-test.txt
cd web; npm ci; cd ..
```

`requirements-test.txt` installs the portal, the contract, the recorder (editable) and the test
extras. Use the venv in every terminal (activate it again in a new one).

**npm quirk.** The development PC's `~/.npmrc` contains `os=linux`, so `npm ci` skips Windows-only
optional packages there. On a clean clone the web build and unit tests still passed, but if you
see *Cannot find module @rollup/rollup-win32-x64-msvc* (or an esbuild/rolldown binary), check
`npm config get os`: if it prints `linux`, remove that line from `~\.npmrc` or install with
`npm ci --os=win32 --cpu=x64`. A fresh office PC normally has no such setting.

## 3. Run the demo portal (13 applications, three users)

```powershell
cd web; npm run build; cd ..
python tools\demo_portal.py              # http://127.0.0.1:8782; --port to change; --scale for 50 apps
```

(It is a seeded copy of the real portal built from the `tests/fixtures/ten-app-session` recording:
10 applications recorded across web, desktop and mainframe, plus the catalog's own.) Sign in as:

| User | Password | Role |
|---|---|---|
| `alice` | `alice-demo` | owner of every application and portal admin |
| `bob` | `bob-demo` | builder of Billing |
| `vera` | `vera-demo` | viewer of Billing |

Port 8782 busy? Use `--port 8790`. Everything outside the portal (build provider, GitHub,
deployment, replay) is a local stand-in, so nothing is sent anywhere. Stop it with Ctrl+C.

## 4. Run the tests

```powershell
python -m pytest -q                      # portal + recorder (the full run is ~10 minutes)
python -m pytest -q tests/recorder       # just the recorder
cd web; npm test; cd ..                  # web unit tests
cd web; npm run lint; npm run typecheck; cd ..
cd web; npm run test:a11y; cd ..         # axe accessibility checks
```

- The browser journey (`tests/e2e`) needs Playwright's Chromium: `python -m playwright install chromium`
  (through a proxy it needs `HTTPS_PROXY` set).
- PostgreSQL run: set `$env:TWINLAB2_TEST_DATABASE_URL = "postgresql://postgres:<password>@localhost:5432/postgres"`
  on a server initialised with locale `C` (the tests create and drop schemas in it).
- Tests that need a visible window or a live desktop are skipped or live in the checklist below.
- CI (`.github/workflows`) runs the same: `test`, `web`, `e2e`, `image`, `supply-chain`, `recorder-dist`.

## 5. Build the recorder and MSI

Easiest, no build tools: download the MSI that CI built. On GitHub open **Actions → recorder-dist →
the latest run on main → Artifacts → `twinlab-recorder-msi`**, or:

```powershell
gh run list --workflow recorder-dist --branch main --limit 1
gh run download <run-id> --name twinlab-recorder-msi --dir C:\temp\tl2-msi
```

To build here:

```powershell
python -m pip install -r requirements-recorder.txt
python recorder\build.py                 # dist\TwinLabRecorder\TwinLabRecorder.exe (a few minutes)
python recorder\build.py --msi           # also dist\TwinLabRecorder-<version>-x64.msi (WiX 5 + .NET SDK)
dist\TwinLabRecorder\TwinLabRecorder.exe version
```

`version` must end with a `window: pywebview …` line (not `missing: …`) for the window to work.
The build is **unsigned**. On a managed PC endpoint protection may stop an unsigned program that
hooks the keyboard; ask IT to allow it or sign it (`TWINLAB_SIGN_CERT` or `TWINLAB_SIGN_PFX`, see
`recorder/build.py`). Installing the MSI needs administrator rights; running the unpacked folder
does not. Install and policy options (`PORTALURL`, `PROXY`, `CABUNDLE`…) are in
`docs/recorder-deployment.md`.

## 6. Pair the recorder

1. Run a portal: the demo (section 3) for a quick try, or the real one
   (`python -m twinlab2.portal`, http://127.0.0.1:8765, README *Run the portal for real*).
2. In the portal, open an application → **Machines** and create a pairing code.
3. Start `TwinLabRecorder.exe`, press **Pair this PC**, enter the portal address, the code and a
   name. The portal must be HTTPS unless it is this machine (`http://127.0.0.1:8782` is fine).
   Command line: `TwinLabRecorder.exe pair --portal <url> --code <code> --name <pc-name>`.
4. Within about 20 seconds *Offered to me* fills. A step-by-step recording walkthrough is
   `docs/RECORDING-GUIDE.md`.

## 7. The on-PC checks

These open real windows and hook the keyboard, so run them in one sitting, with nothing else in the
foreground.

- **GO-DESKTOP checklist**: `recorder/GO-DESKTOP-CHECKLIST.md`. 12 numbered checks (pair, offered
  sessions, record, pause, close during recording, offline portal, upload queue, DPI, desktop
  capture, excluded apps, the window's look) plus the portal's desktop replay. Mark each ✅ or ❌.
- **Desktop batch** (builds the reference CaseDesk, records it live, replays it; ~7 minutes,
  clicks for real): `powershell -ExecutionPolicy Bypass -File tools\rebuild_proof\desktop_batch.ps1 -GoDesktop`.
  It reads the reference apps from a sibling `..\twin-lab` checkout (see section 8).
- **Managed-PC trial items** (none done yet; `docs/design.md` *What works today* lists the
  status labels): install the MSI silently (`msiexec /i … /qn /l*v install.log`) and check exit
  code 0; start the recorder as a normal user; pair over the office proxy (and with TLS
  inspection, via `CABUNDLE`); see whether endpoint protection or AppLocker blocks the unsigned
  exe or its keyboard hooks; record a web, a desktop and (if available) a mainframe target;
  `msiexec /x` leaves no data (unless `KEEPDATA=1`). Note each outcome in the checklist's result
  column or a new note under `docs/`, so the status label can move from *Locally tested* to
  *Externally validated*.

## 8. Where proofs and reports live

The repository holds code, docs and fixtures. The evidence from earlier trials sits **outside** it,
in sibling folders of the project (`prod-echo`): `rebuild-proof`, `rebuild-proof-recorder14`,
`rebuild-trial`, `milestone-trial`, `desktop-trial`, `colour-trial`, `review-notes`, plus
`twin-lab` (the original reference apps).

You do **not** need any of it to clone, test, build or demo. Copy only what you want to read or
extend:

- `twin-lab` (the sibling checkout, clone or copy): needed for `tools\rebuild_proof` and the
  desktop batch, which read the reference apps from `..\twin-lab` (`TWINLAB_CHECKOUT` overrides).
- `milestone-trial\REPORT.md`, `colour-trial\TRIAL-NOTES.md`, `rebuild-trial\README.md`,
  `review-notes\*.md`: the written reports, small.
- The `blind*`, `built`, `builds` and attempt folders are large (hundreds of MB) and only matter
  if you want to re-judge those runs.

New evidence goes to `..\rebuild-proof` by default (`REBUILD_PROOF_EVIDENCE` changes it). Keep it
out of git; summarise results in `docs/VERIFICATION.md`.

## 9. Continuing with Claude Code

Read first, in this order: `README.md`, `docs/design.md` (what is built and what is only
*Locally tested*), `docs/PLAN.md` (what is next), `docs/VERIFICATION.md` (what was checked and its
limits), `CONTRIBUTING.md`, `docs/RECORDING-GUIDE.md`, `docs/recorder-deployment.md`,
`recorder/GO-DESKTOP-CHECKLIST.md`.

Rules the project works by:

- **Merge only when green.** `main` is protected and requires the `test` and `guard` checks; open a
  PR, wait for every check, then merge. Never merge a red PR.
- **No force push**, ever; fix forward with a new commit.
- **No visible windows without asking.** Tests run headless; anything that opens a window or drives
  the desktop waits for an explicit "go desktop" from the person at the PC.
- Dependabot PRs: merge only when all checks pass and the change is low-risk. Majors that need
  toolchain work are closed with the reason and ignored in `.github/dependabot.yml`.
- Keep personal details (names, emails, machine names, internal URLs) out of commits and docs.
- Label claims honestly (*Implemented*, *Locally tested*, *Externally validated*) as `docs/design.md`
  defines them.

Start with `claude` in the repository folder and ask it to read the docs above.
