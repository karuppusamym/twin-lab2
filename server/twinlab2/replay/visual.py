# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Visual fidelity (replication gap R08): the replica's screen compared with the source's.

Declared in `visual.yaml` beside the recording (`--visual` names another file), and versioned
with it: the states to compare, their baselines, thresholds and excluded regions, all set before
any comparison runs.

```yaml
version: 1
viewport: desktop          # desktop (1280x720) | mobile (390x844) | WIDTHxHEIGHT (e.g. 1258x570)
clock: 2026-01-05T09:00:00Z   # optional: Date is fixed at this time where the page shows time
fonts: [Georgia, Arial]    # optional: families to report as available or not
states:
  - id: two-tasks
    after: 5               # the step; its baseline is the recorder's picture screens/0005.png
    required: true
    mode: exact            # exact: zero differing pixels | threshold: up to max_ratio
    max_ratio: 0.0
    tolerance: 0           # a channel difference up to this is not a difference
    exclude: [.todo-list label, {css: .banner}]   # regions left out, by locator
    region: .todoapp       # optional: compare only this element's box
    # approved: approved/two-tasks.png  # instead of the recorder's picture: an approved image
```

A recording made by attaching to a person's own browser keeps that browser's size: its pictures
are, say, 1258x570. `viewport: 1258x570` (what `template` writes, from `recording.json` or the
pictures' size, `recording_viewport`) replays at that size, so the pictures can be compared.

The setup is pinned: Chromium (its version is reported), the viewport, device scale factor 1,
reduced motion, animations and the caret frozen by the screenshot, zoom 1, the clock when one is
declared, and the fonts reported. Pictures are of the viewport, as the recorder takes them.

The recorder paints what shows a typed value in solid magenta (255, 0, 255) before it takes a
picture. Those pixels (and one pixel around them) are excluded automatically and reported as
`masked`, never counted as passed. A state that is all masked or excluded is BLOCKED. "Exact"
means zero differing pixels in what is compared. Pictures of different sizes FAIL: the layout or
the viewport changed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

FILE = "visual.yaml"
VIEWPORTS = {"desktop": {"width": 1280, "height": 720}, "mobile": {"width": 390, "height": 844}}
MASK_RGB = (255, 0, 255)  # Playwright's default mask colour, which the recorder uses
PASS, FAIL, BLOCKED, SKIPPED = "PASS", "FAIL", "BLOCKED", "SKIPPED"


@dataclass
class State:
    id: str
    after: int | str
    required: bool = True
    mode: str = "exact"
    max_ratio: float = 0.0
    tolerance: int = 0
    exclude: list = field(default_factory=list)
    region: Any = None
    approved: str | None = None
    viewport: str | None = None  # default: the file's
    problem: str | None = None


@dataclass
class Spec:
    states: list[State] = field(default_factory=list)
    viewport: str = "desktop"
    clock: str | None = None
    fonts: list[str] = field(default_factory=list)
    path: Path | None = None

    def size(self) -> dict:
        return viewport_size(self.viewport) or dict(VIEWPORTS["desktop"])


_SIZE = re.compile(r"^\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*$")


def viewport_size(name) -> dict | None:
    """{width, height} of a viewport name: desktop, mobile or WIDTHxHEIGHT; None otherwise."""
    if isinstance(name, dict) and name.get("width") and name.get("height"):
        return {"width": int(name["width"]), "height": int(name["height"])}
    if not isinstance(name, str):
        return None
    if name in VIEWPORTS:
        return dict(VIEWPORTS[name])
    m = _SIZE.match(name)
    if m and 0 < int(m.group(1)) <= 7680 and 0 < int(m.group(2)) <= 4320:
        return {"width": int(m.group(1)), "height": int(m.group(2))}
    return None


def recording_viewport(rec_path: Path) -> str | None:
    """The size the recording's pictures were taken at, as WIDTHxHEIGHT: the viewport
    `recording.json` names (a recorder-launched browser), else the size of its pictures (an
    attached browser keeps its own size, so `recording.json` says null). None without either."""
    import json

    try:
        meta = json.loads((Path(rec_path) / "recording.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    size = viewport_size((meta or {}).get("viewport")) if isinstance(meta, dict) else None
    if size:
        return f"{size['width']}x{size['height']}"
    got = _last_size(Path(rec_path), pictures_of(Path(rec_path)))
    return f"{got[0]}x{got[1]}" if got else None


def _last_size(rec_path: Path, pictures: dict[int, str]) -> tuple[int, int] | None:
    """The size of the last picture that is there (a recorder that stopped may name one it did
    not write)."""
    for seq in sorted(pictures, reverse=True):
        got = picture_size(Path(rec_path) / pictures[seq])
        if got:
            return got
    return None


class VisualResult(BaseModel):
    id: str
    after: str
    viewport: str
    required: bool = True
    status: str
    mode: str = "exact"
    max_ratio: float = 0.0
    compared: int = 0  # pixels compared
    differing: int = 0
    ratio: float = 0.0
    excluded: int = 0  # pixels left out by declared regions
    masked: int = 0  # pixels left out because the source picture masked them
    before: str | None = None  # the baseline (source picture or approved image)
    after_path: str | None = None  # the replica's picture
    diff: str | None = None
    note: str | None = None


def load(path: Path | None) -> Spec | None:
    if path is None or not Path(path).exists():
        return None
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a visual baseline file")
    spec = Spec(path=Path(path), viewport=str(data.get("viewport") or "desktop"),
                clock=str(data["clock"]) if data.get("clock") else None,
                fonts=[str(f) for f in data.get("fonts") or []])  # fmt: skip
    seen: set[str] = set()
    for n, e in enumerate(data.get("states") or [], 1):
        if not isinstance(e, dict):
            spec.states.append(State(id=f"state-{n}", after="end", problem="not a mapping"))
            continue
        after = e.get("after", "end")
        st = State(id=str(e.get("id") or f"state-{n}"), after=after,
                   required=bool(e.get("required", True)), mode=str(e.get("mode") or "exact"),
                   max_ratio=float(e.get("max_ratio") or 0.0), tolerance=int(e.get("tolerance") or 0),
                   exclude=list(e.get("exclude") or []), region=e.get("region"),
                   approved=e.get("approved"), viewport=e.get("viewport"))  # fmt: skip
        if str(after).lower() != "end":
            try:
                st.after = int(after)
            except (TypeError, ValueError):
                st.problem = f"after: {after!r} is neither a step number nor end"
        if st.mode not in ("exact", "threshold"):
            st.problem = f"mode {st.mode!r}: exact or threshold"
        if viewport_size(st.viewport or spec.viewport) is None:
            st.problem = f"viewport {st.viewport or spec.viewport!r}: desktop, mobile or WIDTHxHEIGHT"
        if st.after == "end" and not st.approved:
            st.problem = st.problem or "a state at the end needs an approved image"
        if st.id in seen:
            # both states stay in the result; the second cannot stand in for the first
            st.problem = st.problem or f"the id {st.id!r} is used twice"
            st.id = f"{st.id}#{n}"
        seen.add(st.id)
        spec.states.append(st)
    return spec


def digest(spec: Spec | None, rec_path: Path | None, pictures: dict[int, str]) -> str | None:
    """The file and every baseline image it uses: changing either makes a result out of date."""
    if spec is None or spec.path is None:
        return None
    h = hashlib.sha256(Path(spec.path).read_bytes())
    for st in spec.states:
        img = baseline_image(st, rec_path, pictures, spec.path)
        if img is not None and img.exists():
            h.update(img.read_bytes())
    return h.hexdigest()[:12]


def baseline_image(st: State, rec_path: Path | None, pictures: dict[int, str],
                   spec_path: Path | None = None) -> Path | None:  # fmt: skip
    if st.approved:
        base = Path(spec_path).parent if spec_path else Path(rec_path or ".")
        return (base / st.approved).resolve()
    if isinstance(st.after, int) and st.after in pictures and rec_path is not None:
        return (Path(rec_path) / pictures[st.after]).resolve()
    return None


def pictures_of(rec_path: Path) -> dict[int, str]:
    """seq -> the recorder's picture after that step (`screenshot` in steps.jsonl)."""
    from twinlab2.replay.acceptance import _raw_steps

    out = {}
    for d in _raw_steps(rec_path):
        if isinstance(d.get("screenshot"), str) and isinstance(d.get("seq"), int):
            out[d["seq"]] = d["screenshot"]
    return out


def unsettled_pictures(rec_path: Path) -> dict[int, str]:
    """seq -> why the recorder's picture after that step may not show the settled page: the
    recorder says `picture_settled: false` when the next action had begun before it was taken.

    An older recorder did not say so. Its snapshot was taken once the page had been quiet for its
    quiet time (`recording.json`), or at once when the next action came first, and the picture
    after the snapshot: a step whose next action began within the quiet time had its picture
    taken after that action began, and is reported the same way."""
    import json

    from twinlab2.replay.acceptance import _raw_steps
    from twinlab2_contract.evidence.recording import _ts

    raw = [d for d in _raw_steps(rec_path) if isinstance(d.get("seq"), int)]
    said = any("picture_settled" in d for d in raw)
    try:
        meta = json.loads((Path(rec_path) / "recording.json").read_text(encoding="utf-8"))
        quiet = int((meta.get("settle") or {}).get("quiet_ms") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        quiet = 0
    out = {}
    for n, d in enumerate(raw):
        if d.get("picture_settled") is False:
            out[d["seq"]] = str(d.get("picture_note") or "the next action had begun before the "
                                "picture was taken")  # fmt: skip
            continue
        if said or not quiet or not d.get("screenshot"):
            continue
        nxt = next_action(raw, n)
        here, began = _ts(d.get("ts")), _ts(nxt.get("ts")) if nxt is not None else None
        if here is None or began is None:
            continue
        gap = int((began - here).total_seconds() * 1000)
        if 0 <= gap < quiet:
            out[d["seq"]] = (f"step {nxt['seq']} ({nxt.get('action')}) began {gap} ms after this "
                             f"step, within the recorder's quiet time ({quiet} ms): the picture "
                             "was taken after it began and may show what it did")  # fmt: skip
    return out


def next_action(steps: list[dict], n: int) -> dict | None:
    """The step after `steps[n]` in the same tab that can change what its page shows: not a
    dialog (the page is held while one is open), a download or a tab step, and not a step that
    `steps[n]` caused itself. (Twin Lab's `recorder.next_action`, 90bdd91: the server does not
    import the recorder package.)"""
    step = steps[n]
    for s in steps[n + 1 :]:
        action = str(s.get("action") or "")
        if (s.get("tab") or "") != (step.get("tab") or "") or action.startswith("tab-"):
            continue
        if action in ("dialog", "download") or s.get("caused_by") == step.get("seq"):
            continue
        return s
    return None


def picture_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - not a picture
        return None


# --- the pinned setup -----------------------------------------------------------------------------

_FONTS_JS = r"""async (families) => {
  try { await document.fonts.ready; } catch (e) {}
  const loaded = [...document.fonts].filter(f => f.status === "loaded").map(f => f.family);
  const checks = {};
  for (const f of families) {
    // available: the family draws differently from both generic fallbacks
    const c = document.createElement("canvas").getContext("2d");
    const w = (font) => { c.font = "32px " + font; return c.measureText("mmmmmmmmmmlli1WQ").width; };
    checks[f] = w('"' + f + '", monospace') !== w("monospace") || w('"' + f + '", serif') !== w("serif");
  }
  return {loaded: [...new Set(loaded)], available: checks,
          dpr: window.devicePixelRatio, zoom: window.visualViewport ? window.visualViewport.scale : 1};
}"""


def context_options(spec: Spec) -> dict:
    return {"viewport": spec.size(), "device_scale_factor": 1, "reduced_motion": "reduce"}


def prepare(page, spec: Spec) -> None:
    if spec.clock:
        from datetime import datetime

        when = datetime.fromisoformat(spec.clock.replace("Z", "+00:00"))
        page.clock.set_fixed_time(when)


def setup_report(browser, page, spec: Spec) -> dict:
    try:
        fonts = page.evaluate(_FONTS_JS, spec.fonts)
    except Exception:  # noqa: BLE001 - the page went away
        fonts = {}
    return {
        "browser": browser.browser_type.name,
        "browser_version": browser.version,
        "viewport": spec.viewport,
        "size": spec.size(),
        "device_scale_factor": 1,
        "reduced_motion": "reduce",
        "animations": "disabled",
        "caret": "hide",
        "clock": spec.clock,
        "zoom": fonts.get("zoom"),
        "device_pixel_ratio": fonts.get("dpr"),
        "fonts_loaded": fonts.get("loaded") or [],
        "fonts_available": fonts.get("available") or {},
    }


# --- comparing ------------------------------------------------------------------------------------


def boxes(page, locators: list) -> list[tuple[int, int, int, int]]:
    """Every box (x, y, w, h, in pixels) of every element the locators match."""
    from twinlab2.replay.acceptance import locator_for

    out = []
    for spec in locators:
        loc = locator_for(page, spec)
        for i in range(loc.count()):
            b = loc.nth(i).bounding_box()
            if b and b["width"] > 0 and b["height"] > 0:
                out.append((int(b["x"]), int(b["y"]), int(b["width"] + 0.999),
                            int(b["height"] + 0.999)))  # fmt: skip
    return out


def compare_images(before: Path, after: Path, *, exclude: list[tuple[int, int, int, int]] = (),
                   region: tuple[int, int, int, int] | None = None, mode: str = "exact",
                   max_ratio: float = 0.0, tolerance: int = 0,
                   diff_out: Path | None = None) -> dict:  # fmt: skip
    """Differing pixels between two pictures inside the compared region, leaving out declared
    boxes and the source's masked pixels. Returns the counts, the status and a note."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    a = Image.open(before).convert("RGB")
    b = Image.open(after).convert("RGB")
    if a.size != b.size:
        return {"status": FAIL, "compared": 0, "differing": 0, "ratio": 1.0, "excluded": 0,
                "masked": 0, "note": f"the pictures differ in size ({a.size[0]}x{a.size[1]} and "
                                     f"{b.size[0]}x{b.size[1]}): the layout or viewport changed"}  # fmt: skip
    w, h = a.size
    # what is compared: the region (or everything), minus declared boxes, minus masked pixels
    include = Image.new("L", (w, h), 0 if region else 255)
    draw = ImageDraw.Draw(include)
    if region:
        x, y, rw, rh = region
        draw.rectangle([x, y, x + rw - 1, y + rh - 1], fill=255)
    area = _count(include)
    declared = Image.new("L", (w, h), 0)
    dd = ImageDraw.Draw(declared)
    for x, y, bw, bh in exclude:
        dd.rectangle([x, y, x + bw - 1, y + bh - 1], fill=255)
    declared = ImageChops.multiply(declared, include)
    excluded = _count(declared)
    include = ImageChops.subtract(include, declared)
    r, g, bl = a.split()
    magenta = ImageChops.multiply(
        ImageChops.multiply(r.point(lambda v: 255 if v == MASK_RGB[0] else 0),
                            g.point(lambda v: 255 if v == MASK_RGB[1] else 0)),
        bl.point(lambda v: 255 if v == MASK_RGB[2] else 0),
    )  # fmt: skip
    if _count(magenta):
        magenta = magenta.filter(ImageFilter.MaxFilter(3))
    masked_here = ImageChops.multiply(magenta, include)
    masked = _count(masked_here)
    include = ImageChops.subtract(include, masked_here)
    compared = _count(include)
    diff = ImageChops.difference(a, b)
    dr, dg, db = diff.split()
    channel = ImageChops.lighter(ImageChops.lighter(dr, dg), db)
    differs = channel.point(lambda v: 255 if v > tolerance else 0)
    differs = ImageChops.multiply(differs, include)
    differing = _count(differs)
    ratio = differing / compared if compared else 0.0
    if diff_out is not None:
        Path(diff_out).parent.mkdir(parents=True, exist_ok=True)
        grey = Image.blend(b.convert("L").convert("RGB"), Image.new("RGB", (w, h), "white"), 0.6)
        grey.paste((120, 160, 255), mask=ImageChops.subtract(Image.new("L", (w, h), 255),
                                                             ImageChops.add(include, differs)))  # fmt: skip
        grey.paste((255, 0, 0), mask=differs)
        grey.save(diff_out)
    if compared == 0:
        status, note = BLOCKED, ("nothing is left to compare: the region is all excluded or "
                                 "masked")  # fmt: skip
    elif mode == "exact":
        status = PASS if differing == 0 else FAIL
        note = None if differing == 0 else f"{differing} pixel(s) differ; exact allows none"
    else:
        status = PASS if ratio <= max_ratio else FAIL
        note = None if status == PASS else f"{ratio:.4%} differ; the threshold is {max_ratio:.4%}"
    if masked and status == PASS:
        note = f"{masked} masked pixel(s) of the source were excluded, not compared"
    return {"status": status, "compared": compared, "differing": differing, "ratio": ratio,
            "excluded": excluded, "masked": masked, "area": area, "note": note}  # fmt: skip


def _count(mask) -> int:
    hist = mask.histogram()
    return sum(hist[1:])


def shoot(page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    from twinlab2.replay.fidelity import hidden_label

    with hidden_label(page):  # the twin's label is not part of the original's picture
        page.screenshot(path=str(path), animations="disabled", caret="hide", scale="css")
    return path


def check_state(page, st: State, spec: Spec, rec_path: Path, pictures: dict[int, str],
                out_dir: Path, run_viewport: str) -> VisualResult:  # fmt: skip
    vp = st.viewport or spec.viewport
    res = VisualResult(id=st.id, after=str(st.after), viewport=vp, required=st.required,
                       status=BLOCKED, mode=st.mode, max_ratio=st.max_ratio)  # fmt: skip
    if st.problem:
        res.note = st.problem
        return res
    if vp != run_viewport:
        res.status = SKIPPED if not st.required else BLOCKED
        res.note = f"needs a replay at the {vp} size (this one ran at {run_viewport})"
        return res
    img = baseline_image(st, rec_path, pictures, spec.path)
    if img is None or not img.exists():
        res.note = ("no baseline picture: the recorder kept none after this step (screenshots "
                    "off, or the masked picture could not be taken); give an approved image")  # fmt: skip
        return res
    res.before = str(img)
    try:
        excl = boxes(page, st.exclude)
        region = None
        if st.region is not None:
            found = boxes(page, [st.region])
            if len(found) != 1:
                res.note = f"the region matched {len(found)} element(s), not one"
                return res
            region = found[0]
    except Exception as exc:  # noqa: BLE001 - a locator the page cannot read
        res.note = f"an excluded region or the region cannot be located: {exc}"[:300]
        return res
    shot = shoot(page, out_dir / f"{st.id}.after.png")
    res.after_path = str(shot)
    diff = out_dir / f"{st.id}.diff.png"
    got = compare_images(img, shot, exclude=excl, region=region, mode=st.mode,
                         max_ratio=st.max_ratio, tolerance=st.tolerance, diff_out=diff)  # fmt: skip
    res.diff = str(diff) if diff.exists() else None
    for k in ("status", "compared", "differing", "ratio", "excluded", "masked", "note"):
        setattr(res, k, got[k])
    return res


CAROUSEL_REASON = ("a carousel that advances by itself ({how}): which slide shows depends on when "
                   "the picture is taken, not on the application")  # fmt: skip
VIDEO_REASON = "a video: the frame shown depends on when the picture is taken"
ANIMATION_REASON = "a CSS animation runs here: what it draws depends on when the picture is taken"
CHANGED_REASON = ("changed by itself between the snapshots after step {a} and step {b} of the same "
                  "screen, with no action between")  # fmt: skip
_USER = ("click", "dblclick", "type", "select", "check", "uncheck", "keypress", "submit", "upload",
         "goto", "drag")  # fmt: skip


def _region_of(node) -> str | None:
    if node.id:
        return f"#{node.id}"
    if node.classes:
        return node.tag + "".join(f".{c}" for c in node.classes[:2])
    return None


def self_changing(rec) -> list[dict]:
    """Regions of the recorded pages that change by themselves, so a picture compare would
    judge the moment it was taken: Bootstrap carousels that ride (`data-ride="carousel"`,
    `data-bs-ride`), video elements, CSS animations written on an element, and elements whose
    content changed between two snapshots of the same screen with no action between (a dialog
    holds the page; nothing the person did). Each is `{css, reason}`, for `exclude:`."""
    import re
    from collections import Counter
    from urllib.parse import urlsplit

    from twinlab2_contract.evidence import dom as domlib

    found: dict[str, str] = {}
    snaps: list[tuple[object, object]] = []  # (step, parsed snapshot)
    steps = list(getattr(rec, "steps", None) or [])
    for step in steps:
        html = rec.dom(step)
        if not html:
            continue
        root = domlib.parse_html(html)
        snaps.append((step, root))
        for node in domlib._walk(root):
            classes = set(node.classes)
            ride = node.attrs.get("data-ride") == "carousel" or "data-bs-ride" in node.attrs
            if "carousel" in classes and ride:
                how = ("data-ride=carousel" if node.attrs.get("data-ride") == "carousel"
                       else "data-bs-ride")  # fmt: skip
                inner = any("carousel-inner" in c.classes for c in node.children)
                base = f"#{node.id}" if node.id else ".carousel"
                found.setdefault(f"{base} .carousel-inner" if inner else base,
                                 CAROUSEL_REASON.format(how=how))  # fmt: skip
            elif node.tag == "video":
                found.setdefault("video", VIDEO_REASON)
            elif node.tag == "marquee" or re.search(
                r"(?:^|;)\s*animation(?:-name)?\s*:\s*(?!none\b)[^;]+",
                node.attrs.get("style") or "",
            ):
                region = _region_of(node) or node.tag
                found.setdefault(region, ANIMATION_REASON)
    for (a, ra), (b, rb) in zip(snaps, snaps[1:], strict=False):
        between = [s for s in steps if a.seq < s.seq <= b.seq]
        if any(s.action in _USER for s in between):
            continue
        ua, ub = urlsplit(a.url or ""), urlsplit(b.url or "")
        if (ua.path, ua.query) != (ub.path, ub.query):
            continue
        # an id the page uses twice (or a masked one, `item-999`, that many records share) names
        # no one element: such elements are not paired
        ids_a = Counter(n.id for n in domlib._walk(ra) if n.id)
        ids_b = Counter(n.id for n in domlib._walk(rb) if n.id)
        before = {n.id: n for n in domlib._walk(ra)
                  if n.id and ids_a[n.id] == 1 and ids_b[n.id] == 1
                  and not re.search(r"999", n.id)}  # fmt: skip
        for n in domlib._walk(rb):
            o = before.get(n.id) if n.id else None
            if o is None or n.tag in ("html", "body"):
                continue
            own = [c for c in n.children if not c.id]
            olds = [c for c in o.children if not c.id]
            changed = (set(n.classes) != set(o.classes) or domlib._text(n) != domlib._text(o)
                       or [(c.tag, c.classes) for c in own] != [(c.tag, c.classes) for c in olds])  # fmt: skip
            if changed and not any(d.id and d.id in before and _changed(d, before[d.id])
                                   for d in domlib._walk(n)):  # fmt: skip
                found.setdefault(f"#{n.id}", CHANGED_REASON.format(a=a.seq, b=b.seq))
    return [{"css": css, "reason": why} for css, why in found.items()]


def _changed(n, o) -> bool:
    from twinlab2_contract.evidence import dom as domlib

    return set(n.classes) != set(o.classes) or domlib._text(n) != domlib._text(o)


def template(rec, required: bool = True) -> str:
    """The visual.yaml template of a recording: a state per recorder picture, at the size the
    pictures were taken (`recording_viewport`). `required=False` (what Verify generates when
    no visual.yaml is declared) makes every state reported only: it never decides the result.
    A picture a step names that is not in the recording (the recorder dropped it) gets no state:
    the header says which."""
    named = pictures_of(rec.path)
    pictures = {seq: rel for seq, rel in named.items() if (Path(rec.path) / rel).is_file()}
    missing = sorted(set(named) - set(pictures))
    unsettled = unsettled_pictures(rec.path)
    moving = self_changing(rec) if pictures else []
    states = []
    for seq in sorted(pictures):
        # threshold by default: a replica drawn with another font or a sub-pixel shift fails
        # exact at once; a person who wants zero differing pixels sets mode: exact
        st = {"id": f"after-step-{seq}", "after": seq, "required": required,
              "mode": "threshold", "max_ratio": 0.02, "tolerance": 16,
              "exclude": [dict(m) for m in moving]}  # fmt: skip
        if seq in unsettled:
            # the picture may show the next step's effect: reported, never deciding the result
            st["required"] = False
            st["note"] = f"optional: the recorder's picture is not settled ({unsettled[seq]})"
        states.append(st)
    size = _last_size(rec.path, pictures) if pictures else None
    head = (
        "# Visual baselines: the recorder's pictures (or approved images), compared with the\n"
        "# replica's at the pinned setup. Set exclude: (regions by locator) and the mode before\n"
        "# comparing; delete states that are not required. Masked pixels are always excluded.\n"
        "# threshold mode: up to max_ratio (2 %) of the compared pixels may differ by more than\n"
        "# tolerance per channel; mode: exact (max_ratio 0.0, tolerance 0) is the strict choice.\n"
        + (f"# The recorder's pictures are {size[0]}x{size[1]}.\n" if size else
           "# This recording has no pictures: every state needs an approved image.\n")
    )  # fmt: skip
    if missing:
        head += (f"# Not in the recording, so not compared: the pictures of step(s) "
                 f"{', '.join(map(str, missing[:40]))}{' ...' if len(missing) > 40 else ''}.\n")
    if moving:
        head += ("# Excluded in every state by default: regions that change by themselves (delete an\n"
                 "# entry to compare it):\n"
                 + "".join(f"#   {m['css']}: {m['reason']}\n" for m in moving))  # fmt: skip
    body = {"version": 1, "recording": rec.id, "viewport": recording_viewport(rec.path) or "desktop", "fonts": [],
            "states": states}  # fmt: skip
    return head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def write_template(rec, out: Path | None = None) -> Path:
    out = Path(out) if out else rec.path / FILE
    if out.exists():
        raise FileExistsError(f"{out} exists: it is not overwritten (delete it, or --out)")
    out.write_text(template(rec), encoding="utf-8")
    return out
