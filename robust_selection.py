"""Deterministic tolerance proof and selection for optimization finalists."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict

import experiment_index
import optimization_engine
import statistical_engine
from statistical_results import _wilson


ROBUST_PLAN_SCHEMA_VERSION = 1
ROBUST_PLAN_GENERATOR_VERSION = "optimization-finalist-yield-v1"
ROBUST_RESULT_SCHEMA_VERSION = 1
ROBUST_RESULT_GENERATOR_VERSION = "joint-ac-transient-selection-v1"
MAX_FINALISTS = 8
FINALIST_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class RobustSelectionPlanResult(TypedDict):
    plan_id: str
    plan_file: str
    plan_sha256: str
    finalist_count: int
    sample_count: int
    point_count: int
    statistical_plan_ids: dict[str, str]


class RobustSelectionStudyResult(TypedDict):
    study_id: str
    plan_id: str
    study_dir: str
    results_json: str
    results_csv: str
    report_html: str
    finalist_count: int
    selected_finalist: str | None
    selection_explanation: str


class RobustSelectionComparisonResult(TypedDict):
    comparison_id: str
    comparison_dir: str
    comparison_json: str
    report_html: str
    passed: bool
    selected_finalist: str | None
    exact_mismatches: int
    numeric_mismatches: int


def _canonical_json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _write_once(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"artifact target must not be a symlink: {path.name}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"existing artifact differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _root(runs_dir: Path, name: str) -> Path:
    root = runs_dir.resolve() / name
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError(f"{name} root must be a real directory")
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve().parent != runs_dir.resolve():
        raise ValueError(f"{name} root must remain inside runs")
    return root.resolve()


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def _source_finalist(
    runs_dir: Path, finalist: dict[str, object], tie_break_rank: int
) -> dict[str, object]:
    label = finalist.get("label")
    study_id = finalist.get("study_id")
    candidate_index = finalist.get("candidate_index")
    if not isinstance(label, str) or FINALIST_LABEL.fullmatch(label) is None:
        raise ValueError("finalist label is invalid")
    if not isinstance(study_id, str):
        raise ValueError(f"finalist {label} study_id is invalid")
    if not isinstance(candidate_index, int) or isinstance(candidate_index, bool):
        raise ValueError(f"finalist {label} candidate_index is invalid")
    result, artifact = optimization_engine._load_verified_optimization_study(
        runs_dir, study_id
    )
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not 0 <= candidate_index < len(candidates):
        raise ValueError(f"finalist {label} candidate does not exist")
    candidate = candidates[candidate_index]
    if (
        not isinstance(candidate, dict)
        or candidate.get("status") != "feasible"
        or not (candidate.get("selected") is True or candidate.get("pareto") is True)
        or not isinstance(candidate.get("parameters"), dict)
        or not isinstance(candidate.get("objectives"), dict)
        or not isinstance(candidate.get("constraints"), dict)
    ):
        raise ValueError(f"finalist {label} must be a feasible selected or Pareto candidate")
    return {
        "label": label,
        "tie_break_rank": tie_break_rank,
        "source_study_id": study_id,
        "source_results_sha256": hashlib.sha256(artifact).hexdigest(),
        "source_plan_id": result["plan_id"],
        "source_candidate_index": candidate_index,
        "parameters": candidate["parameters"],
        "nominal_objectives": candidate["objectives"],
        "nominal_constraints": candidate["constraints"],
    }


def _validate_variable_nominals(
    finalist: dict[str, object], variables: list[statistical_engine.StatisticalVariable]
) -> None:
    by_name = {variable.get("name"): variable for variable in variables}
    parameters = finalist["parameters"]
    assert isinstance(parameters, dict)
    missing = sorted(set(parameters) - set(by_name))
    if missing:
        raise ValueError(
            f"finalist {finalist['label']} variables omit design parameters: "
            + ", ".join(missing)
        )
    for name, value in parameters.items():
        variable = by_name[name]
        if "nominal" not in variable or _decimal(variable["nominal"], str(name)) != _decimal(
            value, str(name)
        ):
            raise ValueError(
                f"finalist {finalist['label']} variable {name} nominal must match the candidate"
            )


def generate_robust_selection_plan(
    runs_dir: Path,
    finalists: list[dict[str, object]],
    variables_by_finalist: dict[str, list[statistical_engine.StatisticalVariable]],
    sample_count: int,
    seed: int,
    *,
    correlations: list[statistical_engine.StatisticalCorrelation] | None = None,
    corner_axes: list[statistical_engine.StatisticalCornerAxis] | None = None,
    sampling_method: str = "halton",
) -> RobustSelectionPlanResult:
    """Freeze source finalists and one paired statistical plan per finalist."""
    if not isinstance(finalists, list) or not 2 <= len(finalists) <= MAX_FINALISTS:
        raise ValueError(f"finalists must contain 2 to {MAX_FINALISTS} entries")
    sources = [
        _source_finalist(runs_dir, finalist, rank)
        for rank, finalist in enumerate(finalists)
    ]
    labels = [str(source["label"]) for source in sources]
    if len(set(labels)) != len(labels) or set(variables_by_finalist) != set(labels):
        raise ValueError("finalist labels and variable sets must match exactly")
    statistical: dict[str, dict[str, object]] = {}
    point_count: int | None = None
    for source in sources:
        label = str(source["label"])
        variables = variables_by_finalist[label]
        _validate_variable_nominals(source, variables)
        saved = statistical_engine.generate_statistical_plan(
            runs_dir,
            variables,
            sample_count,
            seed,
            correlations,
            corner_axes,
            False,
            sampling_method=sampling_method,
        )
        if point_count is None:
            point_count = saved["point_count"]
        elif saved["point_count"] != point_count:
            raise ValueError("finalist statistical plans must have equal point counts")
        statistical[label] = {
            "plan_id": saved["plan_id"],
            "plan_sha256": saved["plan_sha256"],
            "definition_hash": saved["definition_hash"],
        }
    definition = {
        "finalists": sources,
        "statistical_plans": statistical,
        "sample_count": sample_count,
        "seed": seed,
        "sampling_method": sampling_method,
        "corner_axes": corner_axes or [],
        "required_experiments": ["ac", "transient"],
        "selection_policy": (
            "complete-evidence-then-worst-corner-joint-yield-then-source-rank-v1"
        ),
    }
    portability_definition = {
        "finalists": [
            {
                "label": item["label"],
                "tie_break_rank": item["tie_break_rank"],
                "parameters": item["parameters"],
            }
            for item in sources
        ],
        "statistical_plans": statistical,
        "sample_count": sample_count,
        "seed": seed,
        "sampling_method": sampling_method,
        "corner_axes": corner_axes or [],
        "required_experiments": ["ac", "transient"],
        "selection_policy": definition["selection_policy"],
    }
    definition["portability_signature"] = hashlib.sha256(
        _canonical_json(portability_definition).encode("utf-8")
    ).hexdigest()
    definition_hash = hashlib.sha256(
        _canonical_json(definition).encode("utf-8")
    ).hexdigest()
    plan = {
        "schema_version": ROBUST_PLAN_SCHEMA_VERSION,
        "generator_version": ROBUST_PLAN_GENERATOR_VERSION,
        "definition_hash": definition_hash,
        "definition": definition,
        "finalist_count": len(sources),
        "point_count_per_finalist": point_count,
    }
    artifact = (_canonical_json(plan, pretty=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(artifact).hexdigest()
    plan_id = f"robust-selection-plan-{digest[:16]}"
    root = _root(runs_dir, "robust-selection-plans")
    plan_dir = root / plan_id
    plan_dir.mkdir(exist_ok=True)
    if plan_dir.resolve().parent != root or plan_dir.is_symlink():
        raise ValueError("robust selection plan must remain inside runs")
    plan_file = plan_dir / "robust_selection_plan.json"
    _write_once(plan_file, artifact)
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": digest,
        "finalist_count": len(sources),
        "sample_count": sample_count,
        "point_count": int(point_count or 0) * len(sources),
        "statistical_plan_ids": {
            label: str(item["plan_id"]) for label, item in statistical.items()
        },
    }


def portability_summary(result: dict[str, object]) -> dict[str, object]:
    """Return the bounded decision and numeric surface used across platforms."""
    finalists = result.get("finalists")
    if not isinstance(finalists, list):
        raise ValueError("robust selection result finalists are invalid")
    return {
        "portability_signature": result.get("portability_signature"),
        "selected_finalist": result.get("selected_finalist"),
        "finalists": [
            {
                "label": finalist["label"],
                "parameters": finalist["parameters"],
                "complete_evidence": finalist["complete_evidence"],
                "corner_results": finalist["corner_results"],
                "worst_requirements": finalist["worst_requirements"],
            }
            for finalist in finalists
            if isinstance(finalist, dict)
        ],
    }


def compare_portability_summaries(
    baseline: dict[str, object],
    candidate: dict[str, object],
    metric_tolerances: dict[str, float],
) -> dict[str, object]:
    """Compare robust decisions exactly and worst metrics within tolerances."""
    for label, document in (("baseline", baseline), ("candidate", candidate)):
        if not isinstance(document, dict) or not isinstance(document.get("finalists"), list):
            raise ValueError(f"{label} portability summary is invalid")
    if set(metric_tolerances) == set() or any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        for value in metric_tolerances.values()
    ):
        raise ValueError("metric tolerances must be finite nonnegative values")
    exact_mismatches = 0
    numeric_mismatches = 0
    if baseline.get("portability_signature") != candidate.get("portability_signature"):
        exact_mismatches += 1
    if baseline.get("selected_finalist") != candidate.get("selected_finalist"):
        exact_mismatches += 1
    baseline_finalists = {
        str(item["label"]): item
        for item in baseline["finalists"]  # type: ignore[index]
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    }
    candidate_finalists = {
        str(item["label"]): item
        for item in candidate["finalists"]  # type: ignore[index]
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    }
    if set(baseline_finalists) != set(candidate_finalists):
        raise ValueError("portability finalist labels do not match")
    comparisons: list[dict[str, object]] = []
    for label in sorted(baseline_finalists):
        left = baseline_finalists[label]
        right = candidate_finalists[label]
        exact = {
            "parameters": left.get("parameters") == right.get("parameters"),
            "complete_evidence": left.get("complete_evidence")
            == right.get("complete_evidence"),
            "corner_results": left.get("corner_results") == right.get("corner_results"),
        }
        exact_mismatches += sum(not value for value in exact.values())
        left_requirements = {
            (
                item.get("experiment"),
                item.get("analysis"),
                item.get("metric"),
                item.get("operator"),
            ): item
            for item in left.get("worst_requirements", [])
            if isinstance(item, dict)
        }
        right_requirements = {
            (
                item.get("experiment"),
                item.get("analysis"),
                item.get("metric"),
                item.get("operator"),
            ): item
            for item in right.get("worst_requirements", [])
            if isinstance(item, dict)
        }
        if set(left_requirements) != set(right_requirements):
            raise ValueError(f"finalist {label} requirement sets do not match")
        deltas: list[dict[str, object]] = []
        for key in sorted(left_requirements, key=str):
            metric = str(key[2])
            if metric not in metric_tolerances:
                raise ValueError(f"missing portability tolerance for metric {metric}")
            left_value = float(left_requirements[key]["value"])
            right_value = float(right_requirements[key]["value"])
            delta = abs(left_value - right_value)
            allowed = float(metric_tolerances[metric])
            passed = delta <= allowed
            numeric_mismatches += not passed
            deltas.append(
                {
                    "experiment": key[0],
                    "analysis": key[1],
                    "metric": metric,
                    "operator": key[3],
                    "baseline": left_value,
                    "candidate": right_value,
                    "absolute_delta": delta,
                    "allowed_delta": allowed,
                    "passed": passed,
                }
            )
        comparisons.append(
            {
                "label": label,
                "exact": exact,
                "requirements": deltas,
                "passed": all(exact.values()) and all(item["passed"] for item in deltas),
            }
        )
    return {
        "generator_version": "robust-selection-platform-comparison-v1",
        "portability_signature": baseline.get("portability_signature"),
        "selected_finalist": baseline.get("selected_finalist"),
        "exact_mismatches": exact_mismatches,
        "numeric_mismatches": numeric_mismatches,
        "passed": exact_mismatches == 0 and numeric_mismatches == 0,
        "finalists": comparisons,
    }


def write_portability_comparison(
    runs_dir: Path,
    baseline: dict[str, object],
    candidate: dict[str, object],
    metric_tolerances: dict[str, float],
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> RobustSelectionComparisonResult:
    comparison = compare_portability_summaries(
        baseline, candidate, metric_tolerances
    )
    comparison.update(
        baseline_label=baseline_label,
        candidate_label=candidate_label,
        metric_tolerances=metric_tolerances,
    )
    digest = hashlib.sha256(_canonical_json(comparison).encode("utf-8")).hexdigest()
    comparison_id = f"robust-selection-comparison-{digest[:16]}"
    root = _root(runs_dir, "robust-selection-comparisons")
    comparison_dir = root / comparison_id
    comparison_dir.mkdir(exist_ok=True)
    comparison_json = comparison_dir / "robust_selection_comparison.json"
    report_html = comparison_dir / "report.html"
    _write_once(
        comparison_json,
        (_canonical_json(comparison, pretty=True) + "\n").encode("utf-8"),
    )
    rows = "".join(
        f"<tr><td>{html.escape(str(item['label']))}</td><td>{'PASS' if item['passed'] else 'FAIL'}</td>"
        f"<td>{sum(not value for value in item['exact'].values())}</td>"
        f"<td>{sum(not value['passed'] for value in item['requirements'])}</td></tr>"
        for item in comparison["finalists"]
    )
    status = "PASS" if comparison["passed"] else "FAIL"
    report = f"""<!doctype html><html><head><meta charset="utf-8"><title>Robust selection platform comparison</title><style>body{{font:16px/1.5 system-ui;max-width:1000px;margin:auto;padding:32px;background:#0d1117;color:#e6edf3}}section{{background:#161b22;padding:20px;border-radius:10px;margin:20px 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:8px;border-bottom:1px solid #30363d}}a{{color:#58a6ff}}</style></head><body><h1>{status}: {html.escape(baseline_label)} vs {html.escape(candidate_label)}</h1><p>Selected finalist: {html.escape(str(comparison['selected_finalist']))}</p><section><h2>Acceptance</h2><p>Plan portability, finalist definitions, joint corner outcomes, and selection match exactly. Worst requirement values remain inside their declared metric tolerances.</p><table><thead><tr><th>Finalist</th><th>Status</th><th>Exact mismatches</th><th>Numeric mismatches</th></tr></thead><tbody>{rows}</tbody></table></section><p><a href="robust_selection_comparison.json">comparison JSON</a></p></body></html>"""
    _write_once(report_html, report.encode("utf-8"))
    return {
        "comparison_id": comparison_id,
        "comparison_dir": str(comparison_dir),
        "comparison_json": str(comparison_json),
        "report_html": str(report_html),
        "passed": bool(comparison["passed"]),
        "selected_finalist": comparison["selected_finalist"],
        "exact_mismatches": int(comparison["exact_mismatches"]),
        "numeric_mismatches": int(comparison["numeric_mismatches"]),
    }


def _load_study_result(runs_dir: Path, study_id: str) -> dict[str, object]:
    if re.fullmatch(r"robust-selection-study-[0-9a-f]{16}", study_id) is None:
        raise ValueError("invalid robust selection study_id")
    root = _root(runs_dir, "robust-selection-studies")
    study_dir = root / study_id
    if study_dir.is_symlink() or not study_dir.is_dir() or study_dir.resolve().parent != root:
        raise FileNotFoundError(f"robust selection study not found: {study_id}")
    path = study_dir / "robust_selection_results.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("robust selection result is not a regular file")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("robust selection result is invalid") from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != ROBUST_RESULT_SCHEMA_VERSION
        or result.get("generator_version") != ROBUST_RESULT_GENERATOR_VERSION
        or result.get("study_id") != study_id
    ):
        raise ValueError("robust selection result identity is invalid")
    return result


def compare_saved_robust_selection_studies(
    runs_dir: Path,
    baseline_study_id: str,
    candidate_study_id: str,
    metric_tolerances: dict[str, float],
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> RobustSelectionComparisonResult:
    return write_portability_comparison(
        runs_dir,
        portability_summary(_load_study_result(runs_dir, baseline_study_id)),
        portability_summary(_load_study_result(runs_dir, candidate_study_id)),
        metric_tolerances,
        baseline_label=baseline_label,
        candidate_label=candidate_label,
    )


def load_robust_selection_plan(runs_dir: Path, plan_id: str) -> dict[str, object]:
    if re.fullmatch(r"robust-selection-plan-[0-9a-f]{16}", plan_id) is None:
        raise ValueError("invalid robust selection plan_id")
    root = _root(runs_dir, "robust-selection-plans")
    plan_dir = root / plan_id
    if plan_dir.is_symlink() or not plan_dir.is_dir() or plan_dir.resolve().parent != root:
        raise FileNotFoundError(f"robust selection plan not found: {plan_id}")
    plan_file = plan_dir / "robust_selection_plan.json"
    if plan_file.is_symlink() or not plan_file.is_file():
        raise ValueError("robust selection plan artifact is not a regular file")
    artifact = plan_file.read_bytes()
    if hashlib.sha256(artifact).hexdigest()[:16] != plan_id.rsplit("-", 1)[-1]:
        raise ValueError("robust selection plan content address does not match")
    try:
        plan = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid robust selection plan artifact") from exc
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != ROBUST_PLAN_SCHEMA_VERSION
        or plan.get("generator_version") != ROBUST_PLAN_GENERATOR_VERSION
        or not isinstance(plan.get("definition"), dict)
    ):
        raise ValueError("unsupported robust selection plan")
    expected = hashlib.sha256(
        _canonical_json(plan["definition"]).encode("utf-8")
    ).hexdigest()
    if plan.get("definition_hash") != expected:
        raise ValueError("robust selection definition hash does not match")
    return plan


def inspect_robust_selection_plan(
    runs_dir: Path, plan_id: str
) -> RobustSelectionPlanResult:
    plan = load_robust_selection_plan(runs_dir, plan_id)
    plan_file = (
        runs_dir.resolve()
        / "robust-selection-plans"
        / plan_id
        / "robust_selection_plan.json"
    )
    definition = plan["definition"]
    assert isinstance(definition, dict)
    statistical = definition["statistical_plans"]
    assert isinstance(statistical, dict)
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "finalist_count": int(plan["finalist_count"]),
        "sample_count": int(definition["sample_count"]),
        "point_count": int(plan["point_count_per_finalist"])
        * int(plan["finalist_count"]),
        "statistical_plan_ids": {
            label: str(item["plan_id"]) for label, item in statistical.items()
        },
    }


