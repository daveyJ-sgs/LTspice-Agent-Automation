from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qualification_recipe


class QualificationRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(); self.runs = Path(self.temporary.name) / "runs"
        self.candidate = {"status": "feasible", "selected": True, "parameters": {"R": "100"}}
        self.variables = [{"name": "R", "distribution": "gaussian", "nominal": 100, "sigma": 1, "minimum": 95, "maximum": 105, "unit": "ohm"}]

    def tearDown(self) -> None: self.temporary.cleanup()

    def _preview(self) -> dict[str, object]:
        with patch.object(qualification_recipe, "_candidate", return_value=self.candidate), patch.object(qualification_recipe, "qualification_variables", return_value=self.variables), patch.object(qualification_recipe, "CORRELATIONS", []), patch.object(qualification_recipe, "CORNERS", [{"name": "load", "parameter": "C", "unit": "F", "values": [{"name": "light", "value": 1e-12}, {"name": "heavy", "value": 2e-12}]}]):
            return qualification_recipe.preview_qualification(self.runs, "optimization-study-aaaaaaaaaaaaaaaa", 3, 4, 42)

    def test_preview_is_deterministic_and_does_not_write(self) -> None:
        first = self._preview(); repeated = self._preview()
        self.assertEqual(first, repeated); self.assertEqual(first["plan"]["point_count"], 8)
        self.assertEqual(first["execution"]["total_run_count"], 16)
        self.assertFalse(self.runs.exists())

    def test_preview_rejects_invalid_sample_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_count"):
            qualification_recipe.preview_qualification(self.runs, "study", 0, 1, 42)


if __name__ == "__main__": unittest.main()
