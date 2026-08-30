"""Cross-platform comparison evidence for completed optimization studies."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from pathlib import Path
from typing import TypedDict

import artifacts
import optimization_engine

COMPARISON_SCHEMA_VERSION = 1
COMPARISON_GENERATOR_VERSION = "optimization-platform-comparison-v1"


class ObjectiveTolerance(TypedDict):
    absolute: float
    relative: float


class OptimizationComparisonResult(TypedDict):
    comparison_id: str
    comparison_dir: str
    comparison_json: str
    report_html: str
    passed: bool
    plan_id: str
    candidate_count: int
    exact_mismatches: int
    objective_mismatches: int
    selected_candidate_index: int | None


_canonical_json = artifacts.canonical_json


def _document(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} result must be an object")
    if (
        value.get("schema_version") != optimization_engine.OPTIMIZATION_RESULT_SCHEMA_VERSION
        or value.get("generator_version")
        != optimization_engine.OPTIMIZATION_RESULT_GENERATOR_VERSION
        or not isinstance(value.get("plan_id"), str)
        or not isinstance(value.get("candidates"), list)
        or type(value.get("candidate_count")) is not int
    ):
        raise ValueError(f"{label} optimization result is invalid")
    if len(value["candidates"]) != value["candidate_count"]:  # type: ignore[arg-type]
        raise ValueError(f"{label} candidate count does not match")
    return value


def _tolerances(
    value: object, objective_names: set[str]
) -> dict[str, ObjectiveTolerance]:
    if not isinstance(value, dict) or set(value) != objective_names:
        raise ValueError("tolerances must exactly match the objective names")
    normalized: dict[str, ObjectiveTolerance] = {}
    for name in sorted(value):
        item = value[name]
        if not isinstance(item, dict) or set(item) != {"absolute", "relative"}:
            raise ValueError(f"tolerance {name} must contain absolute and relative")
        absolute = item["absolute"]
        relative = item["relative"]
        if (
            isinstance(absolute, bool)
            or not isinstance(absolute, (int, float))
            or isinstance(relative, bool)
            or not isinstance(relative, (int, float))
            or not math.isfinite(float(absolute))
            or not math.isfinite(float(relative))
            or float(absolute) < 0
            or float(relative) < 0
        ):
            raise ValueError(f"tolerance {name} values must be finite and nonnegative")
        normalized[name] = {
            "absolute": float(absolute),
            "relative": float(relative),
        }
    return normalized


def compare_optimization_documents(
    baseline: object,
    candidate: object,
    tolerances: object,
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> dict[str, object]:
    """Compare platform results with exact decisions and bounded numeric deltas."""
    left = _document(baseline, baseline_label)
    right = _document(candidate, candidate_label)
    if left["plan_id"] != right["plan_id"]:
        raise ValueError("optimization results use different plans")
    if left["candidate_count"] != right["candidate_count"]:
        raise ValueError("optimization results use different candidate counts")
    left_candidates = left["candidates"]
    right_candidates = right["candidates"]
    assert isinstance(left_candidates, list) and isinstance(right_candidates, list)
    objective_names: set[str] | None = None
    for record in left_candidates:
        if not isinstance(record, dict) or not isinstance(record.get("objectives"), dict):
            raise ValueError(f"{baseline_label} candidate evidence is invalid")
        names = set(record["objectives"])
        objective_names = names if objective_names is None else objective_names
        if names != objective_names:
            raise ValueError(f"{baseline_label} objective sets are inconsistent")
    normalized_tolerances = _tolerances(tolerances, objective_names or set())

    comparisons: list[dict[str, object]] = []
    exact_mismatches = 0
    objective_mismatches = 0
    for index, (left_record, right_record) in enumerate(
        zip(left_candidates, right_candidates)
    ):
        if not isinstance(left_record, dict) or not isinstance(right_record, dict):
            raise ValueError("candidate evidence must contain objects")
        if left_record.get("candidate_index") != index or right_record.get(
            "candidate_index"
        ) != index:
            raise ValueError("candidate indexes are not contiguous")
        exact = {
            "parameters": left_record.get("parameters") == right_record.get("parameters"),
            "status": left_record.get("status") == right_record.get("status"),
            "pareto": left_record.get("pareto") == right_record.get("pareto"),
            "selected": left_record.get("selected") == right_record.get("selected"),
        }
        exact_mismatches += sum(not matched for matched in exact.values())
        left_objectives = left_record.get("objectives")
        right_objectives = right_record.get("objectives")
        if not isinstance(left_objectives, dict) or not isinstance(right_objectives, dict):
            raise ValueError("candidate objectives must be objects")
        if set(right_objectives) != set(left_objectives):
            raise ValueError("candidate objective sets do not match")
        objective_deltas: dict[str, dict[str, object]] = {}
        for name in sorted(left_objectives):
            left_value = left_objectives[name]
            right_value = right_objectives[name]
            if not isinstance(left_value, dict) or not isinstance(right_value, dict):
                raise ValueError("objective records must be objects")
            a = left_value.get("value")
            b = right_value.get("value")
            if (
                isinstance(a, bool)
                or not isinstance(a, (int, float))
                or isinstance(b, bool)
                or not isinstance(b, (int, float))
                or not math.isfinite(float(a))
                or not math.isfinite(float(b))
            ):
                raise ValueError("objective values must be finite numbers")
            tolerance = normalized_tolerances[name]
            allowed = tolerance["absolute"] + tolerance["relative"] * max(
                abs(float(a)), abs(float(b))
            )
            delta = abs(float(a) - float(b))
            matched = (
                left_value.get("unit") == right_value.get("unit") and delta <= allowed
            )
            objective_mismatches += not matched
            objective_deltas[name] = {
                "baseline": float(a),
                "candidate": float(b),
                "absolute_delta": delta,
                "allowed_delta": allowed,
                "unit": left_value.get("unit"),
                "passed": matched,
            }
        comparisons.append(
            {
                "candidate_index": index,
                "exact": exact,
                "objectives": objective_deltas,
                "passed": all(exact.values())
                and all(item["passed"] for item in objective_deltas.values()),
            }
        )

    selection_matches = (
        left.get("selected_candidate_index") == right.get("selected_candidate_index")
    )
    if not selection_matches:
        exact_mismatches += 1
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "generator_version": COMPARISON_GENERATOR_VERSION,
        "plan_id": left["plan_id"],
        "baseline": {
            "label": baseline_label,
            "study_id": left.get("study_id"),
            "results_sha256": hashlib.sha256(
                _canonical_json(left).encode("utf-8")
            ).hexdigest(),
        },
        "candidate": {
            "label": candidate_label,
            "study_id": right.get("study_id"),
            "results_sha256": hashlib.sha256(
                _canonical_json(right).encode("utf-8")
            ).hexdigest(),
        },
        "tolerances": normalized_tolerances,
        "candidate_count": left["candidate_count"],
        "selected_candidate_index": left.get("selected_candidate_index"),
        "selection_matches": selection_matches,
        "exact_mismatches": exact_mismatches,
        "objective_mismatches": objective_mismatches,
        "passed": exact_mismatches == 0 and objective_mismatches == 0,
        "candidates": comparisons,
    }


def _report(document: dict[str, object]) -> str:
    rows: list[str] = []
    for candidate in document["candidates"]:  # type: ignore[union-attr]
        assert isinstance(candidate, dict)
        objectives = candidate["objectives"]
        assert isinstance(objectives, dict)
        delta_text = ", ".join(
            f"{html.escape(name)}: {item['absolute_delta']:.4g} / {item['allowed_delta']:.4g}"
            for name, item in objectives.items()
        )
        rows.append(
            "<tr>"
            f"<td>{candidate['candidate_index']}</td>"
            f"<td>{'PASS' if candidate['passed'] else 'FAIL'}</td>"
            f"<td>{html.escape(delta_text)}</td>"
            "</tr>"
        )
    status = "PASS" if document["passed"] else "FAIL"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optimization platform comparison</title><style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:auto;padding:32px;background:#0d1117;color:#e6edf3}}a{{color:#58a6ff}}section{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;margin:20px 0}}table{{border-collapse:collapse;width:100%}}th,td{{padding:9px;border-bottom:1px solid #30363d;text-align:left}}.pass{{color:#3fb950}}.fail{{color:#f85149}}
</style></head><body><main><p>LTspice optimization portability</p>
<h1 class="{status.lower()}">{status}: {html.escape(str(document['baseline']['label']))} vs {html.escape(str(document['candidate']['label']))}</h1>
<p>Plan <code>{html.escape(str(document['plan_id']))}</code>; selected candidate {document['selected_candidate_index']}.</p>
<section><h2>Acceptance contract</h2><p>Candidate parameters, classifications, Pareto membership, and selection must match exactly. Objective values must remain within the recorded absolute plus relative tolerance.</p></section>
<section><h2>Candidate comparison</h2><table><thead><tr><th>Candidate</th><th>Status</th><th>Objective delta / allowance</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section><h2>Evidence</h2><p><a href="optimization_comparison.json">comparison JSON</a></p></section>
</main></body></html>"""


