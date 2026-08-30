"""Controlled local one-at-a-time studies and tornado evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import TypedDict

import artifacts
import experiment_index
import sensitivity_analysis
import statistical_engine
import statistical_results

LOCAL_PLAN_SCHEMA_VERSION = 1
TORNADO_SCHEMA_VERSION = 1
MAX_RELATIVE_STEP = Decimal("0.5")


class LocalSensitivityAnalysisResult(TypedDict):
    experiment_id: str
    tornado_json: str
    tornado_csv: str
    requirement_count: int
    variable_count: int
    complete_effects: int
    invalid_points: int


class PreparedLocalSensitivityStudy(TypedDict):
    parameter_order: list[str]
    parameter_units: dict[str, str]
    points: list[dict[str, str]]
    source: dict[str, object]
    netlist_template: str
    waveform_analyses: list[dict[str, object]]
    filename: str
    ascii_raw: bool
    timeout_seconds: int


_canonical_json = artifacts.canonical_json


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
        raise ValueError("generated OAT value is too long")
    return encoded


def _source_statistical_plan(
    runs_dir: Path,
    manifest: dict[str, object],
) -> tuple[
    statistical_engine.StatisticalPlan,
    statistical_results.SamplingProvenance,
]:
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "statistical":
        raise ValueError("local sensitivity requires a statistical source study")
    provenance = statistical_results._sampling_provenance(source)
    plan = statistical_engine.load_statistical_plan(runs_dir, provenance["plan_id"])
    plan_path = runs_dir / provenance["runs_relative_path"]
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != provenance["plan_sha256"]:
        raise ValueError("statistical plan does not match source provenance")
    if (
        plan["generator_version"] != provenance["generator_version"]
        or plan["definition_hash"] != provenance["definition_hash"]
    ):
        raise ValueError("statistical plan metadata does not match source provenance")
    return plan, provenance


def _save_local_plan(runs_dir: Path, plan: dict[str, object]) -> tuple[str, Path, str]:
    artifact = (_canonical_json(plan, pretty=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(artifact).hexdigest()
    plan_id = f"local-sensitivity-plan-{digest[:16]}"
    root = runs_dir.resolve() / "local-sensitivity-plans"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("local sensitivity plan root is not a real directory")
    root.mkdir(parents=True, exist_ok=True)
    plan_dir = root / plan_id
    try:
        plan_dir.mkdir()
    except FileExistsError:
        if plan_dir.is_symlink() or not plan_dir.is_dir():
            raise ValueError("local sensitivity plan output is not a real directory")
    if plan_dir.resolve().parent != root or plan_dir.resolve().name != plan_id:
        raise ValueError("local sensitivity plan must remain inside runs")
    plan_path = plan_dir / "local_sensitivity_plan.json"
    if plan_path.exists() or plan_path.is_symlink():
        if plan_path.is_symlink() or not plan_path.is_file():
            raise ValueError("local sensitivity plan is not a regular file")
        if plan_path.read_bytes() != artifact:
            raise ValueError("local sensitivity plan content address does not match")
    else:
        temporary = plan_dir / f".local_sensitivity_plan.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(artifact)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, plan_path)
        finally:
            temporary.unlink(missing_ok=True)
    return plan_id, plan_path, digest


def prepare_local_sensitivity_study(
    runs_dir: Path,
    source_experiment_id: str,
    source_point_index: int,
    relative_step: float = 0.01,
) -> PreparedLocalSensitivityStudy:
    """Freeze baseline plus low/high OAT points from one statistical sample."""
    if (
        not isinstance(source_point_index, int)
        or isinstance(source_point_index, bool)
        or source_point_index < 0
    ):
        raise ValueError("source_point_index must be a non-negative integer")
    step = _decimal(relative_step, "relative_step")
    if step <= 0 or step > MAX_RELATIVE_STEP:
        raise ValueError("relative_step must be greater than 0 and at most 0.5")
    _, manifest, results, _ = experiment_index.load_completed_experiment(
        runs_dir, source_experiment_id
    )
    plan, provenance = _source_statistical_plan(runs_dir, manifest)
    plan_points = plan["points"]
    if source_point_index >= len(plan_points):
        raise ValueError("source_point_index is outside the statistical plan")
    result_points = results["points"]
    assert isinstance(result_points, list)
    result_by_index = {int(point["index"]): point for point in result_points}
    baseline_result = result_by_index.get(source_point_index)
    if baseline_result is None or statistical_results._classification(
        baseline_result
    ) not in {"electrical_pass", "electrical_failure"}:
        raise ValueError("source point must have completed electrical evidence")
    baseline_plan = plan_points[source_point_index]
    baseline_parameters = dict(baseline_plan["parameters"])
    if dict(baseline_result["parameters"]) != baseline_parameters:
        raise ValueError("source result parameters do not match its statistical plan")

    definition = plan["definition"]
    variables = definition.get("variables")
    if not isinstance(variables, list):
        raise ValueError("statistical source variables are missing")
    points = [dict(baseline_parameters)]
    point_metadata: list[dict[str, object]] = [
        {
            "index": 0,
            "role": "baseline",
            "variable": None,
            "direction": None,
        }
    ]
    perturbations: list[dict[str, object]] = []
    skipped_variables: list[dict[str, str]] = []
    units = plan["parameter_units"]
    for variable in variables:
        assert isinstance(variable, dict)
        name = str(variable["name"])
        try:
            baseline = _decimal(baseline_parameters[name], f"baseline {name}")
        except ValueError:
            skipped_variables.append({"name": name, "reason": "non_numeric"})
            continue
        if baseline.is_zero():
            skipped_variables.append({"name": name, "reason": "zero_baseline"})
            continue
        delta = abs(baseline) * step
        low = baseline - delta
        high = baseline + delta
        low_parameters = dict(baseline_parameters)
        high_parameters = dict(baseline_parameters)
        low_parameters[name] = _canonical_decimal(low)
        high_parameters[name] = _canonical_decimal(high)
        low_index = len(points)
        points.append(low_parameters)
        high_index = len(points)
        points.append(high_parameters)
        perturbations.append(
            {
                "name": name,
                "unit": str(units.get(name, "")),
                "baseline": _canonical_decimal(baseline),
                "low": low_parameters[name],
                "high": high_parameters[name],
                "delta": _canonical_decimal(delta),
                "low_point_index": low_index,
                "high_point_index": high_index,
            }
        )
        point_metadata.extend(
            [
                {
                    "index": low_index,
                    "role": "perturbation",
                    "variable": name,
                    "direction": "low",
                },
                {
                    "index": high_index,
                    "role": "perturbation",
                    "variable": name,
                    "direction": "high",
                },
            ]
        )
    if not perturbations:
        raise ValueError("source point has no nonzero numeric variables to perturb")

    source_corners = dict(baseline_plan.get("corners", {}))
    source_sample_index = baseline_plan.get("sample_index")
    local_plan: dict[str, object] = {
        "schema_version": LOCAL_PLAN_SCHEMA_VERSION,
        "source_experiment_id": source_experiment_id,
        "source_point_index": source_point_index,
        "source_sample_index": source_sample_index,
        "source_corners": source_corners,
        "source_sampling_provenance": provenance,
        "relative_step": _canonical_decimal(step),
        "parameter_order": list(plan["parameter_order"]),
        "parameter_units": dict(units),
        "baseline_parameters": baseline_parameters,
        "perturbations": perturbations,
        "skipped_variables": skipped_variables,
        "points": points,
        "point_metadata": point_metadata,
    }
    plan_id, plan_path, plan_sha256 = _save_local_plan(runs_dir, local_plan)
    runs_root = runs_dir.resolve()
    relative_path = plan_path.resolve().relative_to(runs_root).as_posix()
    source: dict[str, object] = {
        "kind": "local_sensitivity",
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "runs_relative_path": relative_path,
        "source_experiment_id": source_experiment_id,
        "source_point_index": source_point_index,
        "source_sample_index": source_sample_index,
        "source_corners": source_corners,
        "relative_step": _canonical_decimal(step),
        "perturbations": perturbations,
        "skipped_variables": skipped_variables,
        "point_metadata": point_metadata,
    }
    source_definition = manifest["definition"]
    assert isinstance(source_definition, dict)
    analyses = source_definition.get("waveform_analyses", [])
    assert isinstance(analyses, list)
    return {
        "parameter_order": list(plan["parameter_order"]),
        "parameter_units": dict(units),
        "points": points,
        "source": source,
        "netlist_template": str(source_definition["netlist_template"]),
        "waveform_analyses": analyses,
        "filename": str(source_definition["filename"]),
        "ascii_raw": bool(source_definition["ascii_raw"]),
        "timeout_seconds": int(source_definition["timeout_seconds"]),
    }


def _load_local_plan(
    runs_dir: Path, source: dict[str, object]
) -> dict[str, object]:
    plan_id = source.get("plan_id")
    digest = source.get("plan_sha256")
    relative_path = source.get("runs_relative_path")
    if (
        not isinstance(plan_id, str)
        or re.fullmatch(r"local-sensitivity-plan-[0-9a-f]{16}", plan_id) is None
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or plan_id != f"local-sensitivity-plan-{digest[:16]}"
        or relative_path
        != f"local-sensitivity-plans/{plan_id}/local_sensitivity_plan.json"
    ):
        raise ValueError("local sensitivity plan provenance is invalid")
    root = runs_dir.resolve()
    path = root / str(relative_path)
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(root)
    ):
        raise ValueError("local sensitivity plan artifact is invalid")
    artifact = path.read_bytes()
    if hashlib.sha256(artifact).hexdigest() != digest:
        raise ValueError("local sensitivity plan content address does not match")
    try:
        plan = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("local sensitivity plan artifact is invalid") from exc
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != LOCAL_PLAN_SCHEMA_VERSION
    ):
        raise ValueError("local sensitivity plan schema is unsupported")
    for name in (
        "source_experiment_id",
        "source_point_index",
        "relative_step",
        "perturbations",
        "skipped_variables",
        "point_metadata",
    ):
        if source.get(name) != plan.get(name):
            raise ValueError(
                "local sensitivity manifest provenance does not match plan"
            )
    return plan


def build_tornado_analysis(
    results: dict[str, object],
    plan: dict[str, object],
) -> dict[str, object]:
    """Calculate baseline-relative low/high effects for each requirement."""
    points = results["points"]
    assert isinstance(points, list)
    planned_points = int(results["point_count"])
    plan_points = plan.get("points")
    metadata = plan.get("point_metadata")
    perturbations = plan.get("perturbations")
    if (
        not isinstance(plan_points, list)
        or len(plan_points) != planned_points
        or not isinstance(metadata, list)
        or len(metadata) != planned_points
        or not isinstance(perturbations, list)
    ):
        raise ValueError("local sensitivity plan cardinality is invalid")
    for index, parameters in enumerate(plan_points):
        if not isinstance(parameters, dict):
            raise ValueError("local sensitivity plan points are invalid")
        if metadata[index].get("index") != index:
            raise ValueError("local sensitivity point metadata is invalid")
    for point in points:
        index = int(point["index"])
        if dict(point["parameters"]) != dict(plan_points[index]):
            raise ValueError("local sensitivity result does not match its plan")

    observations, requirement_metadata = sensitivity_analysis._requirement_observations(
        results
    )
    ordered_requirements = sorted(
        requirement_metadata.values(),
        key=lambda requirement: (
            requirement["analysis"],
            requirement["requirement_index"],
            requirement["check_id"],
        ),
    )
    requirements: list[dict[str, object]] = []
    complete_effects = 0
    for requirement in ordered_requirements:
        check_id = str(requirement["check_id"])
        margins = observations[check_id]
        if 0 not in margins:
            raise ValueError("local sensitivity baseline has no electrical evidence")
        baseline_margin = margins[0]
        effects: list[dict[str, object]] = []
        for perturbation in perturbations:
            if not isinstance(perturbation, dict):
                raise ValueError("local sensitivity perturbations are invalid")
            low_index = int(perturbation["low_point_index"])
            high_index = int(perturbation["high_point_index"])
            low_margin = margins.get(low_index)
            high_margin = margins.get(high_index)
            complete = low_margin is not None and high_margin is not None
            low_effect = None if low_margin is None else low_margin - baseline_margin
            high_effect = None if high_margin is None else high_margin - baseline_margin
            input_low = float(perturbation["low"])
            input_high = float(perturbation["high"])
            input_baseline = float(perturbation["baseline"])
            if complete:
                complete_effects += 1
            effects.append(
                {
                    **perturbation,
                    "status": "complete" if complete else "incomplete",
                    "baseline_margin": baseline_margin,
                    "low_margin": low_margin,
                    "high_margin": high_margin,
                    "low_effect": low_effect,
                    "high_effect": high_effect,
                    "low_slope": (
                        None
                        if low_effect is None
                        else low_effect / (input_low - input_baseline)
                    ),
                    "high_slope": (
                        None
                        if high_effect is None
                        else high_effect / (input_high - input_baseline)
                    ),
                    "impact": (
                        None
                        if not complete
                        else max(abs(float(low_effect)), abs(float(high_effect)))
                    ),
                    "baseline_evidence_path": "point-0000/",
                    "low_evidence_path": f"point-{low_index:04d}/",
                    "high_evidence_path": f"point-{high_index:04d}/",
                }
            )
        ranked = sorted(
            (effect for effect in effects if effect["impact"] is not None),
            key=lambda effect: (-float(effect["impact"]), str(effect["name"])),
        )
        rank = 0
        previous: float | None = None
        for effect in ranked:
            impact = float(effect["impact"])
            if previous is None or impact != previous:
                rank += 1
                previous = impact
            effect["rank"] = rank
        for effect in effects:
            effect.setdefault("rank", None)
        effects.sort(
            key=lambda effect: (
                effect["rank"] is None,
                0 if effect["rank"] is None else effect["rank"],
                str(effect["name"]),
            )
        )
        requirements.append(
            {
                **requirement,
                "baseline_margin": baseline_margin,
                "effects": effects,
            }
        )
    invalid_points = sum(
        statistical_results._classification(point)
        not in {"electrical_pass", "electrical_failure"}
        for point in points
    ) + planned_points - len(points)
    return {
        "schema_version": TORNADO_SCHEMA_VERSION,
        "experiment_id": results["experiment_id"],
        "method": "central_relative_one_at_a_time",
        "response": "signed_requirement_margin",
        "source_experiment_id": plan["source_experiment_id"],
        "source_point_index": plan["source_point_index"],
        "source_sample_index": plan["source_sample_index"],
        "source_corners": plan["source_corners"],
        "source_sampling_provenance": plan["source_sampling_provenance"],
        "relative_step": plan["relative_step"],
        "baseline_parameters": plan["baseline_parameters"],
        "skipped_variables": plan["skipped_variables"],
        "invalid_points": invalid_points,
        "requirement_count": len(requirements),
        "variable_count": len(perturbations),
        "complete_effects": complete_effects,
        "requirements": requirements,
    }


def _csv_document(analysis: dict[str, object]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "check_id", "analysis", "requirement_index", "metric", "operator",
        "target", "requirement_unit", "requirement_parameters", "variable",
        "variable_unit", "rank", "status", "baseline_value", "low_value",
        "high_value", "delta", "baseline_margin", "low_margin", "high_margin",
        "low_effect", "high_effect", "low_slope", "high_slope", "impact",
        "baseline_evidence_path", "low_evidence_path", "high_evidence_path",
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
            "requirement_parameters": _canonical_json(requirement["parameters"]),
        }
        for effect in requirement["effects"]:
            writer.writerow(
                {
                    **common,
                    "variable": effect["name"],
                    "variable_unit": effect["unit"],
                    "rank": effect["rank"],
                    "status": effect["status"],
                    "baseline_value": effect["baseline"],
                    "low_value": effect["low"],
                    "high_value": effect["high"],
                    **{
                        field: effect[field]
                        for field in (
                            "delta", "baseline_margin", "low_margin", "high_margin",
                            "low_effect", "high_effect", "low_slope", "high_slope",
                            "impact", "baseline_evidence_path", "low_evidence_path",
                            "high_evidence_path",
                        )
                    },
                }
            )
    return output.getvalue()


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"tornado artifact must not be a symlink: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def analyze_local_sensitivity(
    runs_dir: Path, experiment_id: str
) -> LocalSensitivityAnalysisResult:
    experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
        runs_dir, experiment_id
    )
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "local_sensitivity":
        raise ValueError(f"Experiment {experiment_id} is not a local sensitivity study")
    plan = _load_local_plan(runs_dir, source)
    if point_plan.get("points") != plan.get("points"):
        raise ValueError("local sensitivity experiment points do not match its plan")
    analysis = build_tornado_analysis(results, plan)
    analysis["plan_id"] = source["plan_id"]
    analysis["plan_sha256"] = source["plan_sha256"]
    analysis["runs_relative_path"] = source["runs_relative_path"]
    json_path = experiment_dir / "tornado.json"
    csv_path = experiment_dir / "tornado.csv"
    _write_atomic(
        json_path,
        json.dumps(analysis, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_atomic(csv_path, _csv_document(analysis))
    return {
        "experiment_id": experiment_id,
        "tornado_json": str(json_path),
        "tornado_csv": str(csv_path),
        "requirement_count": int(analysis["requirement_count"]),
        "variable_count": int(analysis["variable_count"]),
        "complete_effects": int(analysis["complete_effects"]),
        "invalid_points": int(analysis["invalid_points"]),
    }
