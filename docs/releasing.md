# Releasing TwinLab2

A release is one version of the whole product: the portal image, the Windows recorder and the
contract they share. It is cut from `main` by pushing a tag; `.github/workflows/release.yml` does
the rest. Nothing is released by hand.

## Versions

- [Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`, with an optional pre-release
  (`1.0.0-rc.1`, published as a GitHub pre-release).
  - MAJOR: a change that breaks a deployed portal's database, API (`/api/v1`, see
    [api-versioning.md](api-versioning.md)) or the recorders already in the field.
  - MINOR: new features, compatible.
  - PATCH: fixes only.
- One version everywhere: `contract/`, `server/` and `recorder/` `pyproject.toml` and the recorder's
  `VERSION` (`recorder/twinlab2_recorder/settings.py`). `tools/release.py` keeps them in step and
  `tests/server/test_supply_chain.py` fails when they disagree.
- The recorder contract version (`CONTRACT_VERSION`) is a protocol version with its own rules
  ([RECORDER-CONTRACT.md](RECORDER-CONTRACT.md)); a release does not change it.

## The changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Every pull request
that changes behaviour adds a line under `## [Unreleased]` (Added, Changed, Deprecated, Removed,
Fixed, Security). The release notes are that section, word for word.

## Cutting a release

1. On a branch from `main`, describe the release under `[Unreleased]`, then

       python tools/release.py bump 0.3.0

   which sets the version in all four places and turns `[Unreleased]` into `## [0.3.0] - <today>`
   (with a new empty `[Unreleased]` above it).
2. Open a pull request ("Release 0.3.0"); merge it when every check is green.
3. Tag the merge commit on `main` and push the tag:

       git tag -a v0.3.0 -m "TwinLab2 0.3.0"
       git push origin v0.3.0

4. The `release` workflow then:
   - checks the tag against the packages' version and `CHANGELOG.md` (`tools/release.py check`);
   - builds the image, scans it (it stops on a fixable high or critical finding:
     [security-supply-chain.md](security-supply-chain.md)), pushes
     `ghcr.io/<owner>/twinlab2-portal:<version>` and records its build provenance in the
     registry (`actions/attest-build-provenance`, signed through GitHub's Sigstore instance);
   - builds `TwinLabRecorder` on `windows-latest` (`recorder/build.py`), signs it when the
     certificate is configured (below), zips it as `TwinLabRecorder-<version>-win-x64.zip`, writes
     its SHA-256 beside it and attests the zip;
   - creates the GitHub release with the changelog section, the zip, its `.sha256` and the image
     SBOM.
5. Deploy by digest, not by tag: the release notes give `ghcr.io/...@sha256:...`
   ([deploying-on-openshift.md](deploying-on-openshift.md)).

A pull request that touches the release path (`release.yml`, `tools/release.py`, `CHANGELOG.md`,
`deploy/Dockerfile`, `recorder/`, `contract/`) runs the image and recorder jobs as a dry run:
built, scanned and uploaded as workflow artifacts, never pushed, attested or released. `Run workflow`
on the Actions tab does the same for any branch.

A failed release: fix on `main`, then tag the next PATCH version. Never move or reuse a tag, and
never delete a published image tag that a deployment may use.

## Checking what you downloaded

    gh attestation verify oci://ghcr.io/<owner>/twinlab2-portal@sha256:<digest> -R karuppusamym/twin-lab2
    gh attestation verify TwinLabRecorder-0.3.0-win-x64.zip -R karuppusamym/twin-lab2
    sha256sum -c TwinLabRecorder-0.3.0-win-x64.zip.sha256          # or: Get-FileHash -Algorithm SHA256

## Code signing (the office provides the certificate)

An unsigned recorder with low-level keyboard hooks is likely to be stopped by endpoint protection on
a managed PC, so production releases must be signed. **The code-signing certificate is the office's
to provide**; this repository holds none. Until it is configured the recorder job skips signing,
says so in its log, and the release notes mark the recorder as not code-signed.

To enable it, a repository admin adds two Actions secrets (Settings, Secrets and variables, Actions),
preferably in a protected `release` environment that requires approval:

| Secret | Value |
|---|---|
| `TWINLAB_SIGN_PFX_BASE64` | the certificate and private key as a PFX file, base64-encoded |
| `TWINLAB_SIGN_PFX_PASSWORD` | the PFX's password |

The job imports the PFX into the runner's user store and `recorder/build.py` signs
`TwinLabRecorder.exe` and Playwright's `node.exe` with `signtool` (SHA-256, RFC 3161 timestamp;
`TWINLAB_SIGN_TIMESTAMP` overrides the timestamp server). A certificate on a hardware token or in a
cloud HSM (EV certificates usually are) needs a different step instead: Azure Trusted Signing or
the vendor's signing tool; replace the placeholder step with it and keep the same outputs.
