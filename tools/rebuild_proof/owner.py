"""Step 2 of the proof: the owner's part, through /api/v1 only, then freeze and keep each ZIP.

    python tools/rebuild_proof/owner.py a|b|c|d

For every application of the case's recording: select all its recorded events (with a note),
write the business scenarios a careful owner would write from what they saw while the session
was recorded (screens, messages, results; never the reference app's source), attach what an
owner realistically has, and freeze one package revision. The ZIP goes to
`<evidence>/<case>/packages/<slug>.zip`, with the pre-freeze draft report beside it.

What an owner attaches:
- web: the HAR of the session (the recorder's own `recording.har`, kind API), and the orders they
  can export from Orders' own search API while signed in (`orders-seed.csv`, kind Database,
  synthetic). None of the reference apps has a database, so there is no DDL to give;
- mainframe and desktop: nothing beyond the scenarios (no database, no API).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, read_json, write_json  # noqa: E402
from portal_api import Portal  # noqa: E402
import record  # noqa: E402

RUN_WEB = ("Run settings: the copy listens on the TCP port given in the PORT environment variable "
           "(default 8080) on 127.0.0.1, and keeps its data so that a restart starts again from the "
           "seed. One command starts it: python run.py (Python) or the equivalent named in the README.")


def scenarios_orders(order_no: str, shared_host: bool) -> list[dict]:
    where = ("Billing runs on the same host, under /billing (a reverse proxy sends /billing/... to "
             "Billing). 'Open in billing' opens BILLING_URL + /billing/start in a new tab; "
             "BILLING_URL is an environment variable, empty meaning the same host."
             if shared_host else
             "Billing is another application (not part of this copy). 'Open in billing' opens "
             "BILLING_URL + /billing/start in a new tab; BILLING_URL is an environment variable.")
    return [
        {"name": "A clerk signs in and finds a customer's orders",
         "starting": "Orders already holds 25 orders (attached: orders-seed.csv, exported from Orders' "
                     "own search while signed in). Any user name with any non-empty password signs in.",
         "actions": "Open the sign-in page (/login). Give the user name only and press Sign in: it is "
                    "refused. Give the password too and sign in. On Find orders choose Search by "
                    "'Customer name', type part of a name (Lindqvist) and press Search.",
         "expected": "After signing in the clerk is on Find orders (/orders/search). The search shows "
                     "the matching orders in a table, 5 rows per page, with 'Page 1 of 3' under it "
                     "and next/previous page buttons. The navigation bar has Search, New order, "
                     "Customers and Sign out.",
         "failures": "Sign in without a password: the sign-in page shows 'Give your user name and your "
                     "password.' and keeps the user name filled in.",
         "examples": "User ana.clerk (synthetic). Search by customer name: Lindqvist.",
         "notes": RUN_WEB},
        {"name": "A new order is taken",
         "starting": "Signed in, on Find orders. The highest existing order number is ORD-1025.",
         "actions": "Press New order. Customer page: name, e-mail, phone, then Next. Items page: "
                    "choose the product (Book shelf), quantity, delivery date, notes, press Save "
                    "order with quantity 0 first (refused), then quantity 2 and Save order.",
         "expected": f"The saved page shows the new order's number ({order_no}, the next number) and "
                     "its total 149.00 (2 x Book shelf at 74.50). It has the links 'Open in billing', "
                     "'Change this order' and 'Back to search'.",
         "failures": "Quantity 0: the items page is shown again with 'The quantity is a number from 1 "
                     "to 99.' next to the quantity, and nothing is saved.",
         "examples": "Customer Priya Okonkwo-Hale, priya.oh@example.test, 0113 496 0522; Book shelf x 2, "
                     "delivery 2026-12-15, notes 'Deliver to the side door' (all synthetic).",
         "questions": "What the other products cost was not seen in this session.",
         "notes": RUN_WEB + " " + where},
    ]


def scenarios_billing(order_no: str) -> list[dict]:
    return [
        {"name": "An order is invoiced",
         "starting": f"Order {order_no} was just saved in Orders. Billing has no invoice yet.",
         "actions": "From the saved order, 'Open in billing' opens Billing's overview in a new tab "
                    "(it also shows an exchange-rate box from another system, which is not part of "
                    "this copy). Choose 'Find an order', type a malformed reference (ORDER-7) and "
                    f"press Find (refused), then {order_no} and Find. On New invoice give the payer, "
                    "payment terms (30 days), amount 149.00, tick Add VAT, a memo, press Save invoice.",
         "expected": "The invoice page shows 'Invoice INV-5001' (the first invoice) and a summary: "
                     f"Order {order_no}, Payer, Payment terms, Amount 149.00, VAT added, Total 178.80 "
                     "(VAT 20%), Memo. It has a 'Find another order' link.",
         "failures": "A reference that is not ORD- and four digits: Find an order shows 'An order "
                     "reference looks like ORD-0000.' and keeps what was typed.",
         "examples": "Payer Okonkwo-Hale Ltd, memo 'Bookshelves for the reading room' (synthetic). "
                     "Payment terms offered: 15 days, 30 days, 60 days.",
         "questions": "Looking up an order that already has an invoice was not done in this session.",
         "notes": RUN_WEB + " Billing is served under /billing on the same host as Orders (a reverse "
                  "proxy sends /billing/... to it); every Billing page address starts with /billing."},
    ]


def scenarios_mainframe() -> list[dict]:
    run = ("Run settings: the copy is a TN3270 server (3270 data stream over telnet, with the "
           "TERMINAL-TYPE and END-OF-RECORD options, model 2: 24 x 80) listening on 127.0.0.1 at the "
           "TCP port given in the PORT environment variable (default 3270). One command starts it.")
    return [
        {"name": "An operator signs on (a wrong password first)",
         "starting": "Any user id signs on with any password except WRONG.",
         "actions": "Connect a 3270 terminal. On DEMO SIGN ON type the user id and a wrong password, "
                    "press Enter; then the right password and Enter.",
         "expected": "The ACCOUNT INQUIRY screen. The password field never shows what is typed.",
         "failures": "Password WRONG: DEMO SIGN ON again, with 'SIGN ON FAILED: CHECK THE PASSWORD'.",
         "examples": "User id TESTER, password DEMO123 (synthetic).", "notes": run},
        {"name": "Accounts are looked up, then sign off",
         "starting": "Signed on, on ACCOUNT INQUIRY.",
         "actions": "Type an account number and press Enter; again for an unknown number and for a "
                    "second account; PF3 signs off.",
         "expected": "Enter shows the account's NAME and BALANCE. PF3 shows SIGNED OFF.",
         "failures": "An unknown account number: 'ACCOUNT NOT FOUND', name and balance empty.",
         "examples": "1000001 ANNA SAMPLE 1,250.00; 1000002 OMAR EXAMPLE 80.15; 1999999 not found "
                     "(synthetic).", "notes": run},
    ]


def scenarios_casedesk(corrected: bool = False) -> list[dict]:
    """corrected (the live batch, case e): the owner's text after this proof found that the
    password rule was misstated for cases C and D (the session typed a wrong password first;
    only the demo password signs in)."""
    run = ("Run settings: a Windows desktop application (the original is WinForms on .NET 6), one "
           "program CaseDesk.exe built with `dotnet build -c Release`. Its data is in memory: every "
           "start shows the same cases.")
    return [
        {"name": "A case is found and resolved",
         "starting": "CaseDesk shows four cases when opened: C-1001 Ada Example (Open), C-1002 Ben "
                     "Sample (In progress), C-1003 Cleo Demo (Open, urgent), C-2001 Dan Synthetic "
                     "(Closed). " + ("Any user name signs in with the demo password 'demo'; any other password "
                                     "is refused." if corrected else "Any user name signs in with any non-empty password."),
         "actions": ("Sign in with a wrong password (refused, OK), then with the demo password. On "
                     if corrected else "Sign in with the user name only (refused, OK), then with the password. On ") +
                    "'CaseDesk - Cases' type C-100 in 'Case or name' and press Enter; Case > Refresh; "
                    "select a case and press Open. In 'Case C-...' type a short summary (Done), choose "
                    "status Resolved, tick Urgent, Save (refused, OK); write a longer summary "
                    "(Card resent by courier) and Save. Then File > Exit.",
         "expected": "The search lists the matching cases (Case No, Customer, Status, Opened). After "
                     "the second Save the case window closes and the list shows the case as Resolved.",
         "failures": ("A wrong password" if corrected else "Sign in without a password") +
                     ": a message box 'Sign in' says 'Enter your user name and "
                     "password.'. Resolved with a short summary: a message box 'Case' says 'A resolved "
                     "or closed case needs a summary of at least 10 characters.' and the case stays open.",
         "examples": "User tester (synthetic).", "notes": run},
    ]


def orders_seed(case: str) -> Path:
    """The orders an owner can export from Orders' own search API while signed in."""
    import httpx

    base = "http://127.0.0.1:18751" if case == "a" else "http://127.0.0.1:18761"
    rows = []
    with httpx.Client(base_url=base, follow_redirects=False) as c:
        c.post("/login", data={"username": "owner.export", "password": "x"})
        page = 1
        while True:
            got = c.get("/api/orders", params={"by": "customer", "q": "", "page": page}).json()
            rows += got["items"]
            if page >= got["pages"]:
                break
            page += 1
    out = EVIDENCE / case / "owner" / "orders-seed.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["number", "customer", "order_date", "status", "total"])
    w.writeheader()
    w.writerows(rows)
    out.write_text(buf.getvalue(), encoding="utf-8")
    return out


