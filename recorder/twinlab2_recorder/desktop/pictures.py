# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Pictures of desktop windows without data, and the check that none shows a known data value.

The recorder takes a picture of the window at each snapshot (twinlab2_recorder.desktop.winlook): every place
the raw tree shows data (a value, a data name, a masked window title) is painted over in its own
background colour and its shape (`X-9999`) is drawn there in the control's font. Where the
places are comes from the raw tree and what is drawn from the clean one (twinlab2_recorder.desktop.privacy),
so a picture holds exactly what the clean tree holds, in the application's own pixels.

A second, independent check runs when the recording ends, while the data values seen are
still in memory (they are never written): each picture is read with OCR (twinlab2_recorder.ocr: Tesseract
or the engine built into Windows) and dropped when a data value of four or more letters or
digits appears in it. A picture that could not be checked (no OCR engine) is dropped too: a
picture is kept only when it was checked. `pictures.json` in the recording says what was
checked, how, and what was dropped (by step, never by value).

Everything here except `check` (which runs OCR) is plain Python.
"""

from __future__ import annotations

import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

DIR = "pictures"
MANIFEST = "pictures.json"
MIN_SECRET = 4  # shorter values are not looked for (OCR noise); the masks still cover them
NO_VALUE_TYPES = {"Window", "Pane", "TitleBar", "ScrollBar", "Slider", "ProgressBar", "Thumb"}


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _rect_ok(rect) -> bool:
    return bool(rect) and len(rect) == 4 and rect[2] > 2 and rect[3] > 2 and rect[0] > -50


def places(raw: dict, clean: dict) -> list[dict]:
    """Where the window shows data, window-relative, with the shape to draw there.
    `{"rect", "text", "hwnd", "secret", "caption"}`; `secret` (the raw text) stays in memory."""
    out: list[dict] = []
    if raw.get("name") and clean.get("name") != raw.get("name"):  # a title with data masked
        bar = next((k for k in raw.get("children") or [] if k.get("type") == "TitleBar"), None)
        if bar and _rect_ok(bar.get("rect")):
            x, y, w, h = bar["rect"]
            buttons = [b["rect"][0] for b in bar.get("children") or []
                       if b.get("type") == "Button" and _rect_ok(b.get("rect"))]  # fmt: skip
            right = min(buttons) if buttons else x + w
            left = max((b["rect"][0] + b["rect"][2] for b in bar.get("children") or []
                        if b.get("type") == "MenuBar" and _rect_ok(b.get("rect"))), default=x)  # fmt: skip
            out.append({"rect": [left, y, max(right - left, 0), h], "text": clean.get("name") or "",
                        "hwnd": raw.get("hwnd") or 0, "secret": raw.get("name"),
                        "caption": True})  # fmt: skip

    def walk(r: dict, c: dict, hwnd: int) -> None:
        h = r.get("hwnd") or hwnd
        t = r.get("type")
        kids_r, kids_c = r.get("children") or [], c.get("children") or []
        rect = r.get("rect")
        if _rect_ok(rect) and r.get("visible", True) and t not in NO_VALUE_TYPES:
            value = r.get("value")
            if r.get("password") or (value is not None and str(value).strip()):
                rr = list(rect)
                if t == "ComboBox":  # keep the drop-down button: it is structure
                    btn = [k["rect"][0] for k in kids_r
                           if k.get("type") == "Button" and _rect_ok(k.get("rect"))]  # fmt: skip
                    if btn and btn[0] > rr[0]:
                        rr[2] = btn[0] - rr[0]
                out.append({"rect": rr, "text": "" if r.get("password") else c.get("value") or "",
                            "hwnd": h, "secret": None if r.get("password") else str(value),
                            "multiline": bool((r.get("win32") or {}).get("multiline"))})  # fmt: skip
                return  # what it holds is covered
            name = (r.get("name") or "").strip()
            if c.get("name_is") == "data" and name and not kids_r:
                out.append({"rect": list(rect), "text": c.get("name") or "", "hwnd": h,
                            "secret": name})  # fmt: skip
        for rk, ck in zip(kids_r, kids_c, strict=False):
            walk(rk, ck, h)

    for rk, ck in zip(raw.get("children") or [], clean.get("children") or [], strict=False):
        walk(rk, ck, raw.get("hwnd") or 0)
    return out


def kept_texts(clean: dict) -> set[str]:
    """Normalised texts the clean tree keeps as structure (labels, buttons, code lists, the
    masked title): a data value equal to one of them is not looked for (it shows anyway)."""
    out: set[str] = set()

    def walk(n: dict) -> None:
        if n.get("name") and n.get("name_is") != "data":
            out.add(norm(n["name"]))
            out.update(norm(w) for w in re.findall(r"\w+", n["name"]))
        for k in n.get("children") or []:
            walk(k)

    walk(clean)
    return out


def secrets_of(found: list[dict]) -> set[str]:
    return {norm(p["secret"]) for p in found if p.get("secret")}


# --- PNG (the recorder writes 8-bit RGB, filter 0) --------------------------------------------------


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, RGB rows without filter bytes) of a PNG the recorder wrote: 8-bit RGB,
    one IDAT stream. Rows with another filter (another writer) are decoded too."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i, w, h, idat = 8, 0, 0, b""
    while i < len(data):
        n = struct.unpack(">I", data[i : i + 4])[0]
        tag, body = data[i + 4 : i + 8], data[i + 8 : i + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype not in (2, 6):
                raise ValueError("only 8-bit RGB or RGBA PNGs")
            bpp = 3 if ctype == 2 else 4
        elif tag == b"IDAT":
            idat += body
        i += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[x] = (line[x] + pred) & 255
        prev = line
        if bpp == 4:
            del line[3::4]
        rows += line
    return w, h, bytes(rows)


def write_png(w: int, h: int, rgb: bytes) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3 : (y + 1) * w * 3]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))  # fmt: skip

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))  # fmt: skip


def upscale(data: bytes, k: int = 2) -> bytes:
    """A PNG k times larger (nearest neighbour): small interface text reads better for OCR."""
    w, h, rgb = read_png(data)
    out = bytearray()
    for y in range(h):
        row = rgb[y * w * 3 : (y + 1) * w * 3]
        big = bytearray()
        for x in range(w):
            big += row[x * 3 : x * 3 + 3] * k
        out += big * k
    return write_png(w * k, h * k, bytes(out))


# --- the OCR check ------------------------------------------------------------------------------------

_MANY = r"""
param([string]$List)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { exit 3 }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
foreach ($p in (Get-Content -LiteralPath $List -Encoding UTF8)) {
  if (-not $p) { continue }
  "=====TWINLAB-OCR $p"
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $result.Lines | ForEach-Object { $_.Text }
  $stream.Dispose()
}
"""


def ocr_engine() -> str | None:
    from twinlab2_recorder import ocr

    return ocr.available()


def ocr_texts(pngs: dict[str, bytes], timeout: float = 300) -> dict[str, str] | None:
    """OCR text of each picture (key -> text), read at twice the size; None when no engine."""
    engine = ocr_engine()
    if engine is None or not pngs:
        return None if engine is None else {}
    with tempfile.TemporaryDirectory(prefix="twinlab-pic-") as tmp:
        files = {}
        for i, (key, data) in enumerate(pngs.items()):
            path = Path(tmp) / f"{i:04d}.png"
            path.write_bytes(upscale(data))
            files[str(path.resolve())] = key
        if engine == "tesseract":
            from twinlab2_recorder import ocr

            return {key: ocr.text(Path(p)) or "" for p, key in files.items()}
        listing = Path(tmp) / "list.txt"
        listing.write_text("\n".join(files), encoding="utf-8")
        script = Path(tmp) / "ocr.ps1"
        script.write_text(_MANY, encoding="utf-8")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                  "Bypass", "-File", str(script), str(listing)],
                                 capture_output=True, timeout=timeout, check=False)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        texts: dict[str, list[str]] = {}
        cur = None
        for line in out.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("=====TWINLAB-OCR "):
                cur = files.get(line[len("=====TWINLAB-OCR ") :].strip())
                texts.setdefault(cur, [])
            elif cur is not None:
                texts[cur].append(line)
        return {k: "\n".join(v) for k, v in texts.items() if k is not None}


def leaks(text: str, secrets: set[str], kept: set[str]) -> int:
    """How many data values (normalised, MIN_SECRET or longer, not also shown as structure)
    appear in a picture's OCR text."""
    flat = norm(text)
    return sum(1 for s in secrets if len(s) >= MIN_SECRET and s not in kept and s in flat)


