"""Deterministic worst-evidenced-case rankings for statistical studies."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import uuid
from pathlib import Path
from typing import TypedDict

import experiment_index
import statistical_results

WORST_CASE_SCHEMA_VERSION = 1
WORST_CASE_LIMIT = 25
_OPERATORS = {"<", "<=", ">", ">="}


class WorstCaseAnalysisResult(TypedDict):
    experiment_id: str
    worst_cases_json: str
    worst_cases_csv: str
    requirement_count: int
    ranked_sample_count: int
    corner_count: int
    invalid_points: int


def _margin(operator: str, target: float, value: float) -> float:
    return target - value if operator in {"<", "<="} else value - target


def _passes(operator: str, target: float, value: float) -> bool:
    return {
        "<": value < target,
        "<=": value <= target,
        ">": value > target,
        ">=": value >= target,
    }[operator]


def _identity(
    analysis: str,
    metric: str,
    operator: str,
    target: float,
    unit: str,
    parameters: dict[str, object],
) -> str:
    encoded = json.dumps(
        [analysis, metric, operator, target, unit, parameters],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dense_ranks(values: list[float]) -> list[int]:
    ranks: list[int] = []
    rank = 0
    previous: float | None = None
    for value in values:
        if previous is None or value != previous:
            rank += 1
            previous = value
        ranks.append(rank)
    return ranks


def _corner_key(corners: dict[str, str]) -> str:
    return json.dumps(corners, separators=(",", ":"), ensure_ascii=False)


def build_worst_case_analysis(
    results: dict[str, object],
    *,
    point_metadata: list[dict[str, object]] | None = None,
    sampling_provenance: statistical_results.SamplingProvenance | None = None,
    _enforce_artifact_budget: bool = True,
) -> dict[str, object]:
    """Rank validated requirement results without comparing unlike checks."""
    points = results["points"]
    assert isinstance(points, list)
    planned_points = int(results["point_count"])
    metadata = statistical_results._point_metadata(point_metadata, planned_points)
    groups: dict[str, dict[str, object]] = {}
    corner_identities = (
        {
            _corner_key(entry["corners"]): dict(entry["corners"])
            for entry in metadata
        }
        if metadata is not None
        else {}
    )
    invalid_points = 0
    expected_requirement_ids: list[str] | None = None
    for point in sorted(points, key=lambda item: item["index"]):
        if statistical_results._classification(point) not in {
            "electrical_pass",
            "electrical_failure",
        }:
            invalid_points += 1
            continue
        point_index = int(point["index"])
        point_meta = None if metadata is None else metadata[point_index]
        requirement_index = 0
        point_requirement_ids: list[str] = []
        for analysis_entry in point.get("analyses", []):
            if not isinstance(analysis_entry, dict):
                continue
            analysis = analysis_entry.get("analysis")
            if not isinstance(analysis, dict):
                continue
            analysis_name = str(analysis_entry["name"])
            for requirement in analysis.get("results", []):
                threshold = requirement["threshold"]
                operator = str(threshold["operator"])
                if operator not in _OPERATORS:
                    raise ValueError("requirement ranking operator is invalid")
                target = float(threshold["target"])
                value = float(requirement["value"])
                if not math.isfinite(target) or not math.isfinite(value):
                    raise ValueError("requirement ranking values must be finite")
                metric = str(requirement["metric"])
                unit = str(requirement["unit"])
                passed = bool(requirement["passed"])
                if passed != _passes(operator, target, value):
                    raise ValueError("requirement pass state does not match its threshold")
                parameters = dict(requirement.get("parameters", {}))
                check_id = _identity(
                    analysis_name,
                    metric,
                    operator,
                    target,
                    unit,
                    parameters,
                )
                point_requirement_ids.append(check_id)
                group = groups.setdefault(
                    check_id,
                    {
                        "check_id": check_id,
                        "analysis": analysis_name,
                        "requirement_index": requirement_index,
                        "metric": metric,
                        "operator": operator,
                        "target": target,
                        "unit": unit,
                        "parameters": parameters,
                        "records": [],
                    },
                )
                expected = (
                    group["analysis"],
                    group["requirement_index"],
                    group["metric"],
                    group["operator"],
                    group["target"],
                    group["unit"],
                    group["parameters"],
                )
                observed = (
                    analysis_name,
                    requirement_index,
                    metric,
                    operator,
                    target,
                    unit,
                    parameters,
                )
                if observed != expected:
                    raise ValueError(
                        "electrically evaluated points must contain one stable "
                        "ordered requirement set"
                    )
                record: dict[str, object] = {
                    "point_index": point_index,
                    "parameters": dict(point["parameters"]),
                    "value": value,
                    "margin": _margin(operator, target, value),
                    "passed": passed,
                    "evidence_path": f"point-{point_index:04d}/",
                }
                if point_meta is not None:
                    record["sample_index"] = point_meta["sample_index"]
                    record["corners"] = dict(point_meta["corners"])
                group["records"].append(record)
                requirement_index += 1
        if expected_requirement_ids is None:
            expected_requirement_ids = point_requirement_ids
        elif point_requirement_ids != expected_requirement_ids:
            raise ValueError(
                "electrically evaluated points must contain one stable ordered "
                "requirement set"
            )

    requirements: list[dict[str, object]] = []
    ranked_sample_count = 0
    ordered_groups = sorted(
        groups.values(),
        key=lambda group: (
            group["analysis"],
            group["requirement_index"],
            group["check_id"],
        ),
    )
    artifact_rows = len(ordered_groups) * len(corner_identities) + sum(
        min(WORST_CASE_LIMIT, len(group["records"])) for group in ordered_groups
    )
    if (
        _enforce_artifact_budget
        and artifact_rows > statistical_results.MAX_ANALYSIS_ROWS
    ):
        raise ValueError("worst-case analysis exceeds artifact row budget")
    for group in ordered_groups:
        records = group.pop("records")
        assert isinstance(records, list)
        records.sort(key=lambda item: (item["margin"], item["point_index"]))
        ranked_sample_count += len(records)
        ranks = _dense_ranks([float(record["margin"]) for record in records])
        for record, rank in zip(records, ranks):
            record["rank"] = rank
        cutoff = min(WORST_CASE_LIMIT, len(records))
        if cutoff:
            cutoff_margin = records[cutoff - 1]["margin"]
            returned_count = sum(
                record["margin"] <= cutoff_margin for record in records
            )
            if (
                _enforce_artifact_budget
                and artifact_rows + returned_count - cutoff
                > statistical_results.MAX_ANALYSIS_ROWS
            ):
                raise ValueError("worst-case analysis exceeds artifact row budget")
            worst_cases = [
                record for record in records if record["margin"] <= cutoff_margin
            ]
        else:
            worst_cases = []
        artifact_rows += len(worst_cases) - cutoff
        by_corner: dict[str, dict[str, object]] = {
            identity: {"corners": corners, "records": []}
            for identity, corners in corner_identities.items()
        }
        for record in records:
            corners = record.get("corners", {})
            assert isinstance(corners, dict)
            corner_identity = _corner_key(corners)
            normalized_corners = {str(name): str(value) for name, value in corners.items()}
            if metadata is None:
                continue
            corner = by_corner.setdefault(
                corner_identity,
                {"corners": normalized_corners, "records": []},
            )
            corner["records"].append(record)
        corner_rankings: list[dict[str, object]] = []
        for corner_identity in sorted(by_corner):
            corner = by_corner[corner_identity]
            corner_records = corner["records"]
            assert isinstance(corner_records, list)
            worst_margin = (
                None
                if not corner_records
                else min(float(record["margin"]) for record in corner_records)
            )
            corner_rankings.append(
                {
                    "corners": corner["corners"],
                    "evaluated_samples": len(corner_records),
                    "worst_margin": worst_margin,
                    "worst_point_indexes": [
                        record["point_index"]
                        for record in corner_records
                        if worst_margin is not None
                        and record["margin"] == worst_margin
                    ],
                }
            )
        corner_rankings.sort(
            key=lambda item: (
                item["worst_margin"] is None,
                0 if item["worst_margin"] is None else item["worst_margin"],
                _corner_key(item["corners"]),
            )
        )
        corner_ranks = _dense_ranks(
            [
                float(corner["worst_margin"])
                for corner in corner_rankings
                if corner["worst_margin"] is not None
            ]
        )
        rank_iterator = iter(corner_ranks)
        for corner in corner_rankings:
            corner["rank"] = (
                None if corner["worst_margin"] is None else next(rank_iterator)
            )
        requirements.append(
            {
                **group,
                "evaluated_samples": len(records),
                "returned_samples": len(worst_cases),
                "nominal_limit": WORST_CASE_LIMIT,
                "worst_margin": None if not records else records[0]["margin"],
                "worst_cases": worst_cases,
                "corner_rankings": corner_rankings,
            }
        )
    result: dict[str, object] = {
        "schema_version": WORST_CASE_SCHEMA_VERSION,
        "experiment_id": results["experiment_id"],
        "ranking_method": "ascending_requirement_margin_dense_ties",
        "invalid_points": invalid_points + planned_points - len(points),
        "requirement_count": len(requirements),
        "ranked_sample_count": ranked_sample_count,
        "corner_count": len(corner_identities),
        "requirements": requirements,
    }
    if sampling_provenance is not None:
        result["sampling_provenance"] = dict(sampling_provenance)
    return result


def _csv_document(analysis: dict[str, object]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "record_type",
        "check_id",
        "rank",
        "analysis",
        "requirement_index",
        "metric",
        "operator",
        "target",
        "unit",
        "requirement_parameters",
        "point_index",
        "sample_index",
        "corners",
        "point_parameters",
        "value",
        "margin",
        "passed",
        "evidence_path",
        "evaluated_samples",
        "worst_point_indexes",
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
            "unit": requirement["unit"],
            "requirement_parameters": json.dumps(
                requirement["parameters"], sort_keys=True, separators=(",", ":")
            ),
        }
        for record in requirement["worst_cases"]:
            writer.writerow(
                {
                    "record_type": "worst_case",
                    **common,
                    "rank": record["rank"],
                    "point_index": record["point_index"],
                    "sample_index": record.get("sample_index"),
                    "corners": json.dumps(
                        record.get("corners", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "point_parameters": json.dumps(
                        record["parameters"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "value": record["value"],
                    "margin": record["margin"],
                    "passed": record["passed"],
                    "evidence_path": record["evidence_path"],
                }
            )
        for corner in requirement["corner_rankings"]:
            writer.writerow(
                {
                    "record_type": "corner_ranking",
                    **common,
                    "rank": corner["rank"],
                    "corners": json.dumps(
                        corner["corners"], sort_keys=True, separators=(",", ":")
                    ),
                    "margin": corner["worst_margin"],
                    "evaluated_samples": corner["evaluated_samples"],
                    "worst_point_indexes": json.dumps(
                        corner["worst_point_indexes"], separators=(",", ":")
                    ),
                }
            )
    return output.getvalue()


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"worst-case artifact must not be a symlink: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def analyze_statistical_worst_cases(
    runs_dir: Path, experiment_id: str
) -> WorstCaseAnalysisResult:
    experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
        runs_dir, experiment_id
    )
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "statistical":
        raise ValueError(f"Experiment {experiment_id} is not a statistical study")
    provenance, _ = statistical_results._verified_sampling_plan(runs_dir, source)
    point_metadata = source.get("point_metadata")
    corner_axes = source.get("corner_axes")
    if corner_axes and not isinstance(point_metadata, list):
        raise ValueError("corner statistical study is missing point_metadata")
    analysis = build_worst_case_analysis(
        results,
        point_metadata=point_metadata,
        sampling_provenance=provenance,
    )
    json_path = experiment_dir / "worst_cases.json"
    csv_path = experiment_dir / "worst_cases.csv"
    _write_atomic(
        json_path,
        json.dumps(analysis, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_atomic(csv_path, _csv_document(analysis))
    return {
        "experiment_id": experiment_id,
        "worst_cases_json": str(json_path),
        "worst_cases_csv": str(csv_path),
        "requirement_count": int(analysis["requirement_count"]),
        "ranked_sample_count": int(analysis["ranked_sample_count"]),
        "corner_count": int(analysis["corner_count"]),
        "invalid_points": int(analysis["invalid_points"]),
    }
