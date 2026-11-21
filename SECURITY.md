# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately with GitHub's
**Report a vulnerability** button on the repository's **Security** tab (private vulnerability
reporting). Include what you found, how to reproduce it, and what it could affect.

You will get an acknowledgement within five working days, and a plan or a fix as soon as the
problem is understood. Reports are credited unless you ask otherwise.

## Supported versions

Only the latest `main` is supported. There are no maintained release branches yet.

## Sensitive data

TwinLab2 handles recordings of real applications and stores evidence, build packs and reports.
If you find real customer data, credentials, tokens or internal hostnames anywhere in this
repository, in its history, or in anything the portal produces, report it the same private way
so it can be removed and the secret revoked.

## What counts

- A way to make the recorder or portal keep or leak typed values, cookies, tokens, credential
  headers or other data that its redaction rules say are dropped or masked.
- Weaknesses in recorder pairing, authentication, review roles or the audit trail.
- A way to read or change another application's evidence, build packs or releases, or to
  tamper with stored artifacts without the SHA-256 checks noticing.
- Weaknesses in the deployment files under `deploy/` or the build runners.

The recorder's collectors were copied from Twin Lab ([docs/UPSTREAM.md](docs/UPSTREAM.md)): report
a flaw in them here, and to [Twin Lab](https://github.com/karuppusamym/twin-lab/security) if it
is in Twin Lab too. Demo apps are meant to be look-alike copies with synthetic data; that they resemble
a real application is by design and not a vulnerability.
