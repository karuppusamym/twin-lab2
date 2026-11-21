"""Route templates with application path prefixes (contract 1.2): a web step under a target's
prefix keeps it whole; after it, route words and plain lower-case words are kept (gap 4 of the
rebuild proof: `/orders/new/items` was `/orders/new/{id}`), and no record segment, query,
fragment, person's slug or typed value is ever kept."""

import json
import unittest

from twinlab2_recorder.workstation import flow, levels, timeline

PREFIXES = ["/finance", "/finance/billing", "/finance/payroll"]


class RouteTemplateTest(unittest.TestCase):
    def test_the_longest_target_prefix_is_kept_whole(self):
        cases = {
            "https://h/finance/billing/invoices/42": "/finance/billing/invoices/{id}",
            "https://h/finance/payroll/runs/7": "/finance/payroll/runs/{id}",
            "https://h/finance/payroll/runs/new": "/finance/payroll/runs/new",
            "https://h/finance/billing": "/finance/billing",
            "https://h/finance/billing/": "/finance/billing",
            "https://h/finance/ledger/2026-11": "/finance/ledger/{id}",
            "https://h/finance": "/finance",
            "https://h/": "/",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url, PREFIXES), expected, url)

    def test_a_prefix_matches_whole_segments_only(self):
        self.assertEqual(flow.route_template("https://h/finance/billing-archive/2025", PREFIXES), "/finance/{id}/{id}")
        # a segment only a prefix keeps (not a plain word): kept under the prefix, never elsewhere
        desk = ["/apps/billing-desk"]
        self.assertEqual(flow.route_template("https://h/apps/billing-desk/x1", desk), "/apps/billing-desk/{id}")
        self.assertEqual(flow.route_template("https://h/apps/billing-desks/x1", desk), "/apps/{id}/{id}")
        self.assertEqual(flow.route_template("https://h/Apps/billing-desk/x1", desk), "/Apps/{id}/{id}")

    def test_no_query_fragment_or_credentials_are_kept(self):
        url = "https://user:pw@h/finance/payroll/runs/7?employee=jane.doe&salary=5000#notes"
        self.assertEqual(flow.route_template(url, PREFIXES), "/finance/payroll/runs/{id}")
        screen = flow.web_step({"url": url, "selector": "input#payrollHours"}, PREFIXES)["screen"]
        self.assertEqual(screen, "https://h/finance/payroll/runs/{id}")
        for leak in ("jane", "5000", "notes", "user", "pw@"):
            self.assertNotIn(leak, screen)

    def test_a_record_in_the_place_of_a_prefix_is_still_an_id(self):
        # only what a target names is kept: /customers/jane-doe is a record, not a prefix
        self.assertEqual(flow.route_template("https://h/customers/jane-doe/edit", PREFIXES), "/customers/{id}/edit")
        self.assertEqual(flow.route_template("https://h/customers/jane-doe/edit", ["/customers"]),
                         "/customers/{id}/edit")

    def test_without_prefixes_plain_words_and_route_words_are_kept(self):
        cases = {
            "https://h/finance/billing/invoices/42": "/finance/billing/invoices/{id}",
            "https://h/billing/invoices/INV-1001": "/billing/invoices/{id}",
            "https://h/billing-admin/rates/edit": "/billing-admin/rates/edit",
            "https://h/123/edit": "/{id}/edit",
            "https://h/customers/jane-doe": "/customers/{id}",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url), expected, url)
            self.assertEqual(flow.route_template(url, ()), expected, url)

    def test_a_prefix_that_is_not_a_plain_path_is_ignored(self):
        for bad in ("finance", "/finance?x=1", "/finance#a", "/a/../b", "/a//b", "/a b", "", None, "/" + "x" * 130):
            self.assertEqual(flow.prefix_segments(bad), (), bad)
            self.assertEqual(flow.route_template("https://h/finance/billing/1", [bad]), "/finance/billing/{id}", bad)


