"""Workspace-confined native LTspice schematic capture for System Builder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, TypedDict

from ltspice_wrapper import LTSPICE


CAPTURE_SCHEMA_VERSION = 1
CAPTURE_VERSION = "native-window-v1"
MAX_SCHEMATIC_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SCHEMATIC_IMAGE_BYTES = 32 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class CaptureResult(TypedDict):
    source_path: str
    source_sha256: str
    schematic_path: str
    capture_method: str
    captured_at: str
    width: int
    height: int


NativeCapture = Callable[[Path, Path, Path], str]


def _portable_path(relative_path: object, suffixes: set[str], label: str) -> PurePosixPath:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
    ):
        raise ValueError(f"{label} must be a non-empty workspace-relative path")
    portable = PurePosixPath(relative_path)
    if portable.is_absolute() or ".." in portable.parts:
        raise ValueError(f"{label} must remain inside the selected workspace")
    if portable.suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise ValueError(f"{label} must use one of: {allowed}")
    return portable


def resolve_workspace_file(
    workspace_root: Path,
    relative_path: object,
    suffixes: set[str],
    *,
    label: str,
    maximum_bytes: int,
) -> Path:
    """Resolve one bounded regular workspace file without following symlinks."""
    portable = _portable_path(relative_path, suffixes, label)
    root = workspace_root.resolve(strict=True)
    cursor = root
    for part in portable.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{label} must not traverse a symbolic link")
    try:
        path = root.joinpath(*portable.parts).resolve(strict=True)
        path.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError(f"{label} was not found inside the selected workspace") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must identify a regular file")
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    return path


def resolve_schematic_image(workspace_root: Path, relative_path: object) -> Path:
    return resolve_workspace_file(
        workspace_root,
        relative_path,
        ALLOWED_IMAGE_SUFFIXES,
        label="schematic image",
        maximum_bytes=MAX_SCHEMATIC_IMAGE_BYTES,
    )


def _managed_asset_directory(workspace_root: Path) -> Path:
    root = workspace_root.resolve(strict=True)
    runs = root / "runs"
    assets = runs / "system-builder-assets"
    for path, label in ((runs, "runs"), (assets, "schematic asset directory")):
        if path.is_symlink():
            raise ValueError(f"{label} must not be a symbolic link")
        path.mkdir(exist_ok=True)
        if not path.is_dir():
            raise ValueError(f"{label} must be a directory")
    return assets


def _png_dimensions(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("LTspice capture did not produce a regular PNG file")
    size = path.stat().st_size
    if not 24 <= size <= MAX_SCHEMATIC_IMAGE_BYTES:
        raise RuntimeError("LTspice capture produced an invalid-sized PNG file")
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RuntimeError("LTspice capture did not produce a valid PNG header")
    width, height = struct.unpack(">II", header[16:24])
    if width < 320 or height < 200 or width > 32_768 or height > 32_768:
        raise RuntimeError("LTspice capture dimensions are outside the safe range")
    return width, height


def _capture_macos(source: Path, output: Path, executable: Path) -> str:
    if not executable.is_file():
        raise FileNotFoundError(f"LTspice executable not found: {executable}")
    launched = subprocess.Popen(
        [str(executable), str(source)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # The process appears before LaunchServices registers the application as
    # scriptable. Activating it immediately can therefore return error -600.
    time.sleep(1.0)
    script = r'''
on run argv
set targetPID to item 1 of argv as integer
set expectedWindowName to item 2 of argv
tell application "System Events"
    set targetProcess to missing value
    repeat 80 times
        repeat with candidateProcess in every process
            if (unix id of candidateProcess) is targetPID then
                set targetProcess to candidateProcess
                exit repeat
            end if
        end repeat
        if targetProcess is not missing value then exit repeat
        delay 0.1
    end repeat
    if targetProcess is missing value then error "LTspice process is unavailable"
    tell targetProcess
        set frontmost to true
        repeat 80 times
            if exists front window then exit repeat
            delay 0.1
        end repeat
        if not (exists front window) then error "LTspice did not open a schematic window"
        if (name of front window) does not contain expectedWindowName then error "LTspice opened the wrong schematic window"
        keystroke " "
        delay 0.8
        set windowPosition to position of front window
        set windowSize to size of front window
        return (item 1 of windowPosition as text) & "," & (item 2 of windowPosition as text) & "," & (item 1 of windowSize as text) & "," & (item 2 of windowSize as text)
    end tell
end tell
end run
'''
    completed = subprocess.run(
        ["osascript", "-e", script, str(launched.pid), source.name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "LTspice window control failed; allow Accessibility access for the "
            f"terminal application and retry. {detail}".strip()
        )
    bounds = completed.stdout.strip().replace(" ", "")
    parts = bounds.split(",")
    if len(parts) != 4 or any(not part.lstrip("-").isdigit() for part in parts):
        raise RuntimeError("LTspice returned invalid window bounds")
    capture = subprocess.run(
        ["/usr/sbin/screencapture", "-x", f"-R{bounds}", str(output)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if capture.returncode != 0:
        detail = capture.stderr.strip() or capture.stdout.strip()
        raise RuntimeError(
            "LTspice screen capture failed; allow Screen Recording access for the "
            f"terminal application and retry. {detail}".strip()
        )
    return "macos-ltspice-window"


_WINDOWS_CAPTURE_SCRIPT = r'''
param([string]$Executable, [string]$Source, [string]$Output)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class NativeWindow {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
}
"@
$launched = Start-Process -FilePath $Executable -ArgumentList ('"' + $Source + '"') -PassThru
$deadline = (Get-Date).AddSeconds(15)
$process = $launched
while ((Get-Date) -lt $deadline) {
  $process.Refresh()
  if ($process.MainWindowHandle -ne 0) { break }
  $candidate = Get-Process -Name "LTspice", "XVIIx64", "scad3" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if ($candidate) { $process = $candidate; break }
  Start-Sleep -Milliseconds 150
}
if ($process.MainWindowHandle -eq 0) { throw "LTspice did not open a schematic window" }
if ($process.MainWindowTitle -notlike ("*" + [IO.Path]::GetFileNameWithoutExtension($Source) + "*")) {
  throw "LTspice opened the wrong schematic window: $($process.MainWindowTitle)"
}
[NativeWindow]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
[NativeWindow]::ShowWindow($process.MainWindowHandle, 3) | Out-Null
Start-Sleep -Milliseconds 500
(New-Object -ComObject WScript.Shell).SendKeys(" ")
Start-Sleep -Milliseconds 900
$rect = New-Object NativeWindow+RECT
if (-not [NativeWindow]::GetWindowRect($process.MainWindowHandle, [ref]$rect)) { throw "LTspice window bounds are unavailable" }
$scale = [NativeWindow]::GetDpiForWindow($process.MainWindowHandle) / 96.0
$left = [int][Math]::Round($rect.Left * $scale)
$top = [int][Math]::Round($rect.Top * $scale)
$width = [int][Math]::Round(($rect.Right - $rect.Left) * $scale)
$height = [int][Math]::Round(($rect.Bottom - $rect.Top) * $scale)
if ($width -lt 320 -or $height -lt 200) { throw "LTspice window is too small to capture" }
$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)
  $bitmap.Save($Output, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
  $graphics.Dispose()
  $bitmap.Dispose()
}
'''


def _capture_windows(source: Path, output: Path, executable: Path) -> str:
    if not executable.is_file():
        raise FileNotFoundError(f"LTspice executable not found: {executable}")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise FileNotFoundError("PowerShell is required for LTspice window capture")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", encoding="utf-8", delete=False
    ) as handle:
        handle.write(_WINDOWS_CAPTURE_SCRIPT)
        script_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                str(executable),
                str(source),
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"LTspice Windows capture failed. {detail}".strip())
    return "windows-ltspice-window"


def _native_capture_for_platform(platform_name: str) -> NativeCapture:
    if platform_name == "darwin":
        return _capture_macos
    if platform_name == "win32":
        return _capture_windows
    raise RuntimeError("native LTspice schematic capture supports macOS and Windows")


def capture_schematic(
    workspace_root: Path,
    source_path: object,
    *,
    platform_name: str = sys.platform,
    executable: Path = LTSPICE,
    native_capture: NativeCapture | None = None,
) -> CaptureResult:
    """Capture a workspace `.asc` into a bounded, content-addressed PNG asset."""
    source = resolve_workspace_file(
        workspace_root,
        source_path,
        {".asc"},
        label="schematic source",
        maximum_bytes=MAX_SCHEMATIC_SOURCE_BYTES,
    )
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    assets = _managed_asset_directory(workspace_root)
    stem = f"schematic-{source_sha256[:20]}-{CAPTURE_VERSION}"
    destination = assets / f"{stem}.png"
    metadata_path = assets / f"{stem}.json"
    # macOS `screencapture` silently declines dot-prefixed output names while
    # still returning zero, so the validated temporary PNG must be visible.
    temporary = assets / f"capture-temp-{os.getpid()}-{time.time_ns()}.png"
    capture = native_capture or _native_capture_for_platform(platform_name)
    try:
        method = capture(source, temporary, executable)
        width, height = _png_dimensions(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    relative_source = source.relative_to(workspace_root.resolve(strict=True)).as_posix()
    relative_image = destination.relative_to(workspace_root.resolve(strict=True)).as_posix()
    result: CaptureResult = {
        "source_path": relative_source,
        "source_sha256": source_sha256,
        "schematic_path": relative_image,
        "capture_method": method,
        "captured_at": datetime.now(UTC).isoformat(),
        "width": width,
        "height": height,
    }
    metadata = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        **result,
        "capture_version": CAPTURE_VERSION,
        "ltspice_executable": str(executable),
    }
    temporary_metadata = metadata_path.with_name(f".{metadata_path.name}.tmp")
    temporary_metadata.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary_metadata, metadata_path)
    return result


def list_schematic_files(
    workspace_root: Path,
    *,
    maximum: int = 250,
) -> dict[str, list[str]]:
    """List bounded workspace schematic sources and images without following links."""
    if not 1 <= maximum <= 1_000:
        raise ValueError("schematic file limit must be between 1 and 1,000")
    root = workspace_root.resolve(strict=True)
    sources: list[str] = []
    images: list[str] = []
    skipped_directories = {".git", ".venv", "__pycache__", "node_modules", "runs"}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in skipped_directories
            and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            if suffix == ".asc":
                sources.append(relative)
            elif suffix in ALLOWED_IMAGE_SUFFIXES:
                images.append(relative)
            if len(sources) + len(images) >= maximum:
                return {"sources": sources, "images": images}
    assets = root / "runs" / "system-builder-assets"
    if assets.is_dir() and not assets.is_symlink():
        for path in sorted(assets.glob("*.png")):
            if path.is_file() and not path.is_symlink():
                images.append(path.relative_to(root).as_posix())
                if len(sources) + len(images) >= maximum:
                    break
    return {"sources": sources, "images": images}
