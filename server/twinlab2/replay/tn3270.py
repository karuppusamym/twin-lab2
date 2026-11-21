# The 3270 data stream parts (Telnet, Screen, addresses, EBCDIC, the title and label rules) are
# copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91
# (lab/labctl/labctl/workstation/tn3270.py, the same code as twinlab2_recorder.workstation.tn3270),
# and the scripted terminal from reference/mainframe/demo_host.py. Licensed under the Apache
# License, Version 2.0 (LICENSE). Source and changes: docs/UPSTREAM.md.
"""Verify for a mainframe (3270) application: replay a recording against a rebuilt TN3270 host.

    python -m twinlab2.replay <recording>/mainframe --surface mainframe --target tn3270://host:port --json r.json

A scripted terminal (a socket client: nothing opens a window) connects to the rebuilt host,
answers the telnet negotiation as an IBM-3278-2-E, and walks the recorded steps:

- `type`: the value is typed into the field at the recorded row and column. It comes from
  `bindings.yaml` beside the recording (`steps: {<seq>: {value: ...}}`, or `fields:
  {<label>: [first, second, ...]}`, in the order the label is typed); without one, a synthetic
  value of the recorded shape (X → A, x → a, 9 → 1). A non-display field (a password) needs a
  bound value: without one the step is BLOCKED;
- `aid`: the recorded key (Enter, PF1-PF24, PA1-PA3, Clear) is sent with the changed fields, and
  the host's answer is awaited;
- `screen`: the live screen is compared with the recorded one, strictly: the **title line**
  (the protected text of the first row that shows any, cleaned the way the recorder cleans it:
  digit tokens and typed values as shapes) and every **input field** (row, column, length,
  numeric, non-display). Other text and data are not compared.

Business checks are the acceptance cases beside the recording (`acceptance.yaml`, the same file
the web replay reads), judged on the live screen text:

```yaml
version: 1
cases:
  - {id: wrong-password-refused, after: 4, contains: "SIGN ON FAILED", required: true}
  - {id: signed-off, after: end, contains: "SIGNED OFF"}
```

`after` is the seq of a recorded step (the screen shown once it ran) or `end`. The report has
the web replay's shape (status, verdict, steps with differences, coverage, cases, problems), so
Verify reads both the same way.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import socket
import time

IAC, SE, SB, WILL, WONT, DO, DONT, EOR = 255, 240, 250, 251, 252, 253, 254, 239
TTYPE, TN3270E = 24, 40
CODE = [0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E,
        0x4F, 0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D,
        0x5E, 0x5F, 0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0x6A, 0x6B, 0x6C,
        0x6D, 0x6E, 0x6F, 0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B,
        0x7C, 0x7D, 0x7E, 0x7F]  # fmt: skip
AIDS = {0x7D: "Enter", 0x6D: "Clear", 0x6C: "PA1", 0x6E: "PA2", 0x6B: "PA3"}
for _n, _b in enumerate([0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B, 0x7C,
                         0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C],
                        1):  # fmt: skip
    AIDS[_b] = f"PF{_n}"
KEYS = {name: code for code, name in AIDS.items()}
SHORT_READ = {0x6D, 0x6C, 0x6E, 0x6B}  # Clear and PA keys send no fields
WRITE, ERASE_WRITE, ERASE_WRITE_ALT, EAU, WSF = (
    {0xF1, 0x01},
    {0xF5, 0x05},
    {0x7E, 0x0D},
    {0x6F, 0x0F},
    {0xF3, 0x11},
)
DIGIT_TOKEN = re.compile(r"\S*\d\S*")
FILE_BINDINGS, FILE_ACCEPTANCE = "bindings.yaml", "acceptance.yaml"


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


class Telnet:
    """Split the host's telnet stream into 3270 records (ended by IAC EOR)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.state = "data"
        self.sub = bytearray()
        self.tn3270e = False

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
                    if self.sub[:1] == bytes([TN3270E]):
                        self.tn3270e = True
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

    def addr(self, row: int, col: int) -> int:
        return ((row - 1) * self.cols + (col - 1)) % self.size

    def write(self, data: bytes, alternate: tuple[int, int] = (24, 80)) -> str | None:
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


