"""Deploying the recorder on managed PCs, without a portal: the machine policy (registry and
policy.json), proxy choice (explicit, PAC/WinINET strings, bypass lists, environment), a real
forward proxy with CONNECT and Basic credentials, a private CA for TLS inspection, retries with
backoff, the updater's checks, the uninstaller's clean-up, the build's exclusions and the MSI
source. Nothing here installs anything or opens a window."""

from __future__ import annotations

import ast
import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree

from net_harness import ForwardProxy, https_server, tls_files
from twinlab2_recorder import admin, net, policy, updater
from twinlab2_recorder.client import Portal, PortalError
from twinlab2_recorder.workstation import exclude

ROOT = Path(__file__).resolve().parents[2]


def isolated_policy(test, doc=None, registry=None):
    """The machine policy for one test: a policy.json (or none) and registry values (or none)."""
    work = tempfile.TemporaryDirectory()
    test.addCleanup(work.cleanup)
    path = Path(work.name) / "policy.json"
    if doc is not None:
        path.write_text(json.dumps(doc), encoding="utf-8")
    patches = [mock.patch.dict(os.environ, {"TWINLAB_RECORDER_POLICY": str(path)}),
               mock.patch.object(policy, "read_registry", return_value=dict(registry or {}))]  # fmt: skip
    for p in patches:
        p.start()
        test.addCleanup(p.stop)
    return path


class PolicyTest(unittest.TestCase):
    def test_no_policy_is_unmanaged_with_defaults(self):
        isolated_policy(self)
        pol = policy.load()
        self.assertEqual((pol.managed, pol.proxy, pol.updates, pol.portal_url), (False, "system", "notify", None))

    def test_the_registry_wins_over_the_file_and_both_over_the_person(self):
        isolated_policy(self, {"portal_url": "https://file.example.test/", "proxy": "proxy.file:3128",
                               "updates": "auto", "ca_bundle": "C:/certs/office.pem"},
                        {"PortalUrl": "https://twinlab.corp.example.test", "LockPortal": 1, "Updates": "off",
                         "UpdateSigners": ["ab cd ef"], "ProxyBypass": "*.corp;10.*"})  # fmt: skip
        pol = policy.load()
        self.assertEqual(pol.portal_url, "https://twinlab.corp.example.test")
        self.assertTrue(pol.lock_portal)
        self.assertEqual(pol.updates, "off")
        self.assertEqual(pol.proxy, "http://proxy.file:3128")  # the file's, the registry has none
        self.assertEqual(pol.proxy_bypass, ["*.corp", "10.*"])
        self.assertEqual(pol.update_signers, ["ABCDEF"])
        self.assertEqual(pol.sources["portal_url"], "registry")
        self.assertEqual(pol.sources["proxy"], "file")
        # the person's own proxy and CA do not override the office's
        network = net.Network.resolve("C:/mine.pem", "http://mine:8080", pol)
        self.assertEqual((network.proxy, network.source, network.ca_bundle),
                         ("http://proxy.file:3128", "policy", "C:/certs/office.pem"))  # fmt: skip

    def test_a_locked_portal_refuses_pairing_elsewhere(self):
        isolated_policy(self, registry={"PortalUrl": "https://twinlab.corp.example.test", "LockPortal": "1"})
        pol = policy.load()
        policy.check_portal_allowed(pol, "https://twinlab.corp.example.test/")
        with self.assertRaisesRegex(PortalError, "allows only the portal"):
            policy.check_portal_allowed(pol, "https://elsewhere.example.test")
        unlocked = policy.MachinePolicy.from_values({"portal_url": "https://a.example.test"}, {})
        policy.check_portal_allowed(unlocked, "https://elsewhere.example.test")

    def test_bad_values_are_reported_not_half_applied(self):
        isolated_policy(self, {"updates": "sometimes", "proxy": "nonsense"})
        pol = policy.load()
        self.assertEqual((pol.updates, pol.proxy), ("notify", "system"))
        self.assertEqual(len(pol.errors), 2)
        path = isolated_policy(self)
        path.write_text("{not json", encoding="utf-8")
        pol = policy.load()
        self.assertTrue(pol.managed)
        self.assertIn("cannot be read", pol.errors[0])

    def test_registry_exclude_entries_are_locked_and_added_to_the_file(self):
        path = isolated_policy(self, {"exclude": [{"kind": "process", "pattern": "WINWORD.EXE", "name": "Word"}]},
                               {"Exclude": ["process:OUTLOOK.EXE|Outlook", "web:*.slack.com", "bogus"],
                                "AllowUserExcludeChanges": 0})  # fmt: skip
        with mock.patch.object(exclude, "managed_path", return_value=path):
            got = exclude.read_managed()
            pol = exclude.load({})
        self.assertEqual(sorted(r.pattern for r in got["rules"]), ["*.slack.com", "OUTLOOK.EXE", "WINWORD.EXE"])
        self.assertFalse(got["allow_user_changes"])
        self.assertTrue(all(r.locked for r in pol.rules if r.pattern in ("OUTLOOK.EXE", "*.slack.com")))
        self.assertTrue(pol.process("outlook.exe"))
        self.assertTrue(pol.url("https://team.slack.com/messages"))
        # an explicit path (tests, the session's own copy) reads the file alone
        self.assertEqual([r.pattern for r in exclude.read_managed(path)["rules"]], ["WINWORD.EXE"])


