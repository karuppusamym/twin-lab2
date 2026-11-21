"""What the build pack tells a blind builder, from the gaps a blind rebuild found (review notes
rebuild-gaps.md, gaps 2, 3, 5, 9, 13, 14, 18, 19 and 20):

- a 3270 screen's title line is compared exactly: the pack says which row holds which title and
  that nothing else goes on it, and the replay note says what *is* compared for 3270 (gap 2);
- when the structure carries protected fields with their positions, each host state gets a
  layout table (row, column, length, attribute, text or shape) (gap 3);
- the redactor leaves prose such as "a password: the sign-in page" alone and still redacts real
  secrets (gap 5);
- the desktop data wording follows the owner's choice, else says in memory unless DATABASE_URL is
  set (gap 9); the run settings say 127.0.0.1 locally and HOST=0.0.0.0 in the container (gap 13);
  a mainframe target serves the version endpoint on its own HTTP port and the OpenShift files
  expose both ports (gap 14);
- the required fields of the recorded forms are listed (gap 18), and "Behaviour not recorded"
  says what the builder does where nothing was recorded (gaps 19, 20).
"""

import json
import tempfile
import unittest

import yaml

from twinlab2 import artifacts, buildpack, demo_target, pack_validator, redact
from twinlab2.portal import VERSION_PATH, create_app
from support import connect, new_database
from test_pack_validator import first_events, mainframe_pack, web_pack, desktop_pack


def task(data, files=None):
    data["target"]["deploy"].setdefault("repository", "https://example.test/demo.git")
    return buildpack.task_markdown(data, "rev", VERSION_PATH, pack_validator.validate(data, files or {}))


def protected(row, col, length, text=None, shape=None, **attrs):
    out = {"kind": "field", "row": row, "col": col, "len": length, "protected": True}
    if text:
        out["text"] = text
    if shape:
        out["shape"] = shape
    out.update(attrs)
    return out


def with_layout(data):
    """The mainframe pack with the protected fields a recorder at contract 1.4 sends."""
    inquiry = data["screens"][0]["states"][0]
    inquiry["controls"] = [*inquiry["controls"], protected(22, 2, 40, shape="XXXXXXX XXXX"),
                           protected(24, 2, 30, "PF3 EXIT ENTER INQUIRE")]
    return data


class TitleLineTest(unittest.TestCase):
    """Gap 2: the builder put `USER <id>` beside the title and failed every inquiry step."""

    def test_each_host_screen_states_its_exact_title_line(self):
        text = task(*mainframe_pack())
        self.assertIn("title (compared exactly): row 1 holds only `ACCOUNT INQUIRY`; put nothing else on that row",
                      text)
        self.assertIn("row 1 holds only `ACCOUNT DETAIL`", text)

    def test_the_title_row_comes_from_the_layout_when_it_is_known(self):
        data, files = mainframe_pack()
        for c in with_layout(data)["screens"][0]["states"][0]["controls"]:
            if c.get("text") == "ACCOUNT INQUIRY":
                c["row"] = 2
        self.assertIn("row 2 holds only `ACCOUNT INQUIRY`", task(data, files))

    def test_without_structure_the_titles_are_still_stated(self):
        data, files = mainframe_pack()
        del data["screens"]
        text = task(data, files)
        self.assertIn("## Host screens", text)
        self.assertIn("row 1 holds only `ACCOUNT INQUIRY`", text)

    def test_the_replay_note_says_what_is_compared_on_a_3270_screen(self):
        text = task(*mainframe_pack())
        self.assertIn(buildpack.REPLAY_NOTE_3270, text)
        for words in ("title line", "row, column, length", "numeric", "non-display"):
            self.assertIn(words, buildpack.REPLAY_NOTE_3270)
        self.assertNotIn(buildpack.REPLAY_NOTE_3270, task(*web_pack()))

    def test_a_screen_without_a_title_is_a_critical_gap(self):
        item = pack_validator.BY_ID["mainframe.title"]
        self.assertEqual((item.severity, item.surfaces), ("critical", ("mainframe",)))
        data, files = mainframe_pack()
        self.assertNotIn("mainframe.title", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})
        for e in first_events(data):
            if e.get("screen") == "ACCOUNT DETAIL":
                e["screen"] = "scr:0a1b2c3d4e5f"
        data["screens"][1]["screen"] = "scr:0a1b2c3d4e5f"
        data["screens"][0]["transitions"][0]["to"] = "scr:0a1b2c3d4e5f"
        gap = next(g for g in pack_validator.validate(data, files)["gaps"] if g["item"] == "mainframe.title")
        self.assertEqual(gap["severity"], "critical")
        self.assertIn("scr:0a1b2c3d4e5f", gap["details"][0])


