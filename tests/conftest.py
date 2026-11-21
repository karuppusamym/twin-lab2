"""The test helpers (support, fakes, and test modules other tests import) are imported by name
from tests/, tests/server and tests/recorder. The packages themselves come from the editable
installs (`pip install -r requirements-test.txt`), or else from the checkout's folders."""

from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for path in (ROOT / "contract", ROOT / "recorder", ROOT / "server", TESTS / "recorder", TESTS / "server", TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import support  # noqa: E402

# Outside local-dev the portal refuses to start without a secret key (auth.check_settings); the
# tests that check that refusal remove it.
import os  # noqa: E402

os.environ.setdefault("TWINLAB2_SECRET_KEY", "tests-only-secret")


def _no_console_windows():
    """On Windows, a test process without a console (started by an IDE, an agent or a service) opens
    a new console window for every console program it starts (git, python, the worker), and those
    windows flash on the desktop of whoever uses the PC. Tests start them with CREATE_NO_WINDOW
    instead. A program that asks for its own console (CREATE_NEW_CONSOLE, DETACHED_PROCESS) is left
    as it asked (the portal's own launches use CREATE_NO_WINDOW too: builds.start_dev_worker,
    providers.local_runner); GUI programs (the opt-in e2e's Notepad and CaseDesk) are not affected."""
    import subprocess

    if sys.platform != "win32" or getattr(subprocess.Popen, "_twinlab2_no_window", False):
        return
    console_flags = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        if not kwargs.get("creationflags", 0) & console_flags:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init
    subprocess.Popen._twinlab2_no_window = True


_no_console_windows()


def pytest_report_header(config):
    return f"portal database: {support.ENGINE}" + (f" ({support.URL.split('@')[-1]})" if support.URL else "")
