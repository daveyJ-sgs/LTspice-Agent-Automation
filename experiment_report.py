"""Portable offline HTML reports for structured LTspice experiments."""

from __future__ import annotations

import base64
import html
import json
import math
import os
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote

import experiment_index
import local_sensitivity
import raw_parser
import sensitivity_analysis
import statistical_results
import worst_case_analysis

DISPLAY_POINT_LIMIT = 400
MAX_TRACE_COUNT = 100
MAX_DISPLAYED_POINTS = 40_000
MAX_ANALYSIS_ROWS = statistical_results.MAX_ANALYSIS_ROWS
REPORT_FILENAME = "report.html"
REPORT_CONTEXT_FILENAME = "report_context.json"
MAX_CONTEXT_TEXT = 1_200
MAX_SCHEMATIC_BYTES = 2_000_000
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


class ReportContext(TypedDict, total=False):
    title: str
    circuit_summary: str
    simulation_summary: str
    mcp_context: str
    schematic_path: str
    schematic_caption: str


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> str:
    if isinstance(value, float):
        return format(value, ".8g")
    return str(value)


def _humanize(value: object) -> str:
    words = str(value).replace("_", " ").replace("-", " ").split()
    return " ".join("ADC" if word.lower() == "adc" else word.capitalize() for word in words)


def _float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _engineering(value: object, unit: str = "") -> str:
    number = _float(value)
    if number is None:
        return str(value)
    prefixes = {
        "F": ((1e-12, "pF"), (1e-9, "nF"), (1e-6, "µF"), (1.0, "F")),
        "s": ((1e-9, "ns"), (1e-6, "µs"), (1e-3, "ms"), (1.0, "s")),
        "Hz": ((1.0, "Hz"), (1e3, "kHz"), (1e6, "MHz"), (1e9, "GHz")),
        "ohm": ((1.0, "Ω"), (1e3, "kΩ"), (1e6, "MΩ")),
        "V": ((1e-6, "µV"), (1e-3, "mV"), (1.0, "V")),
    }
    choices = prefixes.get(unit)
    scale, label = 1.0, unit
    if choices:
        magnitude = abs(number)
        for candidate_scale, candidate_label in choices:
            if magnitude >= candidate_scale:
                scale, label = candidate_scale, candidate_label
    rendered = format(number / scale, ".4g")
    if "e" in rendered.lower() and 1e-3 <= abs(number / scale) < 1e5:
        rendered = f"{number / scale:.4f}".rstrip("0").rstrip(".")
    return f"{rendered} {label}".strip()


def _inside(path: Path, root: Path, message: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(message) from exc
    return resolved


def _validated_context(
    experiment_dir: Path,
    context: ReportContext | None,
) -> tuple[ReportContext, str | None]:
    context_path = experiment_dir / REPORT_CONTEXT_FILENAME
    if context is None and context_path.is_file() and not context_path.is_symlink():
        loaded = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("report context must contain a JSON object")
        context = loaded
    if context is None:
        return {}, None
    allowed = set(ReportContext.__annotations__)
    if set(context) - allowed:
        raise ValueError("report context contains unsupported fields")
    validated: ReportContext = {}
    for name, value in context.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"report context {name} must be a non-empty string")
        if len(value) > MAX_CONTEXT_TEXT:
            raise ValueError(f"report context {name} exceeds the text budget")
        validated[name] = value.strip()
    image_data = None
    image_reference = validated.get("schematic_path")
    if image_reference:
        project_root = Path(__file__).resolve().parent
        image_path = _inside(
            project_root / image_reference,
            project_root,
            "report schematic must remain inside the project directory",
        )
        if image_path.is_symlink() or not image_path.is_file():
            raise ValueError("report schematic must be a regular file")
        suffixes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        media_type = suffixes.get(image_path.suffix.lower())
        if media_type is None:
            raise ValueError("report schematic must be PNG or JPEG")
        data = image_path.read_bytes()
        if len(data) > MAX_SCHEMATIC_BYTES:
            raise ValueError("report schematic exceeds the image budget")
        image_data = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
    return validated, image_data


