"""Machines, recording sessions, applications found by recordings, and automations.

The workstation recorder (agent/) never uploads a recording. It sends **references** at the end
of a session: the session, the applications it recorded (identity and level), and per
application a flow of filtered step labels (twinlab2_recorder.workstation.flow, from Twin Lab). This module:

- pairs machines: a signed-in user makes a one-time code (10 characters from a 32-symbol
  alphabet, valid 10 minutes, single use); the recorder exchanges it for a machine token, of
  which only the SHA-256 is stored. Machines can be revoked;
- serves the agent API under `/api/agent/`: bearer token only, JSON with a fixed schema;
- offers sessions to a machine (made in the portal: name, target applications, automation):
  recording starts only when the person at the PC presses Start;
- maps references to applications by identity key: one not in the catalog is registered with
  status `discovered`. The recording is linked to every application it recorded, with that
  application's flow and the selected steps (the catalog's evidence links), and a session made
  for an automation links the automation to those applications.

This module is the rules and the data; the HTTP routes (the agent API, and /api/v1 for machines,
sessions, recordings, identities, merging and automations) are in routes_registry.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from twinlab2 import completeness as compl
from twinlab2 import event_ids
from twinlab2_contract import recorder_contract as contract

# The tables are created and changed only by migrations.py.

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O or 1/I
CODE_LENGTH = 10
CODE_MINUTES = 10
FAILURES_PER_10_MIN = 20
STATUSES = ("known", "discovered", "not_replicated")
LEVELS = ("full", "steps")
KEY = re.compile(r"^(desktop:[^\x00-\x1f]{1,200}|exe:[\w .()+-]{1,80}\.exe"
                 r"|web:https?://[\w.\-\[\]:]{1,200}(/[^\s\x00-\x1f?#]{1,300})?"
                 r"|mainframe:[\w.\-\[\]:]{1,200}:\d{1,5})$",
                 re.I)  # fmt: skip
SESSION_KEY = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
FLOW_FIELDS = {"n": int, "action": str, "control": str, "label": str, "screen": str, "key": str,
               "channel": str, "captured_at": str, "seq": int}  # and event_id, checked on its own


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def web_parts(key):
    """`web:https://host/orders` -> (`web:https://host`, `/orders`); a host-only key has path ``."""
    rest = key[4:]
    cut = rest.find("/", rest.find("//") + 2)
    return ("web:" + rest, "") if cut < 0 else ("web:" + rest[:cut], rest[cut:])


def path_matches(prefix, path):
    return not prefix or path == prefix or path.startswith(prefix + "/")


def normalise_key(text):
    """An identity key from what a person types: `desktop:Company/Product`, an executable
    name (`exe:CaseDesk.exe`), or a web address (`web:https://host[:port][/path]`). A web
    address keeps its path (without query, fragment or trailing slash): applications that
    share a host are told apart by path, the longest matching path prefix winning."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        text = "web:" + text
    if text.lower().startswith(("tn3270://", "tn3270s://")):
        text = "mainframe:" + text.split("://", 1)[1].rstrip("/")
    if text.startswith("mainframe:"):
        rest = text[10:].lower()
        text = "mainframe:" + (rest if re.search(r":\d{1,5}$", rest) else rest + ":23")
    if text.lower().endswith(".exe") and ":" not in text:
        text = "exe:" + text
    if text.startswith("web:"):
        try:
            parts = urlsplit(text[4:])
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname or parts.username:
            return None
        default = {"http": 80, "https": 443}[scheme]
        host = parts.hostname.lower()
        path = re.sub(r"/{2,}", "/", parts.path or "").rstrip("/")
        text = f"web:{scheme}://{host}" + (f":{port}" if port and port != default else "") + path
    if text.startswith("exe:"):
        text = "exe:" + text[4:].lower()
    return text if KEY.match(text) else None


def target_of(key, level, app_id, name):
    target = {"level": level, "app_id": app_id, "name": name}
    if key.startswith("web:"):
        # the recorder records the whole origin; the portal assigns steps to an application
        # by path when the references arrive
        origin, path = web_parts(key)
        target["origin"] = origin[4:]
        if path:
            target["path"] = path
    elif key.startswith("exe:"):
        target["exe"] = key[4:]
    elif key.startswith("mainframe:"):
        target["tn3270"] = key[10:]
    else:
        target["key"] = key
    return target


# --- validation of what a recorder sends ---------------------------------------------------


