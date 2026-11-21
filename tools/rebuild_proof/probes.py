"""Non-happy-path behaviour: the same probes against each reference app (the oracle) and its blind
rebuild, headless. A probe passes when the rebuild shows what the reference shows: the page it
ends on (path), the error or notice messages shown, and for API probes the HTTP status.

    python tools/rebuild_proof/probes.py web a|c
    python tools/rebuild_proof/probes.py tn3270 b|c

Most probes were never recorded and are not in any package (marked `in_pack: False`): they measure
what a copy built from one recording does off the recorded path, not what the package promised.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, PORTS, write_json  # noqa: E402
import record  # noqa: E402
import replay  # noqa: E402

MSG = (".error, .field-error, .notice, .notice--error, #searchError, #noResults, [role=alert], "
       "#loginError, #quantityError")


def seen(page) -> dict:
    page.wait_for_timeout(600)
    msgs = []
    for el in page.query_selector_all(MSG):
        try:
            if el.is_visible():
                t = " ".join(el.inner_text().split())
                if t:
                    msgs.append(t)
        except Exception:  # noqa: BLE001
            pass
    from urllib.parse import urlsplit

    return {"path": urlsplit(page.url).path, "messages": sorted(set(msgs))}


def signed_in(page, base):
    page.goto(base + "/login")
    page.fill("#username", "probe.user")
    page.fill("#password", "x")
    page.click("#signIn")
    page.wait_for_load_state("load")


def orders_probes():
    def p(name, in_pack, fn):
        return {"name": name, "in_pack": in_pack, "fn": fn}

    def login_empty(page, base):
        page.goto(base + "/login"); page.click("#signIn"); page.wait_for_load_state("load"); return seen(page)

    def login_password_only(page, base):
        page.goto(base + "/login"); page.fill("#password", "x"); page.click("#signIn"); page.wait_for_load_state("load"); return seen(page)

    def search_unauth(page, base):
        page.goto(base + "/orders/search"); page.wait_for_load_state("load"); return seen(page)

    def api_unauth(page, base):
        r = page.request.get(base + "/api/orders?by=customer&q=a&page=1")
        return {"status": r.status}

    def search_empty(page, base):
        signed_in(page, base); page.select_option("#searchBy", label="Customer name"); page.click("#searchBtn"); return seen(page)

    def search_no_hits(page, base):
        signed_in(page, base); page.select_option("#searchBy", label="Customer name"); page.fill("#q", "Zzyzx"); page.click("#searchBtn"); return seen(page)

    def search_number(page, base):
        signed_in(page, base); page.select_option("#searchBy", label="Order number"); page.fill("#q", "ORD-1003"); page.click("#searchBtn")
        page.wait_for_timeout(800); return dict(seen(page), rows=page.locator("#results tbody tr").count())

    def customer_empty(page, base):
        signed_in(page, base); page.click("#newOrder"); page.wait_for_load_state("load"); page.click("#toItems"); page.wait_for_load_state("load"); return seen(page)

    def bad_email(page, base):
        signed_in(page, base); page.click("#newOrder"); page.wait_for_load_state("load"); page.fill("#customerName", "Probe Person")
        page.fill("#customerEmail", "not-an-email"); page.click("#toItems"); page.wait_for_load_state("load"); return seen(page)

    def items(page, base, qty, product="Book shelf"):
        signed_in(page, base); page.click("#newOrder"); page.wait_for_load_state("load"); page.fill("#customerName", "Probe Person")
        page.fill("#customerEmail", "p@example.test"); page.fill("#customerPhone", "0100 000 0000"); page.click("#toItems"); page.wait_for_load_state("load")
        if product:
            page.select_option("#product", label=product)
        if qty is not None:
            page.fill("#quantity", qty)
        page.click("#saveOrder"); page.wait_for_load_state("load"); return seen(page)

    return [p("sign in with nothing", False, login_empty), p("sign in with a password only", False, login_password_only),
            p("search page without signing in (permission)", False, search_unauth),
            p("orders API without signing in (permission)", False, api_unauth),
            p("search with no text", False, search_empty), p("search with no hits", False, search_no_hits),
            p("search by order number", False, search_number), p("customer step with nothing", False, customer_empty),
            p("customer e-mail malformed", False, bad_email),
            p("quantity 100", False, lambda pg, b: items(pg, b, "100")), p("quantity 0 (recorded)", True, lambda pg, b: items(pg, b, "0")),
            p("no product chosen", False, lambda pg, b: items(pg, b, "2", product=None))]


def billing_probes():
    def lookup(page, base, ref):
        page.goto(base + "/billing/lookup"); page.fill("[data-testid=order-ref]", ref); page.click("[data-testid=find]"); page.wait_for_load_state("load"); return seen(page)

    def account_direct(page, base):
        page.goto(base + "/billing/account"); page.wait_for_load_state("load"); return seen(page)

    def invoice_form(page, base, payer, amount):
        lookup(page, base, "ORD-1011")
        if "/billing/account" not in page.url:
            return dict(seen(page), note="the lookup did not open the invoice form")
        if payer:
            page.fill("input[name=payer]", payer)
        page.fill("input[name=amount]", amount); page.click("[data-testid=save-invoice]"); page.wait_for_load_state("load"); return seen(page)

    def invoiced_twice(page, base):
        invoice_form(page, base, "Probe Ltd", "10.00"); return lookup(page, base, "ORD-1011")

    return [{"name": "lookup ORDER-7 (recorded)", "in_pack": True, "fn": lambda pg, b: lookup(pg, b, "ORDER-7")},
            {"name": "lookup lower case ord-1011", "in_pack": False, "fn": lambda pg, b: lookup(pg, b, "ord-1011")},
            {"name": "lookup empty", "in_pack": False, "fn": lambda pg, b: lookup(pg, b, "")},
            {"name": "invoice form without a lookup (direct URL)", "in_pack": False, "fn": account_direct},
            {"name": "invoice amount not a number", "in_pack": False, "fn": lambda pg, b: invoice_form(pg, b, "Probe Ltd", "twelve")},
            {"name": "invoice without payer", "in_pack": False, "fn": lambda pg, b: invoice_form(pg, b, "", "10.00")},
            {"name": "order already invoiced, looked up again", "in_pack": False, "fn": invoiced_twice}]


def run_web(base: str, probes) -> list[dict]:
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for pr in probes:
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                got = pr["fn"](page, base)
            except Exception as exc:  # noqa: BLE001
                got = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"}
            ctx.close()
            out.append({"probe": pr["name"], "in_pack": pr["in_pack"], "observed": got})
        browser.close()
    return out


def compare(ref: list[dict], new: list[dict]) -> list[dict]:
    rows = []
    for a, b in zip(ref, new):
        rows.append({"probe": a["probe"], "in_pack": a["in_pack"], "reference": a["observed"], "rebuild": b["observed"],
                     "same": a["observed"] == b["observed"]})
    return rows


def web(case: str) -> int:
    probes = [("Orders", orders_probes())] + ([("Billing", billing_probes())] if case in ("c", "w") else [])
    ref_base = "http://127.0.0.1:18751" if case == "a" else "http://127.0.0.1:18761"
    procs = record.start_refapps(case)
    try:
        ref = {app: run_web(ref_base, ps) for app, ps in probes}
    finally:
        record.stop(procs)
    procs, proxy = [], None
    try:
        if case == "a":
            app = replay.WEB_APPS["a"]["orders"]
            procs.append(replay.launch(app["build"], app["port"], {"BILLING_URL": "http://127.0.0.1:18752"}))
            base = f"http://127.0.0.1:{app['port']}"
        else:
            o, b = replay.WEB_APPS[case]["orders"], replay.WEB_APPS[case]["billing"]
            procs.append(replay.launch(o["build"], o["port"], {"BILLING_URL": f"http://127.0.0.1:{replay.C_PROXY}"}))
            procs.append(replay.launch(b["build"], b["port"]))
            proxy = replay.PathProxy(replay.C_PROXY, o["port"], b["port"])
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{replay.C_PROXY}"
        new = {app: run_web(base, ps) for app, ps in probes}
    finally:
        if proxy:
            proxy.shutdown()
        for p in procs:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    result = {app: compare(ref[app], new[app]) for app, _ in probes}
    write_json(EVIDENCE / case / "replay" / "probes-web.json", result)
    for app, rows in result.items():
        print(app, f"{sum(r['same'] for r in rows)}/{len(rows)} same")
        for r in rows:
            if not r["same"]:
                print("  DIFF", r["probe"], "| ref", json.dumps(r["reference"])[:160], "| new", json.dumps(r["rebuild"])[:160])
    return 0


def tn3270(case: str) -> int:
    from refapps import load
    from common import DEMO_HOST

    host = load(DEMO_HOST, "twinlab_demo_host")

    def screen(t):
        return [t.text_at(r, 1, 80).rstrip() for r in range(1, 25)]

    def texts(t):  # what the operator reads: the non-blank lines, spaces collapsed
        return [" ".join(line.split()) for line in screen(t) if line.strip()]

    def session(port, steps):
        try:
            t = host.Terminal(port)
            for kind, a, b in steps:
                if kind == "type":
                    t.type(a[0], a[1], b)
                else:
                    t.press(a)
            got = texts(t)
            t.close()
            return got
        except Exception as exc:  # noqa: BLE001
            return [f"error: {type(exc).__name__}: {exc}"]

    signon = [("type", (5, 25), "PROBE"), ("type", (6, 25), "PASS1"), ("key", "Enter", None)]
    probes = [
        ("empty user id and password, Enter", False, [("key", "Enter", None)]),
        ("PF3 on the sign-on screen", False, [("key", "PF3", None)]),
        ("account number empty", False, signon + [("key", "Enter", None)]),
        ("account number with letters", False, signon + [("type", (4, 24), "ABC"), ("key", "Enter", None)]),
        ("unknown account (recorded)", True, signon + [("type", (4, 24), "1999999"), ("key", "Enter", None)]),
        ("PF5 (not recorded) on inquiry", False, signon + [("key", "PF5", None)]),
    ]
    spec = replay.MF[case]
    ref_proc = record.start_refapps("b")[0]
    try:
        ref = [session(PORTS["tn3270"], steps) for _, _, steps in probes]
    finally:
        record.stop([ref_proc])
    proc = replay.launch(spec["build"], spec["port"])
    try:
        new = [session(spec["port"], steps) for _, _, steps in probes]
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    def key_lines(lines):  # title and messages: the first line and any line that is all upper-case words
        return [line for line in lines if any(w in line for w in ("SIGN", "ACCOUNT", "INVALID", "ENTER A", "NOT FOUND"))]

    rows = []
    for (name, in_pack, _), a, b in zip(probes, ref, new):
        # compare what the operator is told: the title and message lines (layout is judged elsewhere)
        rows.append({"probe": name, "in_pack": in_pack, "reference": a, "rebuild": b,
                     "same_title": (a[:1] == b[:1]), "reference_messages": key_lines(a), "rebuild_messages": key_lines(b)})
    write_json(EVIDENCE / case / "replay" / "probes-tn3270.json", rows)
    for r in rows:
        print(f"{r['probe']:40} title same={r['same_title']} | ref {r['reference'][:1]} {[m for m in r['reference_messages'] if m not in r['reference'][:1]]}"
              f" | new {r['rebuild'][:1]} {[m for m in r['rebuild_messages'] if m not in r['rebuild'][:1]]}")
    return 0


def main(argv) -> int:
    if len(argv) < 3 or argv[1] not in ("web", "tn3270"):
        raise SystemExit(__doc__)
    return web(argv[2]) if argv[1] == "web" else tn3270(argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
