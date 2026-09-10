from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qualification_recipe


class QualificationRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary.name) / "runs"
        self.candidate = {
            "status": "feasible",
            "selected": True,
            "parameters": {"R": "100"},
        }
        self.model = {
            "fixed_parameters": {},
            "variables": [
                {
                    "name": "R",
                    "sigma_fraction": 0.01,
                    "minimum_factor": 0.95,
                    "maximum_factor": 1.05,
                    "unit": "ohm",
                }
            ],
            "correlations": [],
            "corner_axes": [],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _preview(self, model: object | None = None) -> dict[str, object]:
        with patch.object(
            qualification_recipe, "_candidate", return_value=self.candidate
        ):
            return qualification_recipe.preview_qualification(
                self.runs,
                "optimization-study-aaaaaaaaaaaaaaaa",
                3,
                4,
                42,
                self.model if model is None else model,
            )

    def test_preview_is_deterministic_and_does_not_write(self) -> None:
        first = self._preview()
        repeated = self._preview()
        self.assertEqual(first, repeated)
        self.assertEqual(first["plan"]["point_count"], 4)
        self.assertEqual(first["execution"]["total_run_count"], 8)
        self.assertFalse(self.runs.exists())

    def test_preview_requires_explicit_nonempty_model(self) -> None:
        with (
            patch.object(
                qualification_recipe, "_candidate", return_value=self.candidate
            ),
            self.assertRaisesRegex(ValueError, "non-empty"),
        ):
            qualification_recipe.preview_qualification(self.runs, "study", 0, 4, 42, {})

    def test_preview_rejects_unmapped_candidate_parameter(self) -> None:
        model = {
            **self.model,
            "variables": [
                {
                    "name": "C",
                    "sigma_fraction": 0.01,
                    "minimum_factor": 0.95,
                    "maximum_factor": 1.05,
                    "unit": "F",
                }
            ],
        }
        with (
            patch.object(
                qualification_recipe, "_candidate", return_value=self.candidate
            ),
            self.assertRaisesRegex(ValueError, "no nominal"),
        ):
            qualification_recipe.preview_qualification(
                self.runs, "study", 0, 4, 42, model
            )

    def test_model_change_changes_preview_identity(self) -> None:
        changed = {
            **self.model,
            "variables": [{**self.model["variables"][0], "sigma_fraction": 0.02}],
        }
        self.assertNotEqual(
            self._preview()["qualification_id"],
            self._preview(changed)["qualification_id"],
        )

    def test_preview_rejects_invalid_sample_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_count"):
            qualification_recipe.preview_qualification(
                self.runs, "study", 0, 1, 42, self.model
            )


if __name__ == "__main__":
    unittest.main()
