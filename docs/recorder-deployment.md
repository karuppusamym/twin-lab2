# Deploying TwinLab Recorder on managed PCs

This guide is for the IT team that puts the recorder on office PCs with Intune, SCCM
(Configuration Manager) or Group Policy. It covers the MSI and its properties, the machine
policy, the network (proxy, TLS inspection, endpoints), endpoint protection, updates, what the
recorder collects, and how to remove it.

The people who record use the [recording guide](RECORDING-GUIDE.md). The portal side is in
[deploying on OpenShift](deploying-on-openshift.md).

## The package

| | |
|---|---|
| File | `TwinLabRecorder-<version>-x64.msi`, built by `python recorder/build.py --msi` (WiX 5) or by the `recorder-dist` CI job (artifact `twinlab-recorder-msi`) |
| Scope | per machine (`ALLUSERS=1`), x64, Windows 10 or 11 |
| Installs to | `%ProgramFiles%\TwinLab Recorder\`, plus a Start-menu shortcut. Nothing runs at logon, no service, no scheduled task, no driver |
| Size | about 150 MB installed (about 50 MB without the web channel); the MSI is compressed |
| Upgrade code | `{C93893E8-7F79-4E4F-9A37-332907954EDA}`. It never changes. A newer MSI replaces an older one (major upgrade), and a downgrade is refused |
| Product code | new for every version: read it from the MSI with Orca, or `python recorder/installer/build_msi.py --inspect <msi>` |
| Exit codes | `0` done, `3010` done but a restart is needed (a file was in use: the recorder was open), `1618` another installation is running, `1603` failed (see the log) |

### Silent install and uninstall

```powershell
msiexec /i TwinLabRecorder-0.3.0-x64.msi /qn /norestart /l*v "%TEMP%\twinlab-install.log"
```

```powershell
msiexec /x TwinLabRecorder-0.3.0-x64.msi /qn /norestart
```

`msiexec /x {ProductCode} /qn /norestart` works the same way. Close the recorder on the PC
before an upgrade or an uninstall; otherwise Windows Installer asks for a restart (`3010`).

**Intune** (Windows app, line-of-business, or Win32 app around the MSI): install command as
above, uninstall `msiexec /x {ProductCode} /qn`, install behaviour *System*, detection rule *MSI
product code*. Or use a registry detection rule:
`HKLM\SOFTWARE\TwinLab\Recorder` value `Version`.

**SCCM**: an MSI deployment type, run *whether or not a user is logged on*, with the same
commands. Detection is the product code.

### MSI properties

Each policy property writes its value under `HKLM\SOFTWARE\Policies\TwinLab\Recorder` (see
*Machine policy*). A major upgrade keeps the values set by an earlier install, so you do not have
to pass them again; a value you pass replaces the old one. Uninstalling removes them.

| Property | Example | Effect |
|---|---|---|
| `PORTALURL` | `https://twinlab.apps.example.com` | The office's portal. The pairing dialog is pre-filled with it, and `pair` works without `--portal` |
| `LOCKPORTAL` | `1` | Pairing with any other portal is refused, and a PC paired elsewhere must pair again |
| `PROXY` | `system` (default), `direct`, `http://proxy.corp:8080` | Which proxy the recorder uses (*Network*) |
| `PROXYBYPASS` | `*.corp.example.com;10.*` | Hosts reached directly when `PROXY` is a proxy URL |
| `CABUNDLE` | `C:\ProgramData\Corp\tls-inspection-root.pem` | Extra root CAs (PEM) on top of Windows' store |
| `UPDATES` | `off`, `notify` (default), `auto` | How updates from the portal are handled (*Updates*) |
| `UPDATESIGNERS` | `3F2A…;9B1C…` | Thumbprints of the code-signing certificates an update must be signed with |
| `ALLOWUNSIGNEDUPDATES` | `1` | Accept an unsigned update. Only for a trial without a certificate |
| `ALLOWUSEREXCLUDECHANGES` | `0` | The person cannot change their own exclude list either |
| `KEEPDATA` | `1` | On uninstall: keep the recorder's data (*Removing it*) |
| `ADDLOCAL` | `Core` | Install without the web channel (*Features*) |

