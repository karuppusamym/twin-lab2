"""The portal modules sit at the repository root and the tests import each other by name."""

from pathlib import Path
import sys

for path in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import support  # noqa: E402


def pytest_report_header(config):
    return f"portal database: {support.ENGINE}" + (f" ({support.URL.split('@')[-1]})" if support.URL else "")
