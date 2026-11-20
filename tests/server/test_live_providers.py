"""Live provider checks. **Skipped by default**: they call the real APIs and spend money.

    TWINLAB2_LIVE_CLAUDE=1 ANTHROPIC_API_KEY=… [CLAUDE_MODEL=…] pytest tests/test_live_providers.py
    TWINLAB2_LIVE_CLAUDE_AGENT=1 ANTHROPIC_API_KEY=… (needs claude-agent-sdk)  … the agent smoke test

Only synthetic data is sent (a made-up order demo). Nothing here has been run yet: see VERIFICATION.md.
"""

import asyncio
import hashlib
import io
import os
import tempfile
import unittest
from zipfile import ZipFile

LIVE = os.environ.get("TWINLAB2_LIVE_CLAUDE") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))
LIVE_AGENT = os.environ.get("TWINLAB2_LIVE_CLAUDE_AGENT") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def synthetic_pack():
    out = io.BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("BUILD_TASK.md", "Build a synthetic order demo. It is not said whether a zero quantity is allowed.")
        z.writestr("evidence.json", '{"format": 2, "scenarios": [], "synthetic": true}')
    raw = out.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(LIVE, "live Claude test: set TWINLAB2_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY (spends API credits)")
class LiveClarifyTest(unittest.TestCase):
    def test_review_asks_and_an_answer_continues_the_conversation(self):
        from providers.clarify import Clarifier, questions_of

        clarifier = Clarifier(os.environ["ANTHROPIC_API_KEY"], os.environ.get("CLAUDE_MODEL") or None)
        try:
            text = clarifier.start(*synthetic_pack())
            questions, ready = questions_of(text)
            self.assertTrue(questions or ready, text)
            clarifier.send("A zero quantity is refused and no order is saved. Nothing else is open.")
            self.assertEqual([m["role"] for m in clarifier.messages], ["user", "assistant", "user", "assistant"])
        finally:
            clarifier.close()


@unittest.skipUnless(LIVE_AGENT, "live agent test: set TWINLAB2_LIVE_CLAUDE_AGENT=1 and ANTHROPIC_API_KEY (spends money)")
class LiveAgentTest(unittest.TestCase):
    def test_one_turn_edits_the_working_tree(self):
        from providers import agents

        class Ctx:
            resume, restarted, remaining, budget = None, False, 0.5, 0.5
            model = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
            secrets = {}

            def __init__(self, cwd):
                import threading
                self.cwd, self.interrupted = cwd, threading.Event()

            def allows(self, tool, tool_input):
                return tool in ("Read", "Write", "Edit", "Glob", "Grep")

            async def ask_tool(self, tool, tool_input):
                return False

            def check_controls(self):
                pass

            def emit(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as work:
            result = asyncio.run(agents.claude("Create hello.txt containing the word hello. Nothing else.", Ctx(work)))
            self.assertFalse(result.is_error, result.text)
            self.assertTrue(os.path.exists(os.path.join(work, "hello.txt")))


if __name__ == "__main__":
    unittest.main()
