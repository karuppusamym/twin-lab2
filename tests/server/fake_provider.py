"""An in-memory build provider for the session tests: no network, no money.

`FakeProvider.world` is shared by every adapter instance (the worker makes one per job), so a
test drives its sessions directly: `ask`, `ask_tool`, `finish`, `plan` (what the next start or
reply does: "ok", "uncertain" (happened, no answer), "lost" (no answer, nothing happened),
"refuse").
"""

from __future__ import annotations

import itertools
import threading

from twinlab2.providers import base
from twinlab2 import providers


class World:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.start_plan, self.reply_plan = [], []
        self.starts = 0
        self.replies = []
        self.ids = itertools.count(1)

    def ask(self, sid, text):
        with self.lock:
            s = self.sessions[sid]
            s["events"].append(base.Event(f"q-{len(s['events'])}", "clarification", text))
            s["state"] = base.WAITING

    def ask_tool(self, sid, tool, tool_input):
        with self.lock:
            s = self.sessions[sid]
            s["events"].append(base.Event(f"t-{len(s['events'])}", "tool_approval", f"Run {tool}?",
                                          {"tool": tool, "input": tool_input, "request_id": f"r{len(s['events'])}"}))
            s["state"] = base.WAITING

    def finish(self, sid, head_sha, cost=1.5):
        with self.lock:
            self.sessions[sid].update(state=base.READY, head=head_sha, cost=cost)


class FakeProvider(base.Provider):
    name = "fake"
    label = "Fake provider"
    capabilities = frozenset({base.START, base.READ_EVENTS, base.REPLY, base.INTERRUPT, base.CANCEL,
                              base.COLLECT_RESULTS, base.RECONCILE_START, base.TOOL_APPROVAL, base.ACKNOWLEDGE})
    budget_unit = cost_unit = "USD"
    world = World()

    @classmethod
    def from_settings(cls, settings, github_values, ctx):
        return cls()

    @classmethod
    def check_budget(cls, value):
        budget = float(value)
        if not 0 < budget <= 100:
            raise ValueError("Give a budget between 0 and 100 USD.")
        return budget

    def _plan(self, plan):
        with self.world.lock:
            return plan.pop(0) if plan else "ok"

    def start(self, spec, state):
        mode = self._plan(self.world.start_plan)
        with self.world.lock:
            self.world.starts += 1
            if mode in ("lost", "refuse"):
                raise base.ProviderError("no answer" if mode == "lost" else "refused", uncertain=mode == "lost",
                                         status=0 if mode == "lost" else 400)
            sid = f"fake-{next(self.world.ids)}"
            self.world.sessions[sid] = {"id": sid, "tag": spec.tag, "branch": spec.branch, "workspace": str(spec.workspace),
                                        "base": spec.base_sha, "prompt": spec.prompt, "state": base.RUNNING,
                                        "events": [], "received": [], "cancelled": False, "interrupted": False,
                                        "head": None, "cost": 0.0, "budget": spec.budget}
        if mode == "uncertain":
            raise base.ProviderError("no answer", uncertain=True)
        return base.Started(session_id=sid, url=f"https://fake.invalid/{sid}", status="running")

    def find_started(self, spec, state):
        with self.world.lock:
            return [base.Started(session_id=s["id"]) for s in self.world.sessions.values() if s["tag"] == spec.tag]

    def read(self, build):
        with self.world.lock:
            s = self.world.sessions[build["provider_session_id"]]
            cursor = int(build["event_cursor"] or 0)
            events = list(s["events"][cursor:])
            acks = [base.Event(f"ack-{m}", "ack", data={"message_id": m}) for m in s["received"]]
            s["received"] = []
            return base.Snapshot(state=s["state"], status=s["state"], events=events + acks,
                                 cursor=str(len(s["events"])), cost=s["cost"], branch=s["branch"],
                                 head_sha=s["head"], questions=[e.text for e in s["events"][-1:]]
                                 if s["state"] == base.WAITING else [])

    def reply(self, build, message):
        mode = self._plan(self.world.reply_plan)
        with self.world.lock:
            self.world.replies.append((build["provider_session_id"], message["kind"], message["body"]))
            if mode == "lost":
                raise base.ProviderError("no answer", uncertain=True)
            s = self.world.sessions[build["provider_session_id"]]
            if mode != "silent":  # "silent": accepted, never acknowledged
                s["received"].append(message["id"])
            s["state"] = base.RUNNING

    def interrupt(self, build):
        with self.world.lock:
            self.world.sessions[build["provider_session_id"]]["interrupted"] = True

    def cancel(self, build, spec=None):
        with self.world.lock:
            ids = [build["provider_session_id"]] if build["provider_session_id"] else \
                [s["id"] for s in self.world.sessions.values() if s["tag"] == build["idempotency_tag"]]
            for sid in ids:
                self.world.sessions[sid]["cancelled"] = True
            return ids


providers.register(FakeProvider)


def reset():
    FakeProvider.world = World()
    return FakeProvider.world
