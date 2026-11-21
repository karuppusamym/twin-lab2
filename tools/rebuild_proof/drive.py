"""The scripted people of the proof: a clerk in Orders and Billing (a headless Chromium the
recorder is attached to, over DevTools) and an operator on the TN3270 host (a socket terminal
pointed at the recorder's proxy). Nothing opens a window.

Every typed value is synthetic and listed in VALUES, so the judge's replay bindings are written
from this file, never from the recording (which keeps only the shape of each value).
"""

from __future__ import annotations

import time

VALUES = {
    "username": "ana.clerk",
    "password": "Demo-Only-42",
    "search_by": "customer",
    "search_for": "Lindqvist",
    "customer_name": "Priya Okonkwo-Hale",
    "customer_email": "priya.oh@example.test",
    "customer_phone": "0113 496 0522",
    "product": "shelf",
    "bad_quantity": "0",
    "quantity": "2",
    "delivery_date": "2026-12-15",
    "notes": "Deliver to the side door",
    "bad_ref": "ORDER-7",
    "payer": "Okonkwo-Hale Ltd",
    "terms": "NET30",
    "amount": "149.00",
    "memo": "Bookshelves for the reading room",
    # mainframe
    "mf_user": "TESTER",
    "mf_bad_password": "WRONG",
    "mf_password": "DEMO123",
    "mf_account": "1000001",
    "mf_account2": "1000002",
    "mf_unknown": "1999999",
}
PAUSE = 0.6


def settle(page, seconds=PAUSE):
    try:
        page.wait_for_load_state("load", timeout=10000)
    except Exception:  # noqa: BLE001 - a page that keeps loading is still usable
        pass
    time.sleep(seconds)


def typed(page, selector, value):
    box = page.locator(selector)
    box.click()
    box.press_sequentially(value, delay=40)
    time.sleep(0.2)


def orders(page, base: str, result: dict) -> None:
    """Sign in (no password first: refused), search orders by customer, new order with quantity 0
    first (refused), corrected and saved."""
    v = VALUES
    page.goto(base.rstrip("/") + "/login")
    settle(page)
    typed(page, "#username", v["username"])
    page.click("#signIn")  # no password: "Give your user name and your password."
    settle(page)
    result["login_error"] = page.inner_text("#loginError")
    typed(page, "#password", v["password"])
    page.click("#signIn")
    settle(page)
    page.select_option("#searchBy", v["search_by"])
    time.sleep(0.3)
    typed(page, "#q", v["search_for"])
    page.click("#searchBtn")
    page.wait_for_selector("#results tbody tr")
    settle(page)
    result["search_rows_page1"] = page.locator("#results tbody tr").count()
    result["search_page_info"] = page.inner_text("#pageInfo")
    page.click("#newOrder")
    settle(page)
    typed(page, "#customerName", v["customer_name"])
    typed(page, "#customerEmail", v["customer_email"])
    typed(page, "#customerPhone", v["customer_phone"])
    page.click("#toItems")
    settle(page)
    page.select_option("#product", v["product"])
    time.sleep(0.3)
    typed(page, "#quantity", v["bad_quantity"])
    page.fill("#deliveryDate", v["delivery_date"])
    time.sleep(0.3)
    typed(page, "#notes", v["notes"])
    page.click("#saveOrder")
    settle(page)
    result["quantity_error"] = page.inner_text("#quantityError")
    page.locator("#quantity").fill("")
    typed(page, "#quantity", v["quantity"])
    page.click("#saveOrder")
    settle(page)
    result["order_number"] = page.inner_text("#savedNumber")
    result["order_total"] = page.inner_text("#savedTotal")


