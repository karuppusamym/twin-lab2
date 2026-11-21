# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Windows only: what a window looks like, read without reading data.

- `control_facts(hwnd)`: what Win32 says about a control that has a window of its own (every
  WinForms control, Win32/MFC controls; WPF and Java controls have none): its font
  (`WM_GETFONT`), and for an edit box its length limit (`EM_GETLIMITTEXT`) and styles
  (multi-line, digits only, upper/lower case, password), for a combo box whether it can be
  typed in, for a button whether it is the window's default button. Messages that only read.
- `window_facts(hwnd)`: the frame: client area within the window, DPI, border style, minimise
  and maximise boxes, owner, where it was placed (centred on its owner or the screen).
- `system_facts()`: the machine's settings that change how every window looks (DPI, the system
  fonts, light or dark apps, high contrast).
- `masked_picture(hwnd, raw, clean)`: a PNG of one window (PrintWindow) with every place that
  shows data painted over in the place's own background colour and its shape (`X-9999`) drawn
  there instead, in the control's own font. The raw tree says where data is; the clean tree
  gives the shape. The unmasked pixels never leave memory.

Imported lazily by twinlab2_recorder.desktop.win and the recorder, after `twinlab2_recorder.desktop.require()`.
"""

from __future__ import annotations

import ctypes
import struct
import zlib
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

WM_GETFONT, EM_GETLIMITTEXT = 0x0031, 0x00D5
SMTO_ABORTIFHUNG = 0x0002
GWL_STYLE, GWL_EXSTYLE = -16, -20
ES_UPPERCASE, ES_LOWERCASE, ES_PASSWORD, ES_MULTILINE, ES_NUMBER, ES_READONLY = (
    0x8, 0x10, 0x20, 0x4, 0x2000, 0x800)  # fmt: skip
WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU, WS_CAPTION = (
    0x40000, 0x20000, 0x10000, 0x80000, 0xC00000)  # fmt: skip
WS_EX_DLGMODALFRAME, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x1, 0x8, 0x80
SPI_GETNONCLIENTMETRICS, SPI_GETHIGHCONTRAST = 0x0029, 0x0042
DEFAULT_GUI_FONT = 17

user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = ctypes.c_void_p
user32.GetWindowDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p]
user32.DrawTextW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]  # fmt: skip
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                         wintypes.UINT]  # fmt: skip
try:
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
except AttributeError:  # before Windows 10 1607
    pass
gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]  # fmt: skip
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.SetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]  # fmt: skip
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = ctypes.c_void_p
gdi32.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.CreateFontIndirectW.argtypes = [ctypes.c_void_p]
gdi32.CreateFontIndirectW.restype = ctypes.c_void_p


class LOGFONTW(ctypes.Structure):
    _fields_ = [("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
                ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
                ("lfWeight", ctypes.c_long), ("lfItalic", ctypes.c_byte),
                ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
                ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
                ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
                ("lfPitchAndFamily", ctypes.c_byte), ("lfFaceName", ctypes.c_wchar * 32)]  # fmt: skip


class NONCLIENTMETRICSW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("iBorderWidth", ctypes.c_int),
                ("iScrollWidth", ctypes.c_int), ("iScrollHeight", ctypes.c_int),
                ("iCaptionWidth", ctypes.c_int), ("iCaptionHeight", ctypes.c_int),
                ("lfCaptionFont", LOGFONTW), ("iSmCaptionWidth", ctypes.c_int),
                ("iSmCaptionHeight", ctypes.c_int), ("lfSmCaptionFont", LOGFONTW),
                ("iMenuWidth", ctypes.c_int), ("iMenuHeight", ctypes.c_int),
                ("lfMenuFont", LOGFONTW), ("lfStatusFont", LOGFONTW),
                ("lfMessageFont", LOGFONTW), ("iPaddedBorderWidth", ctypes.c_int)]  # fmt: skip


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]  # fmt: skip


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                ("lpszDefaultScheme", wintypes.LPWSTR)]  # fmt: skip


class _BMI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]  # fmt: skip


# --- fonts -----------------------------------------------------------------------------------------

_fonts: dict[int, dict] = {}


def dpi_of(hwnd: int) -> int:
    try:
        return int(user32.GetDpiForWindow(hwnd) or 96)
    except (AttributeError, OSError):
        return 96


def _logfont(lf: LOGFONTW, dpi: int) -> dict:
    px = abs(int(lf.lfHeight))
    out = {"face": lf.lfFaceName, "px": px, "pt": round(px * 72 / dpi * 4) / 4 if px else None,
           "weight": int(lf.lfWeight) or 400}  # fmt: skip
    if lf.lfItalic:
        out["italic"] = True
    if lf.lfUnderline:
        out["underline"] = True
    if lf.lfStrikeOut:
        out["strikeout"] = True
    return out


def font_of_handle(hfont: int, dpi: int) -> dict | None:
    if not hfont:
        return None
    key = (hfont, dpi)
    if key not in _fonts:
        lf = LOGFONTW()
        if not gdi32.GetObjectW(ctypes.c_void_p(hfont), ctypes.sizeof(LOGFONTW), ctypes.byref(lf)):
            return None
        _fonts[key] = _logfont(lf, dpi)
    return dict(_fonts[key])


def _send(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> int | None:
    res = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 200,
                                    ctypes.byref(res))  # fmt: skip
    return int(res.value) if ok else None


def font_handle(hwnd: int) -> int:
    return _send(hwnd, WM_GETFONT) or 0


def control_facts(hwnd: int, kind: str, cls: str) -> dict:
    """What Win32 says about one control with a window of its own (module docstring)."""
    out: dict = {}
    dpi = dpi_of(hwnd)
    hfont = font_handle(hwnd)
    font = font_of_handle(hfont, dpi) if hfont else None
    if font:
        out["font"] = font
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    low = (cls or "").lower()
    if kind == "Edit" and "edit" in low:
        limit = _send(hwnd, EM_GETLIMITTEXT)
        if limit:
            out["max_length"] = limit
        for flag, name in ((ES_MULTILINE, "multiline"), (ES_NUMBER, "digits_only"),
                           (ES_UPPERCASE, "upper_case"), (ES_LOWERCASE, "lower_case"),
                           (ES_PASSWORD, "password")):  # fmt: skip
            if style & flag:
                out[name] = True
    elif kind == "ComboBox" and "combobox" in low:
        out["combo_style"] = {1: "simple", 2: "dropdown", 3: "dropdown-list"}.get(style & 3)
    elif kind == "Button" and "button" in low:
        if style & 0xF == 1:  # BS_DEFPUSHBUTTON
            out["default_button"] = True
    return out


def window_facts(hwnd: int) -> dict:
    """The frame of a top-level window (module docstring). Sizes in physical pixels."""
    wr, cr = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    dpi = dpi_of(hwnd)
    out = {
        "size": [wr.right - wr.left, wr.bottom - wr.top],
        "client": [pt.x - wr.left, pt.y - wr.top, cr.right, cr.bottom],
        "dpi": dpi,
        "scale": round(dpi / 96, 2),
        "resizable": bool(style & WS_THICKFRAME),
        "minimize_box": bool(style & WS_MINIMIZEBOX),
        "maximize_box": bool(style & WS_MAXIMIZEBOX),
        "system_menu": bool(style & WS_SYSMENU),
        "caption": style & WS_CAPTION == WS_CAPTION,
        "dialog_frame": bool(ex & WS_EX_DLGMODALFRAME),
        "tool_window": bool(ex & WS_EX_TOOLWINDOW),
        "topmost": bool(ex & WS_EX_TOPMOST),
    }
    font = font_of_handle(font_handle(hwnd), dpi)
    if font:
        out["font"] = font
    owner = int(user32.GetWindow(hwnd, 4) or 0)  # GW_OWNER
    centre = ((wr.left + wr.right) / 2, (wr.top + wr.bottom) / 2)
    placed = None
    if owner:
        orc = wintypes.RECT()
        user32.GetWindowRect(owner, ctypes.byref(orc))
        if _near(centre, ((orc.left + orc.right) / 2, (orc.top + orc.bottom) / 2)):
            placed = "centre of its owner"
    if placed is None:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            w = mi.rcWork
            if _near(centre, ((w.left + w.right) / 2, (w.top + w.bottom) / 2)):
                placed = "centre of the screen"
    out["placed"] = placed or "elsewhere (not centred on its owner or the screen)"
    return out


def _near(a, b, tol: float = 2.5) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def system_facts() -> dict:
    """Machine settings that change how every window looks (module docstring)."""
    out: dict = {}
    try:
        out["dpi"] = int(user32.GetDpiForSystem())
    except (AttributeError, OSError):
        out["dpi"] = 96
    m = NONCLIENTMETRICSW()
    m.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
    if user32.SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, m.cbSize, ctypes.byref(m), 0):
        out["fonts"] = {k: _logfont(getattr(m, f), out["dpi"]) for k, f in (
            ("message", "lfMessageFont"), ("caption", "lfCaptionFont"), ("menu", "lfMenuFont"),
            ("status", "lfStatusFont"))}  # fmt: skip
    gui = font_of_handle(gdi32.GetStockObject(DEFAULT_GUI_FONT) or 0, 96)
    if gui:
        out["fonts"] = {**out.get("fonts", {}), "default_gui": gui}
    hc = HIGHCONTRASTW()
    hc.cbSize = ctypes.sizeof(HIGHCONTRASTW)
    if user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0):
        out["high_contrast"] = bool(hc.dwFlags & 1)
    for name, key, value in (
        ("apps_light_theme", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
         "AppsUseLightTheme"),
        ("text_scale_percent", r"Software\Microsoft\Accessibility", "TextScaleFactor"),
    ):  # fmt: skip
        got = _reg_dword(key, value)
        if got is not None:
            out[name] = bool(got) if name == "apps_light_theme" else got
    out.setdefault("text_scale_percent", 100)
    return out


def _reg_dword(key: str, value: str) -> int | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            v, _ = winreg.QueryValueEx(k, value)
            return int(v)
    except (OSError, ValueError):
        return None


# --- the masked picture ---------------------------------------------------------------------------


def _png(w: int, h: int, bgra: bytes) -> bytes:
    raw = bytearray()
    for y in range(h):
        row = bgra[y * w * 4 : (y + 1) * w * 4]
        raw.append(0)
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        raw += rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))  # fmt: skip

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))  # fmt: skip


def _pixels(mem, bmp, w: int, h: int) -> bytes:
    bmi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    return buf.raw


def _colours(px: bytes, w: int, h: int, r: tuple[int, int, int, int]):
    """The background (most common colour on the rectangle's inner edge) and the text colour
    (the most common colour inside that differs clearly from it) of a rectangle."""
    x0, y0, x1, y1 = max(r[0], 0), max(r[1], 0), min(r[2], w), min(r[3], h)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None, None
    counts: dict[int, int] = {}

    def at(x, y):
        i = (y * w + x) * 4
        return px[i + 2] | px[i + 1] << 8 | px[i] << 16  # BGRA pixels -> 0xBBGGRR, a COLORREF

    edge = [(x, y) for x in range(x0 + 2, x1 - 2, 2) for y in (y0 + 2, y1 - 3)]
    edge += [(x, y) for y in range(y0 + 2, y1 - 2, 2) for x in (x0 + 2, x1 - 3)]
    for x, y in edge:
        c = at(x, y)
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None, None
    bg = max(counts, key=counts.get)
    inner: dict[int, int] = {}
    for y in range(y0 + 1, y1 - 1, 2):
        for x in range(x0 + 1, x1 - 1, 2):
            c = at(x, y)
            if _distance(c, bg) > 120:
                inner[c] = inner.get(c, 0) + 1
    # The text's own colour is the one furthest from the background among those seen more
    # than once: ClearType's coloured fringes are nearer to it.
    common = [c for c, n in inner.items() if n > 1] or list(inner)
    fg = max(common, key=lambda c: (_distance(c, bg), inner[c])) if common else None
    return bg, fg


def _distance(a: int, b: int) -> int:
    return sum(abs((a >> s & 255) - (b >> s & 255)) for s in (0, 8, 16))


def hex_colour(c: int | None) -> str | None:
    """A COLORREF (0xBBGGRR) as `#rrggbb`."""
    if c is None:
        return None
    return f"#{c & 255:02x}{c >> 8 & 255:02x}{c >> 16 & 255:02x}"


def blank(px: bytes) -> bool:
    """Whether a printed window came back black (it had not painted yet)."""
    step = 4 * 97
    return not any(px[i] | px[i + 1] | px[i + 2] for i in range(0, len(px) - 3, step))


def grab(hwnd: int, tries: int = 4) -> tuple[int, int, bytes] | None:
    """The pixels of one window (PrintWindow), BGRA, in memory: (width, height, bytes). A window
    that has not painted yet prints black: it is printed again (up to `tries` times, 60 ms
    apart); None when it stays black."""
    import time

    for n in range(tries):
        got = _grab_once(hwnd)
        if got is None:
            return None
        if not blank(got[2]):
            return got
        if n + 1 < tries:
            time.sleep(0.06)
    return None


def _grab_once(hwnd: int) -> tuple[int, int, bytes] | None:
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
            return None
        return w, h, _pixels(mem, bmp, w, h)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def masked_picture(hwnd: int, places: list[dict], font_for,
                   pixels: tuple[int, int, bytes] | None = None) -> tuple[bytes, list[dict]] | None:  # fmt: skip
    """PNG bytes of one window with each place painted over, and the colours seen. `pixels`:
    the window as `grab` read it when its tree was read (else it is printed now).

    `places`: `{"rect": [x, y, w, h] (window-relative), "text": shape or "", "hwnd": the
    control's own or nearest window or 0, "align": "left"|"centre", "fill": a COLORREF to paint
    with instead of the place's own background (an excluded window's place)}`. `font_for(hwnd)` gives a
    font handle (0: the system message font). Returns None when the window cannot be printed.
    The second value lists, per place, the background and text colours sampled before
    painting (`#rrggbb`), which are the application's colours, not data."""
    if pixels is None or blank(pixels[2]):
        pixels = grab(hwnd)
    if pixels is None:
        return None
    w, h, before = pixels
    # The screen's DC, not the window's: the window may have closed since its pixels were read.
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    bmi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    gdi32.SetDIBits(hdc, bmp, 0, h, before, ctypes.byref(bmi), 0)  # before it is selected
    old = gdi32.SelectObject(mem, bmp)
    made: list[int] = []
    try:
        seen = []
        gdi32.SetBkMode(mem, 1)  # TRANSPARENT
        for p in places:
            x, y, pw, ph = p["rect"]
            r = (x, y, x + pw, y + ph)
            bg, fg = _colours(before, w, h, r)
            seen.append({"background": hex_colour(bg), "text": hex_colour(fg)})
            if isinstance(p.get("fill"), int):  # a fixed colour (an excluded window's place)
                bg = p["fill"]
            if bg is None:
                bg = 0xFFFFFF
            brush = gdi32.CreateSolidBrush(bg)
            rect = wintypes.RECT(max(x, 0), max(y, 0), min(x + pw, w), min(y + ph, h))
            user32.FillRect(mem, ctypes.byref(rect), brush)
            gdi32.DeleteObject(brush)
            text = p.get("text") or ""
            if text and rect.right - rect.left > 4:
                if p.get("caption"):
                    hf = 0
                elif p.get("lf"):
                    hf = _font_from_bytes(p["lf"])
                else:
                    hf = _local_font(font_for(p.get("hwnd") or 0)) if font_for else 0
                if not hf:
                    hf = caption_font() if p.get("caption") else _message_font()
                    made.append(hf)
                prev = gdi32.SelectObject(mem, hf) if hf else None
                gdi32.SetTextColor(mem, fg if fg is not None else 0)
                align = 0x1 if p.get("align") == "centre" else 0x0  # DT_CENTER / DT_LEFT
                if p.get("multiline"):  # top-left, wrapped: DT_WORDBREAK | DT_NOPREFIX
                    inner = wintypes.RECT(rect.left + 3, rect.top + 2, rect.right - 2, rect.bottom)
                    flags = 0x10 | 0x800 | align
                else:  # DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS
                    inner = wintypes.RECT(rect.left + 3, rect.top, rect.right - 2, rect.bottom)
                    flags = 0x20 | 0x4 | 0x800 | 0x8000 | align
                user32.DrawTextW(mem, text, -1, ctypes.byref(inner), flags)
                if prev:
                    gdi32.SelectObject(mem, prev)
        return _png(w, h, _pixels(mem, bmp, w, h)), seen
    finally:
        for f in made:
            gdi32.DeleteObject(f)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)


