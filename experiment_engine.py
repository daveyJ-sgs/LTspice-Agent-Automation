"""Portable experiment definitions, persistence, and durable job execution."""

from __future__ import annotations

import csv
import hashlib
import html
import itertools
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Callable, NotRequired, TypedDict

import artifacts
import frequency_domain_metrics
import waveform_metrics

MAX_EXPERIMENT_POINTS = 1000
MAX_EXPERIMENT_WORKERS = 4
MAX_WAVEFORM_ANALYSES = 32
MAX_REQUIREMENTS_PER_EXPERIMENT = 256
MAX_TIMEOUT_SECONDS = 3_600
EXPERIMENT_ENGINE_VERSION = 1


def _netlist_filename(filename: str) -> str:
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError("filename must be a plain file name without directories")
    if not filename.lower().endswith((".cir", ".net")):
        filename += ".cir"
    return filename


def _validate_timeout(timeout_seconds: int) -> None:
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
        )


def _validate_reuse_cache(reuse_cache: bool) -> None:
    if not isinstance(reuse_cache, bool):
        raise ValueError("reuse_cache must be a boolean")


class WaveformAnalysisResult(TypedDict):
    raw_file: str
    raw_sha256: NotRequired[str]
    raw_size_bytes: NotRequired[int]
    variable: str
    axis_variable: str
    step_index: int
    source_points: int
    analysis_resolution: str
    all_passed: bool
    results: list[waveform_metrics.RequirementResult]
    secondary_variable: str | None


class ExperimentParameter(TypedDict):
    name: str
    values: list[str]
    unit: NotRequired[str]


class ExperimentDerivedParameter(TypedDict):
    name: str
    template: str
    unit: NotRequired[str]


class ExperimentWaveformAnalysis(TypedDict):
    name: str
    variable: str
    requirements: list[waveform_metrics.WaveformRequirement]
    axis_variable: NotRequired[str]
    secondary_variable: NotRequired[str]
    signal_unit: NotRequired[str]
    axis_unit: NotRequired[str]
    raw_filename: NotRequired[str]


class ExperimentAnalysisResult(TypedDict):
    name: str
    status: str
    error: str | None
    analysis: WaveformAnalysisResult | None


class ExperimentPointResult(TypedDict):
    index: int
    parameters: dict[str, str]
    run_dir: str
    simulation_status: str
    duration_seconds: float | None
    measurements: dict[str, float]
    analyses: list[ExperimentAnalysisResult]
    all_passed: bool
    error: str | None
    cache_hit: NotRequired[bool]
    cache_key: NotRequired[str | None]
    native_step_index: NotRequired[int]


class ExperimentResult(TypedDict):
    experiment_id: str
    experiment_dir: str
    manifest: str
    results_json: str
    results_csv: str
    status: str
    parameter_order: list[str]
    derived_parameter_order: list[str]
    parameter_units: dict[str, str]
    point_count: int
    completed_points: int
    error_points: int
    passed_points: int
    failed_points: int
    all_passed: bool
    points: list[ExperimentPointResult]
    execution_mode: str
    native_batch: dict[str, object] | None


class ExperimentJobSnapshot(TypedDict):
    experiment_id: str
    experiment_dir: str
    manifest: str
    results_json: str
    results_csv: str
    status: str
    point_count: int
    finished_points: int
    pending_points: int
    running_points: int
    completed_points: int
    error_points: int
    passed_points: int
    failed_points: int
    all_passed: bool | None
    error: str | None
    execution_mode: str


class MeasurementComparison(TypedDict):
    name: str
    status: str
    baseline: float | None
    candidate: float | None
    delta: float | None


class RequirementComparison(TypedDict):
    check_id: str
    analysis_name: str
    metric: str
    operator: str
    target: float
    unit: str
    parameters: dict[str, float | int | str]
    status: str
    baseline_value: float | None
    candidate_value: float | None
    value_delta: float | None
    baseline_passed: bool | None
    candidate_passed: bool | None


class ExperimentPointComparison(TypedDict):
    status: str
    parameters: dict[str, str]
    baseline_index: int | None
    candidate_index: int | None
    baseline_all_passed: bool | None
    candidate_all_passed: bool | None
    baseline_error: str | None
    candidate_error: str | None
    measurements: list[MeasurementComparison]
    requirements: list[RequirementComparison]


class ExperimentComparisonResult(TypedDict):
    schema_version: int
    comparison_id: str
    comparison_dir: str
    comparison_json: str
    comparison_markdown: str
    baseline_experiment_id: str
    candidate_experiment_id: str
    matched_points: int
    added_points: int
    removed_points: int
    matched_measurements: int
    added_measurements: int
    removed_measurements: int
    requirement_regressions: int
    requirement_improvements: int
    unchanged_requirements: int
    added_requirements: int
    removed_requirements: int
    points: list[ExperimentPointComparison]