def clean(text: str, typed: set[str]) -> str:
    """Protected text as the recorder keeps it: whitespace folded, values typed in this
    session and every token holding a digit as shapes."""
    text = re.sub(r"\s+", " ", text).strip()
    for value in sorted(typed, key=len, reverse=True):
        if len(value) >= 2 and value in text:
            text = text.replace(value, shape(value))
    return DIGIT_TOKEN.sub(lambda m: shape(m.group(0)), text)


def title(screen: Screen, typed: set[str]) -> str | None:
    """The recorder's screen title: the protected, displayed text of the first row that has any."""
    for row in range(screen.rows):
        parts = []
        for f in screen.fields():
            r, _ = screen.rc(f["start"])
            if r == row + 1 and f["protected"] and not f["hidden"]:
                t = screen.text(f["start"], min(f["len"], screen.cols)).strip()
                if t:
                    parts.append(t)
        if parts:
            return clean(" ".join(parts), typed)[:60]
    return None


def inputs(screen: Screen) -> list[list]:
    """The input (unprotected) fields: [row, col, len, numeric, hidden]."""
    out = []
    for f in screen.fields():
        if f["protected"]:
            continue
        row, col = screen.rc(f["start"])
        out.append([row, col, f["len"], f["numeric"], f["hidden"]])
    return out


def recorded_inputs(tree: dict) -> list[list]:
    return [[f["row"], f["col"], f["len"], bool(f.get("numeric")), bool(f.get("hidden"))]
            for f in tree.get("fields") or [] if not f.get("protected")]


def screen_text(screen: Screen) -> str:
    """The displayed text, row by row (non-display fields left out)."""
    hidden = set()
    for f in screen.fields():
        if f["hidden"]:
            hidden.update((f["start"] + k) % screen.size for k in range(f["len"]))
    rows = []
    for r in range(screen.rows):
        chars = []
        for c in range(screen.cols):
            a = r * screen.cols + c
            chars.append(" " if a in hidden or a in screen.attrs else ebcdic([screen.buf[a]]) or " ")
        rows.append("".join(chars).rstrip())
    return "\n".join(rows)


def layout_id(screen: Screen) -> str:
    layout = [(f["start"], f["len"], f["protected"]) for f in screen.fields()]
    return "scr:" + hashlib.sha1(json.dumps([screen.rows, screen.cols, layout]).encode()).hexdigest()[:12]


class Closed(ConnectionError):
    """The host closed the connection."""