```powershell
msiexec /i TwinLabRecorder-0.3.0-x64.msi /qn /norestart PORTALURL=https://twinlab.apps.example.com LOCKPORTAL=1 UPDATES=off
```

### Features

| Feature | What it is |
|---|---|
| `Core` (always) | The recorder with its desktop channel (UI Automation) and mainframe channel (TN3270) |
| `WebChannel` (default on) | Playwright's driver (`node.exe` and its scripts, about 95 MB). It drives the PC's Edge or Chrome to record web applications |

`ADDLOCAL=Core` leaves the web channel out. A web target then fails its check before recording,
with *the web channel is not installed on this PC*, and `TwinLabRecorder.exe selfcheck` says the
same. No browser is installed with the recorder; it uses the PC's Edge.

### Checking an installation

```powershell
& "$env:ProgramFiles\TwinLab Recorder\TwinLabRecorder.exe" selfcheck
```

```powershell
& "$env:ProgramFiles\TwinLab Recorder\TwinLabRecorder.exe" policy --url https://twinlab.apps.example.com
```

`selfcheck` imports every module of the build and says whether the web channel is installed. Its
exit code is 0 when everything imports. `policy` prints the machine policy in effect, where each
value came from (registry or file), any errors in it, the network settings, and the proxy that
would be used for the URL. Both run without a window and are safe to run as SYSTEM.

## Machine policy

The policy is read every time it is needed, so a change applies without restarting the recorder.
It is read from two places:

1. **The registry**: `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\TwinLab\Recorder`. Use Group Policy
   Preferences, Intune (a PowerShell or Win32 app that writes the key, or a custom ADMX), or the
   MSI properties above.
2. **A file**: `%ProgramData%\TwinLabRecorder\policy.json` (the `TWINLAB_RECORDER_POLICY`
   environment variable can name another file). Copy it with the package or with Intune. Make
   it writable only by administrators.

A registry value wins over the same value in the file. Both win over what the person sets in the
recorder and over command-line options (`--proxy`, `--ca-bundle`).

| Registry value | Type | policy.json | Meaning |
|---|---|---|---|
| `PortalUrl` | REG_SZ | `portal_url` | The office's portal |
| `LockPortal` | REG_SZ or REG_DWORD `1` | `lock_portal` | Only that portal can be paired |
| `Proxy` | REG_SZ | `proxy` | `system`, `direct`, or `http://[user:password@]host:port` |
| `ProxyBypass` | REG_SZ, `;`-separated | `proxy_bypass` (list) | Hosts reached directly: `*.corp`, `10.*`, `10.0.0.0/8`, `<local>` |
| `CaBundle` | REG_SZ | `ca_bundle` | PEM file of extra root CAs (environment variables are expanded) |
| `Updates` | REG_SZ | `updates` | `off`, `notify`, `auto` |
| `UpdateSigners` | REG_MULTI_SZ or `;`-separated | `update_signers` (list) | Allowed code-signing certificate thumbprints |
| `AllowUnsignedUpdates` | REG_SZ or REG_DWORD | `allow_unsigned_updates` | Accept unsigned updates |
| `Exclude` | REG_MULTI_SZ, one `kind:pattern` or `kind:pattern\|name` per line | `exclude` (list of `{kind, pattern, name}`) | Locked exclude entries |
| `AllowUserExcludeChanges` | REG_SZ or REG_DWORD `0` | `allow_user_changes` | `0`/`false`: the person's own list is locked too |

```json
{
  "portal_url": "https://twinlab.apps.example.com",
  "lock_portal": true,
  "proxy": "system",
  "ca_bundle": "C:\\ProgramData\\Corp\\tls-inspection-root.pem",
  "updates": "off",
  "exclude": [
    {"kind": "process", "pattern": "SAPlogon.exe", "name": "SAP Logon"},
    {"kind": "web", "pattern": "hr.corp.example.com", "name": "HR portal"},
    {"kind": "title", "pattern": "* - Confidential", "name": "Confidential documents"}
  ],
  "allow_user_changes": false
}
```

