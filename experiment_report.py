"""Portable offline HTML reports for structured LTspice experiments."""

from __future__ import annotations

import html
import json
import math
import os
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote

import experiment_index
import raw_parser

DISPLAY_POINT_LIMIT = 400
MAX_TRACE_COUNT = 100
MAX_DISPLAYED_POINTS = 40_000
REPORT_FILENAME = "report.html"
_SVG_WIDTH = 900
_SVG_HEIGHT = 380
_PLOT_LEFT = 76
_PLOT_RIGHT = 24
_PLOT_TOP = 24
_PLOT_BOTTOM = 58
_COLORS = ("#58a6ff", "#f0883e", "#3fb950", "#d2a8ff", "#f85149", "#a5d6ff")


class ExperimentReportResult(TypedDict):
    experiment_id: str
    report_html: str
    plot_count: int
    trace_count: int
    source_points: int
    displayed_points: int


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> str:
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


def _artifact_path(reference: object, experiment_dir: Path, experiment_id: str) -> tuple[Path, str]:
    return experiment_index.waveform_artifact_path(
        reference, experiment_dir, experiment_id
    )


def _load_artifacts(
    runs_dir: Path, experiment_id: str
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    return experiment_index.load_completed_experiment(runs_dir, experiment_id)


def _sample_indices(values: list[float]) -> list[int]:
    """Retain endpoints and each display bucket's extrema."""
    length = len(values)
    if length <= DISPLAY_POINT_LIMIT:
        return list(range(length))
    if DISPLAY_POINT_LIMIT == 1:
        return [0]
    if DISPLAY_POINT_LIMIT == 2:
        return [0, length - 1]
    if DISPLAY_POINT_LIMIT == 3:
        interior = max(range(1, length - 1), key=lambda index: abs(values[index]))
        return [0, interior, length - 1]
    bucket_count = (DISPLAY_POINT_LIMIT - 2) // 2
    selected = {0, length - 1}
    for bucket in range(bucket_count):
        start = 1 + bucket * (length - 2) // bucket_count
        stop = 1 + (bucket + 1) * (length - 2) // bucket_count
        indexes = range(start, stop)
        selected.add(min(indexes, key=values.__getitem__))
        selected.add(max(indexes, key=values.__getitem__))
    return sorted(selected)


def _real(value: float | complex, field: str) -> float:
    number = float(value.real if isinstance(value, complex) else value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must contain finite values")
    return number


def _trace(
    data: raw_parser.RawData,
    analysis: dict[str, object],
    label: str,
    raw_href: str,
) -> dict[str, object]:
    axis_name = analysis.get("axis_variable")
    variable = analysis.get("variable")
    secondary = analysis.get("secondary_variable")
    step_index = analysis.get("step_index", 0)
    source_points = analysis.get("source_points")
    if not isinstance(axis_name, str) or not axis_name:
        raise ValueError("waveform axis_variable must be a non-empty string")
    if not isinstance(variable, str) or not variable:
        raise ValueError("waveform variable must be a non-empty string")
    if secondary is not None and (not isinstance(secondary, str) or not secondary):
        raise ValueError("waveform secondary_variable must be a string or null")
    if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
        raise ValueError("waveform step_index must be a nonnegative integer")
    if not isinstance(source_points, int) or isinstance(source_points, bool) or source_points < 1:
        raise ValueError("waveform source_points must be a positive integer")
    required = [axis_name, variable, *([] if secondary is None else [secondary])]
    missing = [name for name in required if name not in data.values]
    if missing:
        raise ValueError(f"waveform variable not found in RAW artifact: {missing[0]}")
    slices = raw_parser.step_slices(data)
    if step_index >= len(slices):
        raise ValueError("waveform step_index is outside the RAW artifact")
    selected = slices[step_index]
    axis_values = data.values[axis_name][selected]
    primary_values = data.values[variable][selected]
    secondary_values = None if secondary is None else data.values[secondary][selected]
    if len(axis_values) != source_points:
        raise ValueError("waveform source_points does not match the RAW artifact")
    x = [_real(value, "waveform axis") for value in axis_values]
    complex_trace = any(isinstance(value, complex) for value in primary_values)
    if secondary_values is not None:
        complex_trace = complex_trace or any(
            isinstance(value, complex) for value in secondary_values
        )
    if complex_trace:
        y = []
        for index, primary in enumerate(primary_values):
            value = primary
            if secondary_values is not None:
                denominator = secondary_values[index]
                if abs(denominator) == 0:
                    raise ValueError("waveform secondary_variable contains zero")
                value = primary / denominator
            magnitude = abs(value)
            y.append(-300.0 if magnitude == 0 else 20.0 * math.log10(magnitude))
        y_label = "Gain (dB)" if secondary is not None else "Magnitude (dB)"
        display_floor_db: float | None = -300.0
    else:
        y = [_real(value, "waveform trace") for value in primary_values]
        y_label = variable
        display_floor_db = None
    if not x or len(x) != len(y):
        raise ValueError("waveform trace is empty or inconsistent")
    indices = _sample_indices(y)
    return {
        "label": label,
        "x": [x[index] for index in indices],
        "y": [y[index] for index in indices],
        "source_points": len(x),
        "displayed_points": len(indices),
        "raw_href": raw_href,
        "axis_label": axis_name.title(),
        "y_label": y_label,
        "log_x": axis_name.lower() == "frequency" and all(value > 0 for value in x),
        "variable": variable,
        "secondary_variable": secondary,
        "display_floor_db": display_floor_db,
    }


def _plots(
    experiment_dir: Path, experiment_id: str, results: dict[str, object]
) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    raw_cache: dict[Path, raw_parser.RawData] = {}
    points = results["points"]
    assert isinstance(points, list)
    for point in sorted(points, key=lambda item: item["index"]):
        parameters = point["parameters"]
        label = ", ".join(f"{name}={value}" for name, value in sorted(parameters.items()))
        execution_mode = results.get("execution_mode", "independent")
        if execution_mode == "native":
            native_step_index = point.get("native_step_index")
            if native_step_index != point["index"]:
                raise ValueError("native point index and step mapping are inconsistent")
            expected_step_index = native_step_index
        else:
            expected_step_index = 0
        for entry in point["analyses"]:
            if entry.get("status") != "completed" or not isinstance(entry.get("analysis"), dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("waveform analysis name must be a non-empty string")
            analysis = entry["analysis"]
            if analysis.get("step_index", 0) != expected_step_index:
                raise ValueError("waveform step does not match its experiment point")
            raw_path, raw_href = _artifact_path(
                analysis.get("raw_file"), experiment_dir, experiment_id
            )
            expected_root = (
                "native-batch"
                if results.get("execution_mode", "independent") == "native"
                else f"point-{point['index']:04d}"
            )
            if Path(raw_href).parts[0] != expected_root:
                raise ValueError("waveform artifact does not match its experiment point")
            if raw_path not in raw_cache:
                raw_cache[raw_path] = raw_parser.parse_raw(raw_path)
            trace = _trace(raw_cache[raw_path], analysis, label, raw_href)
            signature = (
                trace["axis_label"],
                trace["y_label"],
                trace["log_x"],
                trace["variable"],
                trace["secondary_variable"],
            )
            group = groups.setdefault(
                name,
                {"name": name, "signature": signature, "traces": []},
            )
            if group["signature"] != signature:
                raise ValueError(f"waveform analysis {name} has inconsistent plot metadata")
            group["traces"].append(trace)
            trace_count = sum(len(item["traces"]) for item in groups.values())
            displayed_points = sum(
                int(candidate["displayed_points"])
                for plot_group in groups.values()
                for candidate in plot_group["traces"]
            )
            if trace_count > MAX_TRACE_COUNT or displayed_points > MAX_DISPLAYED_POINTS:
                raise ValueError("experiment report display budget exceeded")
    return [groups[name] for name in sorted(groups)]


def _extent(values: list[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    if low == high:
        padding = max(abs(low) * 0.05, 1.0)
        return low - padding, high + padding
    return low, high


def _svg(plot: dict[str, object], plot_index: int) -> str:
    traces = plot["traces"]
    assert isinstance(traces, list)
    log_x = bool(traces[0]["log_x"])
    all_x = [math.log10(value) if log_x else value for trace in traces for value in trace["x"]]
    all_y = [value for trace in traces for value in trace["y"]]
    x_min, x_max = _extent(all_x)
    y_min, y_max = _extent(all_y)
    width = _SVG_WIDTH - _PLOT_LEFT - _PLOT_RIGHT
    height = _SVG_HEIGHT - _PLOT_TOP - _PLOT_BOTTOM

    def px(value: float) -> float:
        transformed = math.log10(value) if log_x else value
        return _PLOT_LEFT + (transformed - x_min) * width / (x_max - x_min)

    def py(value: float) -> float:
        return _PLOT_TOP + (y_max - value) * height / (y_max - y_min)

    grid: list[str] = []
    for tick in range(6):
        fraction = tick / 5
        x_pixel = _PLOT_LEFT + fraction * width
        x_value = x_min + fraction * (x_max - x_min)
        if log_x:
            x_value = 10**x_value
        y_pixel = _PLOT_TOP + fraction * height
        y_value = y_max - fraction * (y_max - y_min)
        grid.extend(
            [
                f'<line class="grid-line" x1="{x_pixel:.2f}" y1="{_PLOT_TOP}" x2="{x_pixel:.2f}" y2="{_PLOT_TOP + height}"/>',
                f'<text class="tick" x="{x_pixel:.2f}" y="{_PLOT_TOP + height + 22}" text-anchor="middle">{_text(format(x_value, ".4g"))}</text>',
                f'<line class="grid-line" x1="{_PLOT_LEFT}" y1="{y_pixel:.2f}" x2="{_PLOT_LEFT + width}" y2="{y_pixel:.2f}"/>',
                f'<text class="tick" x="{_PLOT_LEFT - 10}" y="{y_pixel + 4:.2f}" text-anchor="end">{_text(format(y_value, ".4g"))}</text>',
            ]
        )
    paths: list[str] = []
    legend: list[str] = []
    payload_traces: list[dict[str, object]] = []
    for index, trace in enumerate(traces):
        color = _COLORS[index % len(_COLORS)]
        screen = [[px(x), py(y)] for x, y in zip(trace["x"], trace["y"])]
        path_data = " ".join(
            ("M" if point_index == 0 else "L") + f"{point[0]:.2f},{point[1]:.2f}"
            for point_index, point in enumerate(screen)
        )
        paths.append(
            f'<path class="trace" d="{path_data}" stroke="{color}" data-trace-index="{index}"/>'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>{_text(trace["label"])}</span>'
        )
        payload_traces.append(
            {
                "label": trace["label"],
                "x": trace["x"],
                "y": trace["y"],
                "screen": screen,
            }
        )
    plot["payload"] = {
        "axis_label": traces[0]["axis_label"],
        "y_label": traces[0]["y_label"],
        "traces": payload_traces,
    }
    raw_links = sorted({str(trace["raw_href"]) for trace in traces})
    source_links = ", ".join(
        f'<a href="{quote(path, safe="/")}">{_text(path)}</a>' for path in raw_links
    )
    source_counts = sorted({int(trace["source_points"]) for trace in traces})
    source_count = (
        f"{source_counts[0]} points per trace"
        if len(source_counts) == 1
        else f"{source_counts[0]}–{source_counts[-1]} points per trace"
    )
    floor_note = (
        " Zero magnitude is shown at the -300 dB display floor."
        if traces[0]["display_floor_db"] is not None
        else ""
    )
    return f"""
<section class="panel plot-panel">
  <h2>{_text(plot['name'])}</h2>
  <p class="muted">Full-resolution source: {source_count} · {source_links}.{floor_note}</p>
  <div class="legend">{''.join(legend)}</div>
  <div class="plot-wrap">
    <svg class="plot" data-plot-index="{plot_index}" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" aria-label="{_text(plot['name'])} waveform overlay">
      {''.join(grid)}
      <line class="axis" x1="{_PLOT_LEFT}" y1="{_PLOT_TOP + height}" x2="{_PLOT_LEFT + width}" y2="{_PLOT_TOP + height}"/>
      <line class="axis" x1="{_PLOT_LEFT}" y1="{_PLOT_TOP}" x2="{_PLOT_LEFT}" y2="{_PLOT_TOP + height}"/>
      {''.join(paths)}
      <line class="cursor" x1="0" y1="{_PLOT_TOP}" x2="0" y2="{_PLOT_TOP + height}" hidden/>
      <text class="axis-label" x="{_PLOT_LEFT + width / 2:.2f}" y="{_SVG_HEIGHT - 10}" text-anchor="middle">{_text(traces[0]['axis_label'])}</text>
      <text class="axis-label" transform="translate(18 {_PLOT_TOP + height / 2:.2f}) rotate(-90)" text-anchor="middle">{_text(traces[0]['y_label'])}</text>
    </svg>
    <div class="plot-tooltip" hidden></div>
  </div>
</section>"""


def _table_rows(results: dict[str, object]) -> tuple[str, str]:
    point_rows: list[str] = []
    requirement_rows: list[str] = []
    points = results["points"]
    assert isinstance(points, list)
    for point in sorted(points, key=lambda item: item["index"]):
        parameters = ", ".join(
            f"{name}={value}" for name, value in sorted(point["parameters"].items())
        )
        measurements = ", ".join(
            f"{name}={_number(value)}" for name, value in sorted(point["measurements"].items())
        ) or "—"
        state = "pass" if point["all_passed"] else "fail"
        errors = [str(point["error"])] if point.get("error") else []
        errors.extend(
            str(entry["error"])
            for entry in point["analyses"]
            if entry.get("error")
        )
        point_rows.append(
            f'<tr><td>{point["index"]}</td><td>{_text(parameters)}</td>'
            f'<td>{_text(measurements)}</td><td>{_text("; ".join(errors) or "—")}</td>'
            f'<td><span class="badge {state}">{state}</span></td></tr>'
        )
        for entry in point["analyses"]:
            analysis = entry.get("analysis")
            if not isinstance(analysis, dict):
                continue
            for result in analysis.get("results", []):
                threshold = result["threshold"]
                state = "pass" if result["passed"] else "fail"
                requirement_rows.append(
                    f'<tr><td>{point["index"]}</td><td>{_text(parameters)}</td>'
                    f'<td>{_text(entry["name"])}</td><td>{_text(result["metric"])}</td>'
                    f'<td>{_text(_number(result["value"]))} {_text(result["unit"])}</td>'
                    f'<td>{_text(threshold["operator"])} {_text(_number(threshold["target"]))} {_text(threshold["unit"])}</td>'
                    f'<td><span class="badge {state}">{state}</span></td></tr>'
                )
    return "".join(point_rows), "".join(requirement_rows)


def _document(
    experiment_id: str,
    results: dict[str, object],
    record: dict[str, object],
    plots: list[dict[str, object]],
    artifacts: list[str],
) -> str:
    point_rows, requirement_rows = _table_rows(results)
    plot_html = "".join(_svg(plot, index) for index, plot in enumerate(plots))
    payload = json.dumps(
        [plot["payload"] for plot in plots],
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).replace("<", "\\u003c").replace("&", "\\u0026")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    links = " · ".join(
        f'<a href="{quote(name, safe="/")}">{_text(name)}</a>' for name in artifacts
    )
    state = "pass" if results["all_passed"] else "fail"
    no_requirements = (
        '<tr><td colspan="7" class="muted">No waveform requirements were recorded.</td></tr>'
        if not requirement_rows
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment {_text(experiment_id)}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1280px;margin:auto;padding:32px 24px 64px}} h1{{font-size:clamp(24px,4vw,38px);margin:.2rem 0;overflow-wrap:anywhere}} h2{{margin:0 0 10px;font-size:20px}}
a{{color:var(--blue)}} .eyebrow{{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.09em}} .muted{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}} .card,.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px}}
.card{{padding:16px}} .card strong{{display:block;font-size:25px}} .panel{{padding:20px;margin:18px 0;overflow:hidden}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;text-transform:uppercase}} .badge.pass{{color:#aff5b4;background:#1b4721}} .badge.fail{{color:#ffdcd7;background:#5a1e1e}}
.table-wrap{{overflow:auto}} table{{border-collapse:collapse;width:100%;min-width:720px}} th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.plot-wrap{{position:relative}} .plot{{display:block;width:100%;min-height:260px;background:#0b0f14;border:1px solid var(--border);border-radius:8px}}
.grid-line{{stroke:#242b35;stroke-width:1}} .axis{{stroke:#768390;stroke-width:1.25}} .tick{{fill:var(--muted);font-size:11px}} .axis-label{{fill:var(--text);font-size:12px}} .trace{{fill:none;stroke-width:2.2;vector-effect:non-scaling-stroke}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}} .legend span{{display:flex;align-items:center;gap:6px}} .legend i{{display:inline-block;width:18px;height:3px}}
.cursor{{stroke:#c9d1d9;stroke-width:1;stroke-dasharray:4 4}} .plot-tooltip{{position:absolute;pointer-events:none;z-index:2;background:#010409;border:1px solid var(--border);border-radius:6px;padding:8px 10px;white-space:pre;font:12px/1.45 ui-monospace,monospace;box-shadow:0 8px 24px #0008}}
@media(max-width:640px){{main{{padding:20px 12px}}.panel{{padding:12px}}}}
</style>
</head>
<body><main>
<div class="eyebrow">LTspice structured experiment</div>
<h1>{_text(experiment_id)}</h1>
<p><span class="badge {state}">{state}</span> · {_text(record['execution_mode'])} execution · Recorded {_text(record['recorded_at'])}</p>
<p class="muted">Portable evidence: {links}</p>
<div class="cards">
  <div class="card"><span class="muted">Points</span><strong>{results['point_count']}</strong></div>
  <div class="card"><span class="muted">Passed</span><strong>{results['passed_points']}</strong></div>
  <div class="card"><span class="muted">Failed</span><strong>{results['failed_points']}</strong></div>
  <div class="card"><span class="muted">Plots</span><strong>{len(plots)}</strong></div>
</div>
{plot_html}
<section class="panel"><h2>Requirement results</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Parameters</th><th>Analysis</th><th>Metric</th><th>Value</th><th>Requirement</th><th>Status</th></tr></thead><tbody>{requirement_rows}{no_requirements}</tbody></table></div></section>
<section class="panel"><h2>Experiment points</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Parameters</th><th>Measurements</th><th>Errors</th><th>Status</th></tr></thead><tbody>{point_rows}</tbody></table></div></section>
</main>
<script id="report-data" type="application/json">{payload}</script>
<script>
const plots=JSON.parse(document.getElementById("report-data").textContent);
document.querySelectorAll("svg.plot").forEach(svg=>{{
  const plot=plots[Number(svg.dataset.plotIndex)], tip=svg.parentElement.querySelector(".plot-tooltip"), cursor=svg.querySelector(".cursor");
  svg.addEventListener("pointermove", event=>{{
    const point=svg.createSVGPoint(); point.x=event.clientX; point.y=event.clientY;
    const local=point.matrixTransform(svg.getScreenCTM().inverse()); let nearest=null;
    plot.traces.forEach(trace=>trace.screen.forEach((screen,index)=>{{const distance=Math.abs(screen[0]-local.x);if(!nearest||distance<nearest.distance)nearest={{distance,trace,index,screen}}}}));
    if(!nearest)return; cursor.hidden=false; cursor.setAttribute("x1",nearest.screen[0]); cursor.setAttribute("x2",nearest.screen[0]);
    tip.hidden=false; tip.textContent=`${{nearest.trace.label}}\n${{plot.axis_label}}: ${{nearest.trace.x[nearest.index].toPrecision(6)}}\n${{plot.y_label}}: ${{nearest.trace.y[nearest.index].toPrecision(6)}}`;
    tip.style.left=Math.min(event.offsetX+12,svg.clientWidth-tip.offsetWidth-8)+"px"; tip.style.top=Math.max(8,event.offsetY-tip.offsetHeight-8)+"px";
  }});
  svg.addEventListener("pointerleave",()=>{{cursor.hidden=true;tip.hidden=true}});
}});
</script>
</body></html>
"""


def build_experiment_report(runs_dir: Path, experiment_id: str) -> ExperimentReportResult:
    """Build a deterministic, self-contained report for one completed experiment."""
    experiment_dir, _, results, record = _load_artifacts(runs_dir, experiment_id)
    plots = _plots(experiment_dir, experiment_id, results)
    artifacts = ["experiment_manifest.json", "results.json"]
    results_csv = _inside(
        experiment_dir / "results.csv",
        experiment_dir,
        "results CSV must remain inside the experiment directory",
    )
    if results_csv.is_file():
        artifacts.append("results.csv")
    document = _document(experiment_id, results, record, plots, artifacts)
    report_path = _inside(
        experiment_dir / REPORT_FILENAME,
        experiment_dir,
        "report must remain inside the experiment directory",
    )
    temporary = report_path.with_name(f".{REPORT_FILENAME}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(document, encoding="utf-8", newline="\n")
        os.replace(temporary, report_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    traces = [trace for plot in plots for trace in plot["traces"]]
    return {
        "experiment_id": experiment_id,
        "report_html": str(report_path),
        "plot_count": len(plots),
        "trace_count": len(traces),
        "source_points": sum(int(trace["source_points"]) for trace in traces),
        "displayed_points": sum(int(trace["displayed_points"]) for trace in traces),
    }
