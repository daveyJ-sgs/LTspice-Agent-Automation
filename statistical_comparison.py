"""Content-addressed comparisons of compatible statistical experiments."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import uuid
from pathlib import Path
from typing import TypedDict

import experiment_index
import statistical_engine
import statistical_results

STATISTICAL_COMPARISON_SCHEMA_VERSION = 1


class StatisticalComparisonResult(TypedDict):
    comparison_id: str
    comparison_dir: str
    comparison_json: str
    comparison_csv: str
    comparison_html: str
    baseline_experiment_id: str
    candidate_experiment_id: str
    comparison_basis: str
    attribution: str
    sample_plan_changed: bool
    circuit_changed: bool
    aggregate_yield_delta: float | None
    corner_count: int
    requirement_count: int
    paired_points: int


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(manifest: dict[str, object], experiment_id: str) -> dict[str, object]:
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "statistical":
        raise ValueError(f"Experiment {experiment_id} is not a statistical study")
    statistical_results._sampling_provenance(source)
    return source


def _load_plan(
    runs_dir: Path, source: dict[str, object]
) -> dict[str, object]:
    plan_id = str(source["plan_id"])
    plan = statistical_engine.load_statistical_plan(runs_dir, plan_id)
    path = runs_dir / str(source["runs_relative_path"])
    if _sha256(path) != source["plan_sha256"]:
        raise ValueError("statistical plan artifact does not match experiment provenance")
    return plan


def _population_contract(plan: dict[str, object]) -> dict[str, object]:
    definition = plan["definition"]
    assert isinstance(definition, dict)
    return {
        "variables": definition.get("variables", []),
        "correlations": definition.get("correlations", []),
        "corner_axes": definition.get("corner_axes", []),
    }


def _electrical_contract(manifest: dict[str, object]) -> dict[str, object]:
    definition = manifest["definition"]
    assert isinstance(definition, dict)
    return {
        "parameter_order": definition.get("parameter_order"),
        "parameter_units": definition.get("parameter_units"),
        "waveform_analyses": definition.get("waveform_analyses", []),
    }


def _summary(
    results: dict[str, object], source: dict[str, object]
) -> dict[str, object]:
    return statistical_results.build_statistics(
        results,
        point_metadata=source.get("point_metadata"),
        corner_aggregate=source.get("corner_aggregate", False),
        sampling_provenance=statistical_results._sampling_provenance(source),
    )


def _delta(candidate: object, baseline: object) -> float | None:
    if candidate is None or baseline is None:
        return None
    value = float(candidate) - float(baseline)
    if not math.isfinite(value):
        raise ValueError("statistical comparison delta must be finite")
    return value


def _yield_record(summary: dict[str, object]) -> dict[str, object]:
    interval = summary["yield_confidence_interval"]
    assert isinstance(interval, dict)
    return {
        "observed_yield": summary["observed_yield"],
        "confidence_low": interval["low"],
        "confidence_high": interval["high"],
        "evaluated_points": summary["evaluated_points"],
        "invalid_points": summary["invalid_points"],
    }


def _corner_records(
    baseline: dict[str, object], candidate: dict[str, object]
) -> list[dict[str, object]]:
    def keyed(summary: dict[str, object]) -> dict[str, dict[str, object]]:
        return {
            _canonical(item["corners"]): item
            for item in summary.get("corner_results", [])
        }

    baseline_by_corner = keyed(baseline)
    candidate_by_corner = keyed(candidate)
    if set(baseline_by_corner) != set(candidate_by_corner):
        raise ValueError("compatible studies must expose the same corner results")
    records: list[dict[str, object]] = []
    for key in sorted(baseline_by_corner):
        baseline_item = baseline_by_corner[key]
        candidate_item = candidate_by_corner[key]
        baseline_yield = _yield_record(baseline_item)
        candidate_yield = _yield_record(candidate_item)
        records.append(
            {
                "corners": baseline_item["corners"],
                "baseline": baseline_yield,
                "candidate": candidate_yield,
                "yield_delta": _delta(
                    candidate_yield["observed_yield"],
                    baseline_yield["observed_yield"],
                ),
            }
        )
    return records


def _requirement_records(
    baseline: dict[str, object], candidate: dict[str, object]
) -> list[dict[str, object]]:
    fields = ("requirement_index", "analysis", "metric", "operator", "target", "unit")

    def keyed(summary: dict[str, object]) -> dict[tuple[object, ...], dict[str, object]]:
        return {
            tuple(item[field] for field in fields): item
            for item in summary["requirement_margins"]
        }

    baseline_by_requirement = keyed(baseline)
    candidate_by_requirement = keyed(candidate)
    records: list[dict[str, object]] = []
    identities = set(baseline_by_requirement) | set(candidate_by_requirement)
    for identity in sorted(identities, key=_canonical):
        baseline_item = baseline_by_requirement.get(identity)
        candidate_item = candidate_by_requirement.get(identity)
        item = baseline_item or candidate_item
        assert isinstance(item, dict)
        baseline_statistics = (
            None if baseline_item is None else baseline_item["statistics"]
        )
        candidate_statistics = (
            None if candidate_item is None else candidate_item["statistics"]
        )
        records.append(
            {
                **{field: item[field] for field in fields},
                "baseline": baseline_statistics,
                "candidate": candidate_statistics,
                "deltas": {
                    field: None
                    if baseline_statistics is None or candidate_statistics is None
                    else _delta(candidate_statistics[field], baseline_statistics[field])
                    for field in ("mean", "p05", "p50", "p95")
                },
            }
        )
    return records


def _paired_transitions(
    baseline: dict[str, object], candidate: dict[str, object]
) -> tuple[int, dict[str, int]]:
    baseline_points = sorted(baseline["points"], key=lambda item: item["index"])
    candidate_points = sorted(candidate["points"], key=lambda item: item["index"])
    if len(baseline_points) != len(candidate_points):
        raise ValueError("same-plan studies must contain the same point count")
    transitions: dict[str, int] = {}
    for baseline_point, candidate_point in zip(baseline_points, candidate_points):
        if (
            baseline_point["index"] != candidate_point["index"]
            or baseline_point["parameters"] != candidate_point["parameters"]
        ):
            raise ValueError("same-plan studies must preserve exact point parameters")
        before = statistical_results._classification(baseline_point)
        after = statistical_results._classification(candidate_point)
        key = f"{before}->{after}"
        transitions[key] = transitions.get(key, 0) + 1
    return len(baseline_points), dict(sorted(transitions.items()))


def build_statistical_comparison(
    runs_dir: Path,
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> StatisticalComparisonResult:
    """Compare compatible statistical evidence without rerunning LTspice."""
    if baseline_experiment_id == candidate_experiment_id:
        raise ValueError("statistical comparison needs two distinct experiments")
    runs_dir = runs_dir.resolve()
    baseline_dir, baseline_manifest, baseline_results, _ = (
        experiment_index.load_terminal_experiment(runs_dir, baseline_experiment_id)
    )
    candidate_dir, candidate_manifest, candidate_results, _ = (
        experiment_index.load_terminal_experiment(runs_dir, candidate_experiment_id)
    )
    baseline_source = _source(baseline_manifest, baseline_experiment_id)
    candidate_source = _source(candidate_manifest, candidate_experiment_id)
    baseline_plan = _load_plan(runs_dir, baseline_source)
    candidate_plan = _load_plan(runs_dir, candidate_source)
    if _population_contract(baseline_plan) != _population_contract(candidate_plan):
        raise ValueError("statistical population definitions are not compatible")
    if _electrical_contract(baseline_manifest) != _electrical_contract(candidate_manifest):
        raise ValueError("statistical electrical analysis contracts are not compatible")

    baseline_summary = _summary(baseline_results, baseline_source)
    candidate_summary = _summary(candidate_results, candidate_source)
    plan_changed = baseline_source["plan_sha256"] != candidate_source["plan_sha256"]
    baseline_definition = baseline_manifest["definition"]
    candidate_definition = candidate_manifest["definition"]
    assert isinstance(baseline_definition, dict)
    assert isinstance(candidate_definition, dict)
    baseline_circuit = hashlib.sha256(
        str(baseline_definition["netlist_template"]).encode("utf-8")
    ).hexdigest()
    candidate_circuit = hashlib.sha256(
        str(candidate_definition["netlist_template"]).encode("utf-8")
    ).hexdigest()
    circuit_changed = baseline_circuit != candidate_circuit
    if plan_changed and circuit_changed:
        attribution = "confounded_plan_and_circuit"
    elif plan_changed:
        attribution = "sample_plan_change"
    elif circuit_changed:
        attribution = "paired_circuit_outcomes"
    else:
        attribution = "repeat_evidence"
    comparison_basis = (
        "unpaired_population_summary" if plan_changed else "paired_same_plan"
    )
    paired_points = 0
    transitions: dict[str, int] = {}
    if not plan_changed:
        paired_points, transitions = _paired_transitions(
            baseline_results, candidate_results
        )
    baseline_yield = _yield_record(baseline_summary)
    candidate_yield = _yield_record(candidate_summary)
    corners = _corner_records(baseline_summary, candidate_summary)
    requirements = _requirement_records(baseline_summary, candidate_summary)
    identity = {
        "baseline_experiment_id": baseline_experiment_id,
        "candidate_experiment_id": candidate_experiment_id,
        "baseline_results_sha256": _sha256(baseline_dir / "results.json"),
        "candidate_results_sha256": _sha256(candidate_dir / "results.json"),
        "baseline_plan_sha256": baseline_source["plan_sha256"],
        "candidate_plan_sha256": candidate_source["plan_sha256"],
    }
    digest = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
    comparison_id = digest[:16]
    root = runs_dir / "statistical-comparisons"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("statistical comparison root is not a real directory")
    root.mkdir(exist_ok=True)
    directory = root / f"statistical-comparison-{comparison_id}"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("statistical comparison directory is invalid")
    directory.mkdir(exist_ok=True)
    document: dict[str, object] = {
        "schema_version": STATISTICAL_COMPARISON_SCHEMA_VERSION,
        "comparison_id": comparison_id,
        **identity,
        "baseline_circuit_sha256": baseline_circuit,
        "candidate_circuit_sha256": candidate_circuit,
        "sample_plan_changed": plan_changed,
        "circuit_changed": circuit_changed,
        "comparison_basis": comparison_basis,
        "attribution": attribution,
        "aggregate": {
            "baseline": baseline_yield,
            "candidate": candidate_yield,
            "yield_delta": _delta(
                candidate_yield["observed_yield"], baseline_yield["observed_yield"]
            ),
        },
        "corners": corners,
        "requirements": requirements,
        "paired_points": paired_points,
        "classification_transitions": transitions,
    }
    json_path = directory / "comparison.json"
    csv_path = directory / "comparison.csv"
    html_path = directory / "report.html"
    _write_atomic(
        json_path,
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write_atomic(csv_path, _csv_document(document))
    _write_atomic(html_path, _html_document(document))
    return {
        "comparison_id": comparison_id,
        "comparison_dir": str(directory),
        "comparison_json": str(json_path),
        "comparison_csv": str(csv_path),
        "comparison_html": str(html_path),
        "baseline_experiment_id": baseline_experiment_id,
        "candidate_experiment_id": candidate_experiment_id,
        "comparison_basis": comparison_basis,
        "attribution": attribution,
        "sample_plan_changed": plan_changed,
        "circuit_changed": circuit_changed,
        "aggregate_yield_delta": document["aggregate"]["yield_delta"],
        "corner_count": len(corners),
        "requirement_count": len(requirements),
        "paired_points": paired_points,
    }


def _csv_document(document: dict[str, object]) -> str:
    output = io.StringIO(newline="")
    fieldnames = [
        "record_type",
        "name",
        "baseline",
        "candidate",
        "delta",
        "details",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for name in (
        "comparison_basis",
        "attribution",
        "sample_plan_changed",
        "circuit_changed",
    ):
        writer.writerow({"record_type": "metadata", "name": name, "candidate": document[name]})
    aggregate = document["aggregate"]
    assert isinstance(aggregate, dict)
    writer.writerow(
        {
            "record_type": "aggregate_yield",
            "name": "observed_yield",
            "baseline": aggregate["baseline"]["observed_yield"],
            "candidate": aggregate["candidate"]["observed_yield"],
            "delta": aggregate["yield_delta"],
        }
    )
    for corner in document["corners"]:
        writer.writerow(
            {
                "record_type": "corner_yield",
                "name": _canonical(corner["corners"]),
                "baseline": corner["baseline"]["observed_yield"],
                "candidate": corner["candidate"]["observed_yield"],
                "delta": corner["yield_delta"],
            }
        )
    for requirement in document["requirements"]:
        label = f"{requirement['analysis']}:{requirement['metric']}:{requirement['requirement_index']}"
        baseline = requirement["baseline"]
        candidate = requirement["candidate"]
        writer.writerow(
            {
                "record_type": "requirement_margin",
                "name": label,
                "baseline": None if baseline is None else baseline["mean"],
                "candidate": None if candidate is None else candidate["mean"],
                "delta": requirement["deltas"]["mean"],
                "details": _canonical(requirement["deltas"]),
            }
        )
    for name, count in document["classification_transitions"].items():
        writer.writerow(
            {"record_type": "classification_transition", "name": name, "candidate": count}
        )
    return output.getvalue()


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _percent(value: object) -> str:
    return "—" if value is None else f"{100 * float(value):.2f}%"


def _html_document(document: dict[str, object]) -> str:
    aggregate = document["aggregate"]
    assert isinstance(aggregate, dict)
    corner_rows = "".join(
        "<tr><td>"
        + _text(", ".join(f"{name}={value}" for name, value in item["corners"].items()))
        + f"</td><td>{_percent(item['baseline']['observed_yield'])}</td>"
        + f"<td>{_percent(item['candidate']['observed_yield'])}</td>"
        + f"<td>{_percent(item['yield_delta'])}</td></tr>"
        for item in document["corners"]
    ) or '<tr><td colspan="4" class="muted">No named corners.</td></tr>'
    requirement_rows = "".join(
        f"<tr><td>{_text(item['analysis'])} / {_text(item['metric'])}</td>"
        f"<td>{_text(item['unit'])}</td>"
        f"<td>{'—' if item['baseline'] is None else _text(item['baseline']['mean'])}</td>"
        f"<td>{'—' if item['candidate'] is None else _text(item['candidate']['mean'])}</td>"
        f"<td>{'—' if item['deltas']['mean'] is None else _text(item['deltas']['mean'])}</td></tr>"
        for item in document["requirements"]
    ) or '<tr><td colspan="5" class="muted">No evaluated requirement margins.</td></tr>'
    transition_rows = "".join(
        f"<tr><td>{_text(name)}</td><td>{count}</td></tr>"
        for name, count in document["classification_transitions"].items()
    ) or '<tr><td colspan="2" class="muted">Unpaired comparison; no point transitions inferred.</td></tr>'
    baseline_id = str(document["baseline_experiment_id"])
    candidate_id = str(document["candidate_experiment_id"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Statistical comparison {_text(document['comparison_id'])}</title><style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1280px;margin:auto;padding:32px 24px 64px}}h1{{overflow-wrap:anywhere}}a{{color:var(--blue)}}.eyebrow{{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.09em}}.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:24px 0}}.card,.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px}}.card{{padding:16px}}.card strong{{display:block;font-size:22px;overflow-wrap:anywhere}}.panel{{padding:20px;margin:18px 0;overflow:hidden}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:680px}}th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase}}code{{overflow-wrap:anywhere}}@media(max-width:640px){{main{{padding:20px 12px}}.panel{{padding:12px}}}}
</style></head><body><main><div class="eyebrow">LTspice statistical evidence</div><h1>{_text(document['comparison_id'])}</h1><p><a href="../../{_text(baseline_id)}/results.json">{_text(baseline_id)}</a> → <a href="../../{_text(candidate_id)}/results.json">{_text(candidate_id)}</a></p><p class="muted">Structured evidence: <a href="comparison.json">comparison.json</a> · <a href="comparison.csv">comparison.csv</a></p>
<div class="cards"><div class="card"><span class="muted">Basis</span><strong>{_text(document['comparison_basis'])}</strong></div><div class="card"><span class="muted">Attribution</span><strong>{_text(document['attribution'])}</strong></div><div class="card"><span class="muted">Plan changed</span><strong>{_text(document['sample_plan_changed'])}</strong></div><div class="card"><span class="muted">Circuit changed</span><strong>{_text(document['circuit_changed'])}</strong></div></div>
<section class="panel"><h2>Aggregate yield</h2><div class="cards"><div class="card"><span class="muted">Baseline</span><strong>{_percent(aggregate['baseline']['observed_yield'])}</strong></div><div class="card"><span class="muted">Candidate</span><strong>{_percent(aggregate['candidate']['observed_yield'])}</strong></div><div class="card"><span class="muted">Delta</span><strong>{_percent(aggregate['yield_delta'])}</strong></div></div></section>
<section class="panel"><h2>Corner yield</h2><div class="table-wrap"><table><thead><tr><th>Corner</th><th>Baseline</th><th>Candidate</th><th>Delta</th></tr></thead><tbody>{corner_rows}</tbody></table></div></section>
<section class="panel"><h2>Requirement margin distributions</h2><div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Unit</th><th>Baseline mean</th><th>Candidate mean</th><th>Delta</th></tr></thead><tbody>{requirement_rows}</tbody></table></div></section>
<section class="panel"><h2>Point classification transitions</h2><div class="table-wrap"><table><thead><tr><th>Transition</th><th>Points</th></tr></thead><tbody>{transition_rows}</tbody></table></div></section>
</main></body></html>"""


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("statistical comparison artifact must not be a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
