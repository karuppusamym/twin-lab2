"""The owner's journey through the web app, in a real (headless) browser, and what other roles get.

Seeded by tools/e2e_portal.py: Billing Desk (web, repository acme/billing) and CaseDesk
(desktop), one recording "Order to invoice" holding both, alice (owner of both, portal admin)
and bob (no role). The tests run in order and share the portal: the journey's freeze, build
and release are what the role checks look at.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

RECORDING = "Order to invoice"


def sign_in(context, portal, user: str, password: str) -> Page:
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())  # the confirm() of Publish and Remove
    page.goto(portal.url + "/")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    page.get_by_label("Your name").fill(user)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Home", level=1)).to_be_visible()
    return page


def api(page: Page, method: str, path: str, **kwargs):
    """A JSON API call with the browser's own session cookie and form token."""
    token = page.request.get("/api/v1/session").json()["csrf_token"]
    return page.request.fetch(path, method=method, headers={"X-CSRF-Token": token}, **kwargs)


def test_sign_in_is_required_and_checks_the_password(new_context, portal):
    page = new_context().new_page()
    page.goto(portal.url + "/apps/1")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert page.request.get(portal.url + "/api/v1/apps").status == 401
    page.get_by_label("Your name").fill("alice")
    page.get_by_label("Password").fill("not-her-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()


def test_owner_journey_from_workboard_to_published_release(new_context, portal):
    page = sign_in(new_context(), portal, "alice", "alice-e2e")
    tracing = page.context.tracing

    tracing.group("workboard")
    page.get_by_role("table", name="Recording workboards").get_by_role("link", name=RECORDING).click()
    expect(page.get_by_role("heading", name=RECORDING, level=1)).to_be_visible()
    board = page.get_by_role("table", name="Workboard")
    expect(board.get_by_role("row", name=re.compile(r"^Billing Desk web"))).to_be_visible()
    expect(board.get_by_role("row", name=re.compile(r"^CaseDesk desktop"))).to_be_visible()
    expect(page.get_by_text("2 applications: 2 to replicate, 0 not replicated.")).to_be_visible()
    recording_url = page.url
    tracing.group_end()

    tracing.group("triage: CaseDesk is used only")
    page.goto(portal.url + "/triage")
    expect(page.get_by_role("heading", name="Scope triage", level=1)).to_be_visible()
    page.get_by_label("Show decided applications too").click()
    expect(page.get_by_label("Show decided applications too")).to_be_checked()
    page.get_by_role("button", name="CaseDesk: not replicated (used only)").click()
    page.goto(recording_url)
    expect(page.get_by_text("2 applications: 1 to replicate, 1 not replicated.")).to_be_visible()
    tracing.group_end()

    tracing.group("review Billing Desk's steps")
    page.goto(recording_url + "?app_id=1#assign")
    assignment = page.get_by_role("form", name="Assignment")  # open: the address names the application
    expect(assignment.get_by_role("heading", name=re.compile("^Events of Billing Desk"))).to_be_visible()
    assignment.get_by_role("button", name=re.compile(r"^Select all \d+ matching")).click()
    expect(assignment.get_by_text(re.compile(r"^[1-9]\d* events? selected"))).to_be_visible()
    assignment.get_by_label("Outcome and build notes").fill("Saving a case gives the invoice its number.")
    assignment.get_by_role("button", name="Save assignment").click()
    expect(page.get_by_role("status").filter(has_text="Unsaved changes")).to_have_count(0)
    tracing.group_end()

    tracing.group("a business scenario")
    page.goto(portal.url + "/apps/1#behaviour")
    form = page.get_by_role("form", name="New scenario")
    if not form.is_visible():
        page.get_by_text("Add a scenario", exact=True).click()
    form.get_by_label("Scenario name").fill("Invoice is saved")
    form.get_by_label("Starting conditions").fill("A clerk is signed in; case 1 exists.")
    form.get_by_role("textbox", name="Step 1", exact=True).fill("Open case 1 and save it")
    linked = form.get_by_role("group", name="Recorded events that show these steps")
    linked.get_by_role("button", name=re.compile(r"^Select all [1-9]\d* matching")).click()  # Verify replays them
    expect(linked.get_by_text(re.compile(r"· [1-9]\d* selected"))).to_be_visible()
    form.get_by_label("Expected result").fill("The invoice has its number.")
    form.get_by_role("button", name="Add scenario").click()
    expect(page.get_by_text("Invoice is saved").first).to_be_visible()
    tracing.group_end()

    tracing.group("freeze the builder package")
    page.goto(portal.url + "/apps/1/build-pack/preview")
    expect(page.get_by_role("heading", name=re.compile("^Builder package preview"))).to_be_visible()
    gaps = page.get_by_role("form", name="Known gaps")
    if gaps.is_visible():
        gaps.get_by_role("textbox").fill("End-to-end check: the gaps are known.")
        gaps.get_by_role("button").click()
    approve = page.get_by_role("form", name="Approve for the builder")
    approve.get_by_label(re.compile("^Change summary")).fill("First package")
    for box in approve.get_by_role("checkbox").all():
        box.check()
    approve.get_by_role("button", name=re.compile(r"Freeze .*as revision 1")).click()
    page.wait_for_url(re.compile(r"/apps/1/packages/1$"))
    tracing.group_end()

    tracing.group("a build session with the fake provider")
    page.goto(portal.url + "/apps/1/build")
    start = page.get_by_role("form", name="New build session")
    start.get_by_label("Provider").select_option("fake")
    start.get_by_role("button", name="Start build session").click()
    page.wait_for_url(re.compile(r"/builds/\d+$"))
    expect(page.get_by_role("heading", name=re.compile(r"^Build session #\d+: Billing Desk"))).to_be_visible()
    # the e2e portal's worker finishes a fake session with the built commit after a few seconds
    verify = page.get_by_role("button", name="Verify this build")
    expect(verify).to_be_enabled(timeout=60_000)
    tracing.group_end()

    tracing.group("Verify, then publish")
    verify.click()
    page.wait_for_url(re.compile(r"/(releases|verifications)/\d+"), timeout=30_000)
    if "/verifications/" in page.url:
        page.get_by_role("link", name=re.compile("release", re.I)).first.click()
        page.wait_for_url(re.compile(r"/releases/\d+"))
    release_url = page.url
    page.get_by_text("Setup / Advanced: deployment configuration").click()
    deployment = page.get_by_role("form", name="Deployment configuration")
    deployment.get_by_label("Environment").fill("Demo")
    deployment.get_by_label("Demo URL").fill(portal.deployment)
    deployment.get_by_role("button", name="Save deployment").click()
    expect(page.get_by_role("heading", name=re.compile("^Demo"), level=3)).to_be_visible()
    again = page.get_by_role("button", name="Verify this build")
    expect(again).to_be_enabled(timeout=60_000)  # the first verification (from the session page) has finished
    again.click()
    expect(page.get_by_text(re.compile(r"Passed: Tested Verification #\d+"))).to_be_visible(timeout=90_000)
    page.reload()  # the page's heading follows the release's state on the next read
    expect(page.get_by_role("heading", level=1).get_by_text("Tested")).to_be_visible()
    page.get_by_role("button", name="Publish this release").click()
    expect(page.get_by_role("heading", level=1).get_by_text("Published")).to_be_visible()
    page.goto(portal.url + "/")
    expect(page.get_by_role("region", name="Published demos").get_by_role("link").first).to_be_visible()
    assert release_url
    tracing.group_end()


def test_roles_get_403(new_context, portal):
    bob = sign_in(new_context(), portal, "bob", "bob-e2e")

    # no role on Billing Desk: the page says so and the API answers 403
    bob.goto(portal.url + "/apps/1")
    expect(bob.get_by_role("alert")).to_be_visible()
    assert bob.request.get("/api/v1/apps/1").status == 403
    assert api(bob, "POST", "/api/v1/apps/1/scenarios", data={"name": "x", "expected": "y"}).status == 403
    # not a portal admin
    assert bob.request.get("/api/v1/admin/users").status == 403
    bob.goto(portal.url + "/admin/users")
    expect(bob.get_by_role("alert")).to_be_visible()
    assert api(bob, "POST", "/api/v1/setup/builds/repos", data={"repo": "acme/other"}).status == 403

    # alice makes bob a viewer in the web app: he reads the app, he may not freeze or build
    alice = sign_in(new_context(), portal, "alice", "alice-e2e")
    alice.goto(portal.url + "/apps/1")
    roles = alice.get_by_role("form", name="Assign role")
    roles.get_by_label("User (their sign-in name)").fill("bob")
    roles.get_by_label("Role").select_option("viewer")
    roles.get_by_role("button", name="Assign role").click()
    expect(alice.get_by_role("cell", name="bob", exact=True)).to_be_visible()

    bob.goto(portal.url + "/apps/1")
    expect(bob.get_by_role("heading", name=re.compile("Billing Desk"), level=1)).to_be_visible()
    assert bob.request.get("/api/v1/apps/1").status == 200
    assert api(bob, "POST", "/api/v1/apps/1/builds", data={
        "provider": "fake", "revision": "x" * 64, "repo": "acme/billing", "budget": 1, "request_key": "e2e-viewer"}).status == 403
    assert api(bob, "POST", "/api/v1/apps/1/scenarios", data={"name": "x", "expected": "y"}).status == 403
