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
