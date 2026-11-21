"""A headless Chromium with a DevTools port and a small local site, for the web recorder's own
tests (attach mode as a robot's browser is recorded). Nothing opens a visible window: Chromium is
started with --headless=new. Tests skip when Playwright's Chromium is not installed."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


def chromium_path() -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = pw.chromium.executable_path
    except Exception:  # noqa: BLE001 - Playwright or its browser missing
        return None
    return path if path and Path(path).exists() else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listens on {port}")


@contextmanager
def site(pages: dict[str, bytes]):
    """Serves `pages` ({path: html}) on 127.0.0.1; yields the base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = pages.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def headless_chromium(exe: str, size: str = "1000,700"):
    """A headless Chromium with a DevTools port; yields its CDP address."""
    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="tl2-chromium-"))
    args = [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", f"--window-size={size}"]
    if sys.platform.startswith("linux"):  # CI runners do not allow Chromium's sandbox
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    log = (profile.parent / f"{profile.name}.log").open("wb")
    proc = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=log)
    try:
        try:
            _wait_port(port)
        except RuntimeError as exc:
            log.flush()
            said = Path(log.name).read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"{exc}; Chromium exited {proc.poll()}: {said}") from None
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(profile, ignore_errors=True)
        for _ in range(20):  # a Chromium helper process can hold the log a moment longer
            try:
                Path(log.name).unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.25)


def attach_in_thread(cdp: str, out: Path, **kwargs):
    """Starts `attach()` in a thread; returns (stop event, ready event, thread, errors)."""
    from twinlab2_recorder.web import recorder

    stop, ready, errors = threading.Event(), threading.Event(), []

    def run():
        try:
            recorder.attach(cdp, out, stop=stop.is_set, on_ready=ready.set, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop, ready, thread, errors
