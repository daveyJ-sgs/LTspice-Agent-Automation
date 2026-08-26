from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adaptive_boundary
import worst_case_analysis


class FakeManager:
    def __init__(self) -> None:
        self.defined: list[dict[str, object]] = []
        self.statuses: dict[str, str] = {}

    def define_explicit(self, *args: object) -> dict[str, object]:
        experiment_id = f"mcp-experiment-child-{len(self.defined)}"
        self.defined.append(
            {
                "experiment_id": experiment_id,
                "netlist_template": args[0],
                "parameter_order": args[1],
                "points": args[2],
                "parameter_units": args[3],
                "source": args[4],
            }
        )
        self.statuses[experiment_id] = "defined"
        return {"experiment_id": experiment_id}

    def start(self, experiment_id: str) -> dict[str, object]:
        self.statuses[experiment_id] = "queued"
        return {"experiment_id": experiment_id, "status": "queued"}

    def snapshot(self, experiment_id: str) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "status": self.statuses[experiment_id],
        }


class FailFirstStartManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0

    def start(self, experiment_id: str) -> dict[str, object]:
        self.start_calls += 1
        if self.start_calls == 1:
            raise RuntimeError("interrupted before queueing")
        return super().start(experiment_id)


class AdaptiveBoundaryTests(unittest.TestCase):
    @staticmethod
    def check_id() -> str:
        return worst_case_analysis._identity(
            "response", "margin", ">=", 0.0, "dB", {}
        )

    @staticmethod
    def point(index: int, x: str, value: float) -> dict[str, object]:
        passed = value >= 0
        return {
            "index": index,
            "parameters": {"X": x, "FIXED": "10"},
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
                                "metric": "margin",
                                "value": value,
                                "unit": "dB",
                                "passed": passed,
                                "parameters": {},
                                "threshold": {
                                    "operator": ">=",
                                    "target": 0,
                                    "unit": "dB",
                                },
                            }
                        ],
                        "all_passed": passed,
                    },
                }
            ],
            "all_passed": passed,
            "error": None,
        }

    def source(self) -> tuple[dict[str, object], dict[str, object]]:
        manifest = {
            "definition": {
                "parameter_order": ["X", "FIXED"],
                "parameter_units": {"X": "V", "FIXED": "ohm"},
                "netlist_template": "B1 out 0 V={X}\n.end\n",
                "waveform_analyses": [],
                "filename": "circuit.cir",
                "ascii_raw": False,
                "timeout_seconds": 120,
            }
        }
        results = {
            "experiment_id": "mcp-experiment-source",
            "point_count": 2,
            "points": [self.point(0, "0", -1), self.point(1, "1", 1)],
        }
        return manifest, results

    def child_artifacts(
        self,
        runs: Path,
        definition: dict[str, object],
        values: list[float] | None = None,
    ) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
        experiment_id = str(definition["experiment_id"])
        points = definition["points"]
        assert isinstance(points, list)
        if values is None:
            values = [float(point["X"]) - 0.6 for point in points]
        manifest = {
            "definition": {
                "point_plan": {
                    "points": points,
                    "source": definition["source"],
                }
            }
        }
        results = {
            "experiment_id": experiment_id,
            "point_count": len(points),
            "points": [
                self.point(index, str(point["X"]), values[index])
                for index, point in enumerate(points)
            ],
        }
        return runs / experiment_id, manifest, results, {}

    def test_batched_refinement_resumes_and_stops_at_tolerance(self) -> None:
        source_manifest, source_results = self.source()
        manager = FakeManager()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                defined = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                    batch_size=3,
                    max_samples=6,
                    input_tolerance=0.3,
                )

            launched = adaptive_boundary.advance_adaptive_boundary_study(
                runs, defined["adaptive_id"], manager
            )
            self.assertEqual(launched["status"], "running")
            self.assertEqual(manager.defined[0]["points"], [
                {"X": "0.25", "FIXED": "10"},
                {"X": "0.5", "FIXED": "10"},
                {"X": "0.75", "FIXED": "10"},
            ])
            child_id = str(manager.defined[0]["experiment_id"])
            manager.statuses[child_id] = "completed"

            def load(experiment_root: Path, experiment_id: str):
                self.assertEqual(experiment_root, runs)
                self.assertEqual(experiment_id, child_id)
                return self.child_artifacts(runs, manager.defined[0])

            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                side_effect=load,
            ):
                completed = adaptive_boundary.advance_adaptive_boundary_study(
                    runs, defined["adaptive_id"], manager
                )

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["stop_reason"], "input_tolerance")
            self.assertEqual(completed["sample_count"], 3)
            self.assertEqual(completed["batch_count"], 1)
            self.assertAlmostEqual(completed["current_width"], 0.25)
            reloaded = adaptive_boundary.get_adaptive_boundary_study(
                runs, defined["adaptive_id"]
            )
            self.assertEqual(reloaded, completed)

    def test_defined_active_batch_restarts_after_interrupted_start(self) -> None:
        source_manifest, source_results = self.source()
        manager = FailFirstStartManager()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                defined = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                    batch_size=1,
                    max_samples=1,
                    input_tolerance=1e-9,
                )

            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                adaptive_boundary.advance_adaptive_boundary_study(
                    runs, defined["adaptive_id"], manager
                )
            child_id = str(manager.defined[0]["experiment_id"])
            self.assertEqual(manager.statuses[child_id], "defined")

            resumed = adaptive_boundary.advance_adaptive_boundary_study(
                runs, defined["adaptive_id"], manager
            )

            self.assertEqual(resumed["status"], "running")
            self.assertEqual(manager.statuses[child_id], "queued")
            self.assertEqual(manager.start_calls, 2)

    def test_sample_budget_and_definition_are_deterministic(self) -> None:
        source_manifest, source_results = self.source()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                first = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                    batch_size=3,
                    max_samples=3,
                    input_tolerance=1e-9,
                )
                second = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                    batch_size=3,
                    max_samples=3,
                    input_tolerance=1e-9,
                )
            self.assertEqual(first["adaptive_id"], second["adaptive_id"])

            manager = FakeManager()
            adaptive_boundary.advance_adaptive_boundary_study(
                runs, first["adaptive_id"], manager
            )
            child_id = str(manager.defined[0]["experiment_id"])
            manager.statuses[child_id] = "completed"
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=self.child_artifacts(runs, manager.defined[0]),
            ):
                completed = adaptive_boundary.advance_adaptive_boundary_study(
                    runs, first["adaptive_id"], manager
                )
            self.assertEqual(completed["stop_reason"], "sample_budget")

    def test_nonmonotonic_boundary_fails_closed(self) -> None:
        source_manifest, source_results = self.source()
        manager = FakeManager()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                defined = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                    batch_size=3,
                    max_samples=6,
                    input_tolerance=1e-9,
                )
            adaptive_boundary.advance_adaptive_boundary_study(
                runs, defined["adaptive_id"], manager
            )
            child_id = str(manager.defined[0]["experiment_id"])
            manager.statuses[child_id] = "completed"
            artifacts = self.child_artifacts(
                runs, manager.defined[0], values=[1, -1, 1]
            )
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=artifacts,
            ):
                failed = adaptive_boundary.advance_adaptive_boundary_study(
                    runs, defined["adaptive_id"], manager
                )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["stop_reason"], "invalid_boundary_evidence")
            self.assertIn("not a single monotonic boundary", str(failed["error"]))

    def test_source_points_must_be_one_dimensional_opposite_outcomes(self) -> None:
        source_manifest, source_results = self.source()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                source_results["points"][1]["parameters"]["FIXED"] = "changed"
                with self.assertRaisesRegex(ValueError, "differ only"):
                    adaptive_boundary.define_adaptive_boundary_study(
                        runs,
                        "mcp-experiment-source",
                        0,
                        1,
                        self.check_id(),
                        "X",
                    )

    def test_manifest_symlink_is_rejected(self) -> None:
        source_manifest, source_results = self.source()
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            runs.mkdir()
            with patch.object(
                adaptive_boundary.experiment_index,
                "load_completed_experiment",
                return_value=(runs / "source", source_manifest, source_results, {}),
            ):
                defined = adaptive_boundary.define_adaptive_boundary_study(
                    runs,
                    "mcp-experiment-source",
                    0,
                    1,
                    self.check_id(),
                    "X",
                )
            path = Path(defined["manifest"])
            outside = runs / "outside.json"
            outside.write_text("preserve", encoding="utf-8")
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                adaptive_boundary.get_adaptive_boundary_study(
                    runs, defined["adaptive_id"]
                )