def billing(context, page, result: dict) -> None:
    """From the saved order, Open in billing (a new tab): Find an order with a malformed reference
    first (refused), then the new order, write the invoice and save it."""
    v = VALUES
    with context.expect_page() as new_tab:
        page.click("#openBilling")
    bill = new_tab.value
    settle(bill, 1.5)
    bill.click("[data-testid=nav-lookup]")
    settle(bill)
    typed(bill, "[data-testid=order-ref]", v["bad_ref"])
    bill.click("[data-testid=find]")
    settle(bill)
    result["ref_error"] = bill.inner_text("p.notice--error")
    bill.locator("[data-testid=order-ref]").fill("")
    typed(bill, "[data-testid=order-ref]", result["order_number"])
    bill.click("[data-testid=find]")
    settle(bill)
    typed(bill, "input[name=payer]", v["payer"])
    bill.select_option("select[name=terms]", v["terms"])
    time.sleep(0.3)
    typed(bill, "input[name=amount]", v["amount"])
    bill.check("input[name=vat]")
    time.sleep(0.3)
    typed(bill, "textarea[name=memo]", v["memo"])
    bill.click("[data-testid=save-invoice]")
    settle(bill)
    bill.wait_for_selector(".summary__value[data-field=total]")
    settle(bill)
    result["invoice_number"] = bill.inner_text(".inv-no")
    result["invoice_total"] = bill.inner_text(".summary__value[data-field=total]")
    result["invoice_order"] = bill.inner_text(".summary__value[data-field=order]")


class Mainframe:
    """The operator's terminal: Twin Lab's scripted 3270 terminal (demo_host.Terminal) on a socket."""

    def __init__(self, port: int) -> None:
        from refapps import load
        from common import DEMO_HOST

        host = load(DEMO_HOST, "twinlab_demo_host")
        self.t = host.Terminal(port)
        self.log: list[dict] = []

    def screen(self) -> list[str]:
        return [self.t.text_at(r, 1, 80).rstrip() for r in range(1, 25)]

    def key(self, key: str, expect_screen: bool = True) -> None:
        self.t.press(key, expect_screen=expect_screen)
        self.log.append({"key": key, "screen": self.screen() if expect_screen else None})
        time.sleep(0.5)

    def sign_on(self) -> None:
        v = VALUES
        self.t.type(5, 25, v["mf_user"])
        self.t.type(6, 25, v["mf_bad_password"])
        self.key("Enter")  # SIGN ON FAILED: CHECK THE PASSWORD
        self.t.type(6, 25, v["mf_password"])
        self.key("Enter")  # ACCOUNT INQUIRY

    def inquiries(self) -> None:
        v = VALUES
        for account in (v["mf_account"], v["mf_unknown"], v["mf_account2"]):
            self.t.type(4, 24, account)
            self.key("Enter")

    def sign_off(self) -> None:
        self.key("PF3")  # SIGNED OFF
        self.t.close()


def web_page(cdp: str):
    """(playwright, browser, context, page) attached to the recorder's headless browser."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp)
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    return pw, browser, context, page


def case_a(cdp: str, base: str) -> dict:
    result = {"values": VALUES}
    pw, browser, context, page = web_page(cdp)
    try:
        orders(page, base, result)
        time.sleep(1.5)
    finally:
        pw.stop()  # disconnects; the browser stays with the recorder
    return result


def case_w(cdp: str, base: str) -> dict:
    """Orders then Billing (same host, /billing) in the browser the recorder started itself."""
    result = {"values": VALUES}
    pw, browser, context, page = web_page(cdp)
    try:
        orders(page, base, result)
        billing(context, page, result)
        time.sleep(1.5)
    finally:
        pw.stop()  # disconnects; the browser stays with the recorder
    return result


def case_b(proxy_port: int) -> dict:
    m = Mainframe(proxy_port)
    m.sign_on()
    m.inquiries()
    m.sign_off()
    return {"values": VALUES, "screens": m.log}


def case_c(cdp: str, base: str, proxy_port: int) -> dict:
    """One working session across three applications: the operator signs on to the mainframe
    and checks an account, the clerk makes the order in Orders and invoices it in Billing (same
    host, another path), then two more account inquiries and sign off."""
    result = {"values": VALUES}
    m = Mainframe(proxy_port)
    m.sign_on()
    v = VALUES
    m.t.type(4, 24, v["mf_account"])
    m.key("Enter")
    pw, browser, context, page = web_page(cdp)
    try:
        orders(page, base, result)
        billing(context, page, result)
        time.sleep(1.5)
    finally:
        pw.stop()
    for account in (v["mf_unknown"], v["mf_account2"]):
        m.t.type(4, 24, account)
        m.key("Enter")
    m.sign_off()
    result["screens"] = m.log
    return result
