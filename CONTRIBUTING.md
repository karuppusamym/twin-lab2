# Contributing to TwinLab2

Thank you for helping. This page explains how to propose a change and what a pull request needs
before it can merge. By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- **Synthetic data only.** Never commit real customer data, account numbers, hostnames, user
  names, passwords, tokens or recordings of a production system.
- **No secrets** in configuration, logs, fixtures or test output. Credentials come from the
  environment and are never shown or stored in the catalog.
- **Be honest about status.** A capability is *Implemented*, *Locally tested* or *Externally
  validated* (see *What works today* in docs/design.md); raise a label only with the evidence it needs, and record it in
  [VERIFICATION.md](docs/VERIFICATION.md).
- **Do not loosen a test to make it pass.** If a test is wrong, fix it in its own pull request
  and say why.
- The recorder's collectors, the recording format and Verify's replay were copied from Twin Lab
  at 90bdd91 and are this repository's code now ([docs/UPSTREAM.md](docs/UPSTREAM.md) lists what
  came from where). A change taken from Twin Lab says which upstream commit it came from. Changes to what crosses between
  portal and recorder follow the versioned [recorder contract](docs/RECORDER-CONTRACT.md).

## Getting set up

You need Python 3.13.

```bash
python -m pip install -r requirements-test.txt
```

That installs `contract/`, `server/` (with its test and replay extras) and `recorder/` as
editable packages. Run the tests with `python -m pytest` (`tests/server`, `tests/recorder`).

```bash
python -m twinlab2.portal
```

The portal opens on http://127.0.0.1:8765. docs/design.md explains the database and artifact
storage options.

## Making a change

1. Open an issue first for anything larger than a small fix, so the approach can be agreed.
2. Branch from `main`, and keep the pull request to one topic. Add or update tests with the code.
3. Update the README, `VERIFICATION.md` or `RECORDER-CONTRACT.md` where the behaviour they
   describe changed.
4. Run the tests below and paste the results into the pull request.

## Tests

```bash
python -m pytest -q
```

That runs on SQLite. CI runs the same suite on SQLite and on PostgreSQL 16; to run the
PostgreSQL leg locally, set `TWINLAB2_TEST_DATABASE_URL` as *The portal database* in docs/design.md
section shows.

## Review and merge

- A pull request merges only when every check has finished and passed; auto-merge is not used.
- Branches are deleted once merged.

## Licence

TwinLab2 is licensed under the [Apache License 2.0](LICENSE). By submitting a contribution you
agree that it is licensed under the same terms (section 5 of the licence), and that you have the
right to submit it.

## Reporting a security problem

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).
