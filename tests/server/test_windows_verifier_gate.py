"""A Windows verifier's passing desktop Verify stands in for the missing `*windows*` CI check
(and only then), fail-closed. Pure: no window, no GitHub."""

import unittest

from twinlab2 import verification as v

SHA = "a" * 40
APP = {"target_platform": "windows-desktop", "windows_check_pattern": "", "required_checks": "", "git_url": ""}
OK = {"problems": [], "scenarios": [{"id": "s1", "result": "pass"}],
      "dimensions": {"behavioural": {"status": "pass"},
                     "structural": {"surfaces": {"desktop": {"status": "pass"}}}}}


def gate(checks=()):
    return v.evaluate(APP, list(checks))


def take(g=None, **over):
    args = dict(platform="win32", opted_in=True, release_commit=SHA, built_commit=SHA,
                desktop="C:/builds/Calculator.exe", twin=OK)
    args.update(over)
    return v.accept_verifier(g or gate(), **args)


class WindowsVerifierGate(unittest.TestCase):
    def test_without_it_the_gate_is_missing(self):
        g = gate()
        self.assertFalse(g["ok"])
        self.assertEqual(g["windows"]["state"], "missing")

    def test_a_passing_windows_verifier_satisfies_it(self):
        g = take()
        self.assertTrue(g["ok"], g["reasons"])
        self.assertEqual((g["windows"]["state"], g["windows"]["satisfied_by"]), ("verifier", "verifier"))

    def test_a_green_ci_check_still_counts_and_says_so(self):
        g = gate([{"name": "windows-latest", "conclusion": v.GREEN}])
        self.assertTrue(g["ok"])
        self.assertEqual(g["windows"]["satisfied_by"], "ci")
        self.assertIs(take(g), g)  # nothing to replace

    def test_a_failing_windows_check_is_not_overridden(self):
        g = gate([{"name": "windows-latest", "conclusion": "failure"}])
        self.assertIs(take(g), g)
        self.assertFalse(g["ok"])

    def test_fail_closed(self):
        bad = {
            "not windows": dict(platform="linux"),
            "not registered": dict(opted_in=False),
            "another commit": dict(built_commit="b" * 40),
            "no commit reported": dict(built_commit=None),
            "desktop not replayed": dict(desktop=None),
            "no result": dict(twin=None),
            "problems": dict(twin={**OK, "problems": ["not run"]}),
            "a scenario failed": dict(twin={**OK, "scenarios": [{"id": "s1", "result": "fail"}]}),
            "no scenarios": dict(twin={**OK, "scenarios": []}),
            "structure failed": dict(twin={**OK, "dimensions": {**OK["dimensions"], "structural": {
                "surfaces": {"desktop": {"status": "fail"}}}}}),
            "behaviour failed": dict(twin={**OK, "dimensions": {**OK["dimensions"],
                                                                 "behavioural": {"status": "fail"}}}),
            "desktop structure absent": dict(twin={**OK, "dimensions": {"behavioural": {"status": "pass"}}}),
        }
        for name, over in bad.items():
            with self.subTest(name):
                g = gate()
                self.assertIs(take(g, **over), g)
                self.assertFalse(g["ok"])

    def test_other_reasons_still_refuse(self):
        g = gate()
        g["reasons"].append("Required check “build” is failure.")
        taken = take(g)
        self.assertFalse(taken["ok"])
        self.assertEqual(taken["reasons"], ["Required check “build” is failure."])
        self.assertEqual(taken["windows"]["state"], "verifier")

    def test_a_web_release_is_untouched(self):
        g = v.evaluate({**APP, "target_platform": "web"}, [])
        self.assertIs(take(g), g)


if __name__ == "__main__":
    unittest.main()