class HostLayoutTest(unittest.TestCase):
    """Gap 3: where labels, the message line and the key line sit was not in the pack."""

    def test_protected_fields_render_as_a_layout_table(self):
        text = task(with_layout(mainframe_pack()[0]))
        self.assertIn("| Row | Col | Length | Attribute | Text or shape |", text)
        self.assertIn("| 1 | 30 | 15 | protected, intensified | `ACCOUNT INQUIRY` |", text)
        self.assertIn("| 4 | 20 | 10 | input, numeric | (input: ACCOUNT) |", text)
        self.assertIn("| 22 | 2 | 40 | protected | shape `XXXXXXX XXXX` |", text)
        self.assertIn("| 24 | 2 | 30 | protected | `PF3 EXIT ENTER INQUIRE` |", text)
        rows = [line for line in text.splitlines() if line.startswith("| ") and line[2].isdigit()]
        self.assertEqual([int(r.split("|")[1]) for r in rows[:5]], [1, 4, 4, 22, 24])

    def test_protected_fields_are_not_listed_as_input_controls(self):
        text = task(with_layout(mainframe_pack()[0]))
        self.assertNotIn("`field at row 24, column 2, length 30`", text)
        self.assertIn("`field at row 4, column 20, length 10`", text)

    def test_without_protected_fields_the_layout_is_an_important_gap(self):
        item = pack_validator.BY_ID["mainframe.protected"]
        self.assertEqual(item.severity, "important")
        self.assertTrue(item.capture_gap)
        data, files = mainframe_pack()
        data["screens"] = with_layout(data)["screens"]
        self.assertNotIn("mainframe.protected", {g["item"] for g in pack_validator.validate(data, files)["gaps"]})
        data, files = mainframe_pack()
        for st in data["screens"][0]["states"]:
            st["controls"] = [c for c in st["controls"] if not c.get("protected")]
        result = pack_validator.validate(data, files)
        gap = next(g for g in result["gaps"] if g["item"] == "mainframe.protected")
        self.assertEqual(gap["severity"], "important")
        self.assertTrue(result["passed"])
        self.assertEqual(len(gap["details"]), 1)
        self.assertIn("ACCOUNT INQUIRY", gap["details"][0])


# (text, what must be gone or None when nothing may change)
REDACTION_CASES = [
    # prose: an owner's failure case, not a secret (rebuild proof and milestone)
    ("Sign in without a password: the sign-in page shows 'Give your user name and your password.' and keeps the "
     "user name filled in.", None),
    ("Sign in without a password: 'Give your user name and your password.' (owner-stated).", None),
    ("Forgot password: a link under the button opens the reset page.", None),
    ("The token: it expires after an hour of no use.", None),
    # real secrets
    ("password: Hunter2!", "Hunter2!"),
    ("token=abc123", "abc123"),
    ("password=swordfish", "swordfish"),
    ("password: swordfish", "swordfish"),
    ("api_key: 'k3y-value'", "k3y-value"),
    ('{"password": "S3cret value"}', "S3cret"),
    ("Server=db;User Id=sa;Password=S3cret!;Database=app", "S3cret!"),
    ("postgresql://demo:hunter2@db.internal:5432/app", "hunter2"),
    ("mysql://root:pw@localhost/x", ":pw@"),
    ("sqlserver://sa:Pa55word@db/app", "Pa55word"),
]


