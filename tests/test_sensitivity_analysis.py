from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sensitivity_analysis


class SensitivityAnalysisTests(unittest.TestCase):
    @staticmethod
    def point(
        index: int,
        parameters: dict[str, str],
        linear_value: float,
        curve_value: float,
    ) -> dict[str, object]:
        return {
            "index": index,
            "parameters": parameters,
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
                                "metric": "linear_margin",
                                "value": linear_value,
                                "unit": "dB",
                                "passed": linear_value >= 0,
                                "parameters": {},
                                "threshold": {
                                    "operator": ">=",
                                    "target": 0,
                                    "unit": "dB",
                                },
                            },
                            {
                                "metric": "curved_margin",
                                "value": curve_value,
                                "unit": "dB",
                                "passed": curve_value >= 0,
                                "parameters": {},
                                "threshold": {
                                    "operator": ">=",
                                    "target": 0,
                                    "unit": "dB",
                                },
                            },
                        ],
                        "all_passed": linear_value >= 0 and curve_value >= 0,
                    },
                }
            ],
            "all_passed": linear_value >= 0 and curve_value >= 0,
            "error": None,
        }

    @staticmethod
    def plan(points: list[dict[str, object]]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generator_version": "sha256-stratified-gaussian-v7",
            "definition_hash": "a" * 64,
            "definition": {
                "sample_count": len(points),
                "seed": 1,
                "sampling_method": "halton",
                "variables": [
                    {"name": "A", "distribution": "uniform", "unit": "V"},
                    {"name": "B", "distribution": "uniform", "unit": "ohm"},
                    {"name": "C", "distribution": "uniform", "unit": "F"},
                    {"name": "D", "distribution": "discrete", "unit": ""},
                ],
                "correlations": [
                    {"variables": ["A", "B"], "matrix": [["1", "-1"], ["-1", "1"]]}
                ],
            },
            "parameter_order": ["A", "B", "C", "D"],
            "parameter_units": {"A": "V", "B": "ohm", "C": "F", "D": ""},
            "sample_count": len(points),
            "points": points,
        }

    def fixture(
        self, sample_count: int = 6
    ) -> tuple[dict[str, object], dict[str, object]]:
        curve = [0, 1, 2, 2, 1, 0]
        plan_points = []
        result_points = []
        for index in range(sample_count):
            parameters = {
                "A": str(index),
                "B": str(sample_count - index),
                "C": "1",
                "D": "fast" if index % 2 else "slow",
            }
            plan_points.append({"index": index, "parameters": parameters})
            result_points.append(
                self.point(index, parameters, index, curve[index % len(curve)])
            )
        return (
            {
                "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
                "point_count": sample_count,
                "points": result_points,
            },
            self.plan(plan_points),
        )

    def test_reports_monotonic_nonmonotonic_and_unsupported_inputs(self) -> None:
        results, plan = self.fixture()

        analysis = sensitivity_analysis.build_sensitivity_analysis(results, plan)

        self.assertEqual(analysis["method"], "spearman_rank_correlation_average_ties")
        self.assertEqual(analysis["requirement_count"], 2)
        self.assertEqual(analysis["variable_count"], 4)
        self.assertEqual(analysis["scope_count"], 1)
        self.assertEqual(analysis["evaluated_pairs"], 48)
        linear = next(
            requirement
            for requirement in analysis["requirements"]
            if requirement["metric"] == "linear_margin"
        )
        variables = {
            variable["variable"]: variable
            for variable in linear["scopes"][0]["variables"]
        }
        self.assertEqual(variables["A"]["rho"], 1)
        self.assertEqual(variables["A"]["rank"], 1)
        self.assertEqual(variables["A"]["direction"], "positive")
        self.assertEqual(variables["A"]["correlated_with"], ["B"])
        self.assertEqual(variables["B"]["rho"], -1)
        self.assertEqual(variables["B"]["rank"], 1)
        self.assertEqual(variables["C"]["status"], "constant_input")
        self.assertEqual(variables["D"]["status"], "non_numeric_input")
        curved = next(
            requirement
            for requirement in analysis["requirements"]
            if requirement["metric"] == "curved_margin"
        )
        curved_a = next(
            variable
            for variable in curved["scopes"][0]["variables"]
            if variable["variable"] == "A"
        )
        self.assertAlmostEqual(curved_a["rho"], 0)
        self.assertFalse(curved_a["meaningfully_monotonic"])
        self.assertEqual(curved_a["strength"], "negligible")

    def test_named_corners_are_analyzed_separately(self) -> None:
        plan_points = []
        result_points = []
        metadata = []
        point_index = 0
        for sample_index in range(5):
            for corner, value in (
                ("forward", sample_index),
                ("reverse", 4 - sample_index),
            ):
                parameters = {"A": str(sample_index)}
                plan_points.append(
                    {
                        "index": point_index,
                        "parameters": parameters,
                        "sample_index": sample_index,
                        "corners": {"mode": corner},
                    }
                )
                result_points.append(self.point(point_index, parameters, value, value))
                metadata.append(
                    {
                        "index": point_index,
                        "sample_index": sample_index,
                        "corners": {"mode": corner},
                    }
                )
                point_index += 1
        plan = self.plan(plan_points)
        plan["definition"]["variables"] = [
            {"name": "A", "distribution": "uniform", "unit": "V"}
        ]
        plan["parameter_order"] = ["A"]
        plan["parameter_units"] = {"A": "V"}
        results = {
            "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
            "point_count": 10,
            "points": result_points,
        }

        analysis = sensitivity_analysis.build_sensitivity_analysis(
            results, plan, point_metadata=metadata
        )

        linear = next(
            requirement
            for requirement in analysis["requirements"]
            if requirement["metric"] == "linear_margin"
        )
        scopes = {
            scope["corners"]["mode"]: scope["variables"][0]
            for scope in linear["scopes"]
        }
        self.assertEqual(analysis["scope_count"], 2)
        self.assertEqual(scopes["forward"]["rho"], 1)
        self.assertEqual(scopes["reverse"]["rho"], -1)

    def test_insufficient_and_constant_response_are_explicit(self) -> None:
        short_results, short_plan = self.fixture(3)
        short = sensitivity_analysis.build_sensitivity_analysis(
            short_results, short_plan
        )
        short_a = short["requirements"][0]["scopes"][0]["variables"][0]
        self.assertEqual(short_a["status"], "insufficient_samples")

        results, plan = self.fixture()
        for point in results["points"]:
            point["analyses"][0]["analysis"]["results"][0]["value"] = 1
        constant = sensitivity_analysis.build_sensitivity_analysis(results, plan)
        constant_a = constant["requirements"][0]["scopes"][0]["variables"][0]
        self.assertEqual(constant_a["status"], "constant_response")

    def test_plan_parameters_and_corner_attribution_are_fail_closed(self) -> None:
        results, plan = self.fixture()
        plan["points"][0]["parameters"] = {
            **plan["points"][0]["parameters"],
            "A": "changed",
        }
        with self.assertRaisesRegex(ValueError, "parameters do not match"):
            sensitivity_analysis.build_sensitivity_analysis(results, plan)

        plan_points = [
            {
                "index": index,
                "parameters": {"A": str(index)},
                "sample_index": index,
                "corners": {"mode": "actual"},
            }
            for index in range(5)
        ]
        plan = self.plan(plan_points)
        plan["definition"]["variables"] = [
            {"name": "A", "distribution": "uniform", "unit": "V"}
        ]
        plan["parameter_order"] = ["A"]
        plan["parameter_units"] = {"A": "V"}
        results = {
            "experiment_id": "mcp-experiment-20260825-090000-000000-a1b2c3d4",
            "point_count": 5,
            "points": [
                self.point(index, {"A": str(index)}, index, index)
                for index in range(5)
            ],
        }
        metadata = [
            {
                "index": index,
                "sample_index": index,
                "corners": {"mode": "changed"},
            }
            for index in range(5)
        ]
        with self.assertRaisesRegex(ValueError, "corner attribution"):
            sensitivity_analysis.build_sensitivity_analysis(
                results, plan, point_metadata=metadata
            )

    def test_average_ranks_preserve_ties(self) -> None:
        self.assertEqual(
            sensitivity_analysis._average_ranks([1, 1, 3, 2]),
            [1.5, 1.5, 4, 3],
        )
        self.assertEqual(sensitivity_analysis._spearman([1, 1, 2], [2, 2, 3]), 1)

    def test_terminal_study_writes_json_and_csv(self) -> None:
        results, plan = self.fixture()
        artifact = b"validated plan bytes\n"
        digest = hashlib.sha256(artifact).hexdigest()
        plan_id = f"statistical-plan-{digest[:16]}"
        source = {
            "kind": "statistical",
            "sampling_method": "halton",
            "generator_version": plan["generator_version"],
            "plan_id": plan_id,
            "plan_sha256": digest,
            "definition_hash": plan["definition_hash"],
            "runs_relative_path": f"statistical-plans/{plan_id}/statistical_plan.json",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            experiment_dir = runs / results["experiment_id"]
            experiment_dir.mkdir(parents=True)
            plan_path = runs / source["runs_relative_path"]
            plan_path.parent.mkdir(parents=True)
            plan_path.write_bytes(artifact)
            with (
                patch.object(
                    sensitivity_analysis.experiment_index,
                    "load_terminal_experiment",
                    return_value=(
                        experiment_dir,
                        {"definition": {"point_plan": {"source": source}}},
                        results,
                        {},
                    ),
                ),
                patch.object(
                    sensitivity_analysis.statistical_engine,
                    "load_statistical_plan",
                    return_value=plan,
                ),
            ):
                summary = sensitivity_analysis.analyze_statistical_sensitivity(
                    runs, str(results["experiment_id"])
                )

            persisted = json.loads(
                Path(summary["sensitivity_json"]).read_text(encoding="utf-8")
            )
            rows = list(
                csv.DictReader(
                    io.StringIO(
                        Path(summary["sensitivity_csv"]).read_text(encoding="utf-8"),
                        newline="",
                    )
                )
            )
            self.assertEqual(persisted["sampling_provenance"]["plan_sha256"], digest)
            self.assertEqual(len(rows), 8)
            self.assertEqual(summary["evaluated_pairs"], 48)

    def test_writer_rejects_a_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside.json"
            outside.write_text("preserve", encoding="utf-8")
            target = root / "sensitivity.json"
            target.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                sensitivity_analysis._write_atomic(target, "changed")

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
