"""A demo portal: the real app, seeded with seven catalog applications and one recording that
registers six more (13 in the catalog), and test fakes for everything outside it.

    python tools/demo_portal.py [--port 8782] [--state DIR] [--scale]

Build the web app first (`cd web; npm ci; npm run build`). Then open http://127.0.0.1:8782 and
sign in as one of:

- `alice` / `alice-demo`: owner of every catalog application and portal admin;
- `bob` / `bob-demo`: builder of Billing (sees its frozen packages and builds, no evidence);
- `vera` / `vera-demo`: viewer of Billing (its page and published releases only).

The portal runs in the `individual` auth mode: each of them signs in with their own password,
even on this machine (no loopback admin, no shared password).

What is seeded, all through the portal's own APIs (the agent API for the recorder's part):

- Seven catalog applications with their web addresses: Billing and Billing Admin on one host
  (told apart by path), Finance Hub, Accounts Receivable and Payroll on another (nested path
  prefixes, the longest wins), a meter-reading portal and a Japanese invoicing system.
- A paired recorder machine, a requested recording ("Month-end close") offered to it, and the
  recording it sent (contract 1.2): web, desktop and mainframe steps interleaved in time. The
  catalog applications get their steps; the desktop programs (two different ones both titled
  "Billing"), the TN3270 host and two more web origins are registered as *Discovered: decide
  scope*. The recording is `tests/fixtures/ten-app-session` (synthetic, no real data).
- Billing taken through the whole journey: reviewed events, two business scenarios, a frozen
  package, a build session with the stand-in provider, a verified release (Tested) and its
  publication.
- Accounts Receivable up to a frozen package, ready to build.

With `--scale` the catalog is filled up to 50 applications (owners, statuses and stages vary) and
a 2,000-event recording ("Bulk review") is imported, to try the catalog's search, filters and
pages and the recording review's event rows at size (`scale_seed`).

Nothing here calls a real service or costs money: the build provider is `fake`
(tests/server/fake_provider.py), GitHub is a local fake, the deployment is a local stub that
reports the built commit, and the replay is a stand-in. A worker thread works the job queue, so a
new build session started from the page also runs and finishes after a few seconds.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "server", ROOT / "contract", ROOT / "recorder", ROOT / "tests", ROOT / "tests" / "server"):
    sys.path.insert(0, str(path))

SHA = "7c41e9b2d05f3a68c1e4b97a20d6f58e3b9c1a74"  # the commit the stand-in provider "builds"
REPO = "acme/billing"
NOW = "2026-11-20T00:00:00+00:00"
USERS = {"alice": "alice-demo", "bob": "bob-demo", "vera": "vera-demo"}
PURPOSES = {
    "Billing": "Customer invoices: create, correct and send",
    "Billing Admin": "Tariffs and billing rules",
    "Zählerstand Portal – Nord": "Meter readings entered by field staff",
    "請求 システム": "Invoicing for the Japanese branch",
    "Finance Hub": "Finance landing page and reports",
    "Accounts Receivable": "Open items and dunning",
    "Payroll": "Monthly payroll runs",
}


def check(response, *codes):
    if response.status_code not in (codes or (200, 201)):
        raise SystemExit(f"seeding failed: {response.request.method} {response.request.url} -> "
                         f"{response.status_code} {response.text[:800]}")
    return response.json() if response.content and "json" in response.headers.get("content-type", "") else None


def finished(client, verification, timeout=60):
    """A verification once it ended."""
    end = time.monotonic() + timeout
    while verification["state"] in ("queued", "running"):
        if time.monotonic() > end:
            raise SystemExit(f"seeding failed: verification {verification['id']} did not end")
        time.sleep(0.05)
        verification = check(client.get(f"/api/v1/verifications/{verification['id']}"))
    from twinlab2 import verifications

    verifications.join_in_process()
    return verification


def seed(app, world, ctx, database):
    """Everything a demo needs, through the APIs, as the people who would do it."""
    from twinlab2 import builds, worker
    from twinlab2.db import connect
    from twinlab2_contract import recorder_contract
    from twinlab2_recorder.client import APP, VERSION
    from support import Client
    import test_ten_apps
    import generate as fixture

    def signed_in(user):
        client = Client(app, remote="10.0.0.9")
        check(client.post("/api/v1/session", json={"user": user, "password": USERS[user]}))
        return client

    alice = signed_in("alice")

    # the catalog: seven applications with their web addresses
    ids = {}
    for item in fixture.CATALOG:
        app_id = check(alice.post("/api/v1/apps", json={"name": item["name"], "purpose": PURPOSES[item["name"]]}))["id"]
        check(alice.post(f"/api/v1/apps/{app_id}/identities", json={"key": item["key"].removeprefix("web:")}))
        ids[item["name"]] = app_id
    billing, receivable = ids["Billing"], ids["Accounts Receivable"]
    check(alice.put(f"/api/v1/apps/{billing}/roles/bob", json={"role": "builder"}))
    check(alice.put(f"/api/v1/apps/{billing}/roles/vera", json={"role": "viewer"}))

    # a recorder machine, a requested recording, and the recording it sends
    code = check(alice.post("/api/v1/machines/pairing-codes"))["code"]
    paired = check(alice.http.post("/api/agent/pair", headers={"X-TwinLab-Recorder": f"{APP} {VERSION}"}, json={
        "contract_version": recorder_contract.CONTRACT_VERSION, "code": code, "name": "Finance workstation 7"}))
    agent = {"Authorization": f"Bearer {paired['token']}", "X-TwinLab-Recorder": f"{APP} {VERSION}"}
    machine = check(alice.get("/api/v1/machines"))["machines"][0]["id"]
    check(alice.post("/api/v1/capture-sessions", json={
        "name": "Month-end close", "machine_id": machine,
        "targets": [{"app_id": app_id, "level": "full"} for app_id in ids.values()]}))
    sent = check(alice.http.post("/api/agent/references", json=test_ten_apps.references(), headers=agent))
    recording = sent["recording_id"]
    print(f"seeded recording {recording}: {len(sent['mapped'])} applications", flush=True)

    # Billing and Accounts Receivable: review their events, write scenarios, freeze a package
    detail = check(alice.get(f"/api/v1/recordings/{recording}"))

    def events_of(app_id):
        return [s["event_id"] for a in detail["refs"]["apps"] if a["app_id"] == app_id for s in a["steps"]]

    def typed(app_id):
        return [s["event_id"] for a in detail["refs"]["apps"] if a["app_id"] == app_id for s in a["steps"]
                if s.get("action") in ("type", "select")]

    def prepare(app_id, scenarios, summary, before_freeze=None):
        events = events_of(app_id)
        check(alice.put(f"/api/v1/recordings/{recording}/assignments/{app_id}",
                        json={"events": events, "notes": "Month-end close, as recorded"}))
        for number, scenario in enumerate(scenarios):
            linked = events[number::len(scenarios)] or events
            field = scenario.pop("bind", None)  # the example field each linked typing step is bound to
            bindings = [{"event": f"{recording}#{e}", "field": field} for e in linked if e in typed(app_id)] if field else []
            check(alice.post(f"/api/v1/apps/{app_id}/scenarios",
                             json={**scenario, "events": [f"{recording}#{e}" for e in linked], "bindings": bindings}))
        if before_freeze:
            before_freeze()
        draft = check(alice.get(f"/api/v1/apps/{app_id}/package-draft"))
        return check(alice.post(f"/api/v1/apps/{app_id}/packages",
                                json={"sha256": draft["sha256"], "approve": True, "summary": summary}))

    # Orders (discovered in the recording) creates the order whose number Billing types: its
    # events and a scenario, so the interface contract between the two can name the producer
    orders = next(a["app_id"] for a in detail["refs"]["apps"]
                  if any("orders.example.test" in str(s.get("screen") or "") for s in a["steps"]))
    check(alice.put(f"/api/v1/recordings/{recording}/assignments/{orders}",
                    json={"events": events_of(orders), "notes": "Month-end close, as recorded"}))
    check(alice.post(f"/api/v1/apps/{orders}/scenarios", json={
        "name": "An order is created", "starting": "Signed in as a clerk.", "actions": "Create an order, search for it.",
        "expected": "The order is listed with its number.", "failures": "", "examples": "Order number: 1001 (synthetic)",
        "events": [f"{recording}#{e}" for e in events_of(orders)]}))

    def orders_produce_the_order_number():
        contract = check(alice.get(f"/api/v1/apps/{billing}/interfaces"))["contracts"][0]
        doc = contract["doc"]
        for ident in doc["identifiers"]:
            if ident["name"] == "order-number":
                ident.update(producer=orders, consumers=[billing])
        doc["relationships"] = [r for r in doc["relationships"] if r.get("identifier") != "order-number"] + [
            {"kind": "identifier", "identifier": "order-number", "producer": orders, "consumer": billing,
             "when_down": "Billing shows “Orders is not available” and keeps the invoice draft."}]
        doc["launch_order"] = [orders] + [a for a in doc["launch_order"] if a != orders]
        doc["reset_order"] = list(reversed(doc["launch_order"]))
        check(alice.put(f"/api/v1/apps/{billing}/interfaces/{recording}", json={"doc": doc, "expected_revision": 0}))

    package = prepare(billing, [
        {"name": "An invoice is created and sent",
         "starting": "A customer with an open order; the invoice list shows that customer's invoices.",
         "actions": "Open the invoice list, create an invoice for the order, save it, send it.",
         "expected": "The invoice has its number, status Sent, and appears at the top of the list.",
         "failures": "Saving without a customer is refused with a message; the invoice is not created.",
         "examples": "Order number: 1001\nCustomer: Example GmbH; Amount: 1,250.00 EUR (synthetic).",
         "bind": "Order number"},
        {"name": "A sent invoice is corrected",
         "starting": "A sent invoice.",
         "actions": "Open the invoice, choose Correct, change the amount, save.",
         "expected": "A correction invoice is created and linked; the original keeps its number.",
         "failures": "A paid invoice cannot be corrected: the Correct button is disabled.",
         "examples": "Order number: 1001\nAmount: 1,180.00 EUR (synthetic correction)", "bind": "Order number",
         "questions": "Does a correction need a second approver above 10,000 EUR?"},
    ], "First package: invoice creation and correction", before_freeze=orders_produce_the_order_number)
    prepare(receivable, [
        {"name": "An overdue item gets a dunning letter",
         "starting": "An open item 30 days past due.",
         "actions": "Filter open items by overdue, select the item, start dunning level 1.",
         "expected": "The item shows dunning level 1 and the letter is listed under the customer.",
         "failures": "An item with a payment plan is skipped with a note."},
    ], "Open items and dunning")

    # Billing: a build session with the stand-in provider, verified, tested and published
    check(alice.patch(f"/api/v1/apps/{billing}", json={"git_url": f"https://github.com/{REPO}.git"}))
    check(alice.post("/api/v1/setup/builds/repos", json={"repo": REPO}))
    bob = signed_in("bob")
    started = check(bob.post(f"/api/v1/apps/{billing}/builds", json={
        "provider": "fake", "revision": package["revision"], "repo": REPO, "budget": 5, "request_key": "demo-first"}))
    build_id = started["build"]["id"]
    for _ in range(3):
        worker.run(str(database), once=True, ctx=ctx, max_jobs=2, lease=5)
    with closing(connect(database)) as db:
        sid = builds.get(db, build_id)["provider_session_id"]
    world.finish(sid, SHA)
    for _ in range(3):
        worker.run(str(database), once=True, ctx=ctx, max_jobs=2, lease=5)
    check(alice.put(f"/api/v1/apps/{billing}/verification-settings", json={
        "required_checks": "", "target_platform": "web", "windows_check_pattern": "", "lab_url": "http://lab.test/",
        "launch": "python run.py"}))
    return billing, build_id


SCALE_OWNERS = ("alice", "ana", "ben", "chen", "dora")
SCALE_HOSTS = ("billing.example.test", "crm.example.test", "hr.example.test", "stock.example.test",
               "orders.example.test", "tickets.example.test", "payments.example.test", "reports.example.test")
SCALE_ACTIONS = ("click", "type", "select", "navigate", "submit", "toggle", "scroll", "check")


def scale_seed(client, apps=50, events=2000):
    """Fill the catalog up to `apps` applications and import one recording of `events` events
    (synthetic, made-up hosts), through the APIs. Returns the recording's id."""
    import json

    have = check(client.get("/api/v1/apps?limit=1"))["page"]["total"]
    for n in range(have, apps):
        owner = SCALE_OWNERS[n % len(SCALE_OWNERS)]
        app_id = check(client.post("/api/v1/apps", json={
            "name": f"Scale app {n + 1:02d}", "owner": owner,
            "purpose": f"Synthetic catalog entry {n + 1} for {SCALE_HOSTS[n % len(SCALE_HOSTS)]}"}))["id"]
        if n % 5 == 0:
            check(client.patch(f"/api/v1/apps/{app_id}", json={"status": "discovered"}))
        elif n % 7 == 0:
            check(client.patch(f"/api/v1/apps/{app_id}", json={"status": "not_replicated"}))
    lines = [json.dumps({"seq": i + 1, "action": SCALE_ACTIONS[i % len(SCALE_ACTIONS)],
                         "url": f"http://{SCALE_HOSTS[(i // 25) % len(SCALE_HOSTS)]}/step/{i + 1}",
                         "ts": f"2026-11-20T{9 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"})
             for i in range(events)]
    imported = check(client.post("/api/v1/recordings", data={"name": f"Bulk review ({events} events)"},
                                 files={"log": ("steps.jsonl", "\n".join(lines).encode(), "application/x-ndjson")}))
    return imported["id"]


