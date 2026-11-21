"""Step 2 of the launched-browser proof: is the web recording good evidence?

    python tools/rebuild_proof/web_audit.py w [c]     # case w; optionally compared with case c's steps

Checks, written to `<evidence>/<case>/record/web-audit.json`:

- **viewport**: `recording.json` names 1280x720, and every picture is 1280x720;
- **pictures**: per step, kept, dropped (and why) or none (a step observed at the next page);
  a kept picture says nothing of `picture_settled: false`;
- **right step**: each kept picture is read with the recorder's own OCR (Windows OCR) and must
  show the heading of its own step's page (`<h1>`/`<h2>`/`<title>` of `dom/<seq>.html`), not the
  next page's heading;
- **masking**: no typed value (drive.VALUES, 4 characters or more) in any picture's OCR text, in
  steps.jsonl, the DOM snapshots or the HAR;
- **steps**: the same actions as the attach-mode recording of the same clerk (case c's web steps),
  action by action;
- **HAR**: every page the steps loaded is in the HAR, with the API calls the pages made.

Nothing here opens a window: OCR runs PowerShell with CREATE_NO_WINDOW (no console of its own)."""

from __future__ import annotations

from collections import Counter
import html as htmllib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, read_json, write_json  # noqa: E402
import drive  # noqa: E402

VIEWPORT = (1280, 720)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def web_folder(base: Path) -> Path:
    return next(p for p in (base / "recording").iterdir() if p.is_dir()) / "web"


def headings(dom: str) -> list[str]:
    out = []
    for tag in ("h1", "h2", "title"):
        for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", dom, re.S | re.I):
            text = re.sub(r"<[^>]+>", " ", m.group(1))
            text = " ".join(htmllib.unescape(text).split())
            if 3 <= len(text) <= 60:
                out.append(text)
    return out


