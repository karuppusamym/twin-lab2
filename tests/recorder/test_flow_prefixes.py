"""Route templates with application path prefixes (contract 1.2): a web step under a target's
prefix keeps it whole; everything else follows the 1.1 rules, so no record segment, query,
fragment or typed value is ever kept."""

import unittest

from twinlab2_recorder.workstation import flow, levels, timeline

PREFIXES = ["/finance", "/finance/billing", "/finance/payroll"]


class RouteTemplateTest(unittest.TestCase):
    def test_the_longest_target_prefix_is_kept_whole(self):
        cases = {
            "https://h/finance/billing/invoices/42": "/finance/billing/{id}/{id}",
            "https://h/finance/payroll/runs/7": "/finance/payroll/{id}/{id}",
            "https://h/finance/payroll/runs/new": "/finance/payroll/{id}/new",
            "https://h/finance/billing": "/finance/billing",
            "https://h/finance/billing/": "/finance/billing",
            "https://h/finance/ledger/2026-11": "/finance/{id}/{id}",
            "https://h/finance": "/finance",
            "https://h/": "/",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url, PREFIXES), expected, url)

    def test_a_prefix_matches_whole_segments_only(self):
        self.assertEqual(flow.route_template("https://h/finance/billing-archive/2025", PREFIXES), "/finance/{id}/{id}")
        self.assertEqual(flow.route_template("https://h/financed/billing/x", PREFIXES), "/financed/{id}/{id}")
        self.assertEqual(flow.route_template("https://h/Finance/billing/x", PREFIXES), "/Finance/{id}/{id}")

    def test_no_query_fragment_or_credentials_are_kept(self):
        url = "https://user:pw@h/finance/payroll/runs/7?employee=jane.doe&salary=5000#notes"
        self.assertEqual(flow.route_template(url, PREFIXES), "/finance/payroll/{id}/{id}")
        screen = flow.web_step({"url": url, "selector": "input#payrollHours"}, PREFIXES)["screen"]
        self.assertEqual(screen, "https://h/finance/payroll/{id}/{id}")
        for leak in ("jane", "5000", "notes", "user", "pw@"):
            self.assertNotIn(leak, screen)

    def test_a_record_in_the_place_of_a_prefix_is_still_an_id(self):
        # only what a target names is kept: /customers/jane-doe is a record, not a prefix
        self.assertEqual(flow.route_template("https://h/customers/jane-doe/edit", PREFIXES), "/customers/{id}/edit")
        self.assertEqual(flow.route_template("https://h/customers/jane-doe/edit", ["/customers"]),
                         "/customers/{id}/edit")

    def test_without_prefixes_the_rules_are_those_of_1_1(self):
        cases = {
            "https://h/finance/billing/invoices/42": "/finance/{id}/{id}/{id}",
            "https://h/billing/invoices/INV-1001": "/billing/{id}/{id}",
            "https://h/billing-admin/rates/edit": "/billing-admin/{id}/edit",
            "https://h/123/edit": "/{id}/edit",
            "https://h/customers/jane-doe": "/customers/{id}",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url), expected, url)
            self.assertEqual(flow.route_template(url, ()), expected, url)

    def test_a_prefix_that_is_not_a_plain_path_is_ignored(self):
        for bad in ("finance", "/finance?x=1", "/finance#a", "/a/../b", "/a//b", "/a b", "", None, "/" + "x" * 130):
            self.assertEqual(flow.prefix_segments(bad), (), bad)
            self.assertEqual(flow.route_template("https://h/finance/billing/1", [bad]), "/finance/{id}/{id}", bad)


class TargetPathTest(unittest.TestCase):
    def test_a_target_keeps_its_path_prefix(self):
        t = levels.Target.from_dict({"origin": "https://h", "path": "/finance/billing/"})
        self.assertEqual((t.origin, t.path), ("https://h", "/finance/billing"))
        # an address given with its path (record --origin https://h/finance/payroll)
        t = levels.Target.from_dict({"origin": "http://127.0.0.1:8123/finance/payroll"})
        self.assertEqual((t.origin, t.path), ("http://127.0.0.1:8123", "/finance/payroll"))
        t = levels.Target.from_dict({"key": "web:https://h/finance"})
        self.assertEqual((t.origin, t.path), ("https://h", "/finance"))
        self.assertIsNone(levels.Target.from_dict({"origin": "https://h"}).path)
        self.assertIsNone(levels.Target.from_dict({"origin": "https://h", "path": "/a/../b"}).path)
        self.assertIsNone(levels.Target.from_dict({"exe": "x.exe", "path": "/a"}).path)

    def test_the_session_lists_its_prefixes_per_origin(self):
        targets = levels.Targets.from_list([
            {"origin": "https://h", "path": "/finance"}, {"key": "web:https://h/finance/payroll"},
            {"origin": "https://other"}, {"origin": "https://other/x/y"}])
        self.assertEqual(targets.prefixes("https://h"), ["/finance", "/finance/payroll"])
        self.assertEqual(targets.prefixes("https://other"), ["/x/y"])
        self.assertIn({"origin": "https://h", "path": "/finance", "level": "full"}, targets.as_list())

    def test_the_outbox_applies_the_prefixes_of_its_origin_only(self):
        meta = {"id": "20261120T140000Z-0a0b0c0d", "targets": [
            {"origin": "https://h", "path": "/finance/billing"}, {"origin": "https://other", "path": "/x"}]}
        rows = [{"app": "web:https://h", "surface": "web", "level": "full", "global_seq": 1,
                 "_step": {"surface": "web", "url": "https://h/finance/billing/9", "action": "click"}},
                {"app": "web:https://k", "surface": "web", "level": "full", "global_seq": 2,
                 "_step": {"surface": "web", "url": "https://k/finance/billing/9", "action": "click"}}]
        box = timeline.outbox(meta, rows)
        self.assertEqual(box["contract_version"], "1.2.0")
        self.assertEqual(box["flows"]["web:https://h"][0]["screen"], "https://h/finance/billing/{id}")
        self.assertEqual(box["flows"]["web:https://k"][0]["screen"], "https://k/finance/{id}/{id}")
        self.assertEqual([t.get("path") for t in box["session"]["targets"]], ["/finance/billing", "/x"])


if __name__ == "__main__":
    unittest.main()
