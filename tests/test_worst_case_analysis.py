from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worst_case_analysis


class WorstCaseAnalysisTests(unittest.TestCase):
    @staticmethod
    def point(index: int, value: float, second_value: float) -> dict[str, object]:
        passed = value <= 3
        return {
            "index": index,
            "parameters": {"R": str(index + 1)},
            "run_dir": f"point-{index:04d}",
            "simulation_status": "completed",
            "measurements": {},
            "analyses": [
                {
                    "name": "response",
                    "status": "completed",
                    "analysis": {
                        "results": [
                            {
                                "metric": "gain",
                                "value": value,
                                "unit": "dB",
                                "passed": passed,
                                "parameters": {"frequency_value": 1000},
                                "threshold": {
                                    "operator": "<=",
                                    "target": 3,
                                    "unit": "dB",
                                },
                            },
                            {
                                "metric": "bandwidth",
                                "value": second_value,
                                "unit": "Hz",
                                "passed": True,
                                "parameters": {},
                                "threshold": {
                                    "operator": ">=",
                                    "target": 0,
                                    "unit": "Hz",
                                },
                            },
                        ],
                        "all_passed": passed,
                    },
                }
            ],
            "all_passed": passed,
            "error": None,
        }

    def test_ranks_samples_and_finite_corners_with_dense_ties(self) -> None:
        points = [
            self.point(0, 5, 0),
            self.point(1, 4, 0),
            self.point(2, 5, 1),
            self.point(3, 2, 1),
            {
                **self.point(4, 100, 100),
                "simulation_status": "cancelled",
                "all_passed": False,
            },
        ]
        metadata = [
            {"index": 0, "sample_index": 0, "corners": {"load": "light"}},
            {"index": 1, "sample_index": 1, "corners": {"load": "light"}},
            {"index": 2, "sample_index": 0, "corners": {"load": "heavy"}},
            {"index": 3, "sample_index": 1, "corners": {"load": "heavy"}},
            {"index": 4, "sample_index": 0, "corners": {"load": "unused"}},
        ]

        analysis = worst_case_analysis.build_worst_case_analysis(
            {
                "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
                "point_count": 5,
                "points": points,
            },
            point_metadata=metadata,
        )

        self.assertEqual(analysis["schema_version"], 1)
        self.assertEqual(analysis["requirement_count"], 2)
        self.assertEqual(analysis["ranked_sample_count"], 8)
        self.assertEqual(analysis["corner_count"], 3)
        self.assertEqual(analysis["invalid_points"], 1)
        gain = next(
            requirement
            for requirement in analysis["requirements"]
            if requirement["metric"] == "gain"
        )
        self.assertEqual(gain["worst_margin"], -2)
        self.assertEqual(
            [case["point_index"] for case in gain["worst_cases"]],
            [0, 2, 1, 3],
        )
        self.assertEqual(
            [case["rank"] for case in gain["worst_cases"]], [1, 1, 2, 3]
        )
        self.assertEqual(
            [corner["corners"] for corner in gain["corner_rankings"]],
            [
                {"load": "heavy"},
                {"load": "light"},
                {"load": "unused"},
            ],
        )
        self.assertEqual(
            [corner["rank"] for corner in gain["corner_rankings"]],
            [1, 1, None],
        )
        self.assertEqual(
            [corner["worst_point_indexes"] for corner in gain["corner_rankings"]],
            [[2], [0], []],
        )
        csv_document = worst_case_analysis._csv_document(analysis)
        self.assertIn("worst_case", csv_document)
        self.assertIn("corner_ranking", csv_document)
        self.assertIn('"{""load"":""unused""}"', csv_document)

    def test_worst_case_limit_keeps_all_ties_at_the_cutoff(self) -> None:
        values = [100 - index for index in range(24)] + [76, 76, 75, 74, 73, 72]
        points = [
            self.point(index, value, 1) for index, value in enumerate(values)
        ]

        analysis = worst_case_analysis.build_worst_case_analysis(
            {
                "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
                "point_count": 30,
                "points": points,
            }
        )

        gain = next(
            requirement
            for requirement in analysis["requirements"]
            if requirement["metric"] == "gain"
        )
        self.assertEqual(gain["nominal_limit"], 25)
        self.assertEqual(gain["returned_samples"], 26)
        self.assertEqual(
            [case["point_index"] for case in gain["worst_cases"]],
            list(range(26)),
        )
        self.assertEqual(gain["worst_cases"][-2]["rank"], 25)
        self.assertEqual(gain["worst_cases"][-1]["rank"], 25)
        self.assertEqual(gain["corner_rankings"], [])

    def test_tie_expansion_respects_artifact_row_budget(self) -> None:
        points = [self.point(index, 100, 1) for index in range(3)]

        with (
            patch.object(
                worst_case_analysis.statistical_results, "MAX_ANALYSIS_ROWS", 2
            ),
            self.assertRaisesRegex(ValueError, "artifact row budget"),
        ):
            worst_case_analysis.build_worst_case_analysis(
                {
                    "experiment_id": (
                        "mcp-experiment-20260825-090000-000000-a1b2c3d4"
                    ),
                    "point_count": len(points),
                    "points": points,
                }
            )

    def test_all_comparison_operators_have_signed_boundary_semantics(self) -> None:
        expected = {
            "<": False,
            "<=": True,
            ">": False,
            ">=": True,
        }
        for operator, passed in expected.items():
            with self.subTest(operator=operator):
                self.assertEqual(worst_case_analysis._margin(operator, 1, 1), 0)
                self.assertIs(worst_case_analysis._passes(operator, 1, 1), passed)

    def test_rejects_missing_or_reordered_requirements(self) -> None:
        first = self.point(0, 2, 1)
        missing = self.point(1, 2, 1)
        missing["analyses"][0]["analysis"]["results"].pop()
        reordered = self.point(1, 2, 1)
        reordered["analyses"][0]["analysis"]["results"].reverse()

        for point in (missing, reordered):
            with self.subTest(point=point):
                with self.assertRaisesRegex(ValueError, "stable ordered"):
                    worst_case_analysis.build_worst_case_analysis(
                        {
                            "experiment_id": (
                                "mcp-experiment-20260825-090000-000000-a1b2c3d4"
                            ),
                            "point_count": 2,
                            "points": [first, point],
                        }
                    )

    def test_unfinished_points_are_counted_but_not_ranked(self) -> None:
        analysis = worst_case_analysis.build_worst_case_analysis(
            {
                "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
                "point_count": 3,
                "points": [self.point(0, 2, 1), self.point(1, 2, 1)],
            }
        )

        self.assertEqual(analysis["invalid_points"], 1)
        self.assertEqual(analysis["ranked_sample_count"], 4)

    def test_rejects_inconsistent_requirement_evidence(self) -> None:
        point = self.point(0, 2, 1)
        point["analyses"][0]["analysis"]["results"][0]["passed"] = False

        with self.assertRaisesRegex(ValueError, "pass state"):
            worst_case_analysis.build_worst_case_analysis(
                {
                    "experiment_id": (
                        "mcp-experiment-20260825-090000-000000-a1b2c3d4"
                    ),
                    "point_count": 1,
                    "points": [point],
                }
            )

    def test_terminal_statistical_study_writes_portable_artifacts(self) -> None:
        experiment_id = "mcp-experiment-20260825-090000-000000-a1b2c3d4"
        plan_sha256 = "0123456789abcdef" + "0" * 48
        source = {
            "kind": "statistical",
            "sampling_method": "halton",
            "generator_version": "sha256-stratified-gaussian-v7",
            "plan_id": "statistical-plan-0123456789abcdef",
            "plan_sha256": plan_sha256,
            "definition_hash": "a" * 64,
            "runs_relative_path": (
                "statistical-plans/statistical-plan-0123456789abcdef/"
                "statistical_plan.json"
            ),
        }
        results = {
            "experiment_id": experiment_id,
            "point_count": 1,
            "points": [self.point(0, 5, 1)],
        }
        results["points"][0]["parameters"]["MODEL"] = "μ,fast\nlot"
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            experiment_dir = runs / experiment_id
            experiment_dir.mkdir(parents=True)
            with patch.object(
                worst_case_analysis.experiment_index,
                "load_terminal_experiment",
                return_value=(
                    experiment_dir,
                    {"definition": {"point_plan": {"source": source}}},
                    results,
                    {},
                ),
            ), patch.object(
                worst_case_analysis.statistical_results,
                "_verified_sampling_plan",
                return_value=(
                    worst_case_analysis.statistical_results._sampling_provenance(
                        source
                    ),
                    {},
                ),
            ):
                result = worst_case_analysis.analyze_statistical_worst_cases(
                    runs, experiment_id
                )

            persisted = json.loads(
                Path(result["worst_cases_json"]).read_text(encoding="utf-8")
            )
            self.assertEqual(result["requirement_count"], 2)
            self.assertEqual(result["ranked_sample_count"], 2)
            self.assertEqual(
                persisted["sampling_provenance"]["plan_sha256"], plan_sha256
            )
            self.assertEqual(
                persisted["requirements"][0]["worst_cases"][0]["evidence_path"],
                "point-0000/",
            )
            csv_document = Path(result["worst_cases_csv"]).read_text(
                encoding="utf-8"
            )
            rows = list(csv.DictReader(io.StringIO(csv_document, newline="")))
            self.assertEqual({row["record_type"] for row in rows}, {"worst_case"})
            self.assertEqual(
                json.loads(rows[0]["point_parameters"])["MODEL"], "μ,fast\nlot"
            )

    def test_worst_case_writer_rejects_a_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside.json"
            outside.write_text("preserve", encoding="utf-8")
            target = root / "worst_cases.json"
            target.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                worst_case_analysis._write_atomic(target, "changed")

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
