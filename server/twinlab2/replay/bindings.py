# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Synthetic input bindings: the values a replay types, one per recorded step.

A recording keeps no typed value. To replay it, a person (or a generator) writes the values in a
bindings file beside the recording, `bindings.yaml`, keyed by step:

```yaml
version: 1
recording: 20261117-101500
steps:
  4:
    action: type
    field: input.new-todo
    shape: A A          # what was typed, as its shape (letters A, digits 9): for reference
    value: Water the plants
  17:
    action: check
    record: Pay the bills   # a step on a record the recording did not create: its title (the
                            # row whose text holds it as a whole phrase, once; or the whole row text)
```

`value` is what a `type` or `select` step enters. `record` names the record (list item, table
row) a step acts on when the recording cannot: the record was already there when recording
started, or its id is masked and several records share its shape. A step on a record created in
the recording needs nothing: its title is the value bound to the step that typed it.

A `select` step can be bound by position instead (`index: 2`), an `upload` step needs a synthetic
file (`file: synthetic.pdf`, relative to the bindings file; never the original), and a prompt
dialog answered with text needs its `value`. A split part's bindings may also have `outside:`,
the values of other applications' steps it refers to (`<app>#<step>`), supplied from outside.

Twin Lab's `behaviour bindings <recording>` writes the template, listing every step that needs one.
The older `--value selector=value` and `--value 6:selector=value` still work: they are turned into
bindings for the steps they match. A step that needs a value with no binding blocks the replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from twinlab2_contract.evidence import dom as domlib
from twinlab2_contract.evidence.recording import Recording, RecStep

FILE = "bindings.yaml"
VALUED = ("type", "select")


@dataclass
class Bindings:
    values: dict[int, str] = field(default_factory=dict)  # seq -> value typed or selected
    records: dict[int, str] = field(default_factory=dict)  # seq -> title of the record acted on
    # Added for split parts and R12 (all optional):
    outside: dict[str, str] = field(default_factory=dict)  # "<app>#<step>" -> value from outside
    indexes: dict[int, int] = field(default_factory=dict)  # select: seq -> option index
    files: dict[int, str] = field(default_factory=dict)  # upload: seq -> synthetic file path
    # Steps whose value a generator wrote (Twin Lab's seedplan, with the seed), not a person: written
    # as `generated: <the value>`, so a value a person changes afterwards is theirs and is kept.
    generated: set[int] = field(default_factory=set)
    # Steps whose record a generator read from the evidence (the clicked row holds exactly one
    # seed row's title): written as `generated: <the title>` beside `record:`, kept the same way.
    generated_records: set[int] = field(default_factory=set)

    def merged(self, other: Bindings) -> Bindings:
        """These bindings, with `other` filling the steps these do not bind."""
        gen = {s for s in other.generated if s not in self.values} | set(self.generated)
        return Bindings({**other.values, **self.values}, {**other.records, **self.records},
                        {**other.outside, **self.outside}, {**other.indexes, **self.indexes},
                        {**other.files, **self.files}, gen)  # fmt: skip

    def from_outside(self, ref) -> str | None:
        """The value of another application's step (`{app, step}`), supplied from outside."""
        if isinstance(ref, dict):
            return self.outside.get(f"{ref.get('app')}#{ref.get('step')}")
        return None


@dataclass
class Need:
    seq: int
    kind: str  # "value" | "record"
    action: str
    field: str | None
    shape: str | None = None
    note: str | None = None


def record_candidate(step: RecStep) -> dict | None:
    """The step's per-record candidate when it is the primary one (its first locator)."""
    locs = [c for c in step.locators if c.get("kind") not in ("id", "testid")]
    if locs and locs[0].get("kind") == "record":
        return locs[0]
    return None


def needs_value(step: RecStep) -> bool:
    if step.action == "dialog":  # a prompt answered: its text was typed
        d = step.dialog or {}
        return d.get("kind") == "prompt" and d.get("choice") == "accept" and step.blank is None
    return step.action in VALUED and step.blank is None and step.value is None


