"""The vulnerability gate of the supply-chain workflow: fail on a high or critical finding that the
allowlist does not cover.

    python tools/vuln_gate.py --allowlist security/allowlist.toml \
        --trivy python-portal.trivy.json --trivy image.trivy.json --npm-audit web.npm-audit.json \
        [--pip-audit portal.pip-audit.json] [--summary "$GITHUB_STEP_SUMMARY"]

Inputs are the scanners' JSON reports:
- `--trivy`: `trivy sbom|image|fs --format json` (Results[].Vulnerabilities[], with Severity).
- `--npm-audit`: `npm audit --json` (npm 7+: vulnerabilities{name: {severity, via}}).
- `--fixable-only`: a high or critical Trivy finding for which no fixed version exists yet (common
  in the base image's OS packages) is listed but does not block; it blocks as soon as a fix is
  released, so the next scan asks for a rebuild.
- `--pip-audit`: `pip-audit --format json`. pip-audit reports no severity, so its findings are
  listed for the reader but never fail the gate on their own: the same packages are scored
  through their SBOM by Trivy.

The allowlist (TOML) holds one `[[allow]]` table per accepted finding:

    [[allow]]
    id = "CVE-2026-12345"        # or GHSA-..., as the scanner names it (aliases match too)
    package = "example"          # optional: only this package
    reason = "Not reachable: ..."  # required
    expires = 2027-01-31         # required: the gate fails once an entry has expired

Exit status: 0 when nothing blocks, 1 when a finding blocks or an allowlist entry is invalid or
expired. Standard library only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
import sys
import tomllib

BLOCKING = {"HIGH", "CRITICAL"}
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "MODERATE", "LOW", "INFO", "UNKNOWN"]


@dataclass
class Finding:
    source: str
    id: str
    package: str
    version: str
    severity: str
    title: str = ""
    aliases: set[str] = field(default_factory=set)
    fixed: str = ""

    fix_known: bool = True  # False: the scanner says no fixed version exists yet

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING


@dataclass
class Allow:
    id: str
    package: str
    reason: str
    expires: date


def load_allowlist(path: Path, today: date) -> tuple[list[Allow], list[str]]:
    """The entries still in force, and the problems (invalid or expired entries)."""
    if not path.exists():
        return [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries, problems = [], []
    for n, raw in enumerate(data.get("allow", []), 1):
        where = f"{path.name} entry {n} ({raw.get('id', '?')})"
        expires = raw.get("expires")
        if not raw.get("id") or not str(raw.get("reason", "")).strip():
            problems.append(f"{where}: `id` and `reason` are required")
            continue
        if not isinstance(expires, date):
            problems.append(f"{where}: `expires` must be a date (YYYY-MM-DD)")
            continue
        if expires < today:
            problems.append(f"{where}: expired on {expires.isoformat()}; fix the finding or renew the entry")
            continue
        entries.append(Allow(str(raw["id"]).upper(), str(raw.get("package", "")).lower(), str(raw["reason"]), expires))
    return entries, problems


def allowed(finding: Finding, entries: list[Allow]) -> Allow | None:
    ids = {finding.id.upper(), *(a.upper() for a in finding.aliases)}
    for entry in entries:
        if entry.id in ids and (not entry.package or entry.package == finding.package.lower()):
            return entry
    return None


def from_trivy(report: dict, source: str) -> list[Finding]:
    found = []
    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for v in result.get("Vulnerabilities") or []:
            found.append(Finding(
                source=f"{source} ({target})" if target else source, id=v.get("VulnerabilityID", "?"),
                package=v.get("PkgName", "?"), version=v.get("InstalledVersion", ""),
                severity=str(v.get("Severity", "UNKNOWN")).upper(), title=v.get("Title", "") or "",
                aliases=set(v.get("VendorIDs") or []), fixed=v.get("FixedVersion", "") or "",
                fix_known=bool(v.get("FixedVersion"))))
    return found


def from_npm_audit(report: dict, source: str) -> list[Finding]:
    """One finding per advisory (npm lists a package once, with the advisories it is affected by in
    `via`; entries of `via` that are package names only point at another package's advisory)."""
    found = []
    for name, vuln in (report.get("vulnerabilities") or {}).items():
        for via in vuln.get("via") or []:
            if not isinstance(via, dict):
                continue
            url = via.get("url", "") or ""
            ghsa = url.rstrip("/").rsplit("/", 1)[-1] if "GHSA-" in url else ""
            found.append(Finding(
                source=source, id=ghsa or str(via.get("source", "?")), package=via.get("name", name),
                version=via.get("range", ""), severity=str(via.get("severity", vuln.get("severity", "unknown"))).upper(),
                title=via.get("title", "") or "", aliases={str(via.get("source", ""))} - {""}))
    return found


def from_pip_audit(report: dict | list, source: str) -> list[Finding]:
    dependencies = report.get("dependencies", []) if isinstance(report, dict) else report
    found = []
    for dep in dependencies:
        for v in dep.get("vulns") or []:
            found.append(Finding(
                source=source, id=v.get("id", "?"), package=dep.get("name", "?"), version=dep.get("version", ""),
                severity="UNKNOWN", title=(v.get("description") or "")[:120], aliases=set(v.get("aliases") or []),
                fixed=", ".join(v.get("fix_versions") or [])))
    return found


def gate(findings: list[Finding], entries: list[Allow], problems: list[str],
         fixable_only: bool = False) -> tuple[int, list[str]]:
    lines, blocking = [], 0
    unique: dict[tuple[str, str, str, str], Finding] = {}
    for f in findings:
        unique.setdefault((f.source, f.id, f.package, f.version), f)
    order = {s: n for n, s in enumerate(SEVERITIES)}
    rows = sorted(unique.values(), key=lambda f: (order.get(f.severity, 99), f.package, f.id))
    used: set[str] = set()
    lines.append("| Severity | Finding | Package | Fixed in | Found by | Gate |")
    lines.append("|---|---|---|---|---|---|")
    for f in rows:
        entry = allowed(f, entries)
        if entry:
            used.add(entry.id)
            verdict = f"allowed until {entry.expires.isoformat()}"
        elif f.blocking and fixable_only and not f.fix_known:
            verdict = "reported: no fix released yet"
        elif f.blocking:
            blocking += 1
            verdict = "**blocks**"
        else:
            verdict = "reported"
        lines.append(f"| {f.severity} | {f.id} | {f.package} {f.version} | {f.fixed or '-'} | {f.source} | {verdict} |")
    if not rows:
        lines = ["No known vulnerability in the scanned components."]
    rule = "high or critical with a released fix" if fixable_only else "high or critical"
    head = [f"Findings: {len(rows)}; blocking ({rule}, not allowlisted): {blocking}."]
    for p in problems:
        head.append(f"- Allowlist problem: {p}")
    for entry in entries:
        if entry.id not in used:
            head.append(f"- Allowlist entry {entry.id} matched nothing: remove it once it is no longer needed.")
    return (1 if blocking or problems else 0), head + [""] + lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--allowlist", type=Path, default=Path("security/allowlist.toml"))
    parser.add_argument("--trivy", type=Path, action="append", default=[])
    parser.add_argument("--npm-audit", type=Path, action="append", default=[])
    parser.add_argument("--pip-audit", type=Path, action="append", default=[])
    parser.add_argument("--summary", type=Path, help="append the table here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--fixable-only", action="store_true",
                        help="a high or critical Trivy finding without a released fix is listed, not blocking")
    parser.add_argument("--title", default="Vulnerability gate")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for paths, read in ((args.trivy, from_trivy), (args.npm_audit, from_npm_audit), (args.pip_audit, from_pip_audit)):
        for path in paths:
            findings += read(json.loads(path.read_text(encoding="utf-8")), path.name)
    entries, problems = load_allowlist(args.allowlist, args.today)
    status, lines = gate(findings, entries, problems, args.fixable_only)
    text = "\n".join([f"### {args.title}", "", *lines, ""])
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as out:
            out.write(text + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