class ProxyChoiceTest(unittest.TestCase):
    def test_proxy_strings(self):
        self.assertEqual(net.pick_proxy("proxy.corp:8080", "https"), "http://proxy.corp:8080")
        self.assertEqual(net.pick_proxy("a:1;b:2", "https"), "http://a:1")
        self.assertEqual(net.pick_proxy("http=h:80;https=s:443", "https"), "http://s:443")
        self.assertEqual(net.pick_proxy("http=h:80;https=s:443", "http"), "http://h:80")
        self.assertEqual(net.pick_proxy("PROXY p1:8080; PROXY p2:8080; DIRECT", "https"), "http://p1:8080")
        self.assertIsNone(net.pick_proxy("DIRECT", "https"))
        self.assertIsNone(net.pick_proxy("SOCKS s:1080", "https"))
        self.assertEqual(net.pick_proxy("SOCKS s:1080; PROXY p:3128", "https"), "http://p:3128")

    def test_bypass_lists(self):
        rules = ["<local>", "*.corp.example.test", "10.*", "192.168.0.0/16", "exact.example.test"]
        for host in ("intranet", "localhost", "a.corp.example.test", "10.1.2.3", "192.168.4.5", "exact.example.test"):
            self.assertTrue(net.bypassed(host, rules), host)
        for host in ("twinlab.example.test", "11.0.0.1", "corp.example.test.evil.test"):
            self.assertFalse(net.bypassed(host, rules), host)

    def resolve(self, network, url, sysp=net.SystemProxy(), pac=None, env=None):
        with mock.patch.object(net, "system_proxy_config", return_value=sysp), \
             mock.patch.object(net, "winhttp_proxy_for_url", return_value=pac) as asked, \
             mock.patch.dict(os.environ, env or {}, clear=False):  # fmt: skip
            for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
                if k.upper() not in {e.upper() for e in env or {}}:  # Windows: names are case-insensitive
                    os.environ.pop(k, None)
            return network.proxy_for(url), asked

    def test_the_system_proxy_pac_and_wininet(self):
        url = "https://twinlab.example.test/api/agent/sessions"
        system = net.Network()
        # PAC (or WPAD): Windows evaluates it, its answer is used
        got, asked = self.resolve(system, url, net.SystemProxy(pac_url="http://wpad.corp/proxy.pac"),
                                  pac="PROXY pac-proxy.corp:8080; DIRECT")  # fmt: skip
        self.assertEqual(got, "http://pac-proxy.corp:8080")
        asked.assert_called_once_with(url, "http://wpad.corp/proxy.pac", False)
        # a fixed WinINET proxy with its bypass list
        fixed = net.SystemProxy(proxy="http=web:80;https=secure:8443", bypass="<local>;*.example.test")
        self.assertIsNone(self.resolve(system, url, fixed)[0])
        self.assertEqual(self.resolve(system, "https://portal.other.test/", fixed)[0], "http://secure:8443")
        # nothing from Windows: the environment, with NO_PROXY
        env = {"HTTPS_PROXY": "http://env-proxy:3128", "NO_PROXY": "internal.test"}
        self.assertEqual(self.resolve(system, url, env=env)[0], "http://env-proxy:3128")
        self.assertIsNone(self.resolve(system, "https://x.internal.test/", env=env)[0])
        self.assertIsNone(self.resolve(system, url)[0])
        # this machine never goes through the system's proxy
        self.assertIsNone(self.resolve(system, "http://127.0.0.1:8080/x", fixed)[0])

    def test_policy_direct_and_explicit_with_bypass(self):
        pol = policy.MachinePolicy.from_values({"proxy": "direct"}, {})
        self.assertIsNone(net.Network.resolve(policy=pol).proxy_for("https://a.test/"))
        pol = policy.MachinePolicy.from_values({"proxy": "http://p.corp:8080", "proxy_bypass": ["*.corp"]}, {})
        network = net.Network.resolve(proxy="direct", policy=pol)
        self.assertEqual(network.proxy_for("https://a.test/"), "http://p.corp:8080")
        self.assertIsNone(network.proxy_for("https://twinlab.corp/"))
        # the person's proxy applies only when the policy leaves it to the system
        mine = net.Network.resolve(proxy="mine.test:3128", policy=policy.MachinePolicy())
        self.assertEqual((mine.proxy_for("https://a.test/"), mine.source), ("http://mine.test:3128", "user"))


