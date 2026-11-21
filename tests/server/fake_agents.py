"""Fake agent entry points for the runner (TWINLAB2_CLAUDE_AGENT=fake_agents:claude): no SDK, no
network, no money. What they do is read from TWINLAB2_FAKE_AGENT (JSON) in the runner's
environment:

    ask    a question to end the first turn with ('QUESTION: …'), answered before it goes on
    tool   a tool to ask approval for (its decision is written to TOOL.txt)
    hold   wait until interrupted (or cancelled), on the first run of the process only
    cost   the cost of each turn (default 0.25)

Every turn appends its prompt to agent-prompts.txt in the session folder (outside the repo) and
writes app.py in the working tree.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from twinlab2.providers.runner import AgentResult

_held = False


async def claude(prompt, ctx):
    global _held
    plan = json.loads(os.environ.get("TWINLAB2_FAKE_AGENT") or "{}")
    folder = Path(ctx.cwd).parent
    with (folder / "agent-prompts.txt").open("a", encoding="utf-8") as out:
        out.write(json.dumps({"prompt": prompt, "resume": ctx.resume, "model": ctx.model,
                              "remaining": ctx.remaining, "restarted": ctx.restarted, "env_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                              "env_github": bool(os.environ.get("TWINLAB2_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"))})
                  + "\n")
    session_id = ctx.resume or "fake-agent-session"
    cost = float(plan.get("cost", 0.25))
    if plan.get("hold") and not _held and not ctx.resume and not ctx.restarted:
        _held = True
        while not ctx.interrupted.is_set():
            ctx.check_controls()
            await asyncio.sleep(0.1)
        return AgentResult(session_id=session_id, text="", cost=cost, interrupted=True)
    if plan.get("ask") and not ctx.resume:
        return AgentResult(session_id=session_id, text=f"I need one decision.\nQUESTION: {plan['ask']}", cost=cost)
    if plan.get("tool"):
        target = Path(ctx.cwd) / "app.py"
        assert ctx.allows("Write", {"file_path": str(target)})
        assert not ctx.allows("Write", {"file_path": str(Path(ctx.cwd).parent / "outside.txt")})
        approved = await ctx.ask_tool(plan["tool"], {"command": "npm test"})
        (Path(ctx.cwd) / "TOOL.txt").write_text("approved" if approved else "denied", encoding="utf-8")
    (Path(ctx.cwd) / "app.py").write_text(f"# built by a fake agent\nPROMPT_CHARS = {len(prompt)}\n", encoding="utf-8")
    ctx.emit("message", "Built app.py.")
    return AgentResult(session_id=session_id, text="Done.", cost=cost)


copilot = claude
