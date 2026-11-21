# Changelog

All notable changes to TwinLab2 (the portal, the recorder and the contract they share) are
documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). How a release is cut:
[docs/releasing.md](docs/releasing.md).

Versions before the first release tag (0.2.0 and earlier) were not released; their history is in
the merged pull requests.

## [Unreleased]

### Added

- Recorder contract 1.8.0: completeness in separate dimensions (channel availability, event
  detail, screen/state coverage, network evidence), plus the portal's owner-reviewed scope. The
  recording page and the recorder's send review show them; the package validator counts an
  item as observed only as far as the recordings' matching dimension shows it. 1.0 to 1.7 stay
  accepted, and their dimensions are derived where they can be.
- Recorder: queued uploads are bound to the portal and machine they were queued for. After
  re-pairing elsewhere they are held, and moving them needs a transfer in the window, written to
  `upload-queue\transfers.jsonl`.
- recorder/GO-DESKTOP-CHECKLIST.md: office trial on a managed PC (signed binaries, EDR, elevated
  targets, mixed DPI, interrupted shutdown, disk exhaustion).
- Release pipeline: a `vMAJOR.MINOR.PATCH` tag builds the portal image, pushes it to GHCR with
  build provenance, builds the recorder (`TwinLabRecorder-<version>-win-x64.zip` with its SHA-256),
  and publishes a GitHub release with this changelog's section (`.github/workflows/release.yml`).
- Supply chain: CycloneDX SBOMs of the portal and recorder Python environments, the web app's npm
  tree and the container image; pip-audit, npm audit and Trivy behind one gate with an allowlist
  (`security/allowlist.toml`); dependency review on pull requests; Dependabot.
- Browser end-to-end tests (Playwright, headless) of the owner's journey and role refusals against
  `tools/e2e_portal.py`, in CI (`.github/workflows/e2e.yml`).

### Changed

- GitHub Actions are pinned by commit SHA; the image's base images are pinned by digest and the
  portal stage applies Debian's security updates.

### Security

- Recorder recovery writes only inside the session's own recording folders, whatever the capture
  journal names (no traversal, no links followed); fuzz and traversal tests.
- The image no longer ships zlib1g with CVE-2026-85091 (fixed by Debian's update).
