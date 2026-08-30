"""Durable orchestration for multi-experiment optimization studies."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, NotRequired, TypedDict

import artifacts
import experiment_engine
import optimization_engine

OPTIMIZATION_STUDY_SCHEMA_VERSION = 1
OPTIMIZATION_STUDY_ENGINE_VERSION = "durable-optimization-study-v1"
STUDY_ID_PATTERN = re.compile(r"optimization-job-[0-9a-f]{16}")


class OptimizationExperimentDefinition(TypedDict):
    netlist_template: str
    waveform_analyses: NotRequired[list[experiment_engine.ExperimentWaveformAnalysis]]
    filename: NotRequired[str]
    ascii_raw: NotRequired[bool]
    timeout_seconds: NotRequired[int]
    max_concurrency: NotRequired[int]
    reuse_cache: NotRequired[bool]


class OptimizationJobSnapshot(TypedDict):
    optimization_job_id: str
    optimization_job_dir: str
    manifest: str
    plan_id: str
    status: str
    experiments: dict[str, experiment_engine.ExperimentJobSnapshot]
    optimization_study_id: str | None
    results_json: str | None
    results_csv: str | None
    report_html: str | None
    error: str | None


_canonical_json = artifacts.canonical_json


def _plan_source(
    plan_id: str,
    plan: optimization_engine.OptimizationPlan,
    plan_result: optimization_engine.OptimizationPlanResult,
) -> dict[str, object]:
    return {
        "kind": "optimization",
        "plan_id": plan_id,
        "plan_sha256": plan_result["plan_sha256"],
        "runs_relative_path": f"optimization-plans/{plan_id}/optimization_plan.json",
        "generator_version": plan["generator_version"],
        "definition_hash": plan["definition_hash"],
        "candidate_count": plan["candidate_count"],
        "point_metadata": [
            {
                "index": point["index"],
                "candidate_index": point["candidate_index"],
                "corners": point.get("corners", {}),
            }
            for point in plan["points"]
        ],
    }


class OptimizationStudyManager:
    """Compose durable experiments without introducing another job runner."""

    def __init__(
        self,
        runs_dir: Path,
        experiment_manager: experiment_engine.ExperimentJobManager,
        evaluate: Callable[
            [Path, str, dict[str, str]], optimization_engine.OptimizationStudyResult
        ] = optimization_engine.evaluate_optimization_study,
    ) -> None:
        self.runs_dir = runs_dir
        self.experiment_manager = experiment_manager
        self._evaluate = evaluate
        self._lock = threading.RLock()
        self._root = self._confined_root()

    def _confined_root(self) -> Path:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        resolved_runs = self.runs_dir.resolve()
        root = self.runs_dir / "optimization-jobs"
        if root.is_symlink():
            raise ValueError("optimization-jobs must not be a symlink")
        root.mkdir(exist_ok=True)
        resolved = root.resolve()
        if resolved.parent != resolved_runs:
            raise ValueError("optimization-jobs must remain inside runs")
        return resolved

    def _study_dir(self, optimization_job_id: str) -> Path:
        if (
            not isinstance(optimization_job_id, str)
            or STUDY_ID_PATTERN.fullmatch(optimization_job_id) is None
        ):
            raise ValueError("invalid optimization_job_id")
        candidate = self._root / optimization_job_id
        if candidate.is_symlink() or not candidate.is_dir():
            raise FileNotFoundError(
                f"optimization study not found: {optimization_job_id}"
            )
        resolved = candidate.resolve()
        if resolved.parent != self._root:
            raise ValueError("optimization study must remain inside optimization-jobs")
        return resolved

    def _load(self, optimization_job_id: str) -> dict[str, object]:
        path = self._study_dir(optimization_job_id) / "optimization_job.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("optimization study manifest is not a regular file")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid optimization study manifest") from exc
        if not isinstance(manifest, dict):
            raise ValueError("optimization study manifest must be an object")
        definition = manifest.get("definition")
        if (
            manifest.get("schema_version") != OPTIMIZATION_STUDY_SCHEMA_VERSION
            or manifest.get("engine_version") != OPTIMIZATION_STUDY_ENGINE_VERSION
            or manifest.get("optimization_job_id") != optimization_job_id
            or not isinstance(definition, dict)
            or manifest.get("definition_hash") != artifacts.definition_hash(definition)
        ):
            raise ValueError("optimization study manifest integrity check failed")
        plan_id = definition.get("plan_id")
        if not isinstance(plan_id, str):
            raise ValueError("optimization study plan identity is missing")
        plan_result = optimization_engine.inspect_optimization_plan(
            self.runs_dir, plan_id
        )
        if plan_result["plan_sha256"] != definition.get("plan_sha256"):
            raise ValueError("optimization study plan hash does not match")
        return manifest

    def _save(self, manifest: dict[str, object]) -> None:
        manifest["updated_at"] = datetime.now().astimezone().isoformat()
        study_dir = self._study_dir(str(manifest["optimization_job_id"]))
        experiment_engine._write_json(study_dir / "optimization_job.json", manifest)

    def define(
        self,
        plan_id: str,
        experiments: dict[str, OptimizationExperimentDefinition],
    ) -> OptimizationJobSnapshot:
        plan = optimization_engine.load_optimization_plan(self.runs_dir, plan_id)
        plan_result = optimization_engine.inspect_optimization_plan(
            self.runs_dir, plan_id
        )
        required = plan["definition"].get("experiments")
        if (
            not isinstance(experiments, dict)
            or not isinstance(required, list)
            or set(experiments) != set(required)
        ):
            raise ValueError("experiments must exactly match the plan experiment names")
        for name, config in experiments.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                raise ValueError("experiment definitions must be named objects")
            if not isinstance(config.get("netlist_template"), str):
                raise ValueError(f"experiment {name} netlist_template must be a string")

        children: dict[str, dict[str, str]] = {}
        source = _plan_source(plan_id, plan, plan_result)
        for name in sorted(experiments):
            config = experiments[name]
            child = self.experiment_manager.define_explicit(
                config["netlist_template"],
                plan["parameter_order"],
                [point["parameters"] for point in plan["points"]],
                plan["parameter_units"],
                source,
                config.get("waveform_analyses"),
                config.get("filename", "circuit.cir"),
                config.get("ascii_raw", False),
                config.get("timeout_seconds", 120),
                config.get("max_concurrency", 2),
                config.get("reuse_cache", False),
            )
            children[name] = {
                "experiment_id": child["experiment_id"],
                "definition_hash": self.experiment_manager.definition_hash(
                    child["experiment_id"]
                ),
            }

        definition: dict[str, object] = {
            "plan_id": plan_id,
            "plan_sha256": plan_result["plan_sha256"],
            "plan_definition_hash": plan["definition_hash"],
            "experiments": children,
        }
        digest = hashlib.sha256(
            (_canonical_json(definition) + uuid.uuid4().hex).encode("utf-8")
        ).hexdigest()
        optimization_job_id = f"optimization-job-{digest[:16]}"
        study_dir = self._root / optimization_job_id
        study_dir.mkdir()
        now = datetime.now().astimezone().isoformat()
        manifest: dict[str, object] = {
            "schema_version": OPTIMIZATION_STUDY_SCHEMA_VERSION,
            "engine_version": OPTIMIZATION_STUDY_ENGINE_VERSION,
            "optimization_job_id": optimization_job_id,
            "status": "defined",
            "created_at": now,
            "updated_at": now,
            "definition": definition,
            "definition_hash": artifacts.definition_hash(definition),
            "experiment_statuses": {name: "defined" for name in children},
            "result": None,
            "error": None,
        }
        experiment_engine._write_json(study_dir / "optimization_job.json", manifest)
        return self.snapshot(optimization_job_id)

    @staticmethod
    def _status(child_statuses: list[str]) -> str:
        if any(status == "failed" for status in child_statuses):
            return "failed"
        if all(status == "completed" for status in child_statuses):
            return "completed"
        if any(status == "cancelling" for status in child_statuses):
            return "cancelling"
        if any(status == "running" for status in child_statuses):
            return "running"
        if any(status == "queued" for status in child_statuses):
            return "queued"
        if any(status == "cancelled" for status in child_statuses):
            return "cancelled"
        return "defined"

    def _children(
        self, manifest: dict[str, object]
    ) -> tuple[dict[str, experiment_engine.ExperimentJobSnapshot], dict[str, str]]:
        definition = manifest["definition"]
        assert isinstance(definition, dict)
        records = definition["experiments"]
        if not isinstance(records, dict):
            raise ValueError("optimization study experiment mapping is invalid")
        snapshots: dict[str, experiment_engine.ExperimentJobSnapshot] = {}
        ids: dict[str, str] = {}
        for name in sorted(records):
            record = records[name]
            if not isinstance(record, dict) or not isinstance(
                record.get("experiment_id"), str
            ):
                raise ValueError("optimization study child identity is invalid")
            experiment_id = record["experiment_id"]
            snapshot = self.experiment_manager.snapshot(experiment_id)
            if self.experiment_manager.definition_hash(
                experiment_id
            ) != record.get("definition_hash"):
                raise ValueError("optimization study child definition hash does not match")
            ids[name] = experiment_id
            snapshots[name] = snapshot
        return snapshots, ids

    def start(self, optimization_job_id: str) -> OptimizationJobSnapshot:
        with self._lock:
            manifest = self._load(optimization_job_id)
            if manifest.get("status") in {"cancelled", "completed", "failed"}:
                return self.snapshot(optimization_job_id)
            _, ids = self._children(manifest)
            for name in sorted(ids):
                self.experiment_manager.start(ids[name])
            manifest["status"] = "queued"
            self._save(manifest)
        return self.snapshot(optimization_job_id)

    def cancel(self, optimization_job_id: str) -> OptimizationJobSnapshot:
        with self._lock:
            manifest = self._load(optimization_job_id)
            snapshots, ids = self._children(manifest)
            for name in sorted(ids):
                if snapshots[name]["status"] not in {"completed", "failed", "cancelled"}:
                    self.experiment_manager.cancel(ids[name])
            manifest["status"] = "cancelling"
            self._save(manifest)
        return self.snapshot(optimization_job_id)

    def resume(self, optimization_job_id: str) -> OptimizationJobSnapshot:
        with self._lock:
            manifest = self._load(optimization_job_id)
            snapshots, ids = self._children(manifest)
            for name in sorted(ids):
                if snapshots[name]["status"] == "cancelled":
                    self.experiment_manager.resume(ids[name])
                elif snapshots[name]["status"] not in {"completed", "failed"}:
                    self.experiment_manager.start(ids[name])
            manifest["status"] = "queued"
            manifest["error"] = None
            self._save(manifest)
        return self.snapshot(optimization_job_id)

    def snapshot(self, optimization_job_id: str) -> OptimizationJobSnapshot:
        with self._lock:
            manifest = self._load(optimization_job_id)
            snapshots, ids = self._children(manifest)
            statuses = {name: child["status"] for name, child in snapshots.items()}
            status = self._status(list(statuses.values()))
            result = manifest.get("result")
            error = manifest.get("error")
            if status == "completed" and result is None:
                definition = manifest["definition"]
                assert isinstance(definition, dict)
                try:
                    evaluated = self._evaluate(
                        self.runs_dir, str(definition["plan_id"]), ids
                    )
                    result = {
                        "study_id": evaluated["study_id"],
                        "study_relative_path": (
                            f"optimization-studies/{evaluated['study_id']}"
                        ),
                    }
                    error = None
                except Exception as exc:
                    status = "failed"
                    error = str(exc)
            next_state = dict(
                status=status,
                experiment_statuses=statuses,
                result=result,
                error=error,
            )
            changed = any(manifest.get(key) != value for key, value in next_state.items())
            manifest.update(next_state)
            if changed:
                self._save(manifest)

        study_id = result.get("study_id") if isinstance(result, dict) else None
        study_dir = (
            self.runs_dir / "optimization-studies" / str(study_id)
            if study_id is not None
            else None
        )
        job_dir = self._study_dir(optimization_job_id)
        definition = manifest["definition"]
        assert isinstance(definition, dict)
        return {
            "optimization_job_id": optimization_job_id,
            "optimization_job_dir": str(job_dir),
            "manifest": str(job_dir / "optimization_job.json"),
            "plan_id": str(definition["plan_id"]),
            "status": status,
            "experiments": snapshots,
            "optimization_study_id": str(study_id) if study_id is not None else None,
            "results_json": str(study_dir / "optimization_results.json") if study_dir else None,
            "results_csv": str(study_dir / "optimization_results.csv") if study_dir else None,
            "report_html": str(study_dir / "report.html") if study_dir else None,
            "error": str(error) if isinstance(error, str) else None,
        }
