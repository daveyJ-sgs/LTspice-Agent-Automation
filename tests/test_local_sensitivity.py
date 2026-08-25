from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_sensitivity


class LocalSensitivityTests(unittest.TestCase):
    @staticmethod
    def point(
        index: int,
        parameters: dict[str, str],
        value: float,
        *,
        status: str = "completed",
    ) -> dict[str, object]:
        return {
            "index": index,
            "parameters": parameters,
            "run_dir": f"point-{index:04d}",
            "simulation_status": status,
            "measurements": {},
            "analyses": []
            if status != "completed"
            else [
                {
                    "name": "response",
                    "status": "completed",
                    "analysis": {
                        "results": [
                            {
                                "metric": "gain",
                                "value": value,
                                "unit": "dB",
                                "passed": value >= 0,
                                "parameters": {},
                                "threshold": {
                                    "operator": ">=",
                                    "target": 0,
                                    "unit": "dB",
                                },
                            }
                        ],
                        "all_passed": value >= 0,
                    },
                }
            ],
            "all_passed": status == "completed" and value >= 0,
            "error": None if status == "completed" else "simulation failed",
        }

    @staticmethod
    def statistical_plan() -> dict[str, object]:
        return {
            "schema_version": 1,
            "generator_version": "sha256-stratified-gaussian-v7",
            "definition_hash": "a" * 64,
            "definition": {
                "sample_count": 1,
                "seed": 1,
                "sampling_method": "halton",
                "variables": [
                    {"name": "A", "distribution": "uniform", "unit": "V"},
                    {"name": "B", "distribution": "discrete", "unit": ""},
                    {"name": "C", "distribution": "uniform", "unit": "A"},
                ],
            },
            "parameter_order": ["A", "B", "C", "LOAD"],
            "parameter_units": {"A": "V", "B": "", "C": "A", "LOAD": "ohm"},
            "sample_count": 1,
            "points": [
                {
                    "index": 0,
                    "sample_index": 0,
                    "corners": {"load": "nominal"},
                    "parameters": {
                        "A": "10",
                        "B": "fast",
                        "C": "0",
                        "LOAD": "1000",
                    },
                }
            ],
        }

    def test_prepares_content_addressed_oat_points_from_real_evidence(self) -> None:
        source_experiment_id = "mcp-experiment-20260825-090000-000000-a1b2c3d4"
        statistical_plan = self.statistical_plan()
        plan_artifact = b"statistical plan\n"
        digest = hashlib.sha256(plan_artifact).hexdigest()
        plan_id = f"statistical-plan-{digest[:16]}"
        provenance = {
            "kind": "statistical",
            "sampling_method": "halton",
            "generator_version": statistical_plan["generator_version"],
            "plan_id": plan_id,
            "plan_sha256": digest,
            "definition_hash": statistical_plan["definition_hash"],
            "runs_relative_path": f"statistical-plans/{plan_id}/statistical_plan.json",
        }
        source_manifest = {
            "definition": {
                "netlist_template": "R1 in out {A}\n.end\n",
                "waveform_analyses": [],
                "filename": "circuit.cir",
                "ascii_raw": False,
                "timeout_seconds": 120,
                "point_plan": {"source": provenance},
            }
        }
        baseline_parameters = statistical_plan["points"][0]["parameters"]
        source_results = {
            "experiment_id": source_experiment_id,
            "point_count": 1,
            "points": [self.point(0, baseline_parameters, 1)],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            source_dir = runs / source_experiment_id
            source_dir.mkdir(parents=True)
            plan_path = runs / provenance["runs_relative_path"]
            plan_path.parent.mkdir(parents=True)
            plan_path.write_bytes(plan_artifact)
            with (
                patch.object(
                    local_sensitivity.experiment_index,
                    "load_completed_experiment",
                    return_value=(source_dir, source_manifest, source_results, {}),
                ),
                patch.object(
                    local_sensitivity.statistical_engine,
                    "load_statistical_plan",
                    return_value=statistical_plan,
                ),
            ):
                prepared = local_sensitivity.prepare_local_sensitivity_study(
                    runs, source_experiment_id, 0, 0.1
                )
                repeated = local_sensitivity.prepare_local_sensitivity_study(
                    runs, source_experiment_id, 0, 0.1
                )

            self.assertEqual(prepared["points"][0], baseline_parameters)
            self.assertEqual(prepared["points"][1]["A"], "9")
            self.assertEqual(prepared["points"][2]["A"], "11")
            self.assertEqual(len(prepared["points"]), 3)
            self.assertEqual(
                prepared["source"]["skipped_variables"],
                [
                    {"name": "B", "reason": "non_numeric"},
                    {"name": "C", "reason": "zero_baseline"},
                ],
            )
            self.assertEqual(
                prepared["source"]["plan_id"], repeated["source"]["plan_id"]
            )
            persisted = runs / prepared["source"]["runs_relative_path"]
            self.assertTrue(persisted.is_file())
            self.assertEqual(
                hashlib.sha256(persisted.read_bytes()).hexdigest(),
                prepared["source"]["plan_sha256"],
            )

    @staticmethod
    def local_plan() -> dict[str, object]:
        points = [
            {"A": "10", "B": "20"},
            {"A": "9", "B": "20"},
            {"A": "11", "B": "20"},
            {"A": "10", "B": "18"},
            {"A": "10", "B": "22"},
        ]
        perturbations = [
            {
                "name": "A",
                "unit": "V",
                "baseline": "10",
                "low": "9",
                "high": "11",
                "delta": "1",
                "low_point_index": 1,
                "high_point_index": 2,
            },
            {
                "name": "B",
                "unit": "ohm",
                "baseline": "20",
                "low": "18",
                "high": "22",
                "delta": "2",
                "low_point_index": 3,
                "high_point_index": 4,
            },
        ]
        return {
            "schema_version": 1,
            "source_experiment_id": "mcp-experiment-source",
            "source_point_index": 7,
            "source_sample_index": 2,
            "source_corners": {"load": "weak"},
            "source_sampling_provenance": {},
            "relative_step": "0.1",
            "parameter_order": ["A", "B"],
            "parameter_units": {"A": "V", "B": "ohm"},
            "baseline_parameters": points[0],
            "perturbations": perturbations,
            "skipped_variables": [],
            "points": points,
            "point_metadata": [
                {"index": 0, "role": "baseline", "variable": None, "direction": None},
                {
                    "index": 1,
                    "role": "perturbation",
                    "variable": "A",
                    "direction": "low",
                },
                {
                    "index": 2,
                    "role": "perturbation",
                    "variable": "A",
                    "direction": "high",
                },
                {
                    "index": 3,
                    "role": "perturbation",
                    "variable": "B",
                    "direction": "low",
                },
                {
                    "index": 4,
                    "role": "perturbation",
                    "variable": "B",
                    "direction": "high",
                },
            ],
        }

    def test_builds_ranked_tornado_effects_and_keeps_incomplete_rows(self) -> None:
        plan = self.local_plan()
        values = [5, 3, 7, 4, 6]
        points = [
            self.point(index, parameters, values[index])
            for index, parameters in enumerate(plan["points"])
        ]
        results = {
            "experiment_id": "mcp-experiment-local",
            "point_count": 5,
            "points": points,
        }

        analysis = local_sensitivity.build_tornado_analysis(results, plan)

        effects = analysis["requirements"][0]["effects"]
        self.assertEqual(analysis["complete_effects"], 2)
        self.assertEqual([effect["name"] for effect in effects], ["A", "B"])
        self.assertEqual([effect["rank"] for effect in effects], [1, 2])
        self.assertEqual(effects[0]["low_effect"], -2)
        self.assertEqual(effects[0]["high_effect"], 2)
        self.assertEqual(effects[0]["low_slope"], 2)
        self.assertEqual(effects[0]["high_slope"], 2)
        self.assertEqual(effects[0]["impact"], 2)

        results["points"][4] = self.point(
            4, plan["points"][4], 0, status="failed"
        )
        incomplete = local_sensitivity.build_tornado_analysis(results, plan)
        effect_b = next(
            effect
            for effect in incomplete["requirements"][0]["effects"]
            if effect["name"] == "B"
        )
        self.assertEqual(incomplete["invalid_points"], 1)
        self.assertEqual(incomplete["complete_effects"], 1)
        self.assertEqual(effect_b["status"], "incomplete")
        self.assertIsNone(effect_b["rank"])
        self.assertIsNone(effect_b["high_effect"])

    def test_terminal_local_study_writes_portable_artifacts(self) -> None:
        plan = self.local_plan()
        results = {
            "experiment_id": "mcp-experiment-local",
            "point_count": 5,
            "points": [
                self.point(index, parameters, value)
                for index, (parameters, value) in enumerate(
                    zip(plan["points"], [5, 3, 7, 4, 6])
                )
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            experiment_dir = runs / results["experiment_id"]
            experiment_dir.mkdir(parents=True)
            plan_id, plan_path, digest = local_sensitivity._save_local_plan(runs, plan)
            source = {
                "kind": "local_sensitivity",
                "plan_id": plan_id,
                "plan_sha256": digest,
                "runs_relative_path": plan_path.resolve().relative_to(
                    runs.resolve()
                ).as_posix(),
                "source_experiment_id": plan["source_experiment_id"],
                "source_point_index": plan["source_point_index"],
                "source_sample_index": plan["source_sample_index"],
                "source_corners": plan["source_corners"],
                "relative_step": plan["relative_step"],
                "perturbations": plan["perturbations"],
                "skipped_variables": plan["skipped_variables"],
                "point_metadata": plan["point_metadata"],
            }
            manifest = {
                "definition": {
                    "point_plan": {"points": plan["points"], "source": source}
                }
            }
            with patch.object(
                local_sensitivity.experiment_index,
                "load_terminal_experiment",
                return_value=(experiment_dir, manifest, results, {}),
            ):
                summary = local_sensitivity.analyze_local_sensitivity(
                    runs, str(results["experiment_id"])
                )

            persisted = json.loads(
                Path(summary["tornado_json"]).read_text(encoding="utf-8")
            )
            rows = list(
                csv.DictReader(
                    io.StringIO(
                        Path(summary["tornado_csv"]).read_text(encoding="utf-8"),
                        newline="",
                    )
                )
            )
            self.assertEqual(persisted["plan_sha256"], digest)
            self.assertEqual(persisted["source_sampling_provenance"], {})
            self.assertEqual(len(rows), 2)
            self.assertEqual(summary["complete_effects"], 2)

    def test_local_plan_and_output_symlinks_are_rejected(self) -> None:
        plan = self.local_plan()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            plan_id, plan_path, digest = local_sensitivity._save_local_plan(runs, plan)
            outside = runs / "outside.json"
            outside.write_text("preserve", encoding="utf-8")
            plan_path.unlink()
            plan_path.symlink_to(outside)
            source = {
                **plan,
                "plan_id": plan_id,
                "plan_sha256": digest,
                "runs_relative_path": plan_path.parent.resolve().relative_to(
                    runs.resolve()
                ).joinpath(plan_path.name).as_posix(),
            }
            with self.assertRaisesRegex(ValueError, "artifact is invalid"):
                local_sensitivity._load_local_plan(runs, source)

            target = runs / "tornado.json"
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                local_sensitivity._write_atomic(target, "changed")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
