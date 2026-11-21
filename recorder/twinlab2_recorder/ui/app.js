/* The recorder's window: draws twinlab2_recorder.app.RecorderApp's snapshot and calls the bridge
   (twinlab2_recorder.webwindow.Bridge) as window.pywebview.api. No framework: each region is
   redrawn only when its HTML changes, keeping the focus and what is typed in a field. */
"use strict";

/* In WebView2 the bridge is window.pywebview.api; in an Edge app window (no WebView2 on the PC)
   the same calls go to webwindow.PageServer, with the token from the address's #fragment. */
const TOKEN = (location.hash.match(/[#&]t=([\w-]+)/) || [])[1];
const httpApi = new Proxy({}, {get: (_, name) => async (...args) => {
  const r = await fetch(`/api/${name}`, {method: "POST", headers: {"Content-Type": "application/json", "X-TL-Token": TOKEN}, body: JSON.stringify(args)});
  if (!r.ok) throw new Error(`${name}: ${r.status}`);
  return r.json();
}});
const api = () => (window.pywebview && window.pywebview.api) || httpApi;
let snap = null;
let version = -1;
let started = false;
const form = {};          // values typed in fields, by element id (kept across redraws)
const ownChecked = new Set();
let ownFillSeq = 0;
const shown = {};         // the HTML last drawn per region
const autoDismissed = new Set();

const ICON = (name, cls = "i") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const SURFACE = {web: "web", desktop: "desktop", mainframe: "mainframe"};
const surfaceIcon = (s) => ICON(SURFACE[s] ? s : "desktop");
const val = (id, fallback) => (id in form ? form[id] : (fallback == null ? "" : fallback));
const dis = (ok) => (ok ? "" : " disabled");

function patch(id, html) {
  const el = document.getElementById(id);
  if (!el || shown[id] === html) return;
  const active = document.activeElement;
  const keep = active && el.contains(active) && active.id ? {id: active.id, s: active.selectionStart, e: active.selectionEnd} : null;
  el.innerHTML = html;
  shown[id] = html;
  if (keep) {
    const again = document.getElementById(keep.id);
    if (again) {
      again.focus();
      try { if (keep.s != null) again.setSelectionRange(keep.s, keep.e); } catch (e) { /* not a text field */ }
    }
  }
}

/* -- the frame -------------------------------------------------------------------------------- */

function pairingHtml(p) {
  if (!p.paired) return `<button class="pairing off" data-act="nav" data-arg="pair"><span class="dot"></span><span class="txt">Not paired · Pair this PC</span></button>`;
  const host = (p.portal || "").replace(/^https?:\/\//, "");
  const cls = p.error ? "bad" : "";
  const txt = p.error ? `Paired as ${esc(p.machine)} · portal not reachable` : `Paired as ${esc(p.machine)} · ${esc(host)}`;
  return `<button class="pairing ${cls}" data-act="nav" data-arg="settings" title="${esc(p.status)}"><span class="dot"></span><span class="txt">${txt}</span></button>`;
}

function stateHtml(r) {
  if (r.mode === "recording") return `<span class="pill recording"><span class="led"></span>RECORDING <span class="time">${esc(r.elapsed)}</span></span>`;
  if (r.mode === "paused") return `<span class="pill paused"><span class="led"></span>PAUSED <span class="time">${esc(r.elapsed)}</span></span>`;
  if (r.mode === "stopping") return `<span class="pill stopping"><span class="led"></span>FINISHING <span class="time">${esc(r.elapsed)}</span></span>`;
  if (r.mode === "checking") return `<span class="pill checking"><span class="led"></span>CHECKING</span>`;
  return `<span class="pill"><span class="led"></span>Not recording</span>`;
}

function navHtml(s) {
  const unsent = s.recordings.rows.filter((r) => !r.sent).length;
  const busy = s.uploads.rows.filter((r) => ["sending", "queued", "retrying"].includes(r.state)).length;
  const items = [["record", "rec", "Record", s.recording.mode === "paused" ? "‖" : s.recording.mode !== "idle" ? "●" : ""], ["recordings", "list", "Recordings", unsent || ""],
    ["uploads", "up", "Uploads", busy || ""], ["privacy", "shield", "Privacy", ""], ["settings", "gear", "Settings", ""]];
  return items.map(([k, i, t, c]) => `<button data-act="nav" data-arg="${k}"${s.page === k ? ' aria-current="page"' : ""}>${ICON(i)}<span class="label">${t}</span>${c !== "" ? `<span class="count">${esc(c)}<span class="sr-only"> ${k === "record" ? "recording" : "waiting"}</span></span>` : ""}</button>`).join("")
    + `<div class="foot">Version ${esc(s.app.version)}</div>`;
}

function activityHtml(a) {
  if (!a.text) return "";
  const live = ["connecting", "sending", "retrying", "stopping"].includes(a.state);
  return `${live ? '<span class="spin" aria-hidden="true"></span>' : ""}<span>${esc(a.text)}</span>`;
}

/* -- pages ------------------------------------------------------------------------------------ */

function callout(tone, title, text, lines = [], acts = "") {
  const icon = {ok: "check", warn: "warn", bad: "warn", info: "info"}[tone] || "info";
  return `<div class="callout ${tone}" ${tone === "bad" ? 'role="alert"' : ""}>${ICON(icon)}<div><b>${esc(title)}</b>${text ? esc(text) : ""}`
    + (lines.length ? `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : "") + (acts ? `<div class="acts">${acts}</div>` : "") + `</div></div>`;
}

function portalBanner(s) {
  if (!s.pairing.error) return "";
  const host = (s.pairing.portal || "the portal").replace(/^https?:\/\//, "");
  return callout("bad", `We can't reach ${host}`, `Your recordings are safe on this PC, and uploads continue by themselves once the portal is back. It is asked again every 15 to 20 seconds. (${s.pairing.error})`, [],
    `<button class="btn danger small" data-act="poll">Try again now</button>`) + `<div class="mt"></div>`;
}

function excludedCallout(s) {
  const n = s.exclude.rows.length;
  if (!n) return "";
  const names = s.exclude.rows.slice(0, 3).map((r) => r.name || r.pattern).join(", ");
  return `<div class="callout mt">${ICON("shield")}<div><b>${n} application${n === 1 ? " is" : "s are"} never recorded on this PC</b>${esc(names)}${n > 3 ? " and more" : ""}. <button class="link" data-act="nav" data-arg="privacy">Review privacy</button></div></div>`;
}

function pageRecord(s) {
  const r = s.recording;
  if (r.mode !== "idle") return recordingView(s);
  let html = `<div class="page"><h1>Choose what to record</h1><p class="lede">Nothing is recorded until you press <b>Start recording</b>. The session's applications are recorded in full; other applications only as having been used, without their names.</p>`;
  html += portalBanner(s);
  if (s.note) html += callout(s.note.tone, s.note.title, s.note.text, s.note.lines || []) + `<div class="mt"></div>`;
  html += `<div class="tabs" role="tablist" aria-label="What to record">
    <button role="tab" id="tab-offered" aria-selected="${s.record_tab === "offered"}" data-act="tab" data-arg="offered">Offered to me${s.offered.sessions.length ? ` <span class="count">${s.offered.sessions.length}</span>` : ""}</button>
    <button role="tab" id="tab-own" aria-selected="${s.record_tab === "own"}" data-act="tab" data-arg="own">My own session</button></div>`;
  html += `<div role="tabpanel" aria-labelledby="tab-${s.record_tab}">` + (s.record_tab === "own" ? ownTab(s) : offeredTab(s)) + `</div>`;
  return html + excludedCallout(s) + `</div>`;
}

function offeredTab(s) {
  if (!s.pairing.paired) {
    return `<div class="card"><h2>Connect this PC to your portal</h2><p class="muted small">The portal's owner offers sessions to this PC once it is paired. You can still record your own session.</p>
      <div class="row mt"><button class="btn primary big" data-act="nav" data-arg="pair">Pair this PC</button><button class="btn ghost" data-act="tab" data-arg="own">Record my own session</button></div></div>`;
  }
  const o = s.offered;
  let html = `<p class="small muted" id="offered-note">${esc(o.note)}</p>`;
  if (!o.sessions.length) return html + `<div class="list"><div class="empty">No session is offered to this PC right now.</div></div>`;
  html += `<div class="list" role="radiogroup" aria-label="Offered sessions">` + o.sessions.map((x) => `
    <label class="item${o.selected === x.id ? " selected" : ""}"><input type="radio" name="offered" data-act="offered" data-arg="${x.id}"${o.selected === x.id ? " checked" : ""}>
      <span class="body"><span class="title">${esc(x.name)}</span><span class="apps">${x.targets.map((t) => `<span class="tag">${surfaceIcon(t.surface)}<b>${esc(t.name || "?")}</b>${esc(t.surface)}</span>`).join("")}</span></span></label>`).join("") + `</div>`;
  const chosen = o.sessions.find((x) => x.id === o.selected);
  html += `<div class="row mt"><button class="btn primary big" data-act="start-offered"${dis(s.can.start_offered)}>${ICON("rec")}${chosen ? `Start recording “${esc(chosen.name)}”` : "Start recording"}</button>
    <span class="small muted">${chosen ? "Each application is checked first (a few seconds)." : "Choose a session first."}</span></div>`;
  return html;
}

function ownTab(s) {
  const fill = s.own.fill;
  if (fill.seq !== ownFillSeq) { ownFillSeq = fill.seq; if (fill.seq) form["own-extra"] = fill.extra; }
  const apps = s.own.apps;
  let html = `<div class="card stack"><div><label class="f" for="own-name">Session name</label><input type="text" id="own-name" value="${esc(val("own-name", "My session"))}"></div>
    <div><span class="f">Applications from the portal's catalog</span>`;
  html += apps.length ? `<div class="grid2">` + apps.map((a) => `<label class="check"><input type="checkbox" data-act="own-app" data-arg="${a.app_id}"${ownChecked.has(a.app_id) ? " checked" : ""}><span>${surfaceIcon(a.surface)} ${esc(a.name)}</span></label>`).join("") + `</div>`
    : `<p class="small muted" id="own-empty">${esc(s.own.empty)}</p>`;
  html += `</div><div><label class="f" for="own-extra">Other applications, one per line</label><textarea id="own-extra" rows="3" placeholder="CaseDesk.exe&#10;https://billing.example.com&#10;tn3270://mainframe.example.com:23">${esc(val("own-extra", ""))}</textarea>
    <div class="hint">A program (CaseDesk.exe), a web address (https://…), or a mainframe host (tn3270://host:port).</div></div>
    <div class="row"><button class="btn primary big" data-act="start-own"${dis(s.can.start_own)}>${ICON("rec")}Start my session</button><button class="btn ghost" data-act="propose">Fill in from a UiPath project…</button></div></div>`;
  return html;
}

function recordingView(s) {
  const r = s.recording;
  const kicker = {recording: "Recording", paused: "Paused · nothing is being recorded", checking: "Checking", stopping: "Finishing the recording"}[r.mode];
  let sub = r.name ? esc(r.name) : "";
  if (r.mode === "checking") sub = "Checking that each application can be recorded…";
  if (r.mode === "paused") sub = `${esc(r.name)} · resume when you are ready; the time keeps counting`;
  if (r.mode === "stopping") sub = `${esc(r.name)} · finishing… ${r.stop_waited || 0} s${r.stop_limit ? ` of ${r.stop_limit} s before the window closes` : ""}`;
  const icon = r.mode === "recording" ? ICON("rec", "") : r.mode === "paused" ? ICON("pause", "") : r.mode === "stopping" ? ICON("stop", "") : '<span class="spin" style="width:26px;height:26px;border-width:3px" aria-hidden="true"></span>';
  let html = `<div class="page"><div class="hero ${r.mode}"><div class="badge">${icon}</div>
    <div><h1 class="kicker">${kicker}</h1>${r.elapsed ? `<div class="timer" aria-label="Elapsed ${esc(r.elapsed)}">${esc(r.elapsed)}</div>` : ""}<div class="small muted">${sub}</div></div>
    <div class="acts">${r.mode === "recording" ? `<button class="btn secondary" data-act="pause"${dis(s.can.pause)}>${ICON("pause")}Pause</button><button class="btn stop big" data-act="stop"${dis(s.can.stop)}>${ICON("stop")}Stop and save</button>`
      : r.mode === "paused" ? `<button class="btn primary big" data-act="resume"${dis(s.can.resume)}>${ICON("play")}Resume</button><button class="btn ghost" data-act="stop"${dis(s.can.stop)}>${ICON("stop")}Stop and save</button>` : ""}</div></div>`;
  if (r.mode === "paused") {
    html += `<div class="mt"></div>` + callout("warn", "Paused", "Nothing is recorded while paused: no steps, no pictures, no network traffic, not even which applications you use. Press Resume to go on." + (r.pauses > 1 ? ` Paused ${r.pauses} times so far.` : ""));
  }
  if (r.mode === "recording") {
    if (r.health === "trouble") {
      const bad = r.apps.filter((a) => a.tone === "warn" || a.tone === "bad").map((a) => `${a.name}: ${a.status}`);
      html += `<div class="mt"></div>` + callout("warn", "An application's recorder had trouble", "It was restarted where it could be; steps in the gap may be missing. Keep working: nothing else is needed.", bad);
    } else if (r.browser_ready) {
      html += `<div class="mt"></div>` + callout("ok", "The browser is ready", "Work through the process as you normally would. Typed values are painted over in pictures.");
    }
    if (r.lines.length) html += `<div class="mt"></div>` + callout("info", "Before you start", "", r.lines);
  }
  if (r.apps.length) {
    html += `<div class="card mt"><div class="row"><h2 style="margin:0">Live status</h2><span class="spacer"></span><span class="xs muted">Updates every second</span></div>
      <table aria-label="Live status per application"><thead><tr><th>Application</th><th class="hide-narrow">Channel</th><th>Status</th></tr></thead><tbody>`
      + r.apps.map((a) => `<tr><td><span class="surface">${surfaceIcon(a.surface)}</span><b>${esc(a.name)}</b></td><td class="muted hide-narrow">${esc(a.channel)}</td><td><span class="chip ${a.tone}">${esc(a.status)}</span></td></tr>`).join("")
      + `</tbody></table></div>`;
  }
  return html + `<p class="xs muted mt">Other applications you use are noted only as “another application”, never named. Excluded applications leave no trace.</p></div>`;
}

function reviewHtml(rv) {
  if (rv.error != null) return callout("bad", "This recording cannot be reviewed", rv.error);
  const blocks = rv.summary.split("\n\n");
  const head = blocks.shift().split("\n");
  const sections = blocks.map((b) => { const lines = b.split("\n"); return {title: lines[0].replace(/:$/, ""), items: lines.slice(1).map((l) => l.replace(/^\s*•\s*/, "").trim()).filter(Boolean)}; });
  const find = (p) => sections.find((x) => x.title.startsWith(p));
  const list = (sec, icon, cls) => sec ? `<ul class="ticks">${sec.items.map((i) => `<li>${ICON(icon, cls)}<span>${esc(i)}</span></li>`).join("")}</ul>` : "";
  const apps = find("Applications");
  let html = `<p class="small muted">${head.map(esc).join("<br>")}</p>`;
  if (apps) html += `<h3 class="small mt">${esc(apps.title)}</h3>${list(apps, "check", "yes")}`;
  html += `<div class="grid2 mt"><div><h3 class="small">Sent to the portal</h3>${list(find("Evidence"), "check", "yes")}</div><div><h3 class="small">Stays on this PC</h3>${list(find("Withheld"), "x", "no")}</div></div>`;
  const notes = find("Notes");
  if (notes) html += `<h3 class="small mt">Notes</h3>${list(notes, "info", "muted")}`;
  if (rv.json) html += `<h3 class="small mt">What is sent (JSON)</h3><pre class="json" tabindex="0">${esc(rv.json)}</pre>`;
  return html;
}

function pageRecordings(s) {
  const d = s.recordings;
  let html = `<div class="page"><h1>Recordings on this PC</h1><p class="lede">Review what a recording gives the portal before you send it. The recording itself stays here unless you upload it.</p>`;
  html += portalBanner(s);
  if (s.note && s.note.tone !== "warn") html += callout(s.note.tone, s.note.title, s.note.text) + `<div class="mt"></div>`;
  if (!d.rows.length) return html + `<div class="list"><div class="empty" id="done-note">${esc(d.note)}</div></div></div>`;
  html += `<p class="small muted" id="done-note">${esc(d.note)}</p><div class="split"><div class="list" role="radiogroup" aria-label="Recordings">` + d.rows.map((r) => `
    <label class="item${d.selected === r.id ? " selected" : ""}" style="padding:var(--space-3) var(--space-4)"><input type="radio" name="rec" data-act="recording" data-arg="${esc(r.id)}"${d.selected === r.id ? " checked" : ""}>
      <span class="body"><span class="title">${esc(r.name)}</span><span class="row" style="gap:.35rem;margin-top:.2rem"><span class="chip ${r.tone}">${esc(r.state)}</span><span class="chip ${r.sent ? "off" : "warn"}">${r.sent ? "Sent" : "Not sent"}</span></span></span></label>`).join("") + `</div>`;
  const sel = d.rows.find((r) => r.id === d.selected);
  if (!sel || !d.review) {
    html += `<div class="card muted">Choose a recording to see what sending it would give the portal.</div>`;
  } else {
    html += `<div class="card"><div class="row"><h2 style="margin:0">${esc(sel.name)}</h2><span class="spacer"></span><button class="btn ghost small" data-act="details"${dis(s.can.details)}>${d.details ? "Hide what is sent" : "Show what is sent (JSON)"}</button></div>
      ${reviewHtml(d.review)}
      <div class="row mt" style="border-top:1px solid var(--color-border-soft);padding-top:var(--space-4)">
        <button class="btn primary big" data-act="send"${dis(s.can.send)}>${d.busy ? '<span class="spin" aria-hidden="true"></span>Sending…' : "Send summary to the portal"}</button>
        <button class="btn ghost" data-act="upload"${dis(s.can.upload)}>${ICON("up")}Upload the full recording…</button></div>
      ${s.pairing.paired ? "" : `<p class="xs muted mt2">Pair this PC with the portal to send or upload. <button class="link" data-act="nav" data-arg="pair">Pair now</button></p>`}</div>`;
  }
  return html + `</div></div>`;
}

function pageUploads(s) {
  const u = s.uploads;
  let html = `<div class="page"><h1>Uploads</h1><p class="lede">Uploads continue in the background and resume where they stopped, even after the recorder or the PC restarts.</p>`;
  if (u.error) html += callout("warn", "Not sending right now", u.error) + `<div class="mt"></div>`;
  if (!u.rows.length) return html + `<div class="card muted">Nothing is uploaded yet. Choose a recording under Recordings and press <b>Upload the full recording</b>.</div></div>`;
  const chip = {sending: ["Sending", "active"], queued: ["Queued", "off"], retrying: ["Retrying", "warn"], done: ["Done", "ok"], failed: ["Failed", "bad"]};
  html += `<div class="card stack">` + u.rows.map((r) => {
    const [label, tone] = chip[r.state] || [r.state, "off"];
    return `<div><div class="row"><b>${esc(r.name)}</b><span class="chip ${tone}">${label}</span><span class="spacer"></span><span class="small muted">${r.percent}%</span>${r.can_retry ? `<button class="btn ghost small" data-act="retry" data-arg="${esc(r.id)}">Retry now</button>` : ""}</div>
      <div class="bar ${tone === "active" ? "" : tone}" role="progressbar" aria-label="${esc(r.name)} upload" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${r.percent}"><i style="width:${r.percent}%"></i></div>
      <div class="xs muted mt2">${esc(r.detail)}</div></div>`;
  }).join("") + `</div><div class="row mt"><button class="btn secondary" data-act="retry-all">Retry all now</button><span class="small muted" id="upload-note">${esc(u.note)}</span></div></div>`;
  return html;
}

function pagePrivacy(s) {
  const x = s.exclude;
  let html = `<div class="page"><h1>Privacy on this PC</h1><p class="lede">What the recorder collects while a session runs, and what it never touches here.</p>
    <div class="grid2" style="align-items:start"><div class="card"><h2>Collected, only from the session's applications</h2><ul class="ticks">
      <li>${ICON("check", "yes")}<span>The steps you take: clicks, keys pressed, screens opened</span></li>
      <li>${ICON("check", "yes")}<span>Screen structure: names of fields and buttons, not their values</span></li>
      <li>${ICON("check", "yes")}<span>Pictures of windows, with typed values painted over (when switched on)</span></li>
      <li>${ICON("check", "yes")}<span>Web traffic of the session's own browser, kept on this PC</span></li>
      <li>${ICON("x", "no")}<span>Other applications: only that one was used, never its name</span></li></ul>
      <p class="xs muted">You review the summary before anything is sent. The full recording is uploaded only when you choose to.</p></div>
    <div class="card"><h2>Never recorded here</h2><p class="small muted" id="ex-note">${esc(x.note)}</p>
      <p class="xs muted">Nothing is recorded from these: no steps, no pictures, no network traffic, not even that they were used.</p>`;
  html += x.rows.length ? `<table aria-label="Excluded applications"><thead><tr><th>Matches</th><th class="hide-narrow">Kind</th><th>From</th><th><span class="sr-only">Remove</span></th></tr></thead><tbody>` + x.rows.map((r) => `
      <tr><td><b>${esc(r.name || r.pattern)}</b>${r.name ? `<div class="xs muted">${esc(r.pattern)}</div>` : ""}</td><td class="muted hide-narrow">${esc(r.kind)}</td>
      <td><span class="chip ${r.source.startsWith("Office") ? "active" : r.source === "Default" ? "off" : "ok"}">${esc(r.source)}</span></td>
      <td style="text-align:right">${r.locked ? `<span class="xs muted" title="Set by your office">Locked</span>` : `<button class="btn ghost small" data-act="ex-remove" data-arg="${r.index}" aria-label="Remove ${esc(r.name || r.pattern)}"${dis(x.can_edit)}>Remove</button>`}</td></tr>`).join("") + `</tbody></table>` : "";
  html += `<div class="mt"><label class="f" for="ex-pattern">Exclude another</label><div class="row" style="flex-wrap:nowrap">
      <select id="ex-kind" aria-label="Kind" data-act="ex-kind" style="width:auto"${dis(x.can_edit)}>${x.kinds.map((k) => `<option${k === x.kind ? " selected" : ""}>${esc(k)}</option>`).join("")}</select>
      <input type="text" id="ex-pattern" value="${esc(val("ex-pattern", ""))}"${dis(x.can_edit)}><button class="btn secondary" data-act="ex-add"${dis(x.can_edit)}>Add</button></div>
      <div class="hint" id="ex-hint">${esc(x.hint)}</div></div>
      <div class="row mt"><button class="btn ghost" data-act="ex-pick"${dis(x.can_edit)}>Pick from running apps…</button><button class="btn ghost" data-act="ex-defaults"${dis(x.can_edit)}>Restore defaults</button></div></div></div></div>`;
  return html;
}

function pageSettings(s) {
  const st = s.settings;
  const p = s.pairing;
  return `<div class="page"><h1>Settings</h1><p class="lede">These settings apply to the next session you start.</p><div class="stack">
    <div class="card"><h2>Portal</h2><p class="small">${esc(p.status)}</p><button class="btn secondary" data-act="nav" data-arg="pair"${dis(s.can.pair)}>${p.paired ? "Pair again…" : "Pair this PC…"}</button></div>
    <div class="card stack"><h2>Web applications</h2>
      <div><label class="f" for="set-browser">Browser the session opens</label><select id="set-browser" data-act="setting" data-arg="browser" style="width:auto">
        ${["edge", "chrome"].map((b) => `<option value="${b}"${st.browser === b ? " selected" : ""}>${b === "edge" ? "Microsoft Edge" : "Google Chrome"}</option>`).join("")}</select></div>
      <div class="row"><label class="check"><input type="checkbox" id="set-keep" data-act="setting" data-arg="keep_browser_profile"${st.keep_browser_profile ? " checked" : ""}><span>Keep the browser's profile between sessions (needed for UiPath's browser extension)</span></label>
        <button class="btn ghost small" data-act="profile">Open the kept profile…</button></div>
      <div><label class="f" for="set-cdp">Or record a browser already running on this PC, at its DevTools address</label><input type="text" id="set-cdp" data-act="setting" data-arg="cdp" value="${esc(val("set-cdp", st.cdp))}" placeholder="http://127.0.0.1:9222">
        <div class="hint">For example a robot's browser. Leave it empty and the recorder opens a browser of its own.</div></div></div>
    <div class="card"><h2>Network and updates</h2><p class="small muted" id="network-note">${esc(s.updates.network)}</p><p class="small" id="update-note">${esc(s.updates.note)}</p>
      <div class="row"><button class="btn secondary" data-act="check-updates"${dis(s.can.check_updates)}>Check for updates</button>${s.updates.offer ? `<button class="btn primary" data-act="install-update"${dis(s.can.install_update)}>Install TwinLab Recorder ${esc(s.updates.offer)}…</button>` : ""}</div></div>
    <div class="card"><h2>Desktop applications</h2><label class="check"><input type="checkbox" id="set-pictures" data-act="setting" data-arg="pictures"${st.pictures ? " checked" : ""}><span>Take pictures of desktop windows (typed values are painted over; uses more memory in long sessions)</span></label></div>
    <p class="xs muted">TwinLab Recorder ${esc(s.app.version)}</p></div></div>`;
}

function pagePair(s) {
  const p = s.pairing;
  return `<div class="page" style="max-width:30rem;margin:var(--space-4) auto 0"><h1>${p.paired ? "Pair this PC again" : "Connect this PC to your portal"}</h1>
    <p class="lede">Ask the portal's owner for a pairing code (on the portal: Machines → Pair a PC). Nothing is recorded until you start a session.</p>
    <form class="card stack" id="pair-form" data-act="pair" novalidate>
      <div><label class="f" for="pair-url">Portal address</label><input type="text" id="pair-url" autocomplete="url" value="${esc(val("pair-url", ""))}"></div>
      <div><label class="f" for="pair-code">Pairing code</label><input type="text" id="pair-code" class="code" autocomplete="off" value="${esc(val("pair-code", ""))}"></div>
      <div><label class="f" for="pair-name">Name for this PC</label><input type="text" id="pair-name" value="${esc(val("pair-name", ""))}"><div class="hint">Shown to the portal's owner, who offers sessions to this PC.</div></div>
      <button class="btn primary big" type="submit" style="width:100%;justify-content:center"${dis(s.can.pair)}>${p.busy ? '<span class="spin" aria-hidden="true"></span>Pairing…' : "Pair this PC"}</button>
    </form><p class="xs muted" style="text-align:center">${ICON("shield")} Only a portal reached over HTTPS, or one on this PC, can be paired.</p></div>`;
}

const PAGES = {record: pageRecord, recordings: pageRecordings, uploads: pageUploads, privacy: pagePrivacy, settings: pageSettings, pair: pagePair};

/* -- dialogs and toasts ------------------------------------------------------------------------ */

function dialogHtml(d) {
  if (!d) return "";
  const icon = {warn: "warn", bad: "warn", info: "info"}[d.tone] || "info";
  return `<div class="scrim"><div class="dlg ${esc(d.tone)}" role="dialog" aria-modal="true" aria-labelledby="dlg-title" aria-describedby="dlg-text" data-id="${d.id}">
    <h2 id="dlg-title">${ICON(icon)}${esc(d.title)}</h2><p id="dlg-text" class="small">${esc(d.text)}</p>
    ${d.lines && d.lines.length ? `<ul>${d.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
    ${d.items ? `<div class="picks" role="group" aria-label="Programs">${d.items.map((it, i) => `<label class="check"><input type="checkbox" class="pick" id="pick-${i}" value="${esc(it)}"><span>${esc(it)}</span></label>`).join("")}</div>` : ""}
    <div class="acts"><button class="btn ghost" data-act="answer" data-arg="no" id="dlg-no">${esc(d.no)}</button><button class="btn ${d.tone === "warn" ? "primary" : "primary"}" data-act="answer" data-arg="yes" id="dlg-yes">${esc(d.yes)}</button></div></div></div>`;
}

function toastsHtml(list) {
  return list.map((n) => `<div class="toast ${esc(n.tone)}" role="${n.tone === "bad" ? "alert" : "status"}"><div><b>${esc(n.title)}</b><div class="muted">${esc(n.text)}</div></div>
    <button class="x" data-act="dismiss" data-arg="${n.id}" aria-label="Dismiss">×</button></div>`).join("");
}

/* -- drawing ----------------------------------------------------------------------------------- */

let lastDialog = null;
let lastPage = null;

function render() {
  const s = snap;
  patch("pairing", pairingHtml(s.pairing));
  patch("state", stateHtml(s.recording));
  patch("nav", navHtml(s));
  patch("activity", activityHtml(s.activity));
  document.getElementById("activity").className = "activity " + (s.activity.state || "");
  if (s.page === "pair" && lastPage !== "pair") prefillPair();
  patch("main", (PAGES[s.page] || pageRecord)(s));
  if (s.page !== lastPage) { lastPage = s.page; document.getElementById("main").scrollTop = 0; }
  patch("dialog", dialogHtml(s.dialog));
  const did = s.dialog ? s.dialog.id : null;
  if (did !== lastDialog) { lastDialog = did; const b = document.getElementById("dlg-yes"); if (b) b.focus(); }
  patch("toasts", toastsHtml(s.notices));
  for (const n of s.notices) {
    if ((n.tone === "ok" || n.tone === "info") && !autoDismissed.has(n.id)) {
      autoDismissed.add(n.id);
      setTimeout(() => api().dismiss(n.id), 8000);
    }
  }
}

async function prefillPair() {
  if ("pair-url" in form) return;
  const d = await api().pair_defaults();
  form["pair-url"] = d.portal; form["pair-name"] = d.name; form["pair-code"] = form["pair-code"] || "";
  shown.main = null;
  if (snap) render();
}

async function refresh() {
  try {
    const s = await api().snapshot(version);
    if (s) { snap = s; version = s.version; render(); }
  } catch (e) {
    console.error(e);
  }
}

async function loop() {
  await refresh();
  setTimeout(loop, 250);
}

async function call(name, ...args) {
  try {
    return await api()[name](...args);
  } finally {
    refresh();
  }
}

/* -- events ------------------------------------------------------------------------------------ */

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.id && (t.tagName === "INPUT" && t.type === "text" || t.tagName === "TEXTAREA")) form[t.id] = t.value;
});

document.addEventListener("change", (e) => {
  const t = e.target;
  const act = t.dataset.act;
  if (act === "offered") call("select_offered", Number(t.dataset.arg));
  else if (act === "recording") call("select_recording", t.dataset.arg);
  else if (act === "own-app") { const id = Number(t.dataset.arg); if (t.checked) ownChecked.add(id); else ownChecked.delete(id); }
  else if (act === "ex-kind") call("exclude_kind", t.value);
  else if (act === "setting") {
    const v = t.type === "checkbox" ? t.checked : t.value;
    if (t.id === "set-cdp") delete form["set-cdp"];
    call("settings", {[t.dataset.arg]: v});
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "ex-pattern") { e.preventDefault(); addExclude(); }
  if (e.key === "Escape" && snap && snap.dialog) answer(false);
});

document.addEventListener("submit", (e) => {
  if (e.target.id !== "pair-form") return;
  e.preventDefault();
  call("pair", val("pair-url"), val("pair-code"), val("pair-name")).then((ok) => { if (ok) form["pair-code"] = ""; });
});

function addExclude() {
  call("exclude_add", val("ex-pattern"), document.getElementById("ex-kind").value).then((ok) => { if (ok) { form["ex-pattern"] = ""; shown.main = null; render(); } });
}

function answer(yes) {
  const d = snap.dialog;
  const chosen = [...document.querySelectorAll("#dialog .pick:checked")].map((c) => c.value);
  call("answer", d.id, yes, chosen);
}

const ACTIONS = {
  nav: (a) => call("nav", a),
  tab: (a) => call("record_tab", a),
  poll: () => call("poll"),
  "start-offered": () => call("start_offered"),
  "start-own": () => call("start_own", val("own-name", "My session"), [...ownChecked], val("own-extra", "")),
  propose: () => call("propose"),
  stop: () => call("stop"),
  pause: () => call("pause"),
  resume: () => call("resume"),
  details: () => call("details"),
  send: () => call("send"),
  upload: () => call("upload"),
  retry: (a) => call("retry", a),
  "retry-all": () => call("retry_all"),
  profile: () => call("open_profile"),
  "check-updates": () => call("check_updates"),
  "install-update": () => call("install_update"),
  "ex-add": () => addExclude(),
  "ex-remove": (a) => call("exclude_remove", [Number(a)]),
  "ex-pick": () => call("exclude_pick"),
  "ex-defaults": () => call("exclude_defaults"),
  answer: (a) => answer(a === "yes"),
  dismiss: (a) => call("dismiss", Number(a)),
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("button[data-act]");
  if (!el || el.disabled || el.closest("form")) return;  // a form's own button submits it
  const fn = ACTIONS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el.dataset.arg); }
});

function begin() {
  if (started || !((window.pywebview && window.pywebview.api) || (TOKEN && location.protocol === "http:"))) return;
  started = true;
  loop();
}
window.addEventListener("pywebviewready", begin);
begin();
