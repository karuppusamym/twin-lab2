# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Windows desktop applications: record a person using one through UI Automation, turn the
recording into the app model and the brief, and verify a rebuilt demo app against it.

The web path (twinlab2_recorder.web.recorder, Twin Lab's har) reads HTML; this one reads the UI Automation tree that
Windows builds for every window. Bots (UiPath, pywinauto, WinAppDriver) find desktop controls by
the same properties: AutomationId, Name, ControlType, ClassName, FrameworkId and the ancestor
path. See docs/09-desktop.md.

Only the recorder and `verify` need Windows and the optional extra (`pip install -e ".[desktop]"`,
which installs comtypes). Everything that reads a recording (model, split, brief, comparison)
is plain Python and runs anywhere.
"""

from __future__ import annotations

import sys

SURFACE = "desktop"
FORMAT = "twinlab-desktop-recording/1"


class DesktopUnavailable(RuntimeError):
    """The recorder or verify was asked for on a machine that cannot run them."""


def require() -> None:
    """Raise DesktopUnavailable with a clear message unless UI Automation can be used here."""
    if sys.platform != "win32":
        raise DesktopUnavailable(
            "desktop recording and verify need Windows (UI Automation); this is "
            f"{sys.platform}. Reading recordings, building models and briefs works everywhere."
        )
    try:
        if "comtypes" not in sys.modules:
            # UI Automation calls event handlers on its own threads: the process must join the
            # multi-threaded apartment before comtypes initialises COM, or events never arrive.
            sys.coinit_flags = 0
        import comtypes  # noqa: F401
    except ImportError as exc:
        raise DesktopUnavailable(
            'the desktop extra is not installed: run `pip install -e ".[desktop]"` '
            "(or `python -m pip install --user comtypes`)"
        ) from exc