_local: dict[tuple, int] = {}


def logfont_bytes(hwnd: int) -> bytes | None:
    """The LOGFONT of the font a control draws with (WM_GETFONT), as bytes, or None: read while
    the control exists, used later to draw in the same font."""
    hfont = font_handle(hwnd) if hwnd else 0
    if not hfont:
        return None
    lf = LOGFONTW()
    if not gdi32.GetObjectW(ctypes.c_void_p(hfont), ctypes.sizeof(LOGFONTW), ctypes.byref(lf)):
        return None
    return bytes(lf)


def _font_from_bytes(data: bytes) -> int:
    lf = LOGFONTW.from_buffer_copy(data)
    key = (lf.lfFaceName, lf.lfHeight, lf.lfWeight, lf.lfItalic, lf.lfUnderline)
    if key not in _local:
        _local[key] = int(gdi32.CreateFontIndirectW(ctypes.byref(lf)) or 0)
    return _local[key]


def _local_font(hfont: int) -> int:
    """A font of this process like another process's font handle (a GDI handle of another
    process cannot be selected here; its LOGFONT can be read). 0 when unknown."""
    if not hfont:
        return 0
    lf = LOGFONTW()
    if not gdi32.GetObjectW(ctypes.c_void_p(hfont), ctypes.sizeof(LOGFONTW), ctypes.byref(lf)):
        return 0
    key = (lf.lfFaceName, lf.lfHeight, lf.lfWeight, lf.lfItalic, lf.lfUnderline)
    if key not in _local:
        _local[key] = int(gdi32.CreateFontIndirectW(ctypes.byref(lf)) or 0)
    return _local[key]


