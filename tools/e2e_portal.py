"""A local portal for the browser end-to-end check: the real app, test fakes for everything outside.

    python tools/e2e_portal.py --port 8781 [--state DIR]

- The portal (app.create_app) on a fresh SQLite catalog, serving web/dist (build it first).
- Sign-in required (TWINLAB2_AUTH_MODE=individual): alice / alice-e2e (owner and portal admin), bob / bob-e2e.
- Fakes on 127.0.0.1, never a real service: the build provider `fake` (tests/server/fake_provider.py:
  no network, no money), a GitHub for repositories and check runs, and a deployment answering
  /.twinlab/version with the commit the fake provider builds. A worker thread works the job queue,
  and a session the fake provider runs is finished with that commit after a few seconds.
- Seeded through the APIs: Billing Desk (web) and CaseDesk (desktop) with identities, a paired
  recorder machine, and one recording from it that holds both applications.
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
for path in (ROOT / "server", ROOT / "contract", ROOT / "tests", ROOT / "tests" / "server"):
    sys.path.insert(0, str(path))

SHA = "a" * 40
REPO = "acme/billing"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--state", default=None)
    args = parser.parse_args()
    state = Path(args.state or tempfile.mkdtemp(prefix="tl2-e2e-"))
    state.mkdir(parents=True, exist_ok=True)

    import fake_provider
    from fakes import FakeGitHub
    from test_verification import Deployment, FakeGitHub as ChecksGitHub, FakeTwinLab, run

    builds_github = FakeGitHub().start()
    builds_github.add_repo(REPO, sha="b" * 40)
    checks = ChecksGitHub()
    checks.runs[SHA] = [run("build", run_id=1), run("test", run_id=2)]
    for n in range(1, 10):  # the session branches (twinlab2/app-1/session-N-fake) point at the built commit
        checks.branch_heads[f"twinlab2%2Fapp-1%2Fsession-{n}-fake"] = SHA
    deployment = Deployment(SHA)
    os.environ.update({
        "TWINLAB2_GITHUB_TOKEN": builds_github.token, "TWINLAB2_GITHUB_API": builds_github.url,
        "TWINLAB2_WORKSPACES": str(state / "workspaces"), "TWINLAB2_STATE": str(state),
        "TWINLAB2_DEV_WORKER": "0", "TWINLAB2_SECRET_KEY": "e2e-only", "TWINLAB2_HOSTS": "localhost,127.0.0.1",
        # the stand-in provider `fake` is offered on the Build page only in a demo portal
        "TWINLAB2_DEMO": "1",
        # individual mode: even this machine signs in (the sign-in page is part of the check)
        "TWINLAB2_AUTH_MODE": "individual"})
    for name in ("TWINLAB2_TRUST_PROXY_USER", "TWINLAB2_PASSWORD", "DATABASE_URL"):
        os.environ.pop(name, None)
    world = fake_provider.reset()

    from twinlab2 import auth, builds, migrations, verification, worker
    from twinlab2.app import create_app
    from twinlab2.db import connect
    from support import Client
    from test_registry import references

    database = state / "catalog.db"
    with closing(connect(database)) as db:
        migrations.run(db)
        with db:
            auth.save_user(db, "alice", password="alice-e2e", is_admin=True, by="e2e", now="2026-11-20T00:00:00+00:00")
            auth.save_user(db, "bob", password="bob-e2e", by="e2e", now="2026-11-20T00:00:00+00:00")
    app = create_app(database)
    app.config.update(GITHUB=verification.GitHub(token="test-token", api=checks.url), TWINLAB_RUNNER=FakeTwinLab())

    # seed through the APIs, as alice and as the recorder
    alice = Client(app, remote="10.0.0.9")
    assert alice.post("/api/v1/session", json={"user": "alice", "password": "alice-e2e"}).status_code == 200
    billing = alice.post("/api/v1/apps", json={"name": "Billing Desk", "purpose": "Invoices for the demo"}).json()["id"]
    casedesk = alice.post("/api/v1/apps", json={"name": "CaseDesk", "purpose": "Case handling"}).json()["id"]
    alice.patch(f"/api/v1/apps/{billing}", json={"git_url": f"https://github.com/{REPO}.git"})
    alice.post(f"/api/v1/apps/{billing}/identities", json={"key": "http://127.0.0.1:8080"})
    alice.post(f"/api/v1/apps/{casedesk}/identities", json={"key": "desktop:Twin Lab reference/CaseDesk"})
    alice.put(f"/api/v1/apps/{billing}/verification-settings", json={
        "required_checks": "", "target_platform": "web", "windows_check_pattern": "", "lab_url": "http://lab.test/",
        "launch": "python run.py"})
    alice.post("/api/v1/setup/builds/repos", json={"repo": REPO})
    code = alice.post("/api/v1/machines/pairing-codes").json()["code"]
    from twinlab2_contract import recorder_contract

    token = alice.http.post("/api/agent/pair", json={"contract_version": recorder_contract.CONTRACT_VERSION,
                                                     "code": code, "name": "Recorder PC"}).json()["token"]
    sent = alice.http.post("/api/agent/references", json=references(),
                           headers={"Authorization": f"Bearer {token}"}).json()
    print("seeded recording", sent["recording_id"], "mapped", sent["mapped"], flush=True)

    ctx = builds.Context(poll_seconds=0, reconcile_delay=0, reconcile_checks=1, timeout=2)

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
            except Exception as error:  # noqa: BLE001 - keep the e2e worker alive
                print("worker:", error, flush=True)
            time.sleep(1.5)

    threading.Thread(target=work, daemon=True).start()
    import uvicorn

    print(f"portal http://127.0.0.1:{args.port} (state {state}); deployment {deployment.url}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
