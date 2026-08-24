"""Portable comparison reports and experiment dashboard views."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote

import experiment_engine
import experiment_index
import experiment_report

COMPARISON_REPORT_FILENAME = "comparison.html"
DASHBOARD_FILENAME = "dashboard.html"
_COMPARISON_DIRECTORY = re.compile(r"^comparison-([0-9a-f]{16})$")


class ComparisonReportResult(TypedDict):
    comparison_id: str
    comparison_html: str
    plot_count: int
    trace_count: int
    requirement_regressions: int
    requirement_improvements: int


class ExperimentDashboardResult(TypedDict):
    dashboard_html: str
    experiment_count: int
    comparison_count: int
    issue_count: int


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return format(value, ".8g")
    return str(value)


def _inside(path: Path, root: Path, message: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(message) from exc
    return resolved


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _comparison_plots(
    runs_dir: Path,
    baseline_id: str,
    candidate_id: str,
) -> list[dict[str, object]]:
    collections: list[tuple[str, str, list[dict[str, object]]]] = []
    for role, experiment_id in (("Baseline", baseline_id), ("Candidate", candidate_id)):
        experiment_dir, _, results, _ = experiment_report._load_artifacts(
            runs_dir, experiment_id
        )
        collections.append(
            (role, experiment_id, experiment_report._plots(experiment_dir, experiment_id, results))
        )

    groups: dict[str, dict[str, object]] = {}
    for role, experiment_id, plots in collections:
        for plot in plots:
            name = str(plot["name"])
            group = groups.setdefault(
                name,
                {"name": name, "signature": plot["signature"], "traces": []},
            )
            if group["signature"] != plot["signature"]:
                raise ValueError(
                    f"waveform analysis {name} differs between baseline and candidate"
                )
            traces = group["traces"]
            assert isinstance(traces, list)
            for original in plot["traces"]:
                trace = dict(original)
                trace["label"] = f"{role}: {trace['label']}"
                trace["raw_href"] = f"../../{experiment_id}/{trace['raw_href']}"
                traces.append(trace)

    ordered = [groups[name] for name in sorted(groups)]
    traces = [trace for plot in ordered for trace in plot["traces"]]
    displayed = sum(int(trace["displayed_points"]) for trace in traces)
    if (
        len(traces) > experiment_report.MAX_TRACE_COUNT
        or displayed > experiment_report.MAX_DISPLAYED_POINTS
    ):
        raise ValueError("comparison report display budget exceeded")
    return ordered


def _comparison_rows(
    comparison: experiment_engine.ExperimentComparisonResult,
) -> tuple[str, str, str]:
    point_rows: list[str] = []
    measurement_rows: list[str] = []
    requirement_rows: list[str] = []
    for point in comparison["points"]:
        parameters = ", ".join(
            f"{name}={value}" for name, value in sorted(point["parameters"].items())
        )
        point_rows.append(
            f'<tr><td>{_text(parameters)}</td>'
            f'<td>{_text(_number(point["baseline_index"]))}</td>'
            f'<td>{_text(_number(point["candidate_index"]))}</td>'
            f'<td>{_text(_number(point["baseline_all_passed"]))}</td>'
            f'<td>{_text(_number(point["candidate_all_passed"]))}</td>'
            f'<td><span class="badge {_text(point["status"])}">'
            f'{_text(point["status"])}</span></td></tr>'
        )
        for measurement in point["measurements"]:
            measurement_rows.append(
                f'<tr><td>{_text(parameters)}</td><td>{_text(measurement["name"])}</td>'
                f'<td>{_text(_number(measurement["baseline"]))}</td>'
                f'<td>{_text(_number(measurement["candidate"]))}</td>'
                f'<td>{_text(_number(measurement["delta"]))}</td>'
                f'<td><span class="badge {_text(measurement["status"])}">'
                f'{_text(measurement["status"])}</span></td></tr>'
            )
        for requirement in point["requirements"]:
            requirement_rows.append(
                f'<tr><td>{_text(parameters)}</td>'
                f'<td>{_text(requirement["analysis_name"])}</td>'
                f'<td>{_text(requirement["metric"])}</td>'
                f'<td>{_text(requirement["operator"])} '
                f'{_text(_number(requirement["target"]))} {_text(requirement["unit"])}</td>'
                f'<td>{_text(_number(requirement["baseline_value"]))} {_text(requirement["unit"])}</td>'
                f'<td>{_text(_number(requirement["candidate_value"]))} {_text(requirement["unit"])}</td>'
                f'<td>{_text(_number(requirement["value_delta"]))} {_text(requirement["unit"])}</td>'
                f'<td><span class="badge {_text(requirement["status"])}">'
                f'{_text(requirement["status"])}</span></td></tr>'
            )
    return "".join(point_rows), "".join(measurement_rows), "".join(requirement_rows)


def _available_reports(runs_dir: Path, experiment_ids: list[str]) -> set[str]:
    available: set[str] = set()
    for experiment_id in experiment_ids:
        path = runs_dir / experiment_id / experiment_report.REPORT_FILENAME
        try:
            path = _inside(
                path,
                runs_dir,
                "experiment report must remain inside the runs directory",
            )
        except ValueError:
            continue
        if path.is_file():
            available.add(experiment_id)
    return available


def _comparison_document(
    comparison: experiment_engine.ExperimentComparisonResult,
    plots: list[dict[str, object]],
    available_reports: set[str],
) -> str:
    baseline_id = comparison["baseline_experiment_id"]
    candidate_id = comparison["candidate_experiment_id"]
    point_rows, measurement_rows, requirement_rows = _comparison_rows(comparison)
    plot_html = "".join(
        experiment_report._svg(plot, index) for index, plot in enumerate(plots)
    )
    payload = json.dumps(
        [plot["payload"] for plot in plots],
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).replace("<", "\\u003c").replace("&", "\\u0026")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    evidence_links = [
        ("comparison.json", "comparison.json"),
        ("comparison.md", "comparison.md"),
    ]
    if baseline_id in available_reports:
        evidence_links.append((f"../../{baseline_id}/report.html", "baseline report"))
    if candidate_id in available_reports:
        evidence_links.append((f"../../{candidate_id}/report.html", "candidate report"))
    evidence = " · ".join(
        f'<a href="{quote(name, safe="/")}">{_text(label)}</a>'
        for name, label in evidence_links
    )
    no_measurements = (
        '<tr><td colspan="6" class="muted">No measurements were compared.</td></tr>'
        if not measurement_rows
        else ""
    )
    no_requirements = (
        '<tr><td colspan="8" class="muted">No requirements were compared.</td></tr>'
        if not requirement_rows
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment comparison {_text(comparison['comparison_id'])}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1280px;margin:auto;padding:32px 24px 64px}}
h1{{font-size:clamp(24px,4vw,38px);margin:.2rem 0;overflow-wrap:anywhere}}h2{{margin:0 0 10px;font-size:20px}}a{{color:var(--blue)}}.eyebrow{{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.09em}}.muted{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}}.card,.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px}}.card{{padding:16px}}.card strong{{display:block;font-size:25px}}.panel{{padding:20px;margin:18px 0;overflow:hidden}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;text-transform:uppercase;background:#30363d}}.badge.regression,.badge.removed{{color:#ffdcd7;background:#5a1e1e}}.badge.improvement,.badge.added{{color:#aff5b4;background:#1b4721}}.badge.unchanged,.badge.matched{{color:#c9d1d9;background:#30363d}}
.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.plot-wrap{{position:relative}}.plot{{display:block;width:100%;min-height:260px;background:#0b0f14;border:1px solid var(--border);border-radius:8px}}.grid-line{{stroke:#242b35;stroke-width:1}}.axis{{stroke:#768390;stroke-width:1.25}}.tick{{fill:var(--muted);font-size:11px}}.axis-label{{fill:var(--text);font-size:12px}}.trace{{fill:none;stroke-width:2.2;vector-effect:non-scaling-stroke}}.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}}.legend span{{display:flex;align-items:center;gap:6px}}.legend i{{display:inline-block;width:18px;height:3px}}.cursor{{stroke:#c9d1d9;stroke-width:1;stroke-dasharray:4 4}}.plot-tooltip{{position:absolute;pointer-events:none;z-index:2;background:#010409;border:1px solid var(--border);border-radius:6px;padding:8px 10px;white-space:pre;font:12px/1.45 ui-monospace,monospace;box-shadow:0 8px 24px #0008}}
@media(max-width:640px){{main{{padding:20px 12px}}.panel{{padding:12px}}}}
</style></head><body><main>
<div class="eyebrow">LTspice experiment comparison</div><h1>{_text(baseline_id)} → {_text(candidate_id)}</h1>
<p class="muted">Portable evidence: {evidence}</p>
<div class="cards"><div class="card"><span class="muted">Matched points</span><strong>{comparison['matched_points']}</strong></div><div class="card"><span class="muted">Added points</span><strong>{comparison['added_points']}</strong></div><div class="card"><span class="muted">Removed points</span><strong>{comparison['removed_points']}</strong></div><div class="card"><span class="muted">Regressions</span><strong>{comparison['requirement_regressions']}</strong></div><div class="card"><span class="muted">Improvements</span><strong>{comparison['requirement_improvements']}</strong></div><div class="card"><span class="muted">Plots</span><strong>{len(plots)}</strong></div></div>
{plot_html}
<section class="panel"><h2>Point changes</h2><div class="table-wrap"><table><thead><tr><th>Parameters</th><th>Baseline index</th><th>Candidate index</th><th>Baseline passed</th><th>Candidate passed</th><th>Status</th></tr></thead><tbody>{point_rows}</tbody></table></div></section>
<section class="panel"><h2>Requirement changes</h2><div class="table-wrap"><table><thead><tr><th>Parameters</th><th>Analysis</th><th>Metric</th><th>Requirement</th><th>Baseline</th><th>Candidate</th><th>Delta</th><th>Status</th></tr></thead><tbody>{requirement_rows}{no_requirements}</tbody></table></div></section>
<section class="panel"><h2>Measurement changes</h2><div class="table-wrap"><table><thead><tr><th>Parameters</th><th>Measurement</th><th>Baseline</th><th>Candidate</th><th>Delta</th><th>Status</th></tr></thead><tbody>{measurement_rows}{no_measurements}</tbody></table></div></section>
</main><script id="report-data" type="application/json">{payload}</script><script>
const plots=JSON.parse(document.getElementById("report-data").textContent);document.querySelectorAll("svg.plot").forEach(svg=>{{const plot=plots[Number(svg.dataset.plotIndex)],tip=svg.parentElement.querySelector(".plot-tooltip"),cursor=svg.querySelector(".cursor");svg.addEventListener("pointermove",event=>{{const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;const local=point.matrixTransform(svg.getScreenCTM().inverse());let nearest=null;plot.traces.forEach(trace=>trace.screen.forEach((screen,index)=>{{const distance=Math.abs(screen[0]-local.x);if(!nearest||distance<nearest.distance)nearest={{distance,trace,index,screen}}}}));if(!nearest)return;cursor.hidden=false;cursor.setAttribute("x1",nearest.screen[0]);cursor.setAttribute("x2",nearest.screen[0]);tip.hidden=false;tip.textContent=`${{nearest.trace.label}}\n${{plot.axis_label}}: ${{nearest.trace.x[nearest.index].toPrecision(6)}}\n${{plot.y_label}}: ${{nearest.trace.y[nearest.index].toPrecision(6)}}`;tip.style.left=Math.min(event.offsetX+12,svg.clientWidth-tip.offsetWidth-8)+"px";tip.style.top=Math.max(8,event.offsetY-tip.offsetHeight-8)+"px";}});svg.addEventListener("pointerleave",()=>{{cursor.hidden=true;tip.hidden=true}});}});
</script></body></html>"""


