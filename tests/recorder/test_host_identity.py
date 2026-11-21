"""Applications that run inside a host process (java.exe, electron.exe, dotnet.exe, ...) each get an
identity of their own (workstation.hosts): from the command line and the application file's own
metadata, never from a folder, a user name or an argument value. A host whose application cannot
be told stays presence only, with a reason."""

import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from twinlab2_recorder.workstation import hosts, levels, session

JAVA = r"C:\Program Files\Eclipse Adoptium\jdk-21\bin\javaw.exe"


def host(path, exe=None):
    return levels.desktop_identity(path, exe or {"file": os.path.basename(path)})


def write_jar(path, manifest):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)
        z.writestr("com/acme/Main.class", b"\xca\xfe\xba\xbe")


def write_asar(path, files):
    """An Electron archive: a pickled JSON header of offsets, then the files."""
    body, index, offset = b"", {}, 0
    for name, data in files.items():
        index[name] = {"size": len(data), "offset": str(offset)}
        body += data
        offset += len(data)
    header = json.dumps({"files": index}).encode()
    padded = header + b"\0" * (-len(header) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(header)) + padded
    Path(path).write_bytes(struct.pack("<II", 4, len(pickle)) + pickle + body)


class SplitArgsTest(unittest.TestCase):
    def test_windows_quoting(self):
        line = r'"C:\Program Files\Java\bin\javaw.exe" -Dx="a b" -jar "C:\Users\ana\My Apps\billing.jar" \"q\" a\\b'
        self.assertEqual(hosts.split_args(line),
                         [r"C:\Program Files\Java\bin\javaw.exe", "-Dx=a b", "-jar",
                          r"C:\Users\ana\My Apps\billing.jar", '"q"', r"a\\b"])
        self.assertEqual(hosts.split_args(r'node.exe "a\\" b'), ["node.exe", "a\\", "b"])
        self.assertEqual(hosts.split_args(""), [""])


class ResolveTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.dir = Path(work.name)

    def resolve(self, exe_path, argv, cwd=None, exe=None, exe_info=None):
        return hosts.resolve(host(exe_path, exe), {"argv": argv, "cwd": cwd}, exe_info, exe_path)

    def test_a_jar_without_manifest_names_is_java_and_its_file_name(self):
        jar = self.dir / "billing-client.jar"
        write_jar(jar, "Manifest-Version: 1.0\nMain-Class: com.acme.Main\n")
        ident = self.resolve(JAVA, ["javaw.exe", "-Xmx512m", "-Duser.name=ana", "-jar", str(jar), "--user", "ana"])
        self.assertEqual(ident["key"], "desktop:java/billing-client.jar")
        self.assertEqual(ident["display"], "billing-client.jar")
        self.assertFalse(ident["host"])
        self.assertEqual((ident["exe"], ident["runtime"], ident["app_file"]), ("javaw.exe", "java", "billing-client.jar"))
        self.assertNotIn("ana", json.dumps(ident))
        self.assertNotIn(str(self.dir), json.dumps(ident))

    def test_a_jar_manifest_vendor_and_title_are_company_and_product(self):
        jar = self.dir / "ledger-4.2.jar"
        write_jar(jar, "Manifest-Version: 1.0\r\nImplementation-Vendor: Contoso\r\nImplementation-Title: Ledger\r\n"
                       "  Desk\r\nImplementation-Version: 4.2.0\r\n")
        ident = self.resolve(JAVA, ["java.exe", "-jar", "ledger-4.2.jar"], cwd=str(self.dir))  # relative
        self.assertEqual(ident["key"], "desktop:Contoso/Ledger Desk")
        self.assertEqual((ident["display"], ident["version"]), ("Ledger Desk", "4.2.0"))
        # a new version, elsewhere: the same application
        other = self.dir / "v5"
        other.mkdir()
        write_jar(other / "ledger-5.0.jar", "Implementation-Vendor: Contoso\nImplementation-Title: Ledger Desk\n"
                                            "Implementation-Version: 5.0.0\n")
        self.assertEqual(self.resolve(JAVA, ["java.exe", "-jar", str(other / "ledger-5.0.jar")])["key"], ident["key"])

    def test_java_main_class_and_module(self):
        argv = ["java.exe", "-cp", r"C:\Users\ana\lib\*;C:\app\billing.jar", "-Dfoo=bar", "com.acme.billing.Main", "x"]
        self.assertEqual(self.resolve(JAVA, argv)["key"], "desktop:java/com.acme.billing.Main")
        argv = ["java.exe", "--module-path", r"C:\app\mods", "-m", "com.acme.payroll/com.acme.payroll.App"]
        self.assertEqual(self.resolve(JAVA, argv)["key"], "desktop:java/com.acme.payroll")
        self.assertEqual(self.resolve(JAVA, ["java.exe", "--module=org.x.tool/org.x.Main"])["key"],
                         "desktop:java/org.x.tool")

    def test_java_web_start_is_its_jnlp(self):
        jnlp = self.dir / "cache-3fa9c1"  # Web Start keeps the JNLP under a cache name
        jnlp.write_text('<?xml version="1.0"?><jnlp spec="1.0+" codebase="https://apps.example.test/claims" '
                        'href="claims.jnlp"><information><title>Claims &amp; Payments</title>'
                        '<vendor>Fabrikam</vendor></information></jnlp>', encoding="utf-8")
        argv = ["javaw.exe", "-Xbootclasspath/a:x", "com.sun.javaws.Main", "-secure", str(jnlp)]
        self.assertEqual(self.resolve(JAVA, argv)["key"], "desktop:Fabrikam/Claims & Payments")
        jnlp.write_text('<jnlp codebase="https://apps.example.test:8443/hr" href="leave.jnlp"><information>'
                        '</information></jnlp>', encoding="utf-8")
        self.assertEqual(self.resolve(JAVA, argv)["key"], "desktop:java/apps.example.test:8443/leave.jnlp")
        argv = ["javaw.exe", "net.sourceforge.jnlp.runtime.Boot", "-Xnofork",
                "https://apps.example.test/time/timesheet.jnlp?user=ana"]
        self.assertEqual(self.resolve(JAVA, argv)["key"], "desktop:java/apps.example.test/timesheet.jnlp")

    def test_electron_app_folder_asar_and_renamed_exe(self):
        electron = r"C:\tools\electron\electron.exe"
        app = self.dir / "billing-ui"
        app.mkdir()
        (app / "package.json").write_text('{"name": "billing-ui", "productName": "Billing Desk", "version": "1.4.0"}')
        ident = self.resolve(electron, ["electron.exe", "--enable-logging", str(app)])
        self.assertEqual((ident["key"], ident["display"]), ("desktop:electron/Billing Desk", "Billing Desk"))
        asar = self.dir / "app.asar"
        write_asar(asar, {"package.json": b'{"name": "stock-viewer", "version": "2.0.0"}', "main.js": b"//"})
        self.assertEqual(self.resolve(electron, ["electron.exe", str(asar)])["key"], "desktop:electron/stock-viewer")
        # a renamed electron.exe whose version resource is still Electron's
        exe_dir = self.dir / "Kiosk"
        (exe_dir / "resources").mkdir(parents=True)
        write_asar(exe_dir / "resources" / "app.asar", {"package.json": b'{"productName": "Front Desk Kiosk"}'})
        renamed = str(exe_dir / "kiosk.exe")
        electron_info = {"file": "kiosk.exe", "company": "GitHub, Inc.", "product": "Electron", "product_version": "28.1"}
        before = levels.desktop_identity(renamed, electron_info)
        self.assertTrue(before["host"])
        self.assertEqual(hosts.resolve(before, {"argv": []}, None, renamed)["key"], "desktop:electron/Front Desk Kiosk")
        # without an app archive beside it: its own exe name
        bare = str(self.dir / "Notes" / "notes.exe")
        self.assertEqual(self.resolve(bare, ["notes.exe"], exe=dict(electron_info, file="notes.exe"))["key"],
                         "desktop:electron/notes.exe")
        # renderer processes name their app with --app-path
        self.assertEqual(self.resolve(electron, ["electron.exe", "--type=renderer", f"--app-path={app}"])["key"],
                         "desktop:electron/Billing Desk")

    def test_dotnet_entry_dll(self):
        dotnet = r"C:\Program Files\dotnet\dotnet.exe"
        info = {r"C:\apps\Payroll.Client.dll": {"company": "Northwind", "product": "Payroll Client", "product_version": "3.1"}}
        ident = self.resolve(dotnet, ["dotnet.exe", "exec", "--runtimeconfig", "x.json", r"C:\apps\Payroll.Client.dll"],
                             exe_info=lambda p: info.get(p, {}))
        self.assertEqual((ident["key"], ident["display"], ident["version"]),
                         ("desktop:Northwind/Payroll Client", "Payroll Client", "3.1"))
        ident = self.resolve(dotnet, ["dotnet.exe", r"D:\tools\Rates.Editor.dll", "--file", "x"], exe_info=lambda p: {})
        self.assertEqual(ident["key"], "desktop:dotnet/Rates.Editor.dll")

    def test_python_node_scripts_and_generic_names(self):
        py = r"C:\Python313\pythonw.exe"
        self.assertEqual(self.resolve(py, ["pythonw.exe", "-u", r"C:\Users\ana\tools\ledger_tool.pyw"])["key"],
                         "desktop:python/ledger_tool.pyw")
        self.assertEqual(self.resolve(py, ["python.exe", "-X", "utf8", "-m", "acme.rates", "--db", "x"])["key"],
                         "desktop:python/acme.rates")
        proj = self.dir / "intake"
        (proj / "src").mkdir(parents=True)
        (proj / "pyproject.toml").write_text('[project]\nname = "intake-desk"\nversion = "1"\n')
        self.assertEqual(self.resolve(py, ["python.exe", str(proj / "src" / "main.py")])["key"],
                         "desktop:python/intake-desk")
        node = r"C:\Program Files\nodejs\node.exe"
        self.assertEqual(self.resolve(node, ["node.exe", "--max-old-space-size=99", "-r", "dotenv/config",
                                             r"C:\apps\server-monitor.js"])["key"], "desktop:node/server-monitor.js")
        app = self.dir / "dispatch"
        app.mkdir()
        (app / "package.json").write_text('{"name": "dispatch-board"}')
        self.assertEqual(self.resolve(node, ["node.exe", "index.js"], cwd=str(app))["key"], "desktop:node/dispatch-board")

    def test_scripts_of_windows_hosts(self):
        mshta = r"C:\Windows\System32\mshta.exe"
        ident = self.resolve(mshta, ["mshta.exe", r"\\fileserver\share\timesheet.hta", "{1E460BD7-F1C3-4B2E-88BF}"])
        self.assertEqual(ident["key"], "desktop:mshta/timesheet.hta")
        self.assertEqual(levels.Targets.from_list([{"key": ident["key"]}]).level(ident, mshta), levels.FULL)
        self.assertEqual(self.resolve(r"C:\Windows\System32\wscript.exe",
                                      ["wscript.exe", "//nologo", r"D:\scripts\stock-check.vbs", "/user:ana"])["key"],
                         "desktop:wscript/stock-check.vbs")
        self.assertEqual(self.resolve(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                                      ["powershell.exe", "-NoProfile", "-File", r"D:\forms\intake-form.ps1"])["key"],
                         "desktop:powershell/intake-form.ps1")

    def test_hosts_that_cannot_be_told_stay_presence_with_a_reason(self):
        cases = [
            (JAVA, ["javaw.exe", "-version"]),
            (JAVA, ["java.exe", "@C:\\Users\\ana\\args.txt"]),
            (r"C:\Python313\python.exe", ["python.exe", "-c", "import tkinter"]),
            (r"C:\Python313\python.exe", ["python.exe"]),
            (r"C:\Python313\python.exe", ["python.exe", r"C:\x\main.py"]),  # generic, no pyproject
            (r"C:\nodejs\node.exe", ["node.exe", "-e", "require('x')"]),
            (r"C:\nodejs\node.exe", ["node.exe", r"C:\x\index.js"]),
            (r"C:\dotnet\dotnet.exe", ["dotnet.exe", "run", "--project", r"C:\Users\ana\src\App"]),
            (r"C:\Windows\System32\mshta.exe", ["mshta.exe", "vbscript:Close(MsgBox(1))"]),
            (r"C:\Windows\System32\wscript.exe", ["wscript.exe", r"C:\Windows\System32\slmgr.vbs"]),
            (r"C:\tools\electron.exe", ["electron.exe"]),
            (JAVA, []),  # the command line could not be read
            (r"C:\Windows\System32\ApplicationFrameHost.exe", ["ApplicationFrameHost.exe", "-Embedding"]),
            (r"C:\Windows\System32\rundll32.exe", ["rundll32.exe", "shell32.dll,Control_RunDLL"]),
        ]
        targets = levels.Targets.from_list([{"exe": os.path.basename(p).lower()} for p, _ in cases])
        for path, argv in cases:
            ident = self.resolve(path, argv)
            self.assertTrue(ident["host"], argv)
            self.assertTrue(ident.get("reason"), argv)
            self.assertNotIn("ana", ident["reason"])
            self.assertEqual(targets.level(ident, path), levels.PRESENCE, argv)

    def test_a_host_target_records_each_application_as_its_own(self):
        a, b = self.dir / "billing-client.jar", self.dir / "claims-desk.jar"
        write_jar(a, "Main-Class: A\n")
        write_jar(b, "Main-Class: B\n")
        targets = levels.Targets.from_list([{"exe": "javaw.exe"}])
        one = self.resolve(JAVA, ["javaw.exe", "-jar", str(a)])
        two = self.resolve(JAVA, ["javaw.exe", "-jar", str(b)])
        self.assertNotEqual(one["key"], two["key"])
        self.assertEqual((targets.level(one, JAVA), targets.level(two, JAVA)), (levels.FULL, levels.FULL))
        # a target by key records that one only
        only = levels.Targets.from_list([{"key": "desktop:java/claims-desk.jar"}])
        self.assertEqual((only.level(one, JAVA), only.level(two, JAVA)), (levels.PRESENCE, levels.FULL))

    def test_a_packaged_program_without_company_or_product_is_its_package(self):
        ident = levels.desktop_identity(r"C:\Program Files\WindowsApps\Contoso.Rates_1.2.0.0_x64__8wekyb3d8bbwe\App.exe",
                                        {"file": "App.exe"})
        out = hosts.resolve(ident, {"package": "Contoso.Rates_8wekyb3d8bbwe"})
        self.assertEqual(out["key"], "desktop:package/Contoso.Rates_8wekyb3d8bbwe")
        with_resource = levels.desktop_identity("App.exe", {"company": "Contoso", "product": "Rates"})
        self.assertEqual(hosts.resolve(with_resource, {"package": "X_1"})["key"], "desktop:Contoso/Rates")


