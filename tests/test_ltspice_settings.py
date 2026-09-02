"""GUI-configurable LTspice executable override: persistence and live update.

Every test patches ltspice_wrapper._settings_path so nothing here ever
touches the real per-machine settings file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ltspice_wrapper


class LTspiceSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings_path = Path(self.temporary_directory.name) / "settings.json"
        patcher = patch.object(
            ltspice_wrapper, "_settings_path", return_value=self.settings_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._original_ltspice = ltspice_wrapper.LTSPICE
        self.addCleanup(self._restore_ltspice)

    def _restore_ltspice(self) -> None:
        ltspice_wrapper.LTSPICE = self._original_ltspice

    def test_set_persists_applies_immediately_and_rejects_a_missing_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".exe") as fake_executable:
            resolved = ltspice_wrapper.set_ltspice_executable(fake_executable.name)

            self.assertEqual(resolved, Path(fake_executable.name).expanduser())
            self.assertEqual(ltspice_wrapper.LTSPICE, resolved)
            self.assertEqual(
                ltspice_wrapper.get_ltspice_executable_override(), str(resolved)
            )
            status = ltspice_wrapper.ltspice_status()
            self.assertEqual(status["source"], "configured")
            self.assertTrue(status["exists"])

        with self.assertRaises(ValueError):
            ltspice_wrapper.set_ltspice_executable("/does/not/exist/LTspice.exe")
        # The rejected path must not have overwritten the good one.
        self.assertEqual(ltspice_wrapper.LTSPICE, resolved)

    def test_clearing_falls_back_to_discovery(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".exe") as fake_executable:
            ltspice_wrapper.set_ltspice_executable(fake_executable.name)
            self.assertIsNotNone(ltspice_wrapper.get_ltspice_executable_override())

            ltspice_wrapper.set_ltspice_executable(None)

        self.assertIsNone(ltspice_wrapper.get_ltspice_executable_override())
        self.assertEqual(ltspice_wrapper.LTSPICE, ltspice_wrapper._default_ltspice())

    def test_environment_variable_still_takes_priority_over_a_saved_override(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".exe") as overridden, \
             tempfile.NamedTemporaryFile(suffix=".exe") as env_wins:
            ltspice_wrapper.set_ltspice_executable(overridden.name)
            with patch.dict("os.environ", {"LTSPICE_EXECUTABLE": env_wins.name}):
                resolved = ltspice_wrapper._default_ltspice()

        self.assertEqual(resolved, Path(env_wins.name).expanduser())

    def test_settings_file_is_not_reused_across_machines_setting(self) -> None:
        # A fresh settings file (nothing saved yet) must not crash and must
        # report no override.
        self.assertFalse(self.settings_path.exists())
        self.assertIsNone(ltspice_wrapper.get_ltspice_executable_override())


if __name__ == "__main__":
    unittest.main()
