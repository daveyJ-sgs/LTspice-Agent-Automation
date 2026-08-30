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
        self.assertIn("powershell.exe -NoLogo -NoProfile", command)
        self.assertIn('"%~dp0Start-SystemBuilder.ps1" %*', command)