def _classification(point: dict[str, object]) -> str:
    if point.get("simulation_status") != "completed":
        return "simulation_error"
    analyses = point.get("analyses")
    if not isinstance(analyses, list) or any(
        not isinstance(item, dict) or item.get("status") != "completed"
        for item in analyses
    ):
        return "analysis_error"
    return "electrical_pass" if point.get("all_passed") is True else "electrical_failure"


def _requirement_margins(
    experiment_name: str, point: dict[str, object]
) -> list[dict[str, object]]:
    margins: list[dict[str, object]] = []
    analyses = point.get("analyses")
    if not isinstance(analyses, list):
        return margins
    for analysis_entry in analyses:
        if not isinstance(analysis_entry, dict):
            continue
        analysis_name = str(analysis_entry.get("name", "analysis"))
        analysis = analysis_entry.get("analysis")
        results = analysis.get("results") if isinstance(analysis, dict) else None
        if not isinstance(results, list):
            continue
        for requirement_index, result in enumerate(results):
            if not isinstance(result, dict) or not isinstance(result.get("threshold"), dict):
                continue
            threshold = result["threshold"]
            operator = threshold.get("operator")
            value = result.get("value")
            target = threshold.get("target")
            if operator not in {"<=", ">="}:
                continue
            numeric_value = float(_decimal(value, "requirement value"))
            numeric_target = float(_decimal(target, "requirement target"))
            margin = (
                numeric_target - numeric_value
                if operator == "<="
                else numeric_value - numeric_target
            )
            margins.append(
                {
                    "key": (
                        f"{experiment_name}:{analysis_name}:"
                        f"{result.get('metric')}:{requirement_index}"
                    ),
                    "experiment": experiment_name,
                    "analysis": analysis_name,
                    "metric": result.get("metric"),
                    "operator": operator,
                    "target": numeric_target,
                    "value": numeric_value,
                    "unit": result.get("unit", threshold.get("unit", "")),
                    "margin": margin,
                    "passed": result.get("passed") is True,
                }
            )
    return margins