def _write_context(experiment_dir: Path, context: ReportContext) -> None:
    if not context:
        return
    path = _inside(
        experiment_dir / REPORT_CONTEXT_FILENAME,
        experiment_dir,
        "report context must remain inside the experiment directory",
    )
    temporary = path.with_name(f".{REPORT_CONTEXT_FILENAME}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(context, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _artifact_path(reference: object, experiment_dir: Path, experiment_id: str) -> tuple[Path, str]:
    return experiment_index.waveform_artifact_path(
        reference, experiment_dir, experiment_id
    )


def _load_artifacts(
    runs_dir: Path, experiment_id: str
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    return experiment_index.load_completed_experiment(runs_dir, experiment_id)


def _load_generated_json(experiment_dir: Path, filename: str) -> dict[str, object]:
    path = _inside(
        experiment_dir / filename,
        experiment_dir,
        "analysis artifact must remain inside the experiment directory",
    )
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{filename} must be a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{filename} must contain a JSON object")
    return value


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
    details: str,
    legend_label: str,
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
        "details": details,
        "legend_label": legend_label,
        "x": [x[index] for index in indices],
        "y": [y[index] for index in indices],
        "source_points": len(x),
        "displayed_points": len(indices),
        "raw_href": raw_href,
        "axis_label": axis_name.title(),
        "axis_unit": "Hz" if axis_name.lower() == "frequency" else "s" if axis_name.lower() == "time" else "",
        "y_label": y_label,
        "y_unit": "dB" if complex_trace else "V" if variable.lower().startswith("v(") else "",
        "log_x": axis_name.lower() == "frequency" and all(value > 0 for value in x),
        "variable": variable,
        "secondary_variable": secondary,
        "display_floor_db": display_floor_db,
    }


def _point_metadata(manifest: dict[str, object]) -> dict[int, dict[str, object]]:
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    entries = source.get("point_metadata") if isinstance(source, dict) else None
    if not isinstance(entries, list):
        return {}
    return {
        int(entry["index"]): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }


def _parameter_text(parameters: dict[str, object], units: dict[str, object]) -> str:
    return ", ".join(
        f"{name}={_engineering(value, str(units.get(name, '')))}"
        for name, value in sorted(parameters.items())
    )


def _plots(
    experiment_dir: Path,
    experiment_id: str,
    results: dict[str, object],
    manifest: dict[str, object],
) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    raw_cache: dict[Path, raw_parser.RawData] = {}
    points = results["points"]
    assert isinstance(points, list)
    metadata = _point_metadata(manifest)
    units = results.get("parameter_units", {})
    assert isinstance(units, dict)
    for point in sorted(points, key=lambda item: item["index"]):
        parameters = point["parameters"]
        point_index = int(point["index"])
        point_meta = metadata.get(point_index, {})
        corners = point_meta.get("corners", {})
        sample_index = point_meta.get("sample_index")
        corner_label = ", ".join(
            f"{_humanize(name)}: {_humanize(value)}" for name, value in corners.items()
        ) if isinstance(corners, dict) else ""
        sample_label = f"Sample {int(sample_index) + 1}" if isinstance(sample_index, int) else f"Point {point_index + 1}"
        label = " · ".join(value for value in (sample_label, corner_label) if value)
        legend_label = corner_label or label
        details = _parameter_text(parameters, units)
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
            trace = _trace(
                raw_cache[raw_path], analysis, label, details, legend_label, raw_href
            )
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


def _svg(
    plot: dict[str, object],
    plot_index: int,
    interactive: bool = True,
) -> str:
    traces = plot["traces"]
    assert isinstance(traces, list)
    log_x = bool(traces[0]["log_x"])
    axis_unit = str(traces[0]["axis_unit"])
    y_unit = str(traces[0]["y_unit"])
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
                f'<line class="grid-line grid-x" x1="{x_pixel:.2f}" y1="{_PLOT_TOP}" x2="{x_pixel:.2f}" y2="{_PLOT_TOP + height}"/>',
                f'<text class="tick tick-x" data-tick-index="{tick}" x="{x_pixel:.2f}" y="{_PLOT_TOP + height + 22}" text-anchor="middle">{_text(_engineering(x_value, axis_unit))}</text>',
                f'<line class="grid-line grid-y" x1="{_PLOT_LEFT}" y1="{y_pixel:.2f}" x2="{_PLOT_LEFT + width}" y2="{y_pixel:.2f}"/>',
                f'<text class="tick" x="{_PLOT_LEFT - 10}" y="{y_pixel + 4:.2f}" text-anchor="end">{_text(_engineering(y_value, y_unit))}</text>',
            ]
        )
    paths: list[str] = []
    legend: list[str] = []
    legend_colors: dict[str, str] = {}
    payload_traces: list[dict[str, object]] = []
    for index, trace in enumerate(traces):
        legend_label = str(trace["legend_label"])
        if legend_label not in legend_colors:
            legend_colors[legend_label] = _COLORS[len(legend_colors) % len(_COLORS)]
            legend.append(
                f'<span><i style="background:{legend_colors[legend_label]}"></i>{_text(legend_label)}</span>'
            )
        color = legend_colors[legend_label]
        screen = [[px(x), py(y)] for x, y in zip(trace["x"], trace["y"])]
        path_data = " ".join(
            ("M" if point_index == 0 else "L") + f"{point[0]:.2f},{point[1]:.2f}"
            for point_index, point in enumerate(screen)
        )
        paths.append(
            f'<path class="trace" d="{path_data}" stroke="{color}" data-trace-index="{index}" clip-path="url(#plot-clip-{plot_index})"/>'
        )
        payload_traces.append(
            {
                "label": trace["label"],
                "details": trace["details"],
                "color": color,
                "x": trace["x"],
                "y": trace["y"],
                "screen": screen,
            }
        )
    plot["payload"] = {
        "axis_label": traces[0]["axis_label"],
        "axis_unit": axis_unit,
        "y_label": traces[0]["y_label"],
        "y_unit": y_unit,
        "log_x": log_x,
        "x_domain": [x_min, x_max],
        "y_domain": [y_min, y_max],
        "bounds": {
            "left": _PLOT_LEFT,
            "top": _PLOT_TOP,
            "width": width,
            "height": height,
        },
        "traces": payload_traces,
    }
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
    trace_key = "".join(
        f"<li><strong>{_text(trace['label'])}</strong><br><span class=\"muted\">{_text(trace['details'])}</span></li>"
        for trace in traces
    )
    if not interactive:
        return f"""
<section class="panel plot-panel">
  <h2>{_text(_humanize(plot['name']))}</h2>
  <p class="muted">{len(traces)} full-resolution traces · {source_count}.{floor_note}</p>
  <div class="legend">{''.join(legend)}</div>
  <div class="plot-wrap">
    <svg class="plot" data-plot-index="{plot_index}" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" aria-label="{_text(plot['name'])} waveform overlay">
      <defs><clipPath id="plot-clip-{plot_index}"><rect x="{_PLOT_LEFT}" y="{_PLOT_TOP}" width="{width}" height="{height}"/></clipPath></defs>
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
    return f"""