class RouteWordTest(unittest.TestCase):
    """Gap 4: a plain lower-case word is the route's own; ids, records and people stay `{id}`."""

    def test_the_proofs_routes_keep_their_words(self):
        cases = {
            "https://h/orders/new/items": "/orders/new/items",
            "https://h/orders/new/customer": "/orders/new/customer",
            "https://h/orders/saved": "/orders/saved",
            "https://h/billing/lookup": "/billing/lookup",
            "https://h/billing/account": "/billing/account",
            "https://h/billing/invoice": "/billing/invoice",
            "https://h/billing/start": "/billing/start",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url), expected, url)

    def test_records_and_values_stay_ids(self):
        cases = {
            "https://h/orders/10442": "/orders/{id}",
            "https://h/orders/A-1001/items": "/orders/{id}/items",
            "https://h/orders/3f2c9a7e-1b2d-4c5e-9f00-aa11bb22cc33": "/orders/{id}",
            "https://h/orders/jane.doe@example.com": "/orders/{id}",
            "https://h/orders/jane-doe": "/orders/{id}",
            "https://h/orders/Priya": "/orders/{id}",
            "https://h/orders/x": "/orders/{id}",
            "https://h/orders/" + "a" * 25: "/orders/{id}",
            "https://h/orders/abc123": "/orders/{id}",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url), expected, url)

    def test_a_word_after_people_or_accounts_is_an_id(self):
        # a lower-case username or name used as a slug
        cases = {
            "https://h/users/jdoe": "/users/{id}",
            "https://h/users/jdoe/edit": "/users/{id}/edit",
            "https://h/app/u/priya": "/app/{id}/{id}",
            "https://h/crm/customers/okonkwo/orders": "/crm/customers/{id}/orders",
            "https://h/billing/accounts/smith": "/billing/accounts/{id}",
        }
        for url, expected in cases.items():
            self.assertEqual(flow.route_template(url), expected, url)

    def test_a_typed_value_in_the_path_is_its_shape_first(self):
        from twinlab2_recorder.web.recorder import _Secrets

        secrets = _Secrets()
        secrets.add("okonkwo", "https://h", "t1")
        url = secrets.redact_url("https://h/search/okonkwo")
        self.assertNotIn("okonkwo", url)
        self.assertNotIn("okonkwo", flow.route_template(url))


class IdentWithDigitsTest(unittest.TestCase):
    """Identifiers as developers name them keep their short digit run (the proof's "Control ids
    with digits"); all-digit ids, long digit runs, counters and record shapes do not."""

    def test_both_ways(self):
        for good in ("addressLine2", "button1", "btn2FA", "txtLine2", "tab12", "btnSave", "searchBy"):
            self.assertTrue(flow.ident(good), good)
        for bad in ("2", "65535", "20", "12345", "cust10442", "a1b2c3", "mat-input-0", "row_12",
                    "INV-1001", "x1", "btn123", "1abc", "item.3", "X-9999", "a@b", "a" * 41, ""):
            self.assertFalse(flow.ident(bad), bad)

    def test_a_label_keeps_such_an_id(self):
        self.assertEqual(flow.web_step({"selector": "input#addressLine2", "url": "https://h/a"})["label"], "addressLine2")
        self.assertIsNone(flow.web_step({"selector": "input#cust10442", "url": "https://h/a"})["label"])
        step = {"target": {"selector": "uia:AutomationId=btn2FA;ControlType=Button"}}
        self.assertEqual(flow.desktop_step(step)["label"], "btn2FA")
        step = {"target": {"selector": "uia:AutomationId=2;ControlType=Button", "props": {"Name": "OK"}}}
        self.assertEqual(flow.desktop_step(step)["label"], "OK")  # Win32's numeric id: the caption

    def test_the_structure_keeps_it_and_the_portal_agrees(self):
        from twinlab2_contract import structure as st

        state = st.clean_state({"controls": [{"kind": "input", "id": "addressLine2"},
                                             {"kind": "input", "id": "cust10442"}], "events": []})
        self.assertEqual(state["controls"][0]["id"], "addressLine2")
        self.assertNotIn("id", state["controls"][1])
        self.assertEqual(state["withheld"], 1)


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
        self.assertEqual(box["contract_version"], "1.7.0")
        self.assertEqual(box["flows"]["web:https://h"][0]["screen"], "https://h/finance/billing/{id}")
        self.assertEqual(box["flows"]["web:https://k"][0]["screen"], "https://k/finance/billing/{id}")
        self.assertEqual([t.get("path") for t in box["session"]["targets"]], ["/finance/billing", "/x"])


