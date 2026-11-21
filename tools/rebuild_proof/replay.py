"""Step 4 of the proof: run each rebuilt app headlessly and replay the recording against it.

    python tools/rebuild_proof/replay.py web a|c        # web: the portal's own replay, strict
    python tools/rebuild_proof/replay.py tn3270 b|c     # mainframe: the harness's 3270 replay

**Web** goes the way Verify goes: `twinlab2.independent.TwinLabChecks.replay` runs
`python -m twinlab2.replay <recording> --base-url <deployment> --json <report>` (DOM comparison
after every step, then the acceptance cases declared beside the recording), and
`independent.run` turns the reports into per-scenario results from the package's own
`evidence.json`. The judge-side files beside the recording copy (`<evidence>/<case>/judge/`) are
never shown to a builder:

- `bindings.yaml`: the synthetic values the replay types, from drive.VALUES (never from the
  recording); a select is bound by the option text the pack shows, not the source's value;
- `acceptance.yaml`: the business results the owner's scenarios state, judged on the page;
- `replicas.yaml`: source origin -> rebuilt origin. Case C's two web apps share one host, so the
  rebuilt Orders and Billing are put behind one local path proxy (/billing... to Billing, the rest
  to Orders), as a reverse proxy would on the real host;
- `scope.yaml`: the exchange-rate widget's origin (another system, not replicated) is excluded.

**Mainframe**: the portal has no 3270 replay (a gap: Verify can only replay web). The harness
replays the recorded 3270 steps itself: a scripted terminal types the bound values into the
recorded field positions and presses the recorded keys, through the recorder's own TN3270 proxy
(`twinlab2_recorder.workstation.tn3270.Proxy`) in front of the rebuilt host, so the replay's
screens are written by the same code that wrote the recorded ones. Strict comparison per screen
step: the screen's title and every input field (row, column, length, numeric, hidden); text and
data are not compared, as in the web replay. Then acceptance cases on the live screen text.
"""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EVIDENCE, read_json, spawn, wait_port, write_json  # noqa: E402
from drive import VALUES  # noqa: E402

SELECT_LABELS = {"search_by": "Customer name", "product": "Book shelf", "terms": "30 days"}
FIELDS = {  # field (as the bindings template names it) -> the values typed there, in order
    "input#username": ["username"], "input#password": ["password"], "select#searchBy": ["search_by"],
    "input#q": ["search_for"], "input#customerName": ["customer_name"], "input#customerEmail": ["customer_email"],
    "input#customerPhone": ["customer_phone"], "select#product": ["product"], "input#quantity": ["bad_quantity", "quantity"],
    "input#deliveryDate": ["delivery_date"], "textarea#notes": ["notes"],
    'input[name="orderRef"]': ["bad_ref", "@order"], 'input[name="payer"]': ["payer"],
    'select[name="terms"]': ["terms"], 'input[name="amount"]': ["amount"], 'textarea[name="memo"]': ["memo"],
}
# Rebuilt apps: where each listens (BUILD folders are <evidence>/builds/<case>-<slug>)
WEB_APPS = {
    "a": {"orders": {"build": "a-orders", "port": 19201, "source": "http://127.0.0.1:18751"}},
    "c": {"orders": {"build": "c-orders", "port": 19211}, "billing": {"build": "c-billing", "port": 19221}},
    # w: the same two apps on one host as c, recorded in the recorder's own (launched) browser
    "w": {"orders": {"build": "w-orders", "port": 19211}, "billing": {"build": "w-billing", "port": 19221}},
}
C_PROXY = 19260
C_SOURCE = "http://127.0.0.1:18761"
WIDGET = {"a": "http://127.0.0.1:18753", "c": "http://127.0.0.1:18763", "w": "http://127.0.0.1:18763"}
MF = {"b": {"build": "b-account-inquiry", "port": 19231}, "c": {"build": "c-account-inquiry", "port": 19241}}


def recording_folder(case: str) -> Path:
    return next(p for p in (EVIDENCE / case / "recording").iterdir() if p.is_dir())


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- web --------------------------------------------------------------------------------------------


