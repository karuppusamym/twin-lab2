"""The browser end-to-end check: headless Chromium (Playwright) against tools/e2e_portal.py.

Opt-in, because it needs the built web app and a Chromium:

    (cd web && npm ci && npm run build)
    python -m playwright install chromium
    TWINLAB2_E2E=1 python -m pytest tests/e2e -v

The portal runs on fakes only (fake build provider, fake GitHub, fake deployment): no network
beyond 127.0.0.1, no account, no money. `TWINLAB2_E2E_URL` (and `TWINLAB2_E2E_DEPLOYMENT`, the
fake deployment's URL it prints) points the tests at a portal already started by hand instead.

A failing test leaves a Playwright trace in test-results/e2e/<test>.zip
(`python -m playwright show-trace <file>`); CI uploads that folder.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "test-results" / "e2e"

if os.environ.get("TWINLAB2_E2E") != "1" and not os.environ.get("TWINLAB2_E2E_URL"):
    collect_ignore_glob = ["test_*.py"]  # opt-in: nothing to collect (and no Playwright import) otherwise


class Portal:
    def __init__(self, url: str, deployment: str, log: list[str]):
        self.url, self.deployment, self.log = url.rstrip("/"), deployment.rstrip("/") + "/", log


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def portal():
    if os.environ.get("TWINLAB2_E2E_URL"):
        yield Portal(os.environ["TWINLAB2_E2E_URL"], os.environ.get("TWINLAB2_E2E_DEPLOYMENT", ""), [])
        return
    if not (ROOT / "web" / "dist" / "index.html").exists():
        pytest.fail("web/dist is missing: build the web app first (cd web && npm ci && npm run build)")
    port = _free_port()
    state = tempfile.mkdtemp(prefix="tl2-e2e-")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TWINLAB2_WEB_DIST": str(ROOT / "web" / "dist")}
    process = subprocess.Popen([sys.executable, str(ROOT / "tools" / "e2e_portal.py"), "--port", str(port), "--state", state],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace")
    log: list[str] = []
    found: dict[str, str] = {}

    def read():
        for line in process.stdout:
            log.append(line.rstrip())
            match = re.search(r"deployment (http://\S+)", line)
            if match:
                found["deployment"] = match.group(1)

    threading.Thread(target=read, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while True:
        if process.poll() is not None:
            pytest.fail("the e2e portal exited:\n" + "\n".join(log[-60:]))
        try:
            with urllib.request.urlopen(url + "/livez", timeout=2) as response:
                if response.status == 200 and "deployment" in found:
                    break
        except OSError:
            pass
        if time.monotonic() > deadline:
            process.kill()
            pytest.fail("the e2e portal did not start in 90 s:\n" + "\n".join(log[-60:]))
        time.sleep(0.5)
    try:
        yield Portal(url, found["deployment"], log)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        (RESULTS).mkdir(parents=True, exist_ok=True)
        (RESULTS / "portal.log").write_text("\n".join(log) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.failed_call = report.failed
    return report


@pytest.fixture
def new_context(browser, portal, request):
    """A fresh browser context (its own cookies) per user, traced; the trace is kept on failure."""
    contexts = []

    def make():
        context = browser.new_context(base_url=portal.url, viewport={"width": 1280, "height": 900})
        context.set_default_timeout(15_000)
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        contexts.append(context)
        return context

    yield make
    failed = getattr(request.node, "failed_call", True)
    for n, context in enumerate(contexts):
        if failed:
            RESULTS.mkdir(parents=True, exist_ok=True)
            for p, page in enumerate(context.pages):  # what each page showed, readable without the trace viewer
                try:
                    text = f"{page.url}\n\n{page.locator('body').aria_snapshot()}\n"
                except Exception as error:  # noqa: BLE001 - best effort
                    text = f"{page.url}\n\n(no snapshot: {error})\n"
                (RESULTS / f"{request.node.name}-{n}-{p}.txt").write_text(text, encoding="utf-8")
            context.tracing.stop(path=str(RESULTS / f"{request.node.name}-{n}.zip"))
        else:
            context.tracing.stop()
        context.close()
