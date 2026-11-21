# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Browser-data baselines (replication gap R06, the replay side).

The browser data a replay starts from: localStorage, sessionStorage, cookies and IndexedDB, as
sanitized synthetic fixtures declared in `baseline.yaml` beside the recording (`--baseline`
names another file):

```yaml
version: 1
source_origin: https://todo.example.test   # where the recording was made: rebased to the replica
localStorage:
  tl-todos: [{id: 1, title: Syn alpha, completed: false}]   # not a string: stored as JSON
sessionStorage: {}
cookies:
  - {name: theme, value: syn-dark}          # synthetic; always set for the replica, never a domain
indexedDB:
  - database: app
    version: 1
    store: todos
    keyPath: id
    records: [{id: 1, title: Syn alpha}]
```

Every replay runs in a fresh browser context. The baseline is applied once, before the first
step, on the replica's origin (rebased from `source_origin`): cookies first, then storage and
IndexedDB on a blank page of that origin that never reaches the replica's server. A reload in the
replay therefore keeps what the app stored (refresh persistence), and a fresh context starts from
the baseline again (isolation). An empty file, or none, is an empty baseline.

No source-domain cookie or credential reaches a replica: a cookie entry may not name a domain
(it is always the replica's), a cookie whose name looks like a session or credential
(`session`, `auth`, `token`, `sid`, `jwt`, `csrf`...) is refused unless it says
`synthetic: true`, and the replay sends no cookie from any other source (see fidelity.replay).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

FILE = "baseline.yaml"
BLANK_PATH = "/__twinlab_baseline__"
CREDENTIAL = re.compile(r"sess|auth|token|jwt|sid|csrf|xsrf|login|passw|secret|bearer|saml|oidc",
                        re.I)  # fmt: skip
TODO = "TODO"


@dataclass
class Baseline:
    source_origin: str | None = None
    local: dict[str, str] = field(default_factory=dict)
    session: dict[str, str] = field(default_factory=dict)
    cookies: list[dict] = field(default_factory=list)
    indexeddb: list[dict] = field(default_factory=list)
    path: Path | None = None
    digest: str | None = None
    problems: list[str] = field(default_factory=list)  # any problem blocks the replay

    @property
    def empty(self) -> bool:
        return not (self.local or self.session or self.cookies or self.indexeddb)

    def summary(self) -> dict:
        """What was declared, by name only (keys, cookie names, stores): no values."""
        return {
            "file": self.path.name if self.path else None,
            "digest": self.digest,
            "empty": self.empty,
            "localStorage": sorted(self.local),
            "sessionStorage": sorted(self.session),
            "cookies": sorted(c["name"] for c in self.cookies),
            "indexedDB": [
                f"{d.get('database')}/{d.get('store')}: {len(d.get('records') or [])}"
                for d in self.indexeddb
            ],  # fmt: skip
        }


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _todo(value) -> bool:
    if isinstance(value, str):
        return value.strip().upper() == TODO
    if isinstance(value, list):
        return any(_todo(v) for v in value)
    if isinstance(value, dict):
        return any(_todo(v) for v in value.values())
    return False


def digest(path: Path | None) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12] if path else None
    except OSError:
        return None


def load(path: Path | None) -> Baseline | None:
    """The baseline file, or None when there is none."""
    if path is None or not Path(path).exists():
        return None
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a baseline file")
    b = Baseline(path=Path(path), digest=digest(path))
    b.source_origin = data.get("source_origin")
    for area, target in (("localStorage", b.local), ("sessionStorage", b.session)):
        values = data.get(area) or {}
        if not isinstance(values, dict):
            b.problems.append(f"{area} must be a mapping of key to value")
            continue
        for key, value in values.items():
            if _todo(value):
                b.problems.append(f"{area} {key!r}: the value is still TODO")
            target[str(key)] = _text(value)
    for n, c in enumerate(data.get("cookies") or [], 1):
        if not isinstance(c, dict) or not c.get("name"):
            b.problems.append(f"cookie {n}: needs name and value")
            continue
        name = str(c["name"])
        if c.get("domain") or c.get("url"):
            b.problems.append(f"cookie {name!r} names a domain or URL: baseline cookies are "
                              "always set for the replica, never for another site")  # fmt: skip
            continue
        if CREDENTIAL.search(name) and c.get("synthetic") is not True:
            b.problems.append(f"cookie {name!r} looks like a session or credential: a replay never "
                              "sends one from the source; mark a made-up one synthetic: true")  # fmt: skip
            continue
        if c.get("value") is None or _todo(c.get("value")):
            b.problems.append(f"cookie {name!r}: the value is missing or TODO")
            continue
        cookie = {"name": name, "value": _text(c["value"]), "path": str(c.get("path") or "/")}
        for k in ("httpOnly", "secure"):
            if isinstance(c.get(k), bool):
                cookie[k] = c[k]
        if c.get("sameSite") in ("Strict", "Lax", "None"):
            cookie["sameSite"] = c["sameSite"]
        b.cookies.append(cookie)
    for n, d in enumerate(data.get("indexedDB") or [], 1):
        if not isinstance(d, dict) or not d.get("database") or not d.get("store"):
            b.problems.append(f"indexedDB {n}: needs database and store")
            continue
        if _todo(d.get("records")):
            b.problems.append(f"indexedDB {d['database']}/{d['store']}: records are still TODO")
        b.indexeddb.append({"database": str(d["database"]), "version": int(d.get("version") or 1),
                            "store": str(d["store"]), "keyPath": d.get("keyPath"),
                            "records": list(d.get("records") or [])})  # fmt: skip
    return b


