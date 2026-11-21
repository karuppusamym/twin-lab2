# Recorder: checks for the "go desktop" batch

The window's logic is tested headlessly (`tests/recorder/test_ui_threads.py`, `test_ui_model.py`,
and `test_window_headless.py` on a Tk root that is never shown, with `TWINLAB_TK_HEADLESS=1`).
These checks need the visible window, a person at the PC and real applications. Run them in one
sitting, with nothing else in the foreground.

**Setup.** Build (`python recorder/build.py`) and run `dist\TwinLabRecorder\TwinLabRecorder.exe`.
Start a local portal (see the README) with one web application in the catalog and a pairing code
on its Machines page. Have a target ready: CaseDesk, Calculator, or a web page on the portal's
catalog.

Each check: what to do → what you should see. Mark ✅ or ❌, and note anything odd.

## 1. Pair

1. **Pair with portal…**, enter the portal address, the code and a machine name.
   → The status line shows *Connecting: pairing with http://…* at once; the window can be moved
   and resized while it pairs. Then *Paired as <name> with <portal>*, and within ~20 s the
   **Offered sessions** and **My own session** tabs fill from the portal.
2. Pair again with a wrong code.
   → *Not paired: …* in an error dialog with the portal's reason; the window stays usable;
   **Pair** is enabled again.
3. Pair with `http://example.com` (not HTTPS, not this machine).
   → An error at once: the portal must be reached over HTTPS; no *Connecting* line.

## 2. Offered sessions keep the selection

1. Offer two sessions to the machine on the portal. Select the second one in the list.
   → After the next poll (15-20 s) it is still selected.
2. Rename the selected session on the portal, or change its targets.
   → Within one poll its row shows the new name/targets, in the same place, still selected.
     **Start selected session** records the new targets.
3. Withdraw the selected session on the portal.
   → Its row goes; nothing is selected (Start asks you to choose).

## 3. Record

1. Start a session with a web target and a desktop target (CaseDesk or Calculator).
   → *Checking that each channel can start…*, then the indicator below the status shows
     **● RECORDING 00:00:0x <name>** in red, *Targets: …*, *Channels: starting…*, then
     *Channels: desktop ok · web ok*. The time counts every second.
2. Wait for *The browser is ready*, work in the targets for a minute.
   → The indicator stays visible on every tab; the elapsed time keeps counting.
3. Kill one collector process (Task Manager: the `TwinLabRecorder.exe` child running the desktop
   channel), or block the web target.
   → The indicator turns orange: the channel shows *failed* / *restarting*, then *ok (restarted 1×)*,
     or *(1 gap)*.
4. **Stop recording**.
   → **■ STOPPING** and *Finishing the recording… (n s)*, then *Recording saved on this PC*;
     **Recordings on this PC** opens with the session selected.

## 4. Close during recording

1. Start a recording, then close the window (X).
   → It asks *A recording is running. Stop it and close?* Answer **Yes**.
   → The window stays open and responsive (move it, switch tabs); the indicator shows
     **■ STOPPING** and the seconds waited *of 120 s*; the status says *Stopping: finishing the
     recording before the window closes*. Closing again says *Still finishing…*. When the
     recording is finished the window closes by itself, with no dialog.
2. Start again, then close and answer **No**.
   → Nothing changes; the recording goes on.
3. Make finishing take long (a hung target application, or pause the recording process with
   Resource Monitor → Suspend), then close.
   → After 120 s a dialog explains: *The recording did not finish within 120 seconds…* and that the
     next start finishes it. The window closes. Start the recorder again → the session appears in
     **Recordings on this PC** (recovered).

## 5. Portal offline

1. With the machine paired, stop the portal.
   → Within one poll the status line reads *Paired as <name>: the portal cannot be reached: …*
     (not stuck on the old text; no error dialog; the window stays usable).
2. Select a recording and **Send references to the portal**.
   → *Sending: references of <name>*, then *Retrying: … (attempt 2) — the portal cannot be
     reached: …*, up to attempt 4, then an error dialog *The references were not sent: …*. The
     window responds throughout.
3. Start the portal again.
   → The next poll restores *Paired as <name> with <portal>*; sending now works (*Sent. The portal
     mapped it as …*).

## 6. Send review

1. Select a recording in **Recordings on this PC**.
   → A summary, not JSON: *Session*, *Destination: <portal> (as <machine>)*, *Applications (n)*
     with steps per app, *Evidence sent* (steps by surface, screen structure, timeline, checks,
     completeness), *Withheld (stays on this PC)* (other applications used n times, not named;
     typed values, page contents, HAR, pictures, raw journals), and the notes.
2. **Details (JSON)**.
   → The references JSON appears below the summary; the button reads **Hide details**.

## 7. Upload queue

