"""The application a host process runs: a Java, Electron, Node, Python, .NET, HTA, Windows Script
Host or PowerShell program, or the app a WebView2 serves.

A host executable (`levels.HOSTS`: java.exe, electron.exe, dotnet.exe...) runs many applications
under one name, so its own version resource does not say which application it is. `resolve`
reads what the recorder can read without administrator rights: the process command line, its
current folder (for a relative file), the package of a packaged process, and the real
application file's own metadata (a jar's manifest, an Electron or Node app's `package.json`, a
.NET assembly's version resource, a JNLP file's title and vendor).

The identity key keeps the form `desktop:<company>/<product>` when the application file has that
metadata, else `desktop:<runtime>/<file name>`:

| host                        | application file                               | key without metadata                 |
|-----------------------------|------------------------------------------------|--------------------------------------|
| java.exe, javaw.exe         | `-jar` file (manifest Implementation-Vendor/-Title), `-m` module, main class; Web Start / OpenWebStart / IcedTea-Web: the JNLP (vendor/title) | `desktop:java/billing-client.jar`, `desktop:java/com.acme.Main`, `desktop:java/<codebase host>/<name>.jnlp` |
| electron.exe, or an exe whose version resource is Electron's | the app folder or `.asar`: `package.json` productName (else name) | `desktop:electron/<exe name>` for a renamed exe |
| node.exe                    | the script; `package.json` name for a generic script name (index.js...) | `desktop:node/server-monitor.js` |
| python.exe, pythonw.exe     | `-m` module, or the script; `pyproject.toml` name for a generic name (main.py...) | `desktop:python/ledger_tool.pyw` |
| dotnet.exe                  | the entry `.dll` (its version resource)        | `desktop:dotnet/Billing.Client.dll`  |
| mshta.exe                   | the `.hta` file or address                     | `desktop:mshta/timesheet.hta`        |
| wscript.exe, cscript.exe    | the script                                     | `desktop:wscript/stock-check.vbs`    |
| powershell.exe, pwsh.exe    | the `-File` script                             | `desktop:powershell/intake-form.ps1` |
| msedgewebview2.exe          | the hosting process (its own identity)         | -                                    |
| a packaged process without company/product | its package family name         | `desktop:package/<family name>`      |

Only file, module, class and package names reach the key: never a folder, a user name, or an
argument value. The version is a fact, not identity (a new version is the same application).

A host whose application cannot be found (an inline `-c`/`-e` command, an SDK command such as
`dotnet run`, a REPL, a generic script name without package metadata, a file in Windows'
folder, ApplicationFrameHost itself: the modern apps it frames are identified by their own
process) stays a host: presence only, with `reason` saying why.
"""

from __future__ import annotations

import html
import json
import ntpath
import os
import re
import struct
import zipfile
from collections.abc import Callable
from urllib.parse import urlsplit

# The executables resolved here, by the runtime word their keys use.
RUNTIMES = {
    "java.exe": "java", "javaw.exe": "java", "electron.exe": "electron", "node.exe": "node",
    "python.exe": "python", "pythonw.exe": "python", "dotnet.exe": "dotnet", "mshta.exe": "mshta",
    "wscript.exe": "wscript", "cscript.exe": "wscript", "powershell.exe": "powershell",
    "pwsh.exe": "powershell", "msedgewebview2.exe": "webview2",
}  # fmt: skip
# Script names that say nothing about the application: package metadata names it instead.
GENERIC = {"index", "main", "app", "server", "start", "run", "cli", "__main__", "launch", "init"}
# Main classes that start a Java Web Start application from a JNLP file.
JNLP_MAINS = {
    "com.sun.javaws.main", "net.sourceforge.jnlp.runtime.boot",
    "net.adoptopenjdk.icedteaweb.client.commandline.commandlinelauncher",
    "com.openwebstart.launcher.openwebstartlauncher",
}  # fmt: skip
WINDOWS_DIR = "c:\\windows\\"
MAX_READ = 1 << 20  # a metadata file larger than this is not read