def build(state, scale=False):
    """The seeded demo portal in `state`: (app, world, ctx, database), with its fakes running."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "ten-app-session"))

    import fake_provider
    from fakes import FakeGitHub
    from test_verification import Deployment, FakeGitHub as ChecksGitHub, FakeTwinLab, run

    builds_github = FakeGitHub().start()
    builds_github.add_repo(REPO, sha="b" * 40)
    checks = ChecksGitHub()
    checks.runs[SHA] = [run("build", sha=SHA, run_id=1), run("test", sha=SHA, run_id=2)]
    deployment = Deployment(SHA)
    os.environ.update({
        "TWINLAB2_GITHUB_TOKEN": builds_github.token, "TWINLAB2_GITHUB_API": builds_github.url,
        "TWINLAB2_WORKSPACES": str(state / "workspaces"), "TWINLAB2_STATE": str(state),
        "TWINLAB2_DEV_WORKER": "0", "TWINLAB2_SECRET_KEY": "demo-only", "TWINLAB2_HOSTS": "localhost,127.0.0.1",
        "TWINLAB2_BIND_HOST": "127.0.0.1",  # uvicorn.run below listens there
        # everyone signs in with their own password (alice, bob, vera), even on this machine
        "TWINLAB2_AUTH_MODE": "individual",
        # a demo portal: the stand-in build provider is offered on the Build page
        "TWINLAB2_DEMO": "1"})
    for name in ("TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "DATABASE_URL"):
        os.environ.pop(name, None)
    world = fake_provider.reset()

    from twinlab2 import auth, builds, migrations, verification
    from twinlab2.app import create_app
    from twinlab2.db import connect

    database = state / "catalog.db"
    with closing(connect(database)) as db:
        migrations.run(db)
        with db:
            for user, password in USERS.items():
                auth.save_user(db, user, password=password, is_admin=user == "alice", by="demo", now=NOW)
    app = create_app(database)
    app.config.update(GITHUB=verification.GitHub(token="demo-token", api=checks.url), TWINLAB_RUNNER=FakeTwinLab())
    ctx = builds.Context(poll_seconds=0, reconcile_delay=0, reconcile_checks=1, timeout=2)

    billing, build_id = seed(app, world, ctx, database)
    # the session branches (twinlab2/app-<id>/session-N-fake) point at the built commit
    for n in range(1, 20):
        checks.branch_heads[f"twinlab2%2Fapp-{billing}%2Fsession-{n}-fake"] = SHA
    release(app, billing, build_id, deployment)
    if scale:
        from support import Client

        alice = Client(app, remote="10.0.0.9")
        check(alice.post("/api/v1/session", json={"user": "alice", "password": USERS["alice"]}))
        recording = scale_seed(alice)
        print(f"scale: 50 applications, recording {recording} with 2000 events", flush=True)
    return app, world, ctx, database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8782)
    parser.add_argument("--state", default=None)
    parser.add_argument("--scale", action="store_true", help="50 applications and a 2,000-event recording")
    args = parser.parse_args()
    state = Path(args.state or tempfile.mkdtemp(prefix="tl2-demo-"))
    app, world, ctx, database = build(state, scale=args.scale)
    from twinlab2 import worker

    def work():
        started = {}
        while True:
            try:
                worker.run(str(database), once=True, ctx=ctx, max_jobs=4, lease=5)
                with world.lock:
                    for sid, session in world.sessions.items():
                        started.setdefault(sid, time.monotonic())
                        if session["state"] == "running" and time.monotonic() - started[sid] > 6 and not session["head"]:
                            world.finish(sid, SHA)
            except Exception as error:  # noqa: BLE001 - keep the demo worker alive
                print("worker:", error, flush=True)
            time.sleep(1.5)

    threading.Thread(target=work, daemon=True).start()
    import uvicorn

    print(f"demo portal http://127.0.0.1:{args.port} (state {state})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", proxy_headers=False)


def release(app, billing, build_id, deployment):
    """Billing's build verified on its commit (Tested through Verify), then published."""
    from support import Client

    alice = Client(app, remote="10.0.0.9")
    check(alice.post("/api/v1/session", json={"user": "alice", "password": USERS["alice"]}))
    # Verify is a job: 202, then the verification (worked in this process: TWINLAB_RUNNER is a fake)
    outcome = finished(alice, check(alice.post(f"/api/v1/apps/{billing}/verify-commit",
                                               json={"repo": REPO, "sha": SHA, "build_id": build_id}), 202))
    release_id = outcome["release_id"]
    check(alice.put(f"/api/v1/releases/{release_id}/deployments", json={"environment": "Demo", "url": deployment.url}))
    finished(alice, check(alice.post(f"/api/v1/releases/{release_id}/verify"), 202))
    verified = check(alice.get(f"/api/v1/releases/{release_id}"))
    # a build session's release is Tested only through Verify (never by hand-recorded results)
    if verified["release"]["state"] != "tested":
        raise SystemExit(f"seeding failed: Verify left Billing's release {verified['release']['state']}")
    check(alice.post(f"/api/v1/releases/{release_id}/publish"))
    print(f"Billing release {release_id}: {verified['release']['state']}, then published", flush=True)


if __name__ == "__main__":
    main()