# --- internal helpers -------------------------------------------------
def _write_json(path: Path, value: object) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _prepare_experiment(
    netlist_template: str,
    parameters: list[ExperimentParameter],
    waveform_analyses: list[ExperimentWaveformAnalysis],
    derived_parameters: list[ExperimentDerivedParameter] | None = None,
) -> tuple[list[str], list[str], list[dict[str, str]], dict[str, str]]:
    if not isinstance(netlist_template, str) or not netlist_template.strip():
        raise ValueError("netlist_template must be a non-empty string")
    if not isinstance(parameters, list) or not parameters:
        raise ValueError("parameters must be a non-empty list")
    if not isinstance(waveform_analyses, list):
        raise ValueError("waveform_analyses must be a list")
    if len(waveform_analyses) > MAX_WAVEFORM_ANALYSES:
        raise ValueError(
            f"experiment is limited to {MAX_WAVEFORM_ANALYSES} waveform analyses"
        )
    if derived_parameters is None:
        derived_parameters = []
    if not isinstance(derived_parameters, list):
        raise ValueError("derived_parameters must be a list")

    names: list[str] = []
    value_sets: list[list[str]] = []
    units: dict[str, str] = {}
    point_count = 1
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise ValueError("parameters must contain objects")
        name = parameter.get("name")
        values = parameter.get("values")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("parameter names must match [A-Za-z_][A-Za-z0-9_]*")
        if name in names:
            raise ValueError(f"duplicate parameter name: {name}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"parameter {name} values must be a non-empty list")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"parameter {name} values must be non-empty strings")
        if len(set(values)) != len(values):
            raise ValueError(f"parameter {name} values must be unique")
        unit = parameter.get("unit", "")
        if not isinstance(unit, str):
            raise ValueError(f"parameter {name} unit must be a string")
        names.append(name)
        value_sets.append(values)
        units[name] = unit
        point_count *= len(values)
        if point_count > MAX_EXPERIMENT_POINTS:
            raise ValueError(
                f"experiment is limited to {MAX_EXPERIMENT_POINTS} Cartesian points"
            )

    derived_order: list[str] = []
    derived_templates: dict[str, str] = {}
    derived_dependencies: dict[str, list[str]] = {}
    declared_names = set(names)
    for parameter in derived_parameters:
        if not isinstance(parameter, dict):
            raise ValueError("derived_parameters must contain objects")
        name = parameter.get("name")
        template = parameter.get("template")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("derived parameter names must match [A-Za-z_][A-Za-z0-9_]*")
        if name in declared_names:
            raise ValueError(f"duplicate experiment parameter name: {name}")
        if not isinstance(template, str) or not template:
            raise ValueError(
                f"derived parameter {name} template must be a non-empty string"
            )
        unit = parameter.get("unit", "")
        if not isinstance(unit, str):
            raise ValueError(f"derived parameter {name} unit must be a string")
        declared_names.add(name)
        derived_order.append(name)
        derived_templates[name] = template
        units[name] = unit

    placeholder_pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for name in derived_order:
        dependencies = placeholder_pattern.findall(derived_templates[name])
        unknown = next(
            (dependency for dependency in dependencies if dependency not in declared_names),
            None,
        )
        if unknown is not None:
            raise ValueError(
                f"derived parameter {name} references unknown parameter {unknown}"
            )
        derived_dependencies[name] = dependencies

    resolution_order: list[str] = []
    resolved_names = set(names)
    pending = list(derived_order)
    while pending:
        ready = [
            name
            for name in pending
            if all(dependency in resolved_names for dependency in derived_dependencies[name])
        ]
        if not ready:
            cycle = _find_derived_cycle(pending, derived_dependencies)
            raise ValueError(f"derived parameter cycle: {' -> '.join(cycle)}")
        for name in ready:
            resolution_order.append(name)
            resolved_names.add(name)
            pending.remove(name)

    if not derived_order:
        for name in names:
            placeholder = "{" + name + "}"
            if placeholder not in netlist_template:
                raise ValueError(
                    f"parameter placeholder {placeholder} is missing from netlist_template"
                )
    else:
        reachable = {
            name
            for name in placeholder_pattern.findall(netlist_template)
            if name in declared_names
        }
        changed = True
        while changed:
            changed = False
            for name in derived_order:
                if name in reachable:
                    for dependency in derived_dependencies[name]:
                        if dependency not in reachable:
                            reachable.add(dependency)
                            changed = True
        for name in [*names, *derived_order]:
            if name not in reachable:
                raise ValueError(
                    f"experiment parameter {name} is not reachable from netlist_template"
                )

    analysis_names: set[str] = set()
    requirement_count = 0
    supported_metrics = (
        waveform_metrics.SUPPORTED_METRICS | frequency_domain_metrics.SUPPORTED_METRICS
    )
    for analysis in waveform_analyses:
        if not isinstance(analysis, dict):
            raise ValueError("waveform_analyses must contain objects")
        name = analysis.get("name")
        variable = analysis.get("variable")
        requirements = analysis.get("requirements")
        if not isinstance(name, str) or not name:
            raise ValueError("waveform analysis names must be non-empty strings")
        if name in analysis_names:
            raise ValueError(f"duplicate waveform analysis name: {name}")
        if not isinstance(variable, str) or not variable:
            raise ValueError(f"waveform analysis {name} variable must be non-empty")
        if not isinstance(requirements, list) or not requirements:
            raise ValueError(f"waveform analysis {name} requirements must be non-empty")
        requirement_count += len(requirements)
        if requirement_count > MAX_REQUIREMENTS_PER_EXPERIMENT:
            raise ValueError(
                "experiment is limited to "
                f"{MAX_REQUIREMENTS_PER_EXPERIMENT} waveform requirements"
            )
        for field in (
            "axis_variable",
            "secondary_variable",
            "signal_unit",
            "axis_unit",
            "raw_filename",
        ):
            if field in analysis and not isinstance(analysis[field], str):
                raise ValueError(f"waveform analysis {name} {field} must be a string")
        raw_filename = analysis.get("raw_filename")
        if raw_filename is not None and (
            Path(raw_filename).name != raw_filename
            or "/" in raw_filename
            or "\\" in raw_filename
        ):
            raise ValueError(
                f"waveform analysis {name} raw_filename must be a plain file name"
            )
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise ValueError(f"waveform analysis {name} requirements must be objects")
            missing = [
                field for field in ("metric", "operator", "target") if field not in requirement
            ]
            if missing:
                raise ValueError(
                    f"waveform analysis {name} requirement is missing {missing[0]}"
                )
            if requirement["metric"] not in supported_metrics:
                raise ValueError(
                    f"waveform analysis {name} has unknown metric {requirement['metric']}"
                )
            if requirement["operator"] not in {"<", "<=", ">", ">="}:
                raise ValueError(
                    f"waveform analysis {name} has invalid comparison operator"
                )
            try:
                float(requirement["target"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"waveform analysis {name} requirement target must be numeric"
                ) from exc
        analysis_names.add(name)

    points: list[dict[str, str]] = []
    for combination in itertools.product(*value_sets):
        base_values = dict(zip(names, combination))
        resolved_values = dict(base_values)
        for name in resolution_order:
            resolved_values[name] = placeholder_pattern.sub(
                lambda match: resolved_values[match.group(1)],
                derived_templates[name],
            )
        points.append(
            {
                **base_values,
                **{name: resolved_values[name] for name in derived_order},
            }
        )
    return names, derived_order, points, units


def _prepare_explicit_experiment(
    netlist_template: str,
    parameter_order: list[str],
    points: list[dict[str, str]],
    parameter_units: dict[str, str],
    waveform_analyses: list[ExperimentWaveformAnalysis],
) -> tuple[list[str], list[str], list[dict[str, str]], dict[str, str]]:
    """Validate paired points while reusing the ordinary experiment contract."""
    if not isinstance(parameter_order, list) or not parameter_order:
        raise ValueError("explicit parameter_order must be a non-empty list")
    if (
        any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            for name in parameter_order
        )
        or len(parameter_order) != len(set(parameter_order))
    ):
        raise ValueError("explicit parameter_order contains invalid or duplicate names")
    if (
        not isinstance(points, list)
        or not points
        or len(points) > MAX_EXPERIMENT_POINTS
    ):
        raise ValueError(
            f"explicit point plans must contain between 1 and {MAX_EXPERIMENT_POINTS} points"
        )
    if (
        not isinstance(parameter_units, dict)
        or set(parameter_units) != set(parameter_order)
        or any(not isinstance(unit, str) for unit in parameter_units.values())
    ):
        raise ValueError("explicit parameter_units must match parameter_order")
    copied_points: list[dict[str, str]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict) or set(point) != set(parameter_order):
            raise ValueError(f"explicit point {index} parameters do not match parameter_order")
        if any(not isinstance(point[name], str) or not point[name] for name in parameter_order):
            raise ValueError(f"explicit point {index} values must be non-empty strings")
        copied_points.append({name: point[name] for name in parameter_order})

    validation_parameters: list[ExperimentParameter] = [
        {
            "name": name,
            "values": [copied_points[0][name]],
            "unit": parameter_units[name],
        }
        for name in parameter_order
    ]
    _prepare_experiment(
        netlist_template,
        validation_parameters,
        waveform_analyses,
    )
    return list(parameter_order), [], copied_points, dict(parameter_units)


def _find_derived_cycle(
    pending: list[str], dependencies: dict[str, list[str]]
) -> list[str]:
    pending_set = set(pending)
    for start in pending:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in pending_set:
            if current in positions:
                return [*path[positions[current]:], current]
            positions[current] = len(path)
            path.append(current)
            next_name = next(
                (name for name in dependencies[current] if name in pending_set),
                None,
            )
            if next_name is None:
                break
            current = next_name
    return [*pending, pending[0]]


def _render_experiment_netlist(
    netlist_template: str, parameters: dict[str, str]
) -> str:
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: parameters.get(match.group(1), match.group(0)),
        netlist_template,
    )


_NATIVE_STEP_PARAMETER = "__mcp_step_index"
_NATIVE_VALUE_PREFIX = "__mcp_value_"


def _native_table_directive(name: str, values: list[str]) -> str:
    entries = [f"{index},{value}" for index, value in enumerate(values)]
    prefix = f".param {name}=table({_NATIVE_STEP_PARAMETER},"
    lines: list[str] = []
    current = prefix
    for entry in entries:
        separator = "" if current.endswith(",") else ","
        if len(current) + len(separator) + len(entry) + 1 <= 78:
            current += separator + entry
            continue
        if current == prefix or len("+ ," + entry + ")") > 78:
            raise ValueError("native execution parameter expression is too long")
        lines.append(current)
        current = "+ ," + entry
    lines.append(current + ")")
    return "\n".join(lines)


def _native_step_directive(point_count: int) -> str:
    return f".step param {_NATIVE_STEP_PARAMETER} 0 {point_count - 1} 1"


def _render_native_experiment_netlist(
    netlist_template: str,
    parameter_order: list[str],
    derived_parameter_order: list[str],
    combinations: list[dict[str, str]],
) -> str:
    """Render one indexed LTspice deck for an ordered Cartesian experiment."""
    if re.search(r"(?im)^\s*\.step\b", netlist_template):
        raise ValueError("native execution does not allow an existing .step directive")
    if re.search(r"(?i)\b__mcp_[A-Za-z0-9_]*\b", netlist_template):
        raise ValueError("native execution reserves the __mcp_ identifier namespace")

    end_matches = list(
        re.finditer(r"(?im)^\s*\.end\s*(?:;[^\r\n]*)?$", netlist_template)
    )
    if len(end_matches) != 1:
        raise ValueError("native execution requires exactly one active .end directive")

    names = [*parameter_order, *derived_parameter_order]
    placeholder_pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    unsafe_directives = re.compile(
        r"(?i)^\s*\.(?:include|inc|lib|model|subckt|ends|title|wave)\b"
    )
    declared_names = set(names)
    for line in netlist_template.splitlines():
        line_names = {
            match.group(1)
            for match in placeholder_pattern.finditer(line)
            if match.group(1) in declared_names
        }
        if line_names and unsafe_directives.match(line):
            raise ValueError(
                "native execution only supports numeric-expression placeholders; "
                f"unsupported directive context: {line.strip()}"
            )

    safe_expression = re.compile(r"[A-Za-z0-9_.+\-*/^() ]+")
    for combination in combinations:
        for name in names:
            value = combination[name]
            if (
                not any(character.isdigit() for character in value)
                or safe_expression.fullmatch(value) is None
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(
                    "native execution parameter values must be safe numeric "
                    f"expressions without commas or braces: {name}={value!r}"
                )

    generated_names = {
        name: f"{_NATIVE_VALUE_PREFIX}{index}" for index, name in enumerate(names)
    }
    rendered = placeholder_pattern.sub(
        lambda match: (
            "{" + generated_names[match.group(1)] + "}"
            if match.group(1) in generated_names
            else match.group(0)
        ),
        netlist_template,
    )
    directives = [
        _native_table_directive(
            generated_names[name], [combination[name] for combination in combinations]
        )
        for name in names
    ]
    directives.append(_native_step_directive(len(combinations)))
    newline = "\r\n" if "\r\n" in netlist_template else "\n"
    insertion = ("\n".join(directives) + "\n").replace("\n", newline)
    end_match = re.search(r"(?im)^\s*\.end\s*(?:;[^\r\n]*)?$", rendered)
    assert end_match is not None
    return rendered[: end_match.start()] + insertion + rendered[end_match.start() :]


def _write_experiment_csv(
    path: Path,
    parameter_order: list[str],
    points: list[ExperimentPointResult],
) -> None:
    def spreadsheet_safe(value: object) -> object:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return f"'{value}"
        return value

    measurement_names = sorted(
        {
            name
            for point in points
            for name in point["measurements"]
        }
    )
    fieldnames = [
        "point_index",
        *(f"parameter.{name}" for name in parameter_order),
        "simulation_status",
        "all_passed",
        "duration_seconds",
        "run_dir",
        "error",
        *(f"measurement.{name}" for name in measurement_names),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    "point_index": point["index"],
                    **{
                        f"parameter.{name}": spreadsheet_safe(point["parameters"][name])
                        for name in parameter_order
                    },
                    "simulation_status": point["simulation_status"],
                    "all_passed": point["all_passed"],
                    "duration_seconds": point["duration_seconds"],
                    "run_dir": spreadsheet_safe(point["run_dir"]),
                    "error": spreadsheet_safe(point["error"]),
                    **{
                        f"measurement.{name}": value
                        for name, value in point["measurements"].items()
                    },
                }
            )


