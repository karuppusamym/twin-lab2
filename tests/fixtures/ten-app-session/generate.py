"""Write the ten-application workstation session folder beside this file, and the references the
collectors' timeline builds from it (`../references-ten-apps.json`).

    python tests/fixtures/ten-app-session/generate.py

The folder is what a recorder session leaves on the PC: `session.json`, the session journal
(`journal.jsonl`: the applications the session saw, each desktop process with the identity
`levels.desktop_identity` gives its image and version resource), one recording per channel
(`web/`, `desktop/`, `mainframe/` `steps.jsonl`) and the capture journal (`capture/*.jsonl`) with
an `open`, an `event` per step and a clean `close` per channel. Every step carries the event id,
channel and capture time the journal stamps on it (contract 1.1). Each web step has its
after-snapshot (`web/dom/`) and each desktop step its control tree (`desktop/trees/`), whose
controls and regions carry the application's tag, plus a data row that must never reach the
references (contract 1.3: the screen structure). The references are then built
by `twinlab2_recorder.workstation.timeline.build`, the code the recorder runs at Stop: nothing in
them is written by hand. Synthetic: no real person's data.

The applications are chosen to catch separation bugs (see README.md): two web applications on one
origin told apart by path (`/billing`, `/billing-admin`), names that contain each other ("Billing",
"Billing Admin"), three applications whose recorded name is "Billing" (one web, two desktop
programs with the same window title and different executables), names with spaces and non-ASCII
letters, and one TN3270 host. Their steps are interleaved in time.

Three more web applications share `fin.example.test` at nested path prefixes: Finance Hub at
`/finance`, Accounts Receivable at `/finance/billing` and Payroll at `/finance/payroll` (contract
1.2: the recorder keeps a target's whole path prefix, the portal's longest prefix wins). A Finance Hub
step at `/finance/billing-archive` checks that a prefix matches whole segments only.
`LEGACY_STEPS` are the steps without those three: what the 1.1 recorder recorded for
`../references-ten-apps-1.1.json` (built by the recorder of PR 23, kept as it was sent).
`../references-ten-apps-1.2.json` is what the 1.2 recorder of PR 25 built from this session (before
screen structure), kept as it was sent for the portal's 1.2 tests. `../references-ten-apps-1.3.json`
and `../references-ten-apps-1.4.json` are the 1.3 and 1.4 recorders' (the 1.4 one is what this
script wrote before contract 1.5), kept to check that older minors are still accepted.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (ROOT / "recorder", ROOT / "contract"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from twinlab2_recorder.workstation import timeline  # noqa: E402
from twinlab2_recorder.workstation.levels import desktop_identity  # noqa: E402

SESSION_ID = "20261120T140000Z-10a9b8c7"
BUILT = HERE.parent / "references-ten-apps.json"
ERP = "https://erp.example.test"
METER = "https://meter.example.test"
INVOICE = "https://seikyu.example.test:8443"
FIN = "https://fin.example.test"
FINANCE = ("ledger", "receivable", "payroll")  # the tags of the three applications on FIN

# The portal session the recorder was offered: four catalog applications (ids 1-4 in a new
# catalog, made in this order by the test) that the owner set up with their web addresses.
CATALOG = [
    {"name": "Billing", "key": f"web:{ERP}/billing"},
    {"name": "Billing Admin", "key": f"web:{ERP}/billing-admin"},
    {"name": "Zählerstand Portal – Nord", "key": f"web:{METER}"},
    {"name": "請求 システム", "key": f"web:{INVOICE}"},
    {"name": "Finance Hub", "key": f"web:{FIN}/finance"},
    {"name": "Accounts Receivable", "key": f"web:{FIN}/finance/billing"},
    {"name": "Payroll", "key": f"web:{FIN}/finance/payroll"},
]
TARGETS = [
    {"level": "full", "app_id": 1, "name": "Billing", "origin": ERP, "path": "/billing"},
    {"level": "full", "app_id": 2, "name": "Billing Admin", "origin": ERP, "path": "/billing-admin"},
    {"level": "full", "app_id": 3, "name": "Zählerstand Portal – Nord", "origin": METER},
    {"level": "full", "app_id": 4, "name": "請求 システム", "origin": INVOICE},
    {"level": "full", "app_id": 5, "name": "Finance Hub", "origin": FIN, "path": "/finance"},
    {"level": "full", "app_id": 6, "name": "Accounts Receivable", "origin": FIN, "path": "/finance/billing"},
    {"level": "full", "app_id": 7, "name": "Payroll", "origin": FIN, "path": "/finance/payroll"},
    # the person added these on the recorder: not in the catalog yet
    {"level": "full", "origin": "https://orders.example.test"},
    {"level": "full", "origin": "http://127.0.0.1:8081"},
    {"level": "full", "exe": "contosobilling.exe"},
    {"level": "full", "exe": "billing.exe"},
    {"level": "full", "exe": "kasse.exe"},
    {"level": "steps", "key": "mainframe:cics.example.test:3270"},
]

# Desktop processes: image path and version resource, as desktop.win.exe_info reads them.
DESKTOP = {
    21: (r"C:\Program Files\Contoso\ContosoBilling.exe",
         {"company": "Contoso Ltd", "product": "Billing", "product_version": "4.2.0.0",
          "description": "Billing"}),
    22: (r"C:\Apps\Billing\billing.exe", {"description": "Billing"}),  # no company or product
    23: (r"C:\Program Files\Müller & Söhne\Kasse.exe",
         {"company": "Müller & Söhne GmbH", "product": "Kasse – Filiale Nord", "product_version": "2.0.1.0"}),
}

# (app tag, channel, step) in the order they happen. A tag is the application's own word in
# every label, so a step that lands on another application is visible.
W, D, M = "web", "desktop", "mainframe"
LF = chr(10)


def web(url, action, selector):
    return {"url": url, "action": action, "selector": selector}


def desk(pid, aid_window, title, action, control, aid):
    return {"pid": pid, "action": action, "surface": "desktop",
            "window": {"selector": f"uia:AutomationId={aid_window};ControlType=Window", "title": title},
            "target": {"selector": f"uia:AutomationId={aid};ControlType={control}"}}


def mf(action, title, **extra):
    step = {"app": "mainframe:cics.example.test:3270", "surface": "mainframe", "action": action,
            "screen": {"title": title, "id": "s-" + title.lower().replace(" ", "")[:6]}}
    step.update(extra)
    return step


STEPS = [
    ("billing", W, web(f"{ERP}/billing/invoices", "goto", "body")),
    ("billing", W, web(f"{ERP}/billing/invoices/INV-1001", "type", "input#billingInvoiceSearch")),
    ("ledger", W, web(f"{FIN}/finance", "goto", "body")),
    ("receivable", W, web(f"{FIN}/finance/billing/invoices/42", "click", "button#receivableInvoiceOpen")),
    ("contoso", D, desk(21, "ContosoMain", "Billing", "click", "Button", "contosoPostLedger")),
    ("admin", W, web(f"{ERP}/billing-admin/rates", "click", "button#adminRateTable")),
    ("billing", W, web(f"{ERP}/billing/invoices/INV-1001", "click", "button#billingApprove")),
    ("payroll", W, web(f"{FIN}/finance/payroll/runs/7?period=2026-11", "type", "input#payrollRunHours")),
    ("mainframe", M, mf("screen", "ACCOUNT INQUIRY")),
    ("plain", D, desk(22, "BillingMainForm", "Billing", "type", "Edit", "plainCustomerRef")),
    ("admin", W, web(f"{ERP}/billing-admin/rates/edit", "type", "input#adminRateValue")),
    ("meter", W, web(f"{METER}/readings", "type", "input#meterReading")),
    ("mainframe", M, mf("type", "ACCOUNT INQUIRY", field={"label": "MAINFRAMEACCT"})),
    ("ledger", W, web(f"{FIN}/finance/billing-archive/2025", "click", "button#ledgerArchiveOpen")),
    ("orders", W, web("https://orders.example.test/orders/new", "click", "button#ordersCreate")),
    ("contoso", D, desk(21, "ContosoMain", "Billing", "type", "Edit", "contosoAmount")),
    ("invoice", W, web(f"{INVOICE}/seikyu/list", "click", "a#invoiceListOpen")),
    ("hr", W, web("http://127.0.0.1:8081/leave", "click", "button#hrLeaveSubmit")),
    ("receivable", W, web(f"{FIN}/finance/billing", "click", "button#receivableExport")),
    ("kasse", D, desk(23, "KasseMain", "Kasse – Filiale Nord", "click", "Button", "kasseCloseDay")),
    ("plain", D, desk(22, "BillingMainForm", "Billing", "click", "Button", "plainPrintStatement")),
    ("meter", W, web(f"{METER}/readings/confirm", "click", "button#meterConfirm")),
    ("mainframe", M, mf("aid", "ACCOUNT INQUIRY", key="Enter")),
    ("payroll", W, web(f"{FIN}/finance/payroll/runs/new", "click", "button#payrollRunCreate")),
    ("orders", W, web("https://orders.example.test/orders/search", "type", "input#ordersSearch")),
    ("invoice", W, web(f"{INVOICE}/seikyu/new", "type", "input#invoiceAmount")),
    ("kasse", D, desk(23, "KasseMain", "Kasse – Filiale Nord", "type", "Edit", "kasseCount")),
    ("hr", W, web("http://127.0.0.1:8081/leave/new", "type", "input#hrLeaveDays")),
    ("ledger", W, web(f"{FIN}/finance/journal/edit", "type", "input#ledgerJournalAmount")),
    ("admin", W, web(f"{ERP}/billing-admin/rates", "click", "button#adminRatePublish")),
    ("billing", W, web(f"{ERP}/billing/invoices", "click", "button#billingExport")),
]


def page(tag, step):
    """The after-snapshot a web step leaves (`dom/<seq>.html`, values blanked as the web recorder
    writes them): the step's control, a region and a table named after the application's tag, and
    a data row whose text and record id must never reach the references."""
    sel = step["selector"]
    control = ""
    if "#" in sel:
        kind, ident = sel.split("#", 1)
        control = (f'<label for="{ident}">{tag.title()} field</label><input id="{ident}" name="{ident}" required>'
                   if kind == "input" else f'<{kind} id="{ident}" href="#" type="button">Go</{kind}>')
    return ("<html><body>"
            f'<header id="{tag}Header"></header><main id="{tag}Main"><form id="{tag}Form">{control}'
            f'<button id="{tag}Save" disabled>Save</button></form>'
            f'<div id="{tag}Error" hidden>Error</div>'
            f'<table id="{tag}Rows"><thead><tr><th>Number</th><th>Customer</th></tr></thead>'
            '<tbody><tr id="row-INV-9999"><td>INV-9999</td><td>Jane Doe</td>'
            '<td><a class="open" href="#">Jane Doe</a></td></tr></tbody></table></main></body></html>')


def tree(tag, step):
    """The value-free control tree a desktop step leaves (`trees/<seq>.json`): the step's control,
    a disabled button, a grid with column headers and one data row (its name is data)."""
    aid = step["target"]["selector"].split("AutomationId=", 1)[1].split(";", 1)[0]
    control = step["target"]["selector"].rsplit("ControlType=", 1)[1]
    return {"type": "Window", "aid": step["window"]["selector"].split("AutomationId=", 1)[1].split(";", 1)[0],
            "name": "Billing", "name_is": "title", "children": [
                {"type": control, "aid": aid, "enabled": True, "visible": True,
                 **({"name": "Post", "name_is": "structure"} if control == "Button" else {"value": "X99"})},
                {"type": "Button", "aid": f"{tag}Close", "name": "Close", "name_is": "structure", "enabled": False},
                {"type": "DataGrid", "aid": f"{tag}Grid", "children": [
                    {"type": "Header", "children": [{"type": "HeaderItem", "name": "Customer", "name_is": "structure"},
                                                    {"type": "HeaderItem", "name": "Amount", "name_is": "structure"}]},
                    {"type": "DataItem", "name": "Xxxx Xxx", "name_is": "data"}]}]}


def ts(i):
    return f"2026-11-20T14:{i // 60:02d}:{i % 60:02d}.000Z"


def event_id(i):
    return f"019aa1b0-{i:04x}-7000-8000-{i:012x}"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def write(folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    end = ts(len(STEPS) * 5 + 10)
    meta = {"format": timeline.FORMAT, "id": SESSION_ID, "name": "Month-end close",
            "state": "stopped", "started": ts(0), "ended": end, "recorder": "TwinLabRecorder 0.2.0",
            "twinlab_commit": "430dfe538965b46ccbe824f8f2b4f415c5576483", "portal_session_id": 1,
            "targets": TARGETS,
            "apps": {"mainframe:cics.example.test:3270": {
                "key": "mainframe:cics.example.test:3270", "surface": "mainframe", "level": "steps",
                "display": "Kontoführung CICS"}},
            "preflight": {"checks": [{"name": "UI Automation", "ok": True, "detail": "available"},
                                     {"name": "Browser profile", "ok": True, "detail": ""},
                                     {"name": "TN3270 host", "ok": True, "detail": "cics.example.test:3270"}]},
            "notes": []}
    (folder / "session.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                         newline="\n")
    journal = []
    for pid, (path, info) in DESKTOP.items():
        ident = desktop_identity(path, info)
        journal.append({"ts": ts(1), "kind": "app", "pid": pid, "level": "full", "toolkit": "winforms",
                        **{k: v for k, v in ident.items() if v is not None and k not in ("host", "browser",
                                                                                         "description")}})
    journal.append({"ts": ts(2), "kind": "presence", "exe": "notepad.exe"})
    write_jsonl(folder / "journal.jsonl", journal)
    streams = {W: [], D: [], M: []}
    capture = {W: [], D: [], M: [], "network": []}
    for channel in capture:
        capture[channel].append({"type": "open", "channel": channel, "at": ts(0), "pid": 4242})
    for i, (_tag, channel, step) in enumerate(STEPS, 1):
        at = ts(i * 5)
        row = {"seq": len(streams[channel]) + 1, "ts": at, **step, "event_id": event_id(i), "channel": channel,
               "captured_at": at}
        if channel == W:  # the after-snapshot (contract 1.3 reads the screen structure from it)
            row["dom"] = f"dom/{row['seq']:04d}.html"
            (folder / "web" / "dom").mkdir(parents=True, exist_ok=True)
            (folder / "web" / row["dom"]).write_text(page(_tag, step), encoding="utf-8", newline=LF)
        elif channel == D:  # the value-free control tree
            row["tree"] = f"trees/{row['seq']:04d}.json"
            (folder / "desktop" / "trees").mkdir(parents=True, exist_ok=True)
            (folder / "desktop" / row["tree"]).write_text(json.dumps(tree(_tag, step)), encoding="utf-8",
                                                          newline=LF)
        streams[channel].append(row)
        capture[channel].append({"type": "event", "event_id": event_id(i), "channel": channel, "captured_at": at,
                                 "recording": channel, "action": step["action"], "seq": row["seq"], "ts": at})
        capture[channel].append({"type": "step", "event_id": event_id(i), "recording": channel, "step": row})
        if channel == W:
            capture["network"].append({"type": "har", "recording": "web", "event_id": event_id(i),
                                       "entry": {"request": {"method": "GET", "url": step["url"].split("?")[0]}}})
    for channel in capture:
        capture[channel].append({"type": "close", "at": end, "clean": True})
        write_jsonl(folder / "capture" / f"{channel}.jsonl", capture[channel])
    for channel, rows in streams.items():
        write_jsonl(folder / channel / "steps.jsonl", rows)


def tags():
    """{event_id: app tag} of every step: what the tests compare the portal's assignment with."""
    return {event_id(i): tag for i, (tag, _channel, _step) in enumerate(STEPS, 1)}


LEGACY_STEPS = [s for s in STEPS if s[0] not in FINANCE]
LEGACY = HERE.parent / "references-ten-apps-1.1.json"


def legacy_tags():
    """{event_id: app tag} of `../references-ten-apps-1.1.json`: the steps without the three
    applications on FIN, numbered as the 1.1 fixture numbered them."""
    return {event_id(i): tag for i, (tag, _channel, _step) in enumerate(LEGACY_STEPS, 1)}


def urls():
    """{event_id: the URL a web step was on}."""
    return {event_id(i): step["url"] for i, (_tag, channel, step) in enumerate(STEPS, 1) if channel == W}


def build(folder: Path) -> dict:
    """The references the collectors' timeline builds from the folder (it writes them there too)."""
    return json.loads(json.dumps(timeline.build(folder)))


def main():
    folder = HERE / "session"
    write(folder)
    box = build(folder)
    for name in ("session.jsonl", "portal-outbox.json"):  # outputs of the build, not the recording
        (folder / name).unlink()
    BUILT.write_text(json.dumps(box, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(box['apps'])} applications, {sum(len(f) for f in box['flows'].values())} steps -> {BUILT}")


if __name__ == "__main__":
    main()
