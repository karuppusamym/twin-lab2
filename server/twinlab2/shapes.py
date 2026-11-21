"""Rows of the catalog in the shapes of api/openapi.yaml (components.schemas).

Pure functions: they read what they are given and decide nothing about who may see it. The
routes decide that (auth.py roles, restricted evidence) and pass only what the caller may see.
"""

from __future__ import annotations

import json

from twinlab2 import builds
from twinlab2 import journey as journeys
from twinlab2 import providers


def _row(row):
    return dict(row) if row is not None and not isinstance(row, dict) else row


def ref(row, name="name"):
    return {"id": row["id"], "name": row[name]}


def choice(value, label):
    return {"value": value, "label": label}


def journey(j):
    def step(s):
        return {"id": s.id, "title": s.title, "state": s.state, "hint": s.hint, "href": s.href, "action": s.action,
                "blocked": s.blocked}

    return {"stage": j.stage, "missing": list(j.missing), "out_of_scope": j.out_of_scope,
            "steps": [step(s) for s in j.steps], "next": step(j.next) if j.next else None}


def deployment(row, label, reason):
    row = _row(row)
    return {"environment": row["environment"], "url": row["url"], "username": row.get("username") or "",
            "secret_ref": row.get("secret_ref") or "", "verification": row.get("verification") or "unverified",
            "label": label(row), "reason": reason(row), "observed_commit": row.get("observed_commit") or None,
            "checked_at": row.get("checked_at") or None}


def release_summary(row, label):
    """`label` is the release's readable number (registry.release_labels)."""
    return {"id": row["id"], "version": row["version"], "label": label, "revision": row["revision"],
            "commit_sha": row["commit_sha"], "archive_sha": row["archive_sha"] or "", "state": row["state"]}


def package_summary(row):
    return {"rev": row["rev"], "revision": row["revision"], "sha256": row["sha256"], "size": row["size"],
            "approved_by": row["approved_by"], "approved_at": row["approved_at"]}


def flow_step(step):
    out = {"n": step.get("n", step.get("seq", 0)), "action": step.get("action") or "other"}
    for key in ("seq", "event_id", "key", "control", "label", "screen"):
        if key in step:
            out[key] = step[key]
    return out


def event(item, completeness=None, in_gap=None):
    out = {"event_id": item["event_id"], "seq": item["seq"], "action": item.get("action") or "other",
           "source": item.get("source") or "Unknown source"}
    for key in ("key", "control", "label", "screen", "time", "channel"):
        if item.get(key) is not None:
            out[key] = item[key]
    out["in_gap"] = bool(in_gap and in_gap(completeness, item.get("time"), item.get("channel")))
    return out


def recording_status(status):
    return {key: status.get(key) for key in ("label", "summary", "attention", "accepted", "accepted_by",
                                              "accepted_at", "workstation", "completeness", "preflight")}


def build(row, outstanding=0):
    row = _row(row)
    return {
        "id": row["id"], "app_id": row["app_id"], "provider": builds.provider_of(row),
        "provider_label": providers.LABELS.get(builds.provider_of(row), builds.provider_of(row)),
        "model": row.get("model") or "", "state": row["state"], "state_label": builds.label(row["state"]),
        "final": row["state"] in builds.FINAL, "revision": row["revision"], "package_rev": row.get("package_rev"),
        "pack_sha256": row["pack_sha256"], "repo": row["repo"], "base_branch": row["base_branch"],
        "base_sha": row["base_sha"], "branch": row.get("branch") or "", "parent_build": row.get("parent_build"),
        "budget": row["budget"] if row.get("budget") is not None else row.get("acu_budget") or 0,
        "budget_unit": row.get("budget_unit") or "", "cost": row.get("cost"), "cost_unit": row.get("cost_unit") or "",
        "pr_number": row.get("pr_number"), "pr_url": row.get("pr_url"), "head_sha": row.get("head_sha"),
        "outstanding": outstanding, "requested_by": row["requested_by"], "created_at": row["created_at"],
    }


def build_full(row, outstanding=0):
    row = _row(row)
    out = build(row, outstanding)
    out.update({key: row.get(key) for key in ("error", "note", "previous_revision", "provider_session_id",
                                              "provider_url", "provider_status", "provider_detail")})
    out["change_summary"] = row.get("change_summary") or ""
    return out


def message(row, answered=None):
    """A chat message; `decision` is the answer given to a tool approval (approve / deny)."""
    row = _row(row)
    data = json.loads(row.get("data") or "{}")
    decision = None
    reply = (answered or {}).get(row["id"])
    if row["kind"] == "tool_approval" and row["direction"] == "from_provider" and reply is not None:
        decision = json.loads(reply["data"] or "{}").get("decision") or reply["body"] or None
    elif row["kind"] == "tool_approval" and row["direction"] == "to_provider":
        decision = data.get("decision") or row["body"] or None
    return {"id": row["id"], "direction": row["direction"], "kind": row["kind"], "author": row["author"],
            "body": row["body"], "created_at": row["created_at"], "state": row.get("state"),
            "state_label": builds.delivery_label(row["state"]) if row.get("state") else None,
            "error": row.get("error"), "reply_to": row.get("reply_to"), "scenario_id": row.get("scenario_id"),
            "data": data if isinstance(data, dict) else {}, "decision": decision}


def machine(row):
    row = _row(row)
    return {"id": row["id"], "name": row["name"], "paired_by": row["paired_by"], "paired_at": row["paired_at"],
            "last_seen": row.get("last_seen"), "recorder": row.get("recorder") or "",
            "contract_version": row.get("contract_version"), "revoked_at": row.get("revoked_at")}


def automation(row):
    row = _row(row)
    return {"id": row["id"], "name": row["name"], "kind": row.get("kind") or "", "notes": row.get("notes") or "",
            "apps": row.get("apps")}


def status_label(status):
    return journeys.STATUS_LABELS.get(status, str(status).replace("_", " "))
