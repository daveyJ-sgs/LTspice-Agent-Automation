"""Pure preview and immutable publication for selected-design qualification."""

from __future__ import annotations

import hashlib
from pathlib import Path

import artifacts
import optimization_engine
import robust_selection
import statistical_engine

DEFAULT_SAMPLE_COUNT = 32
DEFAULT_SEED = 20260827
FINALIST_LABEL = "selected-design"


def _candidate(
    runs_dir: Path, study_id: str, candidate_index: int
) -> dict[str, object]:
    result, _ = optimization_engine._load_verified_optimization_study(
        runs_dir, study_id
    )
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not 0 <= candidate_index < len(candidates):
        raise ValueError("selected optimization candidate does not exist")
    candidate = candidates[candidate_index]
    if (
        not isinstance(candidate, dict)
        or candidate.get("status") != "feasible"
        or not (candidate.get("selected") is True or candidate.get("pareto") is True)
        or not isinstance(candidate.get("parameters"), dict)
    ):
        raise ValueError(
            "qualification source must be a feasible selected or Pareto candidate"
        )
    return candidate


def _resolve(candidate: dict[str, object], qualification: object):
    if not isinstance(qualification, dict):
        raise ValueError("qualification must be an explicit object")
    configs = qualification.get("variables")
    if not isinstance(configs, list) or not configs:
        raise ValueError("qualification.variables must be a non-empty list")
    fixed = qualification.get("fixed_parameters", {})
    if not isinstance(fixed, dict):
        raise ValueError("qualification.fixed_parameters must be an object")
    params = candidate["parameters"]
    assert isinstance(params, dict)
    nominal = {}
    for name, value in fixed.items():
        if not isinstance(name, str):
            raise ValueError("qualification.fixed_parameters names must be strings")
        try:
            nominal[name] = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"qualification.fixed_parameters.{name} must be numeric"
            ) from exc
    for name, value in params.items():
        if not isinstance(name, str):
            raise ValueError("candidate parameter names must be strings")
        try:
            nominal[name] = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candidate parameter {name} must be numeric") from exc
    definitions = {}
    required = {"name", "sigma_fraction", "minimum_factor", "maximum_factor", "unit"}
    for index, item in enumerate(configs):
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError(
                f"qualification.variables[{index}] requires name, sigma_fraction, minimum_factor, maximum_factor, and unit"
            )
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise ValueError(f"qualification.variables[{index}].name must be a string")
        if name in definitions:
            raise ValueError(f"duplicate qualification variable {name}")
        if name not in nominal:
            raise ValueError(f"qualification model {name} has no nominal parameter")
        if not isinstance(item["unit"], str) or not item["unit"]:
            raise ValueError(f"qualification.variables[{index}].unit must be a string")
        try:
            definition = {
                key: float(item[key])
                for key in ("sigma_fraction", "minimum_factor", "maximum_factor")
            }
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"qualification variable {name} factors must be numeric"
            ) from exc
        definition.update(name=name, unit=item["unit"])
        definitions[name] = definition
    unmapped = sorted(set(params) - set(definitions))
    if unmapped:
        raise ValueError(
            "qualification model is missing candidate parameter(s): "
            + ", ".join(unmapped)
        )
    variables = [
        {
            "name": name,
            "distribution": "gaussian",
            "nominal": nominal[name],
            "sigma": nominal[name] * item["sigma_fraction"],
            "minimum": nominal[name] * item["minimum_factor"],
            "maximum": nominal[name] * item["maximum_factor"],
            "unit": item["unit"],
        }
        for name, item in definitions.items()
    ]
    correlations = qualification.get("correlations", [])
    corners = qualification.get("corner_axes", [])
    if not isinstance(correlations, list) or not isinstance(corners, list):
        raise ValueError("qualification correlations and corner_axes must be lists")
    resolved = {
        "variables": list(definitions.values()),
        "fixed_parameters": fixed,
        "correlations": correlations,
        "corner_axes": corners,
    }
    return variables, correlations, corners, resolved


def qualification_variables(
    runs_dir: Path, study_id: str, candidate_index: int, model: object | None = None
):
    return _resolve(_candidate(runs_dir, study_id, candidate_index), model)[0]


def preview_qualification(
    runs_dir: Path,
    study_id: str,
    candidate_index: int,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    seed: int = DEFAULT_SEED,
    model: object | None = None,
):
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or not 2 <= sample_count <= 4096
    ):
        raise ValueError("sample_count must be an integer from 2 to 4096")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    candidate = _candidate(runs_dir, study_id, candidate_index)
    variables, correlations, corners, resolved = _resolve(candidate, model)
    plan = statistical_engine.build_statistical_plan(
        variables,
        sample_count,
        seed,
        correlations,
        corners,
        False,
        sampling_method="halton",
    )
    digest = hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest()
    statistical_plan_id = f"statistical-plan-{digest[:16]}"
    identity = {
        "source_study_id": study_id,
        "source_candidate_index": candidate_index,
        "source_parameters": candidate["parameters"],
        "qualification": resolved,
        "statistical_plan_id": statistical_plan_id,
        "sample_count": sample_count,
        "seed": seed,
        "correlations": correlations,
        "corner_axes": corners,
        "sampling_method": "halton",
    }
    qualification_id, _ = artifacts.content_address(
        "qualification-preview", artifacts.canonical_bytes(identity)
    )
    points = plan["points"]
    return {
        "valid": True,
        "qualification_id": qualification_id,
        "source": {
            "study_id": study_id,
            "candidate_index": candidate_index,
            "parameters": candidate["parameters"],
        },
        "plan": {
            "statistical_plan_id": statistical_plan_id,
            "statistical_plan_sha256": digest,
            "sample_count": sample_count,
            "corner_count": len(points) // sample_count,
            "point_count": len(points),
            "seed": seed,
            "sampling_method": "halton",
            "variable_count": len(variables),
            "variables": variables,
            "correlations": correlations,
            "corner_axes": corners,
        },
        "execution": {
            "experiment_count": 2,
            "experiments": ["ac", "transient"],
            "total_run_count": len(points) * 2,
        },
    }


def publish_qualification(
    runs_dir: Path,
    study_id: str,
    candidate_index: int,
    sample_count: int,
    seed: int,
    expected_qualification_id: str,
    expected_statistical_plan_id: str,
    expected_total_run_count: int,
    model: object | None = None,
):
    preview = preview_qualification(
        runs_dir, study_id, candidate_index, sample_count, seed, model
    )
    if preview["qualification_id"] != expected_qualification_id:
        raise ValueError("qualification definition changed after preview")
    plan, execution = preview["plan"], preview["execution"]
    assert isinstance(plan, dict) and isinstance(execution, dict)
    if plan["statistical_plan_id"] != expected_statistical_plan_id:
        raise ValueError("statistical plan changed after preview")
    if execution["total_run_count"] != expected_total_run_count:
        raise ValueError("qualification run count changed after preview")
    variables, correlations, corners, _ = _resolve(
        _candidate(runs_dir, study_id, candidate_index), model
    )
    published = robust_selection.generate_robust_selection_plan(
        runs_dir,
        [
            {
                "label": FINALIST_LABEL,
                "study_id": study_id,
                "candidate_index": candidate_index,
            }
        ],
        {FINALIST_LABEL: variables},
        sample_count,
        seed,
        correlations=correlations,
        corner_axes=corners,
        sampling_method="halton",
    )
    if (
        published["statistical_plan_ids"][FINALIST_LABEL]
        != expected_statistical_plan_id
    ):
        raise ValueError("published statistical plan does not match preview")
    return preview, published
