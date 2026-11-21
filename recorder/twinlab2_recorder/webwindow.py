"""The recorder's window: the page in `twinlab2_recorder/ui/` shown in Microsoft Edge WebView2
(pywebview), drawing twinlab2_recorder.app.RecorderApp.

The page asks `Bridge.snapshot(version)` a few times a second and redraws what changed; its
buttons call the Bridge's other methods. pywebview runs each call on a thread of its own; the
Bridge hands it to the window thread (`Loop.call`), so the app's state is only ever touched
there. The page and the Bridge are tested without WebView2: the page in headless Chromium with
the same Bridge behind it (tests/recorder/test_window_page.py).

**Without WebView2** (removed from a PC, an old Windows 10 image), the same page opens in
Microsoft Edge's app mode (`msedge --app=...`, a window without tabs or address bar), served by
`PageServer` on 127.0.0.1: the page calls the Bridge with `fetch("/api/<name>")`, with a token
that only that window has (in the address's #fragment, never sent as part of a request) and the
Host checked against DNS rebinding. Closing that window cannot be refused: when it goes during a
recording it is opened again, so the person sees the recording and can stop it.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from twinlab2_recorder import settings as st
from twinlab2_recorder.app import Loop, RecorderApp

UI = Path(__file__).resolve().parent / "ui"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


class Bridge:
    """What the page may call, as `window.pywebview.api.<name>(...)`. Only public methods are
    exposed (pywebview leaves out names that start with an underscore)."""

    def __init__(self, app: RecorderApp, loop: Loop, host=None) -> None:
        self._app, self._loop, self._host = app, loop, host

    def _do(self, fn, *args):
        return self._loop.call(fn, *args)

    # what to draw
    def snapshot(self, since: int = -1):
        return self._do(
            lambda: None if self._app.version == since else self._app.snapshot()
        )

    def pair_defaults(self):
        return self._do(self._app.pair_defaults)

    # navigation
    def poll(self):
        self._do(self._app.poll_portal)

    def nav(self, page: str):
        self._do(self._app.nav, page)

    def record_tab(self, tab: str):
        self._do(self._app.set_record_tab, tab)

    # recording
    def select_offered(self, session_id):
        self._do(self._app.select_offered, session_id)

    def start_offered(self):
        self._do(self._app.on_start_offered)

    def start_own(self, name: str = "", app_ids: list | None = None, extra: str = ""):
        self._do(self._app.on_start_own, name, list(app_ids or []), extra)

    def stop(self):
        self._do(self._app.on_stop)

    def pause(self):
        self._do(self._app.on_pause)

    def resume(self):
        self._do(self._app.on_resume)

    # recordings and uploads
    def select_recording(self, session_id):
        self._do(self._app.select_recording, session_id)

    def details(self):
        self._do(self._app.on_details)

    def send(self):
        self._do(self._app.on_send)

    def upload(self):
        self._do(self._app.on_upload)

    def retry(self, job_id: str):
        self._do(self._app.on_retry, job_id)

    def retry_all(self):
        self._do(self._app.on_retry_all)

    def transfer(self, job_id: str):
        self._do(self._app.on_transfer, job_id)

    # pairing and settings
    def pair(self, url: str, code: str, name: str):
        return self._do(self._app.on_pair, url, code, name)

    def settings(self, values: dict):
        self._do(self._app.set_settings, dict(values or {}))

    def check_updates(self):
        self._do(self._app.check_updates)

    def install_update(self):
        self._do(self._app.on_install_update)

    def open_profile(self):
        self._do(self._app.on_profile)

    def propose(self):
        folder = None
        if self._host is not None and hasattr(self._host, "choose_folder"):
            folder = self._host.choose_folder(
                "The UiPath project folder (with Main.xaml)"
            )  # waits: not on the loop
        self._do(self._app.on_propose, folder)

    # the exclude list
    def exclude_kind(self, kind: str):
        self._do(self._app.exclude_kind, kind)

    def exclude_add(self, pattern: str, kind: str | None = None):
        return self._do(lambda: self._app.exclude_add(pattern, kind))

    def exclude_remove(self, indices: list):
        self._do(self._app.exclude_remove, list(indices or []))

    def exclude_defaults(self):
        self._do(self._app.exclude_defaults)

    def exclude_pick(self):
        self._do(self._app.exclude_pick)

    # questions and messages
    def answer(self, dialog_id: int, yes: bool, chosen: list | None = None):
        self._do(self._app.answer, dialog_id, bool(yes), list(chosen or []))

    def dismiss(self, notice_id: int):
        self._do(self._app.dismiss, notice_id)

    def close(self):
        """The page's own close request (the window's X goes through `WebHost.closing`)."""
        if self._do(self._app.on_close) and self._host is not None:
            self._host.close()


class WebHost:
    """The pywebview window's powers, for the app and the bridge."""

    def __init__(self) -> None:
        self.window = None
        self.app: RecorderApp | None = None
        self.loop: Loop | None = None
        self.may_close = False

    def close(self) -> None:
        self.may_close = True
        if self.window is not None:
            self.window.destroy()

    def closing(self) -> bool:
        """The window's X: False keeps the window (a recording is finishing)."""
        if self.may_close or self.app is None:
            return True
        ok = self.loop.call(self.app.on_close)
        self.may_close = self.may_close or bool(ok)
        return self.may_close

    def choose_folder(self, title: str) -> str | None:
        import webview

        got = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return got[0] if got else None

    def warn(self, message: str) -> None:
        message_box(message, warning=True)


