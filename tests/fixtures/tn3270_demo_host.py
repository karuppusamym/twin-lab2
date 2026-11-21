# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91
# (reference/mainframe/demo_host.py). Licensed under the Apache License, Version 2.0 (LICENSE).
# Changed for TwinLab2: imports from twinlab2.replay.tn3270, the inbound parser inlined, a
# `wrong` variant (a deliberately wrong rebuild) added, and any free port. A test helper only;
# docs/UPSTREAM.md.
"""Twin Lab's small synthetic TN3270 host (synthetic data only), for Verify's mainframe tests.

Screens: DEMO SIGN ON (user id, password: non-display), ACCOUNT INQUIRY (account number; Enter
shows the account's name and balance, PF3 signs off), SIGNED OFF. Any user id and password are
accepted except password `WRONG`, which shows a message.

`serve(wrong=True)` is a deliberately wrong rebuild: the inquiry screen's title says ACCOUNT
ENQUIRY and its account field is 9 characters long, not 10.
"""

from __future__ import annotations

import socket
import threading

from twinlab2.replay.tn3270 import AIDS, DO, EOR, IAC, SB, SE, SHORT_READ, TTYPE, WILL, Screen, Telnet
from twinlab2.replay.tn3270 import decode_addr, ebcdic, encode_addr

PROT, PROT_HI, INPUT, INPUT_NUM, SECRET = 0x60, 0xE8, 0x40, 0x50, 0x4C
ACCOUNTS = {"1000001": ("ANNA SAMPLE", "1,250.00"), "1000002": ("OMAR EXAMPLE", "80.15")}


def at(row: int, col: int, cols: int = 80) -> int:
    return (row - 1) * cols + (col - 1)


def text(s: str) -> bytes:
    return s.encode("cp037")


# The served host stands in for a rebuilt twin: it shows the "AI generated Twin App" label as a
# protected field on row 23, which none of its screens uses (twinlab2_contract.package.twin_label).
TWIN_LABEL = (23, 2, PROT, "AI generated Twin App", 21)


def build(items: list[tuple], cursor: tuple[int, int]) -> bytes:
    """An Erase/Write: items are (row, col, attribute, text, length), plus the twin's label."""
    out = bytearray([0xF5, 0xC3])  # Erase/Write, WCC: reset, restore keyboard
    for row, col, attr, s, length in [*items, TWIN_LABEL]:
        out += bytes([0x11]) + encode_addr(at(row, col)) + bytes([0x1D, attr])
        body = text(s)[:length]
        out += body
        if len(body) < length:  # end the field with a protected stop
            out += bytes([0x11]) + encode_addr(at(row, col) + 1 + length) + bytes([0x1D, PROT])
    out += bytes([0x11]) + encode_addr(at(*cursor)) + bytes([0x13])
    return bytes(out)


def signon(message: str = "") -> bytes:
    return build([(1, 30, PROT_HI, "DEMO SIGN ON", 12), (5, 10, PROT, "USERID . . .", 12),
                  (5, 24, INPUT, "", 8), (6, 10, PROT, "PASSWORD . .", 12), (6, 24, SECRET, "", 8),
                  (22, 2, PROT_HI, message, 60), (24, 2, PROT, "ENTER=SIGN ON  PF3=EXIT", 23)],
                 (5, 25))  # fmt: skip


def inquiry(account: str = "", message: str = "", wrong: bool = False) -> bytes:
    name, balance = ACCOUNTS.get(account, ("", ""))
    if account and not name:
        message = "ACCOUNT NOT FOUND"
    return build([(1, 30, PROT_HI, "ACCOUNT ENQUIRY" if wrong else "ACCOUNT INQUIRY", 15),
                  (4, 10, PROT, "ACCOUNT NO:", 11),
                  (4, 23, INPUT_NUM, account, 9 if wrong else 10), (6, 10, PROT, "NAME", 4), (6, 23, PROT, name, 30),
                  (7, 10, PROT, "BALANCE", 7), (7, 23, PROT, balance, 15),
                  (22, 2, PROT_HI, message, 60), (24, 2, PROT, "ENTER=SHOW  PF3=SIGN OFF", 24)],
                 (4, 24))  # fmt: skip


def signed_off() -> bytes:
    return build([(1, 30, PROT_HI, "SIGNED OFF", 10)], (1, 1))


def record(data: bytes) -> bytes:
    return data.replace(bytes([IAC]), bytes([IAC, IAC])) + bytes([IAC, EOR])


def read(screen: Screen, data: bytes):
    """One inbound record: (aid, [(field start, text)]) (twinlab2_recorder's Screen.read)."""
    if not data or data[0] == 0x88:
        return None
    aid = data[0]
    if aid in SHORT_READ or len(data) < 3:
        return aid, []
    changed, cur, i = [], None, 3
    while i < len(data):
        if data[i] == 0x11 and i + 2 < len(data):
            cur = []
            changed.append((decode_addr(data[i + 1], data[i + 2]) % screen.size, cur))
            i += 3
        else:
            if cur is None:
                cur = []
                changed.append((0, cur))
            cur.append(data[i])
            i += 1
    return aid, [(start, ebcdic(raw)) for start, raw in changed]


def serve_one(sock: socket.socket, wrong: bool = False) -> None:
    sock.sendall(bytes([IAC, DO, TTYPE, IAC, SB, TTYPE, 1, IAC, SE, IAC, DO, EOR, IAC, WILL, EOR,
                        IAC, DO, 0, IAC, WILL, 0]))  # fmt: skip
    telnet = Telnet()
    screen = Screen()
    state = "signon"
    sock.sendall(record(signon()))
    screen.write(signon(), (24, 80))
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return
            for rec in telnet.feed(data):
                got = read(screen, rec)
                if got is None:
                    continue
                aid, fields = got
                key = AIDS.get(aid)
                values = {screen.rc(start): value.strip() for start, value in fields}
                if key == "PF3":
                    out = signed_off() if state == "inquiry" else None
                    if out is None:
                        return
                    sock.sendall(record(out))
                    return
                if state == "signon":
                    password = values.get((6, 25), "")
                    if password.upper() == "WRONG":
                        out = signon("SIGN ON FAILED: CHECK THE PASSWORD")
                    else:
                        out, state = inquiry(wrong=wrong), "inquiry"
                else:
                    out = inquiry(values.get((4, 24), "").strip(), wrong=wrong)
                screen.write(out, (24, 80))
                sock.sendall(record(out))
    except OSError:
        return
    finally:
        sock.close()


def serve(port: int = 0, wrong: bool = False, stop: threading.Event | None = None) -> tuple[socket.socket, int]:
    """Start the host in threads; (the listening socket, its port). Close the socket to stop it."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(4)
    server.settimeout(0.5)

    def loop():
        while stop is None or not stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=serve_one, args=(client, wrong), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


def flow(port: int, host: str = "127.0.0.1") -> None:
    """Sign on (a wrong password first), look up three accounts, sign off (Twin Lab's flow, with
    the unknown account the rebuild proof used)."""
    from twinlab2.replay.tn3270 import Terminal

    t = Terminal(host, port)
    t.type(5, 25, "TESTER")
    t.type(6, 25, "WRONG")
    t.press("Enter")
    t.type(6, 25, "DEMO123")
    t.press("Enter")
    t.type(4, 24, "1000001")
    t.press("Enter")
    t.type(4, 24, "1999999")
    t.press("Enter")
    t.type(4, 24, "1000002")
    t.press("Enter")
    t.press("PF3")
    t.close()