def needs(rec: Recording) -> list[Need]:
    """Every step that needs a binding, in order. A record titled with a value typed in another
    application (`text_from_outside`) needs none here: a split part's bindings carry it under
    `outside:`."""
    out: list[Need] = []
    for step in rec.steps:
        if needs_value(step) and recorded_value(rec, step) is None:
            out.append(
                Need(
                    step.seq,
                    "value",
                    step.action,
                    step.selector,
                    step.typed_shape,
                    None if step.typed_shape else "shape not known",
                )  # fmt: skip
            )
        cand = record_candidate(step)
        if cand is not None and cand.get("text_from") is None and not cand.get("text_from_outside"):
            out.append(
                Need(
                    step.seq,
                    "record",
                    step.action,
                    step.selector,
                    cand.get("title_shape"),
                    "acts on a record the recording did not create: write the title of the row "
                    "it acts on as db/seed.sql holds it (the one row whose text holds that title "
                    "as a whole phrase), or the row's whole text",
                )  # fmt: skip
            )
    return out


def file_needs(rec: Recording) -> list[Need]:
    """The upload steps: each needs a synthetic file (`file:`), never the original. Kept apart
    from `needs` (values and records), whose two kinds its readers know."""
    return [Need(s.seq, "file", s.action, s.selector,
                 ", ".join(f.get("name_shape") or "?" for f in s.files) or None,
                 "a synthetic file to upload (file: path), never the original")
            for s in rec.steps if s.action == "upload"]  # fmt: skip


def where_to_check(rec: Recording, seq: int) -> str | None:
    """The files that show the record a step acted on: the page as the action met it
    (`dom/<seq>.before.html`) and the first recorder picture from that step on. A click gets
    no picture of its own (it is observed where its page settled), so that picture is usually
    the next step's: `screens/<seq>.png` of the click itself does not exist."""
    from twinlab2.replay.visual import pictures_of

    step = next((s for s in rec.steps if s.seq == seq), None)
    if step is None or not rec.path:
        return None
    parts = []
    if step.dom_before and (Path(rec.path) / step.dom_before).is_file():
        parts.append(f"{step.dom_before} (the page as the step met it)")
    pictures = pictures_of(Path(rec.path))
    later = sorted(p for p in pictures if p >= seq)
    if later:
        whose = "this step's picture" if later[0] == seq else f"the picture after step {later[0]}"
        parts.append(f"{pictures[later[0]]} ({whose})")
    return "; ".join(parts) or None


def template(rec: Recording, existing: Bindings | None = None) -> str:
    """The bindings file for a recording: every step that needs a value, with its shape."""
    existing = existing or Bindings()
    steps: dict[int, dict] = {}
    by_seq = {s.seq: s for s in rec.steps}
    for n in sorted([*needs(rec), *file_needs(rec)], key=lambda n: n.seq):
        entry = steps.setdefault(n.seq, {"action": n.action, "field": n.field})
        if n.kind == "value":
            entry["shape"] = n.shape
            entry["value"] = existing.values.get(n.seq)
            if n.seq in existing.generated and entry["value"] is not None:
                entry["generated"] = entry["value"]
            step = by_seq.get(n.seq)
            if step is not None and step.action == "select":
                if existing.indexes.get(n.seq) is not None:
                    entry["index"] = existing.indexes[n.seq]
                elif step.option_index is not None:
                    entry["recorded_index"] = step.option_index  # a position, not a value
        elif n.kind == "file":
            entry["file_shape"] = n.shape
            entry["file"] = existing.files.get(n.seq)
        else:
            entry["record_shape"] = n.shape
            entry["record"] = existing.records.get(n.seq)
            if n.seq in existing.generated_records and entry["record"] is not None:
                entry["generated"] = entry["record"]
                n.note = "read from the evidence: the clicked element holds this seed row's title"
            seen = where_to_check(rec, n.seq)
            if seen:
                entry["check_against"] = seen
        if n.note:
            entry["note"] = n.note
    head = (
        "# Synthetic values for replaying this recording, one per step. Never a real value.\n"
        "# value: what the step types or selects (or index: the option's position). record: the\n"
        "# title of the record the step acts on. file: a synthetic file to upload. shape is what\n"
        "# was recorded (letters A, digits 9), for reference only. check_against: the files that\n"
        "# show the record a step acted on (a click has no picture of its own).\n"
    )
    body: dict = {"version": 1, "recording": rec.id, "steps": steps}
    if existing.outside:
        body["outside"] = dict(existing.outside)
    return head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def write_template(rec: Recording, out: Path | None = None) -> Path:
    """Write (or complete) the bindings file; values already given are kept."""
    out = Path(out) if out else rec.path / FILE
    existing = load(out) if out.exists() else None
    out.write_text(template(rec, existing), encoding="utf-8")
    return out


