# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Reading a recording: `evidence/<app>/<recording-id>/` with `recording.har`, `dom/<seq>.html`
(outerHTML after each step) and `steps.jsonl`.

steps.jsonl lines: `{"seq", "ts", "url", "action": "goto|click|type|select|submit", "selector",
"dom": "dom/0003.html"}`. `ts` is when the action started and `url` is the page's URL after it.
The harness bot also writes `value` for typed and selected values that are not Private; other
writers may omit it, in which case the value is read from the after-snapshot.

The recorder also writes (all optional, so older recordings load unchanged):

- actions `keypress` (with `key` and `modifiers`: only non-text keys and chords, never typed
  characters), `dblclick`, `check` and `uncheck`;
- `locators`: ranked candidates for the target, each with the number of elements it matched when
  recorded (see twinlab2_contract.evidence.locate.resolve);
- `blank`: a typed value that was empty or only spaces (the value itself, as it is not data);
- `nav` on a `goto` (`navigate`, `reload`, `back_forward`, `push`, `replace`, `pop`) and
  `caused_by` (the seq of the click or key press that caused a submit or navigation);
- `tab`, `opener` and `origin`: the tab a step happened in, the tab that opened it, and the
  top-level page's origin; a recording that used several tabs also has the actions `tab-open`,
  `tab-switch` and `tab-close`, and `storage` (browser storage keys and masked shapes, read by
  Twin Lab's har).

Only request shapes are taken from the HAR (method, URL path, status, timing). Headers, cookies
and bodies are never read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ASSET_EXT = re.compile(
    r"\.(js|mjs|css|map|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|otf|eot|mp4|webm)$", re.I
)
ASSET_TYPES = {"script", "stylesheet", "image", "font", "media", "manifest", "texttrack"}


@dataclass
class RecStep:
    seq: int
    ts: datetime | None
    url: str | None
    action: str
    selector: str | None
    dom: str | None
    value: str | None = None
    typed_shape: str | None = None  # the recorder's shape of what was typed (never the value)
    dom_before: str | None = None  # the page as the action met it (the recorder writes it)
    before_scope: str | None = None  # "frame": dom_before is only the frame's document
    frame: str | None = None  # the frame the action happened in (`iframe#x >>> iframe#y`)
    key: str | None = None  # keypress: `Enter`, `Escape`, `a` (with modifiers: a chord)
    modifiers: list[str] = field(default_factory=list)  # keypress: Control, Alt, Meta, Shift
    locators: list[dict] = field(default_factory=list)  # ranked target candidates (locate.py)
    blank: str | None = None  # type: the value was empty or spaces only (kept, it is no data)
    nav: str | None = None  # goto: how the page was reached
    caused_by: int | None = None  # submit/goto: the step whose action caused it
    tab: str | None = None  # the tab it happened in (t1, t2...)
    opener: str | None = None  # the tab that opened that tab (a popup, target=_blank)
    origin: str | None = None  # of the top-level page
    # Added for the multi-app replay and R12; all optional, so older recordings load unchanged.
    setup: str | None = None  # a setup event (the blank page a browser starts on): why
    after: str | None = None  # "navigation": observed at the page load it caused, not here
    caused_by_outside: dict | None = None  # {app, step}: the cause went to another application
    # A window opened across applications, in a part of a split recording (twinlab2.replay.extracts):
    opens_outside: dict | None = None  # {app, step, tab, url}: this action opened its window
    opened_by_outside: dict | None = None  # {app, step, tab, element}: who opened this window
    option_index: int | None = None  # select: the position of the option chosen
    left: bool = False  # type: the field had lost the focus when the snapshot was taken
    # dialog: {kind: confirm|alert|prompt, choice: accept|dismiss, message: masked text}
    dialog: dict | None = None
    files: list[dict] = field(default_factory=list)  # upload: name shape, type, size class
    download: dict | None = None  # download: {name_shape, ext}
    shadow: str | None = None  # open | closed: the target is inside a shadow root
    surface: str | None = None  # canvas: an action no DOM element describes
    service_worker: str | None = None  # the page registered a service worker
    # an id or name that was a whole typed value, recorded as its placeholder (twinlab2_contract.evidence.typedvalues):
    # [{placeholder, typed_at}], never the value
    typed_placeholders: list[dict] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        return urlsplit(self.url).path if self.url else None


@dataclass
class HarEntry:
    method: str
    url: str
    status: int | None
    start: datetime | None
    end: datetime | None
    resource_type: str | None = None
    mime: str | None = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def has_query(self) -> bool:
        return bool(urlsplit(self.url).query)

    def is_asset(self) -> bool:
        return is_asset(self.path, self.resource_type, self.mime)

    def key(self) -> str:
        return request_key(self.method, self.url)


@dataclass
class Recording:
    id: str
    path: Path
    app: str | None
    steps: list[RecStep] = field(default_factory=list)
    entries: list[HarEntry] = field(default_factory=list)
    bad_lines: int = 0
    # What is damaged or missing (a recorder that crashed, timed out or was restarted): lines
    # dropped, snapshots a step names that are not there, repeated step numbers, an unreadable
    # HAR. The recording still loads; whoever shows a result from it shows these.
    problems: list[str] = field(default_factory=list)

    def dom(self, step: RecStep | None, before: bool = False) -> str | None:
        rel = (step.dom_before if before else step.dom) if step is not None else None
        if not rel:
            return None
        target = (self.path / rel).resolve()
        if self.path.resolve() not in target.parents or not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def before(self, step: RecStep) -> RecStep | None:
        idx = self.steps.index(step)
        return self.steps[idx - 1] if idx > 0 else None

    def requests_between(self, start: datetime | None, end: datetime | None) -> list[HarEntry]:
        if start is None:
            return []
        return [
            e
            for e in self.entries
            if e.start is not None and e.start >= start and (end is None or e.start < end)
        ]


def _ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_asset(path: str, resource_type: str | None = None, mime: str | None = None) -> bool:
    if resource_type and resource_type.lower() in ASSET_TYPES:
        return True
    if mime and re.match(r"^(image|font|text/css|(application|text)/javascript)", mime):
        return True
    return bool(ASSET_EXT.search(path or ""))


def path_pattern(path: str) -> str:
    """`/api/cases/C-1003/notes` -> `/api/cases/*/notes`: segments holding a digit are ids."""
    segs = [("*" if re.search(r"\d", s) else s) for s in (path or "/").split("/")]
    return "/".join(segs) or "/"


def request_key(method: str, url: str) -> str:
    """`GET /api/cases/search*` (a query was present) or `PUT /api/cases/*`: method and pattern."""
    parts = urlsplit(url or "/")
    star = "*" if parts.query else ""
    return f"{(method or 'GET').upper()} {path_pattern(parts.path or '/')}{star}"


def read_har(path: Path, problems: list[str] | None = None) -> list[HarEntry]:
    """The requests of a HAR file. One that cannot be read (empty, cut off, not a HAR) gives no
    requests and, when `problems` is given, a line saying why."""
    path = Path(path)
    problems = problems if problems is not None else []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        problems.append(f"{path.name} could not be read ({exc}): no requests were read")
        return []
    if not text.strip():
        problems.append(f"{path.name} is empty: no requests were read")
        return []
    try:
        data = json.loads(text)
        raw = data["log"]["entries"]
        if not isinstance(raw, list):
            raise TypeError("'entries' is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        why = str(exc) if isinstance(exc, ValueError) else "no log.entries list"
        problems.append(
            f"{path.name} is not a complete HAR file ({why}): no requests were read; "
            "the recorder may have stopped before it was written"
        )
        return []
    entries = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        req = e.get("request") or {}
        res = e.get("response") or {}
        start = _ts(e.get("startedDateTime"))
        total = e.get("time")
        end = start + timedelta(milliseconds=float(total)) if start and total is not None else None
        entries.append(
            HarEntry(
                method=str(req.get("method") or "GET"),
                url=str(req.get("url") or ""),
                status=res.get("status") if isinstance(res.get("status"), int) else None,
                start=start,
                end=end,
                resource_type=e.get("_resourceType"),
                mime=(res.get("content") or {}).get("mimeType"),
            )
        )
    entries.sort(key=lambda x: x.start or datetime.min.replace(tzinfo=timezone.utc))
    return entries


def load(path: Path) -> Recording:
    """Read a recording folder. A damaged one (a recorder that crashed, timed out or was
    restarted) still loads: what could be read is kept and `problems` says what was not."""
    path = Path(path)
    rec = Recording(id=path.name, path=path, app=path.parent.name or None)
    steps_file = path / "steps.jsonl"
    named: list[tuple[int, str]] = []  # (seq, file) of each snapshot or picture a step names
    if steps_file.exists():
        text = steps_file.read_text(encoding="utf-8-sig", errors="replace")
        lines = [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
        for n, line in lines:
            try:
                d = json.loads(line)
                rec.steps.append(
                    RecStep(
                        seq=int(d.get("seq") or len(rec.steps) + 1),
                        ts=_ts(d.get("ts")),
                        url=d.get("url"),
                        action=str(d.get("action") or ""),
                        selector=_mask_ids(d.get("selector")),
                        dom=d.get("dom"),
                        value=d.get("value") if isinstance(d.get("value"), str) else None,
                        typed_shape=(
                            d.get("typed_shape") if isinstance(d.get("typed_shape"), str) else None
                        ),
                        dom_before=(
                            d.get("dom_before") if isinstance(d.get("dom_before"), str) else None
                        ),
                        before_scope=d.get("before_scope")
                        if isinstance(d.get("before_scope"), str)
                        else None,
                        **_extra(d),
                    )
                )
                named += [
                    (rec.steps[-1].seq, d[key])
                    for key in ("dom", "dom_before", "screenshot")
                    if isinstance(d.get(key), str) and d[key]
                ]
            except (ValueError, TypeError, AttributeError) as exc:
                rec.bad_lines += 1
                rec.problems.append(bad_line(n, n == lines[-1][0], exc))
        if not rec.steps:
            rec.problems.append("steps.jsonl has no step: the recording is empty")
    else:
        rec.problems.append("no steps.jsonl: the recording has no steps")
    rec.problems += seq_problems([s.seq for s in rec.steps])
    if len({s.seq for s in rec.steps}) == len(rec.steps):
        rec.steps.sort(key=lambda s: s.seq)
    # else: a number is used twice (the recorder was restarted and appended). Sorting would
    # interleave the two runs, so the steps stay in the order they were written.
    root = path.resolve()
    for seq, rel in named:
        target = (path / rel).resolve()
        if root not in target.parents or not target.is_file():
            rec.problems.append(f"step {seq}: {rel} not found; the step has no such snapshot")
    har = path / "recording.har"
    if har.exists():
        rec.entries = read_har(har, rec.problems)
    else:
        rec.problems.append("no recording.har: no requests were read")
    return rec


def bad_line(n: int, last: bool, exc: Exception | None = None) -> str:
    """The problem line for a steps.jsonl line that is not a step. The last line of a file whose
    recorder was stopped while writing is cut off in the middle; that is said as such."""
    if last and isinstance(exc, json.JSONDecodeError):
        return (
            f"steps.jsonl line {n} (the last) is cut off; dropped. "
            "The recorder may have stopped while writing it"
        )
    return f"steps.jsonl line {n} is not a step; skipped"


def seq_problems(seqs: list[int]) -> list[str]:
    """Step numbers used twice, or not in rising order, in the order the steps were written."""
    count: dict[int, int] = {}
    for seq in seqs:
        count[seq] = count.get(seq, 0) + 1
    twice = sorted(seq for seq, n in count.items() if n > 1)
    if twice:
        return [
            f"steps.jsonl: step number(s) {', '.join(map(str, twice[:10]))} used more than once; "
            "the recorder may have been restarted and appended. The steps are kept in the order "
            "written; a reference to one of these numbers is ambiguous"
        ]
    if seqs != sorted(seqs):
        return ["steps.jsonl: step numbers are not in rising order; check the order of the steps"]
    return []


def _extra(d: dict) -> dict:
    """The optional fields newer recorders write; anything malformed is left out."""
    out: dict = {}
    for name in ("frame", "key", "nav", "blank", "tab", "opener", "origin", "after", "shadow",
                 "surface", "service_worker"):  # fmt: skip
        if isinstance(d.get(name), str):
            out[name] = d[name]
    for name in ("caused_by_outside", "opens_outside", "opened_by_outside", "dialog", "download"):
        if isinstance(d.get(name), dict):
            out[name] = dict(d[name])
    if isinstance(d.get("files"), list):
        out["files"] = [dict(f) for f in d["files"] if isinstance(f, dict)]
    if isinstance(d.get("option_index"), int) and not isinstance(d.get("option_index"), bool):
        out["option_index"] = d["option_index"]
    if d.get("left") is True:
        out["left"] = True
    reason = setup_reason(d)
    if reason:
        out["setup"] = reason
    mods = d.get("modifiers")
    if isinstance(mods, list):
        out["modifiers"] = [m for m in mods if isinstance(m, str)]
    locs = d.get("locators")
    if isinstance(locs, list):
        out["locators"] = [_mask_locator(c) for c in locs if isinstance(c, dict) and c.get("kind")]
    if isinstance(d.get("caused_by"), int):
        out["caused_by"] = d["caused_by"]
    if isinstance(d.get("typed_placeholders"), list):
        out["typed_placeholders"] = [
            {"placeholder": str(n.get("placeholder")), "typed_at": n.get("typed_at")}
            for n in d["typed_placeholders"]
            if isinstance(n, dict) and isinstance(n.get("placeholder"), str)
        ]
    return out


BROWSER_PAGES = ("about:", "chrome:", "chrome-error:", "edge:", "devtools:", "chrome-search:")


def setup_reason(d: dict) -> str | None:
    """Why a step (as written in steps.jsonl) is a setup event, or None: the recorder says so
    (`setup`), or, in an older recording, a page load of a blank tab or a browser-internal page.
    Setup events stay in the recording for provenance and count in no application's steps,
    proposal, sequence or coverage."""
    if isinstance(d.get("setup"), str) and d["setup"]:
        return d["setup"]
    if str(d.get("action") or "") != "goto":
        return None
    url = str(d.get("url") or "")
    if not url or url.startswith("about:"):
        return "blank page"
    if url.startswith(BROWSER_PAGES):
        return "browser page"
    return None


def _mask_locator(cand: dict) -> dict:
    """Record numbers in a candidate's ids and selectors are masked like page ids."""
    out = dict(cand)
    for name in ("css", "rows", "control", "value"):
        if isinstance(out.get(name), str):
            out[name] = _mask_ids(out[name])
    return out


def _mask_ids(selector):
    """Record numbers built into a recorded selector (#open-R-1001) are masked like page ids."""
    if not isinstance(selector, str):
        return selector
    from twinlab2_contract.evidence.sanitize import mask_identifiers

    return mask_identifiers(selector)
