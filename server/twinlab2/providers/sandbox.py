"""What a build runner and the programs it starts may see: an environment built from an allowlist,
a fresh workspace of its own, and the lowest rights this machine can give it.

The environment is never "the worker's minus some names" (a denylist misses every secret it does
not know about: AWS_SECRET_ACCESS_KEY, a database URL under a new name, a cloud SDK's token). It is
built up from nothing: the names below that a Python process, git and the provider SDKs need to
run, then the runner's own non-secret settings, then (only for the phase that needs it) one
credential, put in by the runner itself (`with_credentials`).

Local mode is process separation, not a sandbox: the runner still runs as the worker's OS user
(on POSIX it drops to TWINLAB2_RUNNER_UID/GID when the worker runs as root, and always gets a
private umask and no core dumps). Isolation from the worker's machine is the OpenShift mode's job
(providers/openshift_job.py: a Job per build with no service account token, a read-only root and a
NetworkPolicy). `isolation()` names which one a session got, and the session page shows it.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

# Names a child process may inherit from the worker. Nothing else gets through.
ALLOWED = frozenset({
    # finding programs and the OS itself
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "PROGRAMDATA",
    # locale and time
    "LANG", "LANGUAGE", "TZ",
    # where certificates are (a corporate CA) and how to reach the provider through a proxy
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    # Python itself
    "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
})
ALLOWED_PREFIXES = ("LC_",)
# The runner's own settings: none of them is a secret (keys come on stdin or in a secrets file).
RUNNER_SETTINGS = frozenset({
    "TWINLAB2_CLAUDE_AGENT", "TWINLAB2_COPILOT_AGENT", "TWINLAB2_CLAUDE_TIMEOUT", "TWINLAB2_FAKE_AGENT",
    "TWINLAB2_RUNNER_HEARTBEAT_SECONDS",
})
# What each phase may be given, by secret name -> environment name (the agent SDKs read keys from
# the environment). setup and publish get none: the portal clones and publishes, not the runner.
PHASE_CREDENTIALS = {
    "clarify": {},  # the Messages API key is used inside the runner process, never put in an environment
    "implement": {"anthropic_api_key": "ANTHROPIC_API_KEY", "copilot_token": "COPILOT_GITHUB_TOKEN"},
}


def allowed(name: str) -> bool:
    return name in ALLOWED or name.upper() in ALLOWED or name.startswith(ALLOWED_PREFIXES) or name in RUNNER_SETTINGS


def child_env(parent: dict | None = None, *, home: Path | None = None, temp: Path | None = None,
              pythonpath: list | None = None, extra: dict | None = None) -> dict:
    """The environment of a build runner (and of what it starts): the allowed names of `parent`
    (default os.environ), HOME / USERPROFILE / APPDATA and TEMP / TMP pointing into the session's
    own folder (so no tool reads the worker's ~/.gitconfig, ~/.ssh, ~/.aws or credential stores),
    PYTHONPATH set to `pythonpath` only, then `extra`."""
    parent = os.environ if parent is None else parent
    env = {k: v for k, v in parent.items() if allowed(k)}
    if home is not None:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_CACHE_HOME=str(home / ".cache"), GIT_CONFIG_NOSYSTEM="1")
        if sys.platform == "win32":
            env["HOMEDRIVE"], env["HOMEPATH"] = home.drive or "C:", str(home)[len(home.drive):] or "\\"
    if temp is not None:
        Path(temp).mkdir(parents=True, exist_ok=True)
        env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath)
    env.update(extra or {})
    return env


def with_credentials(env: dict, phase: str, secrets: dict) -> dict:
    """`env` plus the credentials `phase` needs (PHASE_CREDENTIALS) and nothing else."""
    out = {k: v for k, v in env.items() if k not in {n for m in PHASE_CREDENTIALS.values() for n in m.values()}}
    for secret, name in PHASE_CREDENTIALS.get(phase, {}).items():
        if secrets.get(secret):
            out[name] = secrets[secret]
    return out


def fresh_workspace(folder: Path, tag: str) -> Path:
    """The session folder, empty unless it already belongs to this session (tag in state.json or
    in .tag): a folder left by another session or attempt is removed first, never reused."""
    folder = Path(folder)
    mark = folder / ".tag"
    if folder.exists():
        owner = mark.read_text(encoding="utf-8").strip() if mark.exists() else None
        if owner is None and (folder / "state.json").exists():  # a session started before .tag
            import json

            try:
                owner = json.loads((folder / "state.json").read_text(encoding="utf-8")).get("tag")
            except (OSError, ValueError):
                owner = None
        if owner != tag and any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=False)
    folder.mkdir(parents=True, exist_ok=True)
    if not mark.exists():
        mark.write_text(tag, encoding="utf-8")
    if os.name == "posix":
        os.chmod(folder, 0o700)
    return folder


def lower_rights():
    """The runner's first act on POSIX (a preexec function): a private umask, no core dumps (they
    would hold the keys), and the unprivileged user TWINLAB2_RUNNER_UID/GID when started as root."""
    os.umask(0o077)
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    uid, gid = os.environ.get("TWINLAB2_RUNNER_UID"), os.environ.get("TWINLAB2_RUNNER_GID")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and uid:
        if gid:
            os.setgroups([])
            os.setgid(int(gid))
        os.setuid(int(uid))


def popen_options() -> dict:
    """How a runner process is started: on Windows its own process group and a console without a
    window (a DETACHED_PROCESS has no console, so every git it ran opened a terminal window); on
    POSIX a session of its own and lower_rights()."""
    import subprocess

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True, "preexec_fn": lower_rights}


def isolation(mode: str) -> str:
    """The words the session page shows for how its runner was isolated."""
    if mode == "openshift":
        return ("OpenShift Job: own pod, no service account token, read-only root filesystem, "
                "egress limited by NetworkPolicy")
    low = " as an unprivileged user" if os.environ.get("TWINLAB2_RUNNER_UID") and hasattr(os, "geteuid") \
        and os.geteuid() == 0 else ""
    return (f"Local process{low}: allowlisted environment and a fresh workspace, but the same machine "
            "and OS user as the worker (not a sandbox)")