If the file cannot be read (it is not JSON, or not an object), `policy` reports it. The exclude
list is then locked, and no entry of the file applies, so a broken file can never look like a
policy that applies. Values that are not understood (for example `"updates": "sometimes"`) are
reported and their defaults are kept.

### The exclude list

An application on the exclude list is not recorded at all: no UI Automation tree, no event, no
picture, no step, no network entry. Only its name is kept, once, so the recording can say that N
applications were excluded on this PC. The person sees the list on the **Excluded apps** tab. The
office's entries are marked *Office policy (locked)* and cannot be removed there.

| Kind | Pattern | Matches |
|---|---|---|
| `process` | `WINWORD.EXE`, `adobe*.exe` (`.exe` optional) | the program's executable name, case-insensitive |
| `title` | `* - Excel` (with `*`/`?`: the whole title), `Personal` (without: anywhere in it) | the window title, case-insensitive |
| `web` | `mail.google.com`, `*.slack.com`, `https://intranet.example.com/hr/` | a host and its subdomains, or an address prefix on whole path segments |

Every PC starts with defaults that the person can edit: Office (Word, Excel, PowerPoint, OneNote,
Access, Outlook), Teams, the common password managers, and web mail. The local list always wins
over the portal: an application the portal asks for, but this PC excludes, is not recorded, and
the session's notes say so.

## Network

### Endpoints and firewall

The recorder makes outbound HTTPS calls to **one host**, the portal, and only while it is paired:

| Call | When |
|---|---|
| `POST /api/agent/pair` | pairing (once) |
| `GET /api/agent/sessions`, `GET /api/agent/apps` | every 15 to 20 s while the window is open and paired |
| `POST /api/agent/references` | when the person sends a recording's summary |
| `/api/uploads...` | when the person uploads a recording (resumable, 8 MB chunks by default) |
| `GET /api/agent/update`, `/api/agent/update/<file>` | every 6 hours (unless `Updates` is `off`), and the download |

If the portal stores uploads in S3-compatible storage with direct uploads, the chunks go to the
storage host's presigned URLs instead. Allow that host too, or keep the portal in proxy mode.

Plain HTTP is accepted only for this PC itself (localhost). There is no switch that turns off
certificate checks.

The recorder also listens, **on 127.0.0.1 only**, on two kinds of port. Neither needs an inbound
firewall rule, and Windows Firewall does not ask:

- the session browser's DevTools port: a random free port, or `9333` with the kept profile;
- the TN3270 proxy for mainframe targets: `23270`, `23271` and so on. The terminal connects here,
  and the recorder connects out to the real host (TN3270 or TN3270 over TLS). Allow outbound
  traffic to the mainframe hosts the recordings need.

### Proxy

For each request, the recorder decides in this order:

1. the policy's `Proxy`: `direct`, or a proxy URL (with `ProxyBypass`). The default is `system`,
   which goes on to step 2;
2. the person's own `--proxy`, only when the policy says `system`;
3. **the Windows proxy for the signed-in user** (Internet Options / WinINET, as WinHTTP reads it):
   automatic detection (WPAD) and a PAC script (`AutoConfigURL`) are evaluated by Windows itself.
   A fixed proxy is used with its bypass list, including `<local>`;
4. `HTTPS_PROXY` / `NO_PROXY` environment variables;
5. direct.

HTTPS goes through the proxy as a `CONNECT` tunnel. A proxy URL can carry Basic credentials
(`http://user:password@proxy:8080`). **Windows-integrated proxy authentication (NTLM, Kerberos)
is not supported.** Allow the portal's host through the proxy without authentication, or put it
in the bypass list. SOCKS proxies are not used.

### TLS inspection

The recorder trusts Windows' certificate store: the machine's and the user's trusted roots. A
TLS-inspection root that Group Policy or Intune puts in *Trusted Root Certification Authorities*
is therefore trusted with no extra step. If the inspection root is not in the Windows store, give
its PEM file as `CABUNDLE` / `CaBundle`. It is added to the store's roots and does not replace
them.