def origin_of(url: str | None) -> str | None:
    if not url:
        return None
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc else None


_APPLY_JS = r"""async ([local, session, idb]) => {
  localStorage.clear(); sessionStorage.clear();
  for (const [k, v] of Object.entries(local)) localStorage.setItem(k, v);
  for (const [k, v] of Object.entries(session)) sessionStorage.setItem(k, v);
  for (const d of idb) {
    await new Promise((done, fail) => {
      const req = indexedDB.open(d.database, d.version);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(d.store)) {
          db.createObjectStore(d.store, d.keyPath ? {keyPath: d.keyPath} : {autoIncrement: true});
        }
      };
      req.onerror = () => fail(req.error && req.error.message);
      req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction(d.store, "readwrite");
        const st = tx.objectStore(d.store);
        for (const r of d.records) st.put(r);
        tx.oncomplete = () => { db.close(); done(); };
        tx.onerror = () => fail(tx.error && tx.error.message);
      };
    });
  }
  return {local: localStorage.length, session: sessionStorage.length};
}"""


def apply(context, page, base: Baseline | None, origin: str | None) -> dict:
    """Put the baseline into a fresh context, on the replica's `origin`, before the first step.
    The blank page it uses is answered here: the replica's server never sees it."""
    if base is None or base.empty or not origin:
        return {"applied": False, "origin": origin}
    if base.cookies:
        # A url gives the replica's host and the cookie's path (Playwright takes one or the other).
        context.add_cookies([{**{k: v for k, v in c.items() if k != "path"},
                              "url": origin + c.get("path", "/")} for c in base.cookies])  # fmt: skip
    if base.local or base.session or base.indexeddb:
        blank = origin + BLANK_PATH

        def answer(route):
            route.fulfill(status=200, content_type="text/html", body="<!DOCTYPE html><title>.")

        page.route(blank, answer)
        try:
            page.goto(blank)
            page.evaluate(_APPLY_JS, [base.local, base.session, base.indexeddb])
        finally:
            page.unroute(blank, answer)
    return {"applied": True, "origin": origin}


def template(rec, keys: dict[str, list[str]] | None = None) -> str:
    """A baseline file for a recording: its source origin and the storage keys it saw (values
    TODO, as synthetic fixtures a person writes). Empty when the recording saw no storage."""
    origin = next((o for o in (origin_of(s.url) for s in rec.steps) if o), None)
    body: dict = {"version": 1, "recording": rec.id, "source_origin": origin}
    for area in ("localStorage", "sessionStorage"):
        body[area] = {k: TODO for k in (keys or {}).get(area, [])}
    body["cookies"] = []
    body["indexedDB"] = []
    head = (
        "# The browser data the replay starts from: synthetic fixtures only, never values or\n"
        "# cookies of the real system. Keys are those the recording saw (when the recorder kept\n"
        "# them); replace each TODO with a made-up value, or delete the key. Empty is an empty\n"
        "# baseline. Cookies name no domain: they are always set for the replica.\n"
    )
    return head + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def write_template(rec, out: Path | None = None) -> Path:
    from twinlab2.replay.acceptance import storage_keys

    out = Path(out) if out else rec.path / FILE
    if out.exists():
        raise FileExistsError(f"{out} exists: it is not overwritten (delete it, or --out)")
    out.write_text(template(rec, storage_keys(rec.path)), encoding="utf-8")
    return out