def write_optimization_comparison(
    output_root: Path,
    baseline: object,
    candidate: object,
    tolerances: object,
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> OptimizationComparisonResult:
    document = compare_optimization_documents(
        baseline,
        candidate,
        tolerances,
        baseline_label=baseline_label,
        candidate_label=candidate_label,
    )
    comparison_id, _ = artifacts.content_address(
        "optimization-comparison", artifacts.canonical_bytes(document)
    )
    root = optimization_engine._confined_root(output_root, "optimization-comparisons")
    comparison_dir = root / comparison_id
    comparison_dir.mkdir(exist_ok=True)
    comparison_json = comparison_dir / "optimization_comparison.json"
    report_html = comparison_dir / "report.html"
    optimization_engine._write_once(
        comparison_json,
        (_canonical_json(document, pretty=True) + "\n").encode("utf-8"),
    )
    optimization_engine._write_once(report_html, _report(document).encode("utf-8"))
    return {
        "comparison_id": comparison_id,
        "comparison_dir": str(comparison_dir),
        "comparison_json": str(comparison_json),
        "report_html": str(report_html),
        "passed": bool(document["passed"]),
        "plan_id": str(document["plan_id"]),
        "candidate_count": int(document["candidate_count"]),
        "exact_mismatches": int(document["exact_mismatches"]),
        "objective_mismatches": int(document["objective_mismatches"]),
        "selected_candidate_index": document["selected_candidate_index"],  # type: ignore[typeddict-item]
    }


def compare_saved_optimization_studies(
    runs_dir: Path,
    baseline_study_id: str,
    candidate_study_id: str,
    tolerances: object,
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> OptimizationComparisonResult:
    root = optimization_engine._confined_root(runs_dir, "optimization-studies")

    def load(study_id: str) -> dict[str, object]:
        if re.fullmatch(r"optimization-study-[0-9a-f]{16}", study_id) is None:
            raise ValueError("invalid optimization study ID")
        study_dir = root / study_id
        if study_dir.is_symlink() or not study_dir.is_dir():
            raise FileNotFoundError(f"optimization study not found: {study_id}")
        if study_dir.resolve().parent != root:
            raise ValueError("optimization study must remain inside optimization-studies")
        path = study_dir / "optimization_results.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("optimization result is not a regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("optimization result is invalid JSON") from exc
        if not isinstance(value, dict) or value.get("study_id") != study_id:
            raise ValueError("optimization result study identity does not match")
        return value

    return write_optimization_comparison(
        runs_dir,
        load(baseline_study_id),
        load(candidate_study_id),
        tolerances,
        baseline_label=baseline_label,
        candidate_label=candidate_label,
    )