def _text(value, limit, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or re.search(r"[\x00-\x1f]", value):
        raise ValueError("invalid text")
    return value


def clean_references(doc):
    """The references in the fixed schema, or ValueError. Anything not in the schema is dropped."""
    if not isinstance(doc, dict) or doc.get("format") != "twinlab-references/1":
        raise ValueError("not twinlab-references/1")
    s = doc.get("session")
    if not isinstance(s, dict) or not SESSION_KEY.match(str(s.get("id") or "")):
        raise ValueError("session.id")
    out_session = {"id": s["id"], "name": _text(s.get("name") or s["id"], 120, True)}
    for k in ("started", "ended"):
        out_session[k] = _text(s.get(k), 40)
    out_session["state"] = s.get("state") if s.get("state") in ("stopped", "recovered") else "stopped"
    for k in ("automation_id", "portal_session_id"):
        if isinstance(s.get(k), int) and not isinstance(s.get(k), bool):
            out_session[k] = s[k]
    commit = s.get("twinlab_commit")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit):
        out_session["twinlab_commit"] = commit
    apps = []
    for a in (doc.get("apps") or [])[:50]:
        if not isinstance(a, dict):
            raise ValueError("apps")
        key = normalise_key(a.get("key"))
        if key is None or key.startswith("exe:"):
            raise ValueError("app key")
        if a.get("level") not in LEVELS:
            raise ValueError("app level")
        surface = key.split(":", 1)[0] if key.startswith(("web:", "mainframe:")) else "desktop"
        item = {"key": key, "level": a["level"], "surface": surface}
        for k, limit in (("display", 120), ("exe", 120), ("company", 120), ("product", 120),
                         ("version", 40), ("toolkit", 40)):  # fmt: skip
            value = a.get(k)
            if isinstance(value, str) and value and len(value) <= limit and not re.search(r"[\x00-\x1f]", value):
                item[k] = value
        if isinstance(a.get("app_id"), int) and not isinstance(a.get("app_id"), bool):
            item["app_id"] = a["app_id"]
        apps.append(item)
    keys = {a["key"] for a in apps}
    flows = {}
    raw_flows = doc.get("flows") if isinstance(doc.get("flows"), dict) else {}
    seen_ids = set()
    for key, steps in raw_flows.items():
        nkey = normalise_key(key)
        if nkey not in keys or not isinstance(steps, list):
            continue
        clean = []
        if len(steps) > 50000:
            raise ValueError("flow longer than 50000 steps")
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("n"), int):
                raise ValueError("flow step")
            row = {}
            for field, kind in FLOW_FIELDS.items():
                value = step.get(field)
                if isinstance(value, kind) and not isinstance(value, bool) and (
                    kind is int or (0 < len(value) <= 160 and not re.search(r"[\x00-\x1f]", value))
                ):
                    row[field] = value
            if row.get("channel") not in (None, *compl.CHANNELS):
                del row["channel"]
            if step.get("event_id") is not None:
                event_id = event_ids.clean(step["event_id"])
                if event_id is None:
                    raise ValueError("event_id")
                if event_id in seen_ids:
                    raise ValueError(f"event_id {event_id} appears twice")
                seen_ids.add(event_id)
                row["event_id"] = event_id
            clean.append(row)
        flows[nkey] = clean
    timeline = []
    for run in (doc.get("timeline") or [])[:20000]:
        if not isinstance(run, dict) or not isinstance(run.get("steps"), int):
            raise ValueError("timeline")
        app = run.get("app")
        app = "presence" if app == "presence" else normalise_key(app)
        if app is None or (app != "presence" and app not in keys):
            raise ValueError("timeline app")
        timeline.append({"app": app, "steps": run["steps"]})
    out = {"format": "twinlab-references/1", "session": out_session, "apps": apps,
           "flows": flows, "timeline": timeline}  # fmt: skip
    for name, cleaner in (("completeness", compl.clean), ("preflight", compl.clean_preflight)):
        value = cleaner(doc.get(name))
        if value is not None:
            out[name] = value
    return out