def judge_web(case: str, control: bool = False) -> Path:
    """The judge's copy of the web recording, with bindings/acceptance/replicas/scope beside it."""
    from twinlab2.replay import bindings as bmod
    from twinlab2_contract.evidence import recording as recmod

    src = recording_folder(case)
    rec_id = "ws-" + src.name
    out = EVIDENCE / case / ("judge-control" if control else "judge") / rec_id / "web"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src / "web", out)
    rec = recmod.load(out)
    template = yaml.safe_load(bmod.template(rec))
    driver = read_json(EVIDENCE / case / "record" / "driver.json")
    used: dict[str, int] = {}
    for number, entry in sorted((template.get("steps") or {}).items()):
        field = entry.get("field")
        names = FIELDS.get(field)
        if not names:
            raise SystemExit(f"step {number}: no synthetic value is declared for {field}")
        i = used.get(field, 0)
        used[field] = i + 1
        name = names[min(i, len(names) - 1)]
        entry["value"] = driver["order_number"] if name == "@order" else SELECT_LABELS.get(name, VALUES[name])
        entry.pop("note", None)
    (out / "bindings.yaml").write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    steps = rows(out / "steps.jsonl")

    def goto_after(part, nth=1):
        seen = 0
        for s in steps:
            if s.get("action") == "goto" and part in (s.get("url") or ""):
                seen += 1
                if seen == nth:
                    return s["seq"]
        raise SystemExit(f"case {case}: no page load of {part} (#{nth})")

    def body(case_id, after, text, why):
        return {"id": case_id, "after": after, "category": "behaviour", "required": True, "check": "text",
                "locator": {"css": "body"}, "contains": text, "note": why}

    order = driver["order_number"]
    cases = [
        # the refused sign-in reloads /login: the 2nd load, the 3rd when the session's own browser
        # opened on /login first (case w, launched)
        body("signin-without-password-refused", goto_after("/login", 3 if case == "w" else 2), "Give your user name and your password.",
             "Orders scenario 1, failure case."),
        body("search-pages", _after_search(steps), "Page 1 of 3",
             "Orders scenario 1: 5 rows per page, 'Page 1 of 3'."),
        body("quantity-0-refused", goto_after("/orders/new/items", 2), "The quantity is a number from 1 to 99.",
             "Orders scenario 2, failure case."),
        body("order-number", goto_after("/orders/saved"), order, "Orders scenario 2: the next order number."),
        body("order-total", goto_after("/orders/saved"), "149.00", "Orders scenario 2: 2 x Book shelf at 74.50."),
    ]
    if case in ("c", "w"):
        cases += [
            body("bad-reference-refused", goto_after("/billing/lookup", 2), "An order reference looks like ORD-0000.",
                 "Billing scenario, failure case."),
            body("invoice-number", "end", "INV-5001", "Billing scenario: the first invoice."),
            body("invoice-for-order", "end", order, "Billing scenario: the invoice is for the new order."),
            body("invoice-total-with-vat", "end", "178.80", "Billing scenario: 149.00 plus VAT 20%."),
        ]
    (out / "acceptance.yaml").write_text(yaml.safe_dump({
        "version": 1, "recording": rec_id, "scope": ["the owner's scenarios of this recording"],
        "excluded": ["exchange-rate widget (another system, not replicated)"], "cases": cases},
        sort_keys=False), encoding="utf-8")
    if case == "a":
        src_url = WEB_APPS["a"]["orders"]["source"]
        apps = [{"id": "orders", "source": src_url,
                 "replica": src_url if control else f"http://127.0.0.1:{WEB_APPS['a']['orders']['port']}"}]
    else:
        apps = [{"id": "orders-and-billing", "source": C_SOURCE,
                 "replica": C_SOURCE if control else f"http://127.0.0.1:{C_PROXY}"}]
    (out / "replicas.yaml").write_text(yaml.safe_dump({"apps": apps}, sort_keys=False), encoding="utf-8")
    (out / "scope.yaml").write_text(yaml.safe_dump({
        "version": 1, "recording": rec_id, "approved_by": "rebuild proof (judge side)", "approved_on": time.strftime("%Y-%m-%d"),
        "exclusions": [{"id": "fx-widget", "origin": WIDGET[case],
                        "reason": "exchange-rate widget: another system, not part of any package"}]},
        sort_keys=False), encoding="utf-8")
    print(f"judge {case}: {sum(used.values())} bound values, {len(cases)} acceptance cases")
    return out


def _after_search(steps):
    """The step after which the search results are shown (the submit of the search form)."""
    for s in steps:
        if s.get("action") in ("click", "submit") and (s.get("selector") or "").endswith("#searchBtn"):
            return s["seq"]
    raise SystemExit("no search")