def _dominant_sensitivities(
    documents: dict[str, dict[str, object]], limit: int = 8
) -> list[dict[str, object]]:
    associations: list[dict[str, object]] = []
    for experiment_name in ("ac", "transient"):
        analysis = documents.get(f"{experiment_name}_sensitivity")
        requirements = analysis.get("requirements") if isinstance(analysis, dict) else None
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            scopes = requirement.get("scopes")
            if not isinstance(scopes, list):
                continue
            for scope in scopes:
                if not isinstance(scope, dict) or not isinstance(scope.get("variables"), list):
                    continue
                for variable in scope["variables"]:
                    if not isinstance(variable, dict) or variable.get("rho") is None:
                        continue
                    associations.append(
                        {
                            "experiment": experiment_name,
                            "analysis": requirement.get("analysis"),
                            "metric": requirement.get("metric"),
                            "corners": scope.get("corners", {}),
                            "variable": variable.get("variable"),
                            "rho": float(variable["rho"]),
                            "correlated_with": variable.get("correlated_with", []),
                        }
                    )
    associations.sort(
        key=lambda item: (
            -abs(float(item["rho"])),
            str(item["experiment"]),
            str(item["metric"]),
            str(item["variable"]),
        )
    )
    return associations[:limit]


def _joint_candidate(
    finalist: dict[str, object],
    statistical_plan: dict[str, object],
    documents: dict[str, dict[str, object]],
) -> dict[str, object]:
    plan_points = statistical_plan["points"]
    assert isinstance(plan_points, list)
    point_results: list[dict[str, object]] = []
    corner_counts: dict[str, dict[str, object]] = {}
    worst_requirements: dict[str, dict[str, object]] = {}
    for plan_point in plan_points:
        assert isinstance(plan_point, dict)
        index = int(plan_point["index"])
        classifications = {
            name: _classification(documents[name]["points"][index])  # type: ignore[index]
            for name in ("ac", "transient")
        }
        invalid = any(
            value in {"simulation_error", "analysis_error"}
            for value in classifications.values()
        )
        passed = not invalid and all(
            value == "electrical_pass" for value in classifications.values()
        )
        corners = plan_point.get("corners", {})
        corner_key = _canonical_json(corners)
        bucket = corner_counts.setdefault(
            corner_key,
            {"corners": corners, "evaluated": 0, "passed": 0, "invalid": 0},
        )
        if invalid:
            bucket["invalid"] = int(bucket["invalid"]) + 1
        else:
            bucket["evaluated"] = int(bucket["evaluated"]) + 1
            bucket["passed"] = int(bucket["passed"]) + int(passed)
        point_results.append(
            {
                "index": index,
                "sample_index": plan_point.get("sample_index", index),
                "corners": corners,
                "parameters": plan_point["parameters"],
                "experiment_classifications": classifications,
                "classification": "invalid" if invalid else "pass" if passed else "failure",
            }
        )
        for experiment_name in ("ac", "transient"):
            document = documents[experiment_name]
            point = document["points"][index]  # type: ignore[index]
            assert isinstance(point, dict)
            for requirement in _requirement_margins(experiment_name, point):
                key = str(requirement.pop("key"))
                current = worst_requirements.get(key)
                if current is None or float(requirement["margin"]) < float(
                    current["margin"]
                ):
                    worst_requirements[key] = {
                        **requirement,
                        "point_index": index,
                        "corners": corners,
                    }
    corner_results: list[dict[str, object]] = []
    for key in sorted(corner_counts):
        bucket = corner_counts[key]
        evaluated = int(bucket["evaluated"])
        passed = int(bucket["passed"])
        low, high = _wilson(passed, evaluated)
        corner_results.append(
            {
                **bucket,
                "observed_yield": None if evaluated == 0 else passed / evaluated,
                "confidence_low": low,
                "confidence_high": high,
            }
        )
    complete = all(int(item["invalid"]) == 0 for item in corner_results)
    yields = [
        float(item["observed_yield"])
        for item in corner_results
        if item["observed_yield"] is not None
    ]
    lows = [
        float(item["confidence_low"])
        for item in corner_results
        if item["confidence_low"] is not None
    ]
    return {
        **finalist,
        "complete_evidence": complete,
        "worst_corner_yield": min(yields) if yields else None,
        "worst_corner_confidence_low": min(lows) if lows else None,
        "corner_results": corner_results,
        "worst_requirements": [
            worst_requirements[key] for key in sorted(worst_requirements)
        ],
        "dominant_sensitivities": _dominant_sensitivities(documents),
        "points": point_results,
        "selected": False,
    }