<section class="panel plot-panel">
  <h2>{_text(_humanize(plot['name']))}</h2>
  <p class="muted">{len(traces)} full-resolution traces · {source_count}.{floor_note}</p>
  <div class="legend">{''.join(legend)}</div>
  <div class="plot-toolbar"><span class="muted">Drag horizontally to zoom · double-click to reset</span><button class="zoom-reset" type="button" disabled>Reset zoom</button></div>
  <div class="plot-layout">
    <div class="plot-wrap">
    <svg class="plot" data-plot-index="{plot_index}" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" aria-label="{_text(plot['name'])} interactive waveform overlay">
      <defs><clipPath id="plot-clip-{plot_index}"><rect x="{_PLOT_LEFT}" y="{_PLOT_TOP}" width="{width}" height="{height}"/></clipPath></defs>
      {''.join(grid)}
      <line class="axis" x1="{_PLOT_LEFT}" y1="{_PLOT_TOP + height}" x2="{_PLOT_LEFT + width}" y2="{_PLOT_TOP + height}"/>
      <line class="axis" x1="{_PLOT_LEFT}" y1="{_PLOT_TOP}" x2="{_PLOT_LEFT}" y2="{_PLOT_TOP + height}"/>
      {''.join(paths)}
      <rect class="zoom-selection" x="0" y="{_PLOT_TOP}" width="0" height="{height}" hidden/>
      <line class="cursor cursor-x" x1="0" y1="{_PLOT_TOP}" x2="0" y2="{_PLOT_TOP + height}" hidden/>
      <line class="cursor cursor-y" x1="{_PLOT_LEFT}" y1="0" x2="{_PLOT_LEFT + width}" y2="0" hidden/>
      <circle class="cursor-point" cx="0" cy="0" r="5" hidden/>
      <text class="axis-label" x="{_PLOT_LEFT + width / 2:.2f}" y="{_SVG_HEIGHT - 10}" text-anchor="middle">{_text(traces[0]['axis_label'])}</text>
      <text class="axis-label" transform="translate(18 {_PLOT_TOP + height / 2:.2f}) rotate(-90)" text-anchor="middle">{_text(traces[0]['y_label'])}</text>
    </svg>
    </div>
    <aside class="plot-inspector" aria-live="polite">
      <div class="inspector-kicker">Cursor inspector</div>
      <strong class="inspector-trace">Move over a trace</strong>
      <dl><div><dt>{_text(traces[0]['axis_label'])}</dt><dd class="inspector-x">—</dd></div><div><dt>{_text(traces[0]['y_label'])}</dt><dd class="inspector-y">—</dd></div></dl>
      <div class="inspector-parameters muted">Hover near a waveform to inspect its nearest simulated point.</div>
    </aside>
  </div>
  <details class="trace-key"><summary>Trace key and exact sample parameters ({len(traces)})</summary><ol>{trace_key}</ol></details>
</section>"""


def _table_rows(results: dict[str, object]) -> tuple[str, str]:
    point_rows: list[str] = []
    requirement_rows: list[str] = []
    points = results["points"]
    assert isinstance(points, list)
    units = results.get("parameter_units", {})
    assert isinstance(units, dict)
    for point in sorted(points, key=lambda item: item["index"]):
        parameters = _parameter_text(point["parameters"], units)
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
                    f'<td>{_text(_humanize(entry["name"]))}</td><td>{_text(_humanize(result["metric"]))}</td>'
                    f'<td>{_text(_engineering(result["value"], result["unit"]))}</td>'
                    f'<td>{_text(threshold["operator"])} {_text(_engineering(threshold["target"], threshold["unit"]))}</td>'
                    f'<td><span class="badge {state}">{state}</span></td></tr>'
                )
    return "".join(point_rows), "".join(requirement_rows)


def _parameter_summary(results: dict[str, object]) -> str:
    points = results["points"]
    assert isinstance(points, list)
    units = results.get("parameter_units", {})
    assert isinstance(units, dict)
    rows: list[str] = []
    names = sorted({name for point in points for name in point["parameters"]})
    for name in names:
        values = [point["parameters"][name] for point in points]
        numbers = [_float(value) for value in values]
        unit = str(units.get(name, ""))
        if all(value is not None for value in numbers):
            numeric = [float(value) for value in numbers if value is not None]
            unique = sorted(set(numeric))
            if len(unique) <= 4:
                summary = ", ".join(_engineering(value, unit) for value in unique)
            else:
                summary = f"{_engineering(min(numeric), unit)}–{_engineering(max(numeric), unit)}"
        else:
            unique_text = sorted(set(map(str, values)))
            summary = ", ".join(unique_text[:4])
            if len(unique_text) > 4:
                summary += f" + {len(unique_text) - 4} more"
        rows.append(f"<tr><td>{_text(name)}</td><td>{_text(summary)}</td></tr>")
    return "".join(rows)


def _narrative_panel(
    context: ReportContext,
    image_data: str | None,
    results: dict[str, object],
) -> str:
    title = context.get("title", "Structured LTspice experiment")
    circuit = context.get(
        "circuit_summary",
        "This report captures the circuit behavior and the requirements evaluated by the structured experiment.",
    )
    simulation = context.get(
        "simulation_summary",
        f"The run evaluated {results['point_count']} circuit points and retained traceable simulator evidence.",
    )
    mcp_context = context.get(
        "mcp_context",
        "The MCP turns the simulator run into repeatable plots, requirement decisions, and portable evidence for agent and human review.",
    )
    image = ""
    if image_data:
        caption = context.get(
            "schematic_caption",
            "Human-readable schematic view of the circuit represented by the automated netlist.",
        )
        image = (
            f'<figure><img src="{image_data}" alt="Circuit schematic for {_text(title)}">'
            f"<figcaption>{_text(caption)}</figcaption></figure>"
        )
    return f"""