class _Json(BaseHTTPRequestHandler):
    answers: list = []
    seen: list = []

    def log_message(self, *args):
        pass

    def _answer(self):
        self.seen.append((self.command, self.path))
        status, headers, body = self.answers.pop(0) if self.answers else (200, {}, {"sessions": [], "deny_keys": []})
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _answer


class TlsAndProxyTest(unittest.TestCase):
    """A portal behind TLS inspection (its certificate from a private root) and an office proxy."""

    def setUp(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is not installed")
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.tls = tls_files(Path(work.name))
        _Json.answers, _Json.seen = [], []
        self.server, port = https_server(_Json, self.tls)
        self.addCleanup(self.server.shutdown)
        self.url = f"https://localhost:{port}"
        isolated_policy(self)

    def test_the_private_root_is_needed_and_enough(self):
        with self.assertRaisesRegex(PortalError, "cannot be reached.*CERTIFICATE_VERIFY_FAILED"):
            Portal(self.url, "t" * 40, attempts=1).sessions()
        self.assertEqual(Portal(self.url, "t" * 40, ca_bundle=str(self.tls["ca"])).sessions()["sessions"], [])

    def test_the_policys_ca_bundle_applies_without_the_person_naming_it(self):
        isolated_policy(self, registry={"CaBundle": str(self.tls["ca"])})
        self.assertEqual(Portal(self.url, "t" * 40).sessions()["sessions"], [])

    def test_https_through_the_office_proxy_with_basic_credentials(self):
        proxy = ForwardProxy(credentials=("svc-recorder", "s3cret"))
        self.addCleanup(proxy.stop)
        isolated_policy(self, registry={"Proxy": f"http://svc-recorder:s3cret@127.0.0.1:{proxy.port}",
                                        "CaBundle": str(self.tls["ca"])})  # fmt: skip
        self.assertEqual(Portal(self.url, "t" * 40).sessions()["sessions"], [])
        self.assertEqual(proxy.seen, [("CONNECT", f"localhost:{self.url.rsplit(':', 1)[1]}")])
        # wrong credentials: the proxy refuses, the recorder says it cannot reach the portal
        isolated_policy(self, registry={"Proxy": f"http://svc-recorder:wrong@127.0.0.1:{proxy.port}",
                                        "CaBundle": str(self.tls["ca"])})  # fmt: skip
        with self.assertRaisesRegex(PortalError, "cannot be reached"):
            Portal(self.url, "t" * 40, attempts=1).sessions()

    def test_retries_with_backoff_and_retry_after(self):
        waits = []
        _Json.answers = [(503, {"Retry-After": "2"}, {"error": "busy"}), (502, {}, {}),
                         (200, {}, {"sessions": [], "deny_keys": []})]  # fmt: skip
        portal = Portal(self.url, "t" * 40, ca_bundle=str(self.tls["ca"]), sleep=waits.append)
        self.assertEqual(portal.sessions()["sessions"], [])
        self.assertEqual(len(_Json.seen), 3)
        self.assertGreaterEqual(waits[0], 2)  # Retry-After honoured
        self.assertLessEqual(waits[1], 1.0)  # then backoff (0.5 * 2**1, full jitter)
        # a refusal is not retried, nor a POST that is not safe to repeat
        _Json.seen.clear()
        _Json.answers = [(403, {}, {"error": "no"})]
        with self.assertRaises(PortalError):
            portal.sessions()
        _Json.answers = [(503, {}, {"error": "busy"})]
        with self.assertRaises(PortalError):
            portal.send({"x": 1})
        self.assertEqual(len(_Json.seen), 2)
        # gives up after `attempts`
        _Json.answers = [(503, {}, {})] * 3
        with self.assertRaisesRegex(PortalError, "503"):
            portal.sessions()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.folder = Path(work.name)

    def test_versions_and_offers(self):
        self.assertTrue(updater.newer("0.10.0", "0.9.9"))
        self.assertFalse(updater.newer("0.2.0", "0.2.0"))
        self.assertFalse(updater.newer("1.0", "0.2.0"))
        good = {"latest": {"version": "9.0.0", "file": "TwinLabRecorder-9.0.0-x64.msi",
                           "url": "/api/agent/update/TwinLabRecorder-9.0.0-x64.msi", "size": 3, "sha256": "a" * 64}}
        self.assertEqual(updater.Offer.from_answer(good).version, "9.0.0")
        self.assertIsNone(updater.Offer.from_answer({"latest": None}))
        for bad in ({"file": "evil.exe"}, {"size": -1}, {"sha256": "nope"}, {"version": "x"}):
            with self.assertRaises(PortalError):
                updater.Offer.from_answer({"latest": {**good["latest"], **bad}})

    def test_off_never_asks(self):
        portal = mock.Mock()
        self.assertIsNone(updater.check(portal, policy.MachinePolicy(updates="off")))
        portal.update_info.assert_not_called()

    def test_download_checks_size_and_sha256(self):
        import hashlib

        data = b"MSI" * 1000
        offer = updater.Offer("9.0.0", "TwinLabRecorder-9.0.0-x64.msi", "/api/agent/update/x", len(data),
                              hashlib.sha256(data).hexdigest())  # fmt: skip
        portal = mock.Mock()
        portal.download.return_value = iter([data[:100], data[100:]])
        path = updater.download(portal, offer, self.folder)
        self.assertEqual(path.read_bytes(), data)
        portal.download.return_value = iter([data[:-1] + b"X"])
        path.unlink()
        with self.assertRaisesRegex(PortalError, "checksum"):
            updater.download(portal, offer, self.folder)
        self.assertEqual(list((self.folder / "updates").iterdir()), [])  # nothing half-downloaded is kept

    def test_signature_rules(self):
        msi = self.folder / "x.msi"
        signed = lambda _p: {"status": "Valid", "thumbprint": "AB12", "subject": "CN=Office"}  # noqa: E731
        unsigned = lambda _p: {"status": "NotSigned", "thumbprint": "", "subject": ""}  # noqa: E731
        tampered = lambda _p: {"status": "HashMismatch", "thumbprint": "AB12", "subject": "CN=Office"}  # noqa: E731
        default = policy.MachinePolicy()
        self.assertIn("signed by CN=Office", updater.verify_signature(msi, default, signed))
        self.assertIn("signed by", updater.verify_signature(msi, policy.MachinePolicy(update_signers=["AB12"]), signed))
        with self.assertRaisesRegex(updater.UpdateRefused, "does not name"):
            updater.verify_signature(msi, policy.MachinePolicy(update_signers=["FFFF"]), signed)
        with self.assertRaisesRegex(updater.UpdateRefused, "not signed"):
            updater.verify_signature(msi, default, unsigned)
        self.assertIn("unsigned", updater.verify_signature(msi, policy.MachinePolicy(allow_unsigned_updates=True),
                                                           unsigned))  # fmt: skip
        with self.assertRaisesRegex(updater.UpdateRefused, "not signed"):  # signers named: unsigned never passes
            updater.verify_signature(msi, policy.MachinePolicy(allow_unsigned_updates=True, update_signers=["AB12"]),
                                     unsigned)  # fmt: skip
        for pol in (default, policy.MachinePolicy(allow_unsigned_updates=True)):
            with self.assertRaisesRegex(updater.UpdateRefused, "HashMismatch"):
                updater.verify_signature(msi, pol, tampered)

    @unittest.skipUnless(sys.platform == "win32", "Windows' Authenticode check")
    def test_windows_says_how_a_real_file_is_signed(self):
        plain = self.folder / "plain.msi"
        plain.write_bytes(b"not an installer")  # Windows cannot even read a signature from it
        self.assertNotEqual(updater.authenticode(plain)["status"], "Valid")
        with self.assertRaisesRegex(updater.UpdateRefused, "not valid|not signed"):
            updater.verify_signature(plain, policy.MachinePolicy(allow_unsigned_updates=True))
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
        if system.is_file():  # signed by Microsoft (catalog-signed counts)
            got = updater.authenticode(system)
            self.assertEqual(got["status"], "Valid")
            self.assertRegex(got["thumbprint"], r"^[0-9A-F]{40}$")

    def test_never_during_a_recording(self):
        msi = self.folder / "TwinLabRecorder-9.0.0-x64.msi"
        msi.write_bytes(b"x")
        ran = []
        updater.take_lock(self.folder)  # this process records
        with self.assertRaisesRegex(updater.UpdateRefused, "recording is running"):
            updater.install(msi, self.folder, True, run=lambda cmd, **kw: ran.append(cmd))
        updater.release_lock(self.folder)
        with self.assertRaisesRegex(updater.UpdateRefused, "recording is running"):
            updater.install(msi, self.folder, True, busy=lambda: True, run=lambda cmd, **kw: ran.append(cmd))
        # a lock left by a process that died is not a recording
        (self.folder / updater.LOCK).write_text(json.dumps({"pid": 2 ** 22 + 12345}), encoding="utf-8")
        self.assertFalse(updater.recording_active(self.folder))
        updater.install(msi, self.folder, True, run=lambda cmd, **kw: ran.append(cmd))
        self.assertEqual(ran[0][:4], ["msiexec", "/i", str(msi), "/qn"])
        self.assertIn("/norestart", ran[0])
        updater.install(msi, self.folder, False, run=lambda cmd, **kw: ran.append(cmd))
        self.assertIn("/passive", ran[1])


class CleanupTest(unittest.TestCase):
    def test_every_profile_and_program_data(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        profiles = [root / "Users" / "ana", root / "Users" / "ben"]
        for p in profiles:
            data = p / "AppData" / "Local" / "TwinLabRecorder" / "recordings" / "s1"
            data.mkdir(parents=True)
            (data / "journal.jsonl").write_text("{}")
            (p / "AppData" / "Local" / "Temp").mkdir(parents=True)
            (p / "AppData" / "Local" / "Temp" / "twinlab-ocr.ps1").write_text("x")
            (p / "AppData" / "Local" / "Other").mkdir()
        program_data = root / "ProgramData"
        (program_data / "TwinLabRecorder").mkdir(parents=True)
        (program_data / "TwinLabRecorder" / "policy.json").write_text("{}")
        got = admin.cleanup(all_users=True, profiles=profiles, program_data=program_data)
        self.assertEqual(got["failed"], {})
        self.assertEqual(len(got["removed"]), 5)
        for p in profiles:
            self.assertEqual([c.name for c in (p / "AppData" / "Local").iterdir()], ["Other", "Temp"])
            self.assertEqual(list((p / "AppData" / "Local" / "Temp").iterdir()), [])
        self.assertFalse((program_data / "TwinLabRecorder").exists())
        self.assertEqual(admin.cleanup(all_users=True, profiles=profiles, program_data=program_data)["removed"], [])

    def test_this_user_only(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        local = Path(work.name)
        (local / "TwinLabRecorder" / "upload-queue").mkdir(parents=True)
        got = admin.cleanup(local=local)
        self.assertIn(str(local / "TwinLabRecorder"), got["removed"])
        self.assertFalse((local / "TwinLabRecorder").exists())


class BuildTest(unittest.TestCase):
    def test_no_excluded_module_is_one_the_recorder_imports(self):
        sys.path.insert(0, str(ROOT / "recorder"))
        import build

        imported = set()
        for folder in (ROOT / "recorder" / "twinlab2_recorder", ROOT / "contract" / "twinlab2_contract"):
            for path in folder.rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        imported |= {a.name for a in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        imported.add(node.module)
        optional = {"PIL", "pytesseract"}  # ocr.py: Tesseract only when installed; the build uses Windows OCR
        for name in build.EXCLUDES:
            clash = {m for m in imported if m == name or m.startswith(name + ".")} - optional
            self.assertEqual(clash, set(), f"{name} is excluded from the build but imported by the recorder")

    def test_signing_is_skipped_without_a_certificate(self):
        sys.path.insert(0, str(ROOT / "recorder"))
        from installer import sign

        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("TWINLAB_SIGN_CERT", "TWINLAB_SIGN_PFX"):
                os.environ.pop(k, None)
            self.assertFalse(sign.sign_files([Path("nothing.exe")]))
        with mock.patch.dict(os.environ, {"TWINLAB_SIGN_CERT": "AB CD", "TWINLAB_SIGN_TIMESTAMP": "http://ts.test"}):
            cmd = sign.command(Path("x.msi"), "signtool")
        self.assertEqual(cmd[:2], ["signtool", "sign"])
        self.assertIn("/sha1", cmd)
        self.assertIn("ABCD", cmd)
        self.assertIn("http://ts.test", cmd)

    def test_the_msi_source(self):
        sys.path.insert(0, str(ROOT / "recorder"))
        from installer import build_msi

        doc = ElementTree.fromstring(build_msi.wxs("0.3.0").encode("utf-8"))
        ns = {"w": "http://wixtoolset.org/schemas/v4/wxs"}
        package = doc.find("w:Package", ns)
        self.assertEqual((package.get("Scope"), package.get("Version"), package.get("UpgradeCode")),
                         ("perMachine", "0.3.0", build_msi.UPGRADE_CODE))  # fmt: skip
        self.assertIsNotNone(package.find("w:MajorUpgrade", ns))
        features = {f.get("Id") for f in package.iter("{%s}Feature" % ns["w"])}
        self.assertEqual(features, {"Core", "WebChannel"})
        # each policy property is remembered across upgrades and written to the policy key
        props = {p.get("Id") for p in package.findall("w:Property", ns)}
        self.assertTrue(set(build_msi.POLICY) <= props)
        names = {r.get("Name") for r in package.iter("{%s}RegistryValue" % ns["w"]) if r.get("Key") == build_msi.POLICY_KEY}
        self.assertEqual(names, set(build_msi.POLICY.values()))
        self.assertTrue(set(build_msi.POLICY.values()) <= set(policy.REGISTRY_VALUES))
        # the uninstall's clean-up: not on upgrade, not with KEEPDATA
        custom = package.find("w:InstallExecuteSequence/w:Custom", ns)
        self.assertEqual(custom.get("Action"), "PurgeData")
        self.assertIn("NOT UPGRADINGPRODUCTCODE", custom.get("Condition"))
        self.assertIn("NOT KEEPDATA", custom.get("Condition"))
        action = package.find("w:CustomAction", ns)
        self.assertEqual((action.get("Impersonate"), action.get("Execute")), ("no", "deferred"))
        self.assertIn("cleanup --all-users", action.get("ExeCommand"))

    @unittest.skipUnless(os.environ.get("TWINLAB_MSI"), "TWINLAB_MSI names a built MSI (CI's recorder-dist job)")
    def test_a_built_msi(self):
        sys.path.insert(0, str(ROOT / "recorder"))
        from installer import build_msi

        got = build_msi.inspect(Path(os.environ["TWINLAB_MSI"]))
        self.assertEqual(got["all_users"], "1")
        self.assertEqual(got["upgrade_code"], build_msi.UPGRADE_CODE)
        self.assertTrue(got["has_exe"] and got["has_node"])
        self.assertEqual(got["policy_registry"], sorted(build_msi.POLICY.values()))
        self.assertIsNotNone(got["purge"])
        for prop in ("KEEPDATA", *build_msi.POLICY):
            self.assertIn(prop, got["secure_properties"])


if __name__ == "__main__":
    unittest.main()
