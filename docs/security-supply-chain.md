# Supply-chain security

What TwinLab2 does to know what it ships and to keep known-vulnerable components out, and what
stays with the office. Releases: [releasing.md](releasing.md). Reporting a vulnerability:
[SECURITY.md](../SECURITY.md).

## What runs where

| Control | Where | Fails the build on |
|---|---|---|
| Actions pinned by commit SHA | every workflow; `tests/server/test_supply_chain.py` | an action referenced by tag or branch |
| Containers pinned by digest | Dockerfile `FROM`, kubeconform, Trivy | an image referenced by tag only |
| SBOM: Python (portal, recorder) | `supply-chain (python)`: `cyclonedx-py environment` | - |
| SBOM: web app (npm) | `supply-chain (web)`: `npm sbom --sbom-format cyclonedx` | - |
| SBOM: container image | `supply-chain (image)` and each release: `trivy image --format cyclonedx` | - |
| Python advisories | Trivy over the Python SBOMs; pip-audit (OSV) alongside | high or critical |
| npm advisories | `npm audit` | high or critical |
| Image advisories | Trivy (OS and Python packages in the image) | high or critical **with a released fix** |
| New dependencies in a PR | `supply-chain (dependency review)` (GitHub's advisory database) | high or critical |
| Updates | Dependabot, weekly, grouped: actions, pip, npm, Docker base images | - |
| Provenance | release: `actions/attest-build-provenance` for the image and the recorder zip | - |
| Browser journey | `e2e (browser journey)` | any failure (traces uploaded) |

All SBOMs are CycloneDX JSON, uploaded as workflow artifacts (`sbom-python`, `sbom-web`,
`sbom-image`), and the image SBOM is attached to each release. The scans also run every Monday,
so a new advisory against unchanged code shows up without a commit.

The Python SBOMs are taken on Linux, so the recorder's Windows-only dependency (`comtypes`) is not
in them; the recorder's PyInstaller folder itself is not scanned yet.

The dependency-review job needs the repository's dependency graph (Settings, Code security). While
it is off the job is refused by GitHub and, being `continue-on-error`, does not block; enable the
graph and remove `continue-on-error` from `supply-chain.yml` to make it count. That is a repository
setting for the admin (the office, for a private mirror).

## The gate and the allowlist

`tools/vuln_gate.py` reads the scanners' JSON and writes one table per job into the run summary.
It blocks on a high or critical finding unless `security/allowlist.toml` lists it:

```toml
[[allow]]
id = "CVE-2026-12345"
package = "example"
reason = "Only in the web build stage, never shipped; upstream fix tracked in <link>."
expires = 2027-01-31
```

- `reason` and `expires` are required. An expired entry fails the gate, so every exception is
  looked at again. Entries that match nothing are reported, so they get removed.
- An entry is acceptable when the vulnerable code is not reachable in TwinLab2 (with the reason),
  or when no fix exists and the risk is accepted for a short time. A fix that exists is applied
  instead (update the dependency, or rebuild the image).
- An allowlist change goes through a pull request like any code, reviewed by someone other than
  the author.
- pip-audit reports no severity: its findings are listed for the reader, and Trivy scores the
  same packages.
- Image findings without a released fix (typical for Debian base packages) are listed but don't
  block (`--fixable-only`). They block as soon as Debian releases a fix, so the next scan asks for a
  rebuild. The Dockerfile applies Debian's security updates (`apt-get upgrade`) for that reason.

## Required checks

Branch protection on `main` requires `pytest (sqlite)` and `pytest (postgresql)`; `web` and `image`
are expected green before a merge. The new checks are not required yet. Once each has passed for
a week or so, make these required too:

- `e2e (browser journey)`
- `supply-chain (python)`, `supply-chain (web)`, `supply-chain (image)`
- `supply-chain (dependency review)` (pull requests only)

Leave the `release (*)` jobs unrequired: they run only on PRs that touch the release path.

## What stays with the office

- **Code-signing certificate** for the recorder (see [releasing.md](releasing.md#code-signing-the-office-provides-the-certificate)).
- **The registry the cluster pulls from**: images are published to GHCR. An internal mirror
  (Quay, Artifactory) and an admission policy that verifies the provenance (for example Sigstore
  policy-controller or OpenShift's image signature verification) are the platform team's.
- **Private advisory feeds or commercial scanners**, if the office uses them: the gate takes any
  scanner that writes Trivy-format JSON.
- **SLSA level and retention** policies for SBOMs and attestations beyond GitHub's defaults.