def load(path: Path | None) -> Bindings:
    """Read a bindings file. `steps: {4: "text"}` is short for `{4: {value: "text"}}`."""
    if path is None or not Path(path).exists():
        return Bindings()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a bindings file")
    out = Bindings()
    for key, entry in (data.get("steps") or {}).items():
        try:
            seq = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: step {key!r} is not a step number") from exc
        if isinstance(entry, (str, int, float)):
            out.values[seq] = str(entry)
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("value") is not None:
            out.values[seq] = str(entry["value"])
            if entry.get("generated") is not None and str(entry["generated"]) == out.values[seq]:
                out.generated.add(seq)
        if entry.get("record") is not None:
            out.records[seq] = str(entry["record"])
            if entry.get("generated") is not None and str(entry["generated"]) == out.records[seq]:
                out.generated_records.add(seq)
        if isinstance(entry.get("index"), int) and not isinstance(entry["index"], bool):
            out.indexes[seq] = entry["index"]
        if entry.get("file"):
            f = Path(str(entry["file"]))
            out.files[seq] = str(f if f.is_absolute() else Path(path).parent / f)
    for key, value in (data.get("outside") or {}).items():
        if value is not None:
            out.outside[str(key)] = str(value)
    return out


def selector_of(name: str) -> str:
    """The selector of a `values` key: `"6:#loanQuery"` -> `"#loanQuery"`."""
    head, sep, rest = name.partition(":")
    return rest if sep and head.isdigit() else name


def from_values(rec: Recording, values: dict[str, str] | None) -> Bindings:
    """The older per-selector values as bindings: `6:sel` binds step 6, `sel` every step that
    types into that field and has no other value."""
    out = Bindings()
    for step in rec.steps if values else []:
        if step.action not in VALUED or not step.selector:
            continue
        for key in (f"{step.seq}:{step.selector}", step.selector):
            if key in values:
                out.values[step.seq] = values[key]
                break
    return out


def recorded_value(rec: Recording, step: RecStep) -> str | None:
    """A value the recording itself holds: written by the harness bot (`value`, never for a
    Private field), or, for older recordings, the field's value in the after-snapshot."""
    if step.value is not None:
        return step.value
    if step.blank is not None:
        return step.blank
    if new_format(rec):  # this recorder takes typed values out of its snapshots
        return None
    html = rec.dom(step)
    key = domlib.key_of(step.selector)
    if html is None or key is None:
        return None
    el = domlib.snapshot(html).elements.get(key)
    if el is None:
        return None
    return el.selected if el.tag == "select" else el.value


def new_format(rec: Recording) -> bool:
    """Written by the recorder that keeps locators (and takes typed values out of snapshots)."""
    return any(s.locators or s.nav for s in rec.steps)


def supplied_keys(rec: Recording, bindings: Bindings) -> set[str]:
    """Element keys of fields given a value for the replay: their value format is not compared."""
    out = set()
    for step in rec.steps:
        if step.seq in bindings.values:
            key = domlib.key_of(step.selector)
            if key:
                out.add(key)
    return out