class RedactionTest(unittest.TestCase):
    """Gap 5: "password: the sign-in page" became "password: [REDACTED:secret] sign-in page"."""

    def test_prose_is_kept_and_secrets_are_redacted(self):
        for text, secret in REDACTION_CASES:
            with self.subTest(text):
                found = __import__("collections").Counter()
                out = redact.redact_text(text, found)
                if secret is None:
                    self.assertEqual(out, text)
                    self.assertFalse(found)
                else:
                    self.assertNotIn(secret, out)
                    self.assertIn("[REDACTED:", out)


def openshift_yaml(data):
    return next(p for p in data["target"]["deploy"]["files"] if p.endswith("openshift.yaml"))


class TargetTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        database = new_database(work.name)
        app = create_app(database)
        bound = artifacts.bind(artifacts.Binding(app.state.artifacts))
        self.addCleanup(artifacts.unbind, bound)
        self.db = connect(database)
        self.addCleanup(self.db.close)

    def app(self, name, surface, platform="", backend=""):
        with self.db:
            return self.db.execute(
                "INSERT INTO apps(name, git_url, requirements, status, surface, target_platform, data_backend) "
                "VALUES (?, '', '', 'known', ?, ?, ?) RETURNING id", (name, surface, platform, backend)).fetchone()[0]

    def draft(self, app_id):
        d = buildpack.draft(self.db, app_id, VERSION_PATH)
        return d, json.loads(d.manifest), d.files["BUILD_TASK.md"].decode("utf-8")

    def test_desktop_data_is_in_memory_unless_database_url_is_set(self):  # gap 9
        _d, data, text = self.draft(self.app("CaseDesk", "desktop"))
        data_section = text.split("## Data", 1)[1].split("\n## ", 1)[0]
        self.assertIn("in memory unless `DATABASE_URL` is set", data_section)
        self.assertNotIn("reads its connection from `DATABASE_URL`", data_section)
        self.assertEqual(data["target"]["data_backend"]["storage"], "memory unless DATABASE_URL is set")

    def test_the_owners_desktop_choice_wins(self):  # gap 9
        _d, data, text = self.draft(self.app("CaseDesk", "desktop", backend="sqlserver"))
        data_section = text.split("## Data", 1)[1].split("\n## ", 1)[0]
        self.assertIn("SQL Server", data_section)
        self.assertIn("reads its connection from `DATABASE_URL`", data_section)
        self.assertNotIn("in memory", data_section)
        self.assertNotIn("storage", data["target"]["data_backend"])

    def test_run_settings_bind_locally_and_in_the_container(self):  # gap 13
        draft, data, text = self.draft(self.app("Orders", "web"))
        run = text.split("## Run settings", 1)[1].split("\n## ", 1)[0]
        self.assertIn("127.0.0.1", run)
        self.assertIn("`HOST`", run)
        self.assertIn("0.0.0.0", run)
        manifest = yaml.safe_load(draft.files[openshift_yaml(data)])
        pod = next(i for i in manifest["items"] if i["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertIn({"name": "HOST", "value": "0.0.0.0"}, pod["containers"][0]["env"])

    def test_a_mainframe_target_has_a_separate_http_port_for_the_version(self):  # gap 14
        draft, data, text = self.draft(self.app("Account Inquiry", "mainframe"))
        deploy = data["target"]["deploy"]
        self.assertEqual((data["target"]["kind"], deploy["port"], deploy["tn3270_port"]), ("mainframe", 8080, 3270))
        run = text.split("## Run settings", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`TN3270_PORT`", run)
        self.assertIn("separate", run)
        for path in [p for p in deploy["files"] if p.endswith("openshift.yaml")]:
            manifest = yaml.safe_load(draft.files[path])
            items = {i["kind"]: i for i in manifest["items"]}
            container = items["Deployment"]["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual({(p["name"], p["containerPort"]) for p in container["ports"]},
                             {("http", 8080), ("tn3270", 3270)})
            self.assertIn({"name": "TN3270_PORT", "value": "3270"}, container["env"])
            self.assertEqual(container["readinessProbe"]["httpGet"]["port"], "http")
            self.assertEqual({p["name"] for p in items["Service"]["spec"]["ports"]}, {"http", "tn3270"})
            self.assertEqual(items["Route"]["spec"]["port"]["targetPort"], "http")
        release = yaml.safe_load(demo_target.release_manifest(
            {"id": 3, "name": "Account Inquiry", "git_url": ""}, "sqlite", VERSION_PATH,
            {"commit": "a" * 40, "version": "1.0.0"}, surface="mainframe").split("\n", 7)[-1])
        service = next(i for i in release["items"] if i["kind"] == "Service")
        self.assertEqual({p["name"] for p in service["spec"]["ports"]}, {"http", "tn3270"})

    def test_a_web_target_has_one_port(self):  # gap 14: only mainframe gets a second port
        draft, data, _text = self.draft(self.app("Orders", "web"))
        self.assertNotIn("tn3270_port", data["target"]["deploy"])
        manifest = yaml.safe_load(draft.files[openshift_yaml(data)])
        service = next(i for i in manifest["items"] if i["kind"] == "Service")
        self.assertEqual([p["name"] for p in service["spec"]["ports"]], ["http"])


class UnrecordedBehaviourTest(unittest.TestCase):
    """Gaps 18-20: required fields, direct URLs and unlisted keys were never recorded."""

    def section(self, text):
        return text.split("## Behaviour not recorded", 1)[1].split("\n## ", 1)[0]

    def test_required_fields_are_listed(self):  # gap 18
        section = self.section(task(*web_pack()))
        self.assertIn("Required fields", section)
        self.assertIn('`input#amount` "Amount"', section)
        self.assertNotIn("save", section.split("Required fields", 1)[1].split("\n\n", 1)[0])

    def test_web_pages_are_reached_only_as_recorded(self):  # gap 19
        section = self.section(task(*web_pack()))
        self.assertIn("pages are reached only as recorded; a direct URL to a later step returns to the start",
                      section)
        self.assertIn("unless a scenario says otherwise", section)
        self.assertNotIn("keys not listed do nothing", section)

    def test_mainframe_keys_not_listed_do_nothing(self):  # gap 20
        section = self.section(task(*mainframe_pack()))
        self.assertIn("keys not listed do nothing", section)
        self.assertIn("`ACCOUNT INQUIRY`: Enter", section)
        self.assertIn("`ACCOUNT DETAIL`: PF3", section)
        self.assertNotIn("direct URL", section)

    def test_an_aid_step_named_only_by_its_label_keeps_its_key(self):
        # the rebuild proof's PF3 step: the recorder's flow kept `label: PF3` and dropped `key`
        event = {"event_id": "e", "seq": 20, "action": "aid"}
        step = {"action": "aid", "control": "key", "label": "PF3", "key": None, "screen": "ACCOUNT INQUIRY"}
        self.assertEqual(buildpack.event_entry(event, step)["key"], "PF3")
        self.assertNotIn("key", buildpack.event_entry(event, dict(step, label="Hello")))
        self.assertNotIn("key", buildpack.event_entry({**event, "action": "click"},
                                                      dict(step, action="click", control="button")))

    def test_the_builder_follows_the_owners_failure_cases(self):
        for make in (web_pack, desktop_pack, mainframe_pack):
            with self.subTest(make.__name__):
                self.assertIn("Follow the owner's failure cases", self.section(task(*make())))

    def test_no_failure_cases_and_no_precondition_rules_is_an_important_gap(self):
        item = pack_validator.BY_ID["behaviour.unrecorded"]
        self.assertEqual((item.severity, item.surfaces), ("important", pack_validator.SURFACES))
        for make in (web_pack, mainframe_pack):
            with self.subTest(make.__name__):
                data, files = make()
                data["scenarios"][0]["failure_cases"] = []
                data["scenarios"][0]["notes"] = ""
                result = pack_validator.validate(data, files)
                gap = next(g for g in result["gaps"] if g["item"] == "behaviour.unrecorded")
                self.assertEqual(gap["severity"], "important")
                self.assertTrue(result["passed"])
                data["scenarios"][0]["notes"] = "The invoice form is only reached from a found order."
                self.assertNotIn("behaviour.unrecorded",
                                 {g["item"] for g in pack_validator.validate(data, files)["gaps"]})
        data, files = mainframe_pack()
        data["scenarios"][0]["failure_cases"] = []
        self.assertIn("The owner wrote no failure cases", self.section(task(data, files)))


if __name__ == "__main__":
    unittest.main()