def web_steps(case: str) -> dict[str, dict]:
    folder = next((EVIDENCE / case / "recording").iterdir())
    out = {}
    for part in [folder / "web"]:
        if (part / "steps.jsonl").is_file():
            for line in (part / "steps.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    s = json.loads(line)
                    if s.get("event_id"):
                        out[s["event_id"]] = s
    return out


def split_at(events: list[str], steps: dict, marker) -> tuple[list[str], list[str]]:
    for i, e in enumerate(events):
        if marker(steps.get(e) or {}):
            return events[:i], events[i:]
    return events, []


def main(argv) -> int:
    case = argv[1] if len(argv) > 1 else ""
    if case not in record.CASES:
        raise SystemExit(__doc__)
    base = EVIDENCE / case
    driver = read_json(base / "record" / "driver.json")
    order_no = driver.get("order_number", "ORD-1026")
    procs = record.start_refapps(case) if case in ("a", "c") else []
    portal = Portal(case).start()
    summary = {}
    try:
        recs = portal.get("/api/v1/recordings")
        rec_id = (recs.get("recordings") if isinstance(recs, dict) else recs)[0]["id"]
        detail = portal.get(f"/api/v1/recordings/{rec_id}")
        steps = web_steps(case)
        for app in detail["refs"]["apps"]:
            app_id, name = app["app_id"], app["name"]
            events = [s["event_id"] for s in app["steps"]]
            got = portal.get(f"/api/v1/apps/{app_id}/packages")
            done = (got.get("packages") if isinstance(got, dict) else got) or []
            if done:  # frozen by an earlier run: keep it, only download it again
                rev = done[0]["rev"]
                got = portal.get(f"/api/v1/apps/{app_id}/scenarios")
                frozen, scen = done[0], (got.get("scenarios") if isinstance(got, dict) else got) or []
            else:
                frozen = None
            if frozen is None:
                portal.put(f"/api/v1/recordings/{rec_id}/assignments/{app_id}", json={
                    "events": events, "notes": f"{name}: every recorded step of this session belongs to it (reviewed)."})
                if name == "Orders":
                    scen = scenarios_orders(order_no, case == "c")
                    first, second = split_at(events, steps, lambda s: "/orders/new" in (s.get("url") or ""))
                    links = [first, second or events]
                    seed = orders_seed(case)
                    portal.attach(app_id, seed, "Orders as exported from the search (synthetic seed)", "Database", "synthetic")
                elif name == "Billing":
                    scen, links = scenarios_billing(order_no), [events]
                elif name == "Account Inquiry":
                    scen = scenarios_mainframe()
                    cut = next((i for i, s in enumerate(app["steps"]) if s.get("screen") == "ACCOUNT INQUIRY"), len(events))
                    links = [events[:cut] or events, events[cut:] or events]
                else:
                    scen, links = scenarios_casedesk(case == "e"), [events]
                for sc, linked in zip(scen, links):
                    portal.post(f"/api/v1/apps/{app_id}/scenarios", json={**sc, "events": [f"{rec_id}#{e}" for e in linked]})
                if app["key"].startswith("web:"):
                    har = next((base / "recording").iterdir()) / "web" / "recording.har"
                    portal.attach(app_id, har, "HAR of the recorded session (browser developer tools export)", "API", "reviewed")
                draft = portal.get(f"/api/v1/apps/{app_id}/package-draft")
                write_json(base / "packages" / f"{app_id}-draft.json", draft)
                frozen = portal.post(f"/api/v1/apps/{app_id}/packages", json={
                    "sha256": draft["sha256"], "approve": True, "summary": f"{name}: as recorded in '{detail['name']}'"})
                rev = frozen["rev"]
            data = portal.get(f"/api/v1/apps/{app_id}/packages/{rev}/download")
            slug = name.lower().replace(" ", "-")
            zpath = base / "packages" / f"{slug}.zip"
            zpath.write_bytes(data)
            summary[name] = {"app_id": app_id, "revision": rev, "events": len(events), "scenarios": len(scen),
                             "zip": str(zpath), "bytes": len(data), "sha256": frozen.get("sha256")}
            print(f"{case}: {name} rev {rev}: {len(events)} events, {len(scen)} scenarios -> {zpath.name}")
        write_json(base / "packages" / "summary.json", {"recording": rec_id, "apps": summary,
                                                        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    finally:
        portal.stop()
        record.stop(procs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
