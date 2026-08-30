"""Durable orchestration for one selected design's paired qualification."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, TypedDict

import experiment_engine
import experiment_report
import robust_selection
import sensitivity_analysis
import statistical_engine
import statistical_results
import worst_case_analysis


SCHEMA_VERSION = 1
ENGINE_VERSION = "durable-selected-qualification-v1"
JOB_PATTERN = re.compile(r"qualification-job-[0-9a-f]{16}")


class QualificationSnapshot(TypedDict):
    qualification_job_id: str
    qualification_job_dir: str
    manifest: str
    plan_id: str
    status: str
    experiments: dict[str, experiment_engine.ExperimentJobSnapshot]
    qualification_study_id: str | None
    results_json: str | None
    results_csv: str | None
    report_html: str | None
    error: str | None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class QualificationStudyManager:
    """Keep paired AC/transient children recoverable across GUI restarts."""

    def __init__(
        self,
        runs_dir: Path,
        experiment_manager: experiment_engine.ExperimentJobManager,
        evaluate: Callable[[Path, str, dict[str, dict[str, str]]], robust_selection.RobustSelectionStudyResult] = robust_selection.evaluate_robust_selection_study,
    ) -> None:
        self.runs_dir = runs_dir
        self.experiment_manager = experiment_manager
        self._evaluate = evaluate
        self._lock = threading.RLock()
        self._root = self._confined_root()

    def _confined_root(self) -> Path:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        runs = self.runs_dir.resolve()
        root = self.runs_dir / "qualification-jobs"
        if root.is_symlink():
            raise ValueError("qualification-jobs must not be a symlink")
        root.mkdir(exist_ok=True)
        resolved = root.resolve()
        if resolved.parent != runs:
            raise ValueError("qualification-jobs must remain inside runs")
        return resolved

    def _job_dir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or JOB_PATTERN.fullmatch(job_id) is None:
            raise ValueError("invalid qualification_job_id")
        path = self._root / job_id
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self._root:
            raise FileNotFoundError(f"qualification job not found: {job_id}")
        return path.resolve()

    def _load(self, job_id: str) -> dict[str, object]:
        path = self._job_dir(job_id) / "qualification_job.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("qualification manifest is not a regular file")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid qualification manifest") from exc
        definition = manifest.get("definition") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("engine_version") != ENGINE_VERSION
            or manifest.get("qualification_job_id") != job_id
            or not isinstance(definition, dict)
            or manifest.get("definition_hash") != hashlib.sha256(_canonical(definition).encode()).hexdigest()
        ):
            raise ValueError("qualification manifest integrity check failed")
        inspected = robust_selection.inspect_robust_selection_plan(self.runs_dir, str(definition.get("plan_id")))
        if inspected["plan_sha256"] != definition.get("plan_sha256"):
            raise ValueError("qualification plan hash does not match")
        return manifest

    def _save(self, manifest: dict[str, object]) -> None:
        manifest["updated_at"] = datetime.now().astimezone().isoformat()
        experiment_engine._write_json(
            self._job_dir(str(manifest["qualification_job_id"])) / "qualification_job.json",
            manifest,
        )

    def define(self, plan_id: str, experiments: dict[str, dict[str, object]]) -> QualificationSnapshot:
        plan = robust_selection.load_robust_selection_plan(self.runs_dir, plan_id)
        inspected = robust_selection.inspect_robust_selection_plan(self.runs_dir, plan_id)
        definition = plan["definition"]
        assert isinstance(definition, dict)
        finalists = definition["finalists"]
        statistical = definition["statistical_plans"]
        assert isinstance(finalists, list) and isinstance(statistical, dict)
        if len(finalists) != 1:
            raise ValueError("selected qualification requires exactly one finalist")
        label = str(finalists[0]["label"])
        if set(experiments) != {"ac", "transient"}:
            raise ValueError("qualification experiments must contain ac and transient")
        stat_source = statistical[label]
        assert isinstance(stat_source, dict)
        stat_plan = statistical_engine.load_statistical_plan(self.runs_dir, str(stat_source["plan_id"]))
        children: dict[str, dict[str, str]] = {}
        stat_plan_id = str(stat_source["plan_id"])
        stat_inspected = statistical_engine.inspect_statistical_plan(self.runs_dir, stat_plan_id)
        stat_definition = stat_plan["definition"]
        assert isinstance(stat_definition, dict)
        source: dict[str, object] = {
            "kind": "statistical", "plan_id": stat_plan_id,
            "plan_sha256": stat_inspected["plan_sha256"],
            "runs_relative_path": f"statistical-plans/{stat_plan_id}/statistical_plan.json",
            "generator_version": stat_plan["generator_version"],
            "definition_hash": stat_plan["definition_hash"],
            "sampling_method": stat_inspected["sampling_method"],
            "qualification": {
                "robust_plan_id": plan_id,
                "source_study_id": finalists[0]["source_study_id"],
                "source_candidate_index": finalists[0]["source_candidate_index"],
            },
        }
        if stat_definition.get("corner_axes"):
            source.update(
                sample_count=stat_plan["sample_count"],
                corner_axes=stat_definition["corner_axes"],
                corner_aggregate=bool(stat_definition.get("corner_aggregate", False)),
                point_metadata=[
                    {"index": point["index"], "sample_index": point["sample_index"], "corners": point["corners"]}
                    for point in stat_plan["points"]
                ],
            )
        for name in ("ac", "transient"):
            config = experiments[name]
            if not isinstance(config.get("netlist_template"), str):
                raise ValueError(f"experiment {name} netlist_template must be a string")
            child = self.experiment_manager.define_explicit(
                config["netlist_template"], stat_plan["parameter_order"],
                [point["parameters"] for point in stat_plan["points"]], stat_plan["parameter_units"],
                source, config.get("waveform_analyses"), config.get("filename", "circuit.cir"),
                config.get("ascii_raw", False), config.get("timeout_seconds", 120),
                config.get("max_concurrency", 2), config.get("reuse_cache", False),
            )
            child_id = str(child["experiment_id"])
            children[name] = {"experiment_id": child_id, "definition_hash": self.experiment_manager.definition_hash(child_id)}
        job_definition: dict[str, object] = {
            "plan_id": plan_id, "plan_sha256": inspected["plan_sha256"],
            "finalist_label": label, "experiments": children,
        }
        digest = hashlib.sha256((_canonical(job_definition) + uuid.uuid4().hex).encode()).hexdigest()
        job_id = f"qualification-job-{digest[:16]}"
        job_dir = self._root / job_id
        job_dir.mkdir()
        now = datetime.now().astimezone().isoformat()
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION,
            "qualification_job_id": job_id, "status": "defined", "created_at": now,
            "updated_at": now, "definition": job_definition,
            "definition_hash": hashlib.sha256(_canonical(job_definition).encode()).hexdigest(),
            "experiment_statuses": {name: "defined" for name in children}, "result": None, "error": None,
        }
        experiment_engine._write_json(job_dir / "qualification_job.json", manifest)
        return self.snapshot(job_id)

    def _children(self, manifest: dict[str, object]) -> tuple[dict[str, experiment_engine.ExperimentJobSnapshot], dict[str, str]]:
        definition = manifest["definition"]
        assert isinstance(definition, dict)
        records = definition["experiments"]
        assert isinstance(records, dict)
        snapshots: dict[str, experiment_engine.ExperimentJobSnapshot] = {}
        ids: dict[str, str] = {}
        for name in sorted(records):
            record = records[name]
            if not isinstance(record, dict) or not isinstance(record.get("experiment_id"), str):
                raise ValueError("qualification child identity is invalid")
            child_id = record["experiment_id"]
            if self.experiment_manager.definition_hash(child_id) != record.get("definition_hash"):
                raise ValueError("qualification child definition hash does not match")
            ids[name] = child_id
            snapshots[name] = self.experiment_manager.snapshot(child_id)
        return snapshots, ids

    @staticmethod
    def _status(statuses: list[str]) -> str:
        if any(value == "failed" for value in statuses): return "failed"
        if all(value == "completed" for value in statuses): return "completed"
        if any(value == "cancelling" for value in statuses): return "cancelling"
        if any(value == "running" for value in statuses): return "running"
        if any(value == "queued" for value in statuses): return "queued"
        if any(value == "cancelled" for value in statuses): return "cancelled"
        return "defined"

    def start(self, job_id: str) -> QualificationSnapshot:
        with self._lock:
            manifest = self._load(job_id)
            if manifest.get("status") in {"cancelled", "completed", "failed"}: return self.snapshot(job_id)
            _, ids = self._children(manifest)
            for child_id in ids.values(): self.experiment_manager.start(child_id)
            manifest["status"] = "queued"
            self._save(manifest)
        return self.snapshot(job_id)

    def cancel(self, job_id: str) -> QualificationSnapshot:
        with self._lock:
            manifest = self._load(job_id)
            snapshots, ids = self._children(manifest)
            for name, child_id in ids.items():
                if snapshots[name]["status"] not in {"completed", "failed", "cancelled"}: self.experiment_manager.cancel(child_id)
            manifest["status"] = "cancelling"
            self._save(manifest)
        return self.snapshot(job_id)

    def resume(self, job_id: str) -> QualificationSnapshot:
        with self._lock:
            manifest = self._load(job_id)
            snapshots, ids = self._children(manifest)
            for name, child_id in ids.items():
                if snapshots[name]["status"] == "cancelled": self.experiment_manager.resume(child_id)
                elif snapshots[name]["status"] not in {"completed", "failed"}: self.experiment_manager.start(child_id)
            manifest["status"] = "queued"; manifest["error"] = None
            self._save(manifest)
        return self.snapshot(job_id)

    def snapshot(self, job_id: str) -> QualificationSnapshot:
        with self._lock:
            manifest = self._load(job_id)
            snapshots, ids = self._children(manifest)
            statuses = {name: child["status"] for name, child in snapshots.items()}
            status = self._status(list(statuses.values()))
            result = manifest.get("result"); error = manifest.get("error")
            if status == "completed" and result is None:
                definition = manifest["definition"]
                assert isinstance(definition, dict)
                try:
                    for child_id in ids.values():
                        statistical_results.summarize_statistical_experiment(self.runs_dir, child_id)
                        worst_case_analysis.analyze_statistical_worst_cases(self.runs_dir, child_id)
                        sensitivity_analysis.analyze_statistical_sensitivity(self.runs_dir, child_id)
                        experiment_report.build_experiment_report(
                            self.runs_dir,
                            child_id,
                            {
                                "title": "Selected mixed-signal DAQ qualification",
                                "circuit_summary": "Two-pole anti-alias filtering, gain, ADC drive, and sample-and-hold loading.",
                                "simulation_summary": "One frozen manufacturing population is evaluated at both named ADC-load corners.",
                                "mcp_context": "This paired evidence qualifies the nominal GUI-C3 winner without changing its design values.",
                            },
                            max_traces_per_plot=16,
                        )
                    label = str(definition["finalist_label"])
                    evaluated = self._evaluate(self.runs_dir, str(definition["plan_id"]), {label: ids})
                    result = {"study_id": evaluated["study_id"]}; error = None
                except Exception as exc:
                    status = "failed"; error = str(exc)
            changed = any(manifest.get(key) != value for key, value in {"status": status, "experiment_statuses": statuses, "result": result, "error": error}.items())
            manifest.update(status=status, experiment_statuses=statuses, result=result, error=error)
            if changed: self._save(manifest)
        study_id = result.get("study_id") if isinstance(result, dict) else None
        study_dir = self.runs_dir / "robust-selection-studies" / str(study_id) if study_id else None
        definition = manifest["definition"]
        assert isinstance(definition, dict)
        job_dir = self._job_dir(job_id)
        return {
            "qualification_job_id": job_id, "qualification_job_dir": str(job_dir),
            "manifest": str(job_dir / "qualification_job.json"), "plan_id": str(definition["plan_id"]),
            "status": status, "experiments": snapshots,
            "qualification_study_id": str(study_id) if study_id else None,
            "results_json": str(study_dir / "robust_selection_results.json") if study_dir else None,
            "results_csv": str(study_dir / "robust_selection_results.csv") if study_dir else None,
            "report_html": str(study_dir / "report.html") if study_dir else None,
            "error": str(error) if isinstance(error, str) else None,
        }