### Retries and the offline queue

A call that is safe to repeat (reading sessions, upload chunks, starting or completing an upload)
is retried up to 3 times, with exponential backoff and jitter, when the network fails or the
portal answers 429, 502, 503 or 504. A `Retry-After` header is honoured. A recording being
uploaded is kept in the queue on disk (`upload-queue\` in the data folder) until the portal
confirms its SHA-256. If the network goes away in the middle of an upload, the queue waits
(backing off to 10 minutes), survives the recorder or the PC restarting, and resumes from the
last byte the portal has. `tests/server/test_recorder_deployment.py` tests this by cutting every
connection halfway through a chunk.

## Endpoint protection (AV / EDR)

The recorder does things that EDR products watch for, because recording needs them:

- **low-level mouse and keyboard hooks** (`SetWindowsHookEx` `WH_MOUSE_LL` / `WH_KEYBOARD_LL`),
  only while a desktop session records. It keeps what was clicked and the *shape* of what was
  typed, never the keys;
- **UI Automation** of other programs' windows, and `PrintWindow` pictures of target windows when
  the person turns pictures on;
- starting Edge or Chrome with a **DevTools port** on 127.0.0.1 (Playwright, `node.exe`);
- a **local TCP proxy** between a 3270 terminal and the mainframe;
- child processes of `TwinLabRecorder.exe`, one per channel, and `powershell.exe` with no window
  (Windows' built-in OCR for canvas screens, and the signature check of an update).

What to do:

1. **Sign it.** This repository has no code-signing certificate. The office signs with its own
   certificate (*Code signing*). Then allow the publisher, rather than a file hash that changes
   with every version.
2. Allow `%ProgramFiles%\TwinLab Recorder\TwinLabRecorder.exe` and
   `%ProgramFiles%\TwinLab Recorder\_internal\playwright\driver\node.exe` in AppLocker / WDAC
   (publisher rule once signed), and exclude them from ASR rules that block hooks or
   credential-like behaviour, if your EDR flags them.
3. The executables are **not packed** (no UPX): packers are a common false positive, and packing
   would break a signature.

## Code signing

The build signs when it is given a certificate, and skips signing (saying so) when it is not.
The office supplies the certificate; nothing in this repository holds one.

| Variable | |
|---|---|
| `TWINLAB_SIGN_CERT` | SHA-1 thumbprint of a code-signing certificate in the user's or machine's store (a hardware token or HSM-backed certificate appears there too) |
| `TWINLAB_SIGN_PFX`, `TWINLAB_SIGN_PFX_PASSWORD` | or a `.pfx` file and its password (a CI secret) |
| `TWINLAB_SIGN_TIMESTAMP` | RFC 3161 timestamp server, default `http://timestamp.digicert.com` |
| `TWINLAB_SIGNTOOL` | `signtool.exe`, if it is not on `PATH` or in the Windows SDK |

```powershell
$env:TWINLAB_SIGN_CERT = "<thumbprint>"; python recorder/build.py --msi
```

This signs `TwinLabRecorder.exe` and `node.exe` before the MSI is built, then the MSI itself, and
checks each with `signtool verify /pa`. Give the certificate's thumbprint to the PCs as
`UpdateSigners`, so that only your signed builds are installed as updates.

## Updates

The portal offers a recorder version when an administrator puts the MSI and a `latest.json` in
the folder that `TWINLAB2_RECORDER_RELEASES` names on the portal:

```json
{"version": "0.3.0", "file": "TwinLabRecorder-0.3.0-x64.msi",
 "sha256": "<SHA-256 of the MSI>", "notes": "What changed", "published_at": "2026-11-21T10:00:00Z"}
```

The portal checks the MSI's SHA-256 against `latest.json` before it offers the file
(`GET /api/agent/update`, schema `RecorderUpdate` in `api/openapi.yaml`). A half-copied file is
never offered. On the PC, the policy's `Updates` value decides what happens:

| `Updates` | What the recorder does |
|---|---|
| `off` | Never asks. Use this when Intune or SCCM pushes new versions (recommended on managed PCs) |
| `notify` (default) | The **Settings** tab says a version is available. **Install update…** downloads it, checks it, and starts the installer (Windows asks for an administrator's consent). `TwinLabRecorder.exe update --install` does the same |
| `auto` | Downloaded, checked and installed silently (`msiexec /qn`) when nothing is recording, if the recorder runs with the rights to install a per-machine MSI. Otherwise it behaves as `notify` |

Before an update is installed, the downloaded file must have exactly the size and SHA-256 the
portal announced. It must also carry a **valid Authenticode signature**, made by one of the
`UpdateSigners` certificates when that list is set. An unsigned MSI is accepted only with
`AllowUnsignedUpdates` (and no `UpdateSigners`). A signature that is present but not valid is
always refused. An update is **never installed while a session records**: the recording holds a
lock in the data folder, the window waits until the recording has ended, and the command line
refuses.

## What is collected

Everything stays on the PC until the person decides to send it:

- **Recordings**, in `%LOCALAPPDATA%\TwinLabRecorder\recordings\`, per Windows user: the steps
  in the target applications (what was clicked or changed, and in which control), screen
  structure, the session journal, and web page snapshots and network entries (HAR) for web
  targets. Typed values and values read from controls are kept only as their **shape** (`C-1001`
  becomes `X-9999`). Password fields are not kept even as a shape. Desktop pictures, when turned
  on, have every data region painted over.
- **Other applications** used during a session are recorded only as having been used, without
  their names. **Excluded** applications are not recorded at all.
- **Sent to the portal** when the person presses *Send* (or `send`): a summary (the references):
  applications, steps, timeline and completeness. The person sees it before sending.
- **Uploaded** only when the person presses *Upload*: the whole recorded session, as a ZIP.
- **Settings and the pairing token**, in `settings.json`. The token is protected with Windows
  DPAPI for the user, so a copied folder carries no usable token.
- **Logs**: `logs\` in the same folder (a log per recording, update logs).
- **The kept browser profile**, only if the person chooses it (`browser-profile\`).

There is no telemetry, no analytics and no call to any host other than the portal (and its
storage, for direct uploads).

## Removing it

`msiexec /x … /qn` removes the program, the Start-menu shortcut and the policy values it wrote.
It then runs `TwinLabRecorder.exe cleanup --all-users` as SYSTEM, which deletes, for **every
user profile** on the PC, `%LOCALAPPDATA%\TwinLabRecorder` (recordings, the upload queue with
anything not yet uploaded, settings and the pairing token, logs, the kept browser profile) and
the OCR helper script in the profile's temp folder, and also deletes
`%ProgramData%\TwinLabRecorder` (the policy file).

- `KEEPDATA=1` on the uninstall keeps all of it, for example when you replace the recorder by
  hand.
- A major upgrade never deletes data.
- Registry policy values that Group Policy or Intune wrote themselves (not through the MSI
  properties) stay until that policy is removed.
- The machine stays listed on the portal's Machines page until someone revokes it there.

## A trial on a managed PC

Before the wide rollout, run through this on one managed PC. These checks cannot be made in CI.

1. Deploy the signed MSI through Intune or SCCM with `PORTALURL`, `LOCKPORTAL=1` and `UPDATES=off`;
   check `selfcheck` and `policy` as SYSTEM and as the user.
2. Check that the EDR does not block the hooks, `node.exe` or the DevTools port. Record a desktop
   session and a web session.
3. Pair through the office proxy (PAC or a fixed proxy), with TLS inspection on, and send a
   recording. Upload one, unplug the network in the middle, plug it back in, and check that it
   resumes.
4. Put a newer signed MSI on the portal, set `UPDATES=notify` on the PC, and install it from the
   window. Check that it is refused while a recording runs.
5. Uninstall, and check that `%LOCALAPPDATA%\TwinLabRecorder` is gone for each user who used it.
