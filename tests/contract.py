"""api/openapi.yaml as a test oracle: find the operation of a request and check its response.

`check(method, path, status, content_type, body)` raises AssertionError when an /api/v1 response
is not what the spec says: a 2xx status the operation does not declare, a JSON body that does not
match the declared schema, or an error that is not a problem+json `Problem`. Every request the
test client (support.Client) makes goes through it, so every server test is a contract test too.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re

import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
import yaml

SPEC_PATH = Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
METHODS = ("get", "post", "put", "patch", "delete")
SPEC_URI = "urn:twinlab2:openapi"


@lru_cache(maxsize=1)
def spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def registry():
    return Registry().with_resource(SPEC_URI, Resource.from_contents(spec(), default_specification=DRAFT202012))


@lru_cache(maxsize=1)
def operations():
    """[(method, compiled path pattern, template, operation)] of every /api/v1 operation."""
    out = []
    for template, item in spec()["paths"].items():
        if not template.startswith("/api/v1/"):
            continue
        pattern = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$")
        for method in METHODS:
            if method in item:
                out.append((method, pattern, template, item[method]))
    # a literal segment wins over a parameter (…/packages/{rev} vs …/package-draft)
    out.sort(key=lambda o: (o[2].count("{"), -len(o[2])))
    return out


def operation(method, path):
    path = path.split("?", 1)[0]
    for m, pattern, template, op in operations():
        if m == method.lower() and pattern.match(path):
            return template, op
    return None, None


def resolve(node):
    while isinstance(node, dict) and "$ref" in node:
        name = node["$ref"].removeprefix("#/")
        node = spec()
        for part in name.split("/"):
            node = node[part]
    return node


def validate(schema, body, where):
    wrapped = {"$schema": "https://json-schema.org/draft/2020-12/schema", **_rebase(schema)}
    validator = jsonschema.Draft202012Validator(wrapped, registry=registry())
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        raise AssertionError(f"{where}: {first.message} at {'/'.join(map(str, first.absolute_path)) or '(root)'}\n"
                             f"body: {json.dumps(body)[:1500]}")


def _rebase(node):
    """Local refs (#/components/...) of a fragment, pointed at the whole spec."""
    if isinstance(node, dict):
        return {k: (SPEC_URI + v if k == "$ref" and isinstance(v, str) and v.startswith("#") else _rebase(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_rebase(v) for v in node]
    return node


seen = set()  # (method, template, status) of every checked response


def check(method, path, status, content_type, body_bytes):
    if not path.startswith("/api/v1/"):
        return
    template, op = operation(method, path)
    if op is None:
        assert status == 404, f"{method} {path}: not in api/openapi.yaml, answered {status}"
        return
    seen.add((method.upper(), template, status))
    where = f"{method.upper()} {template} → {status}"
    responses = op["responses"]
    declared = responses.get(str(status))
    if status >= 400:
        assert content_type.startswith("application/problem+json"), f"{where}: error is {content_type}"
        validate({"$ref": "#/components/schemas/Problem"}, json.loads(body_bytes), where)
        return
    assert declared is not None, f"{where}: status not declared (declared: {sorted(responses)})"
    declared = resolve(declared)
    content = declared.get("content") or {}
    if not content:
        assert not body_bytes, f"{where}: declared without a body, got {body_bytes[:200]!r}"
        return
    kind = content_type.split(";")[0].strip()
    assert kind in content, f"{where}: answered {kind}, declared {sorted(content)}"
    schema = content[kind].get("schema")
    if schema and "json" in kind and "ndjson" not in kind:  # JSON lines are not one JSON document
        validate(schema, json.loads(body_bytes), where)
