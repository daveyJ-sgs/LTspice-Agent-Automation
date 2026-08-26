"""Yield and descriptive statistics for terminal statistical experiments."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import uuid
from pathlib import Path
from typing import TypedDict

import experiment_index
import statistical_engine

STATISTICS_SCHEMA_VERSION = 2
CONFIDENCE_LEVEL = 0.95
_Z_95 = 1.959963984540054
MAX_ANALYSIS_ROWS = 2_000


class SamplingProvenance(TypedDict):
    sampling_method: str
    generator_version: str
    plan_id: str
    plan_sha256: str
    definition_hash: str
    runs_relative_path: str


class StatisticalSummaryResult(TypedDict):
    experiment_id: str
    statistics_json: str
    statistics_csv: str
    planned_points: int
    evaluated_points: int
    passed_points: int
    electrical_failures: int
    invalid_points: int
    observed_yield: float | None
    confidence_low: float | None
    confidence_high: float | None
    corner_aggregate: str | None
    corner_results: list[dict[str, object]]
    sampling_provenance: SamplingProvenance


def _sampling_provenance(source: dict[str, object]) -> SamplingProvenance:
    sampling_method = source.get("sampling_method", "independent")
    if sampling_method not in {"independent", "latin_hypercube", "halton"}:
        raise ValueError("statistical sampling_method is invalid")
    fields = {
        name: source.get(name)
        for name in (
            "generator_version",
            "plan_id",
            "plan_sha256",
            "definition_hash",
            "runs_relative_path",
        )
    }
    if any(not isinstance(value, str) for value in fields.values()):
        raise ValueError("statistical sampling provenance is incomplete")
    generator_version = fields["generator_version"]
    plan_id = fields["plan_id"]
    plan_sha256 = fields["plan_sha256"]
    definition_hash = fields["definition_hash"]
    runs_relative_path = fields["runs_relative_path"]
    assert all(
        isinstance(value, str)
        for value in (
            generator_version,
            plan_id,
            plan_sha256,
            definition_hash,
            runs_relative_path,
        )
    )
    if re.fullmatch(r"[a-z0-9-]{1,128}", generator_version) is None:
        raise ValueError("statistical generator_version is invalid")
    if re.fullmatch(r"statistical-plan-[0-9a-f]{16}", plan_id) is None:
        raise ValueError("statistical plan_id is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", plan_sha256) is None:
        raise ValueError("statistical plan_sha256 is invalid")
    if plan_id != f"statistical-plan-{plan_sha256[:16]}":
        raise ValueError("statistical plan_id does not match plan_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", definition_hash) is None:
        raise ValueError("statistical definition_hash is invalid")
    expected_path = f"statistical-plans/{plan_id}/statistical_plan.json"
    if runs_relative_path != expected_path:
        raise ValueError("statistical plan artifact path is invalid")
    return {
        "sampling_method": sampling_method,
        "generator_version": generator_version,
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "definition_hash": definition_hash,
        "runs_relative_path": runs_relative_path,
    }


def _verified_sampling_plan(
    runs_dir: Path, source: dict[str, object]
) -> tuple[SamplingProvenance, statistical_engine.StatisticalPlan]:
    provenance = _sampling_provenance(source)
    plan = statistical_engine.load_statistical_plan(runs_dir, provenance["plan_id"])
    plan_path = runs_dir / provenance["runs_relative_path"]
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != provenance["plan_sha256"]:
        raise ValueError("statistical plan does not match experiment provenance")
    definition = plan["definition"]
    if (
        plan["generator_version"] != provenance["generator_version"]
        or plan["definition_hash"] != provenance["definition_hash"]
        or str(definition.get("sampling_method", "independent"))
        != provenance["sampling_method"]
    ):
        raise ValueError("statistical plan metadata does not match experiment provenance")
    return provenance, plan


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _descriptive(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": statistics.fmean(values),
        "standard_deviation": statistics.stdev(values) if len(values) > 1 else None,
        "p05": _percentile(values, 0.05),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _wilson(successes: int, total: int) -> tuple[float | None, float | None]:
    if total == 0:
        return None, None
    proportion = successes / total
    denominator = 1 + _Z_95**2 / total
    center = (proportion + _Z_95**2 / (2 * total)) / denominator
    radius = (
        _Z_95
        * math.sqrt(
            proportion * (1 - proportion) / total
            + _Z_95**2 / (4 * total**2)
        )
        / denominator
    )
    return center - radius, center + radius


def _classification(point: dict[str, object]) -> str:
    simulation_status = point.get("simulation_status")
    if simulation_status == "cancelled":
        return "cancelled"
    if simulation_status != "completed":
        return "simulation_error"
    analyses = point.get("analyses", [])
    if any(
        isinstance(analysis, dict) and analysis.get("status") == "error"
        for analysis in analyses
    ):
        return "analysis_error"
    return "electrical_pass" if point.get("all_passed") is True else "electrical_failure"


def _point_metadata(
    point_metadata: list[dict[str, object]] | None,
    point_count: int,
) -> list[dict[str, object]] | None:
    if point_metadata is None:
        return None
    if not isinstance(point_metadata, list) or len(point_metadata) != point_count:
        raise ValueError("corner point_metadata must match the planned point count")
    normalized: list[dict[str, object]] = []
    corner_order: tuple[str, ...] | None = None
    for index, entry in enumerate(point_metadata):
        if not isinstance(entry, dict) or set(entry) != {
            "index",
            "sample_index",
            "corners",
        }:
            raise ValueError("corner point_metadata entries are invalid")
        sample_index = entry.get("sample_index")
        corners = entry.get("corners")
        if entry.get("index") != index:
            raise ValueError("corner point_metadata indexes must be contiguous")
        if (
            not isinstance(sample_index, int)
            or isinstance(sample_index, bool)
            or sample_index < 0
        ):
            raise ValueError("corner point_metadata sample indexes are invalid")
        if (
            not isinstance(corners, dict)
            or not corners
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or not value
                for name, value in corners.items()
            )
        ):
            raise ValueError("corner point_metadata corners are invalid")
        names = tuple(corners)
        if corner_order is None:
            corner_order = names
        elif names != corner_order:
            raise ValueError("corner point_metadata axes must use one stable order")
        normalized.append(
            {
                "index": index,
                "sample_index": sample_index,
                "corners": dict(corners),
            }
        )
    return normalized


def build_statistics(
    results: dict[str, object],
    *,
    point_metadata: list[dict[str, object]] | None = None,
    corner_aggregate: bool = False,
    sampling_provenance: SamplingProvenance | None = None,
) -> dict[str, object]:
    """Build deterministic statistics from already validated result data."""
    points = results["points"]
    assert isinstance(points, list)
    planned_points = int(results["point_count"])
    metadata = _point_metadata(point_metadata, planned_points)
    if not isinstance(corner_aggregate, bool):
        raise ValueError("corner_aggregate must be a boolean")
    if metadata is None and corner_aggregate:
        raise ValueError("corner_aggregate requires corner point metadata")
    classifications = {
        "electrical_pass": 0,
        "electrical_failure": 0,
        "simulation_error": 0,
        "analysis_error": 0,
        "cancelled": 0,
        "unfinished": 0,
    }
    measurements: dict[str, dict[str, list[object]]] = {}
    margins: dict[str, dict[str, object]] = {}
    failed_samples: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    corner_groups: dict[str, dict[str, object]] = {}
    if metadata is not None:
        for entry in metadata:
            corners = entry["corners"]
            identity = json.dumps(corners, separators=(",", ":"), ensure_ascii=False)
            group = corner_groups.setdefault(
                identity,
                {
                    "corners": corners,
                    "planned_points": 0,
                    "classifications": {
                        "electrical_pass": 0,
                        "electrical_failure": 0,
                        "simulation_error": 0,
                        "analysis_error": 0,
                        "cancelled": 0,
                        "unfinished": 0,
                    },
                },
            )
            group["planned_points"] += 1
            group["classifications"]["unfinished"] += 1
    for point in sorted(points, key=lambda item: item["index"]):
        classification = _classification(point)
        classifications[classification] += 1
        point_meta = None if metadata is None else metadata[int(point["index"])]
        if point_meta is not None:
            identity = json.dumps(
                point_meta["corners"], separators=(",", ":"), ensure_ascii=False
            )
            corner_classifications = corner_groups[identity]["classifications"]
            corner_classifications["unfinished"] -= 1
            corner_classifications[classification] += 1
        failed_requirements: list[dict[str, object]] = []
        for analysis_entry in point.get("analyses", []):
            if not isinstance(analysis_entry, dict):
                continue
            analysis = analysis_entry.get("analysis")
            if not isinstance(analysis, dict):
                continue
            for requirement in analysis.get("results", []):
                if requirement["passed"] is not True:
                    failed_requirements.append(
                        {
                            "analysis": analysis_entry["name"],
                            "metric": requirement["metric"],
                            "value": requirement["value"],
                            "unit": requirement["unit"],
                            "threshold": requirement["threshold"],
                            "parameters": requirement.get("parameters", {}),
                        }
                    )
        sample = {
            "index": point["index"],
            "classification": classification,
            "parameters": point["parameters"],
            "run_dir": point["run_dir"],
            "evidence_path": f"point-{int(point['index']):04d}/",
            "error": point.get("error"),
            "failed_requirements": failed_requirements,
        }
        if point_meta is not None:
            sample["sample_index"] = point_meta["sample_index"]
            sample["corners"] = point_meta["corners"]
        samples.append(sample)
        if classification == "electrical_failure":
            failed_samples.append(sample)
        if classification not in {"simulation_error", "analysis_error", "cancelled"}:
            for name, value in point.get("measurements", {}).items():
                group = measurements.setdefault(
                    name, {"values": [], "point_indexes": []}
                )
                group["values"].append(float(value))
                group["point_indexes"].append(point["index"])
        else:
            continue
        for analysis_entry in point.get("analyses", []):
            if not isinstance(analysis_entry, dict):
                continue
            analysis = analysis_entry.get("analysis")
            if not isinstance(analysis, dict):
                continue
            for requirement_index, requirement in enumerate(analysis.get("results", [])):
                threshold = requirement["threshold"]
                operator = threshold["operator"]
                target = float(threshold["target"])
                value = float(requirement["value"])
                margin = target - value if operator in {"<", "<="} else value - target
                identity = json.dumps(
                    [
                        analysis_entry["name"],
                        requirement_index,
                        requirement["metric"],
                        operator,
                        target,
                        requirement["unit"],
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                group = margins.setdefault(
                    identity,
                    {
                        "analysis": analysis_entry["name"],
                        "requirement_index": requirement_index,
                        "metric": requirement["metric"],
                        "operator": operator,
                        "target": target,
                        "unit": requirement["unit"],
                        "values": [],
                        "point_indexes": [],
                    },
                )
                group["values"].append(margin)
                group["point_indexes"].append(point["index"])

    classifications["unfinished"] = planned_points - len(points)
    passed = classifications["electrical_pass"]
    failed = classifications["electrical_failure"]
    evaluated = passed + failed
    invalid = (
        classifications["simulation_error"]
        + classifications["analysis_error"]
        + classifications["cancelled"]
        + classifications["unfinished"]
    )
    low, high = _wilson(passed, evaluated)
    pooled = metadata is None or corner_aggregate
    result: dict[str, object] = {
        "schema_version": STATISTICS_SCHEMA_VERSION,
        "experiment_id": results["experiment_id"],
        "confidence_level": CONFIDENCE_LEVEL,
        "planned_points": planned_points,
        "finished_points": len(points),
        "classifications": classifications,
        "evaluated_points": evaluated,
        "invalid_points": invalid,
        "observed_yield": (
            None if not pooled or evaluated == 0 else passed / evaluated
        ),
        "planned_pass_fraction": (
            None if not pooled else passed / planned_points
        ),
        "yield_confidence_interval": {
            "method": "wilson",
            "low": low if pooled else None,
            "high": high if pooled else None,
        },
        "measurements": {
            name: {
                **_descriptive(group["values"]),
                "point_indexes": group["point_indexes"],
            }
            for name, group in sorted(measurements.items())
        },
        "requirement_margins": [
            {
                **{
                    name: value
                    for name, value in group.items()
                    if name not in {"values", "point_indexes"}
                },
                "statistics": _descriptive(group["values"]),
                "point_indexes": group["point_indexes"],
            }
            for _, group in sorted(margins.items())
        ],
        "failed_samples": failed_samples,
        "samples": samples,
    }
    if sampling_provenance is not None:
        result["sampling_provenance"] = dict(sampling_provenance)
    if metadata is not None:
        corner_results: list[dict[str, object]] = []
        for group in corner_groups.values():
            group_classifications = group["classifications"]
            group_passed = group_classifications["electrical_pass"]
            group_failed = group_classifications["electrical_failure"]
            group_evaluated = group_passed + group_failed
            group_invalid = sum(
                group_classifications[name]
                for name in (
                    "simulation_error",
                    "analysis_error",
                    "cancelled",
                    "unfinished",
                )
            )
            group_low, group_high = _wilson(group_passed, group_evaluated)
            corner_results.append(
                {
                    "corners": group["corners"],
                    "planned_points": group["planned_points"],
                    "evaluated_points": group_evaluated,
                    "invalid_points": group_invalid,
                    "classifications": group_classifications,
                    "observed_yield": (
                        None
                        if group_evaluated == 0
                        else group_passed / group_evaluated
                    ),
                    "yield_confidence_interval": {
                        "method": "wilson",
                        "low": group_low,
                        "high": group_high,
                    },
                }
            )
        result["corner_aggregate"] = "pooled" if corner_aggregate else None
        result["corner_results"] = corner_results
    return result


def _csv_document(summary: dict[str, object]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "record_type",
        "name",
        "value",
        "count",
        "minimum",
        "maximum",
        "mean",
        "standard_deviation",
        "p05",
        "p50",
        "p95",
        "unit",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    provenance = summary.get("sampling_provenance")
    if isinstance(provenance, dict):
        for name, value in provenance.items():
            writer.writerow(
                {"record_type": "provenance", "name": name, "value": value}
            )
    interval = summary["yield_confidence_interval"]
    for name, value in (
        ("planned_points", summary["planned_points"]),
        ("evaluated_points", summary["evaluated_points"]),
        ("invalid_points", summary["invalid_points"]),
        ("observed_yield", summary["observed_yield"]),
        ("planned_pass_fraction", summary["planned_pass_fraction"]),
        ("confidence_low", interval["low"]),
        ("confidence_high", interval["high"]),
    ):
        writer.writerow({"record_type": "yield", "name": name, "value": value})
    for name, count in summary["classifications"].items():
        writer.writerow(
            {"record_type": "classification", "name": name, "count": count}
        )
    for name, values in summary["measurements"].items():
        writer.writerow(
            {
                "record_type": "measurement",
                "name": name,
                **{field: values[field] for field in fields if field in values},
            }
        )
    for margin in summary["requirement_margins"]:
        writer.writerow(
            {
                "record_type": "requirement_margin",
                "name": f"{margin['analysis']}:{margin['metric']}",
                "unit": margin["unit"],
                **margin["statistics"],
            }
        )
    for corner in summary.get("corner_results", []):
        label = ",".join(
            f"{name}={value}" for name, value in corner["corners"].items()
        )
        interval = corner["yield_confidence_interval"]
        for name, value in (
            ("planned_points", corner["planned_points"]),
            ("evaluated_points", corner["evaluated_points"]),
            ("invalid_points", corner["invalid_points"]),
            ("observed_yield", corner["observed_yield"]),
            ("confidence_low", interval["low"]),
            ("confidence_high", interval["high"]),
        ):
            writer.writerow(
                {
                    "record_type": "corner_yield",
                    "name": f"{label}:{name}",
                    "value": value,
                }
            )
        for name, count in corner["classifications"].items():
            writer.writerow(
                {
                    "record_type": "corner_classification",
                    "name": f"{label}:{name}",
                    "count": count,
                }
            )
    return output.getvalue()


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"statistics artifact must not be a symlink: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def summarize_statistical_experiment(
    runs_dir: Path, experiment_id: str
) -> StatisticalSummaryResult:
    experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
        runs_dir, experiment_id
    )
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "statistical":
        raise ValueError(f"Experiment {experiment_id} is not a statistical study")
    provenance, _ = _verified_sampling_plan(runs_dir, source)
    point_metadata = source.get("point_metadata")
    corner_axes = source.get("corner_axes")
    if corner_axes and not isinstance(point_metadata, list):
        raise ValueError("corner statistical study is missing point_metadata")
    summary = build_statistics(
        results,
        point_metadata=point_metadata,
        corner_aggregate=source.get("corner_aggregate", False),
        sampling_provenance=provenance,
    )
    json_path = experiment_dir / "statistics.json"
    csv_path = experiment_dir / "statistics.csv"
    _write_atomic(
        json_path,
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_atomic(csv_path, _csv_document(summary))
    classifications = summary["classifications"]
    interval = summary["yield_confidence_interval"]
    return {
        "experiment_id": experiment_id,
        "statistics_json": str(json_path),
        "statistics_csv": str(csv_path),
        "planned_points": summary["planned_points"],
        "evaluated_points": summary["evaluated_points"],
        "passed_points": classifications["electrical_pass"],
        "electrical_failures": classifications["electrical_failure"],
        "invalid_points": summary["invalid_points"],
        "observed_yield": summary["observed_yield"],
        "confidence_low": interval["low"],
        "confidence_high": interval["high"],
        "corner_aggregate": summary.get("corner_aggregate"),
        "corner_results": summary.get("corner_results", []),
        "sampling_provenance": provenance,
    }
