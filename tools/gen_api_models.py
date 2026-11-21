"""Write server/twinlab2/api_models.py: a Pydantic model per schema of api/openapi.yaml.

    python tools/gen_api_models.py          # rewrite the file
    python tools/gen_api_models.py --check  # exit 1 when the file is out of date (CI, tests)

The spec is the hand-written contract (the web app's types come from it too); the server's
request and response models are generated from it, so they cannot drift apart. Covered: objects
(required, nullable `type: [x, "null"]`, inline objects), arrays, enums, `$ref`, `allOf` (a base
and extra properties), `oneOf`/`anyOf` with null, `additionalProperties` maps, and names that are
not Python identifiers (aliases).
"""

from __future__ import annotations

import keyword
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "api" / "openapi.yaml"
OUT = ROOT / "server" / "twinlab2" / "api_models.py"


class Gen:
    def __init__(self, schemas):
        self.schemas = schemas
        self.blocks = []  # class definitions, dependencies first
        self.done = set()

    def ref_name(self, ref):
        return ref.rsplit("/", 1)[-1]

    def annotation(self, schema, owner, field):
        """The Python type of `schema`; inline objects become classes named Owner_Field."""
        if "$ref" in schema:
            name = self.ref_name(schema["$ref"])
            self.define(name)
            return name
        for key in ("oneOf", "anyOf"):
            if key in schema:
                parts = [self.annotation(s, owner, field) for s in schema[key]]
                return " | ".join(dict.fromkeys(parts))
        if "allOf" in schema:
            name = f"{owner}_{camel(field)}"
            self.define_object(name, schema)
            return name
        if "enum" in schema:
            values = [v for v in schema["enum"] if v is not None]
            out = f"Literal[{', '.join(repr(v) for v in values)}]"
            return out + (" | None" if None in schema["enum"] or nullable(schema) else "")
        types = schema.get("type")
        types = types if isinstance(types, list) else [types] if types else []
        base = [t for t in types if t != "null"]
        kind = base[0] if base else None
        if kind == "array":
            out = f"list[{self.annotation(schema.get('items', {}), owner, field + '_item')}]"
        elif kind == "object" or (kind is None and "properties" in schema):
            if schema.get("properties"):
                name = f"{owner}_{camel(field)}"
                self.define_object(name, schema)
                out = name
            elif isinstance(schema.get("additionalProperties"), dict):
                out = f"dict[str, {self.annotation(schema['additionalProperties'], owner, field + '_value')}]"
            else:
                out = "dict[str, Any]"
        else:
            names = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
            out = " | ".join(names.get(t, "Any") for t in base) if len(base) > 1 else names.get(kind, "Any")
        return out + (" | None" if "null" in types and out != "Any" else "")

    def define(self, name):
        if name in self.done:
            return
        self.done.add(name)
        schema = self.schemas[name]
        if schema.get("type") == "object" or "properties" in schema or "allOf" in schema:
            self.define_object(name, schema)
        else:
            self.blocks.append(f"{name} = {self.annotation(schema, name, 'value')}\n")

    def define_object(self, name, schema):
        bases, props, required = [], {}, set(schema.get("required", []))
        for part in schema.get("allOf", []):
            if "$ref" in part:
                base = self.ref_name(part["$ref"])
                self.define(base)
                bases.append(base)
            else:
                props.update(part.get("properties", {}))
                required |= set(part.get("required", []))
        props.update(schema.get("properties", {}))
        lines = [f"class {name}({', '.join(bases) or 'Model'}):"]
        doc = (schema.get("description") or "").strip().splitlines()
        if doc:
            lines.append(f'    """{doc[0].replace(chr(34) * 3, "")}"""')
        fields = []
        for prop, sub in props.items():
            kind = self.annotation(sub, name, prop)
            attr = prop if prop.isidentifier() and not keyword.iskeyword(prop) else re.sub(r"\W", "_", prop) + "_"
            alias = f"alias={prop!r}" if attr != prop else ""
            if prop in required:
                default = f" = Field({alias})" if alias else ""
            else:
                if "| None" not in kind and kind != "Any":
                    kind += " | None"
                default = f" = Field(None, {alias})" if alias else " = None"
            fields.append(f"    {attr}: {kind}{default}")
        if not fields and not bases:
            fields.append("    pass")
        if not fields and bases:
            fields.append("    pass")
        self.blocks.append("\n".join(lines + fields) + "\n")


def nullable(schema):
    types = schema.get("type")
    return isinstance(types, list) and "null" in types


def camel(text):
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]", text) if part)


HEADER = '''"""The request and response models of /api/v1: one per schema of api/openapi.yaml.

Generated by tools/gen_api_models.py from the spec: do not edit; change the spec and run it again
(a test fails when this file is out of date).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

'''


def render():
    schemas = yaml.safe_load(SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    gen = Gen(schemas)
    for name in schemas:
        gen.define(name)
    names = sorted(n for n in gen.done if n in schemas)
    body = "\n\n".join(gen.blocks)
    rebuild = "\n".join(f"{n}.model_rebuild()" for n in names
                        if any(b.startswith(f"class {n}(") for b in gen.blocks))
    return HEADER + "\n" + body + "\n\n" + rebuild + "\n"


def main():
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current.replace("\r\n", "\n") != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run python tools/gen_api_models.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
