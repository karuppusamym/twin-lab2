"""Local HTTP contract checks; never call a provider or consume API credits."""
from dataclasses import asdict
import hashlib
import io
import json
import unittest
from zipfile import ZipFile

import httpx

from experiments.provider_probe import ClaudeMessages, Conversation, ProviderFailure


def pack():
    out = io.BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("BUILD_TASK.md", "Build synthetic Orders")
        z.writestr("evidence.json", '{"format":2}')
        z.writestr("attachments/private.txt", "DO_NOT_SEND_RAW_ATTACHMENT")
    raw = out.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


class ProviderProbeTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.mode = "ok"

        def handle(request):
            self.requests.append(request)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            if self.mode == "rate":
                return httpx.Response(429, json={"error": "limited"})
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "Is zero quantity allowed?"}],
                "stop_reason": "max_tokens" if self.mode == "truncated" else "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 10},
            })

        self.p = ClaudeMessages("contract-test-key", "configured-test-model", transport=httpx.MockTransport(handle))
        self.addCleanup(self.p.close)

    def test_package_and_question_answer_use_messages_api(self):
        session = self.p.start(*pack())
        self.assertEqual(self.requests[0].url.path, "/v1/messages")
        self.assertEqual(self.requests[0].headers["anthropic-version"], "2023-06-01")
        self.assertNotIn(b"DO_NOT_SEND_RAW_ATTACHMENT", self.requests[0].content)
        self.p.reply(session, "Reject zero and save no order")
        sent = json.loads(self.requests[-1].content)
        self.assertEqual([m["role"] for m in sent["messages"]], ["user", "assistant", "user"])
        self.assertEqual(len(session.usage), 2)
        self.assertEqual(session.state, "awaiting_user")

    def test_two_sessions_share_pack_not_conversation(self):
        first, second = self.p.start(*pack()), self.p.start(*pack())
        self.p.reply(first, "FIRST_SESSION_ONLY")
        self.p.reply(second, "SECOND_SESSION_ONLY")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.pack_sha256, second.pack_sha256)
        self.assertNotIn(b"FIRST_SESSION_ONLY", self.requests[-1].content)

    def test_serialized_conversation_can_resume(self):
        original = self.p.start(*pack())
        restored = Conversation(**json.loads(json.dumps(asdict(original))))
        self.p.reply(restored, "Continue after worker restart")
        self.assertEqual(restored.id, original.id)
        self.assertEqual(len(restored.messages), 4)
        self.assertEqual(len(original.messages), 2)

    def test_checksum_rejection_happens_before_network(self):
        with self.assertRaises(ValueError):
            self.p.start(pack()[0], "wrong")
        self.assertEqual(self.requests, [])

    def test_timeout_does_not_retry_or_append_unconfirmed_turn(self):
        session = self.p.start(*pack())
        self.mode = "timeout"
        with self.assertRaises(ProviderFailure) as caught:
            self.p.reply(session, "Answer")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(session.messages), 2)
        self.assertEqual(session.state, "delivery_uncertain")

    def test_rate_limit_is_visible(self):
        self.mode = "rate"
        with self.assertRaises(ProviderFailure) as caught:
            self.p.start(*pack())
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.requests), 1)

    def test_truncation_does_not_mean_finished(self):
        self.mode = "truncated"
        self.assertEqual(self.p.start(*pack()).state, "truncated")

    def test_provider_switch_requires_new_session(self):
        session = self.p.start(*pack())
        session.provider = "devin"
        with self.assertRaises(ValueError):
            self.p.reply(session, "Continue")


if __name__ == "__main__":
    unittest.main()
