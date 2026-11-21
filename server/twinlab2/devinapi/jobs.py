# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""The Devin-status mapping of Twin Lab's `devin.jobs` module (`map_status`). Twin Lab's job
store itself is not part of TwinLab2: builds keep their own state (`twinlab2.builds`)."""

from __future__ import annotations


_MONEY_WORDS = ("usage", "credit", "quota", "limit", "billing", "payment")


def map_status(status: str | None, detail: str | None) -> tuple[str, str | None]:
    """Twin Lab job state for a Devin status, and an explanation to record when there is one."""
    status = (status or "").lower()
    detail = (detail or "").lower()
    if status == "error":
        return "failed", "Devin reported an error; open the session for details"
    if status == "exit":
        return "finished", None
    if status == "running":
        if detail == "finished":
            return "finished", None
        if detail in ("waiting_for_user", "waiting_for_approval"):
            return "waiting_for_user", None
        return "running", None
    if status == "suspended":
        if any(word in detail for word in _MONEY_WORDS):
            return "failed", f"Devin suspended the session: {detail} (add credits or raise limits)"
        return "waiting_for_user", None
    return "running", None
