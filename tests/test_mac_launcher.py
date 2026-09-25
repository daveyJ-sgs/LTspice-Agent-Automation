from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
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
        # Discovery is diagnostic only: exporting a discovered install would
        # outrank the path the user saved in System Builder's settings.
        self.assertNotIn("export LTSPICE_EXECUTABLE", script)
        self.assertIn("$HOME/Applications/LTspice.app/Contents/MacOS/LTspice", script)
        self.assertTrue(script.startswith("#!/bin/bash"))

    @unittest.skipUnless(
        os.name == "posix",
        "the executable bit is a POSIX filesystem concept; a checkout on "
        "Windows has no equivalent to assert on, and this launcher never "
        "runs there -- Start-SystemBuilder.cmd/.ps1 do instead",
    )
    def test_launcher_is_executable(self) -> None:
        launcher_path = ROOT / "Start-SystemBuilder.command"
        mode = launcher_path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "launcher must be executable")

    def test_launcher_hardening_contract(self) -> None:
        script = (ROOT / "Start-SystemBuilder.command").read_text(encoding="utf-8")
        self.assertIn('-m venv --clear "$VENV_ROOT"', script)
        self.assertIn("/usr/bin/xcode-select -p", script)
        self.assertIn("read -r _ || true", script)
        self.assertIn("|| STATUS=$?", script)

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"),
        "runs the bash launcher against a stub environment",
    )
    def test_launcher_reports_server_exit_status_and_accepts_spaced_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            (root / ".venv/bin").mkdir(parents=True)
            shutil.copy2(ROOT / "Start-SystemBuilder.command", root)
            for name in ("requirements-gui.txt", "requirements-mcp.txt"):
                (root / name).write_text("", encoding="utf-8")
            (root / "system_builder.py").write_text("", encoding="utf-8")
            # A stub interpreter: real Python for the launcher's probes, but
            # the "server" just echoes its arguments and fails with 3.
            stub = root / ".venv/bin/python"
            stub.write_text(
                "#!/bin/bash\n"
                'case "$1" in\n'
                '  */system_builder.py) shift; echo "SERVER $*"; exit 3 ;;\n'
                "esac\n"
                'if [[ "$*" == *find_spec* ]]; then echo 1; exit 0; fi\n'
                f'exec "{sys.executable}" "$@"\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)
            workspace = Path(temporary) / "My Projects"
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": temporary,
            }
            # The fingerprint marker matches, so no pip install is attempted.
            fingerprint = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import hashlib,pathlib,sys; print(hashlib.sha256(b''.join("
                    "pathlib.Path(p).read_bytes() for p in sys.argv[1:])).hexdigest())",
                    str(root / "requirements-gui.txt"),
                    str(root / "requirements-mcp.txt"),
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            (root / ".venv/.system-builder-requirements").write_text(
                fingerprint, encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    "bash",
                    str(root / "Start-SystemBuilder.command"),
                    "--workspace",
                    str(workspace),
                    "--no-browser",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=environment,
                timeout=60,
                check=False,
            )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn(f"SERVER --workspace {workspace} --no-browser", completed.stdout)
        self.assertIn("Press Return to close this window", completed.stdout)


if __name__ == "__main__":
    unittest.main()
