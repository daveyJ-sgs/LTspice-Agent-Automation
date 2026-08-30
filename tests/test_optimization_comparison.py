from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import optimization_comparison
import optimization_engine
from support import TemporaryRunsTestCase


class OptimizationComparisonTests(TemporaryRunsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.baseline = self._result()
        self.tolerances = {
            "alias_gain": {"absolute": 0.05, "relative": 0.0},
            "settling_time": {"absolute": 25e-9, "relative": 0.0},
        }

    @staticmethod
    def _result() -> dict[str, object]:
        candidates = []
        for index in range(2):
            candidates.append(
                {
                    "candidate_index": index,
                    "parameters": {"R": str(index + 1)},
                    "status": "feasible",
                    "pareto": True,
                    "selected": index == 1,
                    "objectives": {
                        "alias_gain": {"value": -25.0 + index, "unit": "dB"},
                        "settling_time": {
                            "value": 1.0e-6 + index * 0.1e-6,
                            "unit": "s",
                        },
                    },
                    "constraints": {},
                    "errors": [],
                }
            )
        return {
            "schema_version": optimization_engine.OPTIMIZATION_RESULT_SCHEMA_VERSION,
            "generator_version": optimization_engine.OPTIMIZATION_RESULT_GENERATOR_VERSION,
            "plan_id": "optimization-plan-0123456789abcdef",
            "study_id": "optimization-study-aaaaaaaaaaaaaaaa",
            "candidate_count": 2,
            "selected_candidate_index": 1,
            "candidates": candidates,
        }

    def test_exact_decisions_and_bounded_objectives_pass(self) -> None:
        candidate = copy.deepcopy(self.baseline)
        candidate["study_id"] = "optimization-study-bbbbbbbbbbbbbbbb"
        candidate["candidates"][0]["objectives"]["alias_gain"]["value"] += 0.04  # type: ignore[index]
        candidate["candidates"][1]["objectives"]["settling_time"]["value"] += 20e-9  # type: ignore[index]

        result = optimization_comparison.write_optimization_comparison(
            self.runs,
            self.baseline,
            candidate,
            self.tolerances,
            baseline_label="macOS",
            candidate_label="Windows",
        )
        repeated = optimization_comparison.write_optimization_comparison(
            self.runs,
            self.baseline,
            candidate,
            self.tolerances,
            baseline_label="macOS",
            candidate_label="Windows",
        )

        self.assertEqual(result, repeated)
        self.assertTrue(result["passed"])
        self.assertEqual(result["exact_mismatches"], 0)
        self.assertEqual(result["objective_mismatches"], 0)
        document = json.loads(Path(result["comparison_json"]).read_text())
        self.assertEqual(document["candidate_count"], 2)
        report = Path(result["report_html"]).read_text(encoding="utf-8")
        self.assertIn("PASS: macOS vs Windows", report)
        self.assertIn("Acceptance contract", report)

    def test_classification_pareto_selection_and_numeric_drift_fail(self) -> None:
        candidate = copy.deepcopy(self.baseline)
        candidate["selected_candidate_index"] = 0
        candidate["candidates"][0]["status"] = "constraint_failed"  # type: ignore[index]
        candidate["candidates"][0]["pareto"] = False  # type: ignore[index]
        candidate["candidates"][0]["selected"] = True  # type: ignore[index]
        candidate["candidates"][1]["selected"] = False  # type: ignore[index]
        candidate["candidates"][1]["objectives"]["alias_gain"]["value"] += 0.051  # type: ignore[index]

        result = optimization_comparison.compare_optimization_documents(
            self.baseline, candidate, self.tolerances
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["exact_mismatches"], 5)
        self.assertEqual(result["objective_mismatches"], 1)

    def test_rejects_different_plan_and_incomplete_tolerances(self) -> None:
        candidate = copy.deepcopy(self.baseline)
        candidate["plan_id"] = "optimization-plan-fedcba9876543210"
        with self.assertRaisesRegex(ValueError, "different plans"):
            optimization_comparison.compare_optimization_documents(
                self.baseline, candidate, self.tolerances
            )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            optimization_comparison.compare_optimization_documents(
                self.baseline,
                self.baseline,
                {"alias_gain": self.tolerances["alias_gain"]},
            )


if __name__ == "__main__":
    unittest.main()