def merge_references(old, new):
    """The references of a recording after another send of it: what the earlier sends had and
    this one adds. A recorder may send a recording in parts (a recovered session, then the rest)
    or send a batch again, so a send never removes what an earlier one delivered: applications
    are kept by identity key and steps by `event_id`, the latest send's copy of a step winning
    (also when it moved to another application). The session, completeness and preflight are
    the latest send's; the timeline too when it covers every application, else the earlier
    timeline followed by the new one. Both are `clean_references` output with event ids."""
    sent = {step["event_id"] for steps in new["flows"].values() for step in steps}
    apps = {a["key"]: a for a in old["apps"]}
    apps.update({a["key"]: a for a in new["apps"]})
    flows = {}
    for key in apps:
        steps = {s["event_id"]: s for s in old["flows"].get(key, []) if s["event_id"] not in sent}
        steps.update({s["event_id"]: s for s in new["flows"].get(key, [])})
        if key in old["flows"] or key in new["flows"]:
            flows[key] = sorted(steps.values(), key=lambda s: (s.get("captured_at") or "", s["n"], s["event_id"]))
    out = dict(new, apps=list(apps.values()), flows=flows)
    if not {a["key"] for a in old["apps"]} <= {a["key"] for a in new["apps"]}:
        timeline = []
        for run in old["timeline"] + new["timeline"]:
            if timeline and timeline[-1]["app"] == run["app"]:
                timeline[-1] = {"app": run["app"], "steps": timeline[-1]["steps"] + run["steps"]}
            else:
                timeline.append(dict(run))
        out["timeline"] = timeline
    return out


def discovered_name(db, item, shared_host=False):
    """The name a newly discovered application is registered under: what the recorder calls it
    (its display name, else its product, else its key), made unique by `unique_name`. With
    `shared_host` (steps on a web origin outside every path application on it) the recorder's
    display name is that of a path target, so the application is named after its host instead."""
    if shared_host:
        base = web_parts(item["key"])[0].split("://", 1)[-1]
    else:
        base = item.get("display") or item.get("product") or item["key"].split(":", 1)[-1]
    return unique_name(db, base, item)


def unique_name(db, base, item):
    """A name for a newly discovered application that no other application has (ignoring case),
    so that each one's package, folder and BUILD_TASK say which application it is: `base`, else
    `base (qualifier)` with the company or executable of a program or the host of a web or
    mainframe application, else `base 2`, `base 3`, …"""
    taken = {row[0].casefold() for row in db.execute("SELECT name FROM apps")}
    base = base[:120]
    if base.casefold() not in taken:
        return base
    key = item["key"]
    if key.startswith("web:"):
        qualifier = web_parts(key)[0].split("://", 1)[-1]
    elif key.startswith("mainframe:"):
        qualifier = key[10:]
    else:
        qualifier = item.get("company") or item.get("exe") or key.split(":", 1)[-1]
    candidate = f"{base} ({qualifier})"[:120]
    number = 2
    while candidate.casefold() in taken:
        candidate = f"{base[:110]} {number}"
        number += 1
    return candidate


def recording_status(db, recording_id):
    """What the pages say about a recording's completeness: the recorder's report (None from an
    older recorder or an imported event log), its preflight, the owner's acceptance, and the
    sentence and label to show. `attention` is True for a partial or failed recording nobody has
    accepted: a stopped recording is not complete by itself."""
    row = db.execute("SELECT * FROM recording_completeness WHERE recording_id = ?", (recording_id,)).fetchone()
    workstation = db.execute("SELECT 1 FROM session_refs WHERE recording_id = ?", (recording_id,)).fetchone()
    data = json.loads(row["completeness"]) if row is not None and row["completeness"] else None
    accepted = bool(row is not None and row["accepted_at"])
    if data is None:
        text = compl.UNKNOWN_OLDER if workstation else compl.UNKNOWN_IMPORTED
    else:
        text = compl.summary(data)
    return {"completeness": data, "accepted": accepted,
            "accepted_by": row["accepted_by"] if accepted else None, "accepted_at": row["accepted_at"] if accepted else None,
            "preflight": json.loads(row["preflight"]) if row is not None and row["preflight"] else None,
            "summary": text, "label": compl.label(data, accepted), "attention": compl.needs_attention(data, accepted),
            "workstation": bool(workstation)}


def contract_refusal(error):
    """The answer to a recorder outside the contract: what to update, and the versions.
    426 when the version is not accepted (update the recorder or the portal), 400 for a
    message of an accepted version that is not in the contract's shape."""
    body = {"error": str(error), "update": error.update,
            "contract": {"portal": contract.CONTRACT_VERSION, "supported": contract.supported()}}
    if error.errors:
        body["errors"] = error.errors[:20]
    return body, 400 if error.errors else 426


def recorder_of(doc, header=""):
    """The sending recorder's name and version, from the message (else the X-TwinLab-Recorder
    request header, `header`)."""
    rec = doc.get("recorder") if isinstance(doc.get("recorder"), dict) else {}
    if rec.get("name") and rec.get("version"):
        return str(rec["name"])[:60], str(rec["version"])[:40]
    name, _, version = (header or "").strip().partition(" ")
    return name[:60] or "unknown", version[:40] or "unknown"
