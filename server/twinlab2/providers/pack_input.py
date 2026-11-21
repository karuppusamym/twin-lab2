"""Verify and materialize approved inputs outside the repository being published."""

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from zipfile import ZipFile

MAX_FILES = 10_000
MAX_BYTES = 256 * 1024 * 1024
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def materialize(workspace: Path, expected_sha256: str) -> Path:
    archive = (workspace / "pack.zip").read_bytes()
    if hashlib.sha256(archive).hexdigest() != expected_sha256:
        raise ValueError("Package checksum does not match the approved revision")
    with ZipFile(io.BytesIO(archive)) as package:
        members = package.infolist()
        if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_BYTES:
            raise ValueError("Build package exceeds the extraction limit")
        names = set()
        for member in members:
            name = member.filename
            parts = name.split("/")
            if (member.orig_filename != name or not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                    or any(p in ("", ".", "..") or p.endswith((".", " "))
                           or p.split(".", 1)[0].casefold() in RESERVED
                           or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts)
                    or stat.S_ISLNK(member.external_attr >> 16)
                    or name.casefold() in names):
                raise ValueError(f"Unsafe or duplicate build package path: {name}")
            names.add(name.casefold())
        for required in ("BUILD_TASK.md", "evidence.json"):
            if required not in package.namelist():
                raise ValueError(f"Build package is missing {required}")
        json.loads(package.read("evidence.json"))
        # A new private directory prevents stale files or links from a prior attempt
        # becoming inputs on resume. It remains with the session's audit artifacts.
        root = Path(tempfile.mkdtemp(prefix="approved-input-", dir=workspace))
        for member in members:
            target = root.joinpath(*member.filename.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return root
