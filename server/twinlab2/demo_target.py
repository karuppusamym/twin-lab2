"""What a demo application runs on, and how it is deployed: the part of a build pack that tells the
builder which database to use, which tables to start from and which deployment files to commit.

- **Data backend**: `sqlite`, `postgresql` or `sqlserver`. The owner chooses it at onboarding
  (`apps.data_backend`), or it is predicted: from the dialect of the application's data model
  (the `.sql` files attached as *Database*), else SQLite (a file the demo resets by replacing,
  no server to provision).
- **Data model**: the tables named by `CREATE TABLE` in those files. The builder creates the
  demo's tables from them; without any, it infers them from the recorded screens and fields.
- **Deployment**: a web (or mainframe) demo is deployed on OpenShift, where pods are set up by
  hand from what is checked in: the pack carries `deploy/ocp/<name>/openshift.yaml` (an
  ImageStream, a BuildConfig that builds the demo repository's Dockerfile in the cluster, a
  Deployment that follows the built image, a Service and a Route) and a README with the `oc`
  commands, for the builder to commit as they are. A desktop demo is built and installed on a
  local machine or VM: no OpenShift files.

Plain functions over a database connection: no web framework.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import yaml

from twinlab2 import artifacts

BACKENDS = {"": "Predict from the data model", "sqlite": "SQLite", "postgresql": "PostgreSQL",
            "sqlserver": "SQL Server"}
ENGINES = ("sqlite", "postgresql", "sqlserver")
DEFAULT = "sqlite"
PORT = 8080
# dialect markers in DDL: each match counts for its engine
DIALECTS = {
    "sqlserver": [r"^\s*GO\s*$", r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", r"\bN?VARCHAR\s*\(\s*MAX\s*\)",
                  r"\bNVARCHAR\b", r"\[dbo\]", r"\bdbo\.", r"\bDATETIME2\b", r"\bUNIQUEIDENTIFIER\b",
                  r"\bNEWID\s*\(\)", r"\bGETDATE\s*\(\)", r"\bSET\s+ANSI_NULLS\b"],
    "postgresql": [r"\bSERIAL\b", r"\bBIGSERIAL\b", r"\bJSONB\b", r"\bBYTEA\b",
                   r"\bTIMESTAMPTZ\b", r"::\s*[a-z]", r"\bCREATE\s+EXTENSION\b", r"\bGENERATED\s+\w+\s+AS\s+IDENTITY\b",
                   r"\bnow\s*\(\)", r"\bCREATE\s+SCHEMA\b"],
    "sqlite": [r"\bAUTOINCREMENT\b", r"^\s*PRAGMA\b", r"\bWITHOUT\s+ROWID\b", r"\bINTEGER\s+PRIMARY\s+KEY\b"],
}
TABLE = re.compile(r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                   r"((?:[\[\"`]?[\w$]+[\]\"`]?\s*\.\s*)?[\[\"`]?[\w$]+[\]\"`]?)", re.I)
NAME = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")


class _Plain(yaml.SafeDumper):
    """YAML without anchors and aliases: each resource reads on its own."""

    def ignore_aliases(self, data):
        return True


# -- the data backend ---------------------------------------------------------------------------


def data_model_rows(db, app_id):
    """The application's data model files: its *Database* attachments with the `.sql` extension."""
    return db.execute("SELECT * FROM attachments WHERE app_id = ? AND kind = 'Database' AND extension = '.sql' "
                      "ORDER BY id", (app_id,)).fetchall()


def ddl_texts(db, app_id):
    out = []
    for row in data_model_rows(db, app_id):
        content = artifacts.content(db, row, "content")
        if content:
            out.append((row, content.decode("utf-8", errors="replace")))
    return out


def dialect(texts):
    """(engine, number of markers) of the dialect the DDL texts are written in, or (None, 0)."""
    scores = {engine: 0 for engine in DIALECTS}
    for text in texts:
        for engine, patterns in DIALECTS.items():
            for pattern in patterns:
                scores[engine] += len(re.findall(pattern, text, re.I | re.M))
    best = max(scores, key=lambda e: (scores[e], -ENGINES.index(e)))
    return (best, scores[best]) if scores[best] else (None, 0)


def tables(texts):
    """The table names `CREATE TABLE` gives in the DDL texts, unquoted, in order, once each."""
    found = {}
    for text in texts:
        for match in TABLE.finditer(text):
            name = re.sub(r"[\[\]\"`\s]", "", match.group(1))
            found.setdefault(name.lower(), name)
    return list(found.values())