class DeniedPathTest(unittest.TestCase):
    """A not-replicated application known by path on a host shared with targets (deny key
    `web:<origin>/<path>`): its steps leave the PC as presence only."""

    def rows(self, *urls):
        return [{"app": "web:https://h", "surface": "web", "level": "full", "global_seq": n, "ts": f"t{n}",
                 "_step": {"surface": "web", "url": u, "action": "click", "selector": f"button#step{chr(96 + n)}"}}
                for n, u in enumerate(urls, 1)]

    def test_steps_under_a_denied_prefix_are_presence(self):
        meta = {"id": "20261121T030000Z-0a0b0c0d",
                "targets": [{"origin": "https://h", "path": "/finance/billing"}, {"origin": "https://h", "path": "/finance"}],
                "deny_keys": ["web:https://h/finance/payroll", "web:https://other/finance/billing"],
                "apps": {"web:https://h": {"key": "web:https://h", "surface": "web", "level": "full"}}}
        box = timeline.outbox(meta, self.rows("https://h/finance/billing/1", "https://h/finance/payroll/7",
                                              "https://h/finance/payroll", "https://h/finance/ledger"))
        self.assertEqual([s["label"] for s in box["flows"]["web:https://h"]], ["stepa", "stepd"])
        self.assertEqual(box["timeline"], [{"app": "web:https://h", "steps": 1}, {"app": "presence", "steps": 2},
                                           {"app": "web:https://h", "steps": 1}])
        self.assertNotIn("payroll", json.dumps(box["flows"]))

    def test_a_longer_target_prefix_under_a_denied_one_is_recorded(self):
        meta = {"id": "20261121T030000Z-0a0b0c0d", "targets": [{"origin": "https://h", "path": "/finance/billing"}],
                "deny_keys": ["web:https://h/finance"]}
        box = timeline.outbox(meta, self.rows("https://h/finance/billing/1", "https://h/finance/payroll/7"))
        self.assertEqual([s["label"] for s in box["flows"]["web:https://h"]], ["stepa"])

    def test_a_whole_origin_deny_key_is_not_a_prefix(self):
        self.assertEqual(levels.denied_prefixes(["web:https://h", "desktop:Acme/X", "web:https://h/a/b/"], "https://h"),
                         ["/a/b"])
        self.assertTrue(timeline.under_denied_path("https://h/a/b/c", [], ["/a/b"]))
        self.assertFalse(timeline.under_denied_path("https://h/a/bc", [], ["/a/b"]))
        self.assertFalse(timeline.under_denied_path("https://h/a/b", ["/a/b"], ["/a/b"]))  # a target allows it

    def test_a_target_known_by_path_starts_at_its_page(self):
        targets = levels.Targets.from_list([{"origin": "https://h", "path": "/finance/billing"}])
        self.assertEqual(targets.web_start(), "https://h/finance/billing")
        self.assertEqual(levels.Targets.from_list([{"origin": "https://h"}]).web_start(), "https://h/")
        self.assertEqual(levels.Targets.from_list([{"origin": "https://h", "path": "/a", "start": "https://h/x"}])
                         .web_start(), "https://h/x")


if __name__ == "__main__":
    unittest.main()
