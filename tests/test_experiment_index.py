from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import experiment_index


class ExperimentIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"
        self.runs.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def point(
        index: int,
        parameters: dict[str, str],
        *,
        passed: bool = True,
        measurement: float = 1.0,
    ) -> dict[str, object]:
        return {
            "index": index,
            "parameters": parameters,
            "run_dir": f"ignored-absolute-path-{index}",
            "simulation_status": "completed",
            "duration_seconds": 0.01,
            "measurements": {"gain": measurement},
            "analyses": [
                {
                    "name": "bandwidth",
                    "status": "completed",
                    "error": None,
                    "analysis": {
                        "all_passed": passed,
                        "results": [
                            {
                                "metric": "cutoff_frequency",
                                "value": 1200.0,
                                "unit": "Hz",
                                "threshold": {
                                    "operator": ">=",
                                    "target": 1000.0,
                                    "unit": "Hz",
                                },
                                "passed": passed,
                                "parameters": {"reference_frequency": 10.0},
                                "evidence": {},
                            }
                        ]
                    },
                }
            ],
            "all_passed": passed,
            "error": None,
        }

    def write_experiment(
        self,
        experiment_id: str,
        *,
        schema_version: int = 2,
        status: str = "completed",
        execution_mode: str | None = "independent",
        points: list[dict[str, object]] | None = None,
        created_at: str | None = None,
        parameter_values: dict[str, list[str]] | None = None,
    ) -> Path:
        experiment_dir = self.runs / experiment_id
        experiment_dir.mkdir()
        if parameter_values is None and points:
            parameter_values = {
                name: list(
                    dict.fromkeys(
                        str(point["parameters"][name]) for point in points
                    )
                )
                for name in ("R", "C")
            }
        values = parameter_values or {"R": ["1k", "2k"], "C": ["10n", "20n"]}
        definition: dict[str, object] = {
            "netlist_template": "R1 in out {R}\nC1 out 0 {C}\n.end\n",
            "parameters": [
                {"name": "R", "values": values["R"], "unit": "ohm"},
                {"name": "C", "values": values["C"], "unit": "F"},
            ],
            "derived_parameters": [],
            "reuse_cache": False,
        }
        if execution_mode is not None:
            definition["execution_mode"] = execution_mode
        point_values = [] if points is None else points
        passed_points = sum(bool(point["all_passed"]) for point in point_values)
        manifest: dict[str, object] = {
            "schema_version": schema_version,
            "experiment_id": experiment_id,
            "status": status,
            "definition": definition,
            "point_count": len(values["R"]) * len(values["C"]),
            "completed_points": len(point_values),
            "error_points": 0,
            "passed_points": passed_points,
            "failed_points": len(point_values) - passed_points,
            "all_passed": passed_points == len(point_values)
            if status == "completed"
            else None,
        }
        if schema_version == 2:
            manifest.update(
                engine_version=1,
                definition_hash=experiment_index._definition_hash(definition),
                created_at=created_at or "2026-08-24T12:00:00-07:00",
                updated_at=created_at or "2026-08-24T12:00:00-07:00",
                finished_points=len(point_values),
            )
        else:
            manifest["started_at"] = created_at or "2026-08-24T11:00:00-07:00"
        (experiment_dir / "experiment_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        if status in {"completed", "cancelled"} and points is not None:
            result = {
                "experiment_id": experiment_id,
                "status": status,
                "execution_mode": execution_mode or "independent",
                "parameter_order": ["R", "C"],
                "derived_parameter_order": [],
                "parameter_units": {"R": "ohm", "C": "F"},
                "point_count": manifest["point_count"],
                "completed_points": len(point_values),
                "error_points": 0,
                "passed_points": passed_points,
                "failed_points": len(point_values) - passed_points,
                "all_passed": status == "completed"
                and passed_points == len(point_values),
                "points": point_values,
                "native_batch": None,
            }
            (experiment_dir / "results.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
        return experiment_dir

    def test_indexes_schema_v1_v2_and_queries_exact_same_point_parameters(self) -> None:
        sync_id = "mcp-experiment-20260824-110000-000000"
        native_id = "mcp-experiment-20260824-120000-000000-abcd1234"
        running_id = "mcp-experiment-20260824-130000-000000-1234abcd"
        sync_dir = self.write_experiment(
            sync_id,
            schema_version=1,
            execution_mode=None,
            points=[
                self.point(0, {"R": "1k", "C": "10n"}),
                self.point(1, {"R": "1k", "C": "20n"}),
                self.point(2, {"R": "2k", "C": "10n"}),
                self.point(3, {"R": "2k", "C": "20n"}),
            ],
        )
        sync_manifest_path = sync_dir / "experiment_manifest.json"
        sync_manifest = json.loads(sync_manifest_path.read_text(encoding="utf-8"))
        for parameter in sync_manifest["definition"]["parameters"]:
            parameter.pop("unit")
        sync_manifest["definition"]["parameter_units"] = {"R": "", "C": ""}
        sync_manifest_path.write_text(json.dumps(sync_manifest), encoding="utf-8")
        sync_results_path = sync_dir / "results.json"
        sync_results = json.loads(sync_results_path.read_text(encoding="utf-8"))
        sync_results["parameter_units"] = {"R": "", "C": ""}
        sync_results_path.write_text(json.dumps(sync_results), encoding="utf-8")
        self.write_experiment(
            native_id,
            execution_mode="native",
            points=[self.point(0, {"R": "3k", "C": "30n"})],
        )
        self.write_experiment(
            running_id,
            status="running",
            execution_mode="independent",
            points=None,
            created_at="2026-08-24T13:00:00-07:00",
        )

        built = experiment_index.build_experiment_index(self.runs)

        self.assertEqual(built["scanned_experiments"], 3)
        self.assertEqual(built["indexed_experiments"], 3)
        self.assertEqual(built["result_experiments"], 2)
        self.assertEqual(built["indexed_points"], 5)
        self.assertEqual(built["issues"], [])
        all_records = experiment_index.query_experiments(self.runs)
        self.assertEqual(
            [record["experiment_id"] for record in all_records["experiments"]],
            [running_id, native_id, sync_id],
        )
        self.assertEqual(all_records["experiments"][0]["index_state"], "manifest_only")
        sync = all_records["experiments"][2]
        self.assertEqual(sync["execution_mode"], "independent")
        self.assertEqual(sync["measurement_names"], ["gain"])
        self.assertEqual(sync["requirement_metrics"], ["cutoff_frequency"])
        self.assertEqual([item["name"] for item in sync["parameters"]], ["R", "C"])

        native = experiment_index.query_experiments(
            self.runs, execution_mode="native"
        )
        self.assertEqual(native["total"], 1)
        self.assertEqual(native["experiments"][0]["experiment_id"], native_id)
        same_point = experiment_index.query_experiments(
            self.runs, parameters={"R": "1k", "C": "10n"}
        )
        self.assertEqual(
            [record["experiment_id"] for record in same_point["experiments"]],
            [sync_id],
        )
        cross_point = experiment_index.query_experiments(
            self.runs, parameters={"R": "1k", "C": "20n"}
        )
        self.assertEqual(
            [record["experiment_id"] for record in cross_point["experiments"]],
            [sync_id],
        )
        no_cross_match = experiment_index.query_experiments(
            self.runs, parameters={"R": "1k", "C": "30n"}
        )
        self.assertEqual(no_cross_match["total"], 0)
        injection = experiment_index.query_experiments(
            self.runs, parameters={"R": "1k' OR 1=1 --"}
        )
        self.assertEqual(injection["total"], 0)

    def test_indexes_cancelled_partial_results(self) -> None:
        experiment_id = "mcp-experiment-20260824-140000-000000-deadbeef"
        experiment_dir = self.write_experiment(
            experiment_id,
            status="cancelled",
            points=[self.point(0, {"R": "1k", "C": "10n"}, passed=False)],
            parameter_values={"R": ["1k", "2k"], "C": ["10n"]},
        )
        manifest_path = experiment_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            point_count=2,
            finished_points=1,
            completed_points=1,
            failed_points=1,
            all_passed=False,
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        results_path = experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results.update(point_count=2, all_passed=False)
        results_path.write_text(json.dumps(results), encoding="utf-8")

        built = experiment_index.build_experiment_index(self.runs)
        queried = experiment_index.query_experiments(
            self.runs, status="cancelled", all_passed=False
        )

        self.assertEqual(built["issue_count"], 0)
        self.assertEqual(queried["total"], 1)
        record = queried["experiments"][0]
        self.assertEqual(record["finished_points"], 1)
        self.assertEqual(record["point_count"], 2)

    def test_indexes_and_queries_statistical_definitions_and_summaries(self) -> None:
        experiment_id = "mcp-experiment-20260824-141000-000000-acde1234"
        points = [
            self.point(0, {"R": "1k", "C": "10n"}, passed=True),
            self.point(1, {"R": "1k", "C": "20n"}, passed=False),
            self.point(2, {"R": "2k", "C": "10n"}, passed=True),
            self.point(3, {"R": "2k", "C": "20n"}, passed=False),
        ]
        directory = self.write_experiment(experiment_id, points=points)
        manifest_path = directory / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = {
            "kind": "statistical",
            "sampling_method": "halton",
            "generator_version": "sha256-test-v1",
            "plan_id": "statistical-plan-aaaaaaaaaaaaaaaa",
            "plan_sha256": "a" * 64,
            "definition_hash": "b" * 64,
            "runs_relative_path": (
                "statistical-plans/statistical-plan-aaaaaaaaaaaaaaaa/"
                "statistical_plan.json"
            ),
            "corner_aggregate": True,
            "corner_axes": [
                {
                    "name": "process",
                    "parameter": "C",
                    "unit": "F",
                    "values": [
                        {"name": "fast", "value": "10n"},
                        {"name": "slow", "value": "20n"},
                    ],
                }
            ],
            "point_metadata": [
                {
                    "index": index,
                    "sample_index": index // 2,
                    "corners": {"process": "fast" if index % 2 == 0 else "slow"},
                }
                for index in range(4)
            ],
        }
        manifest["definition"] = {
            "netlist_template": "R1 in out {R}\nC1 out 0 {C}\n.end\n",
            "parameters": [],
            "derived_parameters": [],
            "parameter_order": ["R", "C"],
            "derived_parameter_order": [],
            "parameter_units": {"R": "ohm", "C": "F"},
            "point_plan": {
                "schema_version": 1,
                "points": [point["parameters"] for point in points],
                "source": source,
            },
            "execution_mode": "independent",
            "reuse_cache": False,
        }
        manifest["definition_hash"] = experiment_index._definition_hash(
            manifest["definition"]
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        built = experiment_index.build_experiment_index(self.runs)
        all_statistical = experiment_index.query_experiments(
            self.runs, statistical=True
        )

        self.assertEqual(built["issues"], [])
        self.assertEqual(all_statistical["total"], 1)
        record = all_statistical["experiments"][0]
        self.assertEqual(record["sampling_method"], "halton")
        self.assertEqual(record["observed_yield"], 0.5)
        self.assertLess(record["confidence_low"], 0.2)
        self.assertGreater(record["confidence_high"], 0.8)
        self.assertEqual(record["statistical_variables"], ["R"])
        self.assertEqual(
            record["statistical_corners"], {"process": ["fast", "slow"]}
        )
        self.assertEqual(
            [item["observed_yield"] for item in record["corner_summaries"]],
            [1.0, 0.0],
        )
        filters = [
            {"circuit_sha256": record["circuit_sha256"]},
            {"minimum_yield": 0.5},
            {"minimum_confidence_low": 0.1},
            {"corner": {"process": "slow"}},
            {"corner": {"process": "fast"}, "minimum_yield": 0.9},
            {
                "corner": {"process": "fast"},
                "minimum_confidence_low": 0.3,
            },
            {"variable": "R"},
            {"requirement_metric": "cutoff_frequency"},
        ]
        for arguments in filters:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    experiment_index.query_experiments(self.runs, **arguments)["total"],
                    1,
                )
        misses = [
            {"minimum_yield": 0.51},
            {"minimum_confidence_low": 0.2},
            {"corner": {"process": "typical"}},
            {"corner": {"process": "slow"}, "minimum_yield": 0.1},
            {"variable": "C"},
            {"requirement_metric": "rise_time"},
            {"statistical": False},
        ]
        for arguments in misses:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    experiment_index.query_experiments(self.runs, **arguments)["total"],
                    0,
                )

    def test_schema_v2_definition_hash_is_verified(self) -> None:
        experiment_id = "mcp-experiment-20260824-125000-000000-aabbccdd"
        directory = self.write_experiment(
            experiment_id,
            schema_version=2,
            points=[self.point(0, {"R": "1k", "C": "10n"})],
            parameter_values={"R": ["1k"], "C": ["10n"]},
        )
        manifest_path = directory / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["definition"]["netlist_template"] = "tampered\n.end\n"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        built = experiment_index.build_experiment_index(self.runs)

        self.assertEqual(built["indexed_experiments"], 0)
        self.assertEqual(built["issues"][0]["code"], "invalid_manifest")
        self.assertIn("definition_hash", built["issues"][0]["message"])

    def test_malformed_artifacts_are_isolated_and_reported(self) -> None:
        valid_id = "mcp-experiment-20260824-150000-000000-feedface"
        invalid_id = "mcp-experiment-20260824-150100-000000-bad0cafe"
        corrupt_id = "mcp-experiment-20260824-150200-000000-cafebabe"
        oversized_id = "mcp-experiment-20260824-150300-000000-deadc0de"
        self.write_experiment(valid_id, status="defined", points=None)
        invalid_dir = self.write_experiment(
            invalid_id,
            points=[self.point(0, {"R": "1k", "C": "10n"})],
        )
        results_path = invalid_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["points"][0]["measurements"]["gain"] = float("nan")
        results_path.write_text(json.dumps(results), encoding="utf-8")
        corrupt_dir = self.runs / corrupt_id
        corrupt_dir.mkdir()
        (corrupt_dir / "experiment_manifest.json").write_text("not json", encoding="utf-8")
        oversized_dir = self.write_experiment(oversized_id, status="defined", points=None)
        oversized_path = oversized_dir / "experiment_manifest.json"
        oversized = json.loads(oversized_path.read_text(encoding="utf-8"))
        oversized["point_count"] = 10**100
        oversized_path.write_text(json.dumps(oversized), encoding="utf-8")

        built = experiment_index.build_experiment_index(self.runs)
        queried = experiment_index.query_experiments(self.runs)

        self.assertEqual(built["scanned_experiments"], 4)
        self.assertEqual(built["indexed_experiments"], 2)
        self.assertEqual(built["issue_count"], 3)
        self.assertEqual(
            {issue["code"] for issue in built["issues"]},
            {"invalid_manifest", "invalid_results"},
        )
        states = {
            record["experiment_id"]: record["index_state"]
            for record in queried["experiments"]
        }
        self.assertEqual(states[valid_id], "manifest_only")
        self.assertEqual(states[invalid_id], "invalid_results")
        self.assertEqual(
            experiment_index.query_experiments(self.runs, all_passed=True)["total"],
            0,
        )
        connection = sqlite3.connect(built["database_path"])
        try:
            issues = connection.execute(
                "SELECT code FROM index_issues ORDER BY code"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            issues,
            [("invalid_manifest",), ("invalid_manifest",), ("invalid_results",)],
        )

    def test_cross_artifact_and_requirement_pass_counts_fail_closed(self) -> None:
        manifest_mismatch_id = "mcp-experiment-20260824-151000-000000-aabbccdd"
        requirement_mismatch_id = "mcp-experiment-20260824-151100-000000-ddccbbaa"
        manifest_dir = self.write_experiment(
            manifest_mismatch_id,
            points=[self.point(0, {"R": "1k", "C": "10n"})],
        )
        manifest_path = manifest_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["passed_points"] = 0
        manifest["failed_points"] = 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        requirement_dir = self.write_experiment(
            requirement_mismatch_id,
            points=[self.point(0, {"R": "1k", "C": "10n"})],
        )
        results_path = requirement_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["points"][0]["analyses"][0]["analysis"]["results"][0][
            "passed"
        ] = False
        results_path.write_text(json.dumps(results), encoding="utf-8")

        built = experiment_index.build_experiment_index(self.runs)
        queried = experiment_index.query_experiments(self.runs)

        self.assertEqual(built["issue_count"], 2)
        self.assertEqual(
            {record["index_state"] for record in queried["experiments"]},
            {"invalid_results"},
        )
        self.assertTrue(
            any("manifest passed_points" in issue["message"] for issue in built["issues"])
        )
        self.assertTrue(
            any("analysis all_passed" in issue["message"] for issue in built["issues"])
        )

    def test_rebuild_replaces_stale_data_and_replace_failure_preserves_old_index(
        self,
    ) -> None:
        experiment_id = "mcp-experiment-20260824-160000-000000-0123abcd"
        experiment_dir = self.write_experiment(
            experiment_id,
            points=[self.point(0, {"R": "1k", "C": "10n"})],
        )
        first = experiment_index.build_experiment_index(self.runs)
        self.assertEqual(experiment_index.query_experiments(self.runs)["total"], 1)
        manifest_path = experiment_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "failed"
        manifest["all_passed"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (experiment_dir / "results.json").unlink()

        experiment_index.build_experiment_index(self.runs)
        updated = experiment_index.query_experiments(self.runs)
        self.assertEqual(updated["experiments"][0]["status"], "failed")
        before_failure = Path(first["database_path"]).read_bytes()
        with patch.object(experiment_index.os, "replace", side_effect=OSError("busy")):
            with self.assertRaisesRegex(OSError, "busy"):
                experiment_index.build_experiment_index(self.runs)
        self.assertEqual(Path(first["database_path"]).read_bytes(), before_failure)
        self.assertEqual(
            list(self.runs.glob(".experiments.sqlite3.*.tmp")),
            [],
        )
        still_queryable = experiment_index.query_experiments(self.runs)
        self.assertEqual(still_queryable["experiments"][0]["status"], "failed")

    def test_rejects_invalid_queries_and_confines_index_path(self) -> None:
        experiment_index.build_experiment_index(self.runs)
        invalid_arguments = [
            {"limit": True},
            {"limit": 0},
            {"limit": 1001},
            {"offset": True},
            {"offset": -1},
            {"status": "unknown"},
            {"execution_mode": "automatic"},
            {"all_passed": 1},
            {"parameters": {}},
            {"parameters": {"R": 1000}},
            {"circuit_sha256": "ABC"},
            {"statistical": 1},
            {"minimum_yield": -0.1},
            {"minimum_confidence_low": float("nan")},
            {"corner": {}},
            {"corner": {"load": 1}},
            {"variable": ""},
            {"requirement_metric": 1},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                experiment_index.query_experiments(self.runs, **arguments)
        outside = self.runs.parent / "outside.sqlite3"
        with self.assertRaisesRegex(ValueError, "inside the runs directory"):
            experiment_index.build_experiment_index(self.runs, outside)

    def test_symlinked_experiment_escape_is_reported(self) -> None:
        outside = self.runs.parent / "outside"
        outside.mkdir()
        experiment_id = "mcp-experiment-20260824-170000-000000-abcdef12"
        outside_experiment = outside / experiment_id
        outside_experiment.mkdir()
        (outside_experiment / "experiment_manifest.json").write_text("{}", encoding="utf-8")
        try:
            os.symlink(outside_experiment, self.runs / experiment_id, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        built = experiment_index.build_experiment_index(self.runs)

        self.assertEqual(built["indexed_experiments"], 0)
        self.assertEqual(built["issue_count"], 1)
        self.assertEqual(built["issues"][0]["code"], "invalid_manifest")

    def test_symlinked_manifest_escape_is_rejected_before_read(self) -> None:
        experiment_id = "mcp-experiment-20260824-171000-000000-fedcba98"
        experiment_dir = self.runs / experiment_id
        experiment_dir.mkdir()
        outside_manifest = self.runs.parent / "outside-manifest.json"
        outside_manifest.write_text("{}", encoding="utf-8")
        manifest_path = experiment_dir / "experiment_manifest.json"
        try:
            os.symlink(outside_manifest, manifest_path)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")

        with patch.object(experiment_index, "_load_json") as load_json:
            built = experiment_index.build_experiment_index(self.runs)

        load_json.assert_not_called()
        self.assertEqual(built["indexed_experiments"], 0)
        self.assertEqual(built["issues"][0]["code"], "invalid_manifest")


if __name__ == "__main__":
    unittest.main()
