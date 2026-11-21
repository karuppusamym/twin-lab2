# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""The small CSS-selector matcher of Twin Lab's `behaviour.drafts` module, which the replay's
scope exclusions use (`scope.subtree_names`). The rest of `drafts` writes templates."""

from __future__ import annotations

import re


_PART = re.compile(r"^([a-zA-Z][\w-]*|\*)?((?:[#.][\w-]+)*)((?::nth-child\(\d+\))?)$")


def _compound(text: str):
    m = _PART.match(text.strip())
    if not m:
        return None
    tag = (m.group(1) or "*").lower()
    ids = re.findall(r"#([\w-]+)", m.group(2))
    classes = re.findall(r"\.([\w-]+)", m.group(2))
    nth = re.search(r"\d+", m.group(3) or "")
    return tag, ids, classes, int(nth.group()) if nth else None


def _parse(selector: str):
    tokens = re.split(r"\s*(>)\s*|\s+", selector.strip())
    parts, comb = [], " "
    for t in [x for x in tokens if x]:
        if t == ">":
            comb = ">"
            continue
        c = _compound(t)
        if c is None:
            return None
        parts.append((comb, c))
        comb = " "
    return parts


def _fits(node, c) -> bool:
    tag, ids, classes, nth = c
    if tag != "*" and node.tag != tag:
        return False
    if any(node.id != i for i in ids):
        return False
    have = set(node.classes)
    if any(k not in have for k in classes):
        return False
    if nth is not None:
        sibs = [n for n in (node.parent.children if node.parent else []) if n.tag]
        at = next((i for i, x in enumerate(sibs, 1) if x is node), None)  # by identity
        if at != nth:
            return False
    return True


def _matches(node, parts) -> bool:
    if not parts:
        return True
    comb, c = parts[-1]
    if not _fits(node, c):
        return False
    rest = parts[:-1]
    if not rest:
        return True
    up = node.parent
    if comb == ">":
        return up is not None and up.tag != "" and _matches(up, rest)
    while up is not None and up.tag:
        if _matches(up, rest):
            return True
        up = up.parent
    return False
