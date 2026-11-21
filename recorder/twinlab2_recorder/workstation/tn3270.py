# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Record a 3270 (mainframe) application by standing between the terminal and the host.

The terminal (UiPath's terminal activities with a direct TN3270 connection, or an emulator such
as IBM PCOMM, Reflection or wc3270) connects to `127.0.0.1:<listen>` instead of the host; the
proxy forwards every byte, unchanged, to the real host (over TLS when the target says so) and
reads a copy of the 3270 data stream in both directions:

- host to terminal: Write, Erase/Write, Erase/Write Alternate (and the same inside a Write
  Structured Field), Erase All Unprotected: the screen buffer and its fields (position, length,
  protected, numeric, intensified, non-display);
- terminal to host: the AID key (Enter, PF1-PF24, PA1-PA3, Clear), the cursor, and the fields the
  person or robot changed.

A recording folder holds `steps.jsonl`, `screens/<seq>.json` and `mainframe.json`. Steps:

- `screen`: the host showed a screen (the first after connecting, or the answer to a key):
  `screen: {"id", "title", "rows", "cols"}`; its field table is in `screens/<seq>.json`;
- `type`: a field the terminal sent changed: `field: {"row", "col", "len", "label"}`,
  `typed_shape` (letters X/x, digits 9), `blank` when empty; for a non-display field (a
  password) `secret: true` and nothing of the value;
- `aid`: the key that sent the screen: `key`, `cursor`.

