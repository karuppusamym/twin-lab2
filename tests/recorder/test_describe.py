"""`describe` (what `record` and `show` print before a send): several targets on one host get one
line each with their own steps, by the portal's longest-prefix rule (gap 12 of the rebuild proof:
the shared host was listed once, as "Billing 67 steps")."""

import json
from pathlib import Path
import tempfile
import unittest

from twinlab2_recorder import main as agent_main
from twinlab2_recorder import settings as st

HOST = "http://127.0.0.1:18761"


def box(screens):
    flows = [{"n": i, "action": "click", "screen": HOST + s} for i, s in enumerate(screens, 1)]
    return {"session": {"name": "C", "id": "20261121T055140Z-64f844a5",
                        "targets": [{"origin": HOST, "level": "full", "app_id": 1},
                                    {"origin": HOST, "path": "/billing", "level": "full", "app_id": 2}]},
            "apps": [{"key": "web:" + HOST, "surface": "web", "level": "full", "display": "Billing", "origin": HOST},
                     {"key": "mainframe:h:1", "surface": "mainframe", "level": "full", "display": "Account Inquiry"}],
            "flows": {"web:" + HOST: flows, "mainframe:h:1": [{"n": 1}]},
            "timeline": [{"app": "web:" + HOST, "steps": len(flows)}]}  # fmt: skip


TARGETS = [{"origin": HOST, "level": "full", "name": "Orders", "app_id": 1},
           {"origin": HOST + "/billing", "level": "full", "name": "Billing", "app_id": 2},
           {"key": "mainframe:h:1", "level": "full", "name": "Account Inquiry", "app_id": 3}]  # fmt: skip


class Describe(unittest.TestCase):
    def test_one_line_per_target_on_a_shared_host(self):
        b = box(["/orders/search"] * 3 + ["/orders/new/items"] * 2 + ["/billing/lookup", "/billing/{id}", "/billingx"])
        text = agent_main.describe(b, targets=TARGETS)
        self.assertIn(f"2 applications on one host  [web:{HOST}]  8 steps", text)
        self.assertIn("Orders  (the rest of the host)  6 steps", text)
        self.assertIn("Billing  /billing  2 steps", text)
        self.assertIn("Account Inquiry  [mainframe:h:1]  1 steps", text)
        self.assertNotIn("Billing  [web:", text)

    def test_without_names_the_box_targets_are_used(self):
        text = agent_main.describe(box(["/billing/lookup", "/orders"]))
        self.assertIn("app 1  (the rest of the host)  1 steps", text)
        self.assertIn("app 2  /billing  1 steps", text)

    def test_targets_without_paths_are_listed_not_guessed(self):
        bare = [dict(TARGETS[0]), dict(TARGETS[1], origin=HOST)]
        text = agent_main.describe(box(["/orders", "/billing/lookup"]), targets=bare)
        self.assertIn(f"2 applications on one host  [web:{HOST}]  2 steps", text)
        self.assertIn("Orders  (no path: the portal tells its steps apart", text)
        self.assertIn("Billing  (no path: the portal tells its steps apart", text)

    def test_one_target_per_host_is_one_line_as_before(self):
        b = box(["/orders"])
        b["session"]["targets"] = b["session"]["targets"][:1]
        text = agent_main.describe(b, targets=TARGETS[:1])
        self.assertIn(f"full   Billing  [web:{HOST}]  1 steps", text)

    def test_an_offered_target_keeps_its_path(self):
        # the portal sends a web target known by path with its path (contract 1.2); the recorder
        # kept only the origin, so a shared host was one application to it
        from twinlab2_recorder.client import clean_targets

        got = clean_targets([{"origin": HOST, "path": "/billing/", "name": "Billing", "app_id": 2, "level": "full"},
                             {"origin": HOST, "path": "/a/../b", "app_id": 3},
                             {"origin": HOST, "path": "/x?y=1", "app_id": 4},
                             {"exe": "x.exe", "path": "/c", "app_id": 5}])  # fmt: skip
        self.assertEqual([t.get("path") for t in got], ["/billing", None, None, None])

    def test_the_session_targets_come_from_session_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = st.Settings(Path(tmp))
            folder = cfg.recordings / "s1"
            folder.mkdir(parents=True)
            (folder / "session.json").write_text(json.dumps({"targets": TARGETS}), encoding="utf-8")
            got = agent_main.session_targets(cfg, "s1")
        self.assertEqual([(t.get("origin"), t.get("path"), t["name"]) for t in got[:2]],
                         [(HOST, None, "Orders"), (HOST, "/billing", "Billing")])


if __name__ == "__main__":
    unittest.main()
