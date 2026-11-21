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


def pytest_report_header(config):
    return f"portal database: {support.ENGINE}" + (f" ({support.URL.split('@')[-1]})" if support.URL else "")
