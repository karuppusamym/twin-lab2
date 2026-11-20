"""The pinned Twin Lab collectors (labctl) that the recorder is built from.

`twinlab.lock.json` names one Twin Lab commit and the SHA-256 of the `lab/labctl/labctl` tree at
that commit. `fetch` puts exactly that tree in `vendor/twin-lab-<commit>/` and refuses anything
whose digest differs; the build and the recorder (when run from source) use that folder. No
sibling checkout, no branch: changing the collectors means changing the lock in a reviewed
commit.

    python -m agent.twinlab fetch                         download the pinned commit from GitHub
    python -m agent.twinlab fetch --from-checkout ../twin-lab   or take it from a local clone (offline)
    python -m agent.twinlab verify                        check the vendored folder against the lock
    python -m agent.twinlab digest <folder>               the digest of a labctl folder (to move the pin)

The digest is over file paths and contents with line endings normalised to LF, so it is the
same from GitHub's archive, from `git archive`, and from a Windows checkout with autocrlf.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
LOCK = PROJECT / "twinlab.lock.json"
MAX_ARCHIVE = 300 * 1024 * 1024


class PinError(Exception):
    pass


def lock() -> dict:
    doc = json.loads(LOCK.read_text(encoding="utf-8"))
    for k in ("repository", "commit", "archive", "path", "tree_sha256"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            raise PinError(f"twinlab.lock.json: {k} is missing")
    return doc


def vendor_root(pin: dict | None = None) -> Path:
    pin = pin or lock()
    base = Path(os.environ.get("TWINLAB_VENDOR") or PROJECT / "vendor")
    return base / f"twin-lab-{pin['commit'][:12]}"


def labctl_parent(pin: dict | None = None) -> Path:
    """The folder to put on sys.path so that `import labctl` is the pinned tree."""
    pin = pin or lock()
    return vendor_root(pin) / PurePosixPath(pin["path"]).parent


def _skipped(rel: PurePosixPath) -> bool:
    return "__pycache__" in rel.parts or rel.suffix in (".pyc", ".pyo")


def tree_digest(folder: Path) -> str:
    """SHA-256 over `path NUL sha256(content with CRLF as LF) LF` for every file, sorted."""
    folder = Path(folder)
    if not folder.is_dir():
        raise PinError(f"{folder} is not a folder")
    lines = []
    for path in folder.rglob("*"):
        rel = PurePosixPath(path.relative_to(folder).as_posix())
        if path.is_file() and not _skipped(rel):
            body = path.read_bytes().replace(b"\r\n", b"\n")
            lines.append(f"{rel}\0{hashlib.sha256(body).hexdigest()}\n")
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


def verify(pin: dict | None = None) -> Path:
    """The vendored labctl folder when it matches the lock, else PinError."""
    pin = pin or lock()
    folder = vendor_root(pin) / PurePosixPath(pin["path"])
    if not folder.is_dir():
        raise PinError(f"Twin Lab {pin['commit'][:12]} is not vendored: run `python -m agent.twinlab fetch`")
    got = tree_digest(folder)
    if got != pin["tree_sha256"]:
        raise PinError(f"{folder} does not match twinlab.lock.json (digest {got[:16]}..., "
                       f"expected {pin['tree_sha256'][:16]}...): fetch it again")  # fmt: skip
    return folder


def _extract(tar: tarfile.TarFile, target: Path, path: str, strip_first: bool) -> int:
    """Only regular files under `path`; no links, no absolute or parent paths."""
    want = PurePosixPath(path)
    count = 0
    for member in tar.getmembers():
        name = PurePosixPath(member.name)
        if strip_first:
            if len(name.parts) < 2:
                continue
            name = PurePosixPath(*name.parts[1:])
        if name.is_absolute() or ".." in name.parts or want not in (name, *name.parents):
            continue
        if not member.isfile():
            if member.isdir():
                continue
            raise PinError(f"unexpected {member.type!r} entry in the archive: {member.name}")
        if _skipped(name):
            continue
        out = target / Path(*name.parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(member) as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count


def fetch(from_checkout: Path | None = None, pin: dict | None = None) -> Path:
    pin = pin or lock()
    root = vendor_root(pin)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".partial-", dir=root.parent))
    try:
        if from_checkout:
            raw = subprocess.run(["git", "-C", str(from_checkout), "archive", "--format=tar", pin["commit"],
                                  pin["path"]], capture_output=True, check=False)  # fmt: skip
            if raw.returncode:
                raise PinError(f"git archive {pin['commit'][:12]} failed in {from_checkout}: "
                               f"{raw.stderr.decode(errors='replace').strip()}")  # fmt: skip
            with tarfile.open(fileobj=io.BytesIO(raw.stdout), mode="r:") as tar:
                count = _extract(tar, staging, pin["path"], strip_first=False)
        else:
            if not pin["archive"].startswith("https://"):
                raise PinError("the archive address must be HTTPS")
            req = urllib.request.Request(pin["archive"], headers={"User-Agent": "twinlab2-pin"})
            with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r, \
                    tempfile.TemporaryFile() as buf:  # fmt: skip
                size = 0
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise PinError("the archive is larger than expected")
                    buf.write(chunk)
                buf.seek(0)
                with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                    count = _extract(tar, staging, pin["path"], strip_first=True)
        if not count:
            raise PinError(f"no {pin['path']} at {pin['commit'][:12]}")
        got = tree_digest(staging / PurePosixPath(pin["path"]))
        if got != pin["tree_sha256"]:
            raise PinError(f"the downloaded tree's digest {got} is not the pinned {pin['tree_sha256']}: "
                           "refusing it")  # fmt: skip
        (staging / "twinlab-pin.json").write_text(json.dumps(
            {k: pin[k] for k in ("repository", "commit", "path", "tree_sha256")}, indent=1), encoding="utf-8")
        shutil.rmtree(root, ignore_errors=True)
        staging.replace(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return verify(pin)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.twinlab", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--from-checkout", type=Path, help="a local twin-lab clone that has the pinned commit")
    sub.add_parser("verify")
    d = sub.add_parser("digest")
    d.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "digest":
            print(tree_digest(args.folder))
            return 0
        folder = fetch(args.from_checkout) if args.command == "fetch" else verify()
        pin = lock()
        print(f"Twin Lab {pin['commit'][:12]} ({pin['tree_sha256'][:16]}...) at {folder}")
        return 0
    except (PinError, OSError) as exc:
        print(f"twinlab pin: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