def _clean(text: object, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def split_args(cmdline: str) -> list[str]:
    """A Windows command line split as CommandLineToArgvW does: the program name ends at the next
    quote or space; then 2n backslashes before a quote are n and the quote toggles quoting,
    2n+1 are n and a literal quote, and `""` inside quotes is a literal quote."""
    s = cmdline or ""
    i, n = 0, len(s)
    out: list[str] = []
    if s.startswith('"'):
        end = s.find('"', 1)
        end = n if end < 0 else end
        out.append(s[1:end])
        i = end + 1
    else:
        while i < n and s[i] not in " \t":
            i += 1
        out.append(s[:i])
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            return out
        arg, quoted = [], False
        while i < n and (quoted or s[i] not in " \t"):
            if s[i] == "\\":
                j = i
                while j < n and s[j] == "\\":
                    j += 1
                if j < n and s[j] == '"':
                    arg.append("\\" * ((j - i) // 2))
                    if (j - i) % 2:
                        arg.append('"')
                        i = j + 1
                    else:
                        i = j
                    continue
                arg.append(s[i:j])
                i = j
            elif s[i] == '"':
                if quoted and i + 1 < n and s[i + 1] == '"':
                    arg.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
            else:
                arg.append(s[i])
                i += 1
        out.append("".join(arg))


def file_name(path: str) -> str:
    """The last part of a path or address, without folders (Windows or POSIX separators)."""
    return ntpath.basename(str(path or "").rstrip("\\/"))


def _abs(path: str, cwd: str | None) -> str | None:
    if not path:
        return None
    if os.path.isabs(path) or ntpath.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path):
        return path
    return os.path.join(cwd, path) if cwd else None


def _read(path: str | None, limit: int = MAX_READ) -> bytes | None:
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def _in_windows(path: str | None) -> bool:
    return bool(path) and str(path).lower().replace("/", "\\").startswith(WINDOWS_DIR)


# -- application file metadata -------------------------------------------------------------------


def jar_manifest(path: str | None) -> dict:
    """`{"company", "product", "version"}` from a jar's META-INF/MANIFEST.MF (empty when none)."""
    try:
        if not path or not os.path.isfile(path):
            return {}
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("META-INF/MANIFEST.MF")
            if info.file_size > 65536:
                return {}
            text = z.read(info).decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile, ValueError):
        return {}
    attrs: dict[str, str] = {}
    last = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(" ") and last:
            attrs[last] += line[1:]
        elif ":" in line:
            name, value = line.split(":", 1)
            last = name.strip().lower()
            attrs[last] = value.strip()
    pick = lambda *names: next((_clean(attrs[n]) for n in names if _clean(attrs.get(n))), "")  # noqa: E731
    return {"company": pick("implementation-vendor", "bundle-vendor"),
            "product": pick("implementation-title", "bundle-name"),
            "version": pick("implementation-version", "bundle-version")}  # fmt: skip


def asar_file(path: str, inner: str) -> bytes | None:
    """A file inside an Electron `.asar` archive (a JSON header of offsets, then the files)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
            header_size = struct.unpack_from("<I", head, 4)[0]
            json_len = struct.unpack_from("<I", head, 12)[0]
            if json_len > 64 << 20:
                return None
            node = json.loads(f.read(json_len).decode("utf-8"))
            for part in inner.split("/"):
                node = node["files"][part]
            size = int(node["size"])
            if size > MAX_READ:
                return None
            f.seek(8 + header_size + int(node["offset"]))
            return f.read(size)
    except (OSError, KeyError, ValueError, TypeError, struct.error, UnicodeDecodeError):
        return None


def package_json(raw: bytes | None) -> dict:
    """`{"product", "version"}` of a package.json: its productName, else its name."""
    try:
        doc = json.loads((raw or b"").decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {"product": _clean(doc.get("productName") or doc.get("name")),
            "version": _clean(doc.get("version"), 40)}  # fmt: skip


def pyproject_name(raw: bytes | None) -> str:
    text = (raw or b"").decode("utf-8", "replace")
    m = re.search(r"^\[project\][^\[]*?^name\s*=\s*[\"']([^\"']+)[\"']", text, re.M | re.S)
    return _clean(m.group(1)) if m else ""


def jnlp_facts(raw: bytes | None) -> dict:
    """`{"title", "vendor", "codebase", "href"}` of a JNLP file (read with patterns, not an XML
    parser: the file comes from a server)."""
    text = (raw or b"").decode("utf-8", "replace")
    if "<jnlp" not in text:
        return {}
    tag = re.search(r"<jnlp\b[^>]*>", text, re.S)
    attr = lambda name: (re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag.group(0)) if tag else None)  # noqa: E731
    elem = lambda name: re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", text, re.S)  # noqa: E731
    out = {}
    for key, m in (("title", elem("title")), ("vendor", elem("vendor")),
                   ("codebase", attr("codebase")), ("href", attr("href"))):  # fmt: skip
        if m and _clean(html.unescape(m.group(1))):
            out[key] = _clean(html.unescape(m.group(1)))
    return out


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return f"{host}:{parts.port}" if host and parts.port else host
    except ValueError:
        return ""


def _near(start: str, names: tuple[str, ...], levels: int = 2) -> tuple[str, bytes] | None:
    """The first of `names` in the folder `start` or up to `levels` folders above it."""
    if not start:  # never the recorder's own current folder
        return None
    folder = start
    for _ in range(levels + 1):
        for name in names:
            raw = _read(os.path.join(folder, name))
            if raw is not None:
                return name, raw
        parent = os.path.dirname(folder)
        if not parent or parent == folder:
            break
        folder = parent
    return None


# -- per host ------------------------------------------------------------------------------------


class Unresolved(Exception):
    """The host's application cannot be told: its `args[0]` is the reason."""


def _positional(args: list[str], takes_value: set[str], stop: set[str] = frozenset()) -> tuple[int, str]:
    """(index, value) of the first argument that is not an option; options in `takes_value`
    take the next argument. An option in `stop` ends the search: (-1, that option)."""
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low in stop or low.split("=", 1)[0] in stop:
            return -1, low.split("=", 1)[0]
        if a.startswith("-") and a != "-":
            i += 2 if low in takes_value else 1
            continue
        return i, a
    return -1, ""


JAVA_VALUE = {"-cp", "-classpath", "--class-path", "-p", "--module-path", "--upgrade-module-path",
              "--add-modules", "--limit-modules", "--add-reads", "--add-exports", "--add-opens",
              "--patch-module", "--source", "--enable-native-access"}  # fmt: skip


def _java(args, cwd):
    i = 0
    while i < len(args):
        a, low = args[i], args[i].lower()
        if low == "-jar":
            if i + 1 >= len(args):
                raise Unresolved("java -jar without a file")
            jar = args[i + 1]
            meta = jar_manifest(_abs(jar, cwd))
            return file_name(jar), meta, _abs(jar, cwd)
        if low in ("-m", "--module") or low.startswith("--module="):
            mod = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if not mod:
                raise Unresolved("java -m without a module")
            return mod.split("/", 1)[0], {}, None
        if a.startswith("@"):
            raise Unresolved("java reads its arguments from a file (@argfile)")
        if a.startswith("-"):
            i += 2 if low in JAVA_VALUE else 1
            continue
        main = a
        if main.lower() in JNLP_MAINS:
            return _jnlp(args[i + 1 :], cwd)
        if not re.fullmatch(r"[\w$.]+", main):
            raise Unresolved("java's main class is not a class name")
        return main, {}, None
    raise Unresolved("java without -jar, -m or a main class")


def _jnlp(args, cwd):
    for a in args:
        if a.startswith("-"):
            continue
        if a.lower().startswith(("http://", "https://")):
            host = _host_of(a)
            name = file_name(urlsplit(a).path)
            if host and name:
                return f"{host}/{name}", {}, None
            continue
        path = _abs(a, cwd)
        facts = jnlp_facts(_read(path))
        if not facts:
            continue
        meta = {"company": facts.get("vendor", ""), "product": facts.get("title", "")}
        if meta["company"] and meta["product"]:
            return file_name(facts.get("href") or a), meta, path
        host = _host_of(facts.get("codebase", ""))
        name = file_name(urlsplit(facts.get("href", "")).path) or facts.get("title") or ""
        if host and name:
            return f"{host}/{name}", {"product": facts.get("title", "")}, path
        if facts.get("title"):
            return facts["title"], {"product": facts["title"]}, path
    raise Unresolved("a Java Web Start launcher whose JNLP file could not be read")


def _app_folder(path: str | None) -> tuple[str, dict] | None:
    """(name, package.json facts) of an Electron app given as a folder, an .asar or a script."""
    if not path:
        return None
    if path.lower().endswith(".asar"):
        meta = package_json(asar_file(path, "package.json"))
        return (meta["product"], meta) if meta.get("product") else None
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder:
        return None
    meta = package_json(_read(os.path.join(folder, "package.json")))
    return (meta["product"], meta) if meta.get("product") else None


def _electron(args, cwd, exe_path):
    app_path = next((a.split("=", 1)[1] for a in args if a.lower().startswith("--app-path=")), None)
    if app_path is None:
        _i, app_path = _positional(args, set())
    if app_path:
        if file_name(app_path).lower() == "default_app.asar":
            raise Unresolved("electron without an application (its default app)")
        found = _app_folder(_abs(app_path, cwd))
        if found:
            return found[0], {"product": found[0], "version": found[1].get("version", "")}, _abs(app_path, cwd)
    if exe_path and file_name(exe_path).lower() != "electron.exe":  # a renamed Electron
        base = os.path.join(os.path.dirname(exe_path), "resources")
        for candidate in (os.path.join(base, "app.asar"), os.path.join(base, "app")) if os.path.dirname(exe_path) else ():
            found = _app_folder(candidate)
            if found:
                return found[0], {"product": found[0], "version": found[1].get("version", "")}, exe_path
        return file_name(exe_path), {}, exe_path
    raise Unresolved("electron's application folder or .asar could not be read")


def _script(runtime, path, cwd, meta_names):
    full = _abs(path, cwd)
    name = file_name(path)
    if ntpath.splitext(name)[0].lower() not in GENERIC:
        return name, {}, full
    found = _near(os.path.dirname(full), meta_names) if full else None
    if found:
        meta = package_json(found[1]) if found[0] == "package.json" else {"product": pyproject_name(found[1])}
        if meta.get("product"):
            return meta["product"], {"product": meta["product"], "version": meta.get("version", "")}, full
    raise Unresolved(f"{runtime} runs {name}, a name many applications use, with no package name beside it")


def _node(args, cwd):
    i, script = _positional(args, {"-r", "--require", "--import", "--loader", "--experimental-loader",
                                   "-c", "--conditions", "--inspect-port", "--title"},
                            stop={"-e", "--eval", "-p", "--print", "-i", "--interactive"})  # fmt: skip
    if i < 0:
        raise Unresolved("node runs inline code or a prompt" if script else "node without a script")
    return _script("node", script, cwd, ("package.json",))


def _python(args, cwd):
    i = 0
    while i < len(args):
        a = args[i]
        if re.fullmatch(r"-[A-Za-z]*c", a) and "m" not in a:  # -c, or flags ending in c (-Bc)
            raise Unresolved("python runs an inline command (-c)")
        if a == "-m" or (a.startswith("-m") and len(a) > 2 and not a.startswith("--")):
            mod = a[2:] if len(a) > 2 else (args[i + 1] if i + 1 < len(args) else "")
            if not re.fullmatch(r"[\w.]+", mod or ""):
                raise Unresolved("python -m without a module")
            return mod, {}, None
        if a in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        if a == "-":
            break
        return _script("python", a, cwd, ("pyproject.toml",))
    raise Unresolved("python without a script or module (a prompt)")


def _dotnet(args, cwd, exe_info):
    i = 0
    while i < len(args):
        low = args[i].lower()
        if low in ("--additionalprobingpath", "--additional-deps", "--depsfile", "--runtimeconfig",
                   "--fx-version", "--roll-forward", "--roll-forward-on-no-candidate-fx"):  # fmt: skip
            i += 2
            continue
        if low.startswith("-") or low == "exec":
            i += 1
            continue
        if not low.endswith(".dll"):
            word = args[i] if re.fullmatch(r"[\w-]{1,20}", args[i]) else "with a command"
            raise Unresolved(f"dotnet {word} is an SDK command, not an application (the program it "
                             "starts is identified by its own process)")  # fmt: skip
        dll = _abs(args[i], cwd)
        info = (exe_info(dll) if dll and exe_info else None) or {}
        return file_name(args[i]), {"company": _clean(info.get("company")), "product": _clean(info.get("product")),
                                    "version": _clean(info.get("product_version"), 40)}, dll
    raise Unresolved("dotnet without an application .dll")


def _mshta(args, cwd):
    if not args:
        raise Unresolved("mshta without an .hta file")
    a = args[0]
    if re.match(r"^(vbscript|javascript|about):", a, re.I):
        raise Unresolved("mshta runs inline script")
    if a.lower().startswith(("http://", "https://")):
        host, name = _host_of(a), file_name(urlsplit(a).path)
        if host and name:
            return f"{host}/{name}", {}, None
        raise Unresolved("mshta's address has no file name")
    return file_name(a), {}, _abs(a, cwd)


def _wscript(args, cwd):
    script = next((a for a in args if not a.startswith("//")), None)
    if not script:
        raise Unresolved("wscript without a script")
    return file_name(script), {}, _abs(script, cwd)


def _powershell(args, cwd, exe):
    for i, a in enumerate(args):
        low = a.lower()
        if low in ("-command", "-c", "-encodedcommand", "-enc", "-e", "-ec"):
            raise Unresolved("powershell runs an inline command")
        if low in ("-file", "-f") and i + 1 < len(args):
            return file_name(args[i + 1]), {}, _abs(args[i + 1], cwd)
    if exe == "pwsh.exe":
        i, script = _positional(args, {"-executionpolicy", "-ep", "-workingdirectory", "-wd",
                                       "-windowstyle", "-w", "-configurationname", "-settingsfile"})  # fmt: skip
        if i >= 0 and script.lower().endswith(".ps1"):
            return file_name(script), {}, _abs(script, cwd)
    raise Unresolved("powershell without a -File script")


def electron_default(exe: dict | None) -> bool:
    """An executable whose version resource is Electron's own: a renamed electron.exe."""
    exe = exe or {}
    return _clean(exe.get("product")).lower() == "electron" and _clean(exe.get("company")).lower() in (
        "github, inc.", "electron", "openjs foundation", "")


def resolve(ident: dict, facts: dict, exe_info: Callable[[str], dict] | None = None,
            exe_path: str | None = None) -> dict:
    """The identity of the application a host process runs (see the module's table), from
    `ident` (levels.desktop_identity of the host) and `facts` (`{"argv": [...], "cwd",
    "package"}`, read by desktop.win.host_facts; never kept). A host it cannot tell stays a host
    (`"host": True`) with `"reason"`. A WebView2 is returned with `"via_parent": True`: the
    caller identifies the process that hosts it."""
    exe = str(ident.get("exe") or "").lower()
    runtime = RUNTIMES.get(exe) or ("electron" if ident.get("electron") else None)
    package = _clean(facts.get("package"), 200)
    if runtime is None:
        if not ident.get("host"):  # a program of its own: a packaged one without company/product
            if package and not (ident.get("company") and ident.get("product")):
                return dict(ident, key=f"desktop:package/{package}", package=package)
            return ident
        if exe == "applicationframehost.exe":
            return dict(ident, reason="ApplicationFrameHost frames many modern apps: each one is identified "
                                      "by its own process")  # fmt: skip
        return dict(ident, reason=f"{exe or 'this program'} hosts other programs and its application "
                                  "cannot be told")  # fmt: skip
    if runtime == "webview2":
        return dict(ident, via_parent=True, reason="a WebView2 whose hosting application was not found")
    argv = list(facts.get("argv") or [])
    args, cwd = argv[1:], facts.get("cwd")
    if not argv and not (runtime == "electron" and exe != "electron.exe"):
        return dict(ident, host=True, runtime=runtime, reason=f"{exe}'s command line could not be read")
    try:
        if runtime == "java":
            name, meta, path = _java(args, cwd)
        elif runtime == "electron":
            name, meta, path = _electron(args, cwd, exe_path)
        elif runtime == "node":
            name, meta, path = _node(args, cwd)
        elif runtime == "python":
            name, meta, path = _python(args, cwd)
        elif runtime == "dotnet":
            name, meta, path = _dotnet(args, cwd, exe_info)
        elif runtime == "mshta":
            name, meta, path = _mshta(args, cwd)
        elif runtime == "wscript":
            name, meta, path = _wscript(args, cwd)
        else:
            name, meta, path = _powershell(args, cwd, exe)
    except Unresolved as why:
        return dict(ident, host=True, runtime=runtime, reason=str(why.args[0]))
    if _in_windows(path):
        return dict(ident, host=True, runtime=runtime, reason=f"{exe} runs a file that is part of Windows")
    name = _clean(name, 120)
    company, product = _clean(meta.get("company")), _clean(meta.get("product"))
    if company and product:
        key = f"desktop:{company}/{product}"
    else:
        key = f"desktop:{runtime}/{name}"
    out = dict(ident, key=key, host=False, runtime=runtime, app_file=name, company=company or None,
               product=product or None, version=_clean(meta.get("version"), 40) or None,
               display=product or name, description=None)  # fmt: skip
    out.pop("reason", None)
    out.pop("electron", None)
    return out
