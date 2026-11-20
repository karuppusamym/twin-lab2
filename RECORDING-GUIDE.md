# Recording a UiPath process across web, desktop and mainframe

One session records everything the process does on this PC. The session's target applications are recorded in full. Any other application is recorded only as having been used. The recording stays on the PC; the portal, when you use one, receives only references.

## Before the day: once per PC

1. Copy the `TwinLabRecorder` folder to the PC (any folder, for example `C:\Tools\TwinLabRecorder`) and run `TwinLabRecorder.exe`. The build is not signed: Windows SmartScreen asks first (More info → Run anyway). If endpoint protection or AppLocker blocks it, the program needs to be signed or allowed by IT; it cannot work around that.
2. **Web, with UiPath:** UiPath drives a browser through its own extension. The recorder records the browser it starts, so that browser needs the extension:
   - run `TwinLabRecorder.exe browser` (or **Open the kept profile…** in the window). Edge opens with the recorder's kept profile at UiPath's extension page. Install **UiPath Web Automation**, sign in to the web applications if you want, and close that Edge window;
   - record with **Keep the recorder's browser profile** ticked (`--keep-browser-profile`). Its DevTools port is fixed, `9333`, so a robot that attaches by port finds it every time;
   - in the UiPath workflow, set **Use Application/Browser → User data folder mode** to *CustomFolder* and the **User data folder path** to the recorder's profile, `%LOCALAPPDATA%\TwinLabRecorder\browser-profile`. A browser UiPath opens then opens inside the recorder's browser instead of a separate one. If the robot closes the browser, the recorder opens it again with the same profile and keeps recording;
   - **Browser** chooses Edge or Chrome (`--browser chrome`): use the one the workflow uses.
   - Or start the browser yourself with a DevTools port (`msedge.exe --remote-debugging-port=9222 --user-data-dir=C:\Tools\edge-uipath`), install the extension in it once, and record with **attach to a running browser** `http://127.0.0.1:9222` (`--cdp http://127.0.0.1:9222`).
3. **Mainframe:** the recorder stands between the terminal and the host. In the UiPath project, change the terminal connection to the recorder's address for the recording run:
   - **Terminal Session, direct connection (TN3270):** host `127.0.0.1`, port `23270` (the first mainframe target; `23271` for a second), TLS off. The recorder connects to the real host itself, with TLS when the target is written `tn3270s://host:port`;
   - **an emulator (IBM PCOMM, Reflection, BlueZone, wc3270):** change the session profile's host to `127.0.0.1` and port to `23270` for the recording.
   Try it without the mainframe first (below, *A check run*). Stop the recording only after the robot has finished with the mainframe: stopping ends the terminal's connection to the host.

4. **Targets from the workflow:** **Targets from a UiPath project…** in the window (or `TwinLabRecorder.exe propose <project folder>`) reads the project's XAML and proposes the applications, web addresses, terminal hosts and browser it uses, with notes for Simulate input and for addresses held in variables. Check the list before recording.
5. **The robot's log:** set UiPath Assistant's logging level to *Verbose* for the recording run. The recorder then reads the robot's execution log at the end and says which UI activities left no recorded step (**Recordings on this PC**, and `robot\coverage.json` in the session folder).

6. **The portal's version:** if you use a portal, pair the PC (**Pair with portal…**). If the portal answers that the recorder or the portal must be updated, the two speak different versions of the [recorder contract](RECORDER-CONTRACT.md). `TwinLabRecorder.exe version` shows the recorder's version, its contract version and the Twin Lab commit it was built from. Get a recorder built for the portal's contract, or have the portal updated. Recordings already on the PC are kept and can be sent afterwards.

## Record

**With the portal:** in the portal give each application an identity on its page (`CaseDesk.exe`, `https://orders.example.test`, `tn3270://mainframe.example.test:23`), then **Sessions → Offer a session** to the PC, with the automation it is for. On the PC choose the session and **Start**.

**Without the portal:** in the window, **My own session**, one target per line:

```
CaseDesk.exe
https://orders.example.test
tn3270://mainframe.example.test:23
```

or from a command prompt:

```powershell
.\TwinLabRecorder.exe record --name "Order to invoice" --exe CaseDesk.exe --origin https://orders.example.test --tn3270 mainframe.example.test:23 --keep-browser-profile
```

Then:

1. Wait until the window says *The browser is ready* when there is a web target (Edge opens on the first one), or *Recording* otherwise. For a mainframe target it also shows the address the terminal connects to.
2. Run the UiPath process (from Studio or UiPath Assistant). Leave the PC alone while it runs: anything you do in a target application is recorded too.
3. Press **Stop recording** (or Ctrl+C at the command prompt). Finishing takes a few seconds; the browser the recorder started closes.
4. **Recordings on this PC** shows what was recorded per application. **Send references to the portal** sends only that summary; without a portal, nothing leaves the PC.

The recording is in `%LOCALAPPDATA%\TwinLabRecorder\recordings\<session>\` (or `data\recordings\` beside the program with `--portable-data`): `session.jsonl` (the whole timeline), `desktop\`, `web\`, `mainframe\`, and `portal-outbox.json`.

## How UiPath's input reaches the recorder

| Surface | Seen | Not seen, or seen differently |
|---|---|---|
| Desktop | Hardware events: clicks and keys through the mouse and keyboard hooks. Typing by any input method: values the robot sets are recorded as their shape (robot input is on; `--no-robot` for a person) | **Simulate** or **SendWindowMessages** clicks raise no mouse event; a WinForms button pressed that way may raise no UI Automation event either. Use hardware events for clicks in the recording run, or check the steps afterwards |
| Web | Clicks and typing in the recorder's browser, whichever input method, through the page's own events | A browser the recorder did not start or attach to: only "used" |
| Mainframe | Every screen, field sent and key, through the proxy | A terminal connected straight to the host, not through `127.0.0.1:23270` |

UiPath's own programs (Studio, Assistant, the robot) are never recorded. A target application is picked up on its first click or event, even if the robot starts it during the session; it is still best to have it open before the robot starts.

Pictures of desktop windows are off by default (they take much memory on a long run); tick **Pictures of desktop windows** (`--pictures`) for a short run where the look matters.

## A check run (5 minutes, no mainframe needed)

Twin Lab's repository has a synthetic mainframe. Any clone will do: the recorder itself does not need one.

```powershell
python reference\mainframe\demo_host.py serve --port 3270
```

Record with `--tn3270 127.0.0.1:3270`, point the terminal (or `python reference\mainframe\demo_host.py flow --port 23270`) at `127.0.0.1:23270`, stop, and look at **Recordings on this PC**: the mainframe application should show `screen`, `type` and `aid` steps labelled `USERID`, `PASSWORD`, `ACCOUNT NO`, `Enter`, `PF3`.

## When something is missing

**Recordings on this PC** shows each session's notes: why an application was recorded only as used, a browser that was reopened, a port that was taken, an application running as administrator. The window's log for each run is in `%LOCALAPPDATA%\TwinLabRecorder\logs\`.

- **No web steps:** UiPath used a browser the recorder did not start. Use the kept profile with UiPath's extension, or attach by DevTools address.
- **No mainframe steps:** the terminal did not connect to `127.0.0.1:23270`; `mainframe\mainframe.json` says how many connections came and whether the host could be reached.
- **No desktop steps for an application:** it was not a target (check its executable name; the notes say when a target was recorded only as used, for example `javaw.exe` or a program under `C:\Windows`), or it runs as administrator (the notes say so: run the recorder as administrator too).
- **The recorder was closed or crashed:** start it again; it finishes the unfinished session from what it had written (a web recording then has no HAR).