def message_box(text: str, warning: bool = False) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, text, "TwinLab Recorder", 0x30 if warning else 0x10
        )
    else:
        print(text, file=sys.stderr)


def webview2_version() -> str | None:
    """The installed Edge WebView2 Runtime's version (machine-wide or for this user), or None."""
    if sys.platform != "win32":
        return None
    import winreg

    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + client),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + client),
    )
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as k:
                version = winreg.QueryValueEx(k, "pv")[0]
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return str(version)
    return None


def browse_folder(title: str) -> str | None:
    """Windows' own folder picker (the Edge app window has none of its own)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p), ("pszDisplayName", wintypes.LPWSTR),
                    ("lpszTitle", wintypes.LPCWSTR), ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]  # fmt: skip

    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    ole32.CoInitialize(None)
    name = ctypes.create_unicode_buffer(260)
    info = BROWSEINFO(
        None, None, ctypes.cast(name, wintypes.LPWSTR), title, 0x41, None, 0, 0
    )
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
    if not pidl:
        return None
    path = ctypes.create_unicode_buffer(1024)
    ok = shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), path)
    ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    return path.value if ok else None


class PageServer:
    """The page and the Bridge over HTTP on 127.0.0.1 (the Edge app window's way in). Static
    files only from `ui/`; `POST /api/<name>` with a JSON list of arguments and the token in
    `X-TL-Token` calls the Bridge's public method `<name>`."""

    def __init__(self, bridge: Bridge, token: str | None = None) -> None:
        self.bridge = bridge
        self.token = token or secrets.token_urlsafe(24)
        self.last_call = time.monotonic()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self) -> bool:
                return self.headers.get("Host") in (
                    f"127.0.0.1:{outer.port}",
                    f"localhost:{outer.port}",
                )

            def _send(
                self, code: int, body: bytes, kind: str = "application/json"
            ) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(400, b"{}")
                name = self.path.split("?")[0].split("#")[0].lstrip("/") or "index.html"
                path = (UI / name).resolve()
                if UI.resolve() not in path.parents or not path.is_file():
                    return self._send(404, b"{}")
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send(
                    200,
                    path.read_bytes(),
                    kind + ("; charset=utf-8" if kind.startswith("text/") else ""),
                )

            def do_POST(self):
                if not self._host_ok() or not secrets.compare_digest(
                    self.headers.get("X-TL-Token") or "", outer.token
                ):
                    return self._send(403, b"{}")
                name = self.path.removeprefix("/api/")
                fn = (
                    getattr(outer.bridge, name, None)
                    if name.isidentifier() and not name.startswith("_")
                    else None
                )
                if fn is None or not callable(fn):
                    return self._send(404, b"{}")
                try:
                    args = json.loads(
                        self.rfile.read(int(self.headers.get("Content-Length") or 0))
                        or b"[]"
                    )
                    if not isinstance(args, list):
                        raise ValueError("arguments are a list")
                    outer.last_call = time.monotonic()
                    result = fn(*args)
                except Exception as exc:  # noqa: BLE001 - the page shows it
                    return self._send(
                        500,
                        json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                    )
                self._send(200, json.dumps(result).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="tl-page", daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/index.html#t={self.token}"

    def start(self) -> PageServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def edge_app_command(edge: str, url: str, profile: Path) -> list[str]:
    """Edge in app mode: one window, no tabs or address bar, a profile of the recorder's own (its
    own process, so closing it is seen)."""
    return [edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1040,760", "--disable-features=Translate"]  # fmt: skip


class EdgeAppHost:
    """The window's powers when it is an Edge app window."""

    def __init__(self, edge: str, profile: Path) -> None:
        self.edge, self.profile = edge, profile
        self.server: PageServer | None = None
        self.proc: subprocess.Popen | None = None
        self.closed = threading.Event()

    def open(self) -> None:
        self.proc = subprocess.Popen(
            edge_app_command(self.edge, self.server.url, self.profile)
        )

    def close(self) -> None:
        self.closed.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def choose_folder(self, title: str) -> str | None:
        return browse_folder(title)

    def warn(self, message: str) -> None:
        message_box(message, warning=True)


def run_edge_app(cfg: st.Settings, ca_bundle: str | None, why: str) -> bool:
    """The window as an Edge app window; False when there is no Edge either."""
    from twinlab2_recorder.workstation.session import find_browser

    edge = find_browser("edge")
    if edge is None:
        return False
    loop = Loop().start()
    host = EdgeAppHost(edge, cfg.folder / "window-profile")
    app = loop.call(lambda: RecorderApp(cfg, ca_bundle, loop, host).start())
    host.server = PageServer(Bridge(app, loop, host)).start()
    loop.call(app.tell, "Shown in Microsoft Edge", f"This PC has no Edge WebView2 Runtime ({why}), so the recorder's "
              "window is an Edge window. Everything works the same.")  # fmt: skip
    host.open()
    try:
        while not host.closed.is_set():
            if host.proc.poll() is not None:  # the person closed the window
                if loop.call(app.on_close):
                    break
                host.open()  # a recording runs: it stays in view (the page asks whether to stop it)
            host.closed.wait(0.5)
    finally:
        host.server.stop()
        if not app.state.get("destroyed"):
            loop.call(app.destroy)
        loop.stop()
    return True


def run(cfg: st.Settings, ca_bundle: str | None) -> None:
    """Show the window until it closes: WebView2 when the PC has it, else an Edge app window."""
    try:
        import webview
    except ImportError as exc:
        webview, why = None, f"pywebview is missing: {exc}"
    else:
        why = (
            None
            if webview2_version() or sys.platform != "win32"
            else "it is not installed"
        )
    if webview is None or why:
        if run_edge_app(cfg, ca_bundle, why or "unknown"):
            return
        message_box(f"TwinLab Recorder needs the Microsoft Edge WebView2 Runtime, or Microsoft Edge, which are "
                    f"part of Windows 10 and 11 ({why}). Ask IT to install the runtime ({WEBVIEW2_URL}).")  # fmt: skip
        return
    loop = Loop().start()
    host = WebHost()
    app = loop.call(lambda: RecorderApp(cfg, ca_bundle, loop, host).start())
    host.app, host.loop = app, loop
    window = webview.create_window("TwinLab Recorder", url=(UI / "index.html").as_uri(), js_api=Bridge(app, loop, host),
                                   width=1040, height=720, min_size=(600, 460), background_color="#F4F7FA")  # fmt: skip
    host.window = window
    window.events.closing += host.closing
    failed = None
    try:
        webview.start(
            gui="edgechromium",
            private_mode=True,
            storage_path=str(cfg.folder / "webview"),
        )
    except Exception as exc:  # noqa: BLE001 - WebView2 would not start on this PC
        failed = f"{type(exc).__name__}: {exc}"
    finally:
        host.window = None  # gone with webview.start
        if not app.state.get("destroyed"):
            loop.call(app.destroy)
        loop.stop()
    if failed and not run_edge_app(cfg, ca_bundle, f"WebView2 did not start: {failed}"):
        message_box(f"TwinLab Recorder could not open its window ({failed}). Ask IT to install the Microsoft Edge "
                    f"WebView2 Runtime ({WEBVIEW2_URL}).")  # fmt: skip