def build_robust_selection_result(
    plan_id: str,
    plan: dict[str, object],
    documents: dict[str, dict[str, dict[str, object]]],
    evidence: dict[str, dict[str, dict[str, str]]],
) -> dict[str, object]:
    definition = plan["definition"]
    assert isinstance(definition, dict)
    finalists = definition["finalists"]
    statistical_sources = definition["statistical_plans"]
    assert isinstance(finalists, list) and isinstance(statistical_sources, dict)
    records: list[dict[str, object]] = []
    for finalist in finalists:
        assert isinstance(finalist, dict)
        label = str(finalist["label"])
        statistical = statistical_sources[label]
        assert isinstance(statistical, dict)
        statistical_plan = documents[label]["statistical_plan"]
        records.append(
            _joint_candidate(
                finalist,
                statistical_plan,
                documents[label],
            )
        )
    eligible = [
        record
        for record in records
        if record["complete_evidence"]
        and record["worst_corner_yield"] is not None
    ]
    selected = max(
        eligible,
        key=lambda item: (
            float(item["worst_corner_yield"]),
            float(item["worst_corner_confidence_low"]),
            -int(item["tie_break_rank"]),
        ),
        default=None,
    )
    if selected is not None:
        selected["selected"] = True
    selected_label = None if selected is None else str(selected["label"])
    explanation = (
        "No finalist had complete paired AC and transient evidence."
        if selected is None
        else (
            f"{selected_label} was selected by worst named-corner joint yield; "
            "the frozen nominal source rank resolves exact statistical ties."
        )
    )
    identity = {
        "schema_version": ROBUST_RESULT_SCHEMA_VERSION,
        "generator_version": ROBUST_RESULT_GENERATOR_VERSION,
        "plan_id": plan_id,
        "evidence": evidence,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return {
        **identity,
        "study_id": f"robust-selection-study-{digest[:16]}",
        "portability_signature": definition["portability_signature"],
        "selection_policy": definition["selection_policy"],
        "selected_finalist": selected_label,
        "selection_explanation": explanation,
        "finalist_count": len(records),
        "finalists": records,
    }


def _verified_documents(
    runs_dir: Path,
    plan: dict[str, object],
    experiments: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, dict[str, object]]], dict[str, dict[str, dict[str, str]]]]:
    definition = plan["definition"]
    assert isinstance(definition, dict)
    finalists = definition["finalists"]
    statistical_sources = definition["statistical_plans"]
    assert isinstance(finalists, list) and isinstance(statistical_sources, dict)
    labels = {str(item["label"]) for item in finalists}  # type: ignore[index]
    if set(experiments) != labels:
        raise ValueError("experiments must exactly match finalist labels")
    documents: dict[str, dict[str, dict[str, object]]] = {}
    evidence: dict[str, dict[str, dict[str, str]]] = {}
    for label in sorted(labels):
        if set(experiments[label]) != {"ac", "transient"}:
            raise ValueError(f"finalist {label} must provide ac and transient experiments")
        source = statistical_sources[label]
        assert isinstance(source, dict)
        statistical_plan = statistical_engine.load_statistical_plan(
            runs_dir, str(source["plan_id"])
        )
        plan_file = (
            runs_dir
            / "statistical-plans"
            / str(source["plan_id"])
            / "statistical_plan.json"
        )
        if hashlib.sha256(plan_file.read_bytes()).hexdigest() != source["plan_sha256"]:
            raise ValueError(f"finalist {label} statistical plan hash does not match")
        documents[label] = {"statistical_plan": statistical_plan}
        evidence[label] = {}
        for name in ("ac", "transient"):
            experiment_id = experiments[label][name]
            experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
                runs_dir, experiment_id
            )
            if manifest.get("status") != "completed" or results.get("status") != "completed":
                raise ValueError(f"finalist {label} {name} experiment is not completed")
            points = results.get("points")
            plan_points = statistical_plan["points"]
            if not isinstance(points, list) or len(points) != len(plan_points):
                raise ValueError(f"finalist {label} {name} point count does not match")
            for planned, actual in zip(plan_points, points):
                if (
                    not isinstance(actual, dict)
                    or actual.get("index") != planned["index"]
                    or actual.get("parameters") != planned["parameters"]
                ):
                    raise ValueError(f"finalist {label} {name} points do not match")
            results_path = experiment_dir / "results.json"
            documents[label][name] = results
            for analysis_name in ("sensitivity", "worst_cases"):
                analysis_path = experiment_dir / f"{analysis_name}.json"
                if not analysis_path.is_file() or analysis_path.is_symlink():
                    raise ValueError(
                        f"finalist {label} {name} is missing {analysis_name}.json"
                    )
                analysis_document = json.loads(analysis_path.read_text(encoding="utf-8"))
                if not isinstance(analysis_document, dict):
                    raise ValueError(
                        f"finalist {label} {name} {analysis_name} is invalid"
                    )
                documents[label][f"{name}_{analysis_name}"] = analysis_document
            representative_raw = ""
            result_points = results["points"]
            assert isinstance(result_points, list)
            if result_points:
                first = result_points[0]
                analyses = first.get("analyses") if isinstance(first, dict) else None
                if isinstance(analyses, list) and analyses:
                    analysis = analyses[0].get("analysis") if isinstance(analyses[0], dict) else None
                    raw_file = analysis.get("raw_file") if isinstance(analysis, dict) else None
                    if isinstance(raw_file, str):
                        raw_path = Path(raw_file).resolve()
                        representative_raw = raw_path.relative_to(experiment_dir.resolve()).as_posix()
            evidence[label][name] = {
                "experiment_id": experiment_id,
                "results_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
                "representative_raw": representative_raw,
            }
    return documents, evidence


