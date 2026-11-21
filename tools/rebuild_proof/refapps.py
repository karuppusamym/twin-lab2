"""The "production" applications of the proof: Twin Lab's reference apps, run from the sibling
twin-lab checkout (read, never edited) and never shown to a builder.

    python tools/rebuild_proof/refapps.py web-a        # Orders 18751, Billing 18752, widget 18753
    python tools/rebuild_proof/refapps.py web-c        # Orders AND Billing on 18761 (by path), widget 18763
    python tools/rebuild_proof/refapps.py tn3270       # the synthetic TN3270 host on 13270

- Web: `lab/labctl/tests/fixtures/multiapp/apps.py` (in-memory Orders, Billing, exchange-rate
  widget). For case C both applications are served by ONE server on one host: requests under
  /billing go to Billing, everything else to Orders, the way a reverse proxy puts two
  applications on one host. Only Billing's session cookie is renamed (``bsid``; both apps call it
  ``sid``), so the two sign-ins do not overwrite each other on one host.
- Mainframe: `reference/mainframe/demo_host.py` (sign on, account inquiry, sign off).

Each runs until killed. The data is in memory: a restart is a fresh seed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEMO_HOST, MULTIAPP, PORTS  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Shared:
    """Orders and Billing behind one server: Billing's routes for /billing..., Orders' for the rest."""

    def __init__(self, apps, url: str, widget_url: str) -> None:
        self.billing = apps.Billing(widget_url)
        self.billing.cookie = "bsid"
        self.orders = apps.Orders(url)
        # Billing's own "/" (a redirect to /billing/start) is not reachable: the host's root is Orders'.
        self.routes = [r for r in self.billing.routes if r[1].startswith("/billing")] + self.orders.routes


def web(case: str) -> None:
    apps = load(MULTIAPP / "apps.py", "twinlab_multiapp")
    if case == "a":
        p = PORTS["a"]
        running = apps.serve((p["orders"], p["billing"], p["widget"]))
        print(f"orders {running.orders}  billing {running.billing}  widget {running.widget}", flush=True)
    else:
        p = PORTS["c"]
        _, widget_url = apps._start(apps.Widget(), p["widget"])
        url = f"http://127.0.0.1:{p['shared']}"
        apps._start(Shared(apps, url, widget_url), p["shared"])
        print(f"orders {url}/login  billing {url}/billing/start  widget {widget_url}", flush=True)


def tn3270() -> None:
    host = load(DEMO_HOST, "twinlab_demo_host")
    host.serve(PORTS["tn3270"])
    print(f"TN3270 host 127.0.0.1:{PORTS['tn3270']}", flush=True)


def main(argv) -> int:
    what = argv[1] if len(argv) > 1 else ""
    if what == "web-a":
        web("a")
    elif what == "web-c":
        web("c")
    elif what == "tn3270":
        tn3270()
    else:
        raise SystemExit(__doc__)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
