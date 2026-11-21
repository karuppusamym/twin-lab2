# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""UiPath selector -> CSS, from Twin Lab's `analyse.uipath` module (`to_css` and what it
needs), for the robot log the recorder reads. The workflow analyser is not part of TwinLab2."""

from __future__ import annotations

import re


TEXT_ONLY_ATTRS = ("aaname", "innertext", "idx", "tableRow", "tableCol", "title", "text")


def _wild(attr: str, value: str) -> str | None:
    """A UiPath wildcard value as a CSS attribute test. None if it cannot be expressed."""
    if "?" in value:
        return None
    core = value.strip("*")
    if "*" in core or not core:
        return None
    op = {(True, True): "*=", (False, True): "^=", (True, False): "$="}[
        (value.startswith("*"), value.endswith("*"))
    ]
    return f"[{attr}{op}'{core}']"


def to_css(selector: str) -> str | None:
    """UiPath selector string -> CSS selector, or None when it has nothing CSS can test."""
    steps = []
    for tag, body in re.findall(r"<(\w+)((?:\s+[\w-]+\s*=\s*'[^']*')*)\s*/?>", selector):
        if tag.lower() != "webctrl":
            continue  # <html>, <wnd>: the window, not an element in the page
        attrs = dict(re.findall(r"([\w-]+)\s*=\s*'([^']*)'", body))
        if attrs.get("css-selector"):
            steps.append(attrs["css-selector"])
            continue
        css = attrs.get("tag", "").lower()
        for key, prefix in (("id", "#"), ("class", ".")):
            for value in attrs.get(key, "").split() if key == "class" else [attrs.get(key, "")]:
                if not value:
                    continue
                if "*" in value or "?" in value:
                    test = _wild(key, value)
                    if test is None:
                        return None
                    css += test
                else:
                    css += prefix + value
        for key in ("name", "type", "href"):
            if key in attrs:
                test = _wild(key, attrs[key]) if "*" in attrs[key] else f"[{key}='{attrs[key]}']"
                if test is None:
                    return None
                css += test
        if "parentid" in attrs:
            css = f"#{attrs['parentid']} {css or '*'}"
        if (
            not css
            or css == attrs.get("tag", "").lower()
            and any(a in attrs for a in TEXT_ONLY_ATTRS)
        ):
            return None
        if attrs.get("tag", "").lower() in ("iframe", "frame"):
            css += " >>>"  # what follows is inside this frame (twinlab2_contract.evidence.locate)
        steps.append(css)
    return " ".join(steps) or None