class Terminal:
    """A scripted 3270 terminal on a socket: answers the telnet negotiation, keeps the screen,
    types into fields by position and presses keys. No window, no emulator."""

    def __init__(self, host: str, port: int, timeout: float = 15.0, settle: float = 0.25) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.timeout, self.settle = timeout, settle
        self.screen = Screen()
        self.telnet = Telnet()
        self.changed: dict[int, str] = {}
        self.closed = False
        self.wait_screen()

    def _answer(self, data: bytes) -> None:
        i, out = 0, bytearray()
        while i < len(data) - 1:
            if data[i] == IAC and data[i + 1] in (DO, WILL) and i + 2 < len(data):
                option = data[i + 2]
                if option == TN3270E:  # plain TN3270 only: decline TN3270E
                    out += bytes([IAC, WONT if data[i + 1] == DO else DONT, option])
                else:
                    out += bytes([IAC, WILL if data[i + 1] == DO else DO, option])
                i += 3
            elif data[i] == IAC and data[i + 1] == SB and data[i + 2 : i + 4] == bytes([TTYPE, 1]):
                out += bytes([IAC, SB, TTYPE, 0]) + b"IBM-3278-2-E" + bytes([IAC, SE])
                i += 4
            else:
                i += 1
        if out:
            self.sock.sendall(bytes(out))

    def _recv(self, timeout: float) -> bytes | None:
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            return None
        except OSError as exc:
            self.closed = True
            raise Closed(str(exc)) from exc
        if not data:
            self.closed = True
            raise Closed("the host closed the connection")
        return data

    def _apply(self, data: bytes) -> int:
        self._answer(data)
        records = self.telnet.feed(data)
        for rec in records:
            if self.telnet.tn3270e:
                if len(rec) < 5 or rec[0] != 0x00:
                    continue
                rec = rec[5:]
            self.screen.write(rec, (24, 80))
        return len(records)

    def wait_screen(self) -> None:
        """Until the host has written a screen, then until it is quiet for `settle` seconds (a
        host may send a Write after its Erase/Write)."""
        end = time.monotonic() + self.timeout
        while True:
            data = self._recv(max(0.05, end - time.monotonic()))
            if data is None:
                raise TimeoutError(f"the host sent no screen within {self.timeout:.0f} s")
            if self._apply(data):
                break
        while True:
            try:
                data = self._recv(self.settle)
            except Closed:
                return
            if data is None:
                return
            self._apply(data)

    def type(self, row: int, col: int, value: str) -> None:
        f = self.screen.field_at(self.screen.addr(row, col))
        if f is None or f["protected"]:
            raise ValueError(f"no input field at row {row}, column {col}")
        start = self.screen.addr(row, col)
        self.changed[start] = value[: max(0, f["len"] - (start - f["start"]) % self.screen.size)]

    def press(self, key: str = "Enter", expect_screen: bool = True) -> None:
        aid = KEYS.get(key)
        if aid is None:
            raise ValueError(f"key {key!r} is not a 3270 AID key")
        if aid in SHORT_READ:
            out = bytearray([aid])
        else:
            out = bytearray([aid]) + encode_addr(self.screen.cursor)
            for start, value in self.changed.items():
                out += bytes([0x11]) + encode_addr(start) + value.encode("cp037")
        self.changed = {}
        self.sock.sendall(bytes(out).replace(bytes([IAC]), bytes([IAC, IAC])) + bytes([IAC, EOR]))
        if expect_screen:
            self.wait_screen()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --- the replay ----------------------------------------------------------------------------------


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def synthetic(shape_text: str | None) -> str:
    """A synthetic value of a recorded shape: X -> A, x -> a, 9 -> 1, the rest kept."""
    return "".join("A" if c == "X" else "a" if c == "x" else "1" if c == "9" else c
                   for c in (shape_text or "").rstrip("…"))  # fmt: skip


class Values:
    """The values the replay types, from bindings.yaml beside the recording."""

    def __init__(self, data: dict):
        self.by_step = {int(k): str((v or {}).get("value")) for k, v in (data.get("steps") or {}).items()
                        if isinstance(v, dict) and v.get("value") is not None}
        self.by_label = {str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
                         for k, v in (data.get("fields") or {}).items()}
        self.used: dict[str, int] = {}

    def value(self, step: dict) -> tuple[str | None, str]:
        """(value, where it came from); (None, why) when a secret field has no bound value."""
        if step["seq"] in self.by_step:
            return self.by_step[step["seq"]], "bound"
        label = (step.get("field") or {}).get("label") or ""
        values = self.by_label.get(label)
        if values:
            n = self.used.get(label, 0)
            self.used[label] = n + 1
            return values[min(n, len(values) - 1)], "bound"
        if step.get("secret"):
            return None, (f"a non-display field ({label or 'no label'}): bind its value in {FILE_BINDINGS} "
                          f"(fields: {{{label or 'LABEL'}: [...]}})")
        if step.get("blank"):
            return "", "blank as recorded"
        return synthetic(step.get("typed_shape")), "a synthetic value of the recorded shape"


def _parse_target(target: str) -> tuple[str, int]:
    text = str(target or "").strip()
    text = re.sub(r"^(tn3270s?|mainframe|http|https)://", "", text)
    text = re.sub(r"^mainframe:", "", text).split("/", 1)[0]
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"mainframe target {target!r}: give host:port (tn3270://host:port)")
    return host, int(port)


