"""One recorder session across ten applications, through the agent API, to ten separate builder
packages.

The session is `tests/fixtures/ten-app-session` (see its README): six web applications (two on
one origin, told apart by path), three desktop programs (two with the same window title "Billing"
and different executables) and a TN3270 host, with their steps interleaved in time. The
references are what the recorder's own timeline builds from that folder and stamps for sending
(`contract_references`). They arrive in several sends: the first half of the session, the second
half, that second half again (a resent batch), then the whole session.

The test drives only the agent API over HTTP (pairing, the offered session, the references); the
owner's part (the catalog applications with their web addresses, scenarios, attachments, the
approval of each package) goes through the modules the portal's routes call, so it does not
depend on the pages.

Only `pair`, `send`, the offered-session read in `record` and the download-route test use the
HTTP test client; `assignment`, `prepare`, `freeze` and every package assertion read the
database and the domain modules, so moving the routes to another framework changes only those.
"""

import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from zipfile import ZipFile

from twinlab2.portal import VERSION_PATH, create_app
from twinlab2 import artifacts, auth, buildpack, packages, recording_export, redact, registry, scenarios
from twinlab2_recorder import main as agent_main
from twinlab2_contract import recorder_contract as contract
from support import Client, connect, new_database

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
sys.path.insert(0, str(FIXTURES / "ten-app-session"))
import generate as fixture  # noqa: E402

BUILT = FIXTURES / "references-ten-apps.json"
RECORDING = "ws-" + fixture.SESSION_ID
OWNER = "ana"
NOW = "2026-11-20T15:00:00+00:00"

# What each application is in the portal once the references are in: its tag (the word in its
# labels, generate.STEPS), its surface and the name it is registered under.
EXPECTED = {
    "billing": ("web", "Billing"),
    "admin": ("web", "Billing Admin"),
    "meter": ("web", "Zählerstand Portal – Nord"),
    "invoice": ("web", "請求 システム"),
    "orders": ("web", "orders.example.test"),
    "hr": ("web", "127.0.0.1:8081"),
    "contoso": ("desktop", "Billing (Contoso Ltd)"),
    "plain": ("desktop", "Billing (billing.exe)"),
    "kasse": ("desktop", "Kasse – Filiale Nord"),
    "mainframe": ("mainframe", "Kontoführung CICS"),
    "ledger": ("web", "Finance Hub"),
    "receivable": ("web", "Accounts Receivable"),
    "payroll": ("web", "Payroll"),
}
CATALOG_TAGS = ("billing", "admin", "meter", "invoice", "ledger", "receivable", "payroll")  # ids 1-7


def references():
    """The fixture's references as the recorder sends them (stamped and checked by its own code)."""
    return agent_main.contract_references(json.loads(BUILT.read_text(encoding="utf-8")))


def part(doc, keep):
    """The references with only the steps `keep(step)` accepts: the applications without a step
    left out, the timeline cut to the applications sent."""
    out = json.loads(json.dumps(doc))
    out["flows"] = {k: [s for s in v if keep(s)] for k, v in out["flows"].items()}
    out["flows"] = {k: v for k, v in out["flows"].items() if v}
    out["apps"] = [a for a in out["apps"] if a["key"] in out["flows"]]
    out["timeline"] = [r for r in out["timeline"] if r["app"] == "presence" or r["app"] in out["flows"]]
    return out


class TenAppRecordingTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.database = new_database(self.work.name)
        self.app = create_app(self.database)
        self.client = Client(self.app).http  # the recorder's agent API (no CSRF, no session)
        # the portal's artifact store for the domain calls made directly below
        bound = artifacts.bind(artifacts.Binding(self.app.state.artifacts))
        self.addCleanup(artifacts.unbind, bound)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)

    # -- the owner's catalog and the machine -----------------------------------------------------

    def catalog(self):
        """The four catalog applications of the offered session, with their web addresses."""
        with self.db:
            for number, item in enumerate(fixture.CATALOG, 1):
                app_id = self.db.execute(
                    "INSERT INTO apps(name, git_url, requirements, status, surface) VALUES (?, '', '', 'known', 'web') "
                    "RETURNING id", (item["name"],)).fetchone()[0]
                self.assertEqual(app_id, number)
                self.db.execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, 'web', '{}')",
                                (registry.normalise_key(item["key"]), app_id))
                auth.assign(self.db, app_id, OWNER, "owner", "test", NOW)

    def pair(self):
        """A pairing code the owner made, exchanged by the recorder for its token (agent API)."""
        code = "ABCDEFGHJK"
        with self.db:
            self.db.execute("INSERT INTO pairing_codes(code_sha, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
                            (registry.sha(code), OWNER, NOW, "2999-01-01T00:00:00+00:00"))
        response = self.client.post("/api/agent/pair", json={"contract_version": contract.CONTRACT_VERSION,
                                                             "code": code, "name": "Workstation 10"})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def offer(self, machine_id):
        """The portal session the recorder is offered: the catalog applications as targets."""
        targets = []
        for app_id in range(1, len(fixture.CATALOG) + 1):
            name = self.db.execute("SELECT name FROM apps WHERE id = ?", (app_id,)).fetchone()[0]
            for row in self.db.execute("SELECT key FROM app_identities WHERE app_id = ?", (app_id,)).fetchall():
                targets.append(registry.target_of(row[0], "full", app_id, name))
        with self.db:
            self.db.execute("""INSERT INTO capture_sessions(name, machine_id, targets, created_by, created_at, status)
                               VALUES ('Month-end close', ?, ?, ?, ?, 'offered')""",
                            (machine_id, json.dumps(targets), OWNER, NOW))

    def send(self, headers, doc):
        self.assertEqual(contract.errors(doc, "references"), [])
        response = self.client.post("/api/agent/references", json=doc, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def record(self):
        """Pair, take the offered session, send the references in parts; returns {tag: app_id}."""
        self.catalog()
        headers = self.pair()
        self.offer(1)
        offered = self.client.get("/api/agent/sessions", headers=headers).json()["sessions"]
        self.assertEqual([s["id"] for s in offered], [1])
        paths = {(t.get("origin"), t.get("path")) for t in offered[0]["targets"]}
        self.assertTrue({("https://erp.example.test", "/billing"), ("https://erp.example.test", "/billing-admin")} <= paths)
        doc = references()
        self.assertEqual(doc["contract_version"], "1.3.0")
        middle = fixture.ts(len(fixture.STEPS) * 5 // 2)
        first, second = part(doc, lambda s: s["captured_at"] < middle), part(doc, lambda s: s["captured_at"] >= middle)
        self.assertTrue(first["flows"] and second["flows"])
        self.assertNotEqual(set(first["flows"]), set(doc["flows"]))  # each part misses applications
        self.send(headers, first)
        self.send(headers, second)
        again = self.send(headers, second)  # the same batch again
        self.assertEqual(again["discovered"], [])
        self.assignment()  # the two halves hold the whole session: nothing the first sent was lost
        final = self.send(headers, doc)
        self.assertEqual(final["discovered"], [])
        return self.assignment()

    def assignment(self):
        """{tag: app_id} from the stored flows; fails when an application holds steps of two
        applications or a step is on two applications."""
        tags = fixture.tags()
        found, seen = {}, {}
        for row in self.db.execute("SELECT app_id, flow FROM recording_apps WHERE recording_id = ?", (RECORDING,)):
            for step in json.loads(row["flow"]):
                self.assertNotIn(step["event_id"], seen, "a step is on two applications")
                seen[step["event_id"]] = row["app_id"]
                found.setdefault(row["app_id"], set()).add(tags[step["event_id"]])
        self.assertEqual(set(seen), set(tags), "every recorded step lands on an application")
        for app_id, owned in found.items():
            self.assertEqual(len(owned), 1, f"application {app_id} holds the steps of {sorted(owned)}")
        return {next(iter(owned)): app_id for app_id, owned in found.items()}

    # -- the owner's part, per application -------------------------------------------------------

    def prepare(self, app_id, tag):
        """A scenario linking the application's selected events and one synthetic attachment."""
        selection = json.loads(self.db.execute("SELECT selection FROM evidence WHERE app_id = ? AND recording_id = ?",
                                               (app_id, RECORDING)).fetchone()[0])
        name = self.db.execute("SELECT name FROM apps WHERE id = ?", (app_id,)).fetchone()[0]
        scenarios.save(self.db, app_id, {
            "name": f"{name}: month-end", "starting": f"Signed in to {name}.", "actions": f"Open {tag}-start",
            "expected": f"{tag}-done", "failures": "", "examples": f"{tag}-example-1", "questions": "", "notes": "",
            "events": json.dumps([scenarios.event_ref(RECORDING, e) for e in selection])})
        content = f"# {name}\n\nAcceptance notes {tag}-attachment.\n".encode("utf-8")
        with self.db:
            ref = artifacts.save(self.db, artifacts.put(content, kind="restricted_evidence", app_id=app_id,
                                                        content_type="text/markdown"))
            self.db.execute("INSERT INTO attachments(app_id, title, kind, extension, content, artifact_id, share, added_by) "
                            "VALUES (?, ?, 'Acceptance', '.md', ?, ?, 'synthetic', ?)",
                            (app_id, f"{name} acceptance", b"", ref.id, OWNER))

    def freeze(self, app_id):
        draft = buildpack.draft(self.db, app_id, VERSION_PATH)
        with self.db:
            rev = packages.freeze(self.db, app_id, OWNER, draft.sha256, "", VERSION_PATH)
        return packages.get(self.db, app_id, rev)

    # -- the test -------------------------------------------------------------------------------------

    def test_ten_applications_get_ten_separate_packages(self):
        by_tag = self.record()
        apps = {r["id"]: r for r in self.db.execute("SELECT * FROM apps ORDER BY id").fetchall()}
        self.assertEqual(len(apps), len(EXPECTED), [a["name"] for a in apps.values()])
        self.assertEqual(set(by_tag), set(EXPECTED))
        for tag, (surface, name) in EXPECTED.items():
            app = apps[by_tag[tag]]
            self.assertEqual((app["surface"], app["name"]), (surface, name), tag)
        self.assertEqual(len({a["name"].casefold() for a in apps.values()}), len(EXPECTED), "application names are unique")
        # the catalog applications kept their ids; each discovered one is owned by who paired
        self.assertEqual([by_tag[t] for t in CATALOG_TAGS], list(range(1, len(CATALOG_TAGS) + 1)))
        for app_id in apps:
            self.assertEqual(auth.owners(self.db, app_id), [OWNER])
        # every application's evidence is exactly its own steps
        tags = fixture.tags()
        for tag, app_id in by_tag.items():
            selection = json.loads(self.db.execute("SELECT selection FROM evidence WHERE app_id = ? AND recording_id = ?",
                                                   (app_id, RECORDING)).fetchone()[0])
            self.assertEqual(selection, sorted(e for e, t in tags.items() if t == tag), tag)

        frozen = {}
        for tag, app_id in by_tag.items():
            self.prepare(app_id, tag)
        for tag, app_id in by_tag.items():
            frozen[tag] = self.freeze(app_id)
        self.assertEqual(len({row["sha256"] for row in frozen.values()}), len(EXPECTED))

        own_data = {}  # what only that application's package may hold
        for tag, app_id in by_tag.items():
            labels = {s.get("label") for row in self.db.execute(
                "SELECT flow FROM recording_apps WHERE app_id = ? AND recording_id = ?", (app_id, RECORDING))
                for s in json.loads(row["flow"]) if s.get("label")} - {"Enter"}
            hosts = {re.sub(r"^\w+:(//)?", "", k).split("/")[0] for k in [
                r[0] for r in self.db.execute("SELECT key FROM app_identities WHERE app_id = ?", (app_id,))]}
            events = {e for e, t in tags.items() if t == tag}
            own_data[tag] = {"name": apps[app_id]["name"], "markers": labels | events | {
                f"{tag}-start", f"{tag}-done", f"{tag}-example-1", f"{tag}-attachment"}, "hosts": hosts}
        names = set()
        for tag, row in frozen.items():
            app_id = by_tag[tag]
            data = packages.open_package(self.db, row["storage_ref"])
            files = buildpack.read(data)
            evidence = json.loads(files["evidence.json"])
            self.assertEqual(evidence["app"]["id"], app_id)
            self.assertEqual(evidence["app"]["name"], apps[app_id]["name"])
            self.assertEqual([r["recording_id"] for r in evidence["evidence"]], [RECORDING])
            self.assertEqual({e["event_id"] for r in evidence["evidence"] for e in r["events"]},
                             {e for e, t in tags.items() if t == tag})
            self.assertEqual(len(evidence["attachments"]), 1)
            self.assertEqual(set(files), {"BUILD_TASK.md", "evidence.json", evidence["attachments"][0]["path"]})
            self.assertTrue(files["BUILD_TASK.md"].decode("utf-8").startswith(f"# Build {apps[app_id]['name']}\n"))
            text = "\n".join(f"{path}\n{content.decode('utf-8')}" for path, content in files.items())
            mine = text.replace(own_data[tag]["name"], "")
            for other, facts in own_data.items():
                if other == tag:
                    continue
                if facts["name"] not in own_data[tag]["name"]:
                    self.assertNotIn(facts["name"], mine, f"{tag}'s package names {other}")
                for marker in facts["markers"]:
                    self.assertNotIn(marker, text, f"{tag}'s package holds {other}'s {marker}")
                for host in facts["hosts"] - own_data[tag]["hosts"]:
                    self.assertNotIn(host, text, f"{tag}'s package holds {other}'s address {host}")
            for marker in own_data[tag]["markers"]:
                self.assertIn(marker, text, f"{tag}'s package misses its own {marker}")
            filename = packages.download_name(apps[app_id], row)
            self.assertRegex(filename, r"^[\w-]+-rev1-[0-9a-f]{12}\.zip$")
            self.assertTrue(filename.endswith(f"-rev1-{row['revision'][:12]}.zip"))
            self.assertNotRegex(filename, r"[\s/\\:*?\"<>|]")
            names.add(filename)
        self.assertEqual(len(names), len(EXPECTED), sorted(names))
        slugs = {packages.slug(apps[by_tag[t]]) for t in by_tag}
        for expected in ("billing", "billing-admin", "zahlerstand-portal-nord", "請求-システム", "orders-example-test",
                         "127-0-0-1-8081", "billing-contoso-ltd", "billing-billing-exe", "kasse-filiale-nord",
                         "kontofuhrung-cics", "finance-hub", "accounts-receivable", "payroll"):
            self.assertTrue(any(s.startswith(expected) for s in slugs), (expected, sorted(slugs)))
        self.assertEqual(len(slugs), len(EXPECTED))

    def test_the_download_route_names_the_package_by_its_application(self):
        by_tag = self.record()
        app_id = by_tag["meter"]
        self.prepare(app_id, "meter")
        row = self.freeze(app_id)
        owner = self.signed_in(OWNER)
        response = owner.get(f"/api/v1/apps/{app_id}/build-pack")
        self.assertEqual(response.status_code, 200, response.text[:300])
        name = packages.download_name(self.db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone(), row)
        self.assertIn(f"filename={name}", response.headers["Content-Disposition"])
        with ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("evidence.json", archive.namelist())

    def signed_in(self, user):
        """A client signed in as `user` (a local user with a password: the sign-in page)."""
        with self.db:
            auth.save_user(self.db, user, password=f"{user}-pw", by="test", now=NOW)
        client = Client(self.app)
        response = client.post("/api/v1/session", json={"user": user, "password": f"{user}-pw"})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_the_recording_export_holds_the_packages_the_caller_builds(self):
        """GET /api/v1/recordings/{id}/packages/download: one ZIP, the packages of the applications
        the caller builds, the others listed in the manifest only; each included one audited."""
        by_tag = self.record()
        for tag in ("meter", "invoice"):
            self.prepare(by_tag[tag], tag)
            self.freeze(by_tag[tag])
        with self.db:  # an owner of one application of the ten (so a reviewer of the recording), nothing else
            auth.assign(self.db, by_tag["meter"], "bea", "owner", "test", NOW)
        bea = self.signed_in("bea")
        response = bea.get(f"/api/v1/recordings/{RECORDING}/packages/download")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/zip"))
        self.assertIn(f"{RECORDING.lower()}-packages-", response.headers["Content-Disposition"])
        with ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            included = [a for a in manifest["applications"] if a["status"] == "included"]
            self.assertEqual([a["app_id"] for a in included], [by_tag["meter"]])
            self.assertIn(included[0]["path"], archive.namelist())
            self.assertEqual(len(manifest["applications"]), len(EXPECTED))
            self.assertEqual({a["status"] for a in manifest["applications"] if a["app_id"] != by_tag["meter"]},
                             {"not_available"})
        audited = self.db.execute("SELECT app_id, details FROM audit_log WHERE action = 'package.download' "
                                  "AND actor = 'bea'").fetchall()
        self.assertEqual([r["app_id"] for r in audited], [by_tag["meter"]])
        # the owner gets both frozen packages; someone who may not see the recording gets nothing
        owner = self.signed_in(OWNER).get(f"/api/v1/recordings/{RECORDING}/packages/download")
        with ZipFile(io.BytesIO(owner.content)) as archive:
            statuses = {a["app_id"]: a["status"] for a in json.loads(archive.read("manifest.json"))["applications"]}
        self.assertEqual(statuses[by_tag["meter"]], "included")
        self.assertEqual(statuses[by_tag["invoice"]], "included")
        stranger = self.signed_in("sam").get(f"/api/v1/recordings/{RECORDING}/packages/download")
        self.assertEqual((stranger.status_code, stranger.json()["code"]), (403, "restricted"))
        self.assertEqual(self.signed_in("bea").get("/api/v1/recordings/nope/packages/download").status_code, 404)


def step(n, label, screen, channel="web"):
    return {"n": n, "seq": n, "action": "click", "control": "button", "label": label, "screen": screen,
            "event_id": f"019aa1c0-0000-7000-8000-{n:012x}", "channel": channel,
            "captured_at": f"2026-11-20T16:00:{n:02d}.000Z"}


def refs(apps, flows, session_id="20261120T160000Z-0b1c2d3e"):
    timeline = [{"app": key, "steps": len(steps)} for key, steps in flows.items()]
    return {"contract_version": "1.1.0", "format": "twinlab-references/1",
            "session": {"id": session_id, "name": "Focused", "state": "stopped"},
            "apps": apps, "flows": flows, "timeline": timeline,
            "completeness": {"overall": "complete", "channels": {"web": {"status": "complete"}},
                             "network_evidence": "none", "recovered": False}}


class SeparationFixesTest(TenAppRecordingTest):
    test_the_recording_export_holds_the_packages_the_caller_builds = None  # runs in the base class
    """One test per separation bug the ten-application recording found."""

    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None
    FOCUSED = "ws-20261120T160000Z-0b1c2d3e"

    def stored(self):
        """({application name: its labels}, number of events) of the focused recording."""
        out = {}
        for row in self.db.execute("""SELECT apps.name, recording_apps.flow FROM recording_apps
                                      JOIN apps ON apps.id = recording_apps.app_id WHERE recording_id = ?""",
                                   (self.FOCUSED,)):
            out.setdefault(row["name"], []).extend(s["label"] for s in json.loads(row["flow"]))
        events = json.loads(self.db.execute("SELECT events FROM recordings WHERE id = ?", (self.FOCUSED,)).fetchone()[0])
        return {k: sorted(v) for k, v in out.items()}, len(events)

    def test_a_send_with_some_applications_keeps_what_an_earlier_send_delivered(self):
        headers = self.pair()
        orders, hr = "web:https://orders.example.test", "web:http://127.0.0.1:8081"
        first = refs([{"key": orders, "level": "full", "display": "Orders"}],
                     {orders: [step(1, "ordersCreate", "https://orders.example.test/orders/new")]})
        second = refs([{"key": hr, "level": "full", "display": "HR"}],
                      {hr: [step(2, "hrLeave", "http://127.0.0.1:8081/leave")]})
        self.send(headers, first)
        self.send(headers, second)
        self.send(headers, second)  # resent: nothing doubles
        self.assertEqual(self.stored(), ({"Orders": ["ordersCreate"], "HR": ["hrLeave"]}, 2))
        # a later send of a step it sent before, now with another label: that copy wins, once
        later = refs([{"key": orders, "level": "full", "display": "Orders"}],
                     {orders: [step(1, "ordersSubmit", "https://orders.example.test/orders/new"),
                               step(3, "ordersSearch", "https://orders.example.test/orders/search")]})
        self.send(headers, later)
        self.assertEqual(self.stored(), ({"Orders": ["ordersSearch", "ordersSubmit"], "HR": ["hrLeave"]}, 3))
        evidence = dict(self.db.execute("SELECT apps.name, evidence.selection FROM evidence "
                                        "JOIN apps ON apps.id = evidence.app_id").fetchall())
        self.assertEqual({k: len(json.loads(v)) for k, v in evidence.items()}, {"Orders": 2, "HR": 1})

    def test_a_discovered_application_never_takes_another_applications_name(self):
        headers = self.pair()
        web, contoso, plain = "web:https://billing.example.test", "desktop:Contoso Ltd/Billing", "desktop:exe/billing.exe"
        self.send(headers, refs(
            [{"key": web, "level": "full", "display": "Billing"},
             {"key": contoso, "level": "full", "display": "Billing", "exe": "ContosoBilling.exe",
              "company": "Contoso Ltd", "product": "Billing"},
             {"key": plain, "level": "full", "display": "billing", "exe": "billing.exe"}],
            {web: [step(1, "webPay", "https://billing.example.test/pay")],
             contoso: [step(2, "contosoPost", "ContosoMain", "desktop")],
             plain: [step(3, "plainPrint", "BillingMainForm", "desktop")]}))
        names = [r[0] for r in self.db.execute("SELECT name FROM apps ORDER BY id")]
        self.assertEqual(names, ["Billing", "Billing (Contoso Ltd)", "billing (billing.exe)"])
        # another with the same name and the same qualifier is numbered
        self.assertEqual(registry.unique_name(self.db, "Billing", {"key": "desktop:Contoso Ltd/Billing 2",
                                                                    "company": "Contoso Ltd"}), "Billing 2")

    def test_the_rest_of_a_shared_host_is_named_after_the_host_not_a_path_application(self):
        self.catalog()  # Billing (/billing) and Billing Admin (/billing-admin) on erp.example.test
        headers = self.pair()
        erp = "web:https://erp.example.test"
        self.send(headers, refs(
            [{"key": erp, "level": "full", "display": "Billing Admin", "app_id": 2}],  # as the recorder names it
            {erp: [step(1, "adminRate", "https://erp.example.test/billing-admin/{id}"),
                   step(2, "billingPay", "https://erp.example.test/billing/{id}"),
                   step(3, "signIn", "https://erp.example.test/login")]}))
        self.assertEqual(self.stored()[0], {"Billing Admin": ["adminRate"], "Billing": ["billingPay"],
                                            "erp.example.test": ["signIn"]})

    def test_an_event_id_that_looks_like_a_card_number_reaches_the_package_intact(self):
        event = "019aa1b0-0011-7000-8000-000000000011"  # its last 16 digits pass the Luhn check
        self.assertEqual(redact.redact_text(f"Event {event} of ws-1"), f"Event {event} of ws-1")
        self.assertEqual(redact.redact_text("card 4111 1111 1111 1111"), "card [REDACTED:card number]")
        self.assertEqual(redact.redact_text(f"Bearer {event}"), "[REDACTED:token]")
        headers = self.pair()
        web = "web:https://orders.example.test"
        self.send(headers, refs([{"key": web, "level": "full", "display": "Orders"}],
                                {web: [dict(step(1, "ordersCreate", "https://orders.example.test/orders/new"),
                                            event_id=event)]}))
        scenarios.save(self.db, 1, {"name": "Create", "expected": "Created",
                                    "events": json.dumps([scenarios.event_ref(self.FOCUSED, event)])})
        draft = buildpack.draft(self.db, 1, VERSION_PATH)
        data = json.loads(draft.manifest)
        self.assertEqual(data["evidence"][0]["events"][0]["event_id"], event)
        self.assertEqual(data["scenarios"][0]["events"][0]["event_id"], event)
        self.assertNotIn("REDACTED", draft.files["BUILD_TASK.md"].decode("utf-8"))
        self.assertEqual(draft.redacted, [])

    def test_package_names_are_safe_unique_slugs_of_the_application_name(self):
        cases = {"Billing": "billing-1", "Billing Admin": "billing-admin-2",
                 "Zählerstand Portal – Nord": "zahlerstand-portal-nord-3", "請求 システム": "請求-システム-4",
                 "データベース": "データベース-5", "  ": "app-6", "../../etc/passwd": "etc-passwd-7",
                 "a_b__c": "a-b-c-8", "x" * 200: "x" * 60 + "-9"}
        for number, (name, expected) in enumerate(cases.items(), 1):
            self.assertEqual(packages.slug({"id": number, "name": name}), expected)
        row = {"rev": 3, "revision": "0123456789abcdef" * 4}
        self.assertEqual(packages.download_name({"id": 2, "name": "Billing Admin"}, row),
                         "billing-admin-2-rev3-0123456789ab.zip")


def as_1_1(doc):
    """The references as a 1.1 recorder would have built them from the same session: every web
    screen by the first-segment rule, no target paths, contract 1.1.0."""
    from twinlab2_recorder.workstation import flow
    from twinlab2_recorder.workstation.levels import origin_of

    out = json.loads(json.dumps(doc))
    urls = fixture.urls()
    for steps in out["flows"].values():
        for s in steps:
            if s["event_id"] in urls:
                url = urls[s["event_id"]]
                s["screen"] = origin_of(url) + flow.route_template(url)
    for t in out["session"].get("targets") or []:
        t.pop("path", None)
    out["contract_version"] = "1.1.0"
    return out


class OlderRecordingsAndPrefixesTest(TenAppRecordingTest):
    """Contract 1.2 path prefixes, against recordings of 1.1 recorders on the same catalog."""

    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None
    test_the_recording_export_holds_the_packages_the_caller_builds = None

    def flows(self):
        """{app name: [event ids]} of the ten-app recording."""
        out = {}
        for row in self.db.execute("""SELECT apps.name, recording_apps.flow FROM recording_apps
                                      JOIN apps ON apps.id = recording_apps.app_id WHERE recording_id = ?""",
                                   (RECORDING,)):
            out.setdefault(row["name"], []).extend(s["event_id"] for s in json.loads(row["flow"]))
        return out

    def take_offer(self):
        self.catalog()
        headers = self.pair()
        self.offer(1)
        self.client.get("/api/agent/sessions", headers=headers)
        return headers

    def test_every_finance_step_lands_on_its_own_prefix(self):
        by_tag = self.record()
        tags = fixture.tags()
        for tag in fixture.FINANCE:
            screens = [s["screen"] for row in self.db.execute(
                "SELECT flow FROM recording_apps WHERE app_id = ? AND recording_id = ?", (by_tag[tag], RECORDING))
                for s in json.loads(row["flow"])]
            self.assertEqual(len(screens), sum(1 for t in tags.values() if t == tag), tag)
        self.assertEqual([by_tag[t] for t in fixture.FINANCE], [5, 6, 7])
        flows = self.flows()
        self.assertEqual(sorted(tags[e] for e in flows["Finance Hub"]), ["ledger"] * 3)
        self.assertEqual(sorted(tags[e] for e in flows["Accounts Receivable"]), ["receivable"] * 2)
        self.assertEqual(sorted(tags[e] for e in flows["Payroll"]), ["payroll"] * 2)

    def test_a_1_1_recording_of_the_ten_applications_is_assigned_as_before(self):
        """The fixture as the PR 23 recorder sent it, on a catalog that now also has the three
        finance applications: the same ten applications get the same steps."""
        headers = self.take_offer()
        doc = agent_main.contract_references(json.loads(fixture.LEGACY.read_text(encoding="utf-8")))
        self.assertEqual(doc["contract_version"], "1.1.0")
        self.send(headers, doc)
        stored = self.db.execute("SELECT contract_version FROM recording_contracts WHERE recording_id = ?",
                                 (RECORDING,)).fetchone()
        self.assertEqual(stored[0], "1.1.0")
        legacy = fixture.legacy_tags()
        flows = self.flows()
        self.assertEqual({name: sorted({legacy[e] for e in events}) for name, events in flows.items()},
                         {name: [tag] for tag, (_surface, name) in EXPECTED.items() if tag not in fixture.FINANCE})
        self.assertEqual(sum(len(v) for v in flows.values()), len(legacy))
        for app_id in (5, 6, 7):  # the finance catalog applications are not in that recording
            self.assertIsNone(self.db.execute("SELECT 1 FROM recording_apps WHERE app_id = ?", (app_id,)).fetchone())

    def test_a_1_1_recording_under_nested_prefixes_goes_to_the_shortest_one(self):
        """A 1.1 recorder kept only the first segment, so its finance steps are all under
        `/finance`: they go to Finance Hub, as they would have before 1.2; the others are unchanged."""
        headers = self.take_offer()
        doc = as_1_1(references())
        self.assertEqual(contract.errors(doc, "references"), [])
        self.send(headers, doc)
        tags = fixture.tags()
        flows = self.flows()
        self.assertEqual(sorted(tags[e] for e in flows["Finance Hub"]),
                         sorted(t for t in tags.values() if t in fixture.FINANCE))
        self.assertNotIn("Accounts Receivable", flows)
        self.assertNotIn("Payroll", flows)
        for tag, (_surface, name) in EXPECTED.items():
            if tag not in fixture.FINANCE:
                self.assertEqual({tags[e] for e in flows[name]}, {tag}, name)

    def test_the_longest_prefix_wins_whatever_order_the_identities_were_made_in(self):
        headers = self.pair()
        fin = "https://fin.example.test"
        with self.db:  # the deeper prefixes first, the shortest last
            for name, path in (("Payroll", "/finance/payroll"), ("Accounts Receivable", "/finance/billing"),
                               ("Finance Hub", "/finance")):
                app_id = self.db.execute("INSERT INTO apps(name, git_url, requirements, status, surface) "
                                         "VALUES (?, '', '', 'known', 'web') RETURNING id", (name,)).fetchone()[0]
                self.db.execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, 'web', '{}')",
                                (registry.normalise_key(fin + path), app_id))
        key = "web:" + fin
        self.send(headers, refs([{"key": key, "level": "full", "display": "fin"}], {key: [
            step(1, "a", fin + "/finance/billing/{id}"), step(2, "b", fin + "/finance/payroll"),
            step(3, "c", fin + "/finance/{id}/edit"), step(4, "d", fin + "/finance/billing-archive/{id}"),
            step(5, "e", fin + "/finance"), step(6, "f", fin + "/finance/payroll/{id}/new"),
            step(7, "g", fin + "/finance/billing"), step(8, "h", fin + "/other")]}))
        stored = {}
        for row in self.db.execute("""SELECT apps.name, recording_apps.flow FROM recording_apps
                                      JOIN apps ON apps.id = recording_apps.app_id"""):
            stored.setdefault(row["name"], []).extend(s["label"] for s in json.loads(row["flow"]))
        self.assertEqual({k: sorted(v) for k, v in stored.items()},
                         {"Accounts Receivable": ["a", "g"], "Payroll": ["b", "f"], "Finance Hub": ["c", "d", "e"],
                          "fin": ["h"]})  # the rest of the host: the recorded display name