<section class="panel narrative">
  <h2>{_text(title)}</h2>
  {image}
  <div class="story-grid">
    <div><h3>Circuit function</h3><p>{_text(circuit)}</p></div>
    <div><h3>Simulation performed</h3><p>{_text(simulation)}</p></div>
    <div><h3>Why this matters to the MCP</h3><p>{_text(mcp_context)}</p></div>
  </div>
</section>"""


def _statistical_panel(summary: dict[str, object] | None) -> str:
    if summary is None:
        return ""
    classifications = summary["classifications"]
    interval = summary["yield_confidence_interval"]
    observed = summary["observed_yield"]
    yield_text = "—" if observed is None else f"{100 * observed:.2f}%"
    interval_text = (
        "—"
        if interval["low"] is None
        else f"{100 * interval['low']:.2f}%–{100 * interval['high']:.2f}%"
    )
    corner_results = summary.get("corner_results", [])
    corner_rows = ""
    if corner_results:
        for corner in corner_results:
            corner_interval = corner["yield_confidence_interval"]
            corner_yield = corner["observed_yield"]
            corner_classifications = corner["classifications"]
            corner_state = (
                "pass"
                if corner_classifications["electrical_failure"] == 0
                and corner["invalid_points"] == 0
                else "fail"
            )
            corner_rows += (
                "<tr><td>"
                + _text(
                    ", ".join(
                        f"{name}={value}"
                        for name, value in corner["corners"].items()
                    )
                )
                + "</td><td>"
                + ("—" if corner_yield is None else f"{100 * corner_yield:.2f}%")
                + "</td><td>"
                + (
                    "—"
                    if corner_interval["low"] is None
                    else f"{100 * corner_interval['low']:.2f}%–"
                    f"{100 * corner_interval['high']:.2f}%"
                )
                + f"</td><td>{corner['evaluated_points']}</td>"
                + f"<td>{corner['invalid_points']}</td>"
                + f'<td><span class="badge {corner_state}">{corner_state}</span></td></tr>'
            )
    failed_row_items: list[str] = []
    for sample in summary["failed_samples"]:
        metadata_cells = ""
        if corner_results:
            corner_label = ", ".join(
                f"{name}={value}" for name, value in sample["corners"].items()
            )
            metadata_cells = (
                f'<td>{sample["sample_index"]}</td><td>{_text(corner_label)}</td>'
            )
        failed_row_items.append(
            f'<tr><td>{sample["index"]}</td>{metadata_cells}'
            f'<td><a href="point-{sample["index"]:04d}/">'
            f'point-{sample["index"]:04d}</a></td></tr>'
        )
    failed_rows = "".join(failed_row_items)
    if not failed_rows:
        failed_rows = (
            f'<tr><td colspan="{4 if corner_results else 2}" class="muted">'
            "No electrical failures.</td></tr>"
        )
    if corner_results:
        aggregate_label = (
            "Pooled yield"
            if summary.get("corner_aggregate") == "pooled"
            else "Aggregate"
        )
        aggregate_text = (
            yield_text
            if summary.get("corner_aggregate") == "pooled"
            else "Not requested"
        )
        aggregate_interval = (
            interval_text
            if summary.get("corner_aggregate") == "pooled"
            else "Not requested"
        )
        return f"""
<section class="panel"><h2>Operating-corner yield</h2>
<div class="cards">
  <div class="card"><span class="muted">{aggregate_label}</span><strong>{aggregate_text}</strong></div>
  <div class="card"><span class="muted">Pooled Wilson 95%</span><strong>{aggregate_interval}</strong></div>
  <div class="card"><span class="muted">Evaluated</span><strong>{summary['evaluated_points']}</strong></div>
  <div class="card"><span class="muted">Invalid / cancelled</span><strong>{summary['invalid_points']}</strong></div>
  <div class="card"><span class="muted">Named corners</span><strong>{len(corner_results)}</strong></div>
</div>
<div class="table-wrap"><table><thead><tr><th>Corner</th><th>Yield</th><th>Wilson 95% interval</th><th>Evaluated</th><th>Invalid</th><th>Status</th></tr></thead><tbody>{corner_rows}</tbody></table></div>
<h2>Failed samples</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Sample</th><th>Corner</th><th>Evidence</th></tr></thead><tbody>{failed_rows}</tbody></table></div>
</section>"""
    return f"""
<section class="panel"><h2>Statistical yield</h2>
<div class="cards">
  <div class="card"><span class="muted">Observed yield</span><strong>{yield_text}</strong></div>
  <div class="card"><span class="muted">Wilson 95% interval</span><strong>{interval_text}</strong></div>
  <div class="card"><span class="muted">Evaluated</span><strong>{summary['evaluated_points']}</strong></div>
  <div class="card"><span class="muted">Invalid / cancelled</span><strong>{summary['invalid_points']}</strong></div>
</div>
<p class="muted">Electrical pass {classifications['electrical_pass']} · electrical failure {classifications['electrical_failure']} · simulation error {classifications['simulation_error']} · analysis error {classifications['analysis_error']} · cancelled {classifications['cancelled']} · unfinished {classifications['unfinished']}</p>
<h2>Failed samples</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Evidence</th></tr></thead><tbody>{failed_rows}</tbody></table></div>
</section>"""


def _sampling_provenance_panel(summary: dict[str, object] | None) -> str:
    if summary is None:
        return ""
    provenance = summary.get("sampling_provenance")
    if not isinstance(provenance, dict):
        return ""
    method_labels = {
        "independent": "Independent random",
        "latin_hypercube": "Latin hypercube",
        "halton": "Scrambled Halton",
    }
    method = str(provenance["sampling_method"])
    plan_href = quote("../" + str(provenance["runs_relative_path"]), safe="/")
    return f"""
