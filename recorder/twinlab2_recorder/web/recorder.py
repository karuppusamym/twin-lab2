# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Record a person (or a script) clicking through a web application, in the recording format.

A recording is a folder holding:

- `recording.har`: HAR 1.2 with content embedded, written by the browser;
- `dom/<seq>.html`: the page after a step, with the documents of its frames (`<tl-frame>`), its
  shadow roots open and closed (`<tl-shadow>`), and the OCR text of its canvases
  (`<tl-canvas-text>`) written in (password field values blanked);
- `dom/<seq>.before.html`: the page as the action met it (for an action inside a frame, that
  frame's document: `before_scope: "frame"`);
- `screens/<seq>.png`: what the window showed after the step; `screens/<seq>.cN.png`: each canvas;
- `docs/`: PDFs the pages showed, with their text and `documents.json` (form field names only);
- `steps.jsonl`: one JSON object per step: `{"seq", "ts", "url", "action":
  "goto|click|dblclick|type|select|submit|keypress|check|uncheck", "selector", "dom",
  "dom_before", "screenshot"}`, plus `frame` for an action inside a frame (its selector is then
  `iframe#x >>> button#y`, see twinlab2_contract.evidence.locate), `locators` (ranked target candidates with the
  number of elements each matched), `key`/`modifiers` on a keypress, `nav` on a goto, `blank` on
  a type step whose value was empty or spaces, `left` on a type step whose field no longer
  had the focus when its after-snapshot was taken (the replay leaves the field too, so what a
  page does on blur is there), and `caused_by` on a submit or navigation that a
  click or key press caused (see twinlab2_contract.evidence.recording);
- every step also says where it happened: `tab` (t1, t2, ... in the order the tabs opened),
  `opener` (the tab that opened it, for a popup or a link with target=_blank) and `origin` (of
  the top-level page). A recording that used more than one tab has steps of its own for that:
  `tab-open`, `tab-switch` (the next action happened in another tab) and `tab-close`; all steps
  of all tabs are on one sequence, ordered by when they happened. `tabs.json` lists the tabs
  with their HAR page id (`pageref` of the HAR entries each tab made);
- `storage` on a step after which the page's browser storage changed: `localStorage` and
  `sessionStorage` as {key: masked shape} (every stored value parsed and each leaf replaced by a
  placeholder of its type; a credential-like key keeps no shape), `indexedDB` as database and
  store names, `cookies` as names only (and `cookies_by_domain`: the names of every cookie the
  browser holds, per domain), and `origin`. No stored value or cookie value is ever written;
- `mask_boxes` on a step whose picture had something painted over: every text field and
  editable region that shows text (whether or not its value is known yet: a field still being
  typed into is marked at its first input event and painted over as the picture is taken), and
  text that shows a typed value; each rectangle as {x, y, width, height} in CSS pixels of the
  viewport;
- `recording.json`: the browser and its version, the viewport (1280x720 for `record()`, the
  robot's own for `attach()`) and the device scale factor.

Typing is one `type` step per field, written before the key press, click, submit, navigation or
blur that follows it, so "type, then Enter" is always two steps in that order. Only named keys
(Enter, Escape, Tab, arrows...) and chords (Ctrl+S) are key presses: the characters of typed text
never are. Two clicks and the double click they make are one `dblclick` step; a checkbox or radio
button is `check`/`uncheck`.

`record()` opens its own browser. `attach()` records a browser that is already running, such as
the one a robot drives, over the Chrome DevTools protocol. Both follow every tab and popup of the
browser context; `attach()` cannot see what a tab did before it attached.

Capture is deterministic: each action is sent to the recorder the moment it happens, while its
document is alive, and numbered on arrival: one sequence for all tabs, never re-sorted by
timestamps. Nothing depends on a call made while a page unloads (newer Chromium drops those):
a batch the page leaves before it settles simply has no after-snapshot. A tab's `tab-open` step is
written with the tab's first action; a click is sent at once and a double click replaces the
clicks it was made of; the blank page a browser starts on is a `setup` step the recorder writes
itself. A click, key press or submit that loads another page is observed at that page
(`after: navigation`, no snapshot of its own), whatever the page's speed. Steps may also carry
`shadow` (open/closed root), `surface: canvas`, `option_index` (select), `dialog` ({kind,
choice, message}; the message is interface text with data masked, `dialog_message`; a prompt's
text is a typed value), `files` on an `upload` step (name shape, type, size
class: never the file), `download` ({name_shape, ext}) and `service_worker`.

The DOM is captured inside the page, after each navigation (full load or history change) and after
each click, change or submit, once the page has been quiet for `settle_ms` with no fetch or XHR
in flight (up to 15 s; `recording.json` says so under `settle`). A recording script's next action
does not wait for that: just before it, the page writes and snapshots what it has not yet
(`__tlBeforeAction`) and the step's picture is taken, so it shows the page before the action's
events; a picture still taken later says `picture_settled: false`. `attach()` cannot hold a
robot's next action: it takes each picture as soon as the step's snapshot arrives, and drops a
picture taken after the next action began (`picture_dropped: true`, `picture_note` says
why). Values typed are never
written: the recorder holds them in memory only, to take them out of snapshots, the HAR, pictures
(masked) and locator texts where they are a value, never inside an id, class or name (the rule:
twinlab2_contract.evidence.typedvalues; an id that is a whole typed value becomes a placeholder, `tl-typed-a`, and
the step says so in `typed_placeholders`), and to tell which record (list item, table row) a
later action is on:
a step on a record says "the record titled with what was typed at step N", and the replay takes
that title from the bindings (twinlab2.replay.bindings). The raw recording is evidence: it holds
whatever the application showed, so it belongs in `evidence/`, which Git ignores.

Playwright is needed (`pip install playwright` and `python -m playwright install chromium`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from twinlab2_contract.evidence.typedvalues import TypedValues

LISTENER_JS = r"""
(() => {
  // Once per document. A popup's first page (about:blank) and the page it then loads can share
  // one window object: what an earlier install hung on the window stands aside (live()).
  if (window.__tlInstalledFor === document) return;
  window.__tlInstalledFor = document;
  window.__tlInstalled = true;
  const DOC = document;
  const live = () => document === DOC;
  // Runs in every frame. The top page keeps the steps and takes the snapshots; a frame reports
  // its actions and changes to its parent with postMessage (which works across origins), each
  // parent adds the frame's selector, and the top page gets "iframe#x >>> button#save".
  const TOKEN = "__TOKEN__", TOP = window.top === window;
  const SETTLE = __SETTLE__, MAX_WAIT = 3000, JOIN = 150, NAV_JOIN = 1500, HOLD = 500;
  const SERIALIZE = __SERIALIZE__;
  // Installed after the page's own code ran (attaching to an open page): whether the page
  // listens for double clicks cannot be known, so clicks wait to see whether one follows.
  const LATE = document.readyState !== "loading";
  const FRAME_ID = Math.random().toString(36).slice(2, 10);
  let pending = [], timer = null, first = 0, lastAt = 0, tidSeq = 0;

  // Does the page listen for double clicks? Then a click waits a moment: two clicks and the
  // double click they make are one `dblclick` step, not three steps.
  let dblSeen = false;
  try {
    const add = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, fn, opts) {
      if (type === "dblclick") dblSeen = true;
      return add.call(this, type, fn, opts);
    };
  } catch (e) {}

  function selectorOf(el) {
    if (!el || el.nodeType !== 1) return null;
    const tag = el.tagName.toLowerCase();
    if (el.id) return tag + "#" + CSS.escape(el.id);
    const name = el.getAttribute("name");
    if (name) return tag + '[name="' + name + '"]';
    const classes = Array.from(el.classList).slice(0, 2).map((c) => "." + CSS.escape(c)).join("");
    return tag + classes;
  }
  function target(el) {
    const hit = el && el.closest && el.closest(
      "button, a, input, select, textarea, label, summary, [role=button], [role=link], [role=tab], [onclick]");
    return hit || el;
  }
  // Events from inside a shadow root reach the document retargeted to the host: the first
  // element of the composed path is the one actually used.
  function realTarget(e) {
    const path = e.composedPath ? e.composedPath() : [];
    return path.length && path[0].nodeType === 1 ? path[0] : e.target;
  }
  function snapshot() { return SERIALIZE({values: false}); }
  function up(msg) {
    try { window.parent.postMessage(Object.assign({__tl: TOKEN}, msg), "*"); } catch (e) {}
  }
  function frameOf(win) {
    for (const f of document.querySelectorAll("iframe, frame")) {
      try { if (f.contentWindow === win) return selectorOf(f); } catch (e) {}
    }
    return "iframe";
  }
  // A typed value goes to the recorder process only (each frame calls the binding itself, never
  // through postMessage, which the page's own scripts could read). It is kept in memory to take
  // typed values out of snapshots, the HAR and pictures, and is never written.
  function secret(tid, value) {
    try { if (window.__tlSecret) window.__tlSecret({tid, value}); } catch (e) {}
  }

  // --- which element: ranked locator candidates, each with how many elements it matched ------
  const STATE = /^(active|selected|completed|done|editing|checked|disabled|hover|focus|focused|open|opened|closed|show|shown|hidden|current|visible|expanded|collapsed|on|off|(is|has|ng|router-link|ui-state)-.*|even|odd|.*[-_](even|odd))$/i;
  const ROW = "li, tr, [role=row], [role=listitem], [role=option], [role=treeitem]";
  const TESTID = ["data-testid", "data-test-id", "data-test", "data-cy", "data-qa"];
  const ROLE_SEL = {
    link: "a[href], [role=link]", button: "button, input[type=button], input[type=submit], input[type=reset], [role=button]",
    checkbox: "input[type=checkbox], [role=checkbox]", radio: "input[type=radio], [role=radio]",
    textbox: "input, textarea, [role=textbox]", searchbox: "input[type=search], [role=searchbox]",
    combobox: "select, [role=combobox]", listbox: "select, [role=listbox]", tab: "[role=tab]",
    menuitem: "[role=menuitem]", option: "[role=option]", switch: "[role=switch]",
  };
  const NAMED_BY_TEXT = new Set(["link", "button", "tab", "menuitem", "option", "checkbox", "radio", "switch"]);
  const norm = (t) => (t || "").replace(/\s+/g, " ").trim();
  const dataLike = (t) => (String(t).match(/\d/g) || []).length >= 3;  // a record number
  const hasDigit = (t) => /\d/.test(t);
  const q = (v) => JSON.stringify(String(v));
  function seg(el) {
    const cls = Array.from(el.classList).filter((c) => !STATE.test(c) && !dataLike(c)).slice(0, 2);
    return el.localName + cls.map((c) => "." + CSS.escape(c)).join("");
  }
  function shown(el) {
    if (!el.getClientRects().length) return false;
    return getComputedStyle(el).visibility !== "hidden";
  }
  function roleOf(el) {
    const r = el.getAttribute("role");
    if (r) return r.trim().split(/\s+/)[0];
    const tag = el.localName, type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : null;
    if (tag === "button") return "button";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag !== "input") return null;
    if (["button", "submit", "reset"].includes(type)) return "button";
    if (type === "checkbox" || type === "radio") return type;
    if (type === "search") return "searchbox";
    if (["text", "email", "tel", "url"].includes(type)) return "textbox";
    return null;
  }
  function labelOf(el) {
    const al = norm(el.getAttribute("aria-label"));
    if (al) return al;
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = norm(lb.split(/\s+/).map((id) => {
        const n = document.getElementById(id); return n ? n.textContent : "";
      }).join(" "));
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = norm(Array.from(el.labels).map((l) => l.textContent).join(" "));
      if (t) return t;
    }
    return "";
  }
  function nameOf(el, role) {
    const l = labelOf(el);
    if (l) return l;
    if (el.localName === "input" && role === "button") return norm(el.value);
    if (NAMED_BY_TEXT.has(role) && el.localName !== "input") {
      const t = norm(el.textContent);
      if (t) return t;
    }
    return norm(el.getAttribute("title"));
  }
  function count(sel, pred, visibleOnly) {
    let n = 0;
    for (const x of document.querySelectorAll(sel)) if ((!visibleOnly || shown(x)) && pred(x)) n++;
    return n;
  }
  function anchorOf(el) {  // the nearest ancestor with an id or test id of its own
    for (let n = el.parentElement; n; n = n.parentElement) {
      if (n === document.body || n === document.documentElement) break;
      if (n.id && !dataLike(n.id) && document.querySelectorAll("#" + CSS.escape(n.id)).length === 1) {
        return [n, "#" + CSS.escape(n.id)];
      }
      for (const a of TESTID) {
        const v = n.getAttribute(a);
        if (v && !dataLike(v)) {
          const s = "[" + a + "=" + q(v) + "]";
          if (document.querySelectorAll(s).length === 1) return [n, s];
        }
      }
    }
    return [document.body, "body"];
  }
  function pathFrom(stop, el) {
    const parts = [];
    for (let n = el; n && n !== stop && n.nodeType === 1; n = n.parentElement) parts.unshift(seg(n));
    return parts.join(" > ");
  }
  function rowOf(el) {  // the record (list item, table row) a control belongs to
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      if (n.matches(ROW)) return n;
      const p = n.parentElement;
      if (n !== el && p && p !== document.body) {
        const s = seg(n);
        if (s.includes(".") && Array.from(p.children).some((c) => c !== n && seg(c) === s)) return n;
      }
    }
    return null;
  }
  // A record's title: its text without the text of its controls (the same on replay).
  const rowTitle = __ROW_TITLE__;
  // Inside an open shadow root: the same kinds of candidate, counted across every open root
  // (Playwright's role, placeholder, text and CSS locators pierce open roots), and a structure
  // from the host: `x-card#card input.title`. A closed root has none: no locator reaches it.
  function deepAll(sel) {
    const out = [];
    const visit = (root) => {
      try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
      for (const n of root.querySelectorAll("*")) if (n.shadowRoot) visit(n.shadowRoot);
    };
    visit(document);
    return out;
  }
  function shadowLocators(el, root) {
    const list = [];
    const deepCount = (sel, pred) => deepAll(sel).filter(pred).length;
    for (const a of TESTID) {
      const v = el.getAttribute(a);
      if (v && !dataLike(v)) { list.push({kind: "testid", attr: a, value: v, count: deepAll("[" + a + "=" + q(v) + "]").length}); break; }
    }
    if (el.id && !dataLike(el.id)) list.push({kind: "id", value: el.id, count: deepAll("#" + CSS.escape(el.id)).length});
    const role = roleOf(el);
    const name = role ? nameOf(el, role) : "";
    if (role && name && !hasDigit(name) && name.length <= 80 && ROLE_SEL[role]) {
      list.push({kind: "role", role, name, count: deepCount(ROLE_SEL[role], (x) => roleOf(x) === role && nameOf(x, role) === name)});
    }
    const ph = /^(input|textarea)$/.test(el.localName) ? norm(el.getAttribute("placeholder")) : "";
    if (ph && !hasDigit(ph) && ph.length <= 80) {
      list.push({kind: "placeholder", text: ph, count: deepCount("[placeholder]", (x) => norm(x.getAttribute("placeholder")) === ph)});
    }
    if (/^(a|button|summary)$/.test(el.localName)) {
      const t = norm(el.textContent);
      if (t && !hasDigit(t) && t.length <= 80) list.push({kind: "text", tag: el.localName, text: t, count: deepCount(el.localName, (x) => norm(x.textContent) === t)});
    }
    const host = root.host;
    const hostSel = host.localName + (host.id ? "#" + CSS.escape(host.id) : "");
    const inner = pathFrom(null, el);
    let matches = [];
    for (const h of deepAll(hostSel)) {
      if (!h.shadowRoot) continue;
      try { matches = matches.concat(Array.from(h.shadowRoot.querySelectorAll(inner))); } catch (e) {}
    }
    const index = matches.indexOf(el);
    if (index >= 0) list.push({kind: "structure", css: hostSel + " " + inner, index, count: matches.length});
    return {list};
  }
  function locators(el) {
    const root = el && el.getRootNode ? el.getRootNode() : null;
    if (el && el.nodeType === 1 && root && root !== document && root.host && root.mode === "open") {
      return shadowLocators(el, root);
    }
    if (!el || el.nodeType !== 1 || el.getRootNode() !== document) return {list: []};
    if (el === document.body || el === document.documentElement) return {list: []};
    const list = [];
    const row = rowOf(el);
    let strong = false;  // one candidate names this element alone, also among hidden ones
    const add = (c, all) => { list.push(c); if (c.count === 1 && all === 1) strong = true; };
    for (const a of TESTID) {
      const v = el.getAttribute(a);
      if (v && !dataLike(v)) {
        const n = document.querySelectorAll("[" + a + "=" + q(v) + "]").length;
        add({kind: "testid", attr: a, value: v, count: n}, n);
        break;
      }
    }
    if (el.id && !dataLike(el.id)) {
      const n = document.querySelectorAll("#" + CSS.escape(el.id)).length;
      add({kind: "id", value: el.id, count: n}, n);
    }
    const role = roleOf(el);
    const name = role ? nameOf(el, role) : "";
    if (role && name && !hasDigit(name) && name.length <= 80 && ROLE_SEL[role]) {
      const same = (x) => roleOf(x) === role && nameOf(x, role) === name;
      add({kind: "role", role, name, count: count(ROLE_SEL[role], same, true)}, count(ROLE_SEL[role], same, false));
    }
    const isField = /^(input|select|textarea)$/.test(el.localName);
    const label = isField ? labelOf(el) : "";
    if (label && !hasDigit(label) && label.length <= 80) {
      const n = count("input, select, textarea", (x) => labelOf(x) === label, false);
      add({kind: "label", text: label, count: n}, n);
    }
    const ph = isField ? norm(el.getAttribute("placeholder")) : "";
    if (ph && !hasDigit(ph) && ph.length <= 80) {
      const n = count("[placeholder]", (x) => norm(x.getAttribute("placeholder")) === ph, false);
      add({kind: "placeholder", text: ph, count: n}, n);
    }
    if (/^(a|button|summary)$/.test(el.localName)) {
      const t = norm(el.textContent);
      if (t && !hasDigit(t) && t.length <= 80) {
        const n = count(el.localName, (x) => norm(x.textContent) === t, false);
        add({kind: "text", tag: el.localName, text: t, count: n}, n);
      }
    }
    const [anchor, anchorSel] = anchorOf(el);
    const css = anchorSel + " > " + pathFrom(anchor, el);
    let matches = [];
    try { matches = Array.from(document.querySelectorAll(css)); } catch (e) {}
    const index = matches.indexOf(el);
    const structure = index >= 0 ? {kind: "structure", css, index, count: matches.length} : null;
    if (row && !strong) {
      // A control repeated once per record: only the record says which one. Candidates that
      // would pick one by position are left out, so a reordered list blocks instead.
      // The records: the row's list under the nearest identified ancestor, at any depth, so a
      // wrapper added around the list does not lose them.
      const [ra, raSel] = anchorOf(row), holder = row.parentElement;
      // the list itself may be the identified ancestor (PrimeFaces: <tbody id="form:table_data">)
      let rows = raSel + (holder === ra ? " > " : " " + (holder && holder !== document.body ? seg(holder) + " > " : "")) + seg(row);
      // the list may itself be the identified element (`<div id="tbodyid" class="row">`): a
      // locator must find the rows in this very page, so the first form that does is kept
      const finds = (s) => { try { return Array.from(document.querySelectorAll(s)).includes(row); } catch (e) { return false; } };
      if (!finds(rows)) {
        rows = [raSel + " > " + seg(row), raSel + " " + seg(row),
                (holder && holder !== document.body ? seg(holder) + " > " : "") + seg(row)].find(finds) || rows;
      }
      let control = row === el ? "" : pathFrom(row, el);
      // a cell of a table row (`td`) is one of several alike: its place among them names it
      let alike = 0;
      try { alike = control ? row.querySelectorAll(":scope > " + control).length : 0; } catch (e) {}
      if (alike > 1) {
        const up = pathFrom(row, el.parentElement);
        control = (up ? up + " > " : "") + seg(el) + ":nth-child(" + (Array.prototype.indexOf.call(el.parentElement.children, el) + 1) + ")";
      }
      const title = rowTitle(row);
      let same = 0;
      try { for (const r of document.querySelectorAll(rows)) if (rowTitle(r) === title) same++; } catch (e) {}
      const ids = list.filter((c) => (c.kind === "id" || c.kind === "testid") && c.count === 1);
      return {list: ids.concat([{kind: "record", rows, control, count: same}]), title};
    }
    if (structure) list.push(structure);
    return {list};
  }

  // --- browser storage: key names and masked shapes, never a stored value -------------------
  // A value is parsed when it is JSON and every leaf replaced by a placeholder of its type (text
  // by its shape: letters A, digits 9), so only the record's structure leaves the page. A key that
  // looks like a credential keeps no shape at all. Cookies: names only.
  const SECRETISH = /token|secret|passw|api[-_]?key|session|cookie|auth|credential|jwt/i;
  const FIELDLIKE = /^[A-Za-z_$][\w$.-]{0,63}$/;
  function maskValue(v, depth) {
    if (v === null || v === undefined) return null;
    if (Array.isArray(v)) return depth > 6 ? [] : v.slice(0, 50).map((x) => maskValue(x, depth + 1));
    if (typeof v === "object") {
      if (depth > 6) return {};
      const out = {};
      for (const k of Object.keys(v).slice(0, 100)) {
        out[FIELDLIKE.test(k) && !dataLike(k) ? k : "*"] = maskValue(v[k], depth + 1);
      }
      return out;
    }
    if (typeof v === "number") return Number.isInteger(v) ? 0 : 0.5;  // the kind, not the number
    if (typeof v === "boolean") return false;
    return shapeOf(String(v)).slice(0, 32);
  }
  function maskStored(key, raw) {
    if (SECRETISH.test(key)) return "(credential-like key: value not kept)";
    let parsed;
    try { parsed = JSON.parse(raw); } catch (e) { return shapeOf(String(raw)).slice(0, 32); }
    return maskValue(parsed, 0);
  }
  let idb = null;  // [{db, stores}] from the last look (indexedDB answers asynchronously)
  function lookIdb() {
    try {
      if (!window.indexedDB || !indexedDB.databases) return;
      indexedDB.databases().then((list) => Promise.all(list.slice(0, 20).map((d) => new Promise((res) => {
        const done = (stores) => res({db: d.name, stores: stores || []});
        try {
          const r = indexedDB.open(d.name);
          r.onsuccess = () => {
            const db = r.result, stores = Array.from(db.objectStoreNames).slice(0, 20);
            db.close(); done(stores);
          };
          r.onerror = () => done();
          r.onblocked = () => done();
        } catch (e) { done(); }
      })))).then((x) => { idb = x.filter((d) => d.db); }, () => {});
    } catch (e) {}
  }
  function storageInfo() {
    const out = {};
    for (const kind of ["localStorage", "sessionStorage"]) {
      try {
        const s = window[kind], items = {};
        for (let i = 0; i < Math.min(s.length, 200); i++) {
          const k = s.key(i);
          if (k === null) continue;
          items[dataLike(k) ? shapeOf(k) : k] = maskStored(k, s.getItem(k));
        }
        if (Object.keys(items).length) out[kind] = items;
      } catch (e) {}
    }
    if (idb && idb.length) out.indexedDB = idb;
    try {
      const names = document.cookie.split(";").map((c) => c.split("=")[0].trim()).filter(Boolean);
      if (names.length) out.cookies = Array.from(new Set(names)).slice(0, 50);
    } catch (e) {}
    lookIdb();
    return out;
  }

  // --- steps --------------------------------------------------------------------------------
  // Each event goes to the recorder the moment it happens (`__tlEvent`), while the document is
  // alive: the recorder numbers it on arrival, the one sequence of the recording. The batch only
  // decides when the page is quiet enough for the after-snapshot (`__tlStep`), which names the
  // events it belongs to. A batch the page leaves before it is quiet gets no snapshot (what the
  // page shows then is not what the action led to; the next page's load is the next step), and
  // nothing is sent while the document is being torn down: no step depends on such a call.
  let eidSeq = 0;
  let loadStep = false;  // this document's load step was written (or it has none: a blank page)
  let lastStep = Promise.resolve();  // the last snapshot sent (`__tlBeforeAction` waits for it)
  function flush(leaving, settled) {
    clearTimeout(timer); timer = null;
    if (!TOP || !pending.length) return Promise.resolve();
    const eids = pending.map((p) => p.eid); pending = []; first = 0;
    // A field typed into that no longer has the focus as the snapshot is taken: what the page
    // does when a field is left (a message for a required field, a framework marking the field
    // as visited) is in the snapshot, so the replay must leave the field too. When the snapshot
    // is taken because the next action arrived (not after the page settled), what leaving did
    // may not be rendered yet on a busy machine: the step says it was left only when the field
    // or what sits next to it has changed since it was left, so the snapshot and the replay agree.
    const left = eids.filter((e) => {
      const el = fields.get(e);
      fields.delete(e);
      const before = leftLook.get(e);
      leftLook.delete(e);
      try {
        if (!el || (el.isConnected && el.matches(":focus"))) return false;
        return !!settled || before === undefined || lookAround(el) !== before;
      } catch (err) { return false; }
    });
    if (leaving) return Promise.resolve();
    try {
      return lastStep = window.__tlStep({eids, left, doc: FRAME_ID, url: location.href, ts: new Date().toISOString(),
                              html: snapshot(), storage: storageInfo(), sw: !!window.__tlSW});
    } catch (e) { return Promise.resolve(); }
  }
  // The settle rule: the page is quiet for SETTLE ms with no fetch or XHR of its own in flight
  // (up to NET_WAIT ms), so a step's after-snapshot has what a delayed answer shows.
  const NET_WAIT = 15000;
  let inflight = 0;
  function netDone() { inflight = Math.max(0, inflight - 1); quietAgain(); }
  try {
    const origFetch = window.fetch;
    if (origFetch) {
      window.fetch = function (...args) {
        if (!live()) return origFetch.apply(this, args);
        inflight++;
        let p;
        try { p = origFetch.apply(this, args); } catch (e) { netDone(); throw e; }
        p.then(netDone, netDone);
        return p;
      };
    }
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function (...args) {
      if (!live()) return send.apply(this, args);
      inflight++;
      this.addEventListener("loadend", netDone, {once: true});
      return send.apply(this, args);
    };
  } catch (e) {}
  // Read-only looks at what the page still has to do (no page code is run but these getters):
  // 2 when a request is queued or held back (PrimeFaces' ajax queue, a data table's filter
  // waiting out its delay) or a frame of this origin is still loading its page (a dialog-framework
  // page), 1 while jQuery animates (a dialog or panel opening), else 0. JSF's and PrimeFaces'
  // requests themselves are XHRs, counted in `inflight`.
  function busy() {
    try {
      const pf = window.PrimeFaces, queue = pf && pf.ajax && pf.ajax.Queue;
      if (queue && ((queue.requests && queue.requests.length) || (queue.xhrs && queue.xhrs.length)
          || (typeof queue.isEmpty === "function" && !queue.isEmpty()))) return 2;
      if (pf && pf.widgets) for (const k in pf.widgets) if (pf.widgets[k] && pf.widgets[k].filterTimeout) return 2;
    } catch (e) {}
    try {
      for (const f of document.querySelectorAll("iframe")) {
        let d = null;
        try { d = f.contentDocument; } catch (e) { d = null; }  // another origin's frame: its own
        const src = f.getAttribute("src") || "";
        if (d && (d.readyState !== "complete" || (src && !/^(about|javascript):/i.test(src) && d.URL === "about:blank"))) return 2;
      }
    } catch (e) {}
    try {
      const jq = window.jQuery;
      if (jq && jq.timers && jq.timers.length) return 1;
    } catch (e) {}
    return 0;
  }
  function quietAgain() {
    if (!TOP) { changed(); return; }
    if (!pending.length) return;
    clearTimeout(timer);
    const waited = Date.now() - first;
    if (waited > ((inflight || busy() === 2) ? NET_WAIT : MAX_WAIT)) { flush(false, true); return; }
    timer = setTimeout(() => {
      const left = Date.now() - first, b = busy();
      if ((inflight || b === 2) && left < NET_WAIT) quietAgain();
      else if (b === 1 && left < MAX_WAIT) quietAgain();
      else flush(false, true);
    }, SETTLE);
  }
  let lastPing = 0;
  function changed() {  // a frame's page changed: the top page waits until it settles too
    if (Date.now() - lastPing > 100) { lastPing = Date.now(); up({kind: "mut"}); }
  }
  function joins(action) {
    // A click (or key press) and the submit or navigation it causes are one step; anything else
    // starts a new one, so what the page shows now is captured as the result of the previous step.
    const last = pending[pending.length - 1];
    if (!last) return false;
    const gap = Date.now() - lastAt;
    if (action === "submit") return (last.action === "click" || last.action === "keypress") && gap < JOIN;
    if (action === "goto") return ["click", "submit", "keypress"].includes(last.action) && gap < NAV_JOIN;
    // a dialog the action opened (the page waits while it is open: no time limit)
    if (action === "dialog") return ["click", "dblclick", "keypress", "submit"].includes(last.action);
    return false;
  }
  function record(ev) {
    if (pending.length && !joins(ev.action)) flush();
    else if (pending.length) ev.caused = true;
    ev.eid = FRAME_ID + ":" + (++eidSeq);
    try {
      window.__tlEvent(Object.assign({}, ev, {doc: FRAME_ID, url: location.href}));
    } catch (e) {}
    pending.push({action: ev.action, eid: ev.eid});
    if (!first) first = Date.now();
    lastAt = Date.now();
    quietAgain();
  }
  // An event as the action met it: the time and (in the capture phase, before anything handled
  // it) the page; in a frame, the frame's own document.
  function make(action, el, extra) {
    const at = new Date().toISOString();  // before any work, so the action's requests come after
    const before = action === "goto" ? null : snapshot();
    const loc = action === "goto" ? {list: []} : locators(el);
    const ev = Object.assign({action, selector: el ? selectorOf(el) : null, before, at,
                              locators: loc.list, row_title: loc.title}, extra || {});
    const root = el && el.getRootNode ? el.getRootNode() : null;
    if (root && root.host) ev.shadow = root.mode === "open" ? "open" : "closed";
    return ev;
  }
  function emit(ev) {
    if (!TOP) {
      if (ev.action !== "goto") up(Object.assign({kind: "note", frames: []}, ev));
      return;
    }
    record(ev);
  }
  function note(action, el, extra) {
    releaseHeld();
    emit(make(action, el, extra));
  }
  window.addEventListener("message", (e) => {
    if (!live()) return;
    const d = e.data;
    if (!d || d.__tl !== TOKEN || !e.source || e.source === window) return;
    if (d.kind === "mut") { quietAgain(); return; }
    if (d.kind !== "note") return;
    const frames = [frameOf(e.source)].concat(d.frames || []);
    if (!TOP) { up(Object.assign({}, d, {frames})); return; }
    const ev = Object.assign({}, d, {selector: frames.join(" >>> ") + " >>> " + d.selector,
                                     frame: frames.join(" >>> "), before_scope: "frame"});
    delete ev.__tl; delete ev.kind; delete ev.frames;
    record(ev);
  });

  // --- clicks that may be the first half of a double click ---------------------------------
  let held = null;  // {el, events, timer}: in a frame, clicks wait to see whether one follows
  let clicks = [];  // the top page's recent clicks: {el, eid, ev, when}
  function releaseHeld() {
    if (!held) return;
    const h = held; held = null;
    clearTimeout(h.timer);
    for (const ev of h.events) emit(ev);
  }
  function holds(el) {
    if (!(dblSeen || LATE)) {
      let n = el, any = false;
      for (; n && n.nodeType === 1; n = n.parentElement) if (n.ondblclick || n.hasAttribute("ondblclick")) { any = true; break; }
      if (!any) return false;
    }
    const a = el.closest && el.closest("a[href]");
    if (a) {  // a link to another page is followed at once: the page would go before the wait ends
      try {
        const u = new URL(a.href, location.href);
        if (u.origin + u.pathname + u.search !== location.origin + location.pathname + location.search) return false;
      } catch (e) { return false; }
    }
    return true;
  }
  let userClick = false;
  function onClick(el, e) {
    // A click the page's own script makes while the person's click is still being handled is the
    // page's doing, not a step: a JSF command link (Mojarra's jsfcljs) adds a submit button to
    // the form, clicks it and removes it. Replaying the person's click makes the page do it again.
    if (e && e.isTrusted === false && userClick) return;
    if (e && e.isTrusted) { userClick = true; setTimeout(() => { userClick = false; }, 0); }
    if (closedHost(el)) return;
    const t = target(el);
    if (!t || t.nodeType !== 1) return;
    // Enter in a field "clicks" the form's default button: the key press is the step.
    if (e && e.detail === 0 && lastKey && lastKey.key === "Enter" && Date.now() - lastKey.at < 300) return;
    const type = (t.getAttribute("type") || "").toLowerCase();
    if (t.localName === "label" && t.control && /^(checkbox|radio)$/.test(t.control.type)) return;  // its control's click follows
    if (t.localName === "input" && (type === "checkbox" || type === "radio")) {
      note(t.checked ? "check" : "uncheck", t);
      return;
    }
    if (t.localName === "input" && type === "file") return;  // the choice is the upload step
    if (t.localName === "canvas") { note("click", t, {surface: "canvas"}); return; }
    if (!holds(t)) { note("click", t); return; }
    if (TOP) {
      // In the top page a click is sent at once, like any action (its place in the sequence is
      // when it happened); a double click that follows replaces the clicks it was made of.
      const ev = make("click", t);
      emit(ev);
      clicks = clicks.filter((c) => c.el === t && Date.now() - c.when < HOLD).slice(-1);
      clicks.push({el: t, eid: ev.eid, ev, when: Date.now()});
      return;
    }
    if (held && held.el === t && held.events.length < 2) {
      held.events.push(make("click", t));
      clearTimeout(held.timer);
      held.timer = setTimeout(releaseHeld, HOLD);
      return;
    }
    releaseHeld();
    if (TOP && pending.length) flush();  // the step before shows the page before this click
    held = {el: t, events: [make("click", t)], timer: setTimeout(releaseHeld, HOLD)};
  }
  function onDblClick(el) {
    const t = target(el);
    if (!t || t.nodeType !== 1) return;
    const prior = clicks.filter((c) => c.el === t);
    clicks = [];
    if (TOP && prior.length) {
      const firstClick = prior[0].ev;
      emit(Object.assign(make("dblclick", t), {before: firstClick.before, at: firstClick.at,
                                               locators: firstClick.locators, row_title: firstClick.row_title,
                                               replaces: prior.map((c) => c.eid)}));
      return;
    }
    if (held && held.el === t) {
      const firstClick = held.events[0];
      clearTimeout(held.timer); held = null;
      emit(Object.assign(make("dblclick", t), {before: firstClick.before, at: firstClick.at,
                                               locators: firstClick.locators, row_title: firstClick.row_title}));
      return;
    }
    note("dblclick", t);
  }

  // --- typing: kept as one `type` step per field, written before whatever comes next --------
  // What a person typed, kept only as its shape (letters A, digits 9, runs collapsed), never the
  // value: a field that shows another shape than was typed has an input mask.
  const dirty = new Map();  // field -> {typed: text typed, or null when something was deleted}
  function shapeOf(text) {
    let out = "";
    for (const ch of text) {
      const c = /\p{L}/u.test(ch) ? "A" : /\d/.test(ch) ? "9" : ch;
      if (!out || out[out.length - 1] !== c || (c !== "A" && c !== "9")) out += c;
    }
    return out;
  }
  function typable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    const tag = el.localName;
    if (tag === "textarea") return true;
    if (tag !== "input") return false;
    return !/^(checkbox|radio|button|submit|reset|image|hidden|file)$/.test((el.type || "").toLowerCase());
  }
  // A field being typed into is marked at its first input event, before the character is drawn
  // (and, inside a closed shadow root, its host, which is what a picture's mask can reach). Every
  // picture paints over what is marked as it is taken (`[data-tl-typed]` is resolved at capture
  // time), so a picture taken while the next field is being typed never shows that field's value,
  // which the recorder does not know yet. Snapshots leave `data-tl-*` attributes out.
  function markTyped(el) {
    try {
      if (!el.hasAttribute("data-tl-typed")) el.setAttribute("data-tl-typed", "");
      const root = el.getRootNode ? el.getRootNode() : null;
      if (root && root.host && root.mode === "closed" && !root.host.hasAttribute("data-tl-typed")) {
        root.host.setAttribute("data-tl-typed", "");
      }
    } catch (e) {}
  }
  function onInput(e) {
    const el = realTarget(e);
    if (!typable(el)) return;
    // Typing is written as a step when it ends, but it changes the page from its first character
    // (a list filtering as you type, a framework marking the field as edited): the step before
    // it is snapshotted now, in the capture phase, before the page's own handlers see the input.
    // Waiting for a timer here lets a framework mark this field as edited first, which no replay
    // shows at the step before (seen on the Angular reference application). What leaving the
    // previous field does may not be rendered yet: then that step does not say it was left
    // (`leftLook`), and its snapshot and its replay agree.
    if (!dirty.has(el) && TOP && pending.length) flush();
    markTyped(el);
    const d = dirty.get(el) || {typed: ""};
    if (el.type === "password") d.typed = null;
    else if (e.inputType && e.inputType.startsWith("delete")) d.typed = null;
    else if (typeof e.data === "string" && d.typed !== null) d.typed += e.data;
    dirty.set(el, d);
  }
  const written = new WeakMap();  // field -> the value its last type step had (in memory only)
  const fields = new Map();  // a type step not snapshotted yet (event id) -> its field
  // What a field and its surroundings look like (attributes and visibility, never values or
  // text), taken when its type step is written: if it is the same when the snapshot is taken,
  // leaving the field has not been rendered yet.
  const leftLook = new Map();
  function lookAround(el) {
    const one = (n) => {
      if (!n || n.nodeType !== 1) return "";
      const a = Array.from(n.attributes || []).filter((x) => x.name !== "value")
        .map((x) => x.name + "=" + x.value).sort().join("&");
      return n.localName + "[" + a + "]" + (n.hidden ? "h" : "");
    };
    const parts = [one(el), one(el.parentElement)];
    let sib = el.nextElementSibling;
    for (let i = 0; sib && i < 3; i++, sib = sib.nextElementSibling) parts.push(one(sib));
    sib = el.parentElement && el.parentElement.nextElementSibling;
    if (sib) parts.push(one(sib));
    return parts.join("|");
  }
  const valueOf = (el) => el.isContentEditable ? el.innerText : String(el.value == null ? "" : el.value);
  function emitType(el) {
    const d = dirty.get(el);
    if (!d) return;
    dirty.delete(el);
    const value = valueOf(el);
    written.set(el, value);
    const tid = FRAME_ID + "-" + (++tidSeq);
    const extra = {tid};
    if (value.trim() === "") extra.blank = value.slice(0, 64);
    else {
      secret(tid, value);
      if (d.typed) extra.typed_shape = shapeOf(d.typed);
    }
    releaseHeld();
    const ev = make("type", el, extra);
    emit(ev);
    if (TOP && ev.eid) { fields.set(ev.eid, el); try { leftLook.set(ev.eid, lookAround(el)); } catch (e) {} }
  }
  function emitAllTyped() { for (const el of Array.from(dirty.keys())) emitType(el); }
  function onChange(e) {
    const el = realTarget(e), tag = el.localName || "";
    if (tag === "select") { emitAllTyped(); note("select", el, {option_index: el.selectedIndex}); return; }
    if (tag === "input" && (el.type || "").toLowerCase() === "file") {
      // That a file was chosen, its name's shape, type and size class: never the file. Its
      // name is taken out of everything written, like a typed value.
      const files = Array.from(el.files || []).slice(0, 20).map((f) => {
        const tid = FRAME_ID + "-" + (++tidSeq);
        secret(tid, f.name);
        const kb = f.size / 1024, dot = f.name.lastIndexOf(".");
        const ext = dot > 0 ? f.name.slice(dot + 1).toLowerCase() : "";
        const stem = shapeOf(dot > 0 ? f.name.slice(0, dot) : f.name).slice(0, 56);
        return {name_shape: stem + (/^[a-z0-9]{1,8}$/.test(ext) ? "." + ext : ""),
                type: (f.type || "").slice(0, 80),
                size: kb < 1 ? "<1KB" : kb < 1024 ? "1KB-1MB" : kb < 10240 ? "1MB-10MB" : ">10MB"};
      });
      emitAllTyped();
      note("upload", el, {files});
      return;
    }
    if (typable(el)) {
      // Written already at Enter or blur: nothing. A value set without input events (a robot's
      // simulated typing) is written now.
      // The change a browser fires after Enter carries what the page's own key handler made
      // of the value (a list clearing its input): that is no typing.
      const afterEnter = lastKey && lastKey.key === "Enter" && Date.now() - lastKey.at < 500;
      if (!dirty.has(el) && !afterEnter && written.get(el) !== valueOf(el)) dirty.set(el, {typed: null});
      markTyped(el);
      emitType(el);
      return;
    }
    if (tag === "input" && /^(checkbox|radio)$/.test(el.type)) return;  // recorded as check/uncheck
    note("type", el);  // file, range, colour and other fields
  }
  function onSubmit(e) { emitAllTyped(); note("submit", realTarget(e)); }

  // --- keys: only named keys and chords, never the characters of typed text ---------------
  const KEYS = new Set(["Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                        "Home", "End", "PageUp", "PageDown"]);
  const EDITING_CHORDS = new Set(["a", "c", "v", "x", "z", "y"]);
  let lastKey = null;
  function onKey(e) {
    if (e.repeat || e.isComposing) return;
    const k = e.key;
    if (!k || /^(Shift|Control|Alt|Meta|AltGraph|CapsLock|Dead|Unidentified|Process)$/.test(k)) return;
    let key = k;
    const chord = (e.ctrlKey || e.metaKey) && !(e.ctrlKey && e.altKey);  // Ctrl+Alt is AltGr: text
    if (k.length === 1) {
      if (!chord) return;  // a character of typed text
      const code = e.code || "";
      key = code.startsWith("Key") ? code.slice(3).toLowerCase() : code.startsWith("Digit") ? code.slice(5) : null;
      if (!key) return;
    } else if (!KEYS.has(k) && !/^F\d{1,2}$/.test(k)) {
      return;  // Backspace, Delete and the like change the text: the type step has the result
    }
    const el = realTarget(e);
    if (chord && EDITING_CHORDS.has(key) && typable(el)) return;  // select all, copy, paste, undo
    const modifiers = [];
    if (e.ctrlKey) modifiers.push("Control");
    if (e.altKey) modifiers.push("Alt");
    if (e.metaKey) modifiers.push("Meta");
    if (e.shiftKey) modifiers.push("Shift");
    if (dirty.has(el)) emitType(el);
    lastKey = {key, at: Date.now()};
    note("keypress", el && el.nodeType === 1 ? el : null, {key, modifiers});
  }

  // A closed root hides its inside even from the event path: such a click reaches the document
  // as a click on the host, and the listener inside the root (below) records the real element.
  function closedHost(el) {
    return !!(el && el.nodeType === 1 && !el.shadowRoot && window.__tlShadows
              && window.__tlShadows.get(el));
  }
  // Dialogs: which one, and the choice made (a prompt's text is a typed value: its shape only).
  for (const kind of ["alert", "confirm", "prompt"]) {
    const orig = window[kind];
    if (typeof orig !== "function") continue;
    window[kind] = function (...args) {
      if (!live()) return orig.apply(this, args);
      const r = orig.apply(this, args);
      const choice = kind === "alert" ? "accept" : kind === "confirm" ? (r ? "accept" : "dismiss")
        : (r === null ? "dismiss" : "accept");
      // the message is the application's text (data in it is masked in Python: mask_message)
      const extra = {dialog: {kind, choice, message: String(args[0] === undefined ? "" : args[0]).slice(0, 500)}};
      if (kind === "prompt" && typeof r === "string") {
        if (r.trim() === "") extra.blank = r.slice(0, 64);
        else { const tid = FRAME_ID + "-" + (++tidSeq); secret(tid, r); extra.tid = tid; extra.typed_shape = shapeOf(r); }
      }
      note("dialog", null, extra);
      return r;
    };
  }
  // Windows: how a script opens one (its name and features, never its address: that is the new
  // tab's own first step), and a window that a script closes.
  const win = (data) => { try { window.__tlWindow(data); } catch (e) {} };
  const origOpen = window.open;
  if (typeof origOpen === "function") {
    window.open = function (...args) {
      if (live()) win({open: {name: String(args[1] === undefined ? "" : args[1]).slice(0, 64),
                              features: String(args[2] === undefined ? "" : args[2]).slice(0, 300)}});
      return origOpen.apply(this, args);
    };
  }
  const origClose = window.close;
  if (typeof origClose === "function") {
    window.close = function (...args) {
      if (live()) win({close: true});
      return origClose.apply(this, args);
    };
  }
  // Drag and drop: recorded so the replay can say it does not perform it.
  document.addEventListener("dragstart", (e) => { const el = realTarget(e); if (el && el.nodeType === 1) note("drag", el); }, true);
  // A service worker the page registers: the replay blocks workers and says so.
  try {
    const SWC = window.ServiceWorkerContainer && ServiceWorkerContainer.prototype;
    if (SWC && SWC.register) {
      const reg = SWC.register;
      SWC.register = function (...args) { window.__tlSW = true; return reg.apply(this, args); };
    }
  } catch (e) {}
  document.addEventListener("click", (e) => onClick(realTarget(e), e), true);
  document.addEventListener("dblclick", (e) => { const el = realTarget(e); if (!closedHost(el)) onDblClick(el); }, true);
  document.addEventListener("input", onInput, true);
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("focusout", (e) => { const el = realTarget(e); if (dirty.has(el)) emitType(el); }, true);
  document.addEventListener("change", onChange, true);
  document.addEventListener("submit", onSubmit, true);
  // change and submit do not leave a shadow root: listen inside every root (closed ones too, as
  // the shadow hook hands them over), and watch them for changes.
  const watched = new WeakSet();
  function watchRoot(root) {
    if (!root || watched.has(root)) return;
    watched.add(root);
    root.addEventListener("change", onChange, true);
    root.addEventListener("submit", onSubmit, true);
    if (root.mode === "closed") {
      root.addEventListener("click", (e) => onClick(e.target, e), true);
      root.addEventListener("dblclick", (e) => onDblClick(e.target), true);
      root.addEventListener("input", onInput, true);
      root.addEventListener("keydown", onKey, true);
    }
    new MutationObserver(quietAgain).observe(root, {subtree: true, childList: true,
                                                    attributes: true, characterData: true});
  }
  window.__tlOnShadow = watchRoot;
  (window.__tlShadowList || []).forEach(watchRoot);
  for (const el of document.querySelectorAll("*")) if (el.shadowRoot) watchRoot(el.shadowRoot);

  window.__tlFlush = () => { emitAllTyped(); releaseHeld(); return flush(); };
  // Called by the recorder just before a recording script's next action, so the step before it
  // settles and gets its picture before that action's events come (a `fill`, then a `click` a few
  // ms later). `mode` says where the action goes: "leave" (another element than the field being
  // typed into: the field is left now, as the action's own focus change would), "stay" (the
  // same field) or "unknown". Typing still in the focused field is left alone unless "leave".
  // The batch then settles by the usual rule (at most BEFORE_WAIT ms; then it is snapshotted as
  // the action's first event would). A batch ending in a click, key press or submit is not
  // waited for: the navigation or dialog it causes may still join it.
  const BEFORE_WAIT = 3000;
  window.__tlBeforeAction = (mode) => {
    if (!TOP) return Promise.resolve(false);
    const el = document.activeElement;
    if (el && dirty.has(el)) {
      if (mode !== "leave") return Promise.resolve(false);
      try { el.blur(); } catch (e) {}
    }
    const last = pending[pending.length - 1];
    if (!dirty.size && (!last || ["click", "dblclick", "keypress", "submit"].includes(last.action))) {
      return Promise.resolve(false);
    }
    emitAllTyped();
    const until = Date.now() + BEFORE_WAIT;
    return new Promise((done) => {
      const check = () => {
        if (!pending.length) { lastStep.then(() => done(true), () => done(true)); return; }
        if (Date.now() > until) { Promise.resolve(flush()).then(() => done(true), () => done(true)); return; }
        setTimeout(check, 20);
      };
      check();
    });
  };
  // Before the document goes (beforeunload comes while it is still whole): typing not yet written
  // and a click held for a possible double click are sent now, not while it is torn down.
  window.addEventListener("beforeunload", () => { if (live()) { emitAllTyped(); releaseHeld(); } }, true);
  window.addEventListener("pagehide", () => { if (live()) { emitAllTyped(); releaseHeld(); flush(true); } });
  if (TOP) {
    const NAV = {pushState: "push", replaceState: "replace"};
    for (const name of ["pushState", "replaceState"]) {
      const orig = history[name];
      history[name] = function (...args) {
        if (!live()) return orig.apply(this, args);
        // The address an application gives itself while it starts (a client-side router's first
        // navigation, a route guard's redirect: Angular makes both before the load event) is
        // part of that load, whose step has the address the page ended at. Written as a step
        // of its own it would come before the load of its own document.
        if (!loadStep && !eidSeq) return orig.apply(this, args);
        emitAllTyped();
        releaseHeld();
        // The old address still shows the old page: capture it before the address changes.
        if (pending.length && !joins("goto")) flush();
        const r = orig.apply(this, args);
        note("goto", null, {nav: NAV[name]});
        return r;
      };
    }
    window.addEventListener("popstate", () => { if (live()) { emitAllTyped(); note("goto", null, {nav: "pop"}); } });
  }
  new MutationObserver(quietAgain).observe(document, {subtree: true, childList: true,
                                                      attributes: true, characterData: true});
  function loaded() {
    loadStep = true;
    // A blank page (a new tab before its first address) is no page of the application: the
    // recorder writes that setup step itself, so it never depends on this script running there.
    if (location.protocol === "about:") return;
    let nav = "navigate";
    try { nav = (performance.getEntriesByType("navigation")[0] || {}).type || nav; } catch (e) {}
    note("goto", null, {nav});
  }
  if (TOP) {
    if (document.readyState === "complete") loaded();
    else window.addEventListener("load", loaded);
  } else {
    changed();  // a frame that loads is a change of the page
  }
})();
"""


MODIFIERS = ("Control", "Alt", "Meta", "Shift")
LOCATOR_KINDS = {"testid", "id", "role", "label", "placeholder", "text", "structure", "record"}
LOCATOR_FIELDS = {"kind", "attr", "value", "role", "name", "text", "tag", "css", "index", "count",
                  "rows", "control"}  # fmt: skip

# Marks, in one frame, what a picture must paint over: every text field and editable region that
# shows text, and text that shows a value typed there. With `check`, counts what is unmasked.
MARK_TYPED_JS = r"""([values, check]) => {
  // check: count what shows a typed value and is not marked (the page changed since marking).
  if (!check) for (const el of document.querySelectorAll("[data-tl-mask]")) el.removeAttribute("data-tl-mask");
  // The rule of twinlab2_contract.evidence.typedvalues: a value shows where it is a whole token (not inside a
  // longer word: "report" typed does not mask "Reports"), a short one where it is all the text.
  const W = "[\\p{L}\\p{N}_]";
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pats = values.filter((v) => v.length >= 3).map((v) => new RegExp(
    (new RegExp("^" + W, "u").test(v) ? "(?<!" + W + ")" : "") +
    v.split(/\s+/).map(esc).join("\\s+") +
    (new RegExp(W + "$", "u").test(v) ? "(?!" + W + ")" : ""), "iu"));
  const shorts = values.filter((v) => v.length < 3);
  const hit = (t) => {
    t = t || "";
    return pats.some((p) => p.test(t)) || shorts.includes(t.trim().toLowerCase());
  };
  let n = 0;
  // An element inside a closed shadow root cannot be reached by a mask: its host is painted over.
  const reach = (el) => {
    const root = el.getRootNode ? el.getRootNode() : null;
    return root && root.host && root.mode === "closed" ? reach(root.host) : el;
  };
  // Covered only by what was marked before the picture: a field that got `data-tl-typed` after
  // Playwright resolved the masks may show in it, so it does not count (the picture is taken again).
  const covered = (el) => {
    for (let x = el; x; ) {
      if (x.closest && x.closest("[data-tl-mask]")) return true;
      const root = x.getRootNode ? x.getRootNode() : null;
      x = root && root.host ? root.host : null;
    }
    return false;
  };
  const mark = (el) => {
    el = reach(el);
    if (!check) { el.setAttribute("data-tl-mask", ""); n++; }
    else if (!covered(el) && el.getClientRects().length) n++;
  };
  // Every text field and editable region that shows text, whether or not its value is known to
  // the recorder (a field still being typed into, a value a script set): in the document, in
  // open shadow roots and in closed ones (the shadow hook's list).
  const NOT_TEXT = /^(checkbox|radio|button|submit|reset|image|hidden|file|range|color)$/;
  const roots = [document];
  for (let i = 0; i < roots.length && i < 500; i++) {
    for (const x of roots[i].querySelectorAll("*")) if (x.shadowRoot && !roots.includes(x.shadowRoot)) roots.push(x.shadowRoot);
  }
  for (const r of (window.__tlShadowList || [])) if (!roots.includes(r)) roots.push(r);
  for (const root of roots) {
    if (!check) for (const el of root.querySelectorAll("[data-tl-typed]")) mark(el);  // typed into, even if empty now
    for (const el of root.querySelectorAll("input, textarea")) {
      if (el.localName === "input" && NOT_TEXT.test((el.type || "").toLowerCase())) continue;
      if (String(el.value || "") !== "") mark(el);
    }
    for (const el of root.querySelectorAll("[contenteditable]")) {
      if (el.isContentEditable && (el.innerText || el.textContent || "").trim() !== "") mark(el);
    }
  }
  if (document.body) {
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const t = walk.currentNode;
      if (t.parentElement && hit(t.data)) mark(t.parentElement);
    }
  }
  return n;
}"""


WINDOW_FEATURES = ("width", "height", "left", "top", "innerwidth", "innerheight", "popup",
                   "menubar", "toolbar", "location", "status", "resizable", "scrollbars",
                   "noopener", "noreferrer")  # fmt: skip


def window_facts(opened: dict) -> dict:
    """What a `window.open` call says about the window: its target name when that is a plain
    name, and the features this list knows with a number or yes/no; anything else is dropped."""
    import re

    out: dict = {"how": "window.open"}
    name = str(opened.get("name") or "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,39}", name):
        out["name"] = name
    features: dict = {}
    for part in str(opened.get("features") or "").lower().split(","):
        key, _, value = (x.strip() for x in part.partition("="))
        if key not in WINDOW_FEATURES:
            continue
        if re.fullmatch(r"[0-9]{1,5}(px)?", value):
            features[key] = int(value.removesuffix("px"))
        elif value in ("", "yes", "1", "true"):
            features[key] = True
        elif value in ("no", "0", "false"):
            features[key] = False
    if features:
        out["features"] = features
    return out


def link_events(steps: list[dict]) -> bool:
    """`caused_by_event` and a record locator's `text_from_event`: the event_id of the step that
    `caused_by` / `text_from` number, so a reference survives any renumbering. True if added."""
    by_seq = {s["seq"]: s.get("event_id") for s in steps if isinstance(s.get("seq"), int)}
    added = False
    for s in steps:
        cause = by_seq.get(s.get("caused_by")) if isinstance(s.get("caused_by"), int) else None
        if cause and s.get("caused_by_event") != cause:
            s["caused_by_event"] = cause
            added = True
        for cand in s.get("locators") or []:
            ref = cand.get("text_from") if isinstance(cand, dict) else None
            if (
                isinstance(ref, int)
                and by_seq.get(ref)
                and cand.get("text_from_event") != by_seq[ref]
            ):
                cand["text_from_event"] = by_seq[ref]
                added = True
    return added


def _public(step: dict) -> dict:
    """A step as written: without the recorder's own bookkeeping (keys starting with _)."""
    return {k: v for k, v in step.items() if not k.startswith("_")}


def next_action(steps: list[dict], n: int) -> dict | None:
    """The step after `steps[n]` in the same tab that can change what its page shows: not a
    dialog (the page is held while one is open), a download or a tab step, and not a step that
    `steps[n]` caused itself."""
    step = steps[n]
    for s in steps[n + 1 :]:
        action = str(s.get("action") or "")
        if (s.get("tab") or "") != (step.get("tab") or "") or action.startswith("tab-"):
            continue
        if action in ("dialog", "download") or s.get("caused_by") == step.get("seq"):
            continue
        return s
    return None


def mark_unsettled_pictures(steps: list[dict], drop_in: Path | None = None) -> int:
    """A step's picture is taken when the recording loop gets control back, after its snapshot.
    When the person's next action in the same tab had already begun by then (typing ends when
    the person clicks on, and the click starts a dialog's fade), the picture may show what that
    action did: it is kept, and the step says `picture_settled: false` with the reason, so a
    visual comparison does not hold the replica to it. With `drop_in` (the recording folder:
    attach mode, where nothing holds the robot's next action), such a picture is deleted instead
    and the step says `picture_dropped: true` with the reason. Returns how many."""
    from twinlab2_contract.evidence.recording import _ts

    marked = 0
    for n, step in enumerate(steps):
        shot = _ts(step.get("_shot_at"))
        if not step.get("screenshot") or shot is None:
            continue
        nxt = next_action(steps, n)
        began = _ts(nxt.get("ts")) if nxt is not None else None
        if began is None or began >= shot:
            continue
        ms = int((shot - began).total_seconds() * 1000)
        if drop_in is not None:
            (Path(drop_in) / step["screenshot"]).unlink(missing_ok=True)
            step["screenshot"] = None
            step.pop("mask_boxes", None)
            step["picture_dropped"] = True
            step["picture_note"] = (f"no picture: step {nxt.get('seq')} ({nxt.get('action')}) began "
                                    f"{ms} ms before it could be taken, so it would show what that "
                                    "step did")  # fmt: skip
            marked += 1
            continue
        step["picture_settled"] = False
        step["picture_note"] = (f"the picture was taken {ms} ms after step {nxt.get('seq')} "
                                f"({nxt.get('action')}) began: it may show what that step did")  # fmt: skip
        marked += 1
    return marked


def _excluded(exclude: Callable[[str], str | None], url: str) -> bool:
    try:
        return bool(url) and exclude(url) is not None
    except Exception:  # noqa: BLE001 - an address the list cannot read is not excluded by it
        return False


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") else ""


def _shape(text: str) -> str:
    from twinlab2_contract.evidence.dom import shape

    return shape(text) or ""


def dialog_message(text, secrets=None) -> str:
    """The message of a browser dialog (alert, confirm, prompt) as the step keeps it: the
    application's interface text (`Product added.`), with any typed value replaced by its shape
    and every value-like word masked (`sanitize.mask_message`: digits, e-mail addresses)."""
    from twinlab2_contract.evidence import sanitize

    if not isinstance(text, str) or not text.strip():
        return ""
    if secrets:
        text = secrets.redact_text(text)
    return sanitize.mask_message(" ".join(text.split()))


def _locators(raw) -> list[dict]:
    """The page's locator candidates, keeping only known kinds and fields."""
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or c.get("kind") not in LOCATOR_KINDS:
            continue
        cand = {k: v for k, v in c.items() if k in LOCATOR_FIELDS
                and isinstance(v, (str, int)) and not isinstance(v, bool)}  # fmt: skip
        out.append(cand)
    return out


STORAGE_KINDS = ("localStorage", "sessionStorage")
_SHAPE_TEXT = None  # compiled lazily: a text that is already a shape (A, 9 and punctuation)


def _mask_json(value, depth: int = 0):
    """The page masked stored values already; this keeps it so whatever a page sent: every text
    leaf that is not already a shape becomes its shape, numbers 0, booleans false."""
    import re

    global _SHAPE_TEXT
    if _SHAPE_TEXT is None:
        _SHAPE_TEXT = re.compile(r"^[A9\W_]*$|^\(credential-like key: value not kept\)$")
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(k)[:120]: _mask_json(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [_mask_json(v, depth + 1) for v in value[:50]]
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0 if value.is_integer() else 0.5
    if isinstance(value, str):
        return value[:64] if _SHAPE_TEXT.match(value) else _shape(value)[:32]
    return None


def _storage(raw) -> dict | None:
    """Browser storage as the page reported it, keeping only the expected structure."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for kind in STORAGE_KINDS:
        items = raw.get(kind)
        if isinstance(items, dict) and items:
            out[kind] = {str(k)[:120]: _mask_json(v) for k, v in list(items.items())[:200]}
    dbs = []
    found = raw.get("indexedDB")
    for d in found if isinstance(found, list) else []:
        if isinstance(d, dict) and isinstance(d.get("db"), str) and d["db"]:
            stores = [str(s)[:120] for s in d.get("stores") or [] if isinstance(s, str)][:20]
            dbs.append({"db": d["db"][:120], "stores": stores})
    if dbs:
        out["indexedDB"] = dbs[:20]
    names = raw.get("cookies")
    if isinstance(names, list):
        clean = sorted({str(n)[:80] for n in names if isinstance(n, str) and n})[:50]
        if clean:
            out["cookies"] = clean
    return out or None


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


VIEWPORT = {"width": 1280, "height": 720}  # explicit, so pictures compare at a known size
DEVICE_SCALE_FACTOR = 1


def _boxes(masks: list) -> list[dict]:
    """The rectangles painted over in a picture (x, y, width, height in CSS pixels of the
    viewport), so a visual comparison can leave exactly those out."""
    out = []
    for loc in masks:
        try:
            for el in loc.all():
                box = el.bounding_box(timeout=1000)
                if box and box["width"] > 0 and box["height"] > 0:
                    out.append({k: round(float(box[k]), 1) for k in ("x", "y", "width", "height")})
        except Exception:  # noqa: BLE001 - an element that went away is not painted either
            continue
    return out[:200]


def write_meta(
    out_dir: Path, browser, viewport: dict | None, mode: str, settle_ms: int | None = None
) -> None:
    """`recording.json`: how the recording was made (browser, viewport, scale), for replays and
    visual comparison. Optional: older recordings have none."""
    meta = {"recorder": "twinlab", "mode": mode, "viewport": viewport,
            "device_scale_factor": DEVICE_SCALE_FACTOR if viewport else None}  # fmt: skip
    if settle_ms is not None:  # when a step's after-snapshot is taken
        meta["settle"] = {
            "quiet_ms": int(settle_ms),
            "network": "no fetch or XHR in flight",
            "max_ms": 15000,
        }
    try:
        meta["browser"] = browser.browser_type.name
        meta["browser_version"] = browser.version
    except Exception:  # noqa: BLE001
        pass
    (Path(out_dir) / "recording.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _safe_url(page) -> str:
    try:
        url = page.url
    except Exception:  # noqa: BLE001 - a page that went away
        return ""
    return url if isinstance(url, str) and url.startswith(("http:", "https:")) else ""


def _guid(page) -> str:
    """The page's Playwright id: the `pageref` of the HAR entries it made."""
    impl = getattr(page, "_impl_obj", None)
    return str(getattr(impl, "_guid", None) or f"page-{id(page)}")


class _Secrets(TypedValues):
    """Values typed while recording. Held in memory only; used to keep them out of everything
    written: snapshots, the HAR, pictures, locator texts. Blank values are not secrets. Where a
    typed value is taken out, and where it is kept as structure, is twinlab2_contract.evidence.typedvalues."""

    def __bool__(self) -> bool:
        return bool(self.values)

    def add(self, value: str, origin: str, tid: str) -> None:
        if value.strip():
            self.values.append((value, origin, tid))
            self._remember(value, tid)

    def place(self, tid: str, seq: int) -> None:
        self.steps[tid] = seq

    def renumbered(self, mapping: dict[int, int]) -> None:
        self.steps = {t: mapping.get(s, s) for t, s in self.steps.items()}

    def for_origin(self, origin: str) -> list[str]:
        return sorted({v.strip().lower() for v, o, _ in self.values if o == origin or not o})

    def typed_step(self, title: str) -> int | None:
        """The latest step whose typed value is this record title."""
        want = " ".join(title.split()).lower()
        if not want:
            return None
        for value, _, tid in reversed(self.values):
            if " ".join(value.split()).lower() == want and tid in self.steps:
                return self.steps[tid]
        return None

    def clean_step(self, step: dict) -> dict:
        """A step by the rule (twinlab2_contract.evidence.typedvalues): locator candidates whose text holds a typed
        value are dropped; an id, test id or class in the selector, the frame or a locator that
        is a whole typed value gets its placeholder (said in `typed_placeholders`, with the
        placeholders its snapshots use); a typed value in the page address (its query,
        fragment or a whole path segment) or in a storage key is replaced by its shape."""
        if not self.values:
            return step
        out = dict(step)
        used = set(step.get("_ph_dom") or ())
        locs = step.get("locators")
        if locs:
            out["locators"] = self.clean_locators(locs, used)
        for k in ("selector", "frame"):
            if isinstance(step.get(k), str):
                css, shown = self.redact_css(step[k], used)
                out[k] = self._text(css) if shown else css
        if isinstance(step.get("url"), str):
            out["url"] = self.redact_url(step["url"])
        store = step.get("storage")
        if isinstance(store, dict):
            store = dict(store)
            for kind in STORAGE_KINDS:
                if isinstance(store.get(kind), dict):
                    store[kind] = {self.redact_text(k): v for k, v in store[kind].items()}
            out["storage"] = store
        notes = self.placeholder_notes(used)
        if notes:
            out["typed_placeholders"] = notes
        else:
            out.pop("typed_placeholders", None)
        return out

    def scrub_har(self, path: Path) -> None:
        if not self.values or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        path.write_text(json.dumps(self.scrub(data)), encoding="utf-8")


class _Steps:
    """Receives captures from the page and writes `steps.jsonl`, `dom/` and `screens/`."""

    def __init__(
        self,
        out_dir: Path,
        screenshots: bool = True,
        origins: set[str] | None = None,
        outside: Callable | None = None,
        journal=None,
        exclude: Callable[[str], str | None] | None = None,
    ):
        self.out_dir = out_dir
        # The PC's exclude list (twinlab2_recorder.workstation.exclude): `exclude(url)` names the
        # entry an address falls under; such a page is not recorded and not even reported as
        # `outside` (Session.excluded_url keeps the entry's name).
        self.exclude = exclude
        self.seq = 0
        # A workstation session's capture journal (twinlab2_recorder.workstation.journal): each step's event
        # line the moment it is numbered, the step once it is written, and what close() folds.
        self.journal = journal
        self.recording = Path(out_dir).name
        self.eid_event: dict[str, str] = {}  # the page's event id -> the step's event_id
        self.last_event_id: str | None = None
        # A workstation session records only the pages of its target origins: an action, tab
        # step or download elsewhere is not numbered or written; `outside(origin, ts)` hears of it.
        self.origins = {o.lower() for o in origins} if origins is not None else None
        self.outside = outside
        self.screenshots = screenshots
        self.file = (out_dir / "steps.jsonl").open("w", encoding="utf-8")
        # Steps wait here for their picture: Playwright cannot be called from inside the binding
        # callback, so `drain()` takes the pictures when the recording loop gets control back.
        self.waiting: list[tuple[dict, object]] = []
        self.fetched: dict[str, bytes] = {}  # PDFs the pages embed: url -> bytes
        self.written: list[dict] = []  # steps as written, rewritten at close() with every secret
        # Typed values, in memory only (never written): they are taken out of snapshots, the HAR,
        # pictures and locator texts, and tell which record a later action is on.
        self.secrets = _Secrets()
        # Every tab and popup of the context: page id -> {"tab", "opener", "har_page", ...}.
        self.tabs: dict[str, dict] = {}
        self.pages: dict[str, object] = {}  # page id -> the page, to find openers
        self.closing = False  # set before the browser is closed: those closes are not steps
        self.last_storage: dict[str, str] = {}  # tab -> the storage last written for it
        self.last_boxes: list[dict] = []  # the mask rectangles of the last picture taken
        self.tab_last: dict[str, tuple] = {}  # tab -> (seq, action, caused_by, ts, step) of its last step
        # Steps whose batch has not been flushed yet: event id -> (step, page), in arrival order;
        # per tab, the document they came from and the step that started the current batch.
        self.open: dict[str, tuple[dict, object]] = {}
        self.open_doc: dict[str, str] = {}
        self.batch_cause: dict[str, int] = {}
        self.rewrite = False  # a step already written was changed: close() writes them again
        self.window_opens: list[dict] = []  # window.open calls: {tab, at, used, window}
        self.sw_seen: set[str] = set()  # tabs whose page registered a service worker
        self.nav_observed: list[tuple[str, int, int]] = []  # (tab, cause, page load) per load
        self.scripts: list[str] = []  # the init scripts, for a page that missed them
        self.replaced: list[tuple[int, list[str]]] = []  # (double click, the clicks it replaces)
        # attach(): a picture taken after the next action began is dropped (the step says so),
        # not kept as `picture_settled: false`; record() keeps it marked.
        self.drop_late_pictures = False

    LOAD_CAUSES = ("click", "submit", "keypress", "dblclick")

    def recorded(self, url: str, ts: str | None = None) -> bool:
        """Is this address recorded? Always, outside a session; in one, only a target origin
        (a page with no web origin yet, about:blank, is kept: it carries no data)."""
        if getattr(self, "exclude", None) is not None and url and _excluded(self.exclude, url):
            return False
        if self.origins is None:
            return True
        origin = _origin(url or "").lower()
        if not origin or origin in self.origins:
            return True
        if self.outside is not None:
            try:
                self.outside(origin, ts or _now())
            except Exception:  # noqa: BLE001 - the session's bookkeeping never stops a recording
                pass
        return False

    def _loaded_by(self, tab: str | None, event: dict) -> int | None:
        """The step that loaded a new document in this tab: a page load that comes within a few
        seconds of a click, key press or submit in the same tab (a form posted, a link followed)
        was caused by it, as the listener says for a change of address without a load."""
        from twinlab2_contract.evidence.recording import _ts

        if event.get("action") != "goto" or event.get("nav") != "navigate":
            return None
        last = self.tab_last.get(tab or "")
        if not last or last[1] not in self.LOAD_CAUSES:
            return None
        before, now = _ts(last[3]), _ts(event.get("at"))
        if before is None or now is None or not 0 <= (now - before).total_seconds() < 10:
            return None
        return last[2] if isinstance(last[2], int) else last[0]

    # --- tabs ------------------------------------------------------------------------------------

    def tab_of(self, page) -> str | None:
        """The tab id of a page (t1, t2... in the order they opened); registers it if new."""
        if page is None:
            return None
        key = _guid(page)
        if key not in self.tabs:
            # The page's first event came before the browser said the tab opened: it is the
            # same tab-open, whichever of the two arrives first.
            self.on_page(page)
        return self.tabs[key]["tab"]

    def on_page(self, page, quiet: bool = False) -> None:
        """A new tab or popup (`context.on("page")`). Only attributes are read here: Playwright
        is not called from inside an event handler."""
        key = _guid(page)
        if key in self.tabs:
            return
        opener_impl = getattr(getattr(page, "_impl_obj", None), "_opener", None)
        opener_key = str(getattr(opener_impl, "_guid", "")) if opener_impl is not None else ""
        opener = self.tabs.get(opener_key, {}).get("tab")
        info = {"tab": f"t{len(self.tabs) + 1}", "opener": opener, "har_page": key,
                "opened": _now(), "closed": None, "first_url": None}  # fmt: skip
        self.tabs[key] = info
        self.pages[key] = page
        try:
            page.on("close", lambda _p=None, page=page: self.on_page_close(page))
            page.on("download", lambda d, page=page: self.on_download(page, d))
        except Exception:  # noqa: BLE001 - a page that is already gone
            pass
        # The tab-open step is written when the tab's first action reaches the recorder (or when
        # it closes), never when the browser reports the tab: that report can come before or
        # after the click that opened it, and the order must not depend on which came first.
        info["announced"] = quiet or len(self.tabs) == 1

    def _announce(self, info: dict | None, ts: str | None = None) -> None:
        if info is None or info.get("announced") is not False:
            return
        info["announced"] = True
        page = self.pages.get(info["har_page"])
        self._tab_step("tab-open", info, url=_safe_url(page) if page is not None else "", ts=ts)

    def on_page_close(self, page) -> None:
        info = self.tabs.get(_guid(page))
        if info is None or info["closed"]:
            return
        info["closed"] = _now()
        self._announce(info)  # a tab that closed before it did anything was still opened
        self._close_open(info["tab"])  # its last batch gets no snapshot now
        if not self.closing:
            self._tab_step("tab-close", info, url=info.get("last_url") or "")

    def on_download(self, page, download) -> None:
        """A file the page downloaded: a step of its own (the shape of its suggested name and its
        extension), caused by the action before it in that tab. The file is never kept."""
        info = self.tabs.get(_guid(page)) or {}
        tab = info.get("tab")
        try:
            name = str(download.suggested_filename or "")
        except Exception:  # noqa: BLE001
            name = ""
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        ext = ext.lower() if ext.isalnum() and len(ext) <= 8 else ""
        url = _safe_url(page)
        if not self.recorded(url):
            return
        self.seq += 1
        step = {"seq": self.seq, "ts": _now(), "url": url, "action": "download", "selector": None,
                "dom": None, "dom_before": None, "screenshot": None,
                "download": {"name_shape": (_shape(stem) + (f".{ext}" if ext else ""))[:64],
                             "ext": ext}}  # fmt: skip
        if tab:
            step["tab"] = tab
        if _origin(url):
            step["origin"] = _origin(url)
        last = self.tab_last.get(tab or "")
        if last and last[1] in self.LOAD_CAUSES:
            step["caused_by"] = last[2] if isinstance(last[2], int) else last[0]
        self._captured(step)
        self.waiting.append((step, None))

    def _tab_step(self, action: str, info: dict, url: str = "", ts: str | None = None) -> None:
        if not self.recorded(url, ts):
            return
        self.seq += 1
        step = {"seq": self.seq, "ts": ts or _now(), "url": url, "action": action, "selector": None,
                "dom": None, "dom_before": None, "screenshot": None, "tab": info["tab"]}  # fmt: skip
        if info.get("opener"):
            step["opener"] = info["opener"]
        if url:
            step["origin"] = _origin(url)
        self._captured(step)
        self.waiting.append((step, None))

    def _captured(self, step: dict) -> None:
        """A step just numbered: its permanent id (twinlab2_recorder.workstation.journal.stamp), kept
        through renumbering, and its event line in the session's journal."""
        from twinlab2_recorder.workstation import journal as capture

        capture.stamp(step, "web")
        self.last_event_id = step["event_id"]
        if self.journal is not None:
            self.journal.event(step, self.recording)

    def on_window(self, source, data: dict) -> None:
        """A script opened a window (`window.open`: its name and features) or closed its own
        (`window.close`), from the page that did it (`__tlWindow`). Kept for close(), which puts
        it on the tab's `tab-open` and `tab-close` steps."""
        page = source.get("page") if isinstance(source, dict) else None
        if page is None or not isinstance(data, dict):
            return
        tab = self.tab_of(page)
        info = self.tabs.get(_guid(page))
        if data.get("close") is True and info is not None:
            info["closed_by"] = "script"
        opened = data.get("open")
        if isinstance(opened, dict) and tab:
            self.window_opens.append({"tab": tab, "at": _now(), "used": False,
                                      "window": window_facts(opened)})  # fmt: skip

    def _window_facts(self, steps: list[dict]) -> None:
        """`window` on the `tab-open` step of a tab a script opened with `window.open` (the
        call made in the opening tab nearest its opening, within three seconds), and
        `closed_by: script` on the `tab-close` step of a window that a script closed."""
        from datetime import datetime

        def when(text) -> float | None:
            try:
                return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

        by_tab = {info["tab"]: info for info in self.tabs.values()}
        for info in by_tab.values():
            opened = when(info.get("opened"))
            calls = [c for c in self.window_opens if c["tab"] == info.get("opener")
                     and not c["used"] and opened is not None and when(c["at"]) is not None
                     and abs(when(c["at"]) - opened) <= 3]  # fmt: skip
            if calls:
                call = min(calls, key=lambda c: abs(when(c["at"]) - opened))
                call["used"] = True
                info["window"] = call["window"]
        for s in steps:
            info = by_tab.get(s.get("tab")) or {}
            if s.get("action") == "tab-open" and info.get("window"):
                s["window"] = info["window"]
            elif s.get("action") == "tab-close" and info.get("closed_by"):
                s["closed_by"] = info["closed_by"]

    def on_secret(self, source, data: dict) -> None:
        """A typed value, from the frame it was typed in (`__tlSecret`)."""
        if not isinstance(data, dict) or not isinstance(data.get("value"), str):
            return
        frame = source.get("frame") if isinstance(source, dict) else None
        try:
            origin = _origin(frame.url) if frame is not None else ""
        except Exception:  # noqa: BLE001 - a frame that went away
            origin = ""
        self.secrets.add(data["value"], origin, str(data.get("tid") or ""))

    def drain(self, pictures: bool = True) -> None:
        """Finish the waiting steps: frames the page could not read, canvases, the picture.
        Called when the recording loop has control (never from inside the binding)."""
        from twinlab2_contract.evidence.dom import fill_frames

        if pictures:
            self._ensure_installed()
        waiting, self.waiting = self.waiting, []
        # The pictures first, the moment the loop has control: each is of the page as its step
        # left it only until the next action begins (frames, canvases, documents and cookies
        # can wait; they come from the snapshot or do not change on screen).
        for step, page in waiting:
            if pictures and page is not None and self.screenshots and step["dom"]:
                later = self._overtaken(step)
                if later is not None and self.drop_late_pictures:
                    # The next action has already begun: a picture now would show what it did,
                    # and taking it would only make the next step's picture late as well.
                    step["picture_dropped"] = True
                    step["_dropped_for"] = later  # the note is written at close(), renumbered
                    step["picture_note"] = (f"no picture: step {later[0]} ({later[1]}) began before it "
                                            "could be taken, so it would show what that step did")  # fmt: skip
                    continue
                rel = f"screens/{step['seq']:04d}.png"
                try:
                    (self.out_dir / "screens").mkdir(exist_ok=True)
                    shot_at = _now()  # an action that began before this may show in it
                    stop = (lambda step=step: self._overtaken(step)) if self.drop_late_pictures else None
                    if self._picture(page, self.out_dir / rel, stop):
                        step["screenshot"] = rel
                        step["_shot_at"] = shot_at
                        if self.last_boxes:  # painted over: visual comparison leaves them out
                            step["mask_boxes"] = self.last_boxes
                    elif stop is not None and stop() is not None:
                        step["picture_dropped"] = True
                        step["_dropped_for"] = stop()
                        step["picture_note"] = "no picture: the next action began before it could be taken"
                except Exception:  # noqa: BLE001 - a missing picture never loses the step
                    pass
        for step, page in waiting:
            html = step.pop("_html", None)
            live = pictures and page is not None
            if html is not None:
                if live:
                    try:
                        html = fill_frames(page, html, values=False)
                    except Exception:  # noqa: BLE001 - the page may have gone
                        pass
                    if self.screenshots:
                        html = self._canvases(page, html, step["seq"])
                    self._documents(page, html, step.get("url") or "")
                if "data-tl-pending=" in html:
                    step["frames_unread"] = html.count("data-tl-pending=")
                used: set[str] = set()
                html = self.secrets.redact_html(html, used)
                if used:  # ids and names that were a whole typed value: their placeholders
                    step["_ph_dom"] = sorted(used | set(step.get("_ph_dom") or ()))
                (self.out_dir / step["dom"]).write_text(html, encoding="utf-8")
            if live and isinstance(step.get("storage"), dict) and step.get("url"):
                try:  # HttpOnly cookies too, by name; a cookie's value is never read
                    names = {c["name"] for c in page.context.cookies([step["url"]])}
                    names |= set(step["storage"].get("cookies") or [])
                    if names:
                        step["storage"]["cookies"] = sorted(names)[:50]
                    # every site's cookies the browser holds, by name, per domain
                    per: dict[str, set] = {}
                    for c in page.context.cookies():
                        per.setdefault(str(c.get("domain") or ""), set()).add(str(c["name"]))
                    if per:
                        step["storage"]["cookies_by_domain"] = {
                            d: sorted(n)[:50] for d, n in sorted(per.items())[:20]
                        }
                except Exception:  # noqa: BLE001 - the page may have gone
                    pass
            step = self.secrets.clean_step(step)
            self.written.append(step)
            self.file.write(json.dumps(_public(step)) + "\n")
            if self.journal is not None:
                self.journal.step(step, self.recording)
        self.file.flush()

    def _overtaken(self, step: dict) -> tuple | None:
        """(seq, action, event_id) of a later action in the step's tab that has already reached the
        recorder and was not caused by the step (as next_action counts them), else None."""
        last = self.tab_last.get(step.get("tab") or "")
        if not last or not isinstance(step.get("seq"), int) or last[0] <= step["seq"]:
            return None
        action = str(last[1] or "")
        if action in ("dialog", "download") or action.startswith("tab-") or last[2] == step["seq"]:
            return None
        return last[0], action, (last[4] if len(last) > 4 else {}).get("event_id")

    BEFORE_ACTION_JS = "m => window.__tlBeforeAction ? window.__tlBeforeAction(m) : false"

    def before_action(self, target=None, mode: str = "unknown") -> None:
        """Just before a recording script's next action: each open page lets what it has not
        snapshotted yet settle and snapshots it (typing, a batch still settling;
        `__tlBeforeAction`; `mode` is where the action goes on `target`, the page it is made
        on), so `drain` takes those steps' pictures before the action's events come. Without
        this, a `fill` followed a few ms later by a `click` had its picture taken once the
        click had begun."""
        for page in list(self.pages.values()):
            try:
                if not page.is_closed():
                    page.evaluate(self.BEFORE_ACTION_JS, mode if page is target else "unknown")
            except Exception:  # noqa: BLE001 - a page navigating or closing: nothing to take
                continue

    INSTALLED_JS = "() => location.protocol === 'about:' || window.__tlInstalledFor === document"

    def _ensure_installed(self) -> None:
        """A popup's first page (about:blank) and the page it loads can share one window, and the
        browser then runs no init script for the loaded page: the listener is added to it here,
        before the next action (the recording loop calls this before each one)."""
        for page in list(self.pages.values()):
            try:
                if page.is_closed():
                    continue
                for frame in page.frames:
                    if not frame.evaluate(self.INSTALLED_JS):
                        for script in self.scripts:
                            frame.evaluate(script)
            except Exception:  # noqa: BLE001 - a page navigating or closing: its init script runs
                continue

    def _picture(self, page, path: Path, stop: Callable | None = None) -> bool:
        """The window, with what shows a typed value masked. The page may change between masking
        and the picture: then it is taken again, and kept only once nothing typed shows. `stop`:
        when it returns something before a try, the picture is given up (attach mode: the next
        action has begun)."""
        self.last_boxes = []
        for _ in range(3):
            if stop is not None and stop() is not None:
                break
            masks = self._masks(page)
            page.screenshot(path=str(path), timeout=5000, mask=masks)
            if not self._unmasked(page):
                self.last_boxes = _boxes(masks)
                return True
        path.unlink(missing_ok=True)
        return False

    def _unmasked(self, page) -> int:
        """What shows text and was not painted over (the page changed after masking): a field
        or editable region with text, or text showing a typed value. A frame that cannot be
        checked counts as unmasked."""
        n = 0
        for frame in page.frames:
            try:
                values = self.secrets.for_origin(_origin(frame.url))
                n += frame.evaluate(MARK_TYPED_JS, [values, True])
            except Exception:  # noqa: BLE001 - a frame that went away
                if not frame.is_detached():
                    n += 1
        return n

    # Painted over in every picture: what the mask script marked, and every field the person has
    # typed into, as the page is at the moment of the picture (Playwright resolves mask locators
    # while it takes it), so typing that begins after masking is painted over too.
    MASK_SELECTOR = "[data-tl-mask], [data-tl-typed]"

    def _masks(self, page) -> list:
        """Locators that cover every field or editable region showing text and what shows a
        typed value (text such as a new list item), so the picture never shows typed text.
        Each frame is given only the values typed in it."""
        out = []
        for frame in page.frames:
            try:
                values = self.secrets.for_origin(_origin(frame.url))
                frame.evaluate(MARK_TYPED_JS, [values, False])
            except Exception:  # noqa: BLE001 - a frame that went away; its fields are still masked
                pass
            if not frame.is_detached():
                out.append(frame.locator(self.MASK_SELECTOR))
        return out

    def _documents(self, page, html: str, base: str) -> None:
        """PDFs the page embeds, fetched with the page's own sign-in (a browser without a PDF
        viewer, such as a headless one, never loads them itself)."""
        import re
        from urllib.parse import urljoin

        for tag in re.findall(r'<(?:embed|object|iframe)\b[^>]*data-tl-document="pdf"[^>]*>', html):
            m = re.search(r'\b(?:src|data)="([^"]+)"', tag)
            if not m:
                continue
            url = urljoin(base, m.group(1).replace("&amp;", "&"))
            if url in self.fetched:
                continue
            try:
                r = page.request.get(url, timeout=15000)
                if r.ok and r.body().startswith(b"%PDF"):
                    self.fetched[url] = r.body()
            except Exception:  # noqa: BLE001 - kept as a marked element without its document
                pass

    def _canvases(self, page, html: str, seq: int) -> str:
        """A picture of each canvas on the page, and the text OCR reads in it, written into the
        snapshot as <tl-canvas-text> after the canvas: drawn screens have no HTML of their own."""
        import re

        from twinlab2_recorder import ocr

        for cid in dict.fromkeys(re.findall(r'data-tl-canvas="(c\d+)"', html)):
            rel = f"screens/{seq:04d}.{cid}.png"
            try:
                (self.out_dir / "screens").mkdir(exist_ok=True)
                page.locator(f'[data-tl-canvas-id="{cid}"]').first.screenshot(
                    path=str(self.out_dir / rel), timeout=3000
                )
            except Exception:  # noqa: BLE001 - a canvas in a frame, or one that went away
                continue
            words = ocr.text(self.out_dir / rel) or ""
            from html import escape

            note = (f'<tl-canvas-text data-tl-for="{cid}" data-tl-image="{rel}" '
                    f'data-tl-ocr="{ocr.available() or "none"}">{escape(words)}</tl-canvas-text>')  # fmt: skip
            html = re.sub(rf'(<canvas[^>]*data-tl-canvas="{cid}"[^>]*>.*?</canvas>)',
                          lambda m, note=note: m.group(1) + note, html, count=1, flags=re.S)  # fmt: skip
        return html

    def on_event(self, source, event: dict) -> None:
        """One action, sent by the page the moment it happened (`__tlEvent`). It is numbered
        here, on arrival: the one sequence of the whole recording, all tabs together. Its
        after-snapshot comes later with its batch (`on_flush`), or never when the page left
        first; neither changes its number or its order."""
        # Playwright passes the binding's source as a dict: {"context", "page", "frame"}.
        page = source.get("page") if isinstance(source, dict) else None
        if not isinstance(event, dict):
            return
        tab = self.tab_of(page)
        key = tab or ""
        info = self.tabs.get(_guid(page)) if page is not None else None
        self._announce(info, event.get("at") if isinstance(event.get("at"), str) else None)
        url = event.get("url") if isinstance(event.get("url"), str) else ""
        if not self.recorded(url, event.get("at") if isinstance(event.get("at"), str) else None):
            return
        if info is not None and url:
            info["first_url"] = info["first_url"] or url
            info["last_url"] = url
        doc = str(event.get("doc") or "")
        if self.open_doc.get(key) not in (None, doc):
            self._close_open(key)  # a new document in the tab: the old one's batch is final
        self.open_doc[key] = doc
        self.seq += 1
        before, used = None, set()
        if event.get("before"):
            before = f"dom/{self.seq:04d}.before.html"
            (self.out_dir / before).write_text(
                self.secrets.redact_html(event["before"], used), encoding="utf-8"
            )
        # ts is when the action happened; the snapshot is taken once the page is quiet.
        step = {"seq": self.seq, "ts": event.get("at") or _now(), "url": url,
                "action": event.get("action"), "selector": event.get("selector"), "dom": None,
                "dom_before": before, "screenshot": None}  # fmt: skip
        if used:
            step["_ph_dom"] = sorted(used)
        if tab:
            step["tab"] = tab
            if info and info.get("opener"):
                step["opener"] = info["opener"]
        if _origin(url):
            step["origin"] = _origin(url)
        if event.get("frame"):
            step["frame"] = str(event["frame"])  # the action happened inside this frame
            step["before_scope"] = "frame"  # its dom_before is that frame's document
        if event.get("typed_shape"):
            step["typed_shape"] = str(event["typed_shape"])[:64]
        if isinstance(event.get("blank"), str) and not event["blank"].strip():
            step["blank"] = event["blank"][:64]  # empty or spaces: not data, kept as it is
        if event.get("action") == "keypress" and isinstance(event.get("key"), str):
            step["key"] = event["key"][:32]
            step["modifiers"] = [m for m in event.get("modifiers") or [] if m in MODIFIERS]
        if event.get("action") == "goto" and isinstance(event.get("nav"), str):
            step["nav"] = event["nav"][:16]
        if event.get("shadow") in ("open", "closed"):
            step["shadow"] = event["shadow"]
        if event.get("surface") == "canvas":
            step["surface"] = "canvas"
        d = event.get("dialog")
        if isinstance(d, dict) and d.get("kind") in ("alert", "confirm", "prompt"):
            step["dialog"] = {"kind": d["kind"],
                              "choice": "accept" if d.get("choice") == "accept" else "dismiss"}  # fmt: skip
            message = dialog_message(d.get("message"), self.secrets)
            if message:
                step["dialog"]["message"] = message
        if isinstance(event.get("files"), list):
            step["files"] = [{"name_shape": str(f.get("name_shape") or "")[:64],
                              "type": str(f.get("type") or "")[:80],
                              "size": str(f.get("size") or "")[:12]}
                             for f in event["files"][:20] if isinstance(f, dict)]  # fmt: skip
        loaded_by = None if event.get("caused") else self._loaded_by(tab, event)
        if event.get("caused") and key in self.batch_cause:
            step["caused_by"] = self.batch_cause[key]
        elif loaded_by is not None:
            # A new document: its page's listener cannot know what loaded it, the tab can.
            step["caused_by"] = self.batch_cause[key] = loaded_by
            self._observe_at_navigation(key, loaded_by)
        elif not event.get("caused"):
            self.batch_cause[key] = self.seq
        self.tab_last[key] = (step["seq"], step["action"], step.get("caused_by"), step["ts"], step)
        if event.get("tid"):
            self.secrets.place(str(event["tid"]), self.seq)
        locs = _locators(event.get("locators"))
        if locs:
            rec = next((c for c in locs if c["kind"] == "record"), None)
            if rec is not None:
                title = event.get("row_title")
                title = title if isinstance(title, str) else ""
                # The record is the one whose title was typed at that step; its binding
                # gives the title on replay. The title itself is never written.
                rec["text_from"] = self.secrets.typed_step(title)
                rec["title_shape"] = _shape(title)[:64]
            step["locators"] = locs
        eid = str(event.get("eid") or f"x{self.seq}")
        step["_eid"] = eid
        self._captured(step)
        self.eid_event[eid] = step["event_id"]
        if isinstance(event.get("replaces"), list):  # a double click: in place of its clicks
            self.replaced.append((self.seq, [str(e) for e in event["replaces"]]))
            if self.journal is not None:
                self.journal.drop([self.eid_event.get(str(e)) for e in event["replaces"]],
                                  "folded into a double click")  # fmt: skip
        self.open[eid] = (step, page)

    def on_flush(self, source, data: dict) -> None:
        """The page settled after a batch (`__tlStep`): its after-snapshot and storage go to
        the batch's last step, and the batch's steps are final."""
        page = source.get("page") if isinstance(source, dict) else None
        if not isinstance(data, dict):
            return
        tab = self.tab_of(page)
        key = tab or ""
        eids = [str(e) for e in data.get("eids") or [] if str(e) in self.open]
        if not eids:
            return
        url = data.get("url") if isinstance(data.get("url"), str) else None
        info = self.tabs.get(_guid(page)) if page is not None else None
        if info is not None and url:
            info["last_url"] = url
        storage = _storage(data.get("storage"))
        if storage is not None:
            storage["origin"] = _origin(url or "")
            seen = json.dumps(storage, sort_keys=True)
            storage = None if self.last_storage.get(key) == seen else storage
            self.last_storage[key] = seen
        steps = [self.open.pop(e) for e in eids]
        left = (
            {str(e) for e in data.get("left") or []} if isinstance(data.get("left"), list) else ()
        )
        for step, _ in steps:
            if step.get("action") == "type" and step.get("_eid") in left:
                step["left"] = True  # the field had lost the focus when the snapshot was taken
            if url:  # the page's address after the batch
                step["url"] = url
                if _origin(url):
                    step["origin"] = _origin(url)
        last = steps[-1][0]
        last["dom"] = f"dom/{last['seq']:04d}.html"
        last["_html"] = data.get("html") or ""  # written by drain(), once frames are filled in
        if storage is not None:
            last["storage"] = storage  # the storage once the page had settled after it
        if data.get("sw") is True and key not in self.sw_seen:
            self.sw_seen.add(key)
            last["service_worker"] = "registered"  # the replay blocks it and says so
        self.waiting.extend(steps)
        self._release(key)

    def _release(self, key: str) -> None:
        """Nothing goes ahead of a step still open in the same tab: steps wait in arrival order
        (a batch's snapshot can arrive after the next batch's events)."""
        self.waiting.sort(key=lambda sp: sp[0]["seq"])

    def _close_open(self, key: str | None = None) -> None:
        """The steps of a batch whose page went before it settled (or whose tab closed, or the
        recording ended): final without an after-snapshot."""
        gone = [e for e, (s, _) in self.open.items() if key is None or (s.get("tab") or "") == key]
        for e in gone:
            self.waiting.append(self.open.pop(e))
        if gone:
            self.waiting.sort(key=lambda sp: sp[0]["seq"])

    def _observe_at_navigation(self, key: str, cause: int) -> None:
        """A click (key press, submit) that loaded a new document is observed at that page's load:
        a snapshot its batch took before the new page came (a page slower than the settle time)
        shows the page it left, so it is dropped, and the step says `after: navigation`."""
        # Applied to the final steps at close(): a step may be on its way to the file right now
        # (its picture being taken), and what it ends as must not depend on that.
        self.nav_observed.append((key, cause, self.seq))
        self.rewrite = True

    def _apply_replaced(self, steps: list[dict]) -> list[dict]:
        """A double click takes the place of the clicks it was made of (sent at once, as every
        action is): their steps and files go, and it is numbered where the first one was."""
        if not self.replaced:
            return steps
        by_eid = {s.get("_eid"): s for s in steps if s.get("_eid")}
        by_seq = {s.get("seq"): s for s in steps}
        gone: set[int] = set()
        moved: dict[int, int] = {}
        for seq, eids in self.replaced:
            olds = [by_eid[e] for e in eids if e in by_eid]
            dbl = by_seq.get(seq)
            if not olds or dbl is None:
                continue
            first = min(o["seq"] for o in olds)
            for o in olds:
                gone.add(id(o))
                for name in ("dom", "dom_before", "screenshot"):
                    if isinstance(o.get(name), str) and o[name]:
                        (self.out_dir / o[name]).unlink(missing_ok=True)
                moved[o["seq"]] = first
            for name in ("dom", "dom_before", "screenshot"):  # its files take the first's names
                rel = dbl.get(name)
                if isinstance(rel, str) and rel:
                    new = rel.replace(f"{seq:04d}.", f"{first:04d}.", 1)
                    if (self.out_dir / rel).exists():
                        (self.out_dir / rel).replace(self.out_dir / new)
                    dbl[name] = new
            moved[seq] = first
            dbl["seq"] = first
            if isinstance(dbl.get("caused_by"), int):
                dbl.pop("caused_by")
        kept = [s for s in steps if id(s) not in gone]
        for s in kept:  # what pointed at the clicks or the double click points at it, renumbered
            if isinstance(s.get("caused_by"), int) and s["caused_by"] in moved:
                s["caused_by"] = moved[s["caused_by"]]
            for cand in s.get("locators") or []:
                if isinstance(cand, dict) and cand.get("text_from") in moved:
                    cand["text_from"] = moved[cand["text_from"]]
        kept.sort(key=lambda s: (not isinstance(s.get("seq"), int), s.get("seq") or 0))
        self.rewrite = True
        return kept

    def _apply_nav_observed(self, steps: list[dict]) -> None:
        for step in steps:
            seq = step.get("seq")
            if not isinstance(seq, int) or str(step.get("action") or "").startswith(
                ("goto", "tab-")
            ):
                continue
            if not any((step.get("tab") or "") == key and cause <= seq < upto
                       for key, cause, upto in self.nav_observed):  # fmt: skip
                continue
            # observed at the page it loaded (caused_by says which): no snapshot of its own
            step.pop("mask_boxes", None)
            for name in ("dom", "screenshot"):
                rel = step.get(name)
                if isinstance(rel, str) and rel:
                    (self.out_dir / rel).unlink(missing_ok=True)
                step[name] = None

    def setup_step(self, page, url: str, reason: str) -> None:
        """A setup step the recorder itself writes (the blank page a new browser starts on): kept
        for provenance, no part of any application (twinlab2_contract.evidence.recording.setup_reason)."""
        tab = self.tab_of(page)
        self.seq += 1
        step = {"seq": self.seq, "ts": _now(), "url": url, "action": "goto", "selector": None,
                "dom": None, "dom_before": None, "screenshot": None, "nav": "navigate"}  # fmt: skip
        # (a setup event by its address: twinlab2_contract.evidence.recording.setup_reason; no field of its own)
        if tab:
            step["tab"] = tab
        self._captured(step)
        self.waiting.append((step, None))

    def close(self) -> None:
        for info in list(self.tabs.values()):  # a tab opened that never did anything
            self._announce(info)
        self._close_open()  # batches never flushed: final, without an after-snapshot
        self.drain(pictures=False)
        self.file.close()
        # The order is the sequence the steps were numbered in on arrival, nothing else.
        steps = sorted(self.written, key=lambda s: (not isinstance(s.get("seq"), int),
                                                    s.get("seq") or 0))  # fmt: skip
        steps = self._apply_replaced(steps)
        reordered = [s.get("seq") for s in steps] != [s.get("seq") for s in self.written]
        self._apply_nav_observed(steps)
        several = len(self.tabs) > 1
        if several:
            steps = self._in_order(steps)
            self._window_facts(steps)
        else:  # one tab: no tab steps (a single page's recording reads as it always did)
            steps = [s for s in steps if not str(s.get("action") or "").startswith("tab-")]
        renumbered = self._renumber(steps)
        if link_events(steps):
            self.rewrite = True
        if mark_unsettled_pictures(steps, self.out_dir if self.drop_late_pictures else None):
            self.rewrite = True
        for n, st in enumerate(steps):  # pictures dropped while recording: the next action, renumbered
            if st.pop("_dropped_for", None) is not None:
                nxt = next_action(steps, n)
                if nxt is not None:
                    st["picture_note"] = (f"no picture: step {nxt.get('seq')} ({nxt.get('action')}) began "
                                          "before it could be taken, so it would show what that step did")  # fmt: skip
                self.rewrite = True
        if self.secrets:
            # A value typed late may have been on the page before it was typed: every snapshot
            # once more with every value typed (each step then names the placeholders they use).
            self._redact_snapshots(steps)
        if (several or renumbered or reordered or self.rewrite or len(steps) != len(self.written)
                or self.secrets):  # fmt: skip
            # A value typed late may have been on the page before it was typed: go over
            # everything once more with every value typed.
            steps = [self.secrets.clean_step(s) for s in steps]
            (self.out_dir / "steps.jsonl").write_text(
                "".join(json.dumps(_public(s)) + "\n" for s in steps), encoding="utf-8"
            )
        self.written = steps
        if several:
            rows = []
            for info in self.tabs.values():
                row = {k: info.get(k) for k in ("tab", "opener", "har_page", "opened", "closed")}
                row["first_url"] = self.secrets.redact_url(info.get("first_url") or "")
                rows.append(row)
            (self.out_dir / "tabs.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")

    def _redact_snapshots(self, steps: list[dict]) -> None:
        owner = {s[k]: s for s in steps for k in ("dom", "dom_before") if isinstance(s.get(k), str)}
        for path in (self.out_dir / "dom").glob("*.html"):
            text = path.read_text(encoding="utf-8")
            used: set[str] = set()
            clean = self.secrets.redact_html(text, used)
            step = owner.get(f"dom/{path.name}")
            if used and step is not None:
                step["_ph_dom"] = sorted(used | set(step.get("_ph_dom") or ()))
            if clean != text:
                path.write_text(clean, encoding="utf-8")

    def _in_order(self, steps: list[dict]) -> list[dict]:
        """All tabs on one sequence, in the order the recorder numbered the steps as they arrived
        (never re-sorted by timestamps: a page's clock and the recorder's are not one clock).
        Where the next action is in another tab than the one before, a `tab-switch` step says
        so."""
        merged = list(steps)
        out: list[dict] = []
        current, seen = None, set()
        for s in merged:
            action, tab = str(s.get("action") or ""), s.get("tab")
            if action.startswith("tab-"):
                if action == "tab-open":
                    seen.add(tab)  # the new tab has the focus: no switch to it
                    current = tab
                out.append(s)
                continue
            if current is not None and tab and tab != current and tab in seen:
                out.append({"seq": None, "ts": s.get("ts"), "url": s.get("url"),
                            "action": "tab-switch", "selector": None, "dom": None,
                            "dom_before": None, "screenshot": None, "tab": tab,
                            "from_tab": current, **({"origin": s["origin"]} if s.get("origin") else {})})  # fmt: skip
                self._captured_late(out[-1], s)
            if tab:
                seen.add(tab)
                current = tab
            out.append(s)
        return out

    def _captured_late(self, step: dict, at: dict) -> None:
        """A step close() makes (a tab switch): an id of its own, captured when the step it
        stands before was."""
        from twinlab2_recorder.workstation import journal as capture

        capture.stamp(step, "web", at.get("captured_at"))

    def _renumber(self, steps: list[dict]) -> bool:
        """Number the steps 1, 2, 3 in their final order, renaming their files and changing every
        reference to a step number (`caused_by`, a record's `text_from`). True if any changed."""
        import re

        mapping: dict[int, int] = {}
        for n, s in enumerate(steps, 1):
            if isinstance(s.get("seq"), int):
                mapping[s["seq"]] = n
        if all(old == new for old, new in mapping.items()) and all(
            isinstance(s.get("seq"), int) for s in steps
        ):
            return False
        self.secrets.renumbered(mapping)
        moves: list[tuple[Path, Path]] = []
        for n, s in enumerate(steps, 1):
            old = s.get("seq")
            s["seq"] = n
            if isinstance(old, int) and old != n:
                for key in ("dom", "dom_before", "screenshot"):
                    rel = s.get(key)
                    if isinstance(rel, str) and rel:
                        new = re.sub(rf"(^|/){old:04d}\.", rf"\g<1>{n:04d}.", rel)
                        moves.append((self.out_dir / rel, self.out_dir / new))
                        s[key] = new
                for pic in (self.out_dir / "screens").glob(f"{old:04d}.c*.png"):
                    moves.append((pic, pic.with_name(f"{n:04d}." + pic.name.split(".", 1)[1])))
                if isinstance(s.get("dom"), str):
                    s["_canvas_from"] = old
            if isinstance(s.get("caused_by"), int):
                if s["caused_by"] in mapping:
                    s["caused_by"] = mapping[s["caused_by"]]
                else:
                    s.pop("caused_by")
            for cand in s.get("locators") or []:
                if isinstance(cand.get("text_from"), int):
                    cand["text_from"] = mapping.get(cand["text_from"])
        staged = []
        for src, dst in moves:  # in two moves, so no file is overwritten by another
            if src.exists():
                tmp = src.with_name(src.name + ".tlmove")
                src.replace(tmp)
                staged.append((tmp, dst))
        for tmp, dst in staged:
            tmp.replace(dst)
        for s in steps:
            old = s.pop("_canvas_from", None)
            path = self.out_dir / s["dom"] if old is not None else None
            if path is not None and path.exists():
                text = path.read_text(encoding="utf-8")
                fixed = text.replace(f"screens/{old:04d}.", f"screens/{s['seq']:04d}.")
                if fixed != text:
                    path.write_text(fixed, encoding="utf-8")
        return True


class _Har:
    """A HAR 1.2 log built from network events, for a browser Twin Lab did not start (an
    existing context cannot be told to record a HAR). Bodies are embedded as text or base64."""

    def __init__(self, origins: set[str] | None = None, journal=None,
                 exclude: Callable[[str], str | None] | None = None) -> None:  # fmt: skip
        self.entries: list[dict] = []
        # The PC's exclude list: a request to an excluded address, or made by an excluded page,
        # is not kept (not in the HAR, not in the network journal).
        self.exclude = exclude
        # A workstation session's network journal: each entry as it is kept, with the values
        # typed so far taken out (`steps` holds them), linked to the last web event before it.
        self.journal = journal
        self.steps: _Steps | None = None
        self.pages: list[dict] = []  # one HAR page per tab, as Playwright's own HAR has
        # In a workstation session: only requests made by pages of a target origin are kept
        # (their CDN and API calls included); a request with no page (a service worker) is
        # kept when its own origin is a target.
        self.origins = {o.lower() for o in origins} if origins is not None else None

    SECRET_HEADER = ("authorization", "cookie", "token", "secret", "api-key", "apikey", "session",
                     "csrf", "xsrf", "auth", "password", "signature")  # fmt: skip

    def _headers(self, headers: dict) -> list[dict]:
        """Headers as HAR rows; in a workstation session a header that may carry a credential
        keeps its name only."""
        rows = []
        for k, v in headers.items():
            if self.origins is not None and any(w in k.lower() for w in self.SECRET_HEADER):
                v = "[removed]"
            rows.append({"name": k, "value": v})
        return rows

    def _wanted(self, request) -> bool:
        try:
            page_url = request.frame.page.url
        except Exception:  # noqa: BLE001
            page_url = request.url
        if getattr(self, "exclude", None) is not None:
            try:
                own = request.url
            except Exception:  # noqa: BLE001
                own = ""
            if _excluded(self.exclude, own) or _excluded(self.exclude, page_url):
                return False
        if self.origins is None:
            return True
        origin = _origin(page_url or "").lower()
        return bool(origin) and origin in self.origins

    def on_finished(self, request) -> None:
        from datetime import UTC, datetime

        if not self._wanted(request):
            return

        try:
            response = request.response()
        except Exception:  # noqa: BLE001
            response = None
        try:  # the tab that made it (a service worker's request has no page)
            pageref = _guid(request.frame.page)
        except Exception:  # noqa: BLE001
            pageref = None
        timing = request.timing or {}
        total = max(0.0, float(timing.get("responseEnd", 0) or 0))
        start_ms = timing.get("startTime") or 0
        started = (
            datetime.fromtimestamp(start_ms / 1000, UTC) if start_ms > 0 else datetime.now(UTC)
        )
        entry = {
            "startedDateTime": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "time": total,
            "request": {
                "method": request.method,
                "url": request.url,
                "httpVersion": "HTTP/1.1",
                "headers": self._headers(request.headers),
                "queryString": [],
                "cookies": [],
                "headersSize": -1,
                "bodySize": len(request.post_data or ""),
                **(
                    {
                        "postData": {
                            "mimeType": request.headers.get("content-type", ""),
                            "text": request.post_data,
                        }
                    }  # fmt: skip
                    if request.post_data
                    else {}
                ),
            },
            "response": {
                "status": response.status if response else 0,
                "statusText": response.status_text if response else "",
                "httpVersion": "HTTP/1.1",
                "headers": self._headers(response.headers if response else {}),
                "cookies": [],
                "content": self._content(response),
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": total, "receive": 0},
            "_resourceType": request.resource_type,
        }
        if pageref:
            entry["pageref"] = pageref
        self.entries.append(entry)
        if self.journal is not None:
            steps = self.steps
            clean = steps.secrets.scrub(entry) if steps is not None else entry
            self.journal.har(clean, steps.recording if steps is not None else None,
                             steps.last_event_id if steps is not None else None)  # fmt: skip

    MAX_BODY = 2_000_000  # in a workstation session, a body larger than this keeps its size only

    def _content(self, response) -> dict:
        import base64

        if response is None:
            return {"size": 0, "mimeType": ""}
        mime = response.headers.get("content-type", "")
        try:
            body = response.body()
        except Exception:  # noqa: BLE001 - redirects and aborted loads have no body
            return {"size": 0, "mimeType": mime}
        if self.origins is not None and len(body) > self.MAX_BODY:
            return {"size": len(body), "mimeType": mime, "comment": "body not kept: larger than "
                    f"{self.MAX_BODY} bytes"}  # fmt: skip
        try:
            return {"size": len(body), "mimeType": mime, "text": body.decode("utf-8")}
        except UnicodeDecodeError:
            return {"size": len(body), "mimeType": mime, "text": base64.b64encode(body).decode(),
                    "encoding": "base64"}  # fmt: skip

    def write(self, path: Path) -> None:
        doc = {"log": {"version": "1.2", "creator": {"name": "twinlab-attach", "version": "1"},
                       "pages": self.pages, "entries": self.entries}}  # fmt: skip
        path.write_text(json.dumps(doc), encoding="utf-8")


def _init_scripts(settle_ms: int) -> list[str]:
    """The shadow hook first (before the page's own code makes any shadow root), then the
    listener, with a fresh token so a page's own messages are never taken for a frame's."""
    import secrets

    from twinlab2_contract.evidence.dom import SERIALIZE_FN, SHADOW_HOOK_JS
    from twinlab2_contract.evidence.locate import ROW_TITLE_FN

    listener = (LISTENER_JS.replace("__SETTLE__", str(int(settle_ms)))
                .replace("__TOKEN__", secrets.token_hex(8))
                .replace("__ROW_TITLE__", ROW_TITLE_FN)
                .replace("__SERIALIZE__", SERIALIZE_FN))  # fmt: skip
    return [SHADOW_HOOK_JS, listener]


class _Draining:
    """The page handed to a recording script: before each call, the steps recorded so far are
    finished (pictures, frames, canvases), so each step's picture shows the page as it settled,
    not the end of the script."""

    def __init__(self, page, steps: _Steps) -> None:
        self._page, self._steps = page, steps

    def wrap(self, page) -> _Draining:
        """Another tab (a popup the script waited for), handed out the same way."""
        return page if isinstance(page, _Draining) else _Draining(page, self._steps)

    # Calls that send the page input (or name the element the next input goes to): before them,
    # what the page has not snapshotted yet is snapshotted and pictured (`__tlBeforeAction`).
    INPUT = frozenset({
        "click", "dblclick", "fill", "type", "press", "press_sequentially", "check", "uncheck",
        "set_checked", "select_option", "set_input_files", "tap", "drag_and_drop", "hover",
        "focus", "dispatch_event", "locator", "get_by_role", "get_by_text", "get_by_label",
        "get_by_placeholder", "get_by_alt_text", "get_by_title", "get_by_test_id",
        "frame_locator",
    })  # fmt: skip

    def __getattr__(self, name):
        attr = getattr(self._page, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if name in self.INPUT:
                self._steps.before_action(self._page, self._mode(name, args))
            self._steps.drain()
            return attr(*args, **kwargs)

        return call

    # Actions on an element that move the focus to it (a click presses on it, a fill focuses it).
    FOCUSING = frozenset({
        "click", "dblclick", "fill", "type", "press", "press_sequentially", "check", "uncheck",
        "set_checked", "select_option", "set_input_files", "tap", "focus", "drag_and_drop",
    })  # fmt: skip

    def _mode(self, name: str, args) -> str:
        """Where the action goes, for `__tlBeforeAction`: `leave` (an element other than the
        focused one), `stay` (the focused one) or `unknown` (a locator made for later)."""
        if name not in self.FOCUSING or not args or not isinstance(args[0], str):
            return "unknown"
        try:
            handle = self._page.query_selector(args[0])  # no waiting: as the page is now
            if handle is None:
                return "leave"
            same = handle.evaluate("el => el === document.activeElement || el.contains("
                                   "document.activeElement)")  # fmt: skip
            return "stay" if same else "leave"
        except Exception:  # noqa: BLE001 - a selector only the action understands
            return "unknown"


PDF_TYPES = ("application/pdf", "application/x-pdf")


def save_documents(out_dir: Path, fetched: dict[str, bytes] | None = None) -> list[dict]:
    """PDFs the application showed, from the HAR (and any fetched while recording): each is kept
    as docs/NN.pdf with its text in docs/NN.txt, and docs/documents.json lists, per document, its
    address, page count and form field names (never the values typed into them)."""
    import base64

    found: dict[str, bytes] = dict(fetched or {})
    har_file = out_dir / "recording.har"
    try:
        entries = json.loads(har_file.read_text(encoding="utf-8"))["log"]["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        entries = []
    for e in entries:
        content = (e.get("response") or {}).get("content") or {}
        url = (e.get("request") or {}).get("url") or ""
        mime = (content.get("mimeType") or "").split(";")[0].strip().lower()
        if mime not in PDF_TYPES and not url.lower().split("?")[0].endswith(".pdf"):
            continue
        text = content.get("text")
        if not text:
            continue
        body = (
            base64.b64decode(text)
            if content.get("encoding") == "base64"
            else text.encode("latin-1", "replace")
        )
        if body.startswith(b"%PDF"):
            found.setdefault(url, body)
    out = []
    for n, (url, body) in enumerate(found.items(), start=1):
        folder = out_dir / "docs"
        folder.mkdir(exist_ok=True)
        (folder / f"{n:02d}.pdf").write_bytes(body)
        info = {
            "url": url,
            "file": f"docs/{n:02d}.pdf",
            "pages": None,
            "fields": [],
            "text_chars": 0,
        }
        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(body))
            info["pages"] = len(reader.pages)
            words = "\n".join((p.extract_text() or "") for p in reader.pages)
            (folder / f"{n:02d}.txt").write_text(words, encoding="utf-8")
            info["text_chars"] = len(words)
            fields = reader.get_fields() or {}
            info["fields"] = [{"name": k, "type": str(v.get("/FT", "")).lstrip("/")}
                              for k, v in fields.items()]  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - an unreadable PDF is kept, and said so
            info["problem"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out.append(info)
    if out:
        (out_dir / "docs" / "documents.json").write_text(
            json.dumps(out, indent=1), encoding="utf-8"
        )
    return out


def attach(
    cdp_url: str,
    out_dir: Path,
    *,
    settle_ms: int = 400,
    max_seconds: float = 1800,
    screenshots: bool = True,
    stop: Callable[[], bool] | None = None,
    origins: set[str] | None = None,
    outside: Callable | None = None,
    keep_open: bool = False,
    on_ready: Callable[[], None] | None = None,
    journal=None,
    network_journal=None,
    exclude: Callable[[str], str | None] | None = None,
) -> Path:
    """Record a browser that is already running (a robot's Chrome or Edge started with
    `--remote-debugging-port=9222`): pass its address, for example `http://127.0.0.1:9222`.

    Every page of its first context is recorded until the browser closes, `stop()` returns
    true, `max_seconds` pass, or Ctrl+C. Nothing is changed in the browser except the listener
    added to its pages; it is left running.

    `origins` (a workstation session, twinlab2_recorder.workstation): only pages of these origins are
    recorded; an action elsewhere is not written, `outside(origin, ts)` is told of it, and the
    HAR keeps only the requests that pages of these origins made. `journal` and
    `network_journal` (twinlab2_recorder.workstation.journal.Journal) receive each step and each HAR entry
    as it is captured, so a session that dies keeps them. `exclude(url)` (the PC's exclude list,
    twinlab2_recorder.workstation.exclude): an address it names is neither recorded nor in the HAR.
    """
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir)
    (out_dir / "dom").mkdir(parents=True, exist_ok=True)
    steps = _Steps(out_dir, screenshots, origins=origins, outside=outside, journal=journal,
                   exclude=exclude)  # fmt: skip
    steps.drop_late_pictures = True  # nothing holds the robot's next action for the picture
    har = _Har(origins, journal=network_journal, exclude=exclude)
    har.steps = steps
    scripts = steps.scripts = _init_scripts(settle_ms)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError(f"the browser at {cdp_url} has no open window")
            context = browser.contexts[0]
            # The tabs already open are t1, t2...; each tab or popup opened later is recorded
            # from its first page, with a tab-open step.
            for page in context.pages:
                steps.on_page(page, quiet=True)
            context.on("page", steps.on_page)
            try:  # the robot's own window size, as it is (not set by Twin Lab)
                size = context.pages[0].viewport_size if context.pages else None
            except Exception:  # noqa: BLE001
                size = None
            write_meta(out_dir, browser, size, "attach", settle_ms)
            context.expose_binding("__tlEvent", steps.on_event)
            context.expose_binding("__tlStep", steps.on_flush)
            context.expose_binding("__tlSecret", steps.on_secret)
            context.expose_binding("__tlWindow", steps.on_window)
            for s in scripts:
                context.add_init_script(s)
            context.on("requestfinished", har.on_finished)
            context.on("requestfailed", har.on_finished)
            # Pages already open get the listener now, in every frame. Closed shadow roots they
            # made before this moment cannot be reached; new ones are.
            for page in context.pages:
                for frame in page.frames:
                    for s in scripts:
                        try:
                            frame.evaluate(s)
                        except Exception:  # noqa: BLE001 - navigating: the init script runs
                            pass
            import time

            if on_ready is not None:  # every listener is in place: actions from now are recorded
                on_ready()
            started = time.monotonic()
            try:
                while browser.is_connected() and time.monotonic() - started < max_seconds:
                    if stop is not None and stop():
                        break
                    pages = [p for p in context.pages if not p.is_closed()]
                    if not pages:
                        if not keep_open:
                            break
                        try:  # a session waits for the next tab while the browser lives
                            context.wait_for_event("page", timeout=500)
                        except Exception:  # noqa: BLE001 - no tab yet, or the browser closed
                            pass
                        continue
                    steps.drain()
                    # The robot is not held between its actions: wait in short slices, so a
                    # step's picture is taken as soon as its snapshot arrives (a picture taken
                    # after the next action began is dropped at close: drop_late_pictures).
                    try:
                        for _ in range(20):
                            pages[-1].wait_for_timeout(25)
                            if steps.waiting:
                                break
                    except Exception:  # noqa: BLE001 - that tab closed (a robot ends its scope)
                        continue
            except KeyboardInterrupt:
                pass
            for page in context.pages:
                try:
                    page.evaluate("() => window.__tlFlush && window.__tlFlush()")
                except Exception:  # noqa: BLE001
                    pass
            steps.drain()
            steps.closing = True
            # Disconnect only: the robot's browser keeps running.
    finally:
        steps.close()
        har.entries = steps.secrets.scrub(har.entries)
        har.pages = [{"startedDateTime": t["opened"], "id": t["har_page"], "title": t["tab"],
                      "pageTimings": {}} for t in steps.tabs.values()]  # fmt: skip
        har.write(out_dir / "recording.har")
        save_documents(out_dir, steps.fetched)
    return out_dir


def record(
    url: str,
    out_dir: Path,
    script: Callable | None = None,
    headless: bool = False,
    *,
    settle_ms: int = 400,
    max_seconds: float = 1800,
    screenshots: bool = True,
    storage_state: dict | str | Path | None = None,
    browser: str = "chromium",
) -> Path:
    """Open `url` in Chromium (or `browser`: firefox, webkit) and record until the page is closed (or `script(page)` returns).
    A window nobody closes is closed after `max_seconds`; what was recorded until then is kept.

    With `script`, the callback drives the page (for tests and scripted evidence runs); otherwise a
    person clicks through the flow in a headed browser and closes the window to finish. Returns
    the recording folder. `storage_state` (Playwright's format: cookies and localStorage per
    origin) is the browser data the recording starts from, such as a declared baseline.
    """
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir)
    (out_dir / "dom").mkdir(parents=True, exist_ok=True)
    steps = _Steps(out_dir, screenshots)
    try:
        with sync_playwright() as pw:
            browser = getattr(pw, browser).launch(headless=headless)
            context = browser.new_context(
                accept_downloads=True,
                record_har_path=str(out_dir / "recording.har"),
                record_har_content="embed",
                record_har_mode="full",
                storage_state=str(storage_state)
                if isinstance(storage_state, Path)
                else storage_state,
                viewport=VIEWPORT,
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            write_meta(out_dir, browser, VIEWPORT, "record", settle_ms)
            # Every tab and popup the flow opens is recorded, on the same sequence.
            context.on("page", steps.on_page)
            context.expose_binding("__tlEvent", steps.on_event)
            context.expose_binding("__tlStep", steps.on_flush)
            context.expose_binding("__tlSecret", steps.on_secret)
            context.expose_binding("__tlWindow", steps.on_window)
            steps.scripts = _init_scripts(settle_ms)
            for s in steps.scripts:
                context.add_init_script(s)
            page = context.new_page()
            steps.setup_step(page, "about:blank", "blank page")  # where every browser starts
            page.goto(url)

            def open_pages():
                return [p for p in context.pages if not p.is_closed()]

            def flush_all():
                for p in open_pages():
                    try:
                        p.evaluate("() => window.__tlFlush && window.__tlFlush()")
                    except Exception:  # noqa: BLE001 - a page navigating or closing
                        pass

            if script is not None:
                # Each call the script makes first finishes the steps before it, so pictures show
                # each step as it settled, not the end of the script.
                script(_Draining(page, steps))
                live = open_pages()
                if live:
                    live[0].wait_for_timeout(settle_ms + 200)
                flush_all()
                steps.drain()
            else:
                import time

                started = time.monotonic()
                while open_pages():  # until the person closed the last window
                    steps.drain()  # the picture of each step, at most half a second late
                    if time.monotonic() - started > max_seconds:
                        flush_all()
                        steps.drain()
                        print(
                            f"No one closed the window within {max_seconds / 60:.0f} min; stopped."
                        )
                        break
                    try:
                        open_pages()[0].wait_for_timeout(300)
                    except Exception:  # noqa: BLE001 - the window was closed while waiting
                        continue
            steps.closing = True
            context.close()  # writes the HAR
            browser.close()
    finally:
        steps.close()
        steps.secrets.scrub_har(out_dir / "recording.har")  # no typed value stays in the HAR
    save_documents(out_dir, steps.fetched)
    return out_dir
