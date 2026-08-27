from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import optimization_engine


class OptimizationEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def parameters() -> list[optimization_engine.OptimizationParameter]:
        return [
            {
                "name": "R",
                "kind": "preferred_values",
                "series": "E12",
                "values": [1200, 1000],
                "unit": "ohm",
            },
            {
                "name": "C",
                "kind": "continuous",
                "minimum": 8.2e-11,
                "maximum": 1e-10,
                "count": 2,
                "unit": "F",
            },
        ]

    @staticmethod
    def objectives() -> list[optimization_engine.OptimizationObjective]:
        return [
            {
                "name": "alias_rejection",
                "experiment": "ac",
                "analysis": "alias",
                "metric": "ac_gain_db",
                "goal": "minimize",
                "weight": 1,
                "metric_parameters": {"frequency_value": 10_000_000},
            },
            {
                "name": "settling_time",
                "experiment": "transient",
                "analysis": "settling",
                "metric": "settling_time",
                "goal": "minimize",
                "weight": 2,
            },
        ]

    @staticmethod
    def constraints() -> list[optimization_engine.OptimizationConstraint]:
        return [
            {
                "name": "passband_gain",
                "experiment": "ac",
                "analysis": "passband",
                "metric": "ac_gain_db",
                "operator": ">=",
                "target": 3.5,
                "metric_parameters": {"frequency_value": 100_000},
            }
        ]

    @staticmethod
    def corners() -> list[optimization_engine.OptimizationCornerAxis]:
        return [
            {
                "name": "adc_load",
                "parameter": "CADC",
                "unit": "F",
                "values": [
                    {"name": "light", "value": 2e-11},
                    {"name": "heavy", "value": 8e-11},
                ],
            }
        ]

    def test_plan_is_deterministic_reorder_stable_and_expands_corners(self) -> None:
        plan = optimization_engine.build_optimization_plan(
            self.parameters(),
            self.objectives(),
            self.constraints(),
            fixed_parameters={"GAIN": 1.6},
            corner_axes=self.corners(),
        )
        repeated = optimization_engine.build_optimization_plan(
            list(reversed(self.parameters())),
            list(reversed(self.objectives())),
            self.constraints(),
            fixed_parameters={"GAIN": 1.6},
            corner_axes=self.corners(),
        )

        self.assertEqual(plan, repeated)
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["generator_version"], "deterministic-cartesian-v1")
        self.assertEqual(plan["candidate_count"], 4)
        self.assertEqual(plan["point_count"], 8)
        self.assertEqual(plan["parameter_order"], ["C", "CADC", "GAIN", "R"])
        self.assertEqual(plan["points"][0]["candidate_index"], 0)
        self.assertEqual(plan["points"][0]["corners"], {"adc_load": "heavy"})
        self.assertEqual(plan["points"][1]["corners"], {"adc_load": "light"})
        self.assertEqual(
            hashlib.sha256(optimization_engine._plan_bytes(plan)).hexdigest(),
            "6212c079155c7712a0be7ceaefbf1268e92d50f7e05370d1e484074cf8248657",
        )

    def test_plan_is_content_addressed_and_tampering_fails_closed(self) -> None:
        plan = optimization_engine.build_optimization_plan(
            self.parameters(), self.objectives(), self.constraints()
        )
        saved = optimization_engine.save_optimization_plan(self.runs, plan)

        self.assertEqual(
            optimization_engine.inspect_optimization_plan(
                self.runs, saved["plan_id"]
            )["points"],
            plan["points"],
        )
        plan_file = Path(saved["plan_file"])
        document = json.loads(plan_file.read_text(encoding="utf-8"))
        document["points"][0]["parameters"]["C"] = "123"
        plan_file.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content address"):
            optimization_engine.load_optimization_plan(self.runs, saved["plan_id"])

    def test_rejects_duplicate_and_unbounded_candidate_domains(self) -> None:
        duplicate = self.parameters()
        duplicate[0]["values"] = [1000, 1000]
        with self.assertRaisesRegex(ValueError, "unique"):
            optimization_engine.build_optimization_plan(
                duplicate, self.objectives(), self.constraints()
            )
        with self.assertRaisesRegex(ValueError, "candidate grid exceeds"):
            optimization_engine.build_optimization_plan(
                [
                    {
                        "name": f"P{index}",
                        "kind": "continuous",
                        "minimum": 0,
                        "maximum": 1,
                        "count": 4,
                    }
                    for index in range(5)
                ],
                self.objectives(),
                self.constraints(),
            )

    @staticmethod
    def _analysis(name: str, metric: str, value: float, **parameters: float) -> dict[str, object]:
        return {
            "name": name,
            "status": "completed",
            "error": None,
            "analysis": {
                "all_passed": True,
                "results": [
                    {
                        "metric": metric,
                        "value": value,
                        "unit": "dB" if metric == "ac_gain_db" else "s",
                        "parameters": parameters,
                        "passed": True,
                        "threshold": {"operator": "<=", "target": 1},
                        "evidence": {},
                    }
                ],
            },
        }

    def test_evaluates_worst_corners_pareto_and_deterministic_selection(self) -> None:
        plan = optimization_engine.build_optimization_plan(
            [
                {
                    "name": "R",
                    "kind": "continuous",
                    "minimum": 1,
                    "maximum": 3,
                    "count": 3,
                }
            ],
            self.objectives(),
            self.constraints(),
            fixed_parameters={"GAIN": 1.6},
            corner_axes=self.corners(),
        )
        saved = optimization_engine.save_optimization_plan(self.runs, plan)
        ac_points: list[dict[str, object]] = []
        transient_points: list[dict[str, object]] = []
        alias_values = [-30.0, -25.0, -20.0]
        settling_values = [1.4e-6, 1.0e-6, 0.8e-6]
        for point in plan["points"]:
            candidate = point["candidate_index"]
            heavy = point.get("corners") == {"adc_load": "heavy"}
            passband = 3.0 if candidate == 2 and heavy else 4.0
            common = {
                "index": point["index"],
                "parameters": point["parameters"],
                "simulation_status": "completed",
                "error": None,
            }
            ac_points.append(
                {
                    **common,
                    "analyses": [
                        self._analysis(
                            "alias",
                            "ac_gain_db",
                            alias_values[candidate] + (1.0 if heavy else 0.0),
                            frequency_value=10_000_000,
                        ),
                        self._analysis(
                            "passband",
                            "ac_gain_db",
                            passband,
                            frequency_value=100_000,
                        ),
                    ],
                }
            )
            transient_points.append(
                {
                    **common,
                    "analyses": [
                        self._analysis(
                            "settling",
                            "settling_time",
                            settling_values[candidate] + (0.1e-6 if heavy else 0.0),
                        )
                    ],
                }
            )
        experiment_data = {
            "ac": {"status": "completed", "points": ac_points},
            "transient": {"status": "completed", "points": transient_points},
        }
        load_results: dict[str, tuple[Path, dict[str, object], dict[str, object], dict[str, object]]] = {}
        for name, results in experiment_data.items():
            experiment_dir = self.runs / f"experiment-{name}"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "results.json").write_text(
                json.dumps(results), encoding="utf-8"
            )
            load_results[f"experiment-{name}"] = (
                experiment_dir,
                {"status": "completed"},
                results,
                {},
            )

        with patch.object(
            optimization_engine.experiment_index,
            "load_terminal_experiment",
            side_effect=lambda _runs, experiment_id: load_results[experiment_id],
        ):
            result = optimization_engine.evaluate_optimization_study(
                self.runs,
                saved["plan_id"],
                {"ac": "experiment-ac", "transient": "experiment-transient"},
            )
            repeated = optimization_engine.evaluate_optimization_study(
                self.runs,
                saved["plan_id"],
                {"transient": "experiment-transient", "ac": "experiment-ac"},
            )

        self.assertEqual(result, repeated)
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["feasible_candidates"], 2)
        self.assertEqual(result["constraint_failed_candidates"], 1)
        self.assertEqual(result["invalid_candidates"], 0)
        self.assertEqual(result["pareto_candidates"], 2)
        self.assertEqual(result["selected_candidate_index"], 1)
        self.assertTrue(Path(result["results_json"]).is_file())
        self.assertTrue(Path(result["results_csv"]).is_file())
        report = Path(result["report_html"]).read_text(encoding="utf-8")
        self.assertIn("Coarse multi-objective search", report)
        self.assertIn("Objective tradeoff", report)
        self.assertIn("Candidate 1 was selected", report)
        self.assertIn("Selected coarse design", report)
        self.assertIn("Settling Time", report)
        self.assertIn("µs", report)
        self.assertIn('class="grid"', report)

    def test_simulation_or_analysis_errors_are_not_constraint_failures(self) -> None:
        plan = optimization_engine.build_optimization_plan(
            [
                {
                    "name": "R",
                    "kind": "continuous",
                    "minimum": 1,
                    "maximum": 2,
                    "count": 2,
                }
            ],
            [self.objectives()[0]],
            self.constraints(),
        )
        saved = optimization_engine.save_optimization_plan(self.runs, plan)
        points = [
            {
                "index": point["index"],
                "parameters": point["parameters"],
                "simulation_status": "error",
                "error": "LTspice failed",
                "analyses": [],
            }
            for point in plan["points"]
        ]
        results = {"status": "completed", "points": points}
        experiment_dir = self.runs / "experiment-ac"
        experiment_dir.mkdir(parents=True)
        (experiment_dir / "results.json").write_text(
            json.dumps(results), encoding="utf-8"
        )
        with patch.object(
            optimization_engine.experiment_index,
            "load_terminal_experiment",
            return_value=(
                experiment_dir,
                {"status": "completed"},
                results,
                {},
            ),
        ):
            result = optimization_engine.evaluate_optimization_study(
                self.runs, saved["plan_id"], {"ac": "experiment-ac"}
            )

        self.assertEqual(result["feasible_candidates"], 0)
        self.assertEqual(result["constraint_failed_candidates"], 0)
        self.assertEqual(result["invalid_candidates"], 2)
        self.assertEqual(result["selected_candidate_index"], None)

    def test_pareto_dominance_honors_minimize_and_maximize(self) -> None:
        objectives = [
            {"name": "error", "goal": "minimize"},
            {"name": "bandwidth", "goal": "maximize"},
        ]
        better = {
            "objectives": {
                "error": {"value": 1.0},
                "bandwidth": {"value": 2.0},
            }
        }
        worse = {
            "objectives": {
                "error": {"value": 1.5},
                "bandwidth": {"value": 1.8},
            }
        }

        self.assertTrue(optimization_engine._dominates(better, worse, objectives))
        self.assertFalse(optimization_engine._dominates(worse, better, objectives))


if __name__ == "__main__":
    unittest.main()