def _csv_document(result: dict[str, object]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "finalist",
            "selected",
            "corner",
            "evaluated",
            "passed",
            "invalid",
            "observed_yield",
            "confidence_low",
            "confidence_high",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for finalist in result["finalists"]:  # type: ignore[index]
        for corner in finalist["corner_results"]:
            writer.writerow(
                {
                    "finalist": finalist["label"],
                    "selected": str(finalist["selected"]).lower(),
                    "corner": _canonical_json(corner["corners"]),
                    "evaluated": corner["evaluated"],
                    "passed": corner["passed"],
                    "invalid": corner["invalid"],
                    "observed_yield": corner["observed_yield"],
                    "confidence_low": corner["confidence_low"],
                    "confidence_high": corner["confidence_high"],
                }
            )
    return output.getvalue().encode("utf-8")


def _percent(value: object) -> str:
    return "n/a" if value is None else f"{100 * float(value):.2f}%"


def _html_document(result: dict[str, object]) -> bytes:
    cards: list[str] = []
    evidence_links: list[str] = []
    nominal_rows: list[str] = []
    for finalist in result["finalists"]:  # type: ignore[index]
        corner_rows = "".join(
            "<tr>"
            f"<td>{html.escape(', '.join(f'{key}={value}' for key, value in corner['corners'].items()))}</td>"
            f"<td>{corner['passed']} / {corner['evaluated']}</td>"
            f"<td>{_percent(corner['observed_yield'])}</td>"
            f"<td>{_percent(corner['confidence_low'])}–{_percent(corner['confidence_high'])}</td>"
            "</tr>"
            for corner in finalist["corner_results"]
        )
        parameters = ", ".join(
            f"{name}={value}" for name, value in finalist["parameters"].items()
        )
        nominal_objectives = ", ".join(
            f"{name}: {float(item['value']):.5g} {item.get('unit', '')}"
            for name, item in finalist["nominal_objectives"].items()
        )
        nominal_rows.append(
            f"<tr><td>{html.escape(str(finalist['label']))}</td>"
            f"<td>{html.escape(parameters)}</td>"
            f"<td>{html.escape(nominal_objectives)}</td></tr>"
        )
        margin_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(item['experiment']))} / {html.escape(str(item['analysis']))}</td>"
            f"<td>{html.escape(str(item['metric']))}</td>"
            f"<td>{float(item['value']):.5g} {html.escape(str(item['unit']))}</td>"
            f"<td>{float(item['margin']):.5g} {html.escape(str(item['unit']))}</td>"
            f"<td>{html.escape(', '.join(f'{key}={value}' for key, value in item['corners'].items()))}</td>"
            "</tr>"
            for item in finalist["worst_requirements"]
        )
        sensitivity_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(item['experiment']))} / {html.escape(str(item['metric']))}</td>"
            f"<td>{html.escape(str(item['variable']))}</td>"
            f"<td>{float(item['rho']):.3f}</td>"
            f"<td>{html.escape(', '.join(f'{key}={value}' for key, value in item['corners'].items()))}</td>"
            "</tr>"
            for item in finalist["dominant_sensitivities"]
        )
        cards.append(
            f"<section><h2>{'Selected: ' if finalist['selected'] else ''}{html.escape(str(finalist['label']))}</h2>"
            f"<p>{html.escape(parameters)}</p><h3>Joint yield</h3><table><thead><tr><th>Corner</th><th>Joint pass</th><th>Yield</th><th>Wilson 95%</th></tr></thead><tbody>{corner_rows}</tbody></table>"
            f"<details><summary>Worst requirement margins</summary><table><thead><tr><th>Analysis</th><th>Metric</th><th>Worst value</th><th>Signed margin</th><th>Corner</th></tr></thead><tbody>{margin_rows}</tbody></table></details>"
            f"<details><summary>Dominant rank sensitivities</summary><table><thead><tr><th>Response</th><th>Variable</th><th>Spearman rho</th><th>Corner</th></tr></thead><tbody>{sensitivity_rows}</tbody></table></details></section>"
        )
        links = []
        for name, item in result["evidence"][finalist["label"]].items():
            experiment_id = item["experiment_id"]
            raw_link = (
                ""
                if not item.get("representative_raw")
                else f', <a href="../../{html.escape(experiment_id)}/{html.escape(str(item["representative_raw"]))}">representative RAW</a>'
            )
            links.append(
                f'<a href="../../{html.escape(experiment_id)}/report.html">{html.escape(name)} report</a>, '
                f'<a href="../../{html.escape(experiment_id)}/results.json">JSON</a>, '
                f'<a href="../../{html.escape(experiment_id)}/results.csv">CSV</a>, '
                f'<a href="../../{html.escape(experiment_id)}/experiment_manifest.json">manifest</a>, '
                f'<a href="../../{html.escape(experiment_id)}/worst_cases.json">worst cases</a>, '
                f'<a href="../../{html.escape(experiment_id)}/sensitivity.json">sensitivity</a>{raw_link}'
            )
        evidence_links.append(
            f"<li><strong>{html.escape(str(finalist['label']))}</strong>: "
            + " · ".join(links)
            + "</li>"
        )
    status = "PASS" if result["selected_finalist"] is not None else "NO SELECTION"
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DAQ robust finalist selection</title><style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1200px;margin:auto;padding:32px;background:#0d1117;color:#e6edf3}}a{{color:#58a6ff}}section{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:22px;margin:22px 0}}table{{border-collapse:collapse;width:100%}}th,td{{padding:9px;border-bottom:1px solid #30363d;text-align:left}}.muted{{color:#8b949e}}.pass{{color:#3fb950}}
</style></head><body><main><p>PHASE 4D · MIXED-SIGNAL DAQ</p><h1>{status}: {html.escape(str(result['selected_finalist']))}</h1>
<section><h2>What this proves</h2><p>The nominal optimizer finalists were re-run through identical deterministic manufacturing samples at every named ADC-load corner. A point counts as a joint pass only when both its AC anti-alias response and transient acquisition behavior pass.</p><p><strong>Decision:</strong> {html.escape(str(result['selection_explanation']))}</p><p class="muted">This bounded simulation is engineering qualification evidence, not a production yield guarantee.</p></section>
<section><h2>Nominal Pareto context</h2><p>The full source studies retain the complete Pareto fronts; this compact table shows only the finalists entering tolerance proof.</p><table><thead><tr><th>Finalist</th><th>Design</th><th>Worst-corner nominal objectives</th></tr></thead><tbody>{''.join(nominal_rows)}</tbody></table></section>
{''.join(cards)}
<section><h2>Portable evidence</h2><p>Detailed waveform evidence and machine-readable artifacts are retained below.</p><ul>{''.join(evidence_links)}</ul><p><a href="robust_selection_results.json">selection JSON</a> · <a href="robust_selection_results.csv">selection CSV</a> · <a href="../../robust-selection-plans/{html.escape(str(result['plan_id']))}/robust_selection_plan.json">immutable plan</a></p></section>
</main></body></html>"""
    return document.encode("utf-8")


def evaluate_robust_selection_study(
    runs_dir: Path,
    plan_id: str,
    experiments: dict[str, dict[str, str]],
) -> RobustSelectionStudyResult:
    plan = load_robust_selection_plan(runs_dir, plan_id)
    documents, evidence = _verified_documents(runs_dir, plan, experiments)
    result = build_robust_selection_result(plan_id, plan, documents, evidence)
    root = _root(runs_dir, "robust-selection-studies")
    study_id = str(result["study_id"])
    study_dir = root / study_id
    study_dir.mkdir(exist_ok=True)
    if study_dir.is_symlink() or study_dir.resolve().parent != root:
        raise ValueError("robust selection study must remain inside runs")
    results_json = study_dir / "robust_selection_results.json"
    results_csv = study_dir / "robust_selection_results.csv"
    report_html = study_dir / "report.html"
    _write_once(
        results_json,
        (_canonical_json(result, pretty=True) + "\n").encode("utf-8"),
    )
    _write_once(results_csv, _csv_document(result))
    _write_once(report_html, _html_document(result))
    return {
        "study_id": study_id,
        "plan_id": plan_id,
        "study_dir": str(study_dir),
        "results_json": str(results_json),
        "results_csv": str(results_csv),
        "report_html": str(report_html),
        "finalist_count": int(result["finalist_count"]),
        "selected_finalist": result["selected_finalist"],
        "selection_explanation": str(result["selection_explanation"]),
    }