def check(folder: Path, secrets: set[str], kept: set[str]) -> dict:
    """Read every picture of a recording with OCR; drop those that show a data value or could
    not be read. Returns (and writes) the manifest. `secrets` and `kept` are in memory only."""
    folder = Path(folder)
    pics = sorted((folder / DIR).glob("*.png")) if (folder / DIR).is_dir() else []
    data = {p.name: p.read_bytes() for p in pics}
    texts = ocr_texts(data) if data else {}
    engine = ocr_engine() if data else None
    dropped, kept_files = [], []
    for p in pics:
        text = None if texts is None else texts.get(p.name)
        if text is None:
            dropped.append({"picture": p.name, "why": "not checked: no OCR engine read it"})
            p.unlink()
            continue
        n = leaks(text, secrets, kept)
        if n:
            dropped.append({"picture": p.name, "why": f"{n} data value(s) readable in it"})
            p.unlink()
            continue
        kept_files.append(p.name)
    doc = {"format": "twinlab-desktop-pictures/1",
           "how": "window printed (PrintWindow); every place the control tree shows data painted "
                  "over in its own background and its shape drawn there",  # fmt: skip
           "checked_with": engine or "nothing (no OCR engine)",
           "values_looked_for": len([s for s in secrets if len(s) >= MIN_SECRET and s not in kept]),
           "kept": kept_files, "dropped": dropped}  # fmt: skip
    import json

    (folder / MANIFEST).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc


def available() -> bool:
    return sys.platform == "win32"
