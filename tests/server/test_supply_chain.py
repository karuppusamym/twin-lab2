"""The build and release pipeline's own rules: pinned actions and images, the names of the required
checks, the vulnerability gate (tools/vuln_gate.py) and the release version tool (tools/release.py)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "tools"))

import release  # noqa: E402
import vuln_gate  # noqa: E402


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


class PinnedTest(unittest.TestCase):
    def test_every_action_is_pinned_by_commit_sha(self):
        for path in WORKFLOWS.glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"\s*-?\s*uses:\s*(\S+)", line)
                if not match or match.group(1).startswith("./"):
                    continue
                with self.subTest(workflow=path.name, uses=match.group(1)):
                    self.assertRegex(match.group(1), r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
                    self.assertRegex(line, r"#\s*v\d", "keep the version as a comment (Dependabot updates it)")

    def test_containers_run_by_digest(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for image in re.findall(r"(?:ghcr\.io/yannh/kubeconform|aquasec/trivy):\S+", text):
                with self.subTest(workflow=path.name, image=image):
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
        for line in (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                with self.subTest(line=line):
                    self.assertRegex(line, r"^FROM \S+@sha256:[0-9a-f]{64}( AS \w+)?$")

    def test_the_required_checks_keep_their_names(self):
        # branch protection names these checks: renaming one blocks every pull request
        self.assertEqual(workflow("test.yml")["jobs"]["test"]["name"], "pytest (${{ matrix.database }})")
        self.assertEqual(workflow("test.yml")["jobs"]["test"]["strategy"]["matrix"]["database"], ["sqlite", "postgresql"])
        self.assertEqual(workflow("web.yml")["jobs"]["web"]["name"], "web (lint, typecheck, test, build)")
        self.assertEqual(workflow("image.yml")["jobs"]["image"]["name"], "image (build and smoke test)")

    def test_workflows_default_to_read_only(self):
        for path in WORKFLOWS.glob("*.yml"):
            with self.subTest(workflow=path.name):
                self.assertEqual(workflow(path.name)["permissions"], {"contents": "read"})

    def test_dependabot_covers_every_ecosystem(self):
        config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        self.assertEqual({u["package-ecosystem"] for u in config["updates"]}, {"github-actions", "pip", "npm", "docker"})


def trivy(*vulns):
    return {"Results": [{"Target": "img (debian 13)", "Vulnerabilities": [
        {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": "1", "Severity": sev, "FixedVersion": fixed}
        for vid, pkg, sev, fixed in vulns]}]}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_gate(self, *args, allowlist="", today="2026-11-21"):
        (self.dir / "allow.toml").write_text(allowlist, encoding="utf-8")
        return vuln_gate.main(["--allowlist", str(self.dir / "allow.toml"), "--today", today, *args])

    def report(self, name, data):
        (self.dir / name).write_text(json.dumps(data), encoding="utf-8")
        return str(self.dir / name)

    def test_high_or_critical_blocks_lower_does_not(self):
        self.assertEqual(self.run_gate("--trivy", self.report("a.json", trivy(("CVE-1", "zlib", "MEDIUM", "2")))), 0)
        self.assertEqual(self.run_gate("--trivy", self.report("b.json", trivy(("CVE-2", "zlib", "HIGH", "2")))), 1)
        self.assertEqual(self.run_gate("--trivy", self.report("c.json", trivy(("CVE-3", "zlib", "CRITICAL", "")))), 1)

    def test_fixable_only_lists_a_finding_without_a_fix(self):
        unfixed = self.report("u.json", trivy(("CVE-4", "libc", "HIGH", "")))
        fixed = self.report("f.json", trivy(("CVE-5", "libc", "HIGH", "2.1")))
        self.assertEqual(self.run_gate("--fixable-only", "--trivy", unfixed), 0)
        self.assertEqual(self.run_gate("--fixable-only", "--trivy", fixed), 1)

    def test_the_allowlist_needs_a_reason_and_expires(self):
        report = self.report("r.json", trivy(("CVE-6", "zlib", "HIGH", "2")))
        entry = '[[allow]]\nid = "cve-6"\npackage = "zlib"\nreason = "Build stage only."\nexpires = {}\n'
        self.assertEqual(self.run_gate("--trivy", report, allowlist=entry.format("2026-12-31")), 0)
        self.assertEqual(self.run_gate("--trivy", report, allowlist=entry.format("2026-11-01")), 1)  # expired
        self.assertEqual(self.run_gate("--trivy", report, allowlist=entry.replace("Build stage only.", "").format("2026-12-31")), 1)
        other = entry.replace('"zlib"', '"openssl"').format("2026-12-31")
        self.assertEqual(self.run_gate("--trivy", report, allowlist=other), 1)  # another package

    def test_npm_audit_advisories_and_aliases(self):
        audit = {"vulnerabilities": {
            "vite": {"severity": "high", "via": [{"source": 1100, "name": "vite", "severity": "high", "title": "x",
                                                   "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc", "range": "<8"}]},
            "plugin": {"severity": "high", "via": ["vite"]}}}  # only points at vite's advisory
        findings = vuln_gate.from_npm_audit(audit, "npm")
        self.assertEqual([(f.id, f.package, f.severity) for f in findings], [("GHSA-aaaa-bbbb-cccc", "vite", "HIGH")])
        path = self.report("npm.json", audit)
        allow = '[[allow]]\nid = "GHSA-aaaa-bbbb-cccc"\nreason = "Dev server only."\nexpires = 2027-01-01\n'
        self.assertEqual(self.run_gate("--npm-audit", path), 1)
        self.assertEqual(self.run_gate("--npm-audit", path, allowlist=allow), 0)

    def test_pip_audit_findings_are_reported_not_blocking(self):
        report = {"dependencies": [{"name": "pip", "version": "25.1", "vulns": [
            {"id": "PYSEC-1", "aliases": ["CVE-7"], "fix_versions": ["26.0"], "description": "x"}]}, {"name": "skipped"}]}
        self.assertEqual(self.run_gate("--pip-audit", self.report("p.json", report)), 0)

    def test_the_repository_allowlist_is_valid(self):
        entries, problems = vuln_gate.load_allowlist(ROOT / "security" / "allowlist.toml", date.today())
        self.assertEqual(problems, [])


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in [*release.PYPROJECTS, release.RECORDER_SETTINGS]:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, self.root / rel)
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- A thing.\n\n## [0.1.0] - 2026-01-02\n\n- Old.\n", encoding="utf-8")

    def test_the_repository_is_consistent(self):
        self.assertTrue(release.SEMVER.match(release.current()))
        self.assertEqual(release.check(None), release.current())

    def test_tags(self):
        self.assertEqual(release.parse_tag("v1.2.3"), "1.2.3")
        self.assertEqual(release.parse_tag("refs/tags/v1.0.0-rc.1"), "1.0.0-rc.1")
        for bad in ("1.2.3", "v1.2", "v01.2.3", "vnext"):
            with self.subTest(tag=bad), self.assertRaises(release.ReleaseError):
                release.parse_tag(bad)

    def test_bump_then_check_then_notes(self):
        version = release.current(self.root)
        with self.assertRaises(release.ReleaseError):
            release.check("v9.9.9", self.root)  # the packages do not carry 9.9.9
        release.bump("9.9.9", date(2026, 12, 1), self.root)
        self.assertEqual(release.current(self.root), "9.9.9")
        self.assertNotEqual(version, "9.9.9")
        self.assertEqual(release.check("v9.9.9", self.root), "9.9.9")
        self.assertEqual(release.notes("9.9.9", self.root), "### Added\n\n- A thing.\n")
        self.assertIn("## [Unreleased]\n\n## [9.9.9] - 2026-12-01", (self.root / "CHANGELOG.md").read_text(encoding="utf-8"))
        with self.assertRaises(release.ReleaseError):
            release.bump("9.9.10", root=self.root)  # Unreleased is empty now

    def test_disagreeing_versions_are_refused(self):
        path = self.root / "server" / "pyproject.toml"
        path.write_text(re.sub(r'^version = "[^"]+"', 'version = "7.0.0"', path.read_text(encoding="utf-8"), count=1, flags=re.M),
                        encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.current(self.root)

    def test_cli(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "check", "v0.0.0-never"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("release:", out.stderr)
