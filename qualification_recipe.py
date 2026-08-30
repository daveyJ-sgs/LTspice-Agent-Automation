"""Pure preview and immutable publication for selected-design qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import optimization_engine
import robust_selection
import statistical_engine
from examples.mixed_signal_daq_study import CORNERS
from examples.optimize_mixed_signal_daq import FIXED_PARAMETERS
from examples.qualify_mixed_signal_daq_finalists import CORRELATIONS, MODELS


DEFAULT_SAMPLE_COUNT = 32
DEFAULT_SEED = 20260827
FINALIST_LABEL = "selected-design"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _candidate(runs_dir: Path, study_id: str, candidate_index: int) -> dict[str, object]:
    result, _ = optimization_engine._load_verified_optimization_study(runs_dir, study_id)
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
        raise ValueError("qualification source must be a feasible selected or Pareto candidate")
    return candidate


def qualification_variables(runs_dir: Path, study_id: str, candidate_index: int) -> list[statistical_engine.StatisticalVariable]:
    candidate = _candidate(runs_dir, study_id, candidate_index)
    nominal = {**{name: float(value) for name, value in FIXED_PARAMETERS.items()}, **{name: float(value) for name, value in candidate["parameters"].items()}}
    return [
        {
            "name": name, "distribution": "gaussian", "nominal": nominal[name],
            "sigma": nominal[name] * model[0], "minimum": nominal[name] * model[1],
            "maximum": nominal[name] * model[2], "unit": model[3],
        }
        for name, model in MODELS.items()
    ]


def preview_qualification(
    runs_dir: Path,
    study_id: str,
    candidate_index: int,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    """Resolve the statistical workload without writing a plan or launching LTspice."""
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or not 2 <= sample_count <= 4096:
        raise ValueError("sample_count must be an integer from 2 to 4096")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    candidate = _candidate(runs_dir, study_id, candidate_index)
    variables = qualification_variables(runs_dir, study_id, candidate_index)
    plan = statistical_engine.build_statistical_plan(
        variables, sample_count, seed, CORRELATIONS, CORNERS, False,
        sampling_method="halton",
    )
    artifact = statistical_engine._artifact_bytes(plan)
    digest = hashlib.sha256(artifact).hexdigest()
    statistical_plan_id = f"statistical-plan-{digest[:16]}"
    identity = {
        "source_study_id": study_id, "source_candidate_index": candidate_index,
        "source_parameters": candidate["parameters"], "statistical_plan_id": statistical_plan_id,
        "sample_count": sample_count, "seed": seed, "correlations": CORRELATIONS,
        "corner_axes": CORNERS, "sampling_method": "halton",
    }
    qualification_id = f"qualification-preview-{hashlib.sha256(_canonical(identity).encode()).hexdigest()[:16]}"
    return {
        "valid": True, "qualification_id": qualification_id,
        "source": {"study_id": study_id, "candidate_index": candidate_index, "parameters": candidate["parameters"]},
        "plan": {
            "statistical_plan_id": statistical_plan_id, "statistical_plan_sha256": digest,
            "sample_count": sample_count, "corner_count": len(CORNERS[0]["values"]),
            "point_count": len(plan["points"]), "seed": seed, "sampling_method": "halton",
            "variable_count": len(variables), "variables": variables,
            "correlations": CORRELATIONS, "corner_axes": CORNERS,
        },
        "execution": {"experiment_count": 2, "experiments": ["ac", "transient"], "total_run_count": len(plan["points"]) * 2},
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
) -> tuple[dict[str, object], robust_selection.RobustSelectionPlanResult]:
    preview = preview_qualification(runs_dir, study_id, candidate_index, sample_count, seed)
    if preview["qualification_id"] != expected_qualification_id:
        raise ValueError("qualification definition changed after preview")
    plan_preview = preview["plan"]
    execution = preview["execution"]
    assert isinstance(plan_preview, dict) and isinstance(execution, dict)
    if plan_preview["statistical_plan_id"] != expected_statistical_plan_id:
        raise ValueError("statistical plan changed after preview")
    if execution["total_run_count"] != expected_total_run_count:
        raise ValueError("qualification run count changed after preview")
    variables = qualification_variables(runs_dir, study_id, candidate_index)
    published = robust_selection.generate_robust_selection_plan(
        runs_dir,
        [{"label": FINALIST_LABEL, "study_id": study_id, "candidate_index": candidate_index}],
        {FINALIST_LABEL: variables}, sample_count, seed,
        correlations=CORRELATIONS, corner_axes=CORNERS, sampling_method="halton",
    )
    if published["statistical_plan_ids"][FINALIST_LABEL] != expected_statistical_plan_id:
        raise ValueError("published statistical plan does not match preview")
    return preview, published