1. Select a recording, **Upload the recording…**, answer **Yes**.
   → The **Uploads** tab opens; the row goes *sending* with the percentage and *x MB of y MB*
     rising, then *done 100%*.
2. Upload a larger recording and stop the portal half way.
   → The row shows *retrying*, *next try in n s: the portal cannot be reached…*, the percentage
     kept. Start the portal; **Retry selected** → it continues from that percentage (not from 0)
     and ends *done*.
3. Queue an upload with the portal stopped, close the recorder, start the portal, start the
   recorder.
   → Without pressing anything the upload resumes and ends *done*.
4. With the machine unpaired (or the token revoked on the portal), queue an upload.
   → The note under the list says *Not sending: …not paired…*; the job stays *queued*; after
     pairing it goes.

## 8. DPI scaling

Repeat 3.1, 6.1 and 7.1 with Windows display scaling at 100 %, 150 % and 200 % (Settings →
Display → Scale), and with the window on a second monitor at a different scale.
→ Nothing is cut off: the status and indicator lines wrap at the window's width; the Uploads
  columns and buttons fit at the minimum window size (600×460); the text is sharp, not blurred
  (if it is blurred, note it: the program does not declare DPI awareness yet).

## 9. The console

1. Start `TwinLabRecorder.exe` by double click.
   → Only the recorder's window; no console window stays open.
2. `TwinLabRecorder.exe --help` from a command prompt.
   → The commands print; no window opens.

## 10. Desktop capture (recorder contract 1.4, rebuild-proof gaps 7, 8, 16)

Tested only on fixture trees so far (`tests/recorder/test_structure_texts.py`,
`test_desktop_picture_masks.py`). Record CaseDesk with pictures on (`record … --pictures`), then
`show <session>` and open the session folder.

1. **Combo box options (gap 7).** Open the case, open **Status** (`cboStatus`), pick *Resolved*,
   close it.
   → The expand step in `desktop/steps.jsonl` has `items` (the four statuses). In the outbox's
     `screens`, every state of the case window lists `cboStatus` with `options: 4` and
     `option_texts: ["Open", "In progress", "Resolved", "Closed"]`. Repeat with an editable combo
     box or one holding names: only `options` (a count), no texts.
2. **Message boxes (gap 8).** Sign in with an empty password, so the *Sign in* message box
   (`#32770`) opens; press OK.
   → The steps on the message box have `screen: "Sign in"` in the outbox flow (not missing, not
     `(window)`), and the outbox's `screens` has a *Sign in* screen whose state has
     `texts: [{"kind": "message", "text": "Enter your user name and password."}]` and an `OK`
     button. A message box that shows a case number shows it as `99999`.
3. **No typed text in desktop pictures (gap 16, desktop side).** Type a customer name into the
   summary box and, at once, click into the next box and type there.
   → Open `desktop/pictures/*.png`: every text box is painted over in its own background
     (with its shape drawn where it had a value), also one that was empty when its tree was
     read; `desktop/pictures.json` lists nothing dropped for a readable value.
4. **Ids with a digit.** If the app under test has controls such as `txtLine2` or `button1`
   (CaseDesk has none: try Calculator's `num1Button`):
   → their steps keep that id as `label` in the outbox flow and as `id` in `screens`; Win32's
     numeric ids (`2`, `65535`) are still not labels.

## Verify: the portal's desktop replay

Verify's desktop replay (`twinlab2.replay.desktop`) is unit-tested on recorded window trees
(`tests/server/test_verify_surfaces.py`); its live run opens CaseDesk's windows, so it waits for
this batch: step 5 of `tools\rebuild_proof\desktop_batch.ps1 -GoDesktop`
(`desktop_live.py portal-verify`).

1. Run the batch; leave the mouse and keyboard alone until it prints its last line.
   → `portal-desktop-summary.json`: the reference CaseDesk **pass** on both recordings with every
     `opens-*` case PASS (the positive control); each rebuild's result next to Twin Lab's verify
     of step 4 (`desktop-summary.json`), step by step the same failures.
2. Watch while it runs.
   → Only CaseDesk's windows open and close; nothing is typed or clicked into another window
     (UI Automation patterns and window messages only); each CaseDesk closes at the end of its
     replay.
3. A Windows verifier for the portal: set `TWINLAB2_DESKTOP_VERIFY=1` and
   `TWINLAB2_DESKTOP_BUILDS=<folder of built exes, each with .twinlab\version>`, run
   `python -m twinlab2.worker --types verify`, and Verify a desktop release with
   `{"targets": {"desktop": "casedesk/CaseDesk.exe"}}`.
   → The verification runs on that machine, shows each scenario's progress, and ends passed or
     failed with the desktop report; without the two settings it ends with the reason, not run.
