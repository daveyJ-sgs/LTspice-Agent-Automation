from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import statistical_comparison


class StatisticalComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"
        self.runs.mkdir()
        self.baseline_id = "mcp-experiment-20260825-090000-000000-a1b2c3d4"
        self.candidate_id = "mcp-experiment-20260825-090100-000000-b1c2d3e4"
        self.baseline_dir = self.runs / self.baseline_id
        self.candidate_dir = self.runs / self.candidate_id
        self.baseline_dir.mkdir()
        self.candidate_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def source(character: str = "a") -> dict[str, object]:
        digest = character * 64
        plan_id = f"statistical-plan-{digest[:16]}"
        return {
            "kind": "statistical",
            "sampling_method": "halton",
            "generator_version": "sha256-test-v1",
            "plan_id": plan_id,
            "plan_sha256": digest,
            "definition_hash": "f" * 64,
            "runs_relative_path": f"statistical-plans/{plan_id}/statistical_plan.json",
        }

    @staticmethod
    def manifest(
        source: dict[str, object], netlist: str = "R1 in out {R}\n.end\n"
    ) -> dict[str, object]:
        return {
            "definition": {
                "netlist_template": netlist,
                "parameter_order": ["R"],
                "parameter_units": {"R": "ohm"},
                "waveform_analyses": [
                    {
                        "name": "response",
                        "variable": "V(out)",
                        "requirements": [
                            {"metric": "gain", "operator": "<=", "target": 3.0}
                        ],
                    }
                ],
                "point_plan": {"source": source},
            }
        }

    @staticmethod
    def point(index: int, resistance: str, value: float) -> dict[str, object]:
        passed = value <= 3.0
        return {
            "index": index,
            "parameters": {"R": resistance},
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
                                "parameters": {},
                                "threshold": {
                                    "operator": "<=",
                                    "target": 3.0,
                                    "unit": "dB",
                                },
                            }
                        ]
                    },
                }
            ],
            "all_passed": passed,
            "error": None,
        }

    @classmethod
    def results(cls, values: list[float], resistances: list[str] | None = None):
        names = resistances or [f"{index + 1}k" for index in range(len(values))]
        return {
            "experiment_id": "mcp-experiment-fixture",
            "point_count": len(values),
            "points": [
                cls.point(index, names[index], value)
                for index, value in enumerate(values)
            ],
        }

    @staticmethod
    def plan(*, population: str = "nominal") -> dict[str, object]:
        return {
            "definition": {
                "variables": [
                    {
                        "name": "R",
                        "distribution": "discrete",
                        "population": population,
                    }
                ],
                "correlations": [],
                "corner_axes": [],
            }
        }

    def compare(
        self,
        baseline_manifest: dict[str, object],
        candidate_manifest: dict[str, object],
        baseline_results: dict[str, object],
        candidate_results: dict[str, object],
        baseline_plan: dict[str, object] | None = None,
        candidate_plan: dict[str, object] | None = None,
    ):
        (self.baseline_dir / "results.json").write_text("baseline", encoding="utf-8")
        (self.candidate_dir / "results.json").write_text("candidate", encoding="utf-8")
        loaded = [
            (self.baseline_dir, baseline_manifest, baseline_results, {}),
            (self.candidate_dir, candidate_manifest, candidate_results, {}),
        ]
        plans = [baseline_plan or self.plan(), candidate_plan or self.plan()]
        with (
            patch.object(
                statistical_comparison.experiment_index,
                "load_terminal_experiment",
                side_effect=loaded,
            ),
            patch.object(
                statistical_comparison,
                "_load_plan",
                side_effect=plans,
            ),
        ):
            return statistical_comparison.build_statistical_comparison(
                self.runs, self.baseline_id, self.candidate_id
            )

    def test_same_plan_pairs_circuit_outcomes_and_writes_stable_artifacts(self) -> None:
        source = self.source()
        baseline_manifest = self.manifest(source)
        candidate_manifest = self.manifest(
            source, "R1 in out {R}\nC1 out 0 1p\n.end\n"
        )

        first = self.compare(
            baseline_manifest,
            candidate_manifest,
            self.results([1.0, 2.0]),
            self.results([1.5, 4.0]),
        )
        document = json.loads(Path(first["comparison_json"]).read_text())

        self.assertEqual(first["comparison_basis"], "paired_same_plan")
        self.assertEqual(first["attribution"], "paired_circuit_outcomes")
        self.assertFalse(first["sample_plan_changed"])
        self.assertTrue(first["circuit_changed"])
        self.assertEqual(first["paired_points"], 2)
        self.assertEqual(
            document["classification_transitions"],
            {"electrical_pass->electrical_failure": 1, "electrical_pass->electrical_pass": 1},
        )
        self.assertEqual(document["aggregate"]["yield_delta"], -0.5)
        self.assertTrue(Path(first["comparison_csv"]).is_file())
        report = Path(first["comparison_html"]).read_text(encoding="utf-8")
        self.assertIn("paired_same_plan", report)
        self.assertIn("Aggregate yield", report)
        self.assertIn("electrical_pass-&gt;electrical_failure", report)
        before = Path(first["comparison_json"]).read_bytes()
        repeated = self.compare(
            baseline_manifest,
            candidate_manifest,
            self.results([1.0, 2.0]),
            self.results([1.5, 4.0]),
        )
        self.assertEqual(repeated["comparison_id"], first["comparison_id"])
        self.assertEqual(Path(repeated["comparison_json"]).read_bytes(), before)

    def test_changed_plan_is_unpaired_and_both_changes_are_confounded(self) -> None:
        baseline_source = self.source("a")
        candidate_source = self.source("b")
        result = self.compare(
            self.manifest(baseline_source),
            self.manifest(candidate_source, "R1 in out {R}\nC1 out 0 1p\n.end\n"),
            self.results([1.0, 4.0]),
            self.results([1.0, 2.0, 4.0], ["1.1k", "2.1k", "3.1k"]),
        )

        self.assertEqual(result["comparison_basis"], "unpaired_population_summary")
        self.assertEqual(result["attribution"], "confounded_plan_and_circuit")
        self.assertEqual(result["paired_points"], 0)
        self.assertTrue(result["sample_plan_changed"])

    def test_incompatible_population_fails_before_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "population definitions"):
            self.compare(
                self.manifest(self.source("a")),
                self.manifest(self.source("b")),
                self.results([1.0]),
                self.results([1.0]),
                self.plan(population="nominal"),
                self.plan(population="different"),
            )
        self.assertFalse((self.runs / "statistical-comparisons").exists())

    def test_same_plan_rejects_changed_point_mapping(self) -> None:
        source = self.source()
        with self.assertRaisesRegex(ValueError, "exact point parameters"):
            self.compare(
                self.manifest(source),
                self.manifest(source),
                self.results([1.0], ["1k"]),
                self.results([1.0], ["2k"]),
            )


if __name__ == "__main__":
    unittest.main()
