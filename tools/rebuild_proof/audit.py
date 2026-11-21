"""Step 3b: did each blind builder stay in its folder? Reads each builder subagent's transcript
(JSON Lines, listed in `<evidence>/builds/agents.json` as {build: transcript path}) and lists
every tool call, every path outside the build folder, web tools, browser/MCP tools, and any
command that could start a GUI program.

    python tools/rebuild_proof/audit.py            # writes <evidence>/builds/audit.json

A path counts as inside when it is under `<evidence>/builds/<build>` (any spelling: C:\\..., C:/...,
/c/...). System paths a command names without reading the user's files (the Python or dotnet
executable, %TEMP% for a test database) are listed separately as "tooling", not as a breach.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, read_json, write_json  # noqa: E402

PATH = re.compile(r"(?i)(?:\b[a-z]:[\\/]|/c/)[^\"'\s;|&)<>]*")
TOOLING = ("python", "program files", "windows\\system32", "/usr/", "appdata/local/temp", "appdata\\local\\temp",
           "dotnet", ".nuget", "nul")
GUI = re.compile(r"(?i)(start-process|\bstart\s+\S+\.exe|casedesk\.exe(?!\s*--reset)|msedge|chrome\.exe|explorer\.exe|notepad)")


def norm(p: str) -> str:
    p = p.replace("\\\\", "\\").replace("\\", "/").lower()
    return re.sub(r"^/c/", "c:/", p)


def audit(build: str, transcript: Path) -> dict:
    allowed = norm(str(EVIDENCE / "builds" / build))
    calls = collections.Counter()
    outside, tooling, web, gui, touched = [], [], [], [], set()
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        content = (m.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name, inp = c.get("name"), c.get("input") or {}
            calls[name] += 1
            text = json.dumps(inp, ensure_ascii=False)
            # only the fields that name what is read, written or run (never file contents)
            where = " ".join(str(inp.get(k)) for k in ("file_path", "path", "notebook_path", "command", "pattern")
                             if isinstance(inp, dict) and inp.get(k))
            if name.startswith("mcp__") or name in ("WebFetch", "WebSearch"):
                web.append({"tool": name, "input": text[:200]})
            for p in PATH.findall(where):
                n = norm(p)
                if n.startswith(allowed):
                    touched.add(n[len(allowed):].lstrip("/").split("/")[0] or ".")
                elif any(t in n for t in TOOLING):
                    tooling.append({"tool": name, "path": p[:160]})
                else:
                    outside.append({"tool": name, "path": p[:160], "input": text[:240]})
            cmd = inp.get("command") if isinstance(inp, dict) else None
            if cmd and GUI.search(cmd):
                gui.append({"tool": name, "command": cmd[:240]})
            if name in ("Glob", "Grep") and not inp.get("path"):
                outside.append({"tool": name, "path": "(no path: the session's working directory)", "input": text[:200]})
    return {"build": build, "transcript": str(transcript), "tool_calls": dict(calls), "outside": outside,
            "tooling_paths": sorted({t["path"] for t in tooling})[:30], "web_or_browser": web,
            "gui_commands": gui, "inside_top_level": sorted(touched),
            "verdict": "stayed in its folder" if not outside and not web and not gui else "check the flagged calls"}


def main() -> int:
    agents = read_json(EVIDENCE / "builds" / "agents.json")
    out = []
    for build, transcript in agents.items():
        p = Path(transcript)
        out.append(audit(build, p) if p.is_file() else {"build": build, "verdict": "transcript not found", "transcript": transcript})
    write_json(EVIDENCE / "builds" / "audit.json", out)
    for a in out:
        print(f"{a['build']:20} {a['verdict']:28} calls={sum((a.get('tool_calls') or {}).values())} "
              f"outside={len(a.get('outside') or [])} web={len(a.get('web_or_browser') or [])} gui={len(a.get('gui_commands') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
