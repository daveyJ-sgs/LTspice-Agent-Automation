from __future__ import annotations

import stat
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MacLauncherTests(unittest.TestCase):
    def test_launcher_contract_is_local_non_admin_and_diagnostic(self) -> None:
        launcher_path = ROOT / "Start-SystemBuilder.command"
        script = launcher_path.read_text(encoding="utf-8")

        for required in (
            "brew install python@3.13",
            "brew install --cask ltspice",
            "requirements-gui.txt",
            "requirements-mcp.txt",
            "system_builder.py",
            "LTSPICE_EXECUTABLE",
            "/Applications/LTspice.app/Contents/MacOS/LTspice",
            "--no-browser",
        ):
            self.assertIn(required, script)
        self.assertNotIn("sudo ", script)
        self.assertTrue(script.startswith("#!/bin/bash"))

        mode = launcher_path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "launcher must be executable")


if __name__ == "__main__":
    unittest.main()