def prediction(db, app_id):
    """{"engine", "reason"}: what the data model's dialect suggests, else SQLite."""
    texts = [text for _row, text in ddl_texts(db, app_id)]
    engine, markers = dialect(texts)
    if engine:
        return {"engine": engine, "reason": f"The data model is written for {BACKENDS[engine]} "
                                            f"({markers} dialect marker{'s' if markers != 1 else ''})."}
    if texts:
        return {"engine": DEFAULT, "reason": "The data model is plain SQL: SQLite, the simplest to reset."}
    return {"engine": DEFAULT, "reason": "No data model is attached: SQLite, the simplest to reset."}


def backend(db, app):
    """{"engine", "source": "chosen" | "predicted", "reason"} of an application row."""
    chosen = app["data_backend"] if "data_backend" in app.keys() else ""
    if chosen in ENGINES:
        return {"engine": chosen, "source": "chosen", "reason": "Chosen by the application's owner."}
    return dict(prediction(db, app["id"]), source="predicted")


# -- the deployment -------------------------------------------------------------------------------


def kind(db, app):
    """web, desktop or mainframe: the target platform when set, else the application's surface,
    else that of its first identity."""
    app = db.execute("SELECT * FROM apps WHERE id = ?", (app["id"],)).fetchone() or app
    platform = app["target_platform"] if "target_platform" in app.keys() else ""
    if platform == "windows-desktop":
        return "desktop"
    if platform == "web":
        return "web"
    from twinlab2.recording_export import kind_of  # it imports buildpack, which imports this module

    return kind_of(db, app)


def resource_name(app):
    """A lowercase DNS label for the OpenShift resources: the application's name in ASCII
    letters, digits and hyphens with its id, else `demo-<id>`."""
    base = re.sub(r"[^a-z0-9]+", "-", str(app["name"] or "").lower()).strip("-")[:50].strip("-")
    name = f"{base}-{app['id']}" if base and base[0].isalpha() else f"demo-{app['id']}"
    return name if NAME.fullmatch(name) else f"demo-{app['id']}"