def _message_font() -> int:
    m = NONCLIENTMETRICSW()
    m.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
    if not user32.SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, m.cbSize, ctypes.byref(m), 0):
        return 0
    return int(gdi32.CreateFontIndirectW(ctypes.byref(m.lfMessageFont)) or 0)


def caption_font() -> int:
    m = NONCLIENTMETRICSW()
    m.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
    if not user32.SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, m.cbSize, ctypes.byref(m), 0):
        return 0
    return int(gdi32.CreateFontIndirectW(ctypes.byref(m.lfCaptionFont)) or 0)


# --- the icon of a message box ----------------------------------------------------------------

ICONS = {"error": 32513, "question": 32514, "warning": 32515, "information": 32516}  # OIC_*
user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.c_int,
                              ctypes.c_int, wintypes.UINT]  # fmt: skip
user32.LoadImageW.restype = ctypes.c_void_p
user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, ctypes.c_void_p,
                              wintypes.UINT]  # fmt: skip
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
_icon_cache: dict[tuple[str, int, int], bytes] = {}


def _icon_pixels(name: str, size: int, bg: int) -> bytes | None:
    """One system icon drawn at `size` on the colour `bg` (COLORREF), BGRA."""
    key = (name, size, bg)
    if key in _icon_cache:
        return _icon_cache[key]
    icon = None
    try:
        comctl = ctypes.WinDLL("comctl32")
        h = ctypes.c_void_p()
        fn = comctl.LoadIconWithScaleDown  # comctl32 6: the size message boxes use
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                       ctypes.POINTER(ctypes.c_void_p)]  # fmt: skip
        if fn(None, ctypes.c_void_p(ICONS[name]), size, size, ctypes.byref(h)) == 0:
            icon = h.value
    except (OSError, AttributeError):
        icon = None
    if not icon:
        icon = user32.LoadImageW(None, ctypes.c_void_p(ICONS[name]), 1, size, size, 0)
    if not icon:
        return None
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, size, size)
    old = gdi32.SelectObject(mem, bmp)
    brush = gdi32.CreateSolidBrush(bg)
    try:
        r = wintypes.RECT(0, 0, size, size)
        user32.FillRect(mem, ctypes.byref(r), brush)
        user32.DrawIconEx(mem, 0, 0, icon, size, size, 0, None, 3)  # DI_NORMAL
        out = _pixels(mem, bmp, size, size)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(brush)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        user32.DestroyIcon(icon)
    _icon_cache[key] = out
    return out


