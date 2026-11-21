# Copied from Twin Lab (https://github.com/karuppusamym/twin-lab) at commit 90bdd91.
# Licensed under the Apache License, Version 2.0 (LICENSE). Changed for TwinLab2: imports
# renamed to this repository's packages, unused paths cut. Source and changes: docs/UPSTREAM.md.
"""Text from a picture, for screens drawn without HTML (a canvas, a chart, an image of a form).

Uses Tesseract when `pytesseract` and the `tesseract` program are installed, otherwise the OCR
engine built into Windows 10 and 11 (Windows.Media.Ocr, through PowerShell; nothing to install),
otherwise nothing: `available()` says which. OCR text can hold data shown on screen, so it stays
in the evidence on this machine like the page snapshots.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

WINDOWS_OCR = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { exit 3 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$result.Lines | ForEach-Object { $_.Text }
"""


def _tesseract() -> bool:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


@lru_cache(maxsize=1)
def available() -> str | None:
    """'tesseract', 'windows' or None."""
    if _tesseract():
        return "tesseract"
    if sys.platform == "win32" and shutil.which("powershell"):
        return "windows"
    return None


def _script() -> Path:
    import tempfile

    path = Path(tempfile.gettempdir()) / "twinlab-ocr.ps1"
    if not path.exists() or path.read_text(encoding="utf-8") != WINDOWS_OCR:
        path.write_text(WINDOWS_OCR, encoding="utf-8")
    return path


def text(image: Path, timeout: float = 30) -> str | None:
    """The text in a PNG or JPEG, one line per line, or None when no OCR engine could read it."""
    engine = available()
    if engine == "tesseract":
        import pytesseract
        from PIL import Image

        try:
            return pytesseract.image_to_string(Image.open(image)).strip()
        except Exception:  # noqa: BLE001
            return None
    if engine == "windows":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(_script()), str(Path(image).resolve())],
                capture_output=True, timeout=timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console window
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.decode("utf-8", errors="replace").strip()
    return None