class RecordingNameTest(TenAppRecordingTest):
    """A recording named after several applications: no application's package carries that name,
    or any other application's name, in any file."""

    test_ten_applications_get_ten_separate_packages = None
    test_the_download_route_names_the_package_by_its_application = None
    test_the_recording_export_holds_the_packages_the_caller_builds = None
    NAME = "Billing + Payroll + CaseDesk walkthrough"
    RECORDING = "ws-20261121T091500Z-0c1d2e3f"

    def test_no_package_names_the_recording_or_another_application(self):
        headers = self.pair()
        erp, desk = "web:https://erp.example.test", "desktop:Acme Corp/CaseDesk"
        with self.db:
            for name, key in (("Billing", f"{erp}/ar"), ("Payroll", f"{erp}/hr/pay")):
                app_id = self.db.execute("INSERT INTO apps(name, git_url, requirements, status, surface) "
                                         "VALUES (?, '', '', 'known', 'web') RETURNING id", (name,)).fetchone()[0]
                self.db.execute("INSERT INTO app_identities(key, app_id, surface, facts) VALUES (?, ?, 'web', '{}')",
                                (registry.normalise_key(key), app_id))
                auth.assign(self.db, app_id, OWNER, "owner", "test", NOW)
        doc = refs([{"key": erp, "level": "full", "display": "erp"},
                    {"key": desk, "level": "full", "display": "CaseDesk", "exe": "CaseDesk.exe",
                     "company": "Acme Corp", "product": "CaseDesk"}],
                   {erp: [step(1, "arInvoiceOpen", "https://erp.example.test/ar/{id}"),
                          step(2, "hrPayRun", "https://erp.example.test/hr/pay/{id}")],
                    desk: [step(3, "caseOpen", "MainWindow", "desktop")]},
                   session_id=self.RECORDING[3:])
        doc["session"].update(name=self.NAME, started="2026-11-21T09:15:00.000Z")
        self.send(headers, doc)
        apps = {r["name"]: r["id"] for r in self.db.execute("SELECT id, name FROM apps")}
        self.assertEqual(set(apps), {"Billing", "Payroll", "CaseDesk"})
        self.assertEqual(self.db.execute("SELECT name FROM recordings WHERE id = ?", (self.RECORDING,)).fetchone()[0],
                         self.NAME)
        with self.db:
            auth.assign(self.db, apps["CaseDesk"], OWNER, "owner", "test", NOW)
        packs = {}
        for name, app_id in apps.items():
            selection = json.loads(self.db.execute("SELECT selection FROM evidence WHERE app_id = ?",
                                                   (app_id,)).fetchone()[0])
            scenarios.save(self.db, app_id, {"name": "Month end", "expected": "Done", "events": json.dumps(
                [scenarios.event_ref(self.RECORDING, e) for e in selection])})
            content = b"# Acceptance\n\nSynthetic notes.\n"
            with self.db:
                ref = artifacts.save(self.db, artifacts.put(content, kind="restricted_evidence", app_id=app_id,
                                                            content_type="text/markdown"))
                self.db.execute("INSERT INTO attachments(app_id, title, kind, extension, content, artifact_id, share, "
                                "added_by) VALUES (?, 'Acceptance notes', 'Acceptance', '.md', ?, ?, 'synthetic', ?)",
                                (app_id, b"", ref.id, OWNER))
            draft = buildpack.draft(self.db, app_id, VERSION_PATH)
            with self.db:
                rev = packages.freeze(self.db, app_id, OWNER, draft.sha256, "", VERSION_PATH)
            packs[name] = packages.get(self.db, app_id, rev)

        def scan(data, own, where):
            """Every file name and file of a package ZIP, case-insensitively."""
            files = buildpack.read(data)
            self.assertTrue(files)
            for path, content in files.items():
                for text in (path, content.decode("utf-8")):
                    low = text.casefold()
                    self.assertNotIn(self.NAME.casefold(), low, f"{where}: {path}")
                    self.assertNotIn("walkthrough", low, f"{where}: {path}")
                    for other in apps:
                        if other != own:
                            self.assertNotIn(other.casefold(), low, f"{where}: {path} names {other}")
            return files

        for name, row in packs.items():
            filename = packages.download_name({"id": apps[name], "name": name}, row)
            for other in apps:
                if other != name:
                    self.assertNotIn(other.casefold(), filename.casefold())
            files = scan(packages.open_package(self.db, row["storage_ref"]), name, name)
            evidence = json.loads(files["evidence.json"])
            self.assertEqual(evidence["evidence"][0]["recording_id"], self.RECORDING)
            self.assertEqual(evidence["evidence"][0]["recorded_on"], "2026-11-21")
            self.assertNotIn("recording", evidence["evidence"][0])
            self.assertIn(self.RECORDING, files["BUILD_TASK.md"].decode("utf-8"))

        # the per-recording export may name every application in its manifest, and the
        # recording; each application's folder holds only that application's package
        export = recording_export.export(self.db, self.RECORDING, OWNER)
        self.assertEqual(export.manifest["recording"], {"id": self.RECORDING, "name": self.NAME})
        self.assertEqual({e["name"] for e in export.manifest["applications"]}, set(apps))
        with ZipFile(io.BytesIO(export.data)) as archive:
            paths = [p for p in archive.namelist() if p != "manifest.json"]
            self.assertEqual(len(paths), 3)
            for entry in export.manifest["applications"]:
                folder = [p for p in paths if p.startswith(entry["slug"] + "/")]
                self.assertEqual(folder, [entry["path"]])
                for other in apps:
                    if other != entry["name"]:
                        self.assertNotIn(other.casefold(), entry["path"].casefold())
                scan(archive.read(entry["path"]), entry["name"], f"export {entry['path']}")


if __name__ == "__main__":
    unittest.main()
