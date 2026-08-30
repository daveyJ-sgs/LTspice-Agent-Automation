from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qualification_study
import robust_selection


class FakeExperimentManager:
    def __init__(self, runs: Path) -> None:
        self.runs = runs; self.statuses: dict[str, str] = {}; self.counter = 0

    def define_explicit(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        experiment_id = f"mcp-experiment-fake-{self.counter}"; self.counter += 1
        directory = self.runs / experiment_id; directory.mkdir(parents=True)
        manifest = directory / "experiment_manifest.json"
        manifest.write_text(json.dumps({"definition_hash": f"hash-{experiment_id}"}), encoding="utf-8")
        self.statuses[experiment_id] = "defined"
        return {"experiment_id": experiment_id, "manifest": str(manifest)}

    def definition_hash(self, experiment_id: str) -> str:
        return str(json.loads((self.runs / experiment_id / "experiment_manifest.json").read_text())["definition_hash"])

    def snapshot(self, experiment_id: str) -> dict[str, object]:
        status = self.statuses[experiment_id]
        return {
            "experiment_id": experiment_id, "status": status,
            "manifest": str(self.runs / experiment_id / "experiment_manifest.json"),
            "experiment_dir": str(self.runs / experiment_id), "results_json": None, "results_csv": None,
            "point_count": 8, "finished_points": 8 if status == "completed" else 0,
            "pending_points": 0 if status == "completed" else 8, "running_points": 0,
            "completed_points": 8 if status == "completed" else 0, "error_points": 0,
            "passed_points": 8 if status == "completed" else 0, "failed_points": 0,
            "all_passed": True if status == "completed" else None, "error": None,
            "execution_mode": "independent",
        }

    def start(self, experiment_id: str) -> dict[str, object]:
        if self.statuses[experiment_id] == "defined": self.statuses[experiment_id] = "queued"
        return self.snapshot(experiment_id)

    def cancel(self, experiment_id: str) -> dict[str, object]:
        self.statuses[experiment_id] = "cancelled"; return self.snapshot(experiment_id)

    def resume(self, experiment_id: str) -> dict[str, object]:
        self.statuses[experiment_id] = "queued"; return self.snapshot(experiment_id)


class QualificationStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(); self.runs = Path(self.temporary.name) / "runs"
        source = {
            "label": "selected-design", "tie_break_rank": 0,
            "source_study_id": "optimization-study-aaaaaaaaaaaaaaaa",
            "source_results_sha256": "1" * 64, "source_plan_id": "optimization-plan-bbbbbbbbbbbbbbbb",
            "source_candidate_index": 3, "parameters": {"R": "100"},
            "nominal_objectives": {}, "nominal_constraints": {},
        }
        with patch.object(robust_selection, "_source_finalist", return_value=source):
            self.plan = robust_selection.generate_robust_selection_plan(
                self.runs, [{"label": "selected-design", "study_id": "ignored", "candidate_index": 3}],
                {"selected-design": [{"name": "R", "distribution": "gaussian", "nominal": 100, "sigma": 1, "minimum": 95, "maximum": 105, "unit": "ohm"}]},
                4, 42, corner_axes=[{"name": "load", "parameter": "C", "unit": "F", "values": [{"name": "light", "value": 1e-12}, {"name": "heavy", "value": 2e-12}]}], sampling_method="halton",
            )
        self.experiments = {"ac": {"netlist_template": "R1 in 0 {R}\n.end\n"}, "transient": {"netlist_template": "R1 in 0 {R}\n.end\n"}}

    def tearDown(self) -> None: self.temporary.cleanup()

    def test_define_start_cancel_resume_and_manifest_are_durable(self) -> None:
        children = FakeExperimentManager(self.runs)
        manager = qualification_study.QualificationStudyManager(self.runs, children)  # type: ignore[arg-type]
        defined = manager.define(self.plan["plan_id"], self.experiments)
        self.assertEqual(defined["status"], "defined")
        self.assertNotIn(str(self.runs), Path(defined["manifest"]).read_text())
        queued = manager.start(defined["qualification_job_id"]); self.assertEqual(queued["status"], "queued")
        restarted = qualification_study.QualificationStudyManager(self.runs, children)  # type: ignore[arg-type]
        cancelled = restarted.cancel(defined["qualification_job_id"]); self.assertEqual(cancelled["status"], "cancelled")
        resumed = restarted.resume(defined["qualification_job_id"]); self.assertEqual(resumed["status"], "queued")

    def test_completed_children_are_postprocessed_once_then_evaluated(self) -> None:
        children = FakeExperimentManager(self.runs); calls: list[tuple[str, object]] = []

        def evaluate(runs: Path, plan_id: str, experiments: dict[str, dict[str, str]]) -> dict[str, object]:
            calls.append((plan_id, experiments)); study_id = "robust-selection-study-0123456789abcdef"
            directory = runs / "robust-selection-studies" / study_id; directory.mkdir(parents=True)
            for name in ("robust_selection_results.json", "robust_selection_results.csv", "report.html"): (directory / name).write_text(name)
            return {"study_id": study_id, "plan_id": plan_id, "study_dir": str(directory), "results_json": str(directory / "robust_selection_results.json"), "results_csv": str(directory / "robust_selection_results.csv"), "report_html": str(directory / "report.html"), "finalist_count": 1, "selected_finalist": "selected-design", "selection_explanation": "qualified"}

        manager = qualification_study.QualificationStudyManager(self.runs, children, evaluate)  # type: ignore[arg-type]
        defined = manager.define(self.plan["plan_id"], self.experiments)
        for child in defined["experiments"].values(): children.statuses[child["experiment_id"]] = "completed"
        with patch.object(qualification_study.statistical_results, "summarize_statistical_experiment"), patch.object(qualification_study.worst_case_analysis, "analyze_statistical_worst_cases"), patch.object(qualification_study.sensitivity_analysis, "analyze_statistical_sensitivity"), patch.object(qualification_study.experiment_report, "build_experiment_report"):
            completed = manager.snapshot(defined["qualification_job_id"])
            repeated = manager.snapshot(defined["qualification_job_id"])
        self.assertEqual(completed, repeated); self.assertEqual(len(calls), 1)
        self.assertEqual(completed["qualification_study_id"], "robust-selection-study-0123456789abcdef")

    def test_rejects_wrong_experiment_set(self) -> None:
        manager = qualification_study.QualificationStudyManager(self.runs, FakeExperimentManager(self.runs))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "ac and transient"):
            manager.define(self.plan["plan_id"], {"ac": self.experiments["ac"]})


if __name__ == "__main__": unittest.main()