class FakeWin:
    """desktop.win as session._hosted reads it, for a WebView2 and its hosting application."""

    def __init__(self, procs):
        self.procs = procs  # pid: (path, exe_info, argv, parent)

    def process_path(self, pid):
        return self.procs[pid][0]

    def exe_info(self, path):
        return next((p[1] for p in self.procs.values() if p[0] == path), {})

    def host_facts(self, pid):
        return {"argv": self.procs[pid][2], "cwd": None, "package": None}

    def package_family(self, pid):
        return None

    def parent_pid(self, pid):
        return self.procs[pid][3]


class WebViewTest(unittest.TestCase):
    def identify(self, win, pid):
        path = win.process_path(pid)
        return session._hosted(pid, levels.desktop_identity(path, win.exe_info(path)), path, win)

    def test_a_webview2_is_its_hosting_application(self):
        win = FakeWin({
            10: (r"C:\Program Files\Fabrikam\Dispatch.exe", {"company": "Fabrikam", "product": "Dispatch"}, [], 1),
            11: (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\msedgewebview2.exe",
                 {"company": "Microsoft Corporation", "product": "Microsoft Edge WebView2"},
                 ["msedgewebview2.exe", "--embedded-browser-webview=1", "--webview-exe-name=Dispatch.exe"], 10),
            12: (r"C:\x\msedgewebview2.exe", {}, ["msedgewebview2.exe", "--type=renderer"], 11),
        })  # fmt: skip
        ident, path = self.identify(win, 11)
        self.assertEqual((ident["key"], path), ("desktop:Fabrikam/Dispatch", r"C:\Program Files\Fabrikam\Dispatch.exe"))
        ident, _ = self.identify(win, 12)  # a renderer's parent is the WebView2 itself
        self.assertTrue(ident["host"])
        self.assertIn("WebView2", ident["reason"])


if __name__ == "__main__":
    unittest.main()