def launch(build: str, port: int, extra: dict | None = None, log: Path | None = None) -> subprocess.Popen:
    """Start a rebuilt app as its builder said: `python run.py` in its repo, PORT set; its data in
    a fresh folder so every replay starts from the seed."""
    folder = EVIDENCE / "builds" / build
    repo = folder / "repo"
    py = folder / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = repo / ".venv" / "Scripts" / "python.exe"
    data = EVIDENCE / "replay-data" / build
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    env = {"PORT": str(port), "HOST": "127.0.0.1",
           "DATABASE_URL": "sqlite:///" + (data / "app.db").as_posix()}
    env.update(extra or {})
    launch_file = folder / "launch.json"  # written by the harness from the builder's own report
    cmd = [str(py) if py.is_file() else sys.executable, "run.py"]
    if launch_file.is_file():
        spec = read_json(launch_file)
        cmd = [c.replace("{python}", str(py) if py.is_file() else sys.executable) for c in spec.get("cmd", cmd)]
        env.update({k: v.replace("{data}", data.as_posix()).replace("{port}", str(port)) for k, v in (spec.get("env") or {}).items()})
    proc = spawn(cmd, log or EVIDENCE / "replay-logs" / f"{build}.log", cwd=repo, extra_env=env)
    wait_port(port, timeout=90)
    return proc


