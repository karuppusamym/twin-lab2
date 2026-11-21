"""TwinLab2's release version and changelog (docs/releasing.md).

    python tools/release.py current               -> 0.2.0 (the version all packages carry)
    python tools/release.py bump 0.3.0            -> sets the version everywhere, dates CHANGELOG's Unreleased
    python tools/release.py check v0.3.0          -> the tag matches the versions and CHANGELOG has [0.3.0]
    python tools/release.py check --unreleased    -> (pull requests) versions agree, CHANGELOG has [Unreleased]
    python tools/release.py notes 0.3.0           -> that version's CHANGELOG section (the release notes)

One release version: contract/ and server/ (pyproject.toml) carry the same semantic version
(MAJOR.MINOR.PATCH, with an optional -rc.N style pre-release). The recorder program has its own
version (twinlab2_recorder/settings.py VERSION, changed with the recorder's changes; it names the
MSI) and the recorder contract version (CONTRACT_VERSION) is a protocol version: neither is changed
here. CHANGELOG.md follows https://keepachangelog.com/en/1.1.0/.
Standard library only.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
PYPROJECTS = ["contract/pyproject.toml", "server/pyproject.toml"]  # the release (product) version
PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)
SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$", re.M)


class ReleaseError(Exception):
    pass


def versions(root: Path = ROOT) -> dict[str, str]:
    found = {}
    for rel in PYPROJECTS:
        match = PYPROJECT_VERSION.search((root / rel).read_text(encoding="utf-8"))
        found[rel] = match.group(2) if match else ""
    return found


def current(root: Path = ROOT) -> str:
    found = versions(root)
    distinct = set(found.values())
    if len(distinct) != 1 or "" in distinct:
        raise ReleaseError("the packages disagree on the version: " + ", ".join(f"{k}={v or '?'}" for k, v in found.items()))
    return distinct.pop()


def sections(changelog: str) -> dict[str, tuple[str | None, str]]:
    """version -> (date, body) for each `## [version] - date` section."""
    matches = list(SECTION.finditer(changelog))
    out = {}
    for n, m in enumerate(matches):
        end = matches[n + 1].start() if n + 1 < len(matches) else len(changelog)
        body = changelog[m.end():end]
        body = re.split(r"^\[[^\]]+\]: ", body, flags=re.M)[0]  # link references at the end
        out[m.group("version")] = (m.group("date"), body.strip())
    return out


def parse_tag(tag: str) -> str:
    version = tag.removeprefix("refs/tags/").removeprefix("v")
    if not tag.removeprefix("refs/tags/").startswith("v") or not SEMVER.match(version):
        raise ReleaseError(f"{tag!r} is not a release tag: vMAJOR.MINOR.PATCH[-pre], e.g. v0.3.0 or v1.0.0-rc.1")
    return version


def check(tag: str | None, root: Path = ROOT) -> str:
    version = current(root)
    if not SEMVER.match(version):
        raise ReleaseError(f"version {version!r} is not a semantic version")
    entries = sections((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    if tag is None:
        if "Unreleased" not in entries:
            raise ReleaseError("CHANGELOG.md has no [Unreleased] section")
        return version
    wanted = parse_tag(tag)
    if wanted != version:
        raise ReleaseError(f"tag {tag} but the packages carry {version}: run tools/release.py bump {wanted} first")
    if wanted not in entries:
        raise ReleaseError(f"CHANGELOG.md has no [{wanted}] section")
    when, body = entries[wanted]
    if not when:
        raise ReleaseError(f"CHANGELOG.md's [{wanted}] section has no date (## [{wanted}] - YYYY-MM-DD)")
    if not body:
        raise ReleaseError(f"CHANGELOG.md's [{wanted}] section is empty")
    return version


def notes(version: str, root: Path = ROOT) -> str:
    entries = sections((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    if version not in entries:
        raise ReleaseError(f"CHANGELOG.md has no [{version}] section")
    return entries[version][1] + "\n"


def bump(version: str, today: date | None = None, root: Path = ROOT) -> None:
    if not SEMVER.match(version):
        raise ReleaseError(f"{version!r} is not a semantic version")
    old = current(root)
    if version == old:
        raise ReleaseError(f"the packages already carry {version}")
    path = root / "CHANGELOG.md"
    changelog = path.read_text(encoding="utf-8")
    entries = sections(changelog)
    if version in entries:
        raise ReleaseError(f"CHANGELOG.md already has [{version}]")
    if not entries.get("Unreleased", (None, ""))[1]:
        raise ReleaseError("CHANGELOG.md's [Unreleased] section is empty: describe the release first")
    when = (today or date.today()).isoformat()
    changelog = re.sub(r"^## \[Unreleased\]\s*$", f"## [Unreleased]\n\n## [{version}] - {when}", changelog, count=1, flags=re.M)
    path.write_text(changelog, encoding="utf-8", newline="\n")
    for rel, pattern in [(p, PYPROJECT_VERSION) for p in PYPROJECTS]:
        file = root / rel
        text = file.read_text(encoding="utf-8")
        file.write_text(pattern.sub(lambda m: m.group(1) + version + m.group(3), text, count=1), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("current")
    b = sub.add_parser("bump")
    b.add_argument("version")
    c = sub.add_parser("check")
    c.add_argument("tag", nargs="?")
    c.add_argument("--unreleased", action="store_true")
    n = sub.add_parser("notes")
    n.add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "current":
            print(current())
        elif args.command == "bump":
            bump(args.version)
            print(f"version {args.version}: CHANGELOG.md dated; commit, merge, then tag v{args.version}")
        elif args.command == "check":
            if not args.tag and not args.unreleased:
                parser.error("check needs a tag or --unreleased")
            print(check(None if args.unreleased else args.tag))
        elif args.command == "notes":
            sys.stdout.write(notes(args.version))
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