def replay(recording: Path, target: str, *, settle: float = 0.25, timeout: float = 15.0) -> dict:
    """Replay a mainframe recording folder against the TN3270 host at `target` (host:port).
    Returns the report (dict, the web fidelity report's shape)."""
    folder = Path(recording)
    steps = _rows(folder / "steps.jsonl")
    host, port = _parse_target(target)
    values = Values(_yaml(folder / FILE_BINDINGS))
    spec = _yaml(folder / FILE_ACCEPTANCE)
    cases_decl = [c for c in spec.get("cases") or [] if isinstance(c, dict)]
    reports: list[dict] = []
    problems: list[str] = []
    typed: set[str] = set()
    texts: dict = {}  # seq -> the live screen text once that step ran
    term: Terminal | None = None
    connection = None
    stopped = None
    compared = 0
    for step in steps:
        seq, action = step.get("seq"), step.get("action")
        r = {"seq": seq, "action": action, "status": "pass", "note": None, "differences": [],
             "selector": _where(step)}
        if stopped:
            r.update(status="skipped", note=stopped)
            reports.append(r)
            continue
        try:
            if term is None or step.get("connection", connection) != connection:
                if term is not None:
                    term.close()
                term = Terminal(host, port, timeout=timeout, settle=settle)
                connection = step.get("connection", connection)
            if action == "type":
                value, why = values.value(step)
                if value is None:
                    r.update(status="blocked", note=why)
                    stopped = f"not run: step {seq} was blocked"
                else:
                    f = step.get("field") or {}
                    term.type(int(f["row"]), int(f["col"]), value)
                    if len(value) >= 2:
                        typed.add(value)
                    r["note"] = why
            elif action == "aid":
                key = step.get("key") or "Enter"
                nxt = next((s for s in steps if s.get("seq", 0) > seq), None)
                expect = nxt is not None and nxt.get("action") == "screen" and nxt.get("connection") == connection
                try:
                    term.press(key, expect_screen=expect)
                except Closed:
                    if expect:
                        raise
                r["note"] = f"pressed {key}"
            elif action == "screen":
                compared += 1
                diffs = _compare(step, folder, term.screen, typed)
                if diffs:
                    r.update(status="fail", differences=diffs)
            else:
                r.update(status="skipped", note=f"action {action} is not replayed")
        except (Closed, TimeoutError, OSError, ValueError) as exc:
            r.update(status="fail", note=f"{type(exc).__name__}: {exc}",
                     differences=[{"step": seq, "element": "(host)", "check": "answer",
                                   "expected": "a screen", "actual": str(exc)[:200]}])
            stopped = f"not run: step {seq} failed ({type(exc).__name__})"
        if term is not None and not term.closed:
            texts[seq] = screen_text(term.screen)
        reports.append(r)
    end_text = screen_text(term.screen) if term is not None else ""
    if term is not None:
        term.close()
    cases = _cases(cases_decl, texts, end_text, reports)
    return _report(folder, f"tn3270://{host}:{port}", reports, cases, problems, compared, spec)


def _where(step: dict) -> str | None:
    if step.get("action") == "type":
        f = step.get("field") or {}
        return f"field {f.get('label') or '?'} at {f.get('row')},{f.get('col')}"
    if step.get("action") == "aid":
        return step.get("key")
    if step.get("action") == "screen":
        return (step.get("screen") or {}).get("title")
    return None


def _compare(step: dict, folder: Path, live: Screen, typed: set[str]) -> list[dict]:
    seq = step.get("seq")
    diffs = []
    want_title = (step.get("screen") or {}).get("title")
    got_title = title(live, typed)
    if want_title != got_title:
        diffs.append({"step": seq, "element": "(title line)", "check": "title", "expected": want_title,
                      "actual": got_title})
    from twinlab2.replay.fidelity import twin_label_required
    from twinlab2_contract import package

    # the twin's label is protected text on a row the recorded screen leaves empty: it is neither
    # the title line nor an input field, so the comparison never sees it; it must be shown
    if twin_label_required() and package.TWIN_LABEL not in screen_text(live):
        diffs.append({"step": seq, "element": "(AI generated Twin App label)", "check": "twin_label",
                      "expected": package.TWIN_LABEL, "actual": None})
    tree = {}
    if step.get("tree") and (folder / step["tree"]).is_file():
        tree = json.loads((folder / step["tree"]).read_text(encoding="utf-8"))
    if tree:
        want, got = recorded_inputs(tree), inputs(live)
        if want != got:
            diffs.append({"step": seq, "element": "(input fields)",
                          "check": "input fields (row, col, len, numeric, hidden)", "expected": want, "actual": got})
    return diffs