class PathProxy(ThreadingHTTPServer):
    """One host for two rebuilt apps: /billing... to Billing, everything else to Orders."""

    def __init__(self, port: int, orders: int, billing: int) -> None:
        self.routes = (orders, billing)
        super().__init__(("127.0.0.1", port), _ProxyHandler)


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        orders, billing = self.server.routes
        port = billing if self.path.startswith("/billing") else orders
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command in ("POST", "PUT", "PATCH") else None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "keep-alive")}
        conn.request(self.command, self.path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        self.send_response(r.status)
        own = self.server.server_address[1]
        for k, v in r.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            if k.lower() == "location":  # an absolute redirect to the app's own port stays on the host
                v = v.replace(f"127.0.0.1:{port}", f"127.0.0.1:{own}").replace(f"localhost:{port}", f"127.0.0.1:{own}")
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _go


def scenario_results(case: str, report: dict) -> list[dict]:
    """Per app and scenario (each app's own package evidence.json): pass when every linked step that
    the replay ran passed, and the acceptance cases of that app passed."""
    from twinlab2 import independent

    judge = EVIDENCE / case / "judge"
    rec_id = next(judge.iterdir()).name
    seq_of = {s["event_id"]: s["seq"] for s in rows(judge / rec_id / "web" / "steps.jsonl") if s.get("event_id")}
    by_seq = {s["seq"]: s for s in report.get("steps") or []}
    out = []
    for build, app in [("orders", "Orders"), ("billing", "Billing")]:
        evidence = EVIDENCE / "builds" / f"{case}-{build}" / "evidence.json"
        if not evidence.is_file():
            continue
        manifest = read_json(evidence)
        for n, sc in enumerate(manifest.get("scenarios") or [], 1):
            seqs = [seq_of.get(e["event_id"]) for e in sc.get("events") or []]
            steps = [by_seq.get(s) for s in seqs if s in by_seq]
            failed = [s["seq"] for s in steps if s["status"] != "pass"]
            out.append({"app": app, "scenario": sc["name"], "recording": independent.scenario_recordings(
                manifest, f"scenario-{n}"), "linked_steps": len(seqs), "replayed": len(steps),
                "passed": sum(1 for s in steps if s["status"] == "pass"), "failed_steps": failed,
                "result": "pass" if steps and not failed else "fail"})
    return out


def web(case: str) -> int:
    from twinlab2 import independent

    folder = judge_web(case)
    rec_id = folder.parent.name
    procs = []
    proxy = None
    try:
        if case == "a":
            app = WEB_APPS["a"]["orders"]
            procs.append(launch(app["build"], app["port"], {"BILLING_URL": "http://127.0.0.1:18752"}))
            base = f"http://127.0.0.1:{app['port']}"
        else:
            o, b = WEB_APPS[case]["orders"], WEB_APPS[case]["billing"]
            procs.append(launch(o["build"], o["port"], {"BILLING_URL": f"http://127.0.0.1:{C_PROXY}"}))
            procs.append(launch(b["build"], b["port"]))
            proxy = PathProxy(C_PROXY, o["port"], b["port"])
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{C_PROXY}"
        runner = independent.TwinLabChecks(recordings=EVIDENCE / case / "judge", timeout=1800)
        report = runner.replay(rec_id, base)
        out = EVIDENCE / case / "replay"
        write_json(out / "web-fidelity.json", report)
        scen = scenario_results(case, report)
        summary = summarize(report)
        summary["scenarios"] = scen
        write_json(out / "web-summary.json", summary)
        print(json.dumps({k: v for k, v in summary.items() if k != "failed_steps"}, indent=1))
    finally:
        if proxy:
            proxy.shutdown()
        for p in procs:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    return 0


def control_web(case: str) -> int:
    """Positive control: the same judge files and replay against the reference apps themselves
    (replica = source). A failure here is the harness's or the replay's, not a builder's."""
    from twinlab2 import independent
    import record

    folder = judge_web(case, control=True)
    procs = record.start_refapps(case)
    try:
        base = WEB_APPS["a"]["orders"]["source"] if case == "a" else C_SOURCE
        report = independent.TwinLabChecks(recordings=EVIDENCE / case / "judge-control", timeout=1800).replay(
            folder.parent.name, base)
        write_json(EVIDENCE / case / "replay" / "control-web-fidelity.json", report)
        summary = summarize(report)
        write_json(EVIDENCE / case / "replay" / "control-web-summary.json", summary)
        print(json.dumps({k: v for k, v in summary.items() if k != "failed_steps"}, indent=1))
    finally:
        record.stop(procs)
    return 0


def visual_check(case: str, control: bool) -> int:
    """Visual fidelity: every recorded picture compared with the replay's picture of the same step
    (the portal's visual.py through visual_run.py; threshold 2 % of pixels differing by more than 16
    per channel, the recorder's masked pixels and the other system's widget excluded). Not
    required: it is reported as its own dimension and never changes the strict web result."""
    from twinlab2.replay import visual
    import record

    src = judge_web(case, control=control)
    rec_id = src.parent.name
    tag = "control-visual" if control else "visual"
    folder = EVIDENCE / case / f"judge-{tag}" / rec_id / "web"
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(src, folder)
    pictures = visual.pictures_of(folder)
    size = visual.picture_size(folder / pictures[max(pictures)])
    states = [{"id": f"after-step-{seq}", "after": seq, "required": False, "mode": "threshold",
               "max_ratio": 0.02, "tolerance": 16, "exclude": ["iframe#fxWidget"]} for seq in sorted(pictures)]
    (folder / "visual.yaml").write_text(yaml.safe_dump({"version": 1, "recording": rec_id, "viewport": "desktop",
                                                       "fonts": [], "states": states}, sort_keys=False), encoding="utf-8")
    procs, proxy = [], None
    try:
        if control:
            procs = record.start_refapps(case)
            base = WEB_APPS["a"]["orders"]["source"] if case == "a" else C_SOURCE
        elif case == "a":
            app = WEB_APPS["a"]["orders"]
            procs.append(launch(app["build"], app["port"], {"BILLING_URL": "http://127.0.0.1:18752"}))
            base = f"http://127.0.0.1:{app['port']}"
        else:
            o, b = WEB_APPS[case]["orders"], WEB_APPS[case]["billing"]
            procs.append(launch(o["build"], o["port"], {"BILLING_URL": f"http://127.0.0.1:{C_PROXY}"}))
            procs.append(launch(b["build"], b["port"]))
            proxy = PathProxy(C_PROXY, o["port"], b["port"])
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{C_PROXY}"
        out = EVIDENCE / case / "replay"
        report_path = out / f"{tag}-fidelity.json"
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("visual_run.py")), str(folder), base,
                              str(report_path), f"{size[0]}x{size[1]}", str(out / f"{tag}-pictures")],
                             capture_output=True, text=True, timeout=1800)
        if not report_path.is_file():
            raise SystemExit("visual replay wrote no report: " + (run.stderr or run.stdout)[-2000:])
        rep = read_json(report_path)
    finally:
        if proxy:
            proxy.shutdown()
        if control:
            record.stop(procs)
        else:
            for p in procs:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    vis = rep.get("visual") or []
    count = {}
    for v in vis:
        count[v["status"]] = count.get(v["status"], 0) + 1
    ratios = sorted(v.get("ratio") or 0 for v in vis if v.get("compared"))
    summary = {"picture_size": list(size), "states": len(vis), "by_status": count,
               "median_ratio": ratios[len(ratios) // 2] if ratios else None, "max_ratio": ratios[-1] if ratios else None,
               "worst": sorted(({"id": v["id"], "ratio": v.get("ratio"), "status": v["status"], "note": v.get("note")}
                                for v in vis), key=lambda v: -(v["ratio"] or 0))[:8],
               "behaviour_status_of_this_run": rep.get("status")}
    write_json(EVIDENCE / case / "replay" / f"{tag}-summary.json", summary)
    print(json.dumps(summary, indent=1))
    return 0


def summarize(report: dict) -> dict:
    steps = report.get("steps") or []
    count = {}
    for s in steps:
        count[s["status"]] = count.get(s["status"], 0) + 1
    return {"status": report.get("status"), "verdict": report.get("verdict"), "steps": len(steps), "by_status": count,
            "cases": [{k: c.get(k) for k in ("id", "status", "note")} for c in report.get("cases") or []],
            "problems": (report.get("problems") or [])[:15],
            "failed_steps": [{"seq": s["seq"], "action": s["action"], "selector": s.get("selector"), "url": s.get("url"),
                              "status": s["status"], "note": s.get("note"),
                              "differences": [{k: d.get(k) for k in ("element", "check", "expected", "actual")}
                                              for d in (s.get("differences") or [])[:12]]}
                             for s in steps if s["status"] != "pass"]}


# -- mainframe --------------------------------------------------------------------------------------

MF_ACCEPT = [  # (after the n-th Enter/PF key, text the live screen must show, why)
    ("SIGN ON FAILED: CHECK THE PASSWORD", "sign-on scenario: a wrong password is refused"),
    ("ACCOUNT INQUIRY", "sign-on scenario: the right password signs on"),
    ("ANNA SAMPLE", "inquiry scenario: 1000001's name"),
    ("ACCOUNT NOT FOUND", "inquiry scenario: an unknown account"),
    ("OMAR EXAMPLE", "inquiry scenario: 1000002's name"),
    ("SIGNED OFF", "inquiry scenario: PF3 signs off"),
]
MF_ACCEPT_C = [MF_ACCEPT[0], MF_ACCEPT[1], MF_ACCEPT[2], MF_ACCEPT[3], MF_ACCEPT[4], MF_ACCEPT[5]]


def mf_values(case: str) -> dict[str, list[str]]:
    v = VALUES
    return {"USERID": [v["mf_user"]], "PASSWORD": [v["mf_bad_password"], v["mf_password"]],
            "ACCOUNT NO": [v["mf_account"], v["mf_unknown"], v["mf_account2"]]}


def tn3270(case: str, control: bool = False) -> int:
    from twinlab2_recorder.workstation import tn3270 as tmod
    from refapps import load
    from common import DEMO_HOST

    spec = MF[case]
    src = recording_folder(case) / "mainframe"
    recorded = rows(src / "steps.jsonl")
    tag = "control-" if control else ""
    out = EVIDENCE / case / "replay" / f"{tag}mainframe-live"
    if out.exists():
        shutil.rmtree(out)
    if control:  # the reference host itself
        import record
        from common import PORTS

        proc = record.start_refapps("b")[0]
        spec = dict(spec, port=PORTS["tn3270"])
    else:
        proc = launch(spec["build"], spec["port"])
    results, cases = [], []
    try:
        key = recorded[0]["app"]
        proxy = tmod.Proxy("127.0.0.1", spec["port"], out, listen=0, app_key=key)
        proxy.start()
        host = load(DEMO_HOST, "twinlab_demo_host")
        values = mf_values(case)
        used: dict[str, int] = {}
        presses = 0
        term = host.Terminal(proxy.listen)
        for s in recorded:
            r = {"seq": s["seq"], "action": s["action"], "status": "pass", "note": None}
            try:
                if s["action"] == "type":
                    label = (s.get("field") or {}).get("label") or ""
                    i = used.get(label, 0)
                    used[label] = i + 1
                    vals = values.get(label)
                    if not vals:
                        r.update(status="blocked", note=f"no synthetic value for field {label!r}")
                    else:
                        f = s["field"]
                        term.type(f["row"], f["col"], vals[min(i, len(vals) - 1)])
                elif s["action"] == "aid":
                    term.press(s.get("key") or "Enter", expect_screen=True)
                    text = "\n".join(term.text_at(row, 1, 80) for row in range(1, 25))
                    if presses < len(MF_ACCEPT):
                        want, why = MF_ACCEPT[presses]
                        cases.append({"id": f"after-key-{presses + 1}", "contains": want, "note": why,
                                      "status": "PASS" if want in text else "FAIL"})
                    presses += 1
                    time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001 - the step fails, the rest is skipped
                r.update(status="fail", note=f"{type(exc).__name__}: {exc}")
                results.append(r)
                break
            results.append(r)
        try:
            term.close()
        except OSError:
            pass
        time.sleep(1.0)
        proxy.stop()
        proxy.join(5)
        proxy.recorder.close()
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    live = rows(out / "steps.jsonl") if (out / "steps.jsonl").is_file() else []
    rec_screens = [s for s in recorded if s["action"] == "screen"]
    live_screens = [s for s in live if s["action"] == "screen"]
    by_seq = {r["seq"]: r for r in results}

    def inputs(step, folder):
        tree = read_json(folder / step["tree"]) if step.get("tree") else {}
        return [(f["row"], f["col"], f["len"], f["numeric"], f["hidden"]) for f in tree.get("fields") or [] if not f.get("protected")]

    for i, s in enumerate(rec_screens):
        r = by_seq.setdefault(s["seq"], {"seq": s["seq"], "action": "screen", "status": "skipped"})
        if r["status"] != "pass":
            continue
        if i >= len(live_screens):
            r.update(status="fail", note="the rebuilt host sent no screen here")
            continue
        diffs = []
        want_title, got_title = (s.get("screen") or {}).get("title"), (live_screens[i].get("screen") or {}).get("title")
        if want_title != got_title:
            diffs.append({"check": "title", "expected": want_title, "actual": got_title})
        want, got = inputs(s, src), inputs(live_screens[i], out)
        if want != got:
            diffs.append({"check": "input fields (row, col, len, numeric, hidden)", "expected": want, "actual": got})
        if diffs:
            r.update(status="fail", differences=diffs)
    results = sorted(by_seq.values(), key=lambda r: r["seq"])
    status = "pass" if all(r["status"] == "pass" for r in results) and all(c["status"] == "PASS" for c in cases) else "fail"
    count = {}
    for r in results:
        count[r["status"]] = count.get(r["status"], 0) + 1
    summary = {"status": status, "steps": len(results), "by_status": count, "cases": cases,
               "screens": {"recorded": len(rec_screens), "live": len(live_screens)},
               "failed_steps": [r for r in results if r["status"] != "pass"]}
    summary["scenarios"] = mf_scenarios(case, results, cases)
    write_json(EVIDENCE / case / "replay" / f"{tag}mainframe-summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "failed_steps"}, indent=1))
    return 0


def mf_scenarios(case, results, cases):
    evidence = read_json(EVIDENCE / "builds" / MF[case]["build"] / "evidence.json")
    src = recording_folder(case) / "mainframe"
    seq_of = {s["event_id"]: s["seq"] for s in rows(src / "steps.jsonl")}
    by = {r["seq"]: r for r in results}
    out = []
    for n, sc in enumerate(evidence.get("scenarios") or [], 1):
        seqs = [seq_of.get(e["event_id"]) for e in sc.get("events") or []]
        steps = [by[s] for s in seqs if s in by]
        failed = [s["seq"] for s in steps if s["status"] != "pass"]
        out.append({"app": "Account Inquiry", "scenario": sc["name"], "linked_steps": len(seqs), "replayed": len(steps),
                    "passed": len(steps) - len(failed), "failed_steps": failed,
                    "result": "pass" if steps and not failed else "fail"})
    return out


def main(argv) -> int:
    if len(argv) < 3 or argv[1] not in ("web", "tn3270", "judge", "control-web", "control-tn3270", "visual", "control-visual"):
        raise SystemExit(__doc__)
    if argv[1] == "judge":
        judge_web(argv[2])
        return 0
    if argv[1] in ("visual", "control-visual"):
        return visual_check(argv[2], argv[1] == "control-visual")
    if argv[1] == "control-web":
        return control_web(argv[2])
    if argv[1] == "control-tn3270":
        return tn3270(argv[2], control=True)
    return web(argv[2]) if argv[1] == "web" else tn3270(argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