Privacy, as for the other surfaces: what the host shows in protected fields is structure (labels,
titles) with every token holding a digit masked, and any value typed earlier in the session
masked wherever it shows again; what is in an unprotected field is data (its shape only);
non-display fields keep nothing. A protected field that shows data without a digit (a name) is
kept in the local screen file as it is shown; it reaches a label only when it stands right before
an input field on the same row (a label's position), and the portal's label rules apply after
that (twinlab2_recorder.workstation.flow).

The screen id is a hash of the screen's field layout (positions, lengths, protected or not), not
of its texts, so the same map with another record's data is the same screen.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import ssl
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from twinlab2_recorder.workstation import journal as capture

IAC, SE, SB, WILL, WONT, DO, DONT, EOR = 255, 240, 250, 251, 252, 253, 254, 239
TTYPE, TN3270E = 24, 40
CODE = [0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E,
        0x4F, 0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D,
        0x5E, 0x5F, 0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0x6A, 0x6B, 0x6C,
        0x6D, 0x6E, 0x6F, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B,
        0x7C, 0x7D, 0x7E, 0x7F]  # fmt: skip
AIDS = {0x7D: "Enter", 0x6D: "Clear", 0x6C: "PA1", 0x6E: "PA2", 0x6B: "PA3", 0x60: "NoAID",
        0x7E: "Enter", 0xE6: "SysReq"}  # fmt: skip
for _n, _b in enumerate([0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B, 0x7C,
                         0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C],
                        1):  # fmt: skip
    AIDS[_b] = f"PF{_n}"
SHORT_READ = {0x6D, 0x6C, 0x6E, 0x6B}
WRITE, ERASE_WRITE, ERASE_WRITE_ALT, EAU, WSF = (
    {0xF1, 0x01},
    {0xF5, 0x05},
    {0x7E, 0x0D},
    {0x6F, 0x0F},
    {0xF3, 0x11},
)
MODELS = {"2": (24, 80), "3": (32, 80), "4": (43, 80), "5": (27, 132)}
DIGIT_TOKEN = re.compile(r"\S*\d\S*")


def decode_addr(b1: int, b2: int) -> int:
    if b1 & 0xC0 == 0:  # 14-bit binary
        return ((b1 & 0x3F) << 8) | b2
    return ((b1 & 0x3F) << 6) | (b2 & 0x3F)


def encode_addr(addr: int) -> bytes:
    """12-bit encoded (any screen up to 4095 positions)."""
    return bytes([CODE[(addr >> 6) & 0x3F], CODE[addr & 0x3F]])


def ebcdic(data: bytes | list[int]) -> str:
    return bytes(b for b in data if b not in (0x00,)).decode("cp037", errors="replace")


def shape(text: str) -> str:
    out = []
    for c in text[:64]:
        out.append("9" if c.isdigit() else ("X" if c.isupper() else "x") if c.isalpha() else c)
    return "".join(out)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Telnet:
    """Split one direction of a telnet stream into 3270 records (ended by IAC EOR); note the
    terminal type and whether TN3270E is in use."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.buf = bytearray()
        self.state = "data"
        self.sub = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        records = []
        for b in data:
            if self.state == "data":
                if b == IAC:
                    self.state = "iac"
                else:
                    self.buf.append(b)
            elif self.state == "iac":
                if b == IAC:
                    self.buf.append(IAC)
                    self.state = "data"
                elif b == EOR:
                    records.append(bytes(self.buf))
                    self.buf.clear()
                    self.state = "data"
                elif b == SB:
                    self.sub.clear()
                    self.state = "sb"
                elif b in (WILL, WONT, DO, DONT):
                    self.state = "opt"
                else:
                    self.state = "data"
            elif self.state == "opt":
                self.state = "data"
            elif self.state == "sb":
                if b == IAC:
                    self.state = "sb_iac"
                else:
                    self.sub.append(b)
            elif self.state == "sb_iac":
                if b == SE:
                    self.conn.subnegotiation(bytes(self.sub))
                    self.state = "data"
                else:
                    self.sub.append(b)
                    self.state = "sb"
        return records


class Screen:
    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.resize(rows, cols)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.size = rows * cols
        self.buf: list[int] = [0] * self.size
        self.attrs: dict[int, int] = {}  # position of a field attribute -> attribute byte
        self.cursor = 0

    # -- fields -------------------------------------------------------------------------------

    def fields(self) -> list[dict]:
        """Every field: {"attr_pos", "start", "len", "protected", "numeric", "hidden",
        "intensified"}. An unformatted screen is one unprotected field."""
        if not self.attrs:
            return [{"attr_pos": None, "start": 0, "len": self.size, "protected": False,
                     "numeric": False, "hidden": False, "intensified": False}]  # fmt: skip
        pos = sorted(self.attrs)
        out = []
        for i, p in enumerate(pos):
            nxt = pos[(i + 1) % len(pos)]
            start = (p + 1) % self.size
            length = (nxt - start) % self.size if len(pos) > 1 else self.size - 1
            a = self.attrs[p]
            out.append({"attr_pos": p, "start": start, "len": length, "protected": bool(a & 0x20),
                        "numeric": bool(a & 0x10), "hidden": a & 0x0C == 0x0C,
                        "intensified": a & 0x0C == 0x08})  # fmt: skip
        return out

    def field_at(self, addr: int) -> dict | None:
        for f in self.fields():
            off = (addr - f["start"]) % self.size
            if off < f["len"] or (f["len"] == 0 and addr == f["start"]):
                return f
        return None

    def text(self, start: int, length: int) -> str:
        return ebcdic([self.buf[(start + i) % self.size] for i in range(length)])

    def rc(self, addr: int) -> tuple[int, int]:
        return addr // self.cols + 1, addr % self.cols + 1

    # -- host to terminal ---------------------------------------------------------------------

    def write(self, data: bytes, alternate: tuple[int, int]) -> str | None:
        """Apply one outbound 3270 record. Returns the command applied ("ew", "w", "eau") or None."""
        if not data:
            return None
        cmd = data[0]
        if cmd in WSF:
            done = None
            i = 1
            while i + 3 <= len(data):
                ln = (data[i] << 8) | data[i + 1]
                if ln < 3:
                    break
                if data[i + 2] == 0x40 and i + 4 < len(data):  # Outbound 3270DS
                    done = self.write(data[i + 4 : i + ln], alternate) or done
                i += ln
            return done
        if cmd in EAU:
            for f in self.fields():
                if not f["protected"]:
                    for k in range(f["len"]):
                        self.buf[(f["start"] + k) % self.size] = 0
            return "eau"
        if cmd in ERASE_WRITE or cmd in ERASE_WRITE_ALT:
            rows, cols = alternate if cmd in ERASE_WRITE_ALT else (24, 80)
            self.resize(rows, cols)
            kind = "ew"
        elif cmd in WRITE:
            kind = "w"
        else:
            return None
        # data[1] is the WCC; a Write starts at the cursor, an Erase/Write at the first position
        self._orders(data[2:], 0 if kind == "ew" else self.cursor)
        return kind

    def _orders(self, b: bytes, addr: int) -> None:  # noqa: C901 - one switch over the orders
        i, n = 0, len(b)
        while i < n:
            o = b[i]
            if o == 0x11 and i + 2 < n:  # SBA
                addr = decode_addr(b[i + 1], b[i + 2]) % self.size
                i += 3
            elif o == 0x1D and i + 1 < n:  # SF
                self.attrs[addr] = b[i + 1]
                self.buf[addr] = 0
                addr = (addr + 1) % self.size
                i += 2
            elif o in (0x29, 0x2C) and i + 1 < n:  # SFE, MF
                count = b[i + 1]
                attr = None
                for k in range(count):
                    j = i + 2 + 2 * k
                    if j + 1 < n and b[j] == 0xC0:
                        attr = b[j + 1]
                if o == 0x29:
                    self.attrs[addr] = attr or 0
                    self.buf[addr] = 0
                    addr = (addr + 1) % self.size
                elif attr is not None and (addr - 1) % self.size in self.attrs:
                    self.attrs[(addr - 1) % self.size] = attr
                i += 2 + 2 * count
            elif o == 0x28:  # SA
                i += 3
            elif o == 0x13:  # IC
                self.cursor = addr
                i += 1
            elif o == 0x05:  # PT: to the next unprotected field
                nxt = [f["start"] for f in self.fields() if not f["protected"]
                       and (f["start"] - addr) % self.size > 0]  # fmt: skip
                if nxt:
                    addr = min(nxt, key=lambda s: (s - addr) % self.size)
                i += 1
            elif o == 0x3C and i + 3 < n:  # RA
                stop = decode_addr(b[i + 1], b[i + 2]) % self.size
                ch, step = b[i + 3], 4
                if ch == 0x08 and i + 4 < n:
                    ch, step = b[i + 4], 5
                while True:
                    self.buf[addr] = ch
                    addr = (addr + 1) % self.size
                    if addr == stop:
                        break
                i += step
            elif o == 0x12 and i + 2 < n:  # EUA
                stop = decode_addr(b[i + 1], b[i + 2]) % self.size
                while addr != stop:
                    f = self.field_at(addr)
                    if f is not None and not f["protected"] and addr not in self.attrs:
                        self.buf[addr] = 0
                    addr = (addr + 1) % self.size
                i += 3
            elif o == 0x08 and i + 1 < n:  # GE
                self.buf[addr] = b[i + 1]
                addr = (addr + 1) % self.size
                i += 2
            else:
                self.buf[addr] = o
                addr = (addr + 1) % self.size
                i += 1

    # -- terminal to host ---------------------------------------------------------------------

    def read(self, data: bytes) -> tuple[int, int | None, list[tuple[int, str]]] | None:
        """One inbound record: (aid, cursor, [(field start, text)]); it is applied to the buffer."""
        if not data:
            return None
        aid = data[0]
        if aid == 0x88:  # structured field: a query reply, not a key
            return None
        if aid in SHORT_READ or len(data) < 3:
            return aid, None, []
        cursor = decode_addr(data[1], data[2]) % self.size
        changed: list[tuple[int, list[int]]] = []
        i = 3
        cur: list[int] | None = None
        while i < len(data):
            if data[i] == 0x11 and i + 2 < len(data):
                cur = []
                changed.append((decode_addr(data[i + 1], data[i + 2]) % self.size, cur))
                i += 3
            else:
                if cur is None:  # unformatted: data from the start of the screen
                    cur = []
                    changed.append((0, cur))
                cur.append(data[i])
                i += 1
        out = []
        for start, raw in changed:
            f = self.field_at(start)
            if f is not None:
                for k in range(f["len"]):
                    self.buf[(f["start"] + k) % self.size] = raw[k] if k < len(raw) else 0
            out.append((start, ebcdic(raw)))
        self.cursor = cursor
        return aid, cursor, out


class Connection:
    """One terminal session through the proxy."""

    def __init__(self, recorder: Recorder, number: int) -> None:
        self.rec = recorder
        self.number = number
        self.screen = Screen()
        self.alternate = (24, 80)
        self.tn3270e = False
        self.terminal = None
        self.awaiting = True  # the first screen after connecting is a step
        self.lock = threading.Lock()

    def subnegotiation(self, sub: bytes) -> None:
        if not sub:
            return
        if sub[0] == TN3270E:
            self.tn3270e = True
        if sub[0] == TTYPE and len(sub) > 1 and sub[1] == 0:  # TERMINAL-TYPE IS
            self.terminal = sub[2:].decode("ascii", "replace")[:40]
            m = re.search(r"3278-(\d)", self.terminal)
            if m and m.group(1) in MODELS:
                self.alternate = MODELS[m.group(1)]
        if sub[0] == TN3270E and len(sub) > 3 and sub[1] == 2 and sub[2] == 4:  # DEVICE-TYPE IS
            name = sub[3:].split(b"\x01")[0].decode("ascii", "replace")
            m = re.search(r"3278-(\d)", name)
            if m and m.group(1) in MODELS:
                self.alternate = MODELS[m.group(1)]

    def _payload(self, record: bytes) -> bytes | None:
        if self.tn3270e:
            if len(record) < 5 or record[0] != 0x00:  # only 3270-DATA
                return None
            return record[5:]
        return record

    def outbound(self, record: bytes) -> None:
        data = self._payload(record)
        if not data:
            return
        with self.lock:
            kind = self.screen.write(data, self.alternate)
            if kind in ("ew", "w") and (self.awaiting or kind == "ew"):
                self.awaiting = False
                self.rec.screen_step(self)

    def inbound(self, record: bytes) -> None:
        data = self._payload(record)
        if not data:
            return
        with self.lock:
            # the fields as the host defined them, before the terminal's changes are applied
            before = {f["start"]: f for f in self.screen.fields()}
            got = self.screen.read(data)
            if got is None:
                return
            aid, cursor, changed = got
            for start, text in changed:
                f = before.get(start) or self.screen.field_at(start)
                self.rec.field_step(self, f, start, text)
            self.rec.aid_step(self, aid, cursor)
            self.awaiting = True


class Recorder:
    """Writes the steps of every connection to one recording folder."""

    def __init__(self, out_dir: Path, app_key: str, on_step: Callable[[dict], None] | None = None,
                 journal=None):  # fmt: skip
        self.out = Path(out_dir)
        self.journal = journal  # twinlab2_recorder.workstation.journal.Journal of the session, or None
        (self.out / "screens").mkdir(parents=True, exist_ok=True)
        self.app_key = app_key
        self.on_step = on_step
        self.seq = 0
        self.lock = threading.Lock()
        self.file = (self.out / "steps.jsonl").open("a", encoding="utf-8")
        self.typed: set[str] = set()  # values typed in this session, in memory only
        self.connections = 0
        self.notes: list[str] = []
        self.started = _now()

    def close(self) -> None:
        with self.lock:
            self.file.close()
            (self.out / "mainframe.json").write_text(json.dumps({
                "format": "twinlab-mainframe/1", "recorder": "twinlab2_recorder.workstation.tn3270",
                "app": self.app_key, "started": self.started, "ended": _now(),
                "steps": self.seq, "connections": self.connections, "notes": self.notes,
                "privacy": "protected text kept with digit tokens and typed values masked; "
                           "unprotected values as shapes; non-display fields keep nothing",
            }, indent=1), encoding="utf-8")  # fmt: skip
            self.typed.clear()

    def _clean(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        for value in sorted(self.typed, key=len, reverse=True):
            if len(value) >= 2 and value in text:
                text = text.replace(value, shape(value))
        return DIGIT_TOKEN.sub(lambda m: shape(m.group(0)), text)

    def _label(self, conn: Connection, f: dict | None) -> str | None:
        """The protected text right before an input field on the same row: its label."""
        if f is None or f.get("attr_pos") is None:
            return None
        s = conn.screen
        start_row, start_col = s.rc(f["start"])
        prev = None
        for g in s.fields():
            if (
                g["protected"]
                and g["len"]
                and (f["attr_pos"] - (g["start"] + g["len"])) % s.size <= 3
            ):
                prev = g
        if prev is None:
            return None
        row, _ = s.rc(prev["start"])
        if row != start_row:
            return None
        text = s.text(prev["start"], prev["len"])
        text = re.sub(r"[\s.:_=>\-]+$", "", re.sub(r"\s+", " ", text)).strip()
        text = self._clean(text)
        return text[:40] or None

    def _title(self, conn: Connection) -> str | None:
        s = conn.screen
        for row in range(s.rows):
            parts = []
            for f in s.fields():
                r, _ = s.rc(f["start"])
                if r == row + 1 and f["protected"] and not f["hidden"]:
                    t = s.text(f["start"], min(f["len"], s.cols)).strip()
                    if t:
                        parts.append(t)
            if parts:
                return self._clean(" ".join(parts))[:60]
        return None

    def _screen_info(self, conn: Connection) -> dict:
        s = conn.screen
        layout = [(f["start"], f["len"], f["protected"]) for f in s.fields()]
        sid = hashlib.sha1(json.dumps([s.rows, s.cols, layout]).encode()).hexdigest()[:12]
        return {"id": f"scr:{sid}", "title": self._title(conn), "rows": s.rows, "cols": s.cols}

    def _write(self, step: dict, screen_file: dict | None = None) -> None:
        with self.lock:
            if self.file.closed:
                return
            self.seq += 1
            step = {"seq": self.seq, "ts": _now(), "surface": "mainframe", "app": self.app_key,
                    **step, "url": None, "dom": None}  # fmt: skip
            capture.stamp(step, "mainframe", step["ts"])
            if screen_file is not None:
                rel = f"screens/{self.seq:04d}.json"
                (self.out / rel).write_text(
                    json.dumps(screen_file, ensure_ascii=False), encoding="utf-8"
                )
                step["tree"] = rel
            self.file.write(json.dumps(step, ensure_ascii=False) + "\n")
            self.file.flush()
            if self.journal is not None:
                self.journal.event(step, self.out.name)
                self.journal.step(step, self.out.name)
        if self.on_step is not None:
            try:
                self.on_step(step)
            except Exception:  # noqa: BLE001 - bookkeeping never stops the proxy
                pass

    def screen_step(self, conn: Connection) -> None:
        s = conn.screen
        fields = []
        for f in s.fields():
            row, col = s.rc(f["start"])
            item = {"row": row, "col": col, "len": f["len"], "protected": f["protected"],
                    "numeric": f["numeric"], "hidden": f["hidden"],
                    "intensified": f["intensified"]}  # fmt: skip
            raw = s.text(f["start"], f["len"]).rstrip()
            if f["hidden"]:
                pass
            elif f["protected"]:
                item["text"] = self._clean(raw)
            elif raw.strip():
                item["shape"] = shape(raw.strip())
            fields.append(item)
        info = self._screen_info(conn)
        crow, ccol = s.rc(s.cursor)
        self._write({"action": "screen", "connection": conn.number, "screen": info},
                    {"screen": info, "cursor": {"row": crow, "col": ccol}, "fields": fields})  # fmt: skip

    def field_step(self, conn: Connection, f: dict | None, start: int, text: str) -> None:
        s = conn.screen
        row, col = s.rc(start)
        value = text.strip()
        step = {"action": "type", "connection": conn.number, "screen": self._screen_info(conn),
                "field": {"row": row, "col": col, "len": f["len"] if f else None,
                          "label": self._label(conn, f)}}  # fmt: skip
        if f is not None and f["hidden"]:
            step["secret"] = True
        elif value:
            step["typed_shape"] = shape(value)
            with self.lock:
                self.typed.add(value)
        else:
            step["blank"] = True
        self._write(step)

    def aid_step(self, conn: Connection, aid: int, cursor: int | None) -> None:
        step = {"action": "aid", "connection": conn.number, "key": AIDS.get(aid, f"AID-{aid:02X}"),
                "screen": self._screen_info(conn)}  # fmt: skip
        if cursor is not None:
            row, col = conn.screen.rc(cursor)
            step["cursor"] = {"row": row, "col": col}
        self._write(step)


class Proxy(threading.Thread):
    """Listen on 127.0.0.1:`listen`, forward to `host:port` (TLS when `tls`), record both ways."""

    def __init__(self, host: str, port: int, out_dir: Path, *, listen: int = 0, tls: bool = False,
                 tls_verify: bool = True, app_key: str | None = None,
                 stop: Callable[[], bool] | None = None,
                 on_step: Callable[[dict], None] | None = None, journal=None) -> None:  # fmt: skip
        super().__init__(name="tl-tn3270", daemon=True)
        self.host, self.port, self.tls, self.tls_verify = host, int(port), tls, tls_verify
        self.app_key = app_key or f"mainframe:{host.lower()}:{port}"
        self.recorder = Recorder(out_dir, self.app_key, on_step, journal=journal)
        self.stop_fn = stop or (lambda: False)
        self._stopped = threading.Event()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", int(listen)))
        self.server.listen(4)
        self.server.settimeout(0.5)
        self.listen = self.server.getsockname()[1]
        self.threads: list[threading.Thread] = []
        self.sockets: list[socket.socket] = []

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.listen}"

    def stop(self) -> None:
        self._stopped.set()

    def stopping(self) -> bool:
        return self._stopped.is_set() or self.stop_fn()

    def _upstream(self) -> socket.socket:
        raw = socket.create_connection((self.host, self.port), timeout=20)
        if not self.tls:
            return raw
        ctx = ssl.create_default_context()
        if not self.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(raw, server_hostname=self.host)

    def run(self) -> None:
        try:
            while not self.stopping():
                try:
                    client, _ = self.server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                self.recorder.connections += 1
                number = self.recorder.connections
                try:
                    upstream = self._upstream()
                except OSError as exc:
                    self.recorder.notes.append(f"connection {number}: the host could not be reached: "
                                               f"{type(exc).__name__}")  # fmt: skip
                    client.close()
                    continue
                conn = Connection(self.recorder, number)
                self.sockets += [client, upstream]
                for src, dst, feed in (
                    (upstream, client, conn.outbound),
                    (client, upstream, conn.inbound),
                ):
                    t = threading.Thread(
                        target=self._pump, args=(src, dst, conn, feed), daemon=True
                    )
                    t.start()
                    self.threads.append(t)
        finally:
            self.server.close()
            for s in self.sockets:
                try:
                    s.close()
                except OSError:
                    pass
            for t in self.threads:
                t.join(2)
            self.recorder.close()

    def _pump(self, src: socket.socket, dst: socket.socket, conn: Connection, feed) -> None:
        telnet = Telnet(conn)
        src.settimeout(0.5)
        try:
            while not self.stopping():
                try:
                    data = src.recv(65536)
                except TimeoutError:
                    continue
                except (OSError, ssl.SSLError):
                    break
                if not data:
                    break
                dst.sendall(data)  # forwarded first: recording never delays the terminal
                try:
                    for record in telnet.feed(data):
                        feed(record)
                except Exception as exc:  # noqa: BLE001 - a record we cannot read is skipped
                    self.recorder.notes.append(f"connection {conn.number}: a record was not read: "
                                               f"{type(exc).__name__}")  # fmt: skip
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