def typed_by(steps: list[dict], seq: int) -> set[str]:
    """The VALUES names typed (type steps) before step `seq`, by replay.FIELDS (selector -> names)."""
    from replay import FIELDS

    used: dict[str, int] = {}
    out = set()
    for s in steps:
        if s["seq"] >= seq:
            break
        if s["action"] == "type" and s.get("selector") in FIELDS:
            names = FIELDS[s["selector"]]
            i = used.get(s["selector"], 0)
            used[s["selector"]] = i + 1
            out.add(names[min(i, len(names) - 1)])
    return out


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def main(argv) -> int:
    case = argv[1] if len(argv) > 1 else "w"
    compare = argv[2] if len(argv) > 2 else None
    from twinlab2_recorder import ocr

    base = EVIDENCE / case
    web = web_folder(base)
    steps = rows(web / "steps.jsonl")
    meta = read_json(web / "recording.json")
    from PIL import Image

    sizes = Counter()
    report: dict = {"case": case, "recording": web.parent.name, "recording_json": meta}
    # viewport
    for s in steps:
        if s.get("screenshot") and (web / s["screenshot"]).is_file():
            with Image.open(web / s["screenshot"]) as img:
                sizes[f"{img.size[0]}x{img.size[1]}"] += 1
    vp = meta.get("viewport") or {}
    report["viewport"] = {"recording_json": f"{vp.get('width')}x{vp.get('height')}" if vp else None,
                          "picture_sizes": dict(sizes),
                          "ok": vp == {"width": VIEWPORT[0], "height": VIEWPORT[1]} and set(sizes) == {"1280x720"}}
    # pictures per step
    per = []
    for s in steps:
        state = "kept" if s.get("screenshot") else ("dropped" if s.get("picture_dropped") else "none")
        per.append({"seq": s["seq"], "action": s["action"], "tab": s.get("tab"), "url": s.get("url"),
                    "picture": state, "settled": s.get("picture_settled", True) if state == "kept" else None,
                    "note": s.get("picture_note"), "after": s.get("after"), "dom": bool(s.get("dom")),
                    "masked_boxes": len(s.get("mask_boxes") or [])})
    by = Counter(p["picture"] for p in per)
    report["pictures"] = {"steps": len(steps), "by_state": dict(by),
                          "kept_unsettled": [p["seq"] for p in per if p["picture"] == "kept" and p["settled"] is False],
                          "page_loads": {"total": sum(1 for p in per if p["action"] == "goto"),
                                         "kept": sum(1 for p in per if p["action"] == "goto" and p["picture"] == "kept")},
                          "with_snapshot_kept": f"{sum(1 for p in per if p['dom'] and p['picture'] == 'kept')}"
                                                f"/{sum(1 for p in per if p['dom'])}",
                          "steps": per}
    # right step and masking, by OCR
    values = {k: v for k, v in drive.VALUES.items() if isinstance(v, str) and len(v) >= 4 and not k.startswith("mf_")}
    ocr_rows, leaks, wrong = [], [], []
    doms = {s["seq"]: (web / s["dom"]).read_text(encoding="utf-8", errors="replace") if s.get("dom") and (web / s["dom"]).is_file() else ""
            for s in steps}  # fmt: skip
    for n, s in enumerate(steps):
        if not s.get("screenshot"):
            continue
        text = ocr.text(web / s["screenshot"]) or ""
        lines = [norm(line) for line in text.splitlines() if norm(line)]
        page_text = norm(re.sub(r"<[^>]+>", " ", doms.get(s["seq"]) or ""))

        def on_a_line(phrase: str) -> bool:  # OCR keeps a field's or a heading's text on one line
            return bool(norm(phrase)) and any(norm(phrase) in line for line in lines)
        own = headings(doms.get(s["seq"]) or "")
        later = next((t for t in steps[n + 1:] if t.get("dom") and t.get("tab") == s.get("tab")
                      and t.get("url") != s.get("url")), None)  # fmt: skip
        nxt = [h for h in headings(doms.get(later["seq"]) or "") if h not in own] if later else []
        shows_own = any(on_a_line(h) for h in own) if own else None
        shows_next = [h for h in nxt if on_a_line(h)]
        # a chosen option, or a value the page itself shows as text (an order total), is no leak
        typed = typed_by(steps, s["seq"])
        hits = [k for k, v in values.items() if k in typed and on_a_line(v) and norm(v) not in page_text]
        ocr_rows.append({"seq": s["seq"], "action": s["action"], "own_headings": own, "shows_own": shows_own,
                         "shows_next_page": shows_next, "typed_values_seen": hits})
        if hits:
            leaks.append({"seq": s["seq"], "values": hits})
        if shows_own is False:  # its own page's heading is missing (shows_next: reported only;
            # a navigation bar can hold the next page's words)
            wrong.append({"seq": s["seq"], "own": own, "next_seen": shows_next})
    report["ocr"] = {"engine": ocr.available(), "pictures": len(ocr_rows), "typed_values_in_pictures": leaks,
                     "pictures_not_of_their_step": wrong, "rows": ocr_rows}
    # typed values in the files
    files = [web / "steps.jsonl", web / "recording.har", *sorted((web / "dom").glob("*.html"))]
    found = []
    for f in files:
        body = f.read_text(encoding="utf-8", errors="replace")
        for k, v in values.items():
            if k in ("search_by", "product", "terms"):  # chosen options, shown by the page itself
                continue
            if v in body:
                found.append({"file": f.relative_to(web).as_posix(), "value": k})
    report["typed_values_in_files"] = found
    # steps against the attach-mode recording of the same clerk
    if compare:
        where = EVIDENCE / compare if (EVIDENCE / compare / "recording").is_dir() else EVIDENCE.parent / compare
        other = rows(web_folder(where) / "steps.jsonl")
        mine = [(s["action"], s.get("selector")) for s in steps if s["action"] not in ("setup",)]
        theirs = [(s["action"], s.get("selector")) for s in other if s["action"] not in ("setup",)]
        import difflib

        ops = difflib.SequenceMatcher(a=mine, b=theirs, autojunk=False).get_opcodes()
        diff = [{"op": op, "launched": mine[i1:i2], "attach": theirs[j1:j2]} for op, i1, i2, j1, j2 in ops if op != "equal"]
        report["steps_vs_attach"] = {"launched": len(mine), "attach": len(theirs), "same": not diff, "differences": diff[:40],
                                     "launched_by_action": dict(Counter(a for a, _ in mine)),
                                     "attach_by_action": dict(Counter(a for a, _ in theirs))}
    # HAR
    har = read_json(web / "recording.har")
    entries = har["log"]["entries"]
    urls = [e["request"]["url"] for e in entries]
    loads = sorted({s["url"].split("?")[0] for s in steps if s["action"] == "goto" and s.get("url")})
    missing = [u for u in loads if not any(x.split("?")[0] == u for x in urls)]
    api = sorted({re.sub(r"\?.*", "", u) for u in urls if "/api/" in u})
    report["har"] = {"entries": len(entries), "pages": len(har["log"].get("pages") or []),
                     "page_loads_missing": missing, "api_calls": api,
                     "statuses": dict(Counter(str(e["response"]["status"]) for e in entries))}
    out = base / "record" / "web-audit.json"
    write_json(out, report)
    short = {k: v for k, v in report.items() if k not in ("recording_json",)}
    short["pictures"] = {k: v for k, v in report["pictures"].items() if k != "steps"}
    short["ocr"] = {k: v for k, v in report["ocr"].items() if k != "rows"}
    print(json.dumps(short, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