<section class="panel"><h2>Sampling provenance</h2>
<div class="cards">
  <div class="card"><span class="muted">Method</span><strong>{_text(method_labels[method])}</strong></div>
  <div class="card"><span class="muted">Generator</span><code>{_text(provenance['generator_version'])}</code></div>
  <div class="card"><span class="muted">Immutable plan</span><strong><a href="{plan_href}">{_text(provenance['plan_id'])}</a></strong></div>
</div>
<p class="muted">Plan SHA-256<br><code>{_text(provenance['plan_sha256'])}</code></p>
<p class="muted">Definition SHA-256<br><code>{_text(provenance['definition_hash'])}</code></p>
</section>"""


def _distribution_panel(summary: dict[str, object] | None) -> str:
    if summary is None:
        return ""
    rows: list[str] = []
    for name, statistics in sorted(summary.get("measurements", {}).items()):
        rows.append(_distribution_row(str(name), "measurement", "", statistics))
    for requirement in summary.get("requirement_margins", []):
        label = f"{requirement['analysis']} / {requirement['metric']}"
        rows.append(
            _distribution_row(
                label,
                "signed margin",
                str(requirement["unit"]),
                requirement["statistics"],
            )
        )
    if len(rows) > MAX_ANALYSIS_ROWS:
        raise ValueError("statistical distribution rows exceed report budget")
    if not rows:
        return ""
    return f"""
<section class="panel"><h2>Distribution summaries</h2>
<p class="muted">Structured descriptive statistics; charts do not replace the linked JSON/CSV evidence.</p>
<div class="table-wrap"><table><thead><tr><th>Name</th><th>Kind</th><th>Count</th><th>Min</th><th>P05</th><th>Median</th><th>P95</th><th>Max</th><th>Mean</th><th>Std dev</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def _distribution_row(
    name: str,
    kind: str,
    unit: str,
    statistics: dict[str, object],
) -> str:
    suffix = f" {_text(unit)}" if unit else ""
    values = [
        statistics["minimum"],
        statistics["p05"],
        statistics["p50"],
        statistics["p95"],
        statistics["maximum"],
        statistics["mean"],
        statistics["standard_deviation"],
    ]
    cells = "".join(f"<td>{_text(_number(value))}{suffix}</td>" for value in values)
    return (
        f"<tr><td>{_text(name)}</td><td>{_text(kind)}</td>"
        f"<td>{statistics['count']}</td>{cells}</tr>"
    )


def _worst_case_panel(analysis: dict[str, object] | None) -> str:
    if analysis is None:
        return ""
    rows: list[str] = []
    for requirement in analysis["requirements"]:
        cases = requirement["worst_cases"]
        if not cases:
            continue
        case = cases[0]
        corner = ", ".join(
            f"{name}={value}" for name, value in case.get("corners", {}).items()
        ) or "—"
        state = "pass" if case["passed"] else "fail"
        evidence = quote(str(case["evidence_path"]), safe="/")
        rows.append(
            f"<tr><td>{_text(requirement['analysis'])} / {_text(requirement['metric'])}</td>"
            f"<td>{_text(_number(case['margin']))} {_text(requirement['unit'])}</td>"
            f"<td>{_text(corner)}</td><td>{case['point_index']}</td>"
            f'<td><span class="badge {state}">{state}</span></td>'
            f'<td><a href="{evidence}">point-{case["point_index"]:04d}</a></td></tr>'
        )
    if len(rows) > MAX_ANALYSIS_ROWS:
        raise ValueError("worst-case rows exceed report budget")
    if not rows:
        return ""
    return f"""
<section class="panel"><h2>Worst evidenced cases</h2>
<div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Worst margin</th><th>Corner</th><th>Point</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def _sensitivity_panel(analysis: dict[str, object] | None) -> str:
    if analysis is None:
        return ""
    projected_rows = sum(
        len(scope["variables"])
        for requirement in analysis["requirements"]
        for scope in requirement["scopes"]
    )
    if projected_rows > MAX_ANALYSIS_ROWS:
        raise ValueError("sensitivity rows exceed report budget")
    rows: list[str] = []
    for requirement in analysis["requirements"]:
        label = f"{requirement['analysis']} / {requirement['metric']}"
        for scope in requirement["scopes"]:
            corner = ", ".join(
                f"{name}={value}" for name, value in scope["corners"].items()
            ) or "all samples"
            for variable in scope["variables"]:
                rho = variable.get("rho")
                rank = variable.get("rank")
                rows.append(
                    f"<tr><td>{_text(label)}</td><td>{_text(corner)}</td>"
                    f"<td>{'—' if rank is None else rank}</td><td>{_text(variable['variable'])}</td>"
                    f"<td>{'—' if rho is None else _text(_number(rho))}</td>"
                    f"<td>{_text(variable['status'])}</td>"
                    f"<td>{_text(', '.join(variable['correlated_with']) or '—')}</td></tr>"
                )
    if not rows:
        return ""
    return f"""
