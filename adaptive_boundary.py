"""Durable deterministic one-dimensional electrical boundary refinement."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Protocol, TypedDict

import experiment_index
import statistical_results
import worst_case_analysis

ADAPTIVE_SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 8
MAX_ADAPTIVE_SAMPLES = 64
_LOCK = threading.RLock()


class AdaptiveBoundaryStudyResult(TypedDict):
    adaptive_id: str
    adaptive_dir: str
    manifest: str
    status: str
    variable: str
    unit: str
    sample_count: int
    max_samples: int
    batch_count: int
    current_width: float
    input_tolerance: float
    stop_reason: str | None
    active_experiment_id: str | None
    error: str | None


class _ExperimentManager(Protocol):
    def define_explicit(self, *args: object, **kwargs: object) -> dict[str, object]: ...

    def start(self, experiment_id: str) -> dict[str, object]: ...

    def snapshot(self, experiment_id: str) -> dict[str, object]: ...


def _canonical_json(value: object, *, pretty: bool = False) -> str:
    options: dict[str, object] = {
        "sort_keys": True,
        "ensure_ascii": False,
        "allow_nan": False,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return json.dumps(value, **options)  # type: ignore[arg-type]


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal number")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 17
        rounded = +value
    if rounded.is_zero():
        return "0"
    encoded = format(rounded.normalize(), "g").lower()
    if len(encoded) > 128:
        raise ValueError("generated adaptive value is too long")
    return encoded


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("adaptive manifest must not be a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _study_root(runs_dir: Path) -> Path:
    root = runs_dir.resolve() / "adaptive-studies"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("adaptive study root is not a real directory")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _requirement(point: dict[str, object], check_id: str) -> dict[str, object]:
    if statistical_results._classification(point) not in {
        "electrical_pass",
        "electrical_failure",
    }:
        raise ValueError("adaptive boundary point lacks electrical evidence")
    matches: list[dict[str, object]] = []
    for analysis_entry in point.get("analyses", []):
        if not isinstance(analysis_entry, dict):
            continue
        analysis = analysis_entry.get("analysis")
        if not isinstance(analysis, dict):
            continue
        analysis_name = str(analysis_entry["name"])
        for index, result in enumerate(analysis.get("results", [])):
            threshold = result["threshold"]
            identity = worst_case_analysis._identity(
                analysis_name,
                str(result["metric"]),
                str(threshold["operator"]),
                float(threshold["target"]),
                str(result["unit"]),
                dict(result.get("parameters", {})),
            )
            if identity == check_id:
                value = float(result["value"])
                target = float(threshold["target"])
                operator = str(threshold["operator"])
                margin = worst_case_analysis._margin(operator, target, value)
                passed = bool(result["passed"])
                if not math.isfinite(margin):
                    raise ValueError("adaptive requirement margin must be finite")
                matches.append(
                    {
                        "check_id": check_id,
                        "analysis": analysis_name,
                        "requirement_index": index,
                        "metric": str(result["metric"]),
                        "operator": operator,
                        "target": target,
                        "unit": str(result["unit"]),
                        "parameters": dict(result.get("parameters", {})),
                        "value": value,
                        "margin": margin,
                        "passed": passed,
                    }
                )
    if len(matches) != 1:
        raise ValueError("adaptive check_id must identify exactly one requirement")
    return matches[0]


def _endpoint(
    point: dict[str, object],
    check_id: str,
    variable: str,
    source_experiment_id: str,
) -> dict[str, object]:
    requirement = _requirement(point, check_id)
    parameters = dict(point["parameters"])
    value = _decimal(parameters.get(variable), f"parameter {variable}")
    point_index = int(point["index"])
    return {
        "input": _canonical_decimal(value),
        "margin": requirement["margin"],
        "passed": requirement["passed"],
        "value": requirement["value"],
        "point_index": point_index,
        "parameters": parameters,
        "evidence_path": f"{source_experiment_id}/point-{point_index:04d}/",
    }


def _snapshot(manifest: dict[str, object], path: Path) -> AdaptiveBoundaryStudyResult:
    definition = manifest["definition"]
    assert isinstance(definition, dict)
    bracket = manifest["current_bracket"]
    assert isinstance(bracket, dict)
    low = bracket["low"]
    high = bracket["high"]
    assert isinstance(low, dict) and isinstance(high, dict)
    active = manifest.get("active_batch")
    active_id = active.get("experiment_id") if isinstance(active, dict) else None
    return {
        "adaptive_id": str(manifest["adaptive_id"]),
        "adaptive_dir": str(path.parent),
        "manifest": str(path),
        "status": str(manifest["status"]),
        "variable": str(definition["variable"]),
        "unit": str(definition["unit"]),
        "sample_count": int(manifest["sample_count"]),
        "max_samples": int(definition["max_samples"]),
        "batch_count": len(manifest["history"]),
        "current_width": float(
            _decimal(high["input"], "high input")
            - _decimal(low["input"], "low input")
        ),
        "input_tolerance": float(definition["input_tolerance"]),
        "stop_reason": manifest.get("stop_reason"),
        "active_experiment_id": active_id,
        "error": manifest.get("error"),
    }


def _load_manifest(runs_dir: Path, adaptive_id: str) -> tuple[Path, dict[str, object]]:
    if (
        not isinstance(adaptive_id, str)
        or re.fullmatch(r"adaptive-study-[0-9a-f]{16}", adaptive_id) is None
    ):
        raise ValueError("invalid adaptive_id")
    root = _study_root(runs_dir)
    directory = root / adaptive_id
    if directory.is_symlink() or not directory.is_dir():
        raise FileNotFoundError(f"adaptive study not found: {adaptive_id}")
    if directory.resolve().parent != root or directory.resolve().name != adaptive_id:
        raise ValueError("adaptive study must remain inside runs")
    path = directory / "adaptive_manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("adaptive manifest is not a regular file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("adaptive manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != ADAPTIVE_SCHEMA_VERSION
        or manifest.get("adaptive_id") != adaptive_id
    ):
        raise ValueError("adaptive manifest schema is invalid")
    definition = manifest.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("adaptive definition is invalid")
    digest = hashlib.sha256(_canonical_json(definition).encode("utf-8")).hexdigest()
    if adaptive_id != f"adaptive-study-{digest[:16]}":
        raise ValueError("adaptive definition does not match its content address")
    return path, manifest


def define_adaptive_boundary_study(
    runs_dir: Path,
    source_experiment_id: str,
    first_point_index: int,
    second_point_index: int,
    check_id: str,
    variable: str,
    batch_size: int = 3,
    max_samples: int = 12,
    input_tolerance: float = 1e-6,
    max_concurrency: int = 2,
    reuse_cache: bool = True,
) -> AdaptiveBoundaryStudyResult:
    """Define a content-addressed bracket from opposite electrical outcomes."""
    with _LOCK:
        if (
            not isinstance(first_point_index, int)
            or isinstance(first_point_index, bool)
            or first_point_index < 0
            or not isinstance(second_point_index, int)
            or isinstance(second_point_index, bool)
            or second_point_index < 0
            or first_point_index == second_point_index
        ):
            raise ValueError(
                "adaptive source point indexes must be distinct and non-negative"
            )
        if (
            not isinstance(check_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", check_id) is None
        ):
            raise ValueError("adaptive check_id must be a SHA-256 identifier")
        if (
            not isinstance(variable, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) is None
        ):
            raise ValueError("adaptive variable name is invalid")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or batch_size < 1
            or batch_size > MAX_BATCH_SIZE
        ):
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if (
            not isinstance(max_samples, int)
            or isinstance(max_samples, bool)
            or max_samples < 1
            or max_samples > MAX_ADAPTIVE_SAMPLES
        ):
            raise ValueError(f"max_samples must be between 1 and {MAX_ADAPTIVE_SAMPLES}")
        tolerance = _decimal(input_tolerance, "input_tolerance")
        if tolerance <= 0:
            raise ValueError("input_tolerance must be greater than zero")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
            or max_concurrency > 4
        ):
            raise ValueError("max_concurrency must be between 1 and 4")
        if not isinstance(reuse_cache, bool):
            raise ValueError("reuse_cache must be a boolean")

        _, source_manifest, results, _ = experiment_index.load_completed_experiment(
            runs_dir, source_experiment_id
        )
        points = results["points"]
        assert isinstance(points, list)
        by_index = {int(point["index"]): point for point in points}
        if first_point_index not in by_index or second_point_index not in by_index:
            raise ValueError("adaptive source point is missing")
        first = _endpoint(
            by_index[first_point_index], check_id, variable, source_experiment_id
        )
        second = _endpoint(
            by_index[second_point_index], check_id, variable, source_experiment_id
        )
        if first["passed"] == second["passed"]:
            raise ValueError("adaptive source points must bracket opposite pass states")
        first_parameters = dict(first["parameters"])
        second_parameters = dict(second["parameters"])
        for name in set(first_parameters) | set(second_parameters):
            if (
                name != variable
                and first_parameters.get(name) != second_parameters.get(name)
            ):
                raise ValueError(
                    "adaptive source points may differ only in the selected variable"
                )
        ordered = sorted(
            (first, second), key=lambda endpoint: _decimal(endpoint["input"], "input")
        )
        if ordered[0]["input"] == ordered[1]["input"]:
            raise ValueError("adaptive source inputs must be distinct")
        requirement = _requirement(by_index[first_point_index], check_id)
        source_definition = source_manifest["definition"]
        assert isinstance(source_definition, dict)
        parameter_order = source_definition.get("parameter_order")
        parameter_units = source_definition.get("parameter_units")
        if not isinstance(parameter_order, list) or not isinstance(parameter_units, dict):
            raise ValueError("adaptive source experiment parameter metadata is invalid")
        definition: dict[str, object] = {
            "source_experiment_id": source_experiment_id,
            "first_point_index": first_point_index,
            "second_point_index": second_point_index,
            "check_id": check_id,
            "requirement": {
                name: requirement[name]
                for name in (
                    "analysis", "requirement_index", "metric", "operator",
                    "target", "unit", "parameters",
                )
            },
            "variable": variable,
            "unit": str(parameter_units.get(variable, "")),
            "batch_size": batch_size,
            "max_samples": max_samples,
            "input_tolerance": _canonical_decimal(tolerance),
            "max_concurrency": max_concurrency,
            "reuse_cache": reuse_cache,
            "parameter_order": list(parameter_order),
            "parameter_units": dict(parameter_units),
            "netlist_template": source_definition["netlist_template"],
            "waveform_analyses": source_definition.get("waveform_analyses", []),
            "filename": source_definition["filename"],
            "ascii_raw": source_definition["ascii_raw"],
            "timeout_seconds": source_definition["timeout_seconds"],
            "initial_bracket": {"low": ordered[0], "high": ordered[1]},
        }
        digest = hashlib.sha256(_canonical_json(definition).encode("utf-8")).hexdigest()
        adaptive_id = f"adaptive-study-{digest[:16]}"
        root = _study_root(runs_dir)
        directory = root / adaptive_id
        try:
            directory.mkdir()
        except FileExistsError:
            path, manifest = _load_manifest(runs_dir, adaptive_id)
            if manifest["definition"] != definition:
                raise ValueError("adaptive definition content address collision")
            return _snapshot(manifest, path)
        path = directory / "adaptive_manifest.json"
        manifest: dict[str, object] = {
            "schema_version": ADAPTIVE_SCHEMA_VERSION,
            "adaptive_id": adaptive_id,
            "status": "defined",
            "definition": definition,
            "current_bracket": {"low": ordered[0], "high": ordered[1]},
            "sample_count": 0,
            "history": [],
            "active_batch": None,
            "stop_reason": None,
            "error": None,
        }
        _write_manifest(path, manifest)
        return _snapshot(manifest, path)


def _interior_values(low: str, high: str, count: int) -> list[str]:
    low_value = _decimal(low, "low input")
    high_value = _decimal(high, "high input")
    if high_value <= low_value:
        raise ValueError("adaptive bracket ordering is invalid")
    values = [
        _canonical_decimal(
            low_value
            + (high_value - low_value) * Decimal(index) / Decimal(count + 1)
        )
        for index in range(1, count + 1)
    ]
    if len(set(values)) != len(values) or low in values or high in values:
        raise ValueError("adaptive bracket reached numeric resolution")
    return values


def _refine_bracket(
    low: dict[str, object],
    high: dict[str, object],
    observations: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    ordered = sorted(
        [low, *observations, high],
        key=lambda endpoint: _decimal(endpoint["input"], "adaptive input"),
    )
    transitions = [
        (left, right)
        for left, right in zip(ordered, ordered[1:])
        if left["passed"] != right["passed"]
    ]
    if len(transitions) != 1:
        raise ValueError("adaptive observations are not a single monotonic boundary")
    return transitions[0]


def _finish_if_needed(manifest: dict[str, object]) -> bool:
    definition = manifest["definition"]
    bracket = manifest["current_bracket"]
    assert isinstance(definition, dict) and isinstance(bracket, dict)
    low = bracket["low"]
    high = bracket["high"]
    assert isinstance(low, dict) and isinstance(high, dict)
    width = _decimal(high["input"], "high input") - _decimal(low["input"], "low input")
    if width <= _decimal(definition["input_tolerance"], "input_tolerance"):
        manifest.update(status="completed", stop_reason="input_tolerance")
        return True
    if int(manifest["sample_count"]) >= int(definition["max_samples"]):
        manifest.update(status="completed", stop_reason="sample_budget")
        return True
    return False


def _incorporate_batch(
    runs_dir: Path,
    manifest: dict[str, object],
    active: dict[str, object],
) -> None:
    experiment_id = str(active["experiment_id"])
    _, child_manifest, results, _ = experiment_index.load_completed_experiment(
        runs_dir, experiment_id
    )
    child_definition = child_manifest.get("definition")
    child_plan = (
        child_definition.get("point_plan")
        if isinstance(child_definition, dict)
        else None
    )
    child_source = child_plan.get("source") if isinstance(child_plan, dict) else None
    if not isinstance(child_source, dict) or any(
        child_source.get(name) != active.get(name)
        for name in ("adaptive_id", "batch_index", "check_id", "variable", "values")
    ):
        raise ValueError("adaptive child experiment provenance does not match")
    definition = manifest["definition"]
    bracket = manifest["current_bracket"]
    assert isinstance(definition, dict) and isinstance(bracket, dict)
    variable = str(definition["variable"])
    check_id = str(definition["check_id"])
    result_points = results["points"]
    assert isinstance(result_points, list)
    observations: list[dict[str, object]] = []
    for point in sorted(result_points, key=lambda item: item["index"]):
        requirement = _requirement(point, check_id)
        value = str(point["parameters"][variable])
        observations.append(
            {
                "input": value,
                "margin": requirement["margin"],
                "passed": requirement["passed"],
                "value": requirement["value"],
                "point_index": int(point["index"]),
                "parameters": dict(point["parameters"]),
                "evidence_path": f"{experiment_id}/point-{int(point['index']):04d}/",
            }
        )
    low_before = bracket["low"]
    high_before = bracket["high"]
    assert isinstance(low_before, dict) and isinstance(high_before, dict)
    low_after, high_after = _refine_bracket(low_before, high_before, observations)
    width_before = _decimal(high_before["input"], "high input") - _decimal(
        low_before["input"], "low input"
    )
    width_after = _decimal(high_after["input"], "high input") - _decimal(
        low_after["input"], "low input"
    )
    history = manifest["history"]
    assert isinstance(history, list)
    history.append(
        {
            "batch_index": active["batch_index"],
            "experiment_id": experiment_id,
            "values": active["values"],
            "bracket_before": {"low": low_before, "high": high_before},
            "observations": observations,
            "bracket_after": {"low": low_after, "high": high_after},
            "width_before": _canonical_decimal(width_before),
            "width_after": _canonical_decimal(width_after),
            "shrink_ratio": float(width_after / width_before),
            "cumulative_samples": int(manifest["sample_count"]) + len(observations),
        }
    )
    manifest["current_bracket"] = {"low": low_after, "high": high_after}
    manifest["sample_count"] = int(manifest["sample_count"]) + len(observations)
    manifest["active_batch"] = None
    manifest["status"] = "running"


def advance_adaptive_boundary_study(
    runs_dir: Path,
    adaptive_id: str,
    manager: _ExperimentManager,
) -> AdaptiveBoundaryStudyResult:
    """Incorporate one terminal batch or launch the next deterministic batch."""
    with _LOCK:
        path, manifest = _load_manifest(runs_dir, adaptive_id)
        if manifest["status"] in {"completed", "failed"}:
            return _snapshot(manifest, path)
        active = manifest.get("active_batch")
        if isinstance(active, dict):
            child = manager.snapshot(str(active["experiment_id"]))
            child_status = str(child["status"])
            if child_status == "defined":
                manager.start(str(active["experiment_id"]))
                return _snapshot(manifest, path)
            if child_status not in {"completed", "failed", "cancelled"}:
                return _snapshot(manifest, path)
            if child_status != "completed":
                manifest.update(
                    status="failed",
                    stop_reason="child_experiment_failure",
                    error=f"adaptive child experiment ended as {child_status}",
                )
                _write_manifest(path, manifest)
                return _snapshot(manifest, path)
            try:
                _incorporate_batch(runs_dir, manifest, active)
            except Exception as exc:
                manifest.update(
                    status="failed",
                    stop_reason="invalid_boundary_evidence",
                    error=str(exc),
                )
                _write_manifest(path, manifest)
                return _snapshot(manifest, path)
            if _finish_if_needed(manifest):
                _write_manifest(path, manifest)
                return _snapshot(manifest, path)

        definition = manifest["definition"]
        bracket = manifest["current_bracket"]
        assert isinstance(definition, dict) and isinstance(bracket, dict)
        remaining = int(definition["max_samples"]) - int(manifest["sample_count"])
        count = min(int(definition["batch_size"]), remaining)
        low = bracket["low"]
        high = bracket["high"]
        assert isinstance(low, dict) and isinstance(high, dict)
        try:
            values = _interior_values(str(low["input"]), str(high["input"]), count)
        except ValueError as exc:
            manifest.update(
                status="completed",
                stop_reason="numeric_resolution",
                error=str(exc),
            )
            _write_manifest(path, manifest)
            return _snapshot(manifest, path)
        variable = str(definition["variable"])
        base_parameters = dict(low["parameters"])
        points: list[dict[str, str]] = []
        for value in values:
            parameters = dict(base_parameters)
            parameters[variable] = value
            points.append(parameters)
        batch_index = len(manifest["history"])
        source = {
            "kind": "adaptive_boundary_batch",
            "adaptive_id": adaptive_id,
            "batch_index": batch_index,
            "check_id": definition["check_id"],
            "variable": variable,
            "values": values,
        }
        child = manager.define_explicit(
            definition["netlist_template"],
            definition["parameter_order"],
            points,
            definition["parameter_units"],
            source,
            definition["waveform_analyses"],
            definition["filename"],
            definition["ascii_raw"],
            definition["timeout_seconds"],
            definition["max_concurrency"],
            definition["reuse_cache"],
        )
        experiment_id = str(child["experiment_id"])
        manifest["active_batch"] = {
            **source,
            "experiment_id": experiment_id,
        }
        manifest["status"] = "running"
        _write_manifest(path, manifest)
        manager.start(experiment_id)
        return _snapshot(manifest, path)


def get_adaptive_boundary_study(
    runs_dir: Path, adaptive_id: str
) -> AdaptiveBoundaryStudyResult:
    with _LOCK:
        path, manifest = _load_manifest(runs_dir, adaptive_id)
        return _snapshot(manifest, path)