def public_git_url(url):
    """The demo repository's address without credentials, query or fragment."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("https", "http", "ssh", "git"):
        return ""
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def openshift(app, engine, version_path, git_url=None):
    """The OpenShift resources of a web demo, built in the cluster from its repository."""
    name = resource_name(app)
    repo = public_git_url(app["git_url"] if git_url is None else git_url)
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "twinlab2-demos"}
    env = [{"name": "PORT", "value": str(PORT)}, {"name": "DATA_BACKEND", "value": engine}]
    volumes, mounts = [], []
    if engine == "sqlite":
        # the file lives on the pod's own disk: a restart resets the demo to its seed data
        env.append({"name": "DATABASE_URL", "value": "sqlite:////data/demo.sqlite"})
        volumes.append({"name": "data", "emptyDir": {}})
        mounts.append({"name": "data", "mountPath": "/data"})
    else:
        # the database server and its Secret are set up by hand; no credential is in the pack
        env.append({"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": f"{name}-db", "key": "url"}}})
    container = {"name": name, "image": f"{name}:latest", "ports": [{"name": "http", "containerPort": PORT}],
                 "env": env, "readinessProbe": {"httpGet": {"path": "/" + version_path.lstrip("/"), "port": "http"}}}
    if mounts:
        container["volumeMounts"] = mounts
    pod = {"containers": [container]}
    if volumes:
        pod["volumes"] = volumes
    trigger = [{"from": {"kind": "ImageStreamTag", "name": f"{name}:latest"},
                "fieldPath": f'spec.template.spec.containers[?(@.name=="{name}")].image'}]
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "image.openshift.io/v1", "kind": "ImageStream", "metadata": {"name": name, "labels": labels}},
        {"apiVersion": "build.openshift.io/v1", "kind": "BuildConfig", "metadata": {"name": name, "labels": labels},
         "spec": {"source": {"type": "Git", "git": {"uri": repo or "REPLACE-WITH-THE-DEMO-REPOSITORY", "ref": "main"}},
                  "strategy": {"type": "Docker", "dockerStrategy": {"dockerfilePath": "Dockerfile"}},
                  "output": {"to": {"kind": "ImageStreamTag", "name": f"{name}:latest"}},
                  "triggers": [{"type": "ConfigChange"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": name, "labels": labels,
                      "annotations": {"image.openshift.io/triggers": yaml.safe_dump(trigger, default_flow_style=True,
                                                                                    width=1000).strip()}},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": name},
                  "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]}},
        {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": {"name": name, "labels": labels},
         "spec": {"to": {"kind": "Service", "name": name}, "port": {"targetPort": "http"},
                  "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}}},
    ]}


def openshift_readme(app, engine, repo):
    name = resource_name(app)
    lines = [f"# Deploying {name} on OpenShift", "",
             "Pods are set up by hand from what is checked in. From a checkout of this repository, logged in "
             "to the project the demo runs in:", "",
             "```", f"oc apply -f deploy/ocp/{name}/openshift.yaml", f"oc start-build {name} --follow",
             f"oc rollout status deployment/{name}", f"oc get route {name}", "```", "",
             f"The BuildConfig builds `Dockerfile` at the root of {('`' + repo + '`') if repo else 'the demo repository'}"
             " (branch `main`) inside the cluster; the Deployment follows the image it builds. The container listens "
             f"on port {PORT} (`PORT`) and answers the readiness probe at the version endpoint.", ""]
    if engine == "sqlite":
        lines += ["Data: SQLite at `/data/demo.sqlite` on the pod's own disk, created and seeded at start: "
                  "restarting the pod resets the demo.", ""]
    else:
        lines += [f"Data: {BACKENDS[engine]}. Before the first rollout, create the database and a Secret "
                  f"`{name}-db` with the key `url` (the connection URL), by hand:", "",
                  "```", f"oc create secret generic {name}-db --from-literal=url='<connection URL>'", "```", "",
                  "The demo creates its tables and synthetic seed data at start when they are missing.", ""]
    return "\n".join(lines)


def plan(db, app, version_path, included=None):
    """The pack's `target` section and its files: {"kind", "data_backend", "data_model",
    "deploy"}, {path: bytes}. `included`: {attachment path: the bytes the pack carries}; the data
    model lists only those files and the tables in them (an attachment not yet reviewed adds
    nothing but its dialect to the prediction)."""
    surface = kind(db, app)
    chosen = backend(db, app)
    included = included or {}
    model_files = [p for p in (f"attachments/{row['id']}{row['extension']}" for row in data_model_rows(db, app["id"]))
                   if p in included]
    data_model = {"files": model_files,
                  "tables": tables(included[p].decode("utf-8", errors="replace") for p in model_files)}
    files = {}
    if surface == "desktop":
        deploy = {"kind": "local", "notes": "Build an installer or a portable folder and install it on a local "
                                            "machine or VM; no OpenShift resources."}
    else:
        name = resource_name(app)
        repo = public_git_url(app["git_url"])
        base = f"deploy/ocp/{name}"
        manifest = yaml.dump(openshift(app, chosen["engine"], version_path), Dumper=_Plain, sort_keys=False,
                             allow_unicode=True)
        files[f"{base}/openshift.yaml"] = manifest.encode("utf-8")
        files[f"{base}/README.md"] = openshift_readme(app, chosen["engine"], repo).encode("utf-8")
        deploy = {"kind": "openshift", "name": name, "files": sorted(files), "repository": repo or None,
                  "port": PORT}
    out = {"kind": surface, "data_backend": chosen, "data_model": data_model, "deploy": deploy}
    if surface != "desktop":  # what the release checks read the deployed commit from
        out["version_endpoint"] = "/" + version_path.lstrip("/")
    return out, files


def markdown(target):
    """The BUILD_TASK.md sections for the pack's `target`."""
    b, model, deploy = target["data_backend"], target["data_model"], target["deploy"]
    out = ["## Data", "",
           f"Backend: **{BACKENDS[b['engine']]}** ({b['source']}: {b['reason']}). The demo reads its connection "
           "from `DATABASE_URL` and creates its tables and synthetic seed data at start when they are missing; "
           "a reset brings back exactly that seed.", ""]
    if model["files"] or model["tables"]:
        if model["files"]:
            out += ["Data model: " + ", ".join(f"`{p}`" for p in model["files"]) + "."]
        if model["tables"]:
            out += ["Tables to create: " + ", ".join(f"`{t}`" for t in model["tables"]) + "."]
        out += ["Keep their table and column names; adapt the DDL to the backend above when it was written "
                "for another one.", ""]
    else:
        out += ["No data model is attached: infer the tables from the recorded screens, fields and scenarios, "
                "and list them in BUILD-NOTES.md.", ""]
    out += ["## Deployment", ""]
    if deploy["kind"] == "openshift":
        out += [f"OpenShift, set up by hand from what is checked in. Commit `deploy/ocp/{deploy['name']}/` as it "
                "is in this pack, and a `Dockerfile` at the repository root that serves the demo on port "
                f"{deploy['port']} (`PORT`). See `deploy/ocp/{deploy['name']}/README.md`.", ""]
        if not deploy["repository"]:
            out += ["The demo repository is not set yet: replace `REPLACE-WITH-THE-DEMO-REPOSITORY` in "
                    "`openshift.yaml` with its Git address.", ""]
    else:
        out += [deploy["notes"], ""]
    return out
