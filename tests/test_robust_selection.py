from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import robust_selection
import experiment_index
import statistical_engine


class RobustSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _source(label: str, rank: int) -> dict[str, object]:
        nominal = 100 if label == "coarse" else 110
        study_digit = "a" if label == "coarse" else "b"
        plan_digit = "c" if label == "coarse" else "d"
        return {
            "label": label,
            "tie_break_rank": rank,
            "source_study_id": f"optimization-study-{study_digit * 16}",
            "source_results_sha256": ("1" if label == "coarse" else "2") * 64,
            "source_plan_id": f"optimization-plan-{plan_digit * 16}",
            "source_candidate_index": 1,
            "parameters": {"R": str(nominal)},
            "nominal_objectives": {"settling": {"value": rank + 1, "unit": "s"}},
            "nominal_constraints": {},
        }

    @staticmethod
    def _variables(nominal: float) -> list[statistical_engine.StatisticalVariable]:
        return [
            {
                "name": "R",
                "distribution": "gaussian",
                "nominal": nominal,
                "sigma": 1,
                "minimum": nominal - 5,
                "maximum": nominal + 5,
                "unit": "ohm",
            }
        ]

    def _plan(self) -> robust_selection.RobustSelectionPlanResult:
        finalists = [
            {"label": "coarse", "study_id": "ignored", "candidate_index": 1},
            {"label": "refined", "study_id": "ignored", "candidate_index": 1},
        ]

        def source(_runs: Path, finalist: dict[str, object], rank: int) -> dict[str, object]:
            return self._source(str(finalist["label"]), rank)

        with patch.object(robust_selection, "_source_finalist", side_effect=source):
            return robust_selection.generate_robust_selection_plan(
                self.runs,
                finalists,
                {
                    "coarse": self._variables(100),
                    "refined": self._variables(110),
                },
                4,
                42,
                corner_axes=[
                    {
                        "name": "load",
                        "parameter": "CLOAD",
                        "values": [
                            {"name": "light", "value": 1},
                            {"name": "heavy", "value": 2},
                        ],
                    }
                ],
                sampling_method="halton",
            )

    def test_plan_is_deterministic_content_addressed_and_nominal_bound(self) -> None:
        first = self._plan()
        repeated = self._plan()
        self.assertEqual(first, repeated)
        self.assertEqual(first["finalist_count"], 2)
        self.assertEqual(first["point_count"], 16)
        loaded = robust_selection.load_robust_selection_plan(
            self.runs, first["plan_id"]
        )
        self.assertEqual(
            loaded["definition"]["selection_policy"],
            "complete-evidence-then-worst-corner-joint-yield-then-source-rank-v1",
        )

        def source(_runs: Path, finalist: dict[str, object], rank: int) -> dict[str, object]:
            return self._source(str(finalist["label"]), rank)

        with patch.object(robust_selection, "_source_finalist", side_effect=source):
            with self.assertRaisesRegex(ValueError, "nominal must match"):
                robust_selection.generate_robust_selection_plan(
                    self.runs,
                    [
                        {"label": "coarse", "study_id": "x", "candidate_index": 1},
                        {"label": "refined", "study_id": "y", "candidate_index": 1},
                    ],
                    {
                        "coarse": self._variables(99),
                        "refined": self._variables(110),
                    },
                    4,
                    42,
                )

        plan_path = Path(first["plan_file"])
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        document["definition"]["seed"] = 43
        plan_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content address"):
            robust_selection.load_robust_selection_plan(self.runs, first["plan_id"])

    def test_single_finalist_plan_is_a_qualification_not_a_fake_comparison(self) -> None:
        finalist = {"label": "selected-design", "study_id": "ignored", "candidate_index": 1}

        with patch.object(
            robust_selection,
            "_source_finalist",
            return_value=self._source("coarse", 0) | {"label": "selected-design"},
        ):
            saved = robust_selection.generate_robust_selection_plan(
                self.runs,
                [finalist],
                {"selected-design": self._variables(100)},
                4,
                42,
            )

        self.assertEqual(saved["finalist_count"], 1)
        self.assertEqual(saved["point_count"], 4)

    def test_joint_selection_requires_both_analyses_and_uses_source_tie_break(self) -> None:
        saved = self._plan()
        plan = robust_selection.load_robust_selection_plan(self.runs, saved["plan_id"])
        sources = plan["definition"]["statistical_plans"]
        documents: dict[str, dict[str, dict[str, object]]] = {}
        evidence: dict[str, dict[str, dict[str, str]]] = {}
        for label in ("coarse", "refined"):
            statistical_plan = statistical_engine.load_statistical_plan(
                self.runs, sources[label]["plan_id"]
            )
            ac_points = []
            transient_points = []
            for point in statistical_plan["points"]:
                common = {
                    "index": point["index"],
                    "parameters": point["parameters"],
                    "simulation_status": "completed",
                    "error": None,
                    "analyses": [{"name": "check", "status": "completed"}],
                }
                ac_points.append({**common, "all_passed": True})
                transient_points.append({**common, "all_passed": True})
            documents[label] = {
                "statistical_plan": statistical_plan,
                "ac": {"status": "completed", "points": ac_points},
                "transient": {"status": "completed", "points": transient_points},
            }
            evidence[label] = {
                "ac": {"experiment_id": f"{label}-ac", "results_sha256": "1" * 64},
                "transient": {
                    "experiment_id": f"{label}-transient",
                    "results_sha256": "2" * 64,
                },
            }

        tied = robust_selection.build_robust_selection_result(
            saved["plan_id"], plan, documents, evidence
        )
        self.assertEqual(tied["selected_finalist"], "coarse")
        self.assertEqual(
            [item["worst_corner_yield"] for item in tied["finalists"]],
            [1.0, 1.0],
        )

        documents["coarse"]["transient"]["points"][1]["all_passed"] = False
        result = robust_selection.build_robust_selection_result(
            saved["plan_id"], plan, documents, evidence
        )
        self.assertEqual(result["selected_finalist"], "refined")
        coarse = result["finalists"][0]
        self.assertEqual(coarse["points"][1]["classification"], "failure")
        self.assertEqual(coarse["worst_corner_yield"], 0.75)

    def test_robust_study_is_searchable_by_kind_selection_and_source(self) -> None:
        study_id = "robust-selection-study-0123456789abcdef"
        source_id = "optimization-study-aaaaaaaaaaaaaaaa"
        study_dir = self.runs / "robust-selection-studies" / study_id
        study_dir.mkdir(parents=True)
        (study_dir / "report.html").write_text("<html></html>", encoding="utf-8")
        (study_dir / "robust_selection_results.json").write_text(
            json.dumps(
                {
                    "study_id": study_id,
                    "plan_id": "robust-selection-plan-fedcba9876543210",
                    "selected_finalist": "coarse",
                    "finalists": [
                        {
                            "label": "coarse",
                            "selected": True,
                            "complete_evidence": True,
                            "source_study_id": source_id,
                            "worst_corner_yield": 1.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        built = experiment_index.build_experiment_index(self.runs)
        self.assertEqual(built["indexed_studies"], 1)
        queried = experiment_index.query_studies(
            self.runs,
            kind="robust_selection",
            selected="coarse",
            source_study_id=source_id,
        )
        self.assertEqual(queried["total"], 1)
        self.assertEqual(queried["studies"][0]["worst_corner_yield"], 1.0)
        self.assertEqual(queried["studies"][0]["source_study_ids"], [source_id])

    def test_platform_comparison_requires_same_decision_and_bounded_metrics(self) -> None:
        baseline = {
            "portability_signature": "a" * 64,
            "selected_finalist": "coarse",
            "finalists": [
                {
                    "label": "coarse",
                    "parameters": {"R": "100"},
                    "complete_evidence": True,
                    "corner_results": [
                        {
                            "corners": {"load": "heavy"},
                            "evaluated": 32,
                            "passed": 32,
                            "invalid": 0,
                            "observed_yield": 1.0,
                            "confidence_low": 0.89,
                            "confidence_high": 1.0,
                        }
                    ],
                    "worst_requirements": [
                        {
                            "experiment": "ac",
                            "analysis": "response",
                            "metric": "gain",
                            "operator": ">=",
                            "value": 4.0,
                        }
                    ],
                }
            ],
        }
        candidate = json.loads(json.dumps(baseline))
        candidate["finalists"][0]["worst_requirements"][0]["value"] = 4.04
        passed = robust_selection.compare_portability_summaries(
            baseline, candidate, {"gain": 0.05}
        )
        self.assertTrue(passed["passed"])

        candidate["selected_finalist"] = "other"
        candidate["finalists"][0]["worst_requirements"][0]["value"] = 4.06
        failed = robust_selection.compare_portability_summaries(
            baseline, candidate, {"gain": 0.05}
        )
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["exact_mismatches"], 1)
        self.assertEqual(failed["numeric_mismatches"], 1)


if __name__ == "__main__":
    unittest.main()