def icon_difference(px: bytes, w: int, rect, icon: bytes, size: int) -> float:
    """Mean difference per channel (0-255) between a square of a picture and an icon."""
    x0, y0 = rect[0], rect[1]
    total, n = 0, 0
    for y in range(0, size, 2):
        row = ((y0 + y) * w + x0) * 4
        for x in range(0, size, 2):
            i, j = row + x * 4, (y * size + x) * 4
            total += (
                abs(px[i] - icon[j]) + abs(px[i + 1] - icon[j + 1]) + abs(px[i + 2] - icon[j + 2])
            )
            n += 3
    return total / n if n else 255.0


def message_icon(pixels: tuple[int, int, bytes] | None, rect) -> str | None:
    """Which of the four standard icons (error, question, warning, information) a message box
    shows at `rect` (window coordinates, as the tree's), by drawing each one there; None when
    none matches clearly. Only pixels in memory are read."""
    if not pixels or not rect:
        return None
    w, h, px = pixels
    x, y, cw, ch = rect
    if cw < 16 or abs(cw - ch) > 2 or x < 1 or y < 1 or x + cw > w or y + ch > h:
        return None
    size = min(cw, ch)
    i = ((y - 1) * w + x - 1) * 4  # the background just outside the icon's corner
    bg = px[i + 2] | px[i + 1] << 8 | px[i] << 16
    scores = []
    for name in ICONS:
        icon = _icon_pixels(name, size, bg)
        if icon is not None:
            scores.append((icon_difference(px, w, (x, y), icon, size), name))
    if not scores:
        return None
    scores.sort()
    best = scores[0]
    second = scores[1][0] if len(scores) > 1 else 255.0
    return best[1] if best[0] < 24 and best[0] < 0.5 * second else None


def mark_message_icon(raw: dict) -> None:
    """Set `win32.icon` on the Image of a message box (`#32770`) tree from its pixels."""
    if (raw.get("cls") or "") != "#32770" or not raw.get("_pixels"):
        return
    for c in raw.get("children") or []:
        if c.get("type") == "Image" and c.get("rect"):
            kind = message_icon(raw["_pixels"], c["rect"])
            if kind:
                c["win32"] = {**(c.get("win32") or {}), "icon": kind}
