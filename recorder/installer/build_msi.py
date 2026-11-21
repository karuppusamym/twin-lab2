"""The recorder's per-machine MSI: write the WiX 5 source, build it, sign it, inspect it.

    python recorder/installer/build_msi.py --folder dist/TwinLabRecorder --out dist
    python recorder/installer/build_msi.py --inspect dist/TwinLabRecorder-0.3.0-x64.msi

Building needs the `wix` .NET tool, WiX 5 (`dotnet tool install --global wix --version 5.0.2`;
CI does this on windows-latest, .github/workflows/recorder-dist.yml). `--wxs-only` writes the
source without building (tests read it).

What the package is (docs/recorder-deployment.md has the IT view):

- per machine (`Scope="perMachine"`, ALLUSERS=1) into `%ProgramFiles%\\TwinLab Recorder`, x64;
  a Start-menu shortcut; nothing runs at logon;
- features: `Core` (the recorder: desktop and mainframe channels) and `WebChannel`
  (Playwright's driver, about 90 MB): `ADDLOCAL=Core` installs without the web channel;
- silent: `msiexec /i TwinLabRecorder-<v>-x64.msi /qn /norestart`, and
  `msiexec /x {ProductCode} /qn /norestart`; a newer version replaces an older one
  (MajorUpgrade, same UpgradeCode); a downgrade is refused;
- machine policy from MSI properties (`POLICY`): each one given writes its value under
  `HKLM\\SOFTWARE\\Policies\\TwinLab\\Recorder` (twinlab2_recorder.policy), kept across upgrades
  (read back before the upgrade runs) and removed on uninstall;
- uninstall removes the program, the policy values it wrote, and the data: every user's
  `%LOCALAPPDATA%\\TwinLabRecorder` (settings, paired token, recordings, upload queue, browser
  profile, logs) and `%ProgramData%\\TwinLabRecorder` (TwinLabRecorder.exe cleanup --all-users,
  run as SYSTEM before the files go). `KEEPDATA=1` on the uninstall keeps the data. An upgrade
  never removes it.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parent
RECORDER = HERE.parent
UPGRADE_CODE = "{C93893E8-7F79-4E4F-9A37-332907954EDA}"  # never changes: upgrades find older versions by it
MANUFACTURER = "TwinLab"
PRODUCT = "TwinLab Recorder"
POLICY_KEY = r"SOFTWARE\Policies\TwinLab\Recorder"
# MSI property -> registry value of the machine policy (twinlab2_recorder.policy.REGISTRY_VALUES)
POLICY = {
    "PORTALURL": "PortalUrl",
    "LOCKPORTAL": "LockPortal",
    "PROXY": "Proxy",
    "PROXYBYPASS": "ProxyBypass",
    "CABUNDLE": "CaBundle",
    "UPDATES": "Updates",
    "UPDATESIGNERS": "UpdateSigners",
    "ALLOWUNSIGNEDUPDATES": "AllowUnsignedUpdates",
    "ALLOWUSEREXCLUDECHANGES": "AllowUserExcludeChanges",
}
WEB_DRIVER = r"_internal\playwright\driver"


def version() -> str:
    sys.path.insert(0, str(RECORDER))
    from twinlab2_recorder.settings import VERSION

    if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,5}", VERSION):
        raise SystemExit(f"the MSI needs a numeric version, not {VERSION}")
    return VERSION


def policy_xml() -> str:
    """Per policy property: remember it across upgrades, write it when it is set."""
    out = []
    for prop, name in POLICY.items():
        out.append(f"""
    <Property Id="{prop}" Secure="yes">
      <RegistrySearch Id="Find{prop}" Root="HKLM" Key="{POLICY_KEY}" Name="{name}" Type="raw" Bitness="always64" />
    </Property>
    <SetProperty Id="CMDLINE_{prop}" Value="[{prop}]" Before="AppSearch" Sequence="first" Condition="{prop}" />
    <SetProperty Id="{prop}" Value="[CMDLINE_{prop}]" After="AppSearch" Sequence="first" Condition="CMDLINE_{prop}" />""")
    return "".join(out)


def policy_components() -> str:
    out = []
    for prop, name in POLICY.items():
        out.append(f"""
      <Component Id="Policy{name}" Condition="{prop}" Bitness="always64">
        <RegistryValue Root="HKLM" Key="{POLICY_KEY}" Name="{name}" Type="string" Value="[{prop}]" KeyPath="yes" />
      </Component>""")
    return "".join(out)


def wxs(ver: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Written by recorder/installer/build_msi.py: change that file, not this one. -->
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="{PRODUCT}" Manufacturer="{MANUFACTURER}" Version="{ver}" UpgradeCode="{UPGRADE_CODE}"
           Scope="perMachine" Compressed="yes" InstallerVersion="500" Language="1033">
    <SummaryInformation Description="{PRODUCT} {ver}" Manufacturer="{MANUFACTURER}" />
    <MajorUpgrade DowngradeErrorMessage="A newer version of {PRODUCT} is already installed."
                  Schedule="afterInstallInitialize" />
    <MediaTemplate EmbedCab="yes" CompressionLevel="high" />

    <Property Id="ARPNOMODIFY" Value="0" />
    <Property Id="ARPURLINFOABOUT" Value="https://github.com/karuppusamym/twin-lab2" />
    <Property Id="ARPCOMMENTS" Value="Records web, desktop and mainframe applications for the TwinLab portal." />
    <Property Id="KEEPDATA" Secure="yes" />
{policy_xml()}

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="{PRODUCT}" />
    </StandardDirectory>
    <StandardDirectory Id="ProgramMenuFolder" />

    <Feature Id="Core" Title="{PRODUCT}" Description="The recorder, with its desktop and mainframe channels."
             Level="1" AllowAbsent="no" ConfigurableDirectory="INSTALLFOLDER">
      <ComponentGroupRef Id="CoreFiles" />
      <ComponentGroupRef Id="Policy" />
      <ComponentRef Id="StartMenuShortcut" />
      <ComponentRef Id="InstallInfo" />
      <Feature Id="WebChannel" Title="Web channel" Level="1"
               Description="Records web applications in Edge or Chrome (Playwright's driver).">
        <ComponentGroupRef Id="WebFiles" />
      </Feature>
    </Feature>

    <ComponentGroup Id="CoreFiles" Directory="INSTALLFOLDER">
      <Files Include="!(bindpath.App)\\**">
        <Exclude Files="!(bindpath.App)\\{WEB_DRIVER}\\**" />
      </Files>
    </ComponentGroup>
    <ComponentGroup Id="WebFiles" Directory="INSTALLFOLDER" Subdirectory="{WEB_DRIVER}">
      <Files Include="!(bindpath.App)\\{WEB_DRIVER}\\**" />
    </ComponentGroup>

    <ComponentGroup Id="Policy" Directory="INSTALLFOLDER">{policy_components()}
    </ComponentGroup>

    <Component Id="InstallInfo" Directory="INSTALLFOLDER" Bitness="always64">
      <RegistryValue Root="HKLM" Key="SOFTWARE\\TwinLab\\Recorder" Name="InstallFolder" Type="string"
                     Value="[INSTALLFOLDER]" KeyPath="yes" />
      <RegistryValue Root="HKLM" Key="SOFTWARE\\TwinLab\\Recorder" Name="Version" Type="string" Value="{ver}" />
    </Component>

    <Component Id="StartMenuShortcut" Directory="ProgramMenuFolder">
      <Shortcut Id="RecorderShortcut" Name="{PRODUCT}" Description="Record applications for the TwinLab portal"
                Target="[INSTALLFOLDER]TwinLabRecorder.exe" WorkingDirectory="INSTALLFOLDER" />
      <RegistryValue Root="HKCU" Key="Software\\TwinLab\\Recorder" Name="StartMenuShortcut" Type="integer"
                     Value="1" KeyPath="yes" />
    </Component>

    <!-- uninstall (not an upgrade, not KEEPDATA=1): every user's data and ProgramData, as SYSTEM -->
    <CustomAction Id="PurgeData" Directory="INSTALLFOLDER" Execute="deferred" Impersonate="no" Return="ignore"
                  ExeCommand="&quot;[INSTALLFOLDER]TwinLabRecorder.exe&quot; cleanup --all-users" />
    <InstallExecuteSequence>
      <Custom Action="PurgeData" Before="RemoveFiles"
              Condition="REMOVE~=&quot;ALL&quot; AND NOT UPGRADINGPRODUCTCODE AND NOT KEEPDATA" />
    </InstallExecuteSequence>
  </Package>
</Wix>
"""