def _cases(decl: list[dict], texts: dict, end_text: str, steps: list[dict]) -> list[dict]:
    ran = {r["seq"]: r for r in steps}
    out = []
    for n, c in enumerate(decl, 1):
        cid = str(c.get("id") or f"case-{n}")
        want = c.get("contains")
        after = c.get("after", "end")
        item = {"id": cid, "check": "text", "required": bool(c.get("required", True)), "after": str(after),
                "note": c.get("note")}
        if not isinstance(want, str) or not want:
            item.update(status="BLOCKED", note="a mainframe case needs `contains:` (text the screen shows)")
        elif str(after).lower() == "end":
            item["status"] = "PASS" if want in end_text else "FAIL"
        else:
            try:
                seq = int(after)
            except (TypeError, ValueError):
                item.update(status="BLOCKED", note=f"after: {after!r} is neither a step number nor end")
                out.append(item)
                continue
            if seq not in texts or (ran.get(seq) or {}).get("status") not in ("pass", None):
                item.update(status="BLOCKED", note=f"step {seq} did not run")
            else:
                item["status"] = "PASS" if want in texts[seq] else "FAIL"
        if item["status"] == "FAIL":
            item["note"] = (item.get("note") or "") + f" (the screen does not show {want!r})"
        out.append(item)
    return out


def _report(folder: Path, base: str, steps: list[dict], cases: list[dict], problems: list[str],
            compared: int, spec: dict) -> dict:
    failed = [r for r in steps if r["status"] == "fail"]
    blocked = [r for r in steps if r["status"] == "blocked"]
    skipped = [r for r in steps if r["status"] == "skipped" and (r.get("note") or "").startswith("not run")]
    replayed = sum(1 for r in steps if r["status"] in ("pass", "fail"))
    if failed:
        problems.append(f"{len(failed)} step(s) failed, first step {failed[0]['seq']} ({failed[0]['action']})")
    if blocked:
        problems.append(f"step {blocked[0]['seq']} was blocked: {blocked[0]['note']}")
    if not replayed:
        problems.append("no step was replayed: a replay of nothing never passes")
    structural = "blocked" if blocked or not replayed else "fail" if failed or skipped else "pass"
    status = structural
    required = [c for c in cases if c["required"]]
    if status == "pass" and any(c["status"] == "FAIL" for c in required):
        status = "fail"
        problems.append("required acceptance case(s) failed: " + ", ".join(c["id"] for c in required if c["status"] == "FAIL"))
    elif status == "pass" and any(c["status"] == "BLOCKED" for c in required):
        status = "blocked"
    verdict = ("PASS WITHIN DECLARED SCOPE" if status == "pass" else "FAIL" if status == "fail" else "BLOCKED")
    return {
        "recording": folder.parent.name if folder.name == "mainframe" else folder.name, "surface": "mainframe",
        "base_url": base, "status": status, "verdict": verdict, "steps": steps, "problems": problems,
        "coverage": {"steps": len(steps), "replayed": replayed, "compared": compared, "failed": len(failed),
                     "skipped": len(skipped), "unsupported": sum(1 for r in steps if r["status"] == "skipped") - len(skipped)},
        "categories": {"replay": {"status": {"pass": "PASS", "fail": "FAIL"}.get(structural, "BLOCKED"),
                                  "note": "title line and input fields (row, col, len, numeric, hidden) per screen"}},
        "cases": cases, "visual": [],
        "requirements": {"declared": [c["id"] for c in cases], "file": FILE_ACCEPTANCE if spec else None},
    }
