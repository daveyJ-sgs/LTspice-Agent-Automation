from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server
import experiment_engine
import optimization_engine
import optimization_study


class FakeExperimentManager:
    def __init__(self, runs: Path) -> None:
        self.runs = runs
        self.statuses: dict[str, str] = {}
        self.counter = 0

    def define_explicit(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        experiment_id = f"mcp-experiment-fake-{self.counter}"
        self.counter += 1
        experiment_dir = self.runs / experiment_id
        experiment_dir.mkdir(parents=True)
        manifest = experiment_dir / "experiment_manifest.json"
        manifest.write_text(
            json.dumps({"definition_hash": f"hash-{experiment_id}"}),
            encoding="utf-8",
        )
        self.statuses[experiment_id] = "defined"
        return {
            "experiment_id": experiment_id,
            "manifest": str(manifest),
        }

    def snapshot(self, experiment_id: str) -> dict[str, object]:
        manifest_path = self.runs / experiment_id / "experiment_manifest.json"
        return {
            "experiment_id": experiment_id,
            "status": self.statuses[experiment_id],
            "manifest": str(manifest_path),
            "experiment_dir": str(self.runs / experiment_id),
            "results_json": str(self.runs / experiment_id / "results.json"),
            "results_csv": str(self.runs / experiment_id / "results.csv"),
            "point_count": 1,
            "finished_points": 0,
            "pending_points": 1,
            "running_points": 0,
            "completed_points": 0,
            "error_points": 0,
            "passed_points": 0,
            "failed_points": 0,
            "all_passed": None,
            "error": None,
            "execution_mode": "independent",
        }

    def definition_hash(self, experiment_id: str) -> str:
        manifest_path = self.runs / experiment_id / "experiment_manifest.json"
        return str(
            json.loads(manifest_path.read_text(encoding="utf-8"))["definition_hash"]
        )

    def start(self, experiment_id: str) -> dict[str, object]:
        if self.statuses[experiment_id] == "defined":
            self.statuses[experiment_id] = "queued"
        return self.snapshot(experiment_id)

    def cancel(self, experiment_id: str) -> dict[str, object]:
        self.statuses[experiment_id] = "cancelled"
        return self.snapshot(experiment_id)


class OptimizationStudyManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"
        self.plan = optimization_engine.generate_optimization_plan(
            self.runs,
            [
                {
                    "name": "R",
                    "kind": "continuous",
                    "minimum": 1,
                    "maximum": 2,
                    "count": 2,
                }
            ],
            [
                {
                    "name": "gain",
                    "experiment": "ac",
                    "analysis": "response",
                    "metric": "ac_gain_db",
                    "goal": "maximize",
                },
                {
                    "name": "settling",
                    "experiment": "transient",
                    "analysis": "response",
                    "metric": "settling_time",
                    "goal": "minimize",
                },
            ],
            [
                {
                    "name": "minimum_gain",
                    "experiment": "ac",
                    "analysis": "response",
                    "metric": "ac_gain_db",
                    "operator": ">=",
                    "target": -100,
                }
            ],
        )
        self.experiments = {
            "ac": {"netlist_template": "R1 in 0 {R}\n.end\n"},
            "transient": {"netlist_template": "R1 in 0 {R}\n.end\n"},
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _evaluator(
        self, runs: Path, plan_id: str, experiments: dict[str, str]
    ) -> dict[str, object]:
        self.assertEqual(plan_id, self.plan["plan_id"])
        self.assertEqual(set(experiments), {"ac", "transient"})
        study_id = "optimization-study-0123456789abcdef"
        study_dir = runs / "optimization-studies" / study_id
        study_dir.mkdir(parents=True)
        for name in ("optimization_results.json", "optimization_results.csv", "report.html"):
            (study_dir / name).write_text(name, encoding="utf-8")
        return {
            "study_id": study_id,
            "plan_id": plan_id,
            "study_dir": str(study_dir),
            "results_json": str(study_dir / "optimization_results.json"),
            "results_csv": str(study_dir / "optimization_results.csv"),
            "report_html": str(study_dir / "report.html"),
            "candidate_count": 2,
            "feasible_candidates": 2,
            "constraint_failed_candidates": 0,
            "invalid_candidates": 0,
            "pareto_candidates": 1,
            "selected_candidate_index": 0,
            "selection_explanation": "fixture",
        }

    def test_define_persists_only_portable_child_and_plan_identities(self) -> None:
        children = FakeExperimentManager(self.runs)
        manager = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        defined = manager.define(self.plan["plan_id"], self.experiments)

        self.assertEqual(defined["status"], "defined")
        document = Path(defined["manifest"]).read_text(encoding="utf-8")
        self.assertNotIn(str(self.runs), document)
        manifest = json.loads(document)
        self.assertEqual(manifest["definition"]["plan_id"], self.plan["plan_id"])
        self.assertEqual(
            set(manifest["definition"]["experiments"]), {"ac", "transient"}
        )

    def test_start_restart_snapshot_and_evaluate_are_idempotent(self) -> None:
        children = FakeExperimentManager(self.runs)
        first = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        defined = first.define(self.plan["plan_id"], self.experiments)
        queued = first.start(defined["optimization_job_id"])
        self.assertEqual(queued["status"], "queued")

        restarted = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        resumed = restarted.start(defined["optimization_job_id"])
        self.assertEqual(resumed["status"], "queued")
        for child in resumed["experiments"].values():
            children.statuses[child["experiment_id"]] = "completed"

        completed = restarted.snapshot(defined["optimization_job_id"])
        manifest = Path(completed["manifest"])
        modified_at = manifest.stat().st_mtime_ns
        repeated = restarted.snapshot(defined["optimization_job_id"])
        self.assertEqual(completed, repeated)
        self.assertEqual(manifest.stat().st_mtime_ns, modified_at)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            completed["optimization_study_id"],
            "optimization-study-0123456789abcdef",
        )
        self.assertTrue(Path(str(completed["report_html"])).is_file())

    def test_cancel_is_cooperative_and_terminal(self) -> None:
        children = FakeExperimentManager(self.runs)
        manager = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        defined = manager.define(self.plan["plan_id"], self.experiments)
        manager.start(defined["optimization_job_id"])
        cancelled = manager.cancel(defined["optimization_job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            {child["status"] for child in cancelled["experiments"].values()},
            {"cancelled"},
        )

    def test_rejects_wrong_experiment_set_and_tampered_child(self) -> None:
        children = FakeExperimentManager(self.runs)
        manager = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            manager.define(self.plan["plan_id"], {"ac": self.experiments["ac"]})

        defined = manager.define(self.plan["plan_id"], self.experiments)
        child = next(iter(defined["experiments"].values()))
        manifest_path = Path(str(child["manifest"]))
        manifest_path.write_text(
            json.dumps({"definition_hash": "tampered"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "definition hash"):
            manager.snapshot(defined["optimization_job_id"])

    def test_manifest_relocates_without_rewriting_machine_paths(self) -> None:
        children = FakeExperimentManager(self.runs)
        manager = optimization_study.OptimizationStudyManager(
            self.runs, children, self._evaluator  # type: ignore[arg-type]
        )
        defined = manager.define(self.plan["plan_id"], self.experiments)
        manager.start(defined["optimization_job_id"])

        relocated = Path(self.temporary_directory.name) / "windows-runs"
        shutil.copytree(self.runs, relocated)
        relocated_children = FakeExperimentManager(relocated)
        relocated_children.statuses = dict(children.statuses)
        resumed = optimization_study.OptimizationStudyManager(
            relocated, relocated_children, self._evaluator  # type: ignore[arg-type]
        ).snapshot(defined["optimization_job_id"])

        self.assertEqual(resumed["status"], "queued")
        self.assertTrue(
            Path(resumed["manifest"]).resolve().is_relative_to(relocated.resolve())
        )

    def test_mcp_lifecycle_tools_use_the_shared_experiment_manager(self) -> None:
        children = FakeExperimentManager(self.runs)
        with (
            patch.object(mcp_server, "RUNS_DIR", self.runs),
            patch.object(mcp_server, "_optimization_study_manager", None),
            patch.object(mcp_server, "_get_experiment_manager", return_value=children),
        ):
            defined = mcp_server.define_optimization_study(
                self.plan["plan_id"], self.experiments
            )
            queued = mcp_server.start_optimization_study(
                defined["optimization_job_id"]
            )
            inspected = mcp_server.get_optimization_study(
                defined["optimization_job_id"]
            )
            cancelled = mcp_server.cancel_optimization_study(
                defined["optimization_job_id"]
            )

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(inspected["status"], "queued")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_partially_checkpointed_study_resumes_after_runs_root_move(self) -> None:
        first_manager = experiment_engine.ExperimentJobManager(
            self.runs,
            workers=1,
            execute_point=lambda *args, **kwargs: {},
        )
        first_study = optimization_study.OptimizationStudyManager(
            self.runs, first_manager, self._evaluator
        )
        experiments = {
            name: {**definition, "max_concurrency": 1}
            for name, definition in self.experiments.items()
        }
        defined = first_study.define(self.plan["plan_id"], experiments)
        plan = optimization_engine.load_optimization_plan(
            self.runs, self.plan["plan_id"]
        )
        for child in defined["experiments"].values():
            experiment_dir = Path(child["experiment_dir"])
            point_dir = experiment_dir / "point-0000"
            point_dir.mkdir()
            (point_dir / "point_result.json").write_text(
                json.dumps(
                    {
                        "index": 0,
                        "parameters": plan["points"][0]["parameters"],
                        "run_dir": str(point_dir),
                        "simulation_status": "completed",
                        "duration_seconds": 0.01,
                        "measurements": {},
                        "analyses": [],
                        "all_passed": True,
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = Path(child["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "queued"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        first_manager.shutdown()

        relocated = Path(self.temporary_directory.name) / "relocated-runs"
        shutil.copytree(self.runs, relocated)
        executed: list[tuple[int, dict[str, str]]] = []

        def execute(
            index: int,
            parameters: dict[str, str],
            attempt_dir: Path,
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            del args, kwargs
            executed.append((index, parameters))
            return {
                "index": index,
                "parameters": parameters,
                "run_dir": str(attempt_dir),
                "simulation_status": "completed",
                "duration_seconds": 0.01,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
            }

        resumed_manager = experiment_engine.ExperimentJobManager(
            relocated, workers=1, execute_point=execute
        )
        try:
            resumed_study = optimization_study.OptimizationStudyManager(
                relocated, resumed_manager, self._evaluator
            )
            deadline = time.monotonic() + 3
            current = resumed_study.snapshot(defined["optimization_job_id"])
            while current["status"] != "completed" and time.monotonic() < deadline:
                time.sleep(0.01)
                current = resumed_study.snapshot(defined["optimization_job_id"])
        finally:
            resumed_manager.shutdown()

        self.assertEqual(current["status"], "completed")
        self.assertEqual(len(executed), 2)
        self.assertEqual({index for index, _ in executed}, {1})


if __name__ == "__main__":
    unittest.main()
