"""Global rank sensitivity for terminal statistical experiments."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
import uuid
from pathlib import Path
from typing import TypedDict

import experiment_index
import statistical_engine
import statistical_results
import worst_case_analysis

SENSITIVITY_SCHEMA_VERSION = 1
MINIMUM_SAMPLES = 5
MEANINGFUL_ABS_RHO = 0.5


class SensitivityAnalysisResult(TypedDict):
    experiment_id: str
    sensitivity_json: str
    sensitivity_csv: str
    requirement_count: int
    variable_count: int
    scope_count: int
    evaluated_pairs: int
    invalid_points: int


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        average = ((start + 1) + end) / 2
        for position in range(start, end):
            ranks[ordered[position]] = average
        start = end
    return ranks


def _spearman(inputs: list[float], responses: list[float]) -> float:
    input_ranks = _average_ranks(inputs)
    response_ranks = _average_ranks(responses)
    input_mean = statistics.fmean(input_ranks)
    response_mean = statistics.fmean(response_ranks)
    numerator = sum(
        (input_rank - input_mean) * (response_rank - response_mean)
        for input_rank, response_rank in zip(input_ranks, response_ranks)
    )
    input_square_sum = sum((rank - input_mean) ** 2 for rank in input_ranks)
    response_square_sum = sum((rank - response_mean) ** 2 for rank in response_ranks)
    correlation = numerator / math.sqrt(input_square_sum * response_square_sum)
    if not math.isfinite(correlation):
        raise ValueError("rank correlation must be finite")
    return max(-1.0, min(1.0, correlation))


def _strength(rho: float) -> str:
    magnitude = abs(rho)
    if magnitude >= 0.9:
        return "very_strong"
    if magnitude >= 0.7:
        return "strong"
    if magnitude >= MEANINGFUL_ABS_RHO:
        return "moderate"
    if magnitude >= 0.3:
        return "weak"
    return "negligible"


def _scope_key(corners: dict[str, str]) -> str:
    return json.dumps(corners, separators=(",", ":"), ensure_ascii=False)


def _correlated_inputs(definition: dict[str, object]) -> dict[str, list[str]]:
    related: dict[str, set[str]] = {}
    for group in definition.get("correlations", []):
        if not isinstance(group, dict) or not isinstance(group.get("variables"), list):
            continue
        names = [str(name) for name in group["variables"]]
        for name in names:
            related.setdefault(name, set()).update(
                other for other in names if other != name
            )
    return {name: sorted(names) for name, names in related.items()}


def _requirement_observations(
    results: dict[str, object],
) -> tuple[dict[str, dict[int, float]], dict[str, dict[str, object]]]:
    validated = worst_case_analysis.build_worst_case_analysis(results)
    requirements = validated["requirements"]
    assert isinstance(requirements, list)
    requirement_metadata = {
        str(requirement["check_id"]): {
            name: requirement[name]
            for name in (
                "check_id",
                "analysis",
                "requirement_index",
                "metric",
                "operator",
                "target",
                "unit",
                "parameters",
            )
        }
        for requirement in requirements
    }
    observations: dict[str, dict[int, float]] = {
        check_id: {} for check_id in requirement_metadata
    }
    points = results["points"]
    assert isinstance(points, list)
    for point in points:
        if statistical_results._classification(point) not in {
            "electrical_pass",
            "electrical_failure",
        }:
            continue
        point_index = int(point["index"])
        for analysis_entry in point.get("analyses", []):
            if not isinstance(analysis_entry, dict):
                continue
            analysis = analysis_entry.get("analysis")
            if not isinstance(analysis, dict):
                continue
            analysis_name = str(analysis_entry["name"])
            for requirement in analysis.get("results", []):
                threshold = requirement["threshold"]
                check_id = worst_case_analysis._identity(
                    analysis_name,
                    str(requirement["metric"]),
                    str(threshold["operator"]),
                    float(threshold["target"]),
                    str(requirement["unit"]),
                    dict(requirement.get("parameters", {})),
                )
                observations[check_id][point_index] = worst_case_analysis._margin(
                    str(threshold["operator"]),
                    float(threshold["target"]),
                    float(requirement["value"]),
                )
    return observations, requirement_metadata


def _variable_result(
    variable: dict[str, object],
    unit: str,
    correlated_with: list[str],
    raw_inputs: list[str],
    responses: list[float],
) -> dict[str, object]:
    common: dict[str, object] = {
        "variable": str(variable["name"]),
        "distribution": str(variable["distribution"]),
        "unit": unit,
        "correlated_with": correlated_with,
        "sample_count": len(responses),
        "distinct_response_count": len(set(responses)),
        "rho": None,
        "absolute_rho": None,
        "rank": None,
        "direction": None,
        "strength": None,
        "meaningfully_monotonic": False,
    }
    try:
        inputs = [float(value) for value in raw_inputs]
    except ValueError:
        return {**common, "distinct_input_count": None, "status": "non_numeric_input"}
    if any(not math.isfinite(value) for value in inputs):
        raise ValueError(f"variable {variable['name']} contains a non-finite value")
    common["distinct_input_count"] = len(set(inputs))
    if len(inputs) < MINIMUM_SAMPLES:
        return {**common, "status": "insufficient_samples"}
    if len(set(inputs)) < 2:
        return {**common, "status": "constant_input"}
    if len(set(responses)) < 2:
        return {**common, "status": "constant_response"}
    rho = _spearman(inputs, responses)
    return {
        **common,
        "status": "ok",
        "rho": rho,
        "absolute_rho": abs(rho),
        "direction": "positive" if rho > 0 else "negative" if rho < 0 else "none",
        "strength": _strength(rho),
        "meaningfully_monotonic": abs(rho) >= MEANINGFUL_ABS_RHO,
    }


def build_sensitivity_analysis(
    results: dict[str, object],
    plan: statistical_engine.StatisticalPlan,
    *,
    point_metadata: list[dict[str, object]] | None = None,
    sampling_provenance: statistical_results.SamplingProvenance | None = None,
) -> dict[str, object]:
    """Calculate descriptive Spearman sensitivity without pooling corners."""
    planned_points = int(results["point_count"])
    metadata = statistical_results._point_metadata(point_metadata, planned_points)
    plan_points = plan["points"]
    if len(plan_points) != planned_points:
        raise ValueError("statistical plan point count does not match results")
    points = results["points"]
    assert isinstance(points, list)
    for point in points:
        point_index = int(point["index"])
        if dict(point["parameters"]) != dict(plan_points[point_index]["parameters"]):
            raise ValueError("result parameters do not match the statistical plan")
    if metadata is not None:
        for index, entry in enumerate(metadata):
            plan_point = plan_points[index]
            if (
                plan_point.get("sample_index") != entry["sample_index"]
                or plan_point.get("corners") != entry["corners"]
            ):
                raise ValueError(
                    "corner attribution does not match the statistical plan"
                )
    elif any("corners" in point or "sample_index" in point for point in plan_points):
        raise ValueError("corner statistical plan is missing point metadata")
    definition = plan["definition"]
    variables = definition.get("variables")
    if not isinstance(variables, list) or not variables:
        raise ValueError("statistical plan variables are missing")
    parameter_units = plan["parameter_units"]
    correlated = _correlated_inputs(definition)
    observations, requirement_metadata = _requirement_observations(results)

    scope_corners = (
        sorted(
            {
                _scope_key(dict(entry["corners"])): dict(entry["corners"])
                for entry in metadata
            }.values(),
            key=_scope_key,
        )
        if metadata is not None
        else [{}]
    )
    point_scopes = {
        index: _scope_key({} if metadata is None else dict(metadata[index]["corners"]))
        for index in range(planned_points)
    }
    scope_indexes = {
        _scope_key(corners): [
            index
            for index in range(planned_points)
            if point_scopes[index] == _scope_key(corners)
        ]
        for corners in scope_corners
    }

    requirements: list[dict[str, object]] = []
    evaluated_pairs = 0
    ordered_metadata = sorted(
        requirement_metadata.values(),
        key=lambda requirement: (
            requirement["analysis"],
            requirement["requirement_index"],
            requirement["check_id"],
        ),
    )
    for requirement in ordered_metadata:
        check_id = str(requirement["check_id"])
        requirement_scopes: list[dict[str, object]] = []
        for corners in scope_corners:
            indexes = [
                index
                for index in scope_indexes[_scope_key(corners)]
                if index in observations[check_id]
            ]
            responses = [observations[check_id][index] for index in indexes]
            variable_results: list[dict[str, object]] = []
            for variable in variables:
                assert isinstance(variable, dict)
                name = str(variable["name"])
                raw_inputs = [
                    str(plan_points[index]["parameters"][name])
                    for index in indexes
                ]
                variable_results.append(
                    _variable_result(
                        variable,
                        str(parameter_units.get(name, "")),
                        correlated.get(name, []),
                        raw_inputs,
                        responses,
                    )
                )
                evaluated_pairs += len(responses)
            ranked = sorted(
                (
                    result
                    for result in variable_results
                    if result["status"] == "ok"
                ),
                key=lambda result: (
                    -float(result["absolute_rho"]),
                    str(result["variable"]),
                ),
            )
            rank = 0
            previous: float | None = None
            for result in ranked:
                magnitude = float(result["absolute_rho"])
                if previous is None or magnitude != previous:
                    rank += 1
                    previous = magnitude
                result["rank"] = rank
            variable_results.sort(
                key=lambda result: (
                    result["rank"] is None,
                    0 if result["rank"] is None else result["rank"],
                    str(result["variable"]),
                )
            )
            requirement_scopes.append(
                {
                    "corners": corners,
                    "evaluated_samples": len(indexes),
                    "variables": variable_results,
                }
            )
        requirements.append({**requirement, "scopes": requirement_scopes})

    invalid_points = sum(
        statistical_results._classification(point)
        not in {"electrical_pass", "electrical_failure"}
        for point in points
    ) + planned_points - len(points)
    analysis: dict[str, object] = {
        "schema_version": SENSITIVITY_SCHEMA_VERSION,
        "experiment_id": results["experiment_id"],
        "method": "spearman_rank_correlation_average_ties",
        "response": "signed_requirement_margin",
        "minimum_samples": MINIMUM_SAMPLES,
        "meaningful_absolute_rho": MEANINGFUL_ABS_RHO,
        "interpretation": "descriptive_association_not_independent_causality",
        "invalid_points": invalid_points,
        "requirement_count": len(requirements),
        "variable_count": len(variables),
        "scope_count": len(scope_corners),
        "evaluated_pairs": evaluated_pairs,
        "requirements": requirements,
    }
    if sampling_provenance is not None:
        analysis["sampling_provenance"] = dict(sampling_provenance)
    return analysis


def _csv_document(analysis: dict[str, object]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "check_id", "analysis", "requirement_index", "metric", "operator",
        "target", "requirement_unit", "requirement_parameters", "corners",
        "evaluated_samples", "variable", "distribution", "variable_unit",
        "correlated_with", "sample_count", "distinct_input_count",
        "distinct_response_count", "rho", "absolute_rho", "rank", "direction",
        "strength", "meaningfully_monotonic", "status",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for requirement in analysis["requirements"]:
        common = {
            "check_id": requirement["check_id"],
            "analysis": requirement["analysis"],
            "requirement_index": requirement["requirement_index"],
            "metric": requirement["metric"],
            "operator": requirement["operator"],
            "target": requirement["target"],
            "requirement_unit": requirement["unit"],
            "requirement_parameters": json.dumps(
                requirement["parameters"], sort_keys=True, separators=(",", ":")
            ),
        }
        for scope in requirement["scopes"]:
            for variable in scope["variables"]:
                writer.writerow(
                    {
                        **common,
                        "corners": json.dumps(
                            scope["corners"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "evaluated_samples": scope["evaluated_samples"],
                        **{
                            field: variable.get(field)
                            for field in (
                                "variable", "distribution", "sample_count",
                                "distinct_input_count", "distinct_response_count",
                                "rho", "absolute_rho", "rank", "direction",
                                "strength", "meaningfully_monotonic", "status",
                            )
                        },
                        "variable_unit": variable["unit"],
                        "correlated_with": json.dumps(
                            variable["correlated_with"], separators=(",", ":")
                        ),
                    }
                )
    return output.getvalue()


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"sensitivity artifact must not be a symlink: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def analyze_statistical_sensitivity(
    runs_dir: Path, experiment_id: str
) -> SensitivityAnalysisResult:
    experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
        runs_dir, experiment_id
    )
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "statistical":
        raise ValueError(f"Experiment {experiment_id} is not a statistical study")
    provenance = statistical_results._sampling_provenance(source)
    plan = statistical_engine.load_statistical_plan(runs_dir, provenance["plan_id"])
    plan_path = runs_dir / provenance["runs_relative_path"]
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != provenance["plan_sha256"]:
        raise ValueError("statistical plan does not match experiment provenance")
    if (
        plan["generator_version"] != provenance["generator_version"]
        or plan["definition_hash"] != provenance["definition_hash"]
        or str(plan["definition"].get("sampling_method", "independent"))
        != provenance["sampling_method"]
    ):
        raise ValueError(
            "statistical plan metadata does not match experiment provenance"
        )
    point_metadata = source.get("point_metadata")
    corner_axes = source.get("corner_axes")
    if corner_axes and not isinstance(point_metadata, list):
        raise ValueError("corner statistical study is missing point_metadata")
    analysis = build_sensitivity_analysis(
        results,
        plan,
        point_metadata=point_metadata,
        sampling_provenance=provenance,
    )
    json_path = experiment_dir / "sensitivity.json"
    csv_path = experiment_dir / "sensitivity.csv"
    _write_atomic(
        json_path,
        json.dumps(analysis, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_atomic(csv_path, _csv_document(analysis))
    return {
        "experiment_id": experiment_id,
        "sensitivity_json": str(json_path),
        "sensitivity_csv": str(csv_path),
        "requirement_count": int(analysis["requirement_count"]),
        "variable_count": int(analysis["variable_count"]),
        "scope_count": int(analysis["scope_count"]),
        "evaluated_pairs": int(analysis["evaluated_pairs"]),
        "invalid_points": int(analysis["invalid_points"]),
    }
