# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: only the
# definitions named below were taken from their module. Source and changes: docs/UPSTREAM.md.
"""Two small pieces of Twin Lab's builder modules that the replay uses: `normalise_origin`
(from its `split` module) and `BUSY_JS` (from its `jsfviews` module). The rest of those
modules builds packs and is not part of TwinLab2."""

from __future__ import annotations

from urllib.parse import urlsplit


DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_origin(url: str) -> str:
    """scheme://host[:port] in lower case, default ports left out; '' for anything not http(s)."""
    p = urlsplit(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    try:
        port = p.port
    except ValueError:
        return ""
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and port != DEFAULT_PORTS[p.scheme]:
        host += f":{port}"
    return f"{p.scheme}://{host}"


# Read-only: whether PrimeFaces still holds a request (queued, in flight, or a data table's filter
# waiting out its delay) or jQuery animates. The replay waits for it as the recorder does.
BUSY_JS = """() => {
  try {
    const pf = window.PrimeFaces, q = pf && pf.ajax && pf.ajax.Queue;
    if (q && ((q.requests && q.requests.length) || (q.xhrs && q.xhrs.length))) return true;
    if (pf && pf.widgets) for (const k in pf.widgets) if (pf.widgets[k] && pf.widgets[k].filterTimeout) return true;
    const jq = window.jQuery;
    return !!(jq && (jq.active > 0 || (jq.timers && jq.timers.length)));
  } catch (e) { return false; }
}"""
