# A workstation session, as Twin Lab's collectors leave it

Synthetic: the reference CaseDesk desktop application, a local web application and the synthetic
TN3270 host, as in `VERIFICATION.md`. No real person's data. `tests/test_contract.py` builds the
references from this folder with the pinned Twin Lab (`twinlab.lock.json`) and checks them against
`contract/recorder-contract.schema.json`. `../references-from-pinned-twinlab.json` is that build's
output, kept so the check also runs where the pinned Twin Lab is not vendored; the test fails if
the pinned Twin Lab builds something else from this folder (regenerate it when moving the pin).