def build(folder: Path, out: Path, wxs_only: bool = False) -> Path | None:
    ver = version()
    if not (folder / "TwinLabRecorder.exe").is_file():
        raise SystemExit(f"{folder} has no TwinLabRecorder.exe: run recorder/build.py first")
    work = Path(tempfile.mkdtemp(prefix="twinlab-msi-"))
    source = work / "TwinLabRecorder.wxs"
    source.write_text(wxs(ver), encoding="utf-8")
    if wxs_only:
        shutil.copy(source, out / source.name)
        print(f"wrote {out / source.name}")
        return None
    wix = shutil.which("wix")
    if not wix:
        raise SystemExit("the wix tool (WiX 5) is not on PATH: dotnet tool install --global wix --version 5.0.2")
    msi = out / f"TwinLabRecorder-{ver}-x64.msi"
    subprocess.run([wix, "build", "-arch", "x64", "-bindpath", f"App={folder}", "-o", str(msi), str(source)],
                   check=True)  # fmt: skip
    from installer import sign

    sign.sign_files([msi])
    print(f"built {msi} ({msi.stat().st_size / 1e6:.0f} MB)")
    return msi


# -- inspecting a built MSI without installing it (msi.dll, read-only) ---------------------------------------


def _msi():
    lib = ctypes.windll.msi
    lib.MsiOpenDatabaseW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    lib.MsiDatabaseOpenViewW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    lib.MsiViewExecute.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    lib.MsiViewFetch.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    lib.MsiRecordGetStringW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_wchar_p,
                                        ctypes.POINTER(ctypes.c_ulong)]  # fmt: skip
    lib.MsiRecordGetFieldCount.argtypes = [ctypes.c_ulong]
    lib.MsiCloseHandle.argtypes = [ctypes.c_ulong]
    return lib


