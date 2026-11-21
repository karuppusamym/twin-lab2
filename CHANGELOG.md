# Changelog

All notable changes to TwinLab2 (the portal, the recorder and the contract they share) are
documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). How a release is cut:
[docs/releasing.md](docs/releasing.md).

Versions before the first release tag (0.2.0 and earlier) were not released; their history is in
the merged pull requests.

## [Unreleased]

### Added

- Package format 3 leftovers: the desktop picture comparison masks the twin label (title-bar text and the
  `TwinAppLabel` footer); Verify checks app-to-app contracts (GET API response fields, file columns, and the
  peer-down message with the producer's lab agent held at 503, always removed afterwards); approved visual
  baselines become required visual states in Verify (reported-only without them).
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

- The image no longer ships zlib1g with CVE-2026-85091 (fixed by Debian's update).