<section class="panel"><h2>Global rank sensitivity</h2>
<p class="muted">Spearman association with signed margin; descriptive, not causal.</p>
<div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Corner</th><th>Rank</th><th>Variable</th><th>Rho</th><th>Status</th><th>Correlated with</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def _tornado_panel(analysis: dict[str, object] | None) -> str:
    if analysis is None:
        return ""
    rows: list[str] = []
    for requirement in analysis["requirements"]:
        label = f"{requirement['analysis']} / {requirement['metric']}"
        for effect in requirement["effects"]:
            rank = effect.get("rank")
            low_effect = effect.get("low_effect")
            high_effect = effect.get("high_effect")
            impact = effect.get("impact")
            rows.append(
                f"<tr><td>{_text(label)}</td><td>{'—' if rank is None else rank}</td>"
                f"<td>{_text(effect['name'])}</td><td>{_text(effect['status'])}</td>"
                f"<td>{'—' if low_effect is None else _text(_number(low_effect))}</td>"
                f"<td>{'—' if high_effect is None else _text(_number(high_effect))}</td>"
                f"<td>{'—' if impact is None else _text(_number(impact))} {_text(requirement['unit'])}</td></tr>"
            )
    if len(rows) > MAX_ANALYSIS_ROWS:
        raise ValueError("tornado rows exceed report budget")
    if not rows:
        return ""
    return f"""
<section class="panel"><h2>Local OAT tornado data</h2>
<p class="muted">Controlled low/high perturbations around one evidenced point.</p>
<div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Rank</th><th>Variable</th><th>Status</th><th>Low effect</th><th>High effect</th><th>Impact</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def _document(
    experiment_id: str,
    results: dict[str, object],
    record: dict[str, object],
    plots: list[dict[str, object]],
    artifacts: list[str],
    context: ReportContext,
    image_data: str | None,
    statistical_summary: dict[str, object] | None = None,
    worst_cases: dict[str, object] | None = None,
    sensitivity: dict[str, object] | None = None,
    tornado: dict[str, object] | None = None,
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
    raw_links = sorted(
        {str(trace["raw_href"]) for plot in plots for trace in plot["traces"]}
    )
    raw_link_html = " · ".join(
        f'<a href="{quote(name, safe="/")}">{_text(name)}</a>' for name in raw_links
    )
    parameter_rows = _parameter_summary(results)
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
<title>{_text(context.get('title', experiment_id))}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1280px;margin:auto;padding:32px 24px 64px}} h1{{font-size:clamp(24px,4vw,38px);margin:.2rem 0;overflow-wrap:anywhere}} h2{{margin:0 0 10px;font-size:22px}} h3{{margin:0 0 6px;font-size:16px}}
a{{color:var(--blue)}} .eyebrow{{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.09em}} .muted{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}} .card,.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px}}
.card{{padding:16px}} .card strong{{display:block;font-size:25px}} .card code{{display:block;margin-top:6px}} .panel{{padding:20px;margin:18px 0;overflow:hidden}} code{{overflow-wrap:anywhere}}
.narrative figure{{margin:18px 0}} .narrative img{{display:block;width:100%;height:auto;border:1px solid var(--border);border-radius:8px;background:#c8c8c8}} figcaption{{margin-top:8px;color:var(--muted);font-size:13px}} .story-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} .story-grid p{{margin:0}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;text-transform:uppercase}} .badge.pass{{color:#aff5b4;background:#1b4721}} .badge.fail{{color:#ffdcd7;background:#5a1e1e}}
.table-wrap{{overflow:auto}} table{{border-collapse:collapse;width:100%;min-width:720px}} th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.plot-layout{{display:grid;grid-template-columns:minmax(0,4fr) minmax(220px,1fr);gap:14px;align-items:stretch}} .plot-wrap{{position:relative;min-width:0}} .plot{{display:block;width:100%;min-height:260px;background:#0b0f14;border:1px solid var(--border);border-radius:8px;cursor:crosshair;touch-action:none;user-select:none}}
.plot-toolbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 0}} button{{appearance:none;border:1px solid var(--border);border-radius:6px;background:#21262d;color:var(--text);padding:6px 10px;font:inherit;cursor:pointer}} button:hover:not(:disabled){{border-color:var(--blue)}} button:disabled{{opacity:.45;cursor:default}}
.plot-inspector{{border:1px solid var(--border);border-radius:8px;background:#0b0f14;padding:16px;min-width:0;overflow-wrap:anywhere}} .inspector-kicker{{color:var(--blue);font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px}} .inspector-trace{{display:block;font-size:16px;margin-bottom:14px}} .plot-inspector dl{{margin:0 0 14px}} .plot-inspector dl div{{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--border);padding:7px 0}} .plot-inspector dt{{color:var(--muted)}} .plot-inspector dd{{margin:0;font:600 13px ui-monospace,monospace;text-align:right}} .inspector-parameters{{font:12px/1.5 ui-monospace,monospace}}
.grid-line{{stroke:#242b35;stroke-width:1}} .axis{{stroke:#768390;stroke-width:1.25}} .tick{{fill:var(--muted);font-size:11px}} .axis-label{{fill:var(--text);font-size:12px}} .trace{{fill:none;stroke-width:2.2;vector-effect:non-scaling-stroke}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}} .legend span{{display:flex;align-items:center;gap:6px}} .legend i{{display:inline-block;width:18px;height:3px}}
.trace-key{{margin-top:12px}} .trace-key ol{{columns:2;column-gap:32px;padding-left:22px}} .trace-key li{{break-inside:avoid;margin:0 0 9px}} details>summary{{cursor:pointer;font-weight:650;color:var(--blue)}} .evidence-appendix>details>summary{{font-size:18px}}
.cursor{{stroke:#c9d1d9;stroke-width:1;stroke-dasharray:4 4;pointer-events:none}} .cursor-point{{stroke:#fff;stroke-width:1.5;pointer-events:none}} .zoom-selection{{fill:#58a6ff33;stroke:var(--blue);stroke-width:1;pointer-events:none}} .cursor[hidden],.cursor-point[hidden],.zoom-selection[hidden]{{display:none}}
@media(max-width:900px){{.plot-layout{{grid-template-columns:1fr}}.plot-inspector{{min-height:150px}}}}
@media(max-width:760px){{main{{padding:20px 12px}}.panel{{padding:12px}}.story-grid{{grid-template-columns:1fr}}.trace-key ol{{columns:1}}.plot-toolbar{{align-items:flex-start;flex-direction:column}}}}
</style>
</head>
<body><main>
<div class="eyebrow">LTspice structured experiment</div>
<h1>{_text(context.get('title', experiment_id))}</h1>
<p><span class="badge {state}">{state}</span> · {_text(record['execution_mode'])} execution · Recorded {_text(record['recorded_at'])}</p>
{_narrative_panel(context, image_data, results)}
<div class="cards">
  <div class="card"><span class="muted">Points</span><strong>{results['point_count']}</strong></div>
  <div class="card"><span class="muted">Passed</span><strong>{results['passed_points']}</strong></div>
  <div class="card"><span class="muted">Failed</span><strong>{results['failed_points']}</strong></div>
  <div class="card"><span class="muted">Plots</span><strong>{len(plots)}</strong></div>
</div>
{plot_html}
{_statistical_panel(statistical_summary)}
<section class="panel"><h2>Design-space summary</h2><p class="muted">Compact ranges across all {results['point_count']} simulated points. Exact values remain in the evidence appendix.</p><div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Values / range</th></tr></thead><tbody>{parameter_rows}</tbody></table></div></section>
<section class="panel"><details><summary>Engineering analysis details</summary>
{_distribution_panel(statistical_summary)}
{_worst_case_panel(worst_cases)}
{_sensitivity_panel(sensitivity)}
{_tornado_panel(tornado)}
</details></section>
<section class="panel evidence-appendix"><details><summary>Evidence and complete run data</summary>
<p class="muted">Structured artifacts: {links}</p>
<p class="muted">Full-resolution LTspice RAW waveforms: {raw_link_html}</p>
{_sampling_provenance_panel(statistical_summary)}
<section><h2>Requirement results</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Parameters</th><th>Analysis</th><th>Metric</th><th>Value</th><th>Requirement</th><th>Status</th></tr></thead><tbody>{requirement_rows}{no_requirements}</tbody></table></div></section>
<section><h2>Experiment points</h2><div class="table-wrap"><table><thead><tr><th>Point</th><th>Parameters</th><th>Measurements</th><th>Errors</th><th>Status</th></tr></thead><tbody>{point_rows}</tbody></table></div></section>
</details></section>
</main>
<script id="report-data" type="application/json">{payload}</script>
<script>
const plots=JSON.parse(document.getElementById("report-data").textContent);
const formatEngineering=(value,unit)=>{{
  const scales={{Hz:[[1e9,"GHz"],[1e6,"MHz"],[1e3,"kHz"],[1,"Hz"]],s:[[1,"s"],[1e-3,"ms"],[1e-6,"µs"],[1e-9,"ns"]],V:[[1,"V"],[1e-3,"mV"],[1e-6,"µV"]]}};
  let scale=1,label=unit;if(scales[unit]){{for(const candidate of scales[unit]){{if(Math.abs(value)>=candidate[0]){{[scale,label]=candidate;break;}}}}}}
  return `${{Number((value/scale).toPrecision(4))}}${{label?" "+label:""}}`;
}};
document.querySelectorAll("svg.plot").forEach(svg=>{{
  const plot=plots[Number(svg.dataset.plotIndex)];
  const panel=svg.closest(".plot-panel"), inspector=panel.querySelector(".plot-inspector"), reset=panel.querySelector(".zoom-reset");
  const cursorX=svg.querySelector(".cursor-x"), cursorY=svg.querySelector(".cursor-y"), marker=svg.querySelector(".cursor-point"), selection=svg.querySelector(".zoom-selection");
  const traceName=inspector.querySelector(".inspector-trace"), inspectorX=inspector.querySelector(".inspector-x"), inspectorY=inspector.querySelector(".inspector-y"), parameters=inspector.querySelector(".inspector-parameters");
  const bounds=plot.bounds, original=[...plot.x_domain], state={{min:plot.x_domain[0],max:plot.x_domain[1],dragStart:null}};
  const transform=value=>plot.log_x?Math.log10(value):value, inverse=value=>plot.log_x?10**value:value;
  const clamp=value=>Math.max(bounds.left,Math.min(bounds.left+bounds.width,value));
  const localPoint=event=>{{const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;return point.matrixTransform(svg.getScreenCTM().inverse());}};
  const mapX=value=>bounds.left+(transform(value)-state.min)*bounds.width/(state.max-state.min);
  const mapY=value=>bounds.top+(plot.y_domain[1]-value)*bounds.height/(plot.y_domain[1]-plot.y_domain[0]);
  const render=()=>{{
    plot.traces.forEach((trace,index)=>{{
      trace.screen=trace.x.map((value,pointIndex)=>[mapX(value),mapY(trace.y[pointIndex])]);
      const path=trace.screen.map((point,pointIndex)=>`${{pointIndex?"L":"M"}}${{point[0].toFixed(2)}},${{point[1].toFixed(2)}}`).join(" ");
      svg.querySelector(`path[data-trace-index="${{index}}"]`).setAttribute("d",path);
    }});
    svg.querySelectorAll(".tick-x").forEach((tick,index)=>{{
      const transformed=state.min+index*(state.max-state.min)/5;
      tick.textContent=formatEngineering(inverse(transformed),plot.axis_unit);
    }});
    reset.disabled=Math.abs(state.min-original[0])<1e-12&&Math.abs(state.max-original[1])<1e-12;
  }};
  const hideCursor=()=>{{cursorX.setAttribute("hidden","");cursorY.setAttribute("hidden","");marker.setAttribute("hidden","");}};
  const inspect=local=>{{
    if(local.x<bounds.left||local.x>bounds.left+bounds.width||local.y<bounds.top||local.y>bounds.top+bounds.height){{hideCursor();return;}}
    let nearest=null;
    plot.traces.forEach(trace=>trace.screen.forEach((screen,index)=>{{
      if(screen[0]<bounds.left||screen[0]>bounds.left+bounds.width)return;
      const distance=Math.hypot(screen[0]-local.x,screen[1]-local.y);
      if(!nearest||distance<nearest.distance)nearest={{distance,trace,index,screen}};
    }}));
    if(!nearest)return;
    cursorX.removeAttribute("hidden");cursorY.removeAttribute("hidden");marker.removeAttribute("hidden");
    cursorX.setAttribute("x1",nearest.screen[0]);cursorX.setAttribute("x2",nearest.screen[0]);
    cursorY.setAttribute("y1",nearest.screen[1]);cursorY.setAttribute("y2",nearest.screen[1]);
    marker.setAttribute("cx",nearest.screen[0]);marker.setAttribute("cy",nearest.screen[1]);marker.setAttribute("fill",nearest.trace.color);
    traceName.textContent=nearest.trace.label;traceName.style.color=nearest.trace.color;
    inspectorX.textContent=formatEngineering(nearest.trace.x[nearest.index],plot.axis_unit);
    inspectorY.textContent=formatEngineering(nearest.trace.y[nearest.index],plot.y_unit);
    parameters.textContent=nearest.trace.details;
  }};
  svg.addEventListener("pointerdown",event=>{{
    if(event.button!==0)return;const local=localPoint(event);
    if(local.x<bounds.left||local.x>bounds.left+bounds.width||local.y<bounds.top||local.y>bounds.top+bounds.height)return;
    state.dragStart=clamp(local.x);selection.removeAttribute("hidden");selection.setAttribute("x",state.dragStart);selection.setAttribute("width",0);svg.setPointerCapture(event.pointerId);event.preventDefault();
  }});
  svg.addEventListener("pointermove",event=>{{
    const local=localPoint(event);
    if(state.dragStart!==null){{const current=clamp(local.x);selection.setAttribute("x",Math.min(state.dragStart,current));selection.setAttribute("width",Math.abs(current-state.dragStart));return;}}
    inspect(local);
  }});
  svg.addEventListener("pointerup",event=>{{
    if(state.dragStart===null)return;const finish=clamp(localPoint(event).x), start=state.dragStart;state.dragStart=null;selection.setAttribute("hidden","");
    if(Math.abs(finish-start)>=8){{const span=state.max-state.min,newMin=state.min+(Math.min(start,finish)-bounds.left)*span/bounds.width,newMax=state.min+(Math.max(start,finish)-bounds.left)*span/bounds.width;state.min=newMin;state.max=newMax;render();hideCursor();}}
    if(svg.hasPointerCapture(event.pointerId))svg.releasePointerCapture(event.pointerId);
  }});
  svg.addEventListener("pointercancel",event=>{{state.dragStart=null;selection.setAttribute("hidden","");if(svg.hasPointerCapture(event.pointerId))svg.releasePointerCapture(event.pointerId);}});
  svg.addEventListener("pointerleave",()=>{{if(state.dragStart===null)hideCursor();}});
  const resetZoom=()=>{{state.min=original[0];state.max=original[1];render();hideCursor();}};
  reset.addEventListener("click",resetZoom);svg.addEventListener("dblclick",event=>{{event.preventDefault();resetZoom();}});
  render();
}});
</script>
</body></html>
"""


