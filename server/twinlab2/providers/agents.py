"""The agent entry points the runner calls (providers/runner.py): one turn of a coding agent in
the session's working tree. Each takes (prompt, ctx) and returns runner.AgentResult.

- `claude`: the Claude Agent SDK (`claude-agent-sdk`, the Claude Code harness as a library),
  with a per-tool permission callback: reads and edits inside the working tree run, anything else
  (shell commands, web access, …) is asked for in the session chat. Budget: `max_budget_usd` is
  what is left of the session's USD budget. Resumes its own session id after a question.
- `copilot`: the GitHub Copilot SDK (`github-copilot-sdk`): a Copilot CLI session in the working
  tree, every permission request asked in the chat. **Authentication and deployment are not
  validated**: it is behind TWINLAB2_ENABLE_COPILOT and has never run against Copilot.

Neither package is needed by the portal or the tests (`pip install -r requirements-providers.txt`
on the machine that runs the worker). Tests set TWINLAB2_CLAUDE_AGENT / TWINLAB2_COPILOT_AGENT to
fakes, so no real agent ever runs and nothing is spent.
"""

from __future__ import annotations

import asyncio

from twinlab2.providers.runner import AgentResult


async def claude(prompt, ctx):
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                                  PermissionResultDeny, ResultMessage, TextBlock)

    async def can_use_tool(tool, tool_input, _context):
        if ctx.allows(tool, tool_input):
            return PermissionResultAllow()
        if await ctx.ask_tool(tool, tool_input):
            return PermissionResultAllow()
        return PermissionResultDeny(message="The person denied this tool in the TwinLab2 session chat.")

    options = ClaudeAgentOptions(
        model=ctx.model or None, cwd=ctx.cwd, resume=ctx.resume, permission_mode="default",
        can_use_tool=can_use_tool, max_budget_usd=ctx.remaining,
        setting_sources=[],  # no user or project settings from the worker machine
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": "You are building a demo application in a TwinLab2 build session."})
    texts, result = [], AgentResult()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async def watch():
            while True:
                await asyncio.sleep(0.5)
                ctx.check_controls()
                if ctx.interrupted.is_set():
                    await client.interrupt()
                    return

        watcher = asyncio.create_task(watch())
        try:
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            texts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result = AgentResult(session_id=message.session_id, cost=message.total_cost_usd,
                                         is_error=bool(message.is_error) and not ctx.interrupted.is_set(),
                                         interrupted=ctx.interrupted.is_set(),
                                         detail={"subtype": message.subtype, "turns": message.num_turns})
        finally:
            watcher.cancel()
    result.text = (texts[-1] if texts else "") if not result.is_error else "; ".join(texts[-1:]) or "The agent failed."
    if texts:
        ctx.emit("message", texts[-1][:4000])
    return result


async def copilot(prompt, ctx):
    from copilot import CopilotClient
    from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

    async def on_permission(request, _invocation):
        kind = getattr(request, "kind", "tool")
        details = {k: v for k, v in vars(request).items() if isinstance(v, (str, int, bool, list))} if hasattr(request, "__dict__") else {}
        if kind == "read" or await ctx.ask_tool(f"copilot:{kind}", details):
            return PermissionDecisionApproveOnce()
        return PermissionDecisionReject(feedback="Denied in the TwinLab2 session chat.")

    client = CopilotClient(working_directory=ctx.cwd, github_token=ctx.secrets.get("copilot_token") or None)
    await client.start()
    try:
        if ctx.resume:
            session = await client.resume_session(ctx.resume, on_permission_request=on_permission,
                                                  model=ctx.model or None)
        else:
            session = await client.create_session(on_permission_request=on_permission, model=ctx.model or None,
                                                  session_id=ctx.session.spec["tag"], working_directory=ctx.cwd)
        final = await session.send_and_wait(prompt, timeout=3600.0)
        text = getattr(getattr(final, "data", None), "content", "") or ""
        if text:
            ctx.emit("message", text[:4000])
        await session.disconnect()
        return AgentResult(session_id=ctx.session.spec["tag"], text=text, cost=None)
    finally:
        await client.stop()
