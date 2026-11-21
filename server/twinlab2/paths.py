"""Where TwinLab2 keeps local files, and how a child process finds the packages.

- `ROOT`: the repository checkout when the package runs from one (`server/twinlab2` inside it,
  as with `pip install -e server`), else the current directory. Local settings files
  (`twinlab.local.yaml`) are read from here.
- `STATE`: local state (the SQLite catalog, artifacts, recordings) unless a setting names
  another place: `TWINLAB2_STATE`, else `<ROOT>/.state`.
- `child_env(env)`: an environment whose PYTHONPATH reaches `twinlab2` and `twinlab2_contract`,
  for the worker, build runners and replays started as `python -m ...`.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
SOURCE = PACKAGE.parent  # the folder that holds the package: server/ in a checkout


def _checkout() -> Path | None:
    root = SOURCE.parent
    if (SOURCE / "pyproject.toml").is_file() and (root / "contract").is_dir():
        return root
    return None


CHECKOUT = _checkout()
ROOT = CHECKOUT or Path.cwd()
STATE = Path(os.environ.get("TWINLAB2_STATE") or ROOT / ".state")


def child_env(env: dict | None = None) -> dict:
    """A copy of `env` (default os.environ) with the package folders first on PYTHONPATH."""
    import twinlab2_contract

    env = dict(os.environ if env is None else env)
    folders = [str(SOURCE), str(Path(twinlab2_contract.__file__).resolve().parents[1])]
    rest = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p not in folders]
    env["PYTHONPATH"] = os.pathsep.join(folders + rest)
    return env
