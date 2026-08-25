"""Yield and descriptive statistics for terminal statistical experiments."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
import uuid
from pathlib import Path
from typing import TypedDict

import experiment_index

STATISTICS_SCHEMA_VERSION = 1
CONFIDENCE_LEVEL = 0.95
_Z_95 = 1.959963984540054


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


def build_statistics(results: dict[str, object]) -> dict[str, object]:
    """Build deterministic statistics from already validated result data."""
    points = results["points"]
    assert isinstance(points, list)
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
    for point in sorted(points, key=lambda item: item["index"]):
        classification = _classification(point)
        classifications[classification] += 1
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

    classifications["unfinished"] = int(results["point_count"]) - len(points)
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
    return {
        "schema_version": STATISTICS_SCHEMA_VERSION,
        "experiment_id": results["experiment_id"],
        "confidence_level": CONFIDENCE_LEVEL,
        "planned_points": results["point_count"],
        "finished_points": len(points),
        "classifications": classifications,
        "evaluated_points": evaluated,
        "invalid_points": invalid,
        "observed_yield": None if evaluated == 0 else passed / evaluated,
        "planned_pass_fraction": passed / int(results["point_count"]),
        "yield_confidence_interval": {"method": "wilson", "low": low, "high": high},
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
    summary = build_statistics(results)
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
    }
