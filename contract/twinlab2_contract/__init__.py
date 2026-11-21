"""What the TwinLab2 recorder and portal agree on.

- `recorder_contract`: the versioned messages a recorder sends (pair, references), checked
  against `recorder-contract.schema.json` on both sides (docs/RECORDER-CONTRACT.md);
- `evidence`: the recording format itself, which the recorder writes and the portal's replay
  reads: how page text is masked (`sanitize`), typed values, DOM snapshots, step locators and
  the recording loader. Copied from Twin Lab 90bdd91 (`upstream`, docs/UPSTREAM.md).
"""
