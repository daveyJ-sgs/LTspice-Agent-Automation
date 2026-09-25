from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_contract_is_local_non_admin_and_diagnostic(self) -> None:
        powershell = (ROOT / "Start-SystemBuilder.ps1").read_text(encoding="utf-8")
        command = (ROOT / "Start-SystemBuilder.cmd").read_text(encoding="utf-8")

        for required in (
            "[string]$Workspace = $PSScriptRoot",
            "[switch]$NoBrowser",
            'Join-Path $projectRoot ".venv"',
            '"requirements-gui.txt"',
            '"system_builder.py"',
            "LTSPICE_EXECUTABLE",
            "winget install --id Python.Python.3.13",
            "winget install --id AnalogDevices.LTspice",
        ):
            self.assertIn(required, powershell)
        self.assertNotIn("-Verb RunAs", powershell)
        # Discovery is diagnostic only: exporting a discovered install would
        # outrank the path the user saved in System Builder's settings.
        self.assertNotIn("$env:LTSPICE_EXECUTABLE =", powershell)
        self.assertIn("powershell.exe -NoLogo -NoProfile", command)
        self.assertIn('"%~dp0Start-SystemBuilder.ps1" %*', command)

    def test_native_probes_cannot_trip_windows_powershell_stop_preference(self) -> None:
        # Under Windows PowerShell 5.1, `2>$null` on a native command with
        # $ErrorActionPreference = "Stop" is a terminating error, so every
        # stderr-silenced probe must run in Test-Python313's "Continue" scope.
        powershell = (ROOT / "Start-SystemBuilder.ps1").read_text(encoding="utf-8")
        probe = powershell[
            powershell.index("function Test-Python313")
            : powershell.index("function Find-CompatiblePython")
        ]
        self.assertIn('$ErrorActionPreference = "Continue"', probe)
        self.assertIn("2>$null", probe)
        self.assertEqual(powershell.count("2>$null"), 1)
        self.assertIn("Test-Python313 -Command $venvPython", powershell)
        self.assertIn('-like "*\\WindowsApps\\python.exe"', powershell)