def _experiment_counts(
    points: list[ExperimentPointResult], point_count: int
) -> dict[str, int]:
    completed_points = sum(
        point["simulation_status"] == "completed" for point in points
    )
    error_points = sum(
        point["simulation_status"] not in {"completed", "cancelled"}
        or any(analysis["status"] == "error" for analysis in point["analyses"])
        for point in points
    )
    passed_points = sum(point["all_passed"] for point in points)
    return {
        "finished_points": len(points),
        "pending_points": point_count - len(points),
        "completed_points": completed_points,
        "error_points": error_points,
        "passed_points": passed_points,
        "failed_points": len(points) - passed_points,
    }


def _definition_hash(definition: dict[str, object]) -> str:
    return artifacts.definition_hash(definition)


def _write_text(path: Path, value: str) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_path.write_text(value, encoding="utf-8")
    os.replace(temporary_path, path)


_canonical_json = artifacts.canonical_json


def _finite_number(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _finite_delta(candidate: float, baseline: float, field: str) -> float:
    delta = candidate - baseline
    if not math.isfinite(delta):
        raise ValueError(f"{field} delta must be finite")
    return delta


def _comparison_experiment_path(runs_dir: Path, experiment_id: str) -> Path:
    if (
        not isinstance(experiment_id, str)
        or not experiment_id.startswith("mcp-experiment-")
        or Path(experiment_id).name != experiment_id
        or "/" in experiment_id
        or "\\" in experiment_id
    ):
        raise ValueError("experiment_id must be a plain mcp-experiment-* directory name")
    experiment_dir = runs_dir / experiment_id
    if not experiment_dir.is_dir():
        raise FileNotFoundError(f"Experiment not found: {experiment_id}")
    try:
        experiment_dir.resolve().relative_to(runs_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Experiment must be inside the runs directory: {experiment_id}") from exc
    return experiment_dir


def _normalize_requirement(
    analysis_name: str, result: object, point_index: int
) -> tuple[str, dict[str, object]]:
    prefix = f"point {point_index} requirement"
    if not isinstance(result, dict):
        raise ValueError(f"{prefix} must be an object")
    metric = result.get("metric")
    threshold = result.get("threshold")
    parameters = result.get("parameters")
    if not isinstance(metric, str) or not metric:
        raise ValueError(f"{prefix} metric must be a non-empty string")
    if not isinstance(threshold, dict):
        raise ValueError(f"{prefix} threshold must be an object")
    operator = threshold.get("operator")
    unit = threshold.get("unit")
    if not isinstance(operator, str) or not operator:
        raise ValueError(f"{prefix} threshold operator must be a non-empty string")
    if not isinstance(unit, str):
        raise ValueError(f"{prefix} threshold unit must be a string")
    target = _finite_number(threshold.get("target"), f"{prefix} threshold target")
    if not isinstance(parameters, dict) or not all(
        isinstance(name, str) for name in parameters
    ):
        raise ValueError(f"{prefix} parameters must be an object with string keys")
    normalized_parameters: dict[str, float | int | str] = {}
    for name, value in parameters.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{prefix} parameter {name} must be a scalar")
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError(f"{prefix} parameter {name} must be finite")
        normalized_parameters[name] = value
    value = _finite_number(result.get("value"), f"{prefix} value")
    passed = result.get("passed")
    if not isinstance(passed, bool):
        raise ValueError(f"{prefix} passed must be a boolean")
    identity = {
        "analysis_name": analysis_name,
        "metric": metric,
        "operator": operator,
        "target": target,
        "unit": unit,
        "parameters": normalized_parameters,
    }
    identity_key = _canonical_json(identity)
    return identity_key, {
        **identity,
        "check_id": hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16],
        "value": value,
        "passed": passed,
    }


def _normalize_comparison_point(
    point: object, *, explicit_point_plan: bool = False
) -> dict[str, object]:
    if not isinstance(point, dict):
        raise ValueError("experiment points must contain objects")
    index = point.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("point index must be a nonnegative integer")
    parameters = point.get("parameters")
    if not isinstance(parameters, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in parameters.items()
    ):
        raise ValueError(f"point {index} parameters must map strings to strings")
    measurements = point.get("measurements")
    if not isinstance(measurements, dict) or not all(
        isinstance(name, str) for name in measurements
    ):
        raise ValueError(f"point {index} measurements must be an object")
    normalized_measurements = {
        name: _finite_number(value, f"point {index} measurement {name}")
        for name, value in measurements.items()
    }
    all_passed = point.get("all_passed")
    error = point.get("error")
    analyses = point.get("analyses")
    if not isinstance(all_passed, bool):
        raise ValueError(f"point {index} all_passed must be a boolean")
    if error is not None and not isinstance(error, str):
        raise ValueError(f"point {index} error must be a string or null")
    if not isinstance(analyses, list):
        raise ValueError(f"point {index} analyses must be a list")
    requirements: dict[str, dict[str, object]] = {}
    for entry in analyses:
        if not isinstance(entry, dict):
            raise ValueError(f"point {index} analyses must contain objects")
        analysis_name = entry.get("name")
        analysis = entry.get("analysis")
        if not isinstance(analysis_name, str) or not analysis_name:
            raise ValueError(f"point {index} analysis name must be a non-empty string")
        if analysis is None:
            continue
        if not isinstance(analysis, dict) or not isinstance(analysis.get("results"), list):
            raise ValueError(f"point {index} analysis result must contain a results list")
        for result in analysis["results"]:
            identity_key, normalized = _normalize_requirement(
                analysis_name, result, index
            )
            if identity_key in requirements:
                raise ValueError(
                    f"point {index} has duplicate requirement identity "
                    f"{normalized['check_id']}"
                )
            requirements[identity_key] = normalized
    return {
        "index": index,
        "parameters": dict(parameters),
        "parameter_key": _canonical_json(
            [index, parameters] if explicit_point_plan else parameters
        ),
        "measurements": normalized_measurements,
        "requirements": requirements,
        "all_passed": all_passed,
        "error": error,
    }


def _load_comparison_experiment(
    runs_dir: Path, experiment_id: str
) -> tuple[list[dict[str, object]], str]:
    import experiment_index

    _, manifest, document, record = experiment_index.load_completed_experiment(
        runs_dir, experiment_id
    )
    definition = manifest.get("definition")
    explicit_point_plan = (
        isinstance(definition, dict)
        and isinstance(definition.get("point_plan"), dict)
    )
    points = [
        _normalize_comparison_point(
            point, explicit_point_plan=explicit_point_plan
        )
        for point in document["points"]
    ]
    indexes: set[int] = set()
    parameter_keys: set[str] = set()
    for point in points:
        index = point["index"]
        parameter_key = point["parameter_key"]
        if index in indexes:
            raise ValueError(f"Experiment {experiment_id} has duplicate point index {index}")
        if parameter_key in parameter_keys:
            raise ValueError(f"Experiment {experiment_id} has duplicate parameter map")
        indexes.add(index)  # type: ignore[arg-type]
        parameter_keys.add(parameter_key)  # type: ignore[arg-type]
    points.sort(key=lambda point: point["index"])
    return points, str(record["results_sha256"])


def _compare_measurements(
    baseline: dict[str, float], candidate: dict[str, float]
) -> list[MeasurementComparison]:
    comparisons: list[MeasurementComparison] = []
    for name in sorted(set(baseline) | set(candidate)):
        baseline_value = baseline.get(name)
        candidate_value = candidate.get(name)
        if baseline_value is None:
            status = "added"
        elif candidate_value is None:
            status = "removed"
        else:
            status = "matched"
        comparisons.append(
            {
                "name": name,
                "status": status,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": None
                if baseline_value is None or candidate_value is None
                else _finite_delta(
                    candidate_value,
                    baseline_value,
                    f"measurement {name}",
                ),
            }
        )
    return comparisons


def _compare_requirements(
    baseline: dict[str, dict[str, object]],
    candidate: dict[str, dict[str, object]],
) -> list[RequirementComparison]:
    comparisons: list[RequirementComparison] = []
    for identity_key in sorted(set(baseline) | set(candidate)):
        baseline_check = baseline.get(identity_key)
        candidate_check = candidate.get(identity_key)
        check = baseline_check or candidate_check
        assert check is not None
        baseline_passed = None if baseline_check is None else baseline_check["passed"]
        candidate_passed = None if candidate_check is None else candidate_check["passed"]
        baseline_value = None if baseline_check is None else baseline_check["value"]
        candidate_value = None if candidate_check is None else candidate_check["value"]
        if baseline_check is None:
            status = "added"
        elif candidate_check is None:
            status = "removed"
        elif baseline_passed and not candidate_passed:
            status = "regression"
        elif not baseline_passed and candidate_passed:
            status = "improvement"
        else:
            status = "unchanged"
        comparisons.append(
            {
                "check_id": check["check_id"],  # type: ignore[typeddict-item]
                "analysis_name": check["analysis_name"],  # type: ignore[typeddict-item]
                "metric": check["metric"],  # type: ignore[typeddict-item]
                "operator": check["operator"],  # type: ignore[typeddict-item]
                "target": check["target"],  # type: ignore[typeddict-item]
                "unit": check["unit"],  # type: ignore[typeddict-item]
                "parameters": check["parameters"],  # type: ignore[typeddict-item]
                "status": status,
                "baseline_value": baseline_value,  # type: ignore[typeddict-item]
                "candidate_value": candidate_value,  # type: ignore[typeddict-item]
                "value_delta": None
                if baseline_value is None or candidate_value is None
                else _finite_delta(
                    candidate_value,  # type: ignore[arg-type]
                    baseline_value,  # type: ignore[arg-type]
                    f"requirement {check['check_id']}",
                ),
                "baseline_passed": baseline_passed,  # type: ignore[typeddict-item]
                "candidate_passed": candidate_passed,  # type: ignore[typeddict-item]
            }
        )
    return comparisons


def _comparison_markdown(result: ExperimentComparisonResult) -> str:
    def cell(value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return format(value, ".12g")
        escaped = html.escape(str(value), quote=False)
        return (
            escaped.replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("`", "\\`")
            .replace("\r\n", "<br>")
            .replace("\n", "<br>")
        )

    lines = [
        "# Experiment comparison",
        "",
        f"Baseline: `{cell(result['baseline_experiment_id'])}`  ",
        f"Candidate: `{cell(result['candidate_experiment_id'])}`",
        "",
        "## Summary",
        "",
        "| Item | Matched/unchanged | Added/improved | Removed/regressed |",
        "| --- | ---: | ---: | ---: |",
        "| Points | "
        f"{result['matched_points']} | {result['added_points']} | "
        f"{result['removed_points']} |",
        "| Measurements | "
        f"{result['matched_measurements']} | {result['added_measurements']} | "
        f"{result['removed_measurements']} |",
        "| Requirements | "
        f"{result['unchanged_requirements']} | "
        f"{result['added_requirements']} added, "
        f"{result['requirement_improvements']} improved | "
        f"{result['removed_requirements']} removed, "
        f"{result['requirement_regressions']} regressed |",
        "",
        "## Points",
        "",
    ]
    for point in result["points"]:
        parameters = ", ".join(
            f"{cell(name)}={cell(value)}" for name, value in sorted(point["parameters"].items())
        )
        lines.extend(
            [
                f"### {cell(point['status']).title()}: {parameters or '(no parameters)'}",
                "",
                f"Baseline index: {cell(point['baseline_index'])}; "
                f"candidate index: {cell(point['candidate_index'])}",
                "",
            ]
        )
        if point["measurements"]:
            lines.extend([
                "| Measurement | Status | Baseline | Candidate | Delta |",
                "| --- | --- | ---: | ---: | ---: |",
            ])
            lines.extend(
                f"| {cell(item['name'])} | {cell(item['status'])} | "
                f"{cell(item['baseline'])} | {cell(item['candidate'])} | "
                f"{cell(item['delta'])} |"
                for item in point["measurements"]
            )
            lines.append("")
        if point["requirements"]:
            lines.extend([
                "| Check | Analysis / metric | Status | Baseline | Candidate | Delta |",
                "| --- | --- | --- | ---: | ---: | ---: |",
            ])
            lines.extend(
                f"| `{cell(item['check_id'])}` | {cell(item['analysis_name'])} / "
                f"{cell(item['metric'])} | {cell(item['status'])} | "
                f"{cell(item['baseline_value'])} | "
                f"{cell(item['candidate_value'])} | "
                f"{cell(item['value_delta'])} |"
                for item in point["requirements"]
            )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def compare_experiments(
    runs_dir: Path,
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> ExperimentComparisonResult:
    """Compare two complete experiment artifacts without running simulations."""
    baseline_points, baseline_hash = _load_comparison_experiment(
        runs_dir, baseline_experiment_id
    )
    candidate_points, candidate_hash = _load_comparison_experiment(
        runs_dir, candidate_experiment_id
    )
    candidate_by_parameters = {
        point["parameter_key"]: point for point in candidate_points
    }
    baseline_keys = {point["parameter_key"] for point in baseline_points}
    point_comparisons: list[ExperimentPointComparison] = []

    def point_comparison(
        status: str,
        baseline: dict[str, object] | None,
        candidate: dict[str, object] | None,
    ) -> ExperimentPointComparison:
        point = baseline or candidate
        assert point is not None
        return {
            "status": status,
            "parameters": point["parameters"],  # type: ignore[typeddict-item]
            "baseline_index": (
                None if baseline is None else baseline["index"]
            ),  # type: ignore[typeddict-item]
            "candidate_index": (
                None if candidate is None else candidate["index"]
            ),  # type: ignore[typeddict-item]
            "baseline_all_passed": (
                None if baseline is None else baseline["all_passed"]
            ),  # type: ignore[typeddict-item]
            "candidate_all_passed": (
                None if candidate is None else candidate["all_passed"]
            ),  # type: ignore[typeddict-item]
            "baseline_error": (
                None if baseline is None else baseline["error"]
            ),  # type: ignore[typeddict-item]
            "candidate_error": (
                None if candidate is None else candidate["error"]
            ),  # type: ignore[typeddict-item]
            "measurements": _compare_measurements(
                {} if baseline is None else baseline["measurements"],  # type: ignore[arg-type]
                {} if candidate is None else candidate["measurements"],  # type: ignore[arg-type]
            ),
            "requirements": _compare_requirements(
                {} if baseline is None else baseline["requirements"],  # type: ignore[arg-type]
                {} if candidate is None else candidate["requirements"],  # type: ignore[arg-type]
            ),
        }

    for baseline in baseline_points:
        candidate = candidate_by_parameters.get(baseline["parameter_key"])
        point_comparisons.append(
            point_comparison("removed" if candidate is None else "matched", baseline, candidate)
        )
    for candidate in candidate_points:
        if candidate["parameter_key"] not in baseline_keys:
            point_comparisons.append(point_comparison("added", None, candidate))

    comparison_key = {
        "schema_version": 1,
        "baseline_experiment_id": baseline_experiment_id,
        "candidate_experiment_id": candidate_experiment_id,
        "baseline_results_sha256": baseline_hash,
        "candidate_results_sha256": candidate_hash,
    }
    comparison_id = hashlib.sha256(
        _canonical_json(comparison_key).encode("utf-8")
    ).hexdigest()[:16]
    comparison_dir = runs_dir / "comparisons" / f"comparison-{comparison_id}"
    try:
        comparison_dir.resolve().relative_to(runs_dir.resolve())
    except ValueError as exc:
        raise ValueError("Comparison output must be inside the runs directory") from exc
    comparison_json = comparison_dir / "comparison.json"
    comparison_markdown = comparison_dir / "comparison.md"
    measurements = [item for point in point_comparisons for item in point["measurements"]]
    requirements = [item for point in point_comparisons for item in point["requirements"]]
    result: ExperimentComparisonResult = {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "comparison_dir": str(comparison_dir),
        "comparison_json": str(comparison_json),
        "comparison_markdown": str(comparison_markdown),
        "baseline_experiment_id": baseline_experiment_id,
        "candidate_experiment_id": candidate_experiment_id,
        "matched_points": sum(point["status"] == "matched" for point in point_comparisons),
        "added_points": sum(point["status"] == "added" for point in point_comparisons),
        "removed_points": sum(point["status"] == "removed" for point in point_comparisons),
        "matched_measurements": sum(item["status"] == "matched" for item in measurements),
        "added_measurements": sum(item["status"] == "added" for item in measurements),
        "removed_measurements": sum(item["status"] == "removed" for item in measurements),
        "requirement_regressions": sum(item["status"] == "regression" for item in requirements),
        "requirement_improvements": sum(item["status"] == "improvement" for item in requirements),
        "unchanged_requirements": sum(item["status"] == "unchanged" for item in requirements),
        "added_requirements": sum(item["status"] == "added" for item in requirements),
        "removed_requirements": sum(item["status"] == "removed" for item in requirements),
        "points": point_comparisons,
    }
    comparison_dir.mkdir(parents=True, exist_ok=True)
    _write_json(comparison_json, result)
    _write_text(comparison_markdown, _comparison_markdown(result))
    return result


def run_experiment_sync(
    runs_dir: Path,
    execute_point: Callable[..., ExperimentPointResult],
    netlist_template: str,
    parameters: list[ExperimentParameter],
    waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    derived_parameters: list[ExperimentDerivedParameter] | None = None,
    reuse_cache: bool = False,
    execution_mode: str = "independent",
    execute_native: Callable[..., tuple[list[ExperimentPointResult], dict[str, object]]] | None = None,
    explicit_points: list[dict[str, str]] | None = None,
    explicit_parameter_order: list[str] | None = None,
    explicit_parameter_units: dict[str, str] | None = None,
    source_point_plan: dict[str, object] | None = None,
) -> ExperimentResult:
    """Execute an experiment independently or as one validated native batch."""
    normalized_filename = _netlist_filename(filename)
    _validate_timeout(timeout_seconds)
    _validate_reuse_cache(reuse_cache)
    if execution_mode not in {"independent", "native"}:
        raise ValueError("execution_mode must be 'independent' or 'native'")
    if execution_mode == "native" and execute_native is None:
        raise ValueError("native execution requires an execute_native callback")
    analyses = [] if waveform_analyses is None else waveform_analyses
    if explicit_points is None:
        (
            parameter_order,
            derived_parameter_order,
            combinations,
            parameter_units,
        ) = _prepare_experiment(
            netlist_template, parameters, analyses, derived_parameters
        )
    else:
        if execution_mode != "independent":
            raise ValueError("explicit point plans currently require independent execution")
        if derived_parameters:
            raise ValueError("explicit point plans do not accept derived_parameters")
        (
            parameter_order,
            derived_parameter_order,
            combinations,
            parameter_units,
        ) = _prepare_explicit_experiment(
            netlist_template,
            [] if explicit_parameter_order is None else explicit_parameter_order,
            explicit_points,
            {} if explicit_parameter_units is None else explicit_parameter_units,
            analyses,
        )
    native_netlist = None
    if execution_mode == "native":
        native_netlist = _render_native_experiment_netlist(
            netlist_template,
            parameter_order,
            derived_parameter_order,
            combinations,
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    experiment_id = f"mcp-experiment-{stamp}"
    experiment_dir = runs_dir / experiment_id
    experiment_dir.mkdir(parents=True)
    manifest_path = experiment_dir / "experiment_manifest.json"
    results_json_path = experiment_dir / "results.json"
    results_csv_path = experiment_dir / "results.csv"
    started_at = datetime.now().astimezone()
    started_clock = time.monotonic()
    definition: dict[str, object] = {
        "netlist_template": netlist_template,
        "parameters": parameters,
        "parameter_order": parameter_order,
        "derived_parameters": []
        if derived_parameters is None
        else derived_parameters,
        "derived_parameter_order": derived_parameter_order,
        "parameter_units": parameter_units,
        "waveform_analyses": analyses,
        "filename": normalized_filename,
        "ascii_raw": ascii_raw,
        "timeout_seconds": timeout_seconds,
        "reuse_cache": reuse_cache,
        "execution_mode": execution_mode,
    }
    if explicit_points is not None:
        definition["parameters"] = []
        definition["point_plan"] = {
            "schema_version": 1,
            "points": combinations,
            "source": {} if source_point_plan is None else source_point_plan,
        }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "status": "running",
        "started_at": started_at.isoformat(),
        "definition": definition,
        "point_count": len(combinations),
    }
    _write_json(manifest_path, manifest)

    def execute(index: int, combination: dict[str, str]) -> ExperimentPointResult:
        arguments = (
            index,
            combination,
            experiment_dir / f"point-{index:04d}",
            netlist_template,
            normalized_filename,
            ascii_raw,
            timeout_seconds,
            analyses,
        )
        if reuse_cache:
            return execute_point(*arguments, None, True)
        return execute_point(*arguments)

    native_batch: dict[str, object] | None = None
    if execution_mode == "native":
        assert execute_native is not None and native_netlist is not None
        points, native_batch = execute_native(
            combinations,
            experiment_dir / "native-batch",
            native_netlist,
            normalized_filename,
            ascii_raw,
            timeout_seconds,
            analyses,
            reuse_cache,
        )
    else:
        points = [
            execute(index, combination) for index, combination in enumerate(combinations)
        ]
    counts = _experiment_counts(points, len(combinations))
    completed_points = counts["completed_points"]
    error_points = counts["error_points"]
    passed_points = counts["passed_points"]
    result: ExperimentResult = {
        "experiment_id": experiment_id,
        "experiment_dir": str(experiment_dir),
        "manifest": str(manifest_path),
        "results_json": str(results_json_path),
        "results_csv": str(results_csv_path),
        "status": "completed",
        "parameter_order": parameter_order,
        "derived_parameter_order": derived_parameter_order,
        "parameter_units": parameter_units,
        "point_count": len(points),
        "completed_points": completed_points,
        "error_points": error_points,
        "passed_points": passed_points,
        "failed_points": len(points) - passed_points,
        "all_passed": passed_points == len(points),
        "points": points,
        "execution_mode": execution_mode,
        "native_batch": native_batch,
    }
    _write_experiment_csv(
        results_csv_path,
        [*parameter_order, *derived_parameter_order],
        points,
    )
    _write_json(results_json_path, result)

    manifest.update(
        status="completed",
        finished_at=datetime.now().astimezone().isoformat(),
        duration_seconds=time.monotonic() - started_clock,
        completed_points=completed_points,
        error_points=error_points,
        passed_points=passed_points,
        failed_points=len(points) - passed_points,
        all_passed=result["all_passed"],
        artifacts=[results_json_path.name, results_csv_path.name],
        points=[
            {
                "index": point["index"],
                "parameters": point["parameters"],
                "run_dir": point["run_dir"],
                "simulation_status": point["simulation_status"],
                "all_passed": point["all_passed"],
                "error": point["error"],
            }
            for point in points
        ],
    )
    if native_batch is not None:
        manifest["native_batch"] = native_batch
    _write_json(manifest_path, manifest)
    return result


class _RunsProcessLock:
    """Hold one cross-platform advisory lock for a runs directory."""

    def __init__(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError("experiment manager lock must not be a symlink")
        self._handle = path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise RuntimeError(
                "another experiment manager already owns this runs directory"
            ) from exc
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._released = True


class ExperimentJobManager:
    """Runs one durable experiment at a time with bounded point concurrency."""

    def __init__(
        self,
        runs_dir: Path,
        workers: int = MAX_EXPERIMENT_WORKERS,
        *,
        execute_point: Callable[..., ExperimentPointResult],
        execute_native: Callable[
            ...,
            tuple[list[ExperimentPointResult], dict[str, object]],
        ]
        | None = None,
    ) -> None:
        if workers < 1 or workers > MAX_EXPERIMENT_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_EXPERIMENT_WORKERS}")
        self.runs_dir = runs_dir
        self.workers = workers
        self._execute_point = execute_point
        self._execute_native = execute_native
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = _RunsProcessLock(self.runs_dir / ".experiment-manager.lock")
        try:
            self._lock = threading.RLock()
            self._stopping = threading.Event()
            self._events: dict[str, threading.Event] = {}
            self._queue: queue.Queue[str | None] = queue.Queue()
            self._executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="ltspice-experiment-point",
            )
            self._recover_jobs()
            self._coordinator = threading.Thread(
                target=self._coordinate,
                name="ltspice-experiment-coordinator",
                daemon=True,
            )
            self._coordinator.start()
        except Exception:
            self._process_lock.release()
            raise

    def _experiment_dir(self, experiment_id: str) -> Path:
        if (
            not isinstance(experiment_id, str)
            or not experiment_id.startswith("mcp-experiment-")
            or Path(experiment_id).name != experiment_id
            or "/" in experiment_id
            or "\\" in experiment_id
        ):
            raise ValueError("invalid experiment_id")
        candidate = self.runs_dir / experiment_id
        if not candidate.is_dir():
            raise FileNotFoundError(f"experiment not found: {experiment_id}")
        runs_dir = self.runs_dir.resolve()
        experiment_dir = candidate.resolve()
        try:
            experiment_dir.relative_to(runs_dir)
        except ValueError as exc:
            raise ValueError("experiment must remain inside the runs directory") from exc
        if experiment_dir.parent != runs_dir or experiment_dir.name != experiment_id:
            raise ValueError("experiment directory must be a direct, named child of runs")
        return experiment_dir

    def _load_manifest(self, experiment_id: str) -> dict[str, object]:
        path = self._experiment_dir(experiment_id) / "experiment_manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid experiment manifest: {experiment_id}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid experiment manifest: {experiment_id}")
        if value.get("experiment_id") != experiment_id:
            raise ValueError(f"experiment manifest ID does not match: {experiment_id}")
        return value

    def _save_manifest(self, manifest: dict[str, object]) -> None:
        manifest["updated_at"] = datetime.now().astimezone().isoformat()
        experiment_dir = self._experiment_dir(str(manifest["experiment_id"]))
        _write_json(experiment_dir / "experiment_manifest.json", manifest)

    def _recover_jobs(self) -> None:
        for manifest_path in sorted(self.runs_dir.glob("mcp-experiment-*/experiment_manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
                continue
            status = manifest.get("status")
            experiment_id = str(manifest.get("experiment_id", ""))
            if experiment_id != manifest_path.parent.name:
                continue
            if status in {"queued", "running"}:
                manifest["status"] = "queued"
                manifest["running_points"] = 0
                self._save_manifest(manifest)
                event = threading.Event()
                if manifest.get("cancel_requested") is True:
                    event.set()
                self._events[experiment_id] = event
                self._queue.put(experiment_id)
            elif status == "cancelling":
                definition = manifest.get("definition")
                execution_mode = (
                    definition.get("execution_mode", "independent")
                    if isinstance(definition, dict)
                    else "independent"
                )
                if execution_mode == "native":
                    manifest["status"] = "queued"
                    manifest["running_points"] = 0
                    manifest["cancel_requested"] = True
                    self._save_manifest(manifest)
                    event = threading.Event()
                    event.set()
                    self._events[experiment_id] = event
                    self._queue.put(experiment_id)
                else:
                    manifest["status"] = "cancelled"
                    manifest["running_points"] = 0
                    self._save_manifest(manifest)

    def define(
        self,
        netlist_template: str,
        parameters: list[ExperimentParameter],
        waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
        filename: str = "circuit.cir",
        ascii_raw: bool = False,
        timeout_seconds: int = 120,
        derived_parameters: list[ExperimentDerivedParameter] | None = None,
        max_concurrency: int = 2,
        reuse_cache: bool = False,
        execution_mode: str = "independent",
    ) -> ExperimentJobSnapshot:
        normalized_filename = _netlist_filename(filename)
        _validate_timeout(timeout_seconds)
        _validate_reuse_cache(reuse_cache)
        if execution_mode not in {"independent", "native"}:
            raise ValueError("execution_mode must be 'independent' or 'native'")
        if execution_mode == "native" and self._execute_native is None:
            raise ValueError("native execution requires an execute_native callback")
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise ValueError("max_concurrency must be an integer")
        concurrency_limit = (
            self.workers
            if execution_mode == "independent"
            else MAX_EXPERIMENT_WORKERS
        )
        if max_concurrency < 1 or max_concurrency > concurrency_limit:
            raise ValueError(
                f"max_concurrency must be between 1 and {concurrency_limit}"
            )
        analyses = [] if waveform_analyses is None else waveform_analyses
        derived = [] if derived_parameters is None else derived_parameters
        parameter_order, derived_order, combinations, units = _prepare_experiment(
            netlist_template,
            parameters,
            analyses,
            derived,
        )
        if execution_mode == "native":
            _render_native_experiment_netlist(
                netlist_template,
                parameter_order,
                derived_order,
                combinations,
            )
        definition: dict[str, object] = {
            "netlist_template": netlist_template,
            "parameters": parameters,
            "parameter_order": parameter_order,
            "derived_parameters": derived,
            "derived_parameter_order": derived_order,
            "parameter_units": units,
            "waveform_analyses": analyses,
            "filename": normalized_filename,
            "ascii_raw": ascii_raw,
            "timeout_seconds": timeout_seconds,
            "max_concurrency": max_concurrency,
            "reuse_cache": reuse_cache,
            "execution_mode": execution_mode,
        }
        return self._persist_definition(definition, len(combinations))

    def define_explicit(
        self,
        netlist_template: str,
        parameter_order: list[str],
        points: list[dict[str, str]],
        parameter_units: dict[str, str],
        source_point_plan: dict[str, object],
        waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
        filename: str = "circuit.cir",
        ascii_raw: bool = False,
        timeout_seconds: int = 120,
        max_concurrency: int = 2,
        reuse_cache: bool = False,
    ) -> ExperimentJobSnapshot:
        """Persist a frozen explicit-point experiment for durable execution."""
        normalized_filename = _netlist_filename(filename)
        _validate_timeout(timeout_seconds)
        _validate_reuse_cache(reuse_cache)
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise ValueError("max_concurrency must be an integer")
        if max_concurrency < 1 or max_concurrency > self.workers:
            raise ValueError(
                f"max_concurrency must be between 1 and {self.workers}"
            )
        if not isinstance(source_point_plan, dict):
            raise ValueError("source_point_plan must be an object")
        analyses = [] if waveform_analyses is None else waveform_analyses
        parameter_order, derived_order, combinations, units = (
            _prepare_explicit_experiment(
                netlist_template,
                parameter_order,
                points,
                parameter_units,
                analyses,
            )
        )
        definition: dict[str, object] = {
            "netlist_template": netlist_template,
            "parameters": [],
            "parameter_order": parameter_order,
            "derived_parameters": [],
            "derived_parameter_order": derived_order,
            "parameter_units": units,
            "waveform_analyses": analyses,
            "filename": normalized_filename,
            "ascii_raw": ascii_raw,
            "timeout_seconds": timeout_seconds,
            "max_concurrency": max_concurrency,
            "reuse_cache": reuse_cache,
            "execution_mode": "independent",
            "point_plan": {
                "schema_version": 1,
                "points": combinations,
                "source": source_point_plan,
            },
        }
        return self._persist_definition(definition, len(combinations))

    def _persist_definition(
        self, definition: dict[str, object], point_count: int
    ) -> ExperimentJobSnapshot:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        experiment_id = f"mcp-experiment-{stamp}-{uuid.uuid4().hex[:8]}"
        experiment_dir = self.runs_dir / experiment_id
        experiment_dir.mkdir(parents=True)
        created_at = datetime.now().astimezone().isoformat()
        manifest: dict[str, object] = {
            "schema_version": 2,
            "engine_version": EXPERIMENT_ENGINE_VERSION,
            "experiment_id": experiment_id,
            "status": "defined",
            "created_at": created_at,
            "updated_at": created_at,
            "definition_hash": _definition_hash(definition),
            "definition": definition,
            "point_count": point_count,
            "finished_points": 0,
            "pending_points": point_count,
            "running_points": 0,
            "completed_points": 0,
            "error_points": 0,
            "passed_points": 0,
            "failed_points": 0,
            "all_passed": None,
            "cancel_requested": False,
            "artifacts": [],
        }
        self._save_manifest(manifest)
        return self.snapshot(experiment_id)

    def start(self, experiment_id: str) -> ExperimentJobSnapshot:
        with self._lock:
            manifest = self._load_manifest(experiment_id)
            status = str(manifest.get("status"))
            if status == "defined":
                manifest["status"] = "queued"
                self._save_manifest(manifest)
                self._events[experiment_id] = threading.Event()
                self._queue.put(experiment_id)
            elif status not in {
                "queued",
                "running",
                "cancelling",
                "cancelled",
                "completed",
                "failed",
            }:
                raise ValueError(f"cannot start experiment in status {status}")
        return self.snapshot(experiment_id)

    def cancel(self, experiment_id: str) -> ExperimentJobSnapshot:
        with self._lock:
            manifest = self._load_manifest(experiment_id)
            status = str(manifest.get("status"))
            if status in {"defined", "queued"}:
                manifest["status"] = "cancelled"
                manifest["all_passed"] = False
                manifest["cancel_requested"] = True
                self._save_manifest(manifest)
                self._events.setdefault(experiment_id, threading.Event()).set()
            elif status == "running":
                manifest["status"] = "cancelling"
                manifest["cancel_requested"] = True
                self._save_manifest(manifest)
                self._events.setdefault(experiment_id, threading.Event()).set()
        return self.snapshot(experiment_id)

    def resume(self, experiment_id: str) -> ExperimentJobSnapshot:
        """Resume unfinished independent work after cooperative cancellation."""
        with self._lock:
            manifest = self._load_manifest(experiment_id)
            status = str(manifest.get("status"))
            if status in {"defined", "queued", "running", "completed"}:
                return self.start(experiment_id)
            if status == "cancelling":
                raise ValueError("wait for cancellation to finish before resuming")
            if status == "failed":
                raise ValueError("failed experiments cannot be resumed")
            if status != "cancelled":
                raise ValueError(f"cannot resume experiment in status {status}")
            definition = manifest.get("definition")
            if (
                not isinstance(definition, dict)
                or definition.get("execution_mode", "independent") != "independent"
            ):
                raise ValueError("only independent experiments can be resumed")
            experiment_dir = self._experiment_dir(experiment_id)
            for path in experiment_dir.glob("point-*/point_result.json"):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 2 * 1024 * 1024
                ):
                    raise ValueError(f"invalid point checkpoint: {path}")
                try:
                    point = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid point checkpoint: {path}") from exc
                if isinstance(point, dict) and point.get("simulation_status") == "cancelled":
                    path.unlink()
            manifest.update(
                status="queued",
                running_points=0,
                cancel_requested=False,
                all_passed=None,
            )
            manifest.pop("finished_at", None)
            self._save_manifest(manifest)
            self._events[experiment_id] = threading.Event()
            self._queue.put(experiment_id)
        return self.snapshot(experiment_id)

    def snapshot(self, experiment_id: str) -> ExperimentJobSnapshot:
        with self._lock:
            manifest = self._load_manifest(experiment_id)
        experiment_dir = self.runs_dir / experiment_id
        definition = manifest.get("definition")
        execution_mode = (
            str(definition.get("execution_mode", "independent"))
            if isinstance(definition, dict)
            else "independent"
        )
        snapshot: ExperimentJobSnapshot = {
            "experiment_id": experiment_id,
            "experiment_dir": str(experiment_dir),
            "manifest": str(experiment_dir / "experiment_manifest.json"),
            "results_json": str(experiment_dir / "results.json"),
            "results_csv": str(experiment_dir / "results.csv"),
            "status": str(manifest["status"]),
            "point_count": int(manifest["point_count"]),
            "finished_points": int(manifest.get("finished_points", 0)),
            "pending_points": int(manifest.get("pending_points", 0)),
            "running_points": int(manifest.get("running_points", 0)),
            "completed_points": int(manifest.get("completed_points", 0)),
            "error_points": int(manifest.get("error_points", 0)),
            "passed_points": int(manifest.get("passed_points", 0)),
            "failed_points": int(manifest.get("failed_points", 0)),
            "all_passed": manifest.get("all_passed")
            if isinstance(manifest.get("all_passed"), bool)
            else None,
            "error": str(manifest["error"])
            if isinstance(manifest.get("error"), str)
            else None,
            "execution_mode": execution_mode,
        }
        return snapshot

    def definition_hash(self, experiment_id: str) -> str:
        """Return the immutable definition identity under the manager lock."""
        with self._lock:
            manifest = self._load_manifest(experiment_id)
        return str(manifest["definition_hash"])

    def _coordinate(self) -> None:
        while True:
            experiment_id = self._queue.get()
            if experiment_id is None:
                self._queue.task_done()
                return
            try:
                if self._stopping.is_set():
                    continue
                with self._lock:
                    manifest = self._load_manifest(experiment_id)
                    if manifest.get("status") != "queued":
                        continue
                    manifest["status"] = "running"
                    self._save_manifest(manifest)
                self._run_job(experiment_id)
            except Exception as exc:
                try:
                    with self._lock:
                        manifest = self._load_manifest(experiment_id)
                        manifest.update(
                            status="failed",
                            running_points=0,
                            all_passed=False,
                            error=str(exc),
                        )
                        self._save_manifest(manifest)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def _load_checkpoints(
        self, experiment_dir: Path
    ) -> dict[int, ExperimentPointResult]:
        points: dict[int, ExperimentPointResult] = {}
        for path in sorted(experiment_dir.glob("point-*/point_result.json")):
            try:
                point = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(point, dict):
                    raise TypeError("checkpoint must contain an object")
                if not isinstance(point.get("index"), int) or isinstance(
                    point.get("index"), bool
                ):
                    raise TypeError("checkpoint index must be an integer")
                index = int(point["index"])
                directory_index = int(path.parent.name.removeprefix("point-"))
                required = {
                    "parameters",
                    "run_dir",
                    "simulation_status",
                    "duration_seconds",
                    "measurements",
                    "analyses",
                    "all_passed",
                    "error",
                }
                if (
                    index != directory_index
                    or not required <= point.keys()
                    or point.get("simulation_status")
                    not in {"completed", "error", "cancelled"}
                ):
                    raise ValueError("checkpoint shape does not match its directory")
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid point checkpoint: {path}") from exc
            if index in points:
                raise ValueError(f"duplicate point checkpoint index: {index}")
            points[index] = point
        return points

    @staticmethod
    def _validate_native_checkpoint(
        value: object,
        definition_hash: str,
        combinations: list[dict[str, str]],
    ) -> tuple[list[ExperimentPointResult], dict[str, object]]:
        if not isinstance(value, dict):
            raise TypeError("checkpoint must contain an object")
        if value.get("schema_version") != 1:
            raise ValueError("checkpoint schema version is not supported")
        if value.get("definition_hash") != definition_hash:
            raise ValueError("definition hash does not match")
        points = value.get("points")
        native_batch = value.get("native_batch")
        if not isinstance(points, list) or not isinstance(native_batch, dict):
            raise TypeError("checkpoint payload is incomplete")
        if len(points) != len(combinations):
            raise ValueError("checkpoint point count does not match")
        required = {
            "parameters",
            "run_dir",
            "simulation_status",
            "duration_seconds",
            "measurements",
            "analyses",
            "all_passed",
            "error",
            "native_step_index",
        }
        shared_run_dir = native_batch.get("run_dir")
        for index, point in enumerate(points):
            if (
                not isinstance(point, dict)
                or not required <= point.keys()
                or type(point.get("index")) is not int
                or type(point.get("native_step_index")) is not int
                or point.get("index") != index
                or point.get("parameters") != combinations[index]
                or point.get("native_step_index") != index
                or point.get("simulation_status")
                not in {"completed", "error", "cancelled"}
                or not isinstance(point.get("run_dir"), str)
                or point.get("run_dir") != shared_run_dir
                or not isinstance(point.get("measurements"), dict)
                or not isinstance(point.get("analyses"), list)
                or not isinstance(point.get("all_passed"), bool)
                or (
                    point.get("error") is not None
                    and not isinstance(point.get("error"), str)
                )
            ):
                raise ValueError("checkpoint point mapping does not match")
        if native_batch.get("step_count") != len(combinations):
            raise ValueError("checkpoint batch mapping does not match")
        batch_status = native_batch.get("status")
        if batch_status not in {"completed", "error"}:
            raise ValueError("checkpoint batch status is not supported")
        if batch_status == "error" and any(
            point["simulation_status"] != "error" for point in points
        ):
            raise ValueError("checkpoint error batch contains a non-error point")
        if (
            batch_status != "error"
            and native_batch.get("validated_step_order")
            != list(range(len(combinations)))
        ):
            raise ValueError("checkpoint batch mapping does not match")
        return points, native_batch

    def _load_native_checkpoint(
        self,
        experiment_dir: Path,
        definition_hash: str,
        combinations: list[dict[str, str]],
    ) -> tuple[list[ExperimentPointResult], dict[str, object]] | None:
        path = experiment_dir / "native-batch" / "batch_result.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return self._validate_native_checkpoint(
                value, definition_hash, combinations
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid native batch checkpoint: {path}") from exc

    @staticmethod
    def _next_attempt_dir(point_dir: Path) -> Path:
        point_dir.mkdir(parents=True, exist_ok=True)
        attempts = [
            int(path.name.removeprefix("attempt-"))
            for path in point_dir.glob("attempt-[0-9][0-9][0-9][0-9]")
            if path.is_dir()
        ]
        return point_dir / f"attempt-{max(attempts, default=-1) + 1:04d}"

    def _persist_progress(
        self,
        experiment_id: str,
        points: list[ExperimentPointResult],
        point_count: int,
        running_points: int,
        status: str,
    ) -> None:
        counts = _experiment_counts(points, point_count)
        counts["pending_points"] -= running_points
        with self._lock:
            manifest = self._load_manifest(experiment_id)
            current_status = manifest.get("status")
            if current_status in {"cancelling", "cancelled"}:
                status = str(current_status)
                self._events.setdefault(experiment_id, threading.Event()).set()
            manifest.update(status=status, running_points=running_points, **counts)
            self._save_manifest(manifest)

    def _run_job(self, experiment_id: str) -> None:
        experiment_dir = self._experiment_dir(experiment_id)
        event = self._events.setdefault(experiment_id, threading.Event())
        try:
            manifest = self._load_manifest(experiment_id)
            definition = manifest.get("definition")
            if not isinstance(definition, dict):
                raise ValueError("experiment definition is missing")
            if manifest.get("engine_version") != EXPERIMENT_ENGINE_VERSION:
                raise ValueError("experiment engine version is not supported")
            if manifest.get("definition_hash") != _definition_hash(definition):
                raise ValueError("experiment definition hash does not match")
            analyses = definition["waveform_analyses"]
            derived = definition["derived_parameters"]
            point_plan = definition.get("point_plan")
            if point_plan is None:
                parameter_order, derived_order, combinations, units = _prepare_experiment(
                    str(definition["netlist_template"]),
                    definition["parameters"],
                    analyses,
                    derived,
                )
            else:
                if not isinstance(point_plan, dict):
                    raise ValueError("experiment point plan is invalid")
                parameter_order, derived_order, combinations, units = (
                    _prepare_explicit_experiment(
                        str(definition["netlist_template"]),
                        definition["parameter_order"],
                        point_plan.get("points"),
                        definition["parameter_units"],
                        analyses,
                    )
                )
            execution_mode = str(definition.get("execution_mode", "independent"))
            if execution_mode == "native":
                self._run_native_job(
                    experiment_id,
                    experiment_dir,
                    manifest,
                    definition,
                    parameter_order,
                    derived_order,
                    combinations,
                    units,
                    analyses,
                    event,
                )
                return
            if execution_mode != "independent":
                raise ValueError("experiment execution mode is not supported")
            checkpoints = self._load_checkpoints(experiment_dir)
            if any(index < 0 or index >= len(combinations) for index in checkpoints):
                raise ValueError("point checkpoint index is outside the experiment")
            if any(
                point.get("parameters") != combinations[index]
                for index, point in checkpoints.items()
            ):
                raise ValueError("point checkpoint parameters do not match the experiment")
            self._persist_progress(
                experiment_id,
                list(checkpoints.values()),
                len(combinations),
                0,
                "running",
            )
            pending = [index for index in range(len(combinations)) if index not in checkpoints]
            active: dict[Future[ExperimentPointResult], tuple[int, Path]] = {}
            next_pending = 0
            concurrency = int(definition["max_concurrency"])
            while next_pending < len(pending) or active:
                while (
                    not event.is_set()
                    and not self._stopping.is_set()
                    and len(active) < concurrency
                    and next_pending < len(pending)
                ):
                    index = pending[next_pending]
                    next_pending += 1
                    point_root = experiment_dir / f"point-{index:04d}"
                    attempt_dir = self._next_attempt_dir(point_root)
                    arguments = (
                        index,
                        combinations[index],
                        attempt_dir,
                        str(definition["netlist_template"]),
                        str(definition["filename"]),
                        bool(definition["ascii_raw"]),
                        int(definition["timeout_seconds"]),
                        analyses,
                        event,
                    )
                    if bool(definition.get("reuse_cache", False)):
                        arguments = (*arguments, True)
                    future = self._executor.submit(self._execute_point, *arguments)
                    active[future] = (index, point_root)
                self._persist_progress(
                    experiment_id,
                    list(checkpoints.values()),
                    len(combinations),
                    len(active),
                    "running",
                )
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    index, point_root = active.pop(future)
                    try:
                        point = future.result()
                    except Exception as exc:
                        point = {
                            "index": index,
                            "parameters": combinations[index],
                            "run_dir": str(point_root),
                            "simulation_status": "error",
                            "duration_seconds": None,
                            "measurements": {},
                            "analyses": [],
                            "all_passed": False,
                            "error": str(exc),
                        }
                    _write_json(point_root / "point_result.json", point)
                    checkpoints[index] = point

            points = [checkpoints[index] for index in sorted(checkpoints)]
            counts = _experiment_counts(points, len(combinations))
            if self._stopping.is_set() and not event.is_set():
                self._persist_progress(
                    experiment_id,
                    points,
                    len(combinations),
                    0,
                    "queued",
                )
                return
            with self._lock:
                manifest = self._load_manifest(experiment_id)
                cancelled = event.is_set() or manifest.get("status") in {
                    "cancelling",
                    "cancelled",
                }
                final_status = "cancelled" if cancelled else "completed"
                all_passed = (
                    final_status == "completed"
                    and len(points) == len(combinations)
                    and counts["passed_points"] == len(combinations)
                )
                result: ExperimentResult = {
                    "experiment_id": experiment_id,
                    "experiment_dir": str(experiment_dir),
                    "manifest": str(experiment_dir / "experiment_manifest.json"),
                    "results_json": str(experiment_dir / "results.json"),
                    "results_csv": str(experiment_dir / "results.csv"),
                    "status": final_status,
                    "parameter_order": parameter_order,
                    "derived_parameter_order": derived_order,
                    "parameter_units": units,
                    "point_count": len(combinations),
                    "completed_points": counts["completed_points"],
                    "error_points": counts["error_points"],
                    "passed_points": counts["passed_points"],
                    "failed_points": counts["failed_points"],
                    "all_passed": all_passed,
                    "points": points,
                    "execution_mode": "independent",
                    "native_batch": None,
                }
                _write_experiment_csv(
                    experiment_dir / "results.csv",
                    [*parameter_order, *derived_order],
                    points,
                )
                _write_json(experiment_dir / "results.json", result)
                manifest.update(
                    status=final_status,
                    running_points=0,
                    all_passed=all_passed,
                    finished_at=datetime.now().astimezone().isoformat(),
                    artifacts=["results.json", "results.csv"],
                    **counts,
                )
                self._save_manifest(manifest)
        except Exception as exc:
            try:
                with self._lock:
                    manifest = self._load_manifest(experiment_id)
                    manifest.update(
                        status="failed",
                        running_points=0,
                        all_passed=False,
                        error=str(exc),
                        finished_at=datetime.now().astimezone().isoformat(),
                    )
                    self._save_manifest(manifest)
            except Exception:
                pass

    def _run_native_job(
        self,
        experiment_id: str,
        experiment_dir: Path,
        manifest: dict[str, object],
        definition: dict[str, object],
        parameter_order: list[str],
        derived_order: list[str],
        combinations: list[dict[str, str]],
        units: dict[str, str],
        analyses: list[ExperimentWaveformAnalysis],
        event: threading.Event,
    ) -> None:
        definition_hash = str(manifest["definition_hash"])
        checkpoint = self._load_native_checkpoint(
            experiment_dir, definition_hash, combinations
        )
        if checkpoint is None:
            if event.is_set():
                points: list[ExperimentPointResult] = []
                native_batch: dict[str, object] | None = None
            else:
                if self._execute_native is None:
                    raise ValueError("native execution is unavailable")
                native_root = experiment_dir / "native-batch"
                netlist = _render_native_experiment_netlist(
                    str(definition["netlist_template"]),
                    parameter_order,
                    derived_order,
                    combinations,
                )
                self._persist_progress(
                    experiment_id,
                    [],
                    len(combinations),
                    len(combinations),
                    "running",
                )
                if event.is_set():
                    points = []
                    native_batch = None
                else:
                    attempt_dir = self._next_attempt_dir(native_root)
                    points, native_batch = self._execute_native(
                        combinations,
                        attempt_dir,
                        netlist,
                        str(definition["filename"]),
                        bool(definition["ascii_raw"]),
                        int(definition["timeout_seconds"]),
                        analyses,
                        bool(definition.get("reuse_cache", False)),
                        event,
                    )
                    checkpoint_value = {
                        "schema_version": 1,
                        "definition_hash": definition_hash,
                        "points": points,
                        "native_batch": native_batch,
                    }
                    points, native_batch = self._validate_native_checkpoint(
                        checkpoint_value, definition_hash, combinations
                    )
                    _write_json(native_root / "batch_result.json", checkpoint_value)
        else:
            points, native_batch = checkpoint

        for point in points:
            point_root = experiment_dir / f"point-{point['index']:04d}"
            point_root.mkdir(parents=True, exist_ok=True)
            _write_json(point_root / "point_result.json", point)

        if self._stopping.is_set() and not event.is_set():
            self._persist_progress(
                experiment_id,
                points,
                len(combinations),
                0,
                "queued",
            )
            return

        counts = _experiment_counts(points, len(combinations))
        with self._lock:
            manifest = self._load_manifest(experiment_id)
            cancelled = event.is_set() or manifest.get("status") in {
                "cancelling",
                "cancelled",
            }
            final_status = "cancelled" if cancelled else "completed"
            all_passed = (
                final_status == "completed"
                and len(points) == len(combinations)
                and counts["passed_points"] == len(combinations)
            )
            result: ExperimentResult = {
                "experiment_id": experiment_id,
                "experiment_dir": str(experiment_dir),
                "manifest": str(experiment_dir / "experiment_manifest.json"),
                "results_json": str(experiment_dir / "results.json"),
                "results_csv": str(experiment_dir / "results.csv"),
                "status": final_status,
                "parameter_order": parameter_order,
                "derived_parameter_order": derived_order,
                "parameter_units": units,
                "point_count": len(combinations),
                "completed_points": counts["completed_points"],
                "error_points": counts["error_points"],
                "passed_points": counts["passed_points"],
                "failed_points": counts["failed_points"],
                "all_passed": all_passed,
                "points": points,
                "execution_mode": "native",
                "native_batch": native_batch,
            }
            _write_experiment_csv(
                experiment_dir / "results.csv",
                [*parameter_order, *derived_order],
                points,
            )
            _write_json(experiment_dir / "results.json", result)
            artifacts = ["results.json", "results.csv"]
            if native_batch is not None:
                artifacts.append("native-batch/batch_result.json")
            manifest.update(
                status=final_status,
                running_points=0,
                all_passed=all_passed,
                finished_at=datetime.now().astimezone().isoformat(),
                artifacts=artifacts,
                native_batch=native_batch,
                **counts,
            )
            self._save_manifest(manifest)

    def wait(self, experiment_id: str, timeout: float = 10.0) -> ExperimentJobSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot(experiment_id)
            if snapshot["status"] in {"completed", "cancelled", "failed"}:
                return snapshot
            time.sleep(0.01)
        raise TimeoutError(f"experiment did not finish within {timeout} seconds")

    def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._queue.put(None)
        self._coordinator.join()
        self._executor.shutdown(wait=True)
        self._process_lock.release()