def query(msi: Path, sql: str) -> list[list[str]]:
    """Rows of an SQL query on the MSI's tables, as strings (the database opened read-only)."""
    lib = _msi()
    db, view, rec = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
    if lib.MsiOpenDatabaseW(str(msi), ctypes.c_wchar_p(0), ctypes.byref(db)) != 0:  # MSIDBOPEN_READONLY
        raise OSError(f"cannot open {msi}")
    rows = []
    try:
        if lib.MsiDatabaseOpenViewW(db, sql, ctypes.byref(view)) != 0:
            raise OSError(f"bad query: {sql}")
        lib.MsiViewExecute(view, 0)
        while lib.MsiViewFetch(view, ctypes.byref(rec)) == 0:
            row = []
            for i in range(1, lib.MsiRecordGetFieldCount(rec) + 1):
                size = ctypes.c_ulong(0)
                lib.MsiRecordGetStringW(rec, i, ctypes.c_wchar_p(None), ctypes.byref(size))
                buf = ctypes.create_unicode_buffer(size.value + 1)
                size = ctypes.c_ulong(size.value + 1)
                lib.MsiRecordGetStringW(rec, i, buf, ctypes.byref(size))
                row.append(buf.value)
            rows.append(row)
            lib.MsiCloseHandle(rec)
        lib.MsiCloseHandle(view)
    finally:
        lib.MsiCloseHandle(db)
    return rows


def inspect(msi: Path) -> dict:
    """What an IT reviewer asks of a package, read from its tables."""
    props = dict((r[0], r[1]) for r in query(msi, "SELECT `Property`, `Value` FROM `Property`"))
    features = query(msi, "SELECT `Feature`, `Feature_Parent`, `Level` FROM `Feature`")
    files = query(msi, "SELECT `File`, `FileName`, `FileSize` FROM `File`")
    registry = query(msi, "SELECT `Root`, `Key`, `Name` FROM `Registry`")
    actions = query(msi, "SELECT `Action`, `Condition` FROM `InstallExecuteSequence`")
    custom = query(msi, "SELECT `Action`, `Type`, `Target` FROM `CustomAction`")
    names = [r[1].split("|")[-1] for r in files]
    return {
        "product": props.get("ProductName"), "version": props.get("ProductVersion"),
        "product_code": props.get("ProductCode"), "upgrade_code": props.get("UpgradeCode"),
        "all_users": props.get("ALLUSERS"), "secure_properties": props.get("SecureCustomProperties", ""),
        "features": features, "files": len(files), "installed_bytes": sum(int(r[2] or 0) for r in files),
        "has_exe": "TwinLabRecorder.exe" in names, "has_node": "node.exe" in names,
        "policy_registry": sorted(r[2] for r in registry if r[1] == POLICY_KEY),
        "purge": next((r for r in actions if r[0] == "PurgeData"), None),
        "custom_actions": custom, "msi_bytes": msi.stat().st_size,
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folder", type=Path, help="the built TwinLabRecorder folder")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--wxs-only", action="store_true")
    parser.add_argument("--inspect", type=Path, help="print what a built MSI holds (read-only)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(RECORDER))
    if args.inspect:
        import json

        print(json.dumps(inspect(args.inspect), indent=1))
        return 0
    if not args.folder:
        parser.error("--folder is needed to build")
    args.out.mkdir(parents=True, exist_ok=True)
    build(args.folder.resolve(), args.out.resolve(), args.wxs_only)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
