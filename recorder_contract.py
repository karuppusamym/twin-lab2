"""The versioned boundary between a workstation recorder and the portal.

`contract/recorder-contract.schema.json` is the machine-checkable contract and
`RECORDER-CONTRACT.md` explains it. This module is what both sides run:

- `CONTRACT_VERSION`: the version this portal and this repository's recorder implement;
- `compatibility(version)`: whether a sender's `contract_version` is accepted, and if not, what
  the person has to update (the recorder or the portal);
- `validate(doc, message)`: the document against the schema's `pair` or `references` message.

The validator is a small, dependency-free subset of JSON Schema 2020-12: the keywords the
schema uses (type, required, properties, additionalProperties, items, enum, const, pattern,
minLength, maxLength, minimum, maxItems, $ref to #/$defs, anyOf, oneOf). The tests cross-check
it with the `jsonschema` package when that is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

CONTRACT_VERSION = "1.0.0"
OLDEST_SUPPORTED = "1.0.0"  # the oldest contract version this portal still accepts
RECORDER_MIN = "0.2.0"  # the first TwinLabRecorder that declares a contract version
SCHEMA_PATH = Path(__file__).resolve().parent / "contract" / "recorder-contract.schema.json"
VERSION = re.compile(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")


class ContractError(ValueError):
    """A message outside the contract. `update` names who has to change: recorder or portal."""

    def __init__(self, message: str, update: str | None = None, errors: list[str] | None = None):
        super().__init__(message)
        self.update = update
        self.errors = errors or []


def parse(version) -> tuple[int, int, int] | None:
    match = VERSION.match(version) if isinstance(version, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def supported() -> str:
    """The accepted range, for messages and the portal's answers."""
    return f">={OLDEST_SUPPORTED} <{parse(CONTRACT_VERSION)[0] + 1}.0.0"


def compatibility(version) -> tuple[int, int, int]:
    """The parsed version when this portal accepts it, else ContractError.

    Same major as the portal and not older than OLDEST_SUPPORTED. A newer minor or patch is
    accepted: within a major, versions only add optional fields, which this portal ignores."""
    mine = parse(CONTRACT_VERSION)
    if version is None:
        raise ContractError(
            "this recorder does not declare a contract_version: it was built before the versioned "
            f"recorder contract. Update TwinLabRecorder to {RECORDER_MIN} or later (this portal "
            f"speaks contract {CONTRACT_VERSION}).", update="recorder")  # fmt: skip
    theirs = parse(version)
    if theirs is None:
        raise ContractError(f"contract_version {str(version)[:20]!r} is not MAJOR.MINOR.PATCH", update="recorder")
    if theirs[0] > mine[0]:
        raise ContractError(
            f"this recorder speaks contract {version}, newer than this portal's {CONTRACT_VERSION} "
            f"(supported {supported()}). Update the portal, or use a recorder for contract "
            f"{mine[0]}.x.", update="portal")  # fmt: skip
    if theirs < parse(OLDEST_SUPPORTED):
        raise ContractError(
            f"this recorder speaks contract {version}, which this portal no longer accepts "
            f"(supported {supported()}). Update TwinLabRecorder to a build for contract "
            f"{CONTRACT_VERSION}.", update="recorder")  # fmt: skip
    return theirs


# --- the schema ------------------------------------------------------------------------------

_SCHEMA = None


def schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        if _SCHEMA.get("x-contract-version") != CONTRACT_VERSION:
            raise RuntimeError("the schema file and recorder_contract.CONTRACT_VERSION differ")
    return _SCHEMA


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _equal(a, b) -> bool:
    return type(a) is type(b) and a == b


def _check(value, node: dict, path: str, errors: list[str]) -> None:  # noqa: C901 - one keyword each
    root = schema()
    if "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/"):
            raise RuntimeError(f"unsupported $ref {ref}")
        _check(value, root["$defs"][ref[8:]], path, errors)
    if "type" in node and not _TYPES[node["type"]](value):
        errors.append(f"{path}: expected {node['type']}")
        return
    if "const" in node and not _equal(value, node["const"]):
        errors.append(f"{path}: must be {node['const']!r}")
    if "enum" in node and not any(_equal(value, option) for option in node["enum"]):
        errors.append(f"{path}: must be one of {', '.join(map(str, node['enum']))}")
    for keyword in ("anyOf", "oneOf"):
        if keyword in node:
            passed = 0
            for option in node[keyword]:
                trial: list[str] = []
                _check(value, option, path, trial)
                passed += not trial
            if passed == 0 or (keyword == "oneOf" and passed > 1):
                errors.append(f"{path}: matches {passed} of the allowed forms")
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{path}: empty" if node["minLength"] == 1 else f"{path}: too short")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{path}: longer than {node['maxLength']} characters")
        if "pattern" in node and not re.search(node["pattern"], value):
            errors.append(f"{path}: not in the expected form")
    if _TYPES["integer"](value) and "minimum" in node and value < node["minimum"]:
        errors.append(f"{path}: less than {node['minimum']}")
    if isinstance(value, list):
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.append(f"{path}: more than {node['maxItems']} items")
        if "items" in node:
            for i, item in enumerate(value):
                _check(item, node["items"], f"{path}[{i}]", errors)
                if len(errors) > 20:
                    return
    if isinstance(value, dict):
        for name in node.get("required", ()):
            if name not in value:
                errors.append(f"{path}.{name}: required")
        props = node.get("properties", {})
        extra = node.get("additionalProperties", True)
        for name, item in value.items():
            if name in props:
                _check(item, props[name], f"{path}.{name}", errors)
            elif extra is False:
                errors.append(f"{path}.{name}: not allowed")
            elif isinstance(extra, dict):
                _check(item, extra, f"{path}[{name!r}]", errors)
            if len(errors) > 20:
                return


def errors(doc, message: str) -> list[str]:
    """Every way `doc` is outside the `pair` or `references` message (empty when it conforms)."""
    if message not in ("pair", "references"):
        raise ValueError(message)
    out: list[str] = []
    _check(doc, {"$ref": f"#/$defs/{message}"}, "$", out)
    return out


def validate(doc, message: str) -> tuple[int, int, int]:
    """Check the version first (so an old or newer recorder hears what to update, not a list of
    field errors), then the shape. Returns the sender's parsed contract version."""
    version = compatibility(doc.get("contract_version") if isinstance(doc, dict) else None)
    found = errors(doc, message)
    if found:
        raise ContractError(f"outside recorder contract {CONTRACT_VERSION}: " + "; ".join(found[:5]),
                            update="recorder", errors=found)  # fmt: skip
    return version
