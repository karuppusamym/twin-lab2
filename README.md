# TwinLab2

TwinLab2 turns recordings of how people use a set of applications into runnable, versioned demo
copies of those applications. An owner reviews a recording, decides which applications it found
are in scope, and freezes a package per application for a coding agent. A build session hands the
package to the agent; Verify replays the recorded scenarios against what it built; a release that
passes is Tested and can be published for others to open or download.

This page is the user guide. How it works, what has been tested and the design decisions are in
[docs/design.md](docs/design.md).

## Run the demo

You need Python 3.12+ and Node 20.19+. From the repository root:

```powershell
python -m pip install -r requirements-test.txt
cd web; npm ci; npm run build; cd ..
python tools/demo_portal.py
```

Open http://127.0.0.1:8782 (`--port` picks another port). The demo is the real portal with a
seeded catalog. Everything outside the portal is a local stand-in (build provider, GitHub,
deployment, replay), so nothing calls a real service or costs money.

**Sign in** as one of three demo users. Each has their own password:

| User | Password | Role | What they see |
|---|---|---|---|
| `alice` | `alice-demo` | owner of every application, portal admin | recordings, scope triage, every application's journey, Setup and Users |
| `bob` | `bob-demo` | builder of Billing | Billing's frozen packages and build sessions, no recorded evidence |
| `vera` | `vera-demo` | viewer of Billing | Billing's page and its published release |

**What is seeded.** Seven catalog applications, then one recording, *Month-end close*, with web,
desktop and mainframe steps. The recording registers six more applications it found, which wait
for a scope decision, so the catalog shows **13 applications**. *Billing* has been taken through
the whole journey to a published release. *Accounts Receivable* has a frozen package and is ready
to build. With `--scale` the catalog grows to 50 applications plus a 2,000-event recording, to try
search, filters and paging.

## The main journey

1. **Home** shows what your role needs next. As `alice`, open the *Month-end close* recording.
2. The recording's **workboard** has one row per application it found, with its scope, review,
   package, build and release, and the next action for each. A timeline below shows each
   application's activity over the recording.
3. **Scope triage** decides the applications the recording discovered. For each one, choose
   **Replicate**, **Same as …** (merge into an existing application, previewed first) or **Not
   replicated (used only)**.
4. **Applications** lists them in collapsible groups with counts: **Needs a scope decision**,
   **In build**, **Tested**, **Published** and **Not replicated** (collapsed at first).
   An **application's page** follows one journey:
   **Choose application → Add examples → Review required behaviour → Connect repository → Build →
   Verification settings → Verify → Open or download**. A step that cannot be done yet says why
   and links to the fix. Add business scenarios here, preview the builder package file by file,
   and freeze it.
5. **Build** starts a build session with a provider on a frozen package revision. The session page
   is a chat: the provider's questions, your answers and the commit it produced.
6. **Verify this build** replays the scenarios against the built commit. If they pass, the release
   is **Tested**. **Publish** makes it available to viewers, who can then **Open or download** it.

As `bob`, start a build session on Billing (verifying and publishing stay with owners). As `vera`,
see what a viewer gets.

## Run the portal for real

```powershell
python -m pip install -r requirements.txt
cd web; npm ci; npm run build; cd ..
python -m twinlab2.portal            # http://127.0.0.1:8765
python -m twinlab2.worker            # build sessions, in a second terminal
```

The catalog is SQLite in `.state/` by default. For PostgreSQL, sign-in modes, artifact storage and
OpenShift, see [docs/design.md](docs/design.md) and
[docs/deploying-on-openshift.md](docs/deploying-on-openshift.md). To record applications on a
Windows PC, pair a machine under **Setup** and follow the
[recording guide](docs/RECORDING-GUIDE.md).

## Develop

- **Web app:** start the portal, then run `npm run dev` in `web/` and open http://localhost:5173.
  After a change to `api/openapi.yaml`, run `npm run gen` (web types) and
  `python tools/gen_api_models.py` (server models). CI fails when either is stale.
- **Tests:** `python -m pytest` for the server and recorder; `npm test` in `web/`.
  `npm run test:a11y` runs the accessibility checks: axe on the main pages, and colour contrast of
  the design tokens in the light and dark themes.

## Documentation

| Document | What it covers |
|---|---|
| [docs/design.md](docs/design.md) | architecture, what works today, the database, access and evidence, build sessions, releases, the design proposal |
| [docs/RECORDER-CONTRACT.md](docs/RECORDER-CONTRACT.md) | the recorder ↔ portal contract (now 1.4.0) |
| [docs/RECORDING-GUIDE.md](docs/RECORDING-GUIDE.md) | recording on a Windows PC |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | test results and their limits |
| [docs/PACKAGE-COMPLETENESS.md](docs/PACKAGE-COMPLETENESS.md) | what a builder package must contain |
| [docs/api-versioning.md](docs/api-versioning.md) | how the APIs are versioned |
| [docs/deploying-on-openshift.md](docs/deploying-on-openshift.md) | running the portal on OpenShift (not yet run on a cluster) |
| [docs/UPSTREAM.md](docs/UPSTREAM.md) | where the code copied from Twin Lab came from |

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md) and
[SECURITY.md](SECURITY.md). TwinLab2 is licensed under the [Apache License 2.0](LICENSE).
