from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class TemporaryRunsTestCase(unittest.TestCase):
    """Provide an isolated root and runs path for unittest test cases."""

    def setUp(self) -> None:
        super().setUp()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.runs = self.root / "runs"