def build_experiment_report(
    runs_dir: Path,
    experiment_id: str,
    report_context: ReportContext | None = None,
) -> ExperimentReportResult:
    """Build a deterministic, self-contained report for one completed experiment."""
    experiment_dir, manifest, results, record = _load_artifacts(runs_dir, experiment_id)
    context, image_data = _validated_context(experiment_dir, report_context)
    plots = _plots(experiment_dir, experiment_id, results, manifest)
    artifacts = ["experiment_manifest.json", "results.json"]
    if context:
        artifacts.append(REPORT_CONTEXT_FILENAME)
    results_csv = _inside(
        experiment_dir / "results.csv",
        experiment_dir,
        "results CSV must remain inside the experiment directory",
    )
    if results_csv.is_file():
        artifacts.append("results.csv")
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    source = point_plan.get("source") if isinstance(point_plan, dict) else None
    statistical_summary = None
    worst_cases = None
    sensitivity = None
    tornado = None
    if isinstance(source, dict) and source.get("kind") == "statistical":
        statistical_results.summarize_statistical_experiment(runs_dir, experiment_id)
        worst_case_analysis.analyze_statistical_worst_cases(runs_dir, experiment_id)
        sensitivity_analysis.analyze_statistical_sensitivity(runs_dir, experiment_id)
        statistical_summary = _load_generated_json(experiment_dir, "statistics.json")
        worst_cases = _load_generated_json(experiment_dir, "worst_cases.json")
        sensitivity = _load_generated_json(experiment_dir, "sensitivity.json")
        artifacts.extend(
            [
                "statistics.json",
                "statistics.csv",
                "worst_cases.json",
                "worst_cases.csv",
                "sensitivity.json",
                "sensitivity.csv",
            ]
        )
    elif isinstance(source, dict) and source.get("kind") == "local_sensitivity":
        local_sensitivity.analyze_local_sensitivity(runs_dir, experiment_id)
        tornado = _load_generated_json(experiment_dir, "tornado.json")
        artifacts.extend(["tornado.json", "tornado.csv"])
    document = _document(
        experiment_id,
        results,
        record,
        plots,
        artifacts,
        context,
        image_data,
        statistical_summary,
        worst_cases,
        sensitivity,
        tornado,
    )
    report_path = _inside(
        experiment_dir / REPORT_FILENAME,
        experiment_dir,
        "report must remain inside the experiment directory",
    )
    temporary = report_path.with_name(f".{REPORT_FILENAME}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(document, encoding="utf-8", newline="\n")
        os.replace(temporary, report_path)
        _write_context(experiment_dir, context)
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
