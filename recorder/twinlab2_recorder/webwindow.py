"""The recorder's window: the page in `twinlab2_recorder/ui/` shown in Microsoft Edge WebView2
(pywebview), drawing twinlab2_recorder.app.RecorderApp.

The page asks `Bridge.snapshot(version)` a few times a second and redraws what changed; its
buttons call the Bridge's other methods. pywebview runs each call on a thread of its own; the
Bridge hands it to the window thread (`Loop.call`), so the app's state is only ever touched
there. The page and the Bridge are tested without WebView2: the page in headless Chromium with
the same Bridge behind it (tests/recorder/test_window_page.py).
"""

from __future__ import annotations

from pathlib import Path
import sys

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

    # pairing and settings
    def pair(self, url: str, code: str, name: str):
        return self._do(self._app.on_pair, url, code, name)

    def settings(self, values: dict):
        self._do(self._app.set_settings, dict(values or {}))

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


def run(cfg: st.Settings, ca_bundle: str | None) -> None:
    """Show the window until it closes."""
    try:
        import webview
    except ImportError:
        message_box("This build of TwinLab Recorder has no window (pywebview is missing). Use the commands: "
                    "TwinLabRecorder --help.")  # fmt: skip
        return
    loop = Loop().start()
    host = WebHost()
    app = loop.call(lambda: RecorderApp(cfg, ca_bundle, loop, host).start())
    host.app, host.loop = app, loop
    window = webview.create_window("TwinLab Recorder", url=(UI / "index.html").as_uri(), js_api=Bridge(app, loop, host),
                                   width=1040, height=720, min_size=(600, 460), background_color="#F4F7FA")  # fmt: skip
    host.window = window
    window.events.closing += host.closing
    try:
        webview.start(
            gui="edgechromium",
            private_mode=True,
            storage_path=str(cfg.folder / "webview"),
        )
    except Exception as exc:  # noqa: BLE001 - no WebView2 runtime on this PC
        message_box(f"TwinLab Recorder needs the Microsoft Edge WebView2 Runtime, which is part of Windows 10 "
                    f"and 11 (it may have been removed on this PC). Ask IT to install it ({WEBVIEW2_URL}).\n\n"
                    f"{type(exc).__name__}: {exc}")  # fmt: skip
    finally:
        host.window = None  # gone with webview.start
        if not app.state.get("destroyed"):
            loop.call(app.destroy)
        loop.stop()