def build_comparison_report(
    runs_dir: Path,
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> ComparisonReportResult:
    """Build a portable baseline-versus-candidate waveform report."""
    runs_dir = runs_dir.resolve()
    # Validate both experiments and every plotted RAW artifact before comparison
    # files are published.
    plots = _comparison_plots(runs_dir, baseline_experiment_id, candidate_experiment_id)
    comparison = experiment_engine.compare_experiments(
        runs_dir, baseline_experiment_id, candidate_experiment_id
    )
    comparison_dir = _inside(
        Path(comparison["comparison_dir"]),
        runs_dir,
        "comparison report must remain inside the runs directory",
    )
    available_reports = _available_reports(
        runs_dir, [baseline_experiment_id, candidate_experiment_id]
    )
    document = _comparison_document(comparison, plots, available_reports)
    report_path = _inside(
        comparison_dir / COMPARISON_REPORT_FILENAME,
        comparison_dir,
        "comparison report must remain inside its comparison directory",
    )
    _write_text(report_path, document)
    traces = [trace for plot in plots for trace in plot["traces"]]
    return {
        "comparison_id": comparison["comparison_id"],
        "comparison_html": str(report_path),
        "plot_count": len(plots),
        "trace_count": len(traces),
        "requirement_regressions": comparison["requirement_regressions"],
        "requirement_improvements": comparison["requirement_improvements"],
    }


def _all_experiments(runs_dir: Path) -> list[experiment_index.ExperimentIndexRecord]:
    records: list[experiment_index.ExperimentIndexRecord] = []
    offset = 0
    while True:
        page = experiment_index.query_experiments(
            runs_dir, limit=experiment_index.MAX_QUERY_LIMIT, offset=offset
        )
        records.extend(page["experiments"])
        offset += len(page["experiments"])
        if offset >= page["total"] or not page["experiments"]:
            return records


def _comparisons(runs_dir: Path) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    issues = 0
    root = runs_dir / "comparisons"
    if not root.is_dir():
        return records, issues
    for path in sorted(root.glob("comparison-*/comparison.json")):
        try:
            path = _inside(
                path,
                runs_dir,
                "comparison artifact must remain inside the runs directory",
            )
            match = _COMPARISON_DIRECTORY.fullmatch(path.parent.name)
            if match is None:
                raise ValueError("invalid comparison directory name")
            document, _ = experiment_index._load_json(path)
            if document.get("schema_version") != 1:
                raise ValueError("unsupported comparison schema_version")
            comparison_id = document.get("comparison_id")
            baseline_id = document.get("baseline_experiment_id")
            candidate_id = document.get("candidate_experiment_id")
            if comparison_id != match.group(1):
                raise ValueError("comparison ID does not match its directory")
            if not isinstance(baseline_id, str) or not isinstance(candidate_id, str):
                raise ValueError("comparison experiment IDs must be strings")
            experiment_engine._comparison_experiment_path(runs_dir, baseline_id)
            experiment_engine._comparison_experiment_path(runs_dir, candidate_id)
            regressions = experiment_index._plain_int(
                document.get("requirement_regressions"), "requirement_regressions"
            )
            improvements = experiment_index._plain_int(
                document.get("requirement_improvements"), "requirement_improvements"
            )
            added_points = experiment_index._plain_int(
                document.get("added_points"), "added_points"
            )
            removed_points = experiment_index._plain_int(
                document.get("removed_points"), "removed_points"
            )
            points = document.get("points")
            if not isinstance(points, list) or any(
                not isinstance(point, dict)
                or point.get("status") not in {"matched", "added", "removed"}
                or not isinstance(point.get("requirements"), list)
                for point in points
            ):
                raise ValueError("comparison points are invalid")
            requirements = [
                requirement
                for point in points
                for requirement in point["requirements"]
            ]
            if any(
                not isinstance(requirement, dict)
                or requirement.get("status")
                not in {"regression", "improvement", "unchanged", "added", "removed"}
                for requirement in requirements
            ):
                raise ValueError("comparison requirements are invalid")
            if regressions != sum(
                requirement["status"] == "regression" for requirement in requirements
            ) or improvements != sum(
                requirement["status"] == "improvement" for requirement in requirements
            ):
                raise ValueError("comparison requirement counts are inconsistent")
            if added_points != sum(
                point["status"] == "added" for point in points
            ) or removed_points != sum(
                point["status"] == "removed" for point in points
            ):
                raise ValueError("comparison point counts are inconsistent")
            comparison_html = _inside(
                path.parent / COMPARISON_REPORT_FILENAME,
                runs_dir,
                "comparison report must remain inside the runs directory",
            )
            records.append(
                {
                    "comparison_id": comparison_id,
                    "baseline_experiment_id": baseline_id,
                    "candidate_experiment_id": candidate_id,
                    "requirement_regressions": regressions,
                    "requirement_improvements": improvements,
                    "added_points": added_points,
                    "removed_points": removed_points,
                    "html": comparison_html.is_file(),
                }
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            issues += 1
    return records, issues


def _dashboard_document(
    experiments: list[experiment_index.ExperimentIndexRecord],
    comparisons: list[dict[str, object]],
    issue_count: int,
    available_reports: set[str],
) -> str:
    experiment_rows: list[str] = []
    for record in experiments:
        experiment_id = record["experiment_id"]
        parameters = ", ".join(
            (
                f"{item['name']}=[{', '.join(item['values'])}]"
                if item["values"] is not None
                else f"{item['name']}={item['template']}"
            )
            for item in record["parameters"]
        ) or "—"
        state = (
            "pass" if record["all_passed"] is True else
            "fail" if record["all_passed"] is False else "neutral"
        )
        results = None if record["results_path"] is None else quote(record["results_path"], safe="/")
        links = [
            f'<a href="{quote(record["manifest_path"], safe="/")}">manifest</a>'
        ]
        if results is not None:
            links.append(f'<a href="{results}">results</a>')
        if experiment_id in available_reports:
            links.append(
                f'<a class="report-link" href="{quote(experiment_id, safe="")}/report.html">report</a>'
            )
        search = " ".join(
            [
                experiment_id,
                record["status"],
                record["execution_mode"],
                record["index_state"],
                parameters,
                *record["measurement_names"],
                *record["requirement_metrics"],
            ]
        ).lower()
        experiment_rows.append(
            f'<tr data-search="{_text(search)}" data-status="{_text(record["status"])}" data-mode="{_text(record["execution_mode"])}">'
            f'<td><code>{_text(experiment_id)}</code></td><td>{_text(record["recorded_at"])}</td>'
            f'<td><span class="badge {_text(state)}">{_text(record["status"])}</span></td>'
            f'<td>{_text(record["execution_mode"])}</td><td>{record["finished_points"]}/{record["point_count"]}</td>'
            f'<td>{record["passed_points"]}/{record["failed_points"]}</td><td>{_text(parameters)}</td>'
            f'<td>{" · ".join(links)}</td></tr>'
        )
    comparison_rows: list[str] = []
    for record in comparisons:
        comparison_id = str(record["comparison_id"])
        directory = f"comparisons/comparison-{comparison_id}"
        primary = (
            f'{directory}/{COMPARISON_REPORT_FILENAME}'
            if record["html"]
            else f"{directory}/comparison.json"
        )
        comparison_rows.append(
            f'<tr data-search="{_text((comparison_id + " " + str(record["baseline_experiment_id"]) + " " + str(record["candidate_experiment_id"])).lower())}">'
            f'<td><a href="{quote(primary, safe="/")}"><code>{_text(comparison_id)}</code></a></td>'
            f'<td>{_text(record["baseline_experiment_id"])}</td><td>{_text(record["candidate_experiment_id"])}</td>'
            f'<td>{record["added_points"]}/{record["removed_points"]}</td>'
            f'<td><span class="badge fail">{record["requirement_regressions"]}</span></td>'
            f'<td><span class="badge pass">{record["requirement_improvements"]}</span></td></tr>'
        )
    data = json.dumps(
        {"experiment_count": len(experiments), "comparison_count": len(comparisons)},
        separators=(",", ":"),
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LTspice experiment dashboard</title><style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}main{{max-width:1440px;margin:auto;padding:32px 24px 64px}}h1{{font-size:clamp(26px,4vw,42px);margin:.2rem 0}}h2{{margin-top:0}}a{{color:var(--blue)}}code{{overflow-wrap:anywhere}}.eyebrow{{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.09em}}.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:24px 0}}.card,.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px}}.card{{padding:16px}}.card strong{{display:block;font-size:26px}}.panel{{padding:20px;margin:18px 0}}.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}}input,select{{background:#0b0f14;color:var(--text);border:1px solid var(--border);border-radius:7px;padding:9px 11px}}input{{min-width:min(420px,100%)}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:900px}}th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;text-transform:uppercase;background:#30363d}}.badge.pass{{color:#aff5b4;background:#1b4721}}.badge.fail{{color:#ffdcd7;background:#5a1e1e}}.badge.neutral{{color:#c9d1d9;background:#30363d}}[hidden]{{display:none!important}}@media(max-width:640px){{main{{padding:20px 12px}}.panel{{padding:12px}}}}
</style></head><body><main><div class="eyebrow">LTspice automation</div><h1>Structured experiment dashboard</h1><p class="muted">Rebuildable human view of the experiment index and comparison artifacts.</p><div class="cards"><div class="card"><span class="muted">Experiments</span><strong>{len(experiments)}</strong></div><div class="card"><span class="muted">Comparisons</span><strong>{len(comparisons)}</strong></div><div class="card"><span class="muted">Artifact issues</span><strong>{issue_count}</strong></div></div>
<section class="panel"><h2>Experiments</h2><div class="controls"><input id="search" type="search" placeholder="Search IDs, status, mode, or parameters" aria-label="Search experiments"><select id="status" aria-label="Filter by status"><option value="">All statuses</option>{''.join(f'<option value="{status}">{status}</option>' for status in sorted(experiment_index.EXPERIMENT_STATUSES))}</select><select id="mode" aria-label="Filter by execution mode"><option value="">All modes</option><option value="independent">independent</option><option value="native">native</option></select><span id="visible" class="muted"></span></div><div class="table-wrap"><table><thead><tr><th>Experiment</th><th>Recorded</th><th>Status</th><th>Mode</th><th>Finished</th><th>Pass/fail</th><th>Parameters</th><th>Artifacts</th></tr></thead><tbody id="experiments">{''.join(experiment_rows)}</tbody></table></div></section>
<section class="panel"><h2>Comparisons</h2><div class="table-wrap"><table><thead><tr><th>Comparison</th><th>Baseline</th><th>Candidate</th><th>Added/removed points</th><th>Regressions</th><th>Improvements</th></tr></thead><tbody id="comparisons">{''.join(comparison_rows)}</tbody></table></div></section>
</main><script id="dashboard-data" type="application/json">{data}</script><script>const search=document.getElementById("search"),status=document.getElementById("status"),mode=document.getElementById("mode"),rows=[...document.querySelectorAll("#experiments tr")],comparisons=[...document.querySelectorAll("#comparisons tr")],visible=document.getElementById("visible");function filter(){{const term=search.value.trim().toLowerCase();let count=0;rows.forEach(row=>{{row.hidden=!(row.dataset.search.includes(term)&&(!status.value||row.dataset.status===status.value)&&(!mode.value||row.dataset.mode===mode.value));if(!row.hidden)count++}});comparisons.forEach(row=>row.hidden=!!term&&!row.dataset.search.includes(term));visible.textContent=`${{count}} shown`;}}[search,status,mode].forEach(control=>control.addEventListener("input",filter));filter();</script></body></html>"""


def build_experiment_dashboard(runs_dir: Path) -> ExperimentDashboardResult:
    """Build a searchable offline dashboard from the derived experiment index."""
    runs_dir = runs_dir.resolve()
    index_build = experiment_index.build_experiment_index(runs_dir)
    experiments = _all_experiments(runs_dir)
    comparisons, comparison_issues = _comparisons(runs_dir)
    database_path = experiment_index._database_path(runs_dir, None)
    connection = sqlite3.connect(database_path)
    try:
        index_issue_count = connection.execute(
            "SELECT COUNT(*) FROM index_issues"
        ).fetchone()[0]
    finally:
        connection.close()
    if int(index_issue_count) != index_build["issue_count"]:
        raise RuntimeError("experiment index issue count changed during dashboard build")
    issue_count = int(index_issue_count) + comparison_issues
    available_reports = _available_reports(
        runs_dir, [record["experiment_id"] for record in experiments]
    )
    document = _dashboard_document(
        experiments, comparisons, issue_count, available_reports
    )
    path = _inside(
        runs_dir / DASHBOARD_FILENAME,
        runs_dir,
        "experiment dashboard must remain inside the runs directory",
    )
    _write_text(path, document)
    return {
        "dashboard_html": str(path),
        "experiment_count": len(experiments),
        "comparison_count": len(comparisons),
        "issue_count": issue_count,
    }
