#!/usr/bin/env python3
"""MCP server exposing the LTspice automation toolkit as agent tools.

Wraps ltspice_wrapper.py, raw_parser.py, and report_runs.py directly (no
separate REST process required) so an MCP client can run netlists, pull
measurements and waveform data, sweep a parameter, and browse run history.
Waveform requirements are evaluated against full-resolution parsed vectors.

Run directly for local stdio use:

    .venv/bin/python mcp_server.py

Or register with Claude Code:

    claude mcp add ltspice -- /path/to/automation/.venv/bin/python \\
        /path/to/automation/mcp_server.py
"""

from __future__ import annotations

import csv
import itertools
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict

from mcp.server.mcpserver import MCPServer

import frequency_domain_metrics
import ltspice_wrapper as wrapper
import raw_parser
import report_runs
import waveform_metrics

PROJECT_DIR = wrapper.PROJECT_DIR
RUNS_DIR = wrapper.RUNS_DIR
EXAMPLES_DIR = PROJECT_DIR / "examples"
MAX_EXPERIMENT_POINTS = 1000

mcp = MCPServer(
    name="ltspice",
    instructions=(
        "Run LTspice circuit simulations in batch mode and inspect the "
        "results. Provide a netlist as text (.cir-style) or a path to an "
        "existing netlist file; text netlists are the reliable automation "
        "boundary. Every run returns a run_dir that later tools accept to "
        "pull measurements or waveform data. Structured experiments expand "
        "ordered parameter sets and reuse waveform requirements at every point."
    ),
)


class WaveformAnalysisResult(TypedDict):
    raw_file: str
    variable: str
    axis_variable: str
    step_index: int
    source_points: int
    analysis_resolution: str
    all_passed: bool
    results: list[waveform_metrics.RequirementResult]
    secondary_variable: str | None


class ExperimentParameter(TypedDict):
    name: str
    values: list[str]
    unit: NotRequired[str]


class ExperimentWaveformAnalysis(TypedDict):
    name: str
    variable: str
    requirements: list[waveform_metrics.WaveformRequirement]
    axis_variable: NotRequired[str]
    secondary_variable: NotRequired[str]
    signal_unit: NotRequired[str]
    axis_unit: NotRequired[str]
    raw_filename: NotRequired[str]


class ExperimentAnalysisResult(TypedDict):
    name: str
    status: str
    error: str | None
    analysis: WaveformAnalysisResult | None


class ExperimentPointResult(TypedDict):
    index: int
    parameters: dict[str, str]
    run_dir: str
    simulation_status: str
    duration_seconds: float | None
    measurements: dict[str, float]
    analyses: list[ExperimentAnalysisResult]
    all_passed: bool
    error: str | None


class ExperimentResult(TypedDict):
    experiment_id: str
    experiment_dir: str
    manifest: str
    results_json: str
    results_csv: str
    status: str
    parameter_order: list[str]
    parameter_units: dict[str, str]
    point_count: int
    completed_points: int
    error_points: int
    passed_points: int
    failed_points: int
    all_passed: bool
    points: list[ExperimentPointResult]


# --- internal helpers -------------------------------------------------


def _series_to_json(values: list[float | complex]) -> list[float] | dict[str, list[float]]:
    if values and isinstance(values[0], complex):
        return {"real": [v.real for v in values], "imag": [v.imag for v in values]}
    return list(values)


def _within_runs(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(RUNS_DIR.resolve())
    except ValueError as exc:
        raise ValueError(f"Path must be inside the runs directory: {resolved}") from exc
    return resolved


def _netlist_filename(filename: str) -> str:
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError("filename must be a plain file name without directories")
    if not filename.lower().endswith((".cir", ".net")):
        filename += ".cir"
    return filename


def _validate_timeout(timeout_seconds: int) -> None:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")


def _resolve_run_dir(run_dir: str) -> Path:
    """Accept an in-tree absolute path or a run_dir name relative to runs/."""
    path = Path(run_dir).expanduser()
    if not path.is_absolute():
        path = RUNS_DIR / path
    path = _within_runs(path)
    if not path.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")
    return path


def _find_raw(run_dir: Path, raw_filename: str | None) -> Path:
    if raw_filename:
        if Path(raw_filename).name != raw_filename or "/" in raw_filename or "\\" in raw_filename:
            raise ValueError("raw_filename must be a plain file name")
        candidate = run_dir / raw_filename
        if not candidate.is_file():
            raise FileNotFoundError(f"Raw file not found: {candidate}")
        return candidate
    raw_files = sorted(run_dir.glob("*.raw"))
    if not raw_files:
        raise FileNotFoundError(f"No .raw file found in {run_dir}")
    # Prefer a transient/AC/step result over a bias-point (.op.raw) file.
    primary = [path for path in raw_files if not path.name.endswith(".op.raw")]
    return (primary or raw_files)[0]


def _summarize_run(output_dir: Path) -> dict[str, object]:
    manifest = json.loads((output_dir / "run_manifest.json").read_text())
    measurements: dict[str, float] = {}
    for log_path in sorted(output_dir.glob("*.log")):
        try:
            measurements.update(wrapper.parse_measurements(log_path))
        except (OSError, UnicodeError, ValueError):
            pass
    return {
        "run_dir": str(output_dir),
        "run_id": output_dir.name,
        "status": manifest.get("status"),
        "duration_seconds": manifest.get("duration_seconds"),
        "measurements": measurements,
        "result_files": manifest.get("result_files", []),
    }


def _run_netlist_text(
    netlist: str, filename: str, ascii_raw: bool, timeout_seconds: int, dest_dir: Path
) -> Path:
    filename = _netlist_filename(filename)
    with tempfile.TemporaryDirectory(prefix="mcp-input-") as tmp:
        source_path = Path(tmp) / filename
        source_path.write_text(netlist)
        return wrapper.run_netlist(
            source_path,
            output_dir=dest_dir,
            timeout_seconds=timeout_seconds,
            ascii_raw=ascii_raw,
        )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_experiment(
    netlist_template: str,
    parameters: list[ExperimentParameter],
    waveform_analyses: list[ExperimentWaveformAnalysis],
) -> tuple[list[str], list[dict[str, str]], dict[str, str]]:
    if not isinstance(netlist_template, str) or not netlist_template.strip():
        raise ValueError("netlist_template must be a non-empty string")
    if not isinstance(parameters, list) or not parameters:
        raise ValueError("parameters must be a non-empty list")
    if not isinstance(waveform_analyses, list):
        raise ValueError("waveform_analyses must be a list")

    names: list[str] = []
    value_sets: list[list[str]] = []
    units: dict[str, str] = {}
    point_count = 1
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise ValueError("parameters must contain objects")
        name = parameter.get("name")
        values = parameter.get("values")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("parameter names must match [A-Za-z_][A-Za-z0-9_]*")
        if name in names:
            raise ValueError(f"duplicate parameter name: {name}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"parameter {name} values must be a non-empty list")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"parameter {name} values must be non-empty strings")
        if len(set(values)) != len(values):
            raise ValueError(f"parameter {name} values must be unique")
        placeholder = "{" + name + "}"
        if placeholder not in netlist_template:
            raise ValueError(f"parameter placeholder {placeholder} is missing from netlist_template")
        unit = parameter.get("unit", "")
        if not isinstance(unit, str):
            raise ValueError(f"parameter {name} unit must be a string")
        names.append(name)
        value_sets.append(values)
        units[name] = unit
        point_count *= len(values)
        if point_count > MAX_EXPERIMENT_POINTS:
            raise ValueError(
                f"experiment is limited to {MAX_EXPERIMENT_POINTS} Cartesian points"
            )

    analysis_names: set[str] = set()
    supported_metrics = (
        waveform_metrics.SUPPORTED_METRICS | frequency_domain_metrics.SUPPORTED_METRICS
    )
    for analysis in waveform_analyses:
        if not isinstance(analysis, dict):
            raise ValueError("waveform_analyses must contain objects")
        name = analysis.get("name")
        variable = analysis.get("variable")
        requirements = analysis.get("requirements")
        if not isinstance(name, str) or not name:
            raise ValueError("waveform analysis names must be non-empty strings")
        if name in analysis_names:
            raise ValueError(f"duplicate waveform analysis name: {name}")
        if not isinstance(variable, str) or not variable:
            raise ValueError(f"waveform analysis {name} variable must be non-empty")
        if not isinstance(requirements, list) or not requirements:
            raise ValueError(f"waveform analysis {name} requirements must be non-empty")
        for field in (
            "axis_variable",
            "secondary_variable",
            "signal_unit",
            "axis_unit",
            "raw_filename",
        ):
            if field in analysis and not isinstance(analysis[field], str):
                raise ValueError(f"waveform analysis {name} {field} must be a string")
        raw_filename = analysis.get("raw_filename")
        if raw_filename is not None and (
            Path(raw_filename).name != raw_filename
            or "/" in raw_filename
            or "\\" in raw_filename
        ):
            raise ValueError(
                f"waveform analysis {name} raw_filename must be a plain file name"
            )
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise ValueError(f"waveform analysis {name} requirements must be objects")
            missing = [
                field for field in ("metric", "operator", "target") if field not in requirement
            ]
            if missing:
                raise ValueError(
                    f"waveform analysis {name} requirement is missing {missing[0]}"
                )
            if requirement["metric"] not in supported_metrics:
                raise ValueError(
                    f"waveform analysis {name} has unknown metric {requirement['metric']}"
                )
            if requirement["operator"] not in {"<", "<=", ">", ">="}:
                raise ValueError(
                    f"waveform analysis {name} has invalid comparison operator"
                )
            try:
                float(requirement["target"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"waveform analysis {name} requirement target must be numeric"
                ) from exc
        analysis_names.add(name)

    points = [
        dict(zip(names, combination))
        for combination in itertools.product(*value_sets)
    ]
    return names, points, units


def _render_experiment_netlist(
    netlist_template: str, parameters: dict[str, str]
) -> str:
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: parameters.get(match.group(1), match.group(0)),
        netlist_template,
    )


def _write_experiment_csv(
    path: Path,
    parameter_order: list[str],
    points: list[ExperimentPointResult],
) -> None:
    measurement_names = sorted(
        {
            name
            for point in points
            for name in point["measurements"]
        }
    )
    fieldnames = [
        "point_index",
        *(f"parameter.{name}" for name in parameter_order),
        "simulation_status",
        "all_passed",
        "duration_seconds",
        "run_dir",
        "error",
        *(f"measurement.{name}" for name in measurement_names),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    "point_index": point["index"],
                    **{
                        f"parameter.{name}": point["parameters"][name]
                        for name in parameter_order
                    },
                    "simulation_status": point["simulation_status"],
                    "all_passed": point["all_passed"],
                    "duration_seconds": point["duration_seconds"],
                    "run_dir": point["run_dir"],
                    "error": point["error"],
                    **{
                        f"measurement.{name}": value
                        for name, value in point["measurements"].items()
                    },
                }
            )


# --- tools --------------------------------------------------------------


@mcp.tool()
def run_netlist(
    netlist: str,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """Run LTspice in batch mode on netlist text and return status and measurements.

    `netlist` is the full text of a .cir/.net deck (SPICE directives such as
    .ac/.tran/.step and .meas lines). Raises with LTspice's diagnostic output
    if the simulation fails or times out. The returned run_dir can be passed
    to get_measurements, get_waveform, or export_waveform_csv.
    """
    _validate_timeout(timeout_seconds)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output_dir = _run_netlist_text(
        netlist, filename, ascii_raw, timeout_seconds, RUNS_DIR / f"mcp-{stamp}"
    )
    return _summarize_run(output_dir)


@mcp.tool()
def run_netlist_file(
    path: str,
    output_dir: str | None = None,
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """Run an existing .cir/.net file; any explicit output_dir must be under runs/."""
    _validate_timeout(timeout_seconds)
    netlist_path = Path(path).expanduser()
    if netlist_path.suffix.lower() not in (".cir", ".net"):
        raise ValueError("path must identify a .cir or .net file")
    resolved_output = _within_runs(Path(output_dir)) if output_dir else None
    result_dir = wrapper.run_netlist(
        netlist_path,
        output_dir=resolved_output,
        timeout_seconds=timeout_seconds,
        ascii_raw=ascii_raw,
    )
    return _summarize_run(result_dir)


@mcp.tool()
def get_measurements(run_dir: str) -> dict[str, float]:
    """Return the scalar .meas values recorded in a run's log file(s)."""
    resolved = _resolve_run_dir(run_dir)
    logs = sorted(resolved.glob("*.log"))
    if not logs:
        raise FileNotFoundError(f"No .log file found in {resolved}")
    measurements: dict[str, float] = {}
    for log_path in logs:
        measurements.update(wrapper.parse_measurements(log_path))
    return measurements


@mcp.tool()
def get_waveform(
    run_dir: str,
    variables: list[str] | None = None,
    max_points: int = 200,
    raw_filename: str | None = None,
) -> dict[str, object]:
    """Return (optionally downsampled) waveform vectors from a run's .raw file.

    Set `variables` to a subset of vector names (see the returned "variables"
    list on a first call) to limit the payload. `max_points` evenly
    downsamples each vector; set it high (or to the reported total_points)
    for full resolution.
    """
    if not isinstance(max_points, int) or isinstance(max_points, bool) or max_points <= 0:
        raise ValueError("max_points must be a positive integer")
    resolved = _resolve_run_dir(run_dir)
    raw_path = _find_raw(resolved, raw_filename)
    data = raw_parser.parse_raw(raw_path)

    wanted = variables or data.variables
    unknown = sorted(set(wanted) - set(data.variables))
    if unknown:
        raise KeyError(f"Unknown variable(s) {unknown}; available: {data.variables}")

    total_points = data.points
    if total_points <= max_points:
        indices = range(total_points)
    else:
        step = total_points / max_points
        indices = [int(i * step) for i in range(max_points)]

    series = {
        name: _series_to_json([data.values[name][i] for i in indices]) for name in wanted
    }
    return {
        "raw_file": str(raw_path),
        "flags": data.flags,
        "variables": data.variables,
        "step_count": data.step_count,
        "points_per_step": data.points_per_step,
        "total_points": total_points,
        "returned_points": len(indices),
        "data": series,
    }


@mcp.tool()
def analyze_waveform(
    run_dir: str,
    variable: str,
    requirements: list[waveform_metrics.WaveformRequirement],
    axis_variable: str | None = None,
    step_index: int | None = None,
    signal_unit: str = "",
    axis_unit: str | None = None,
    raw_filename: str | None = None,
    secondary_variable: str | None = None,
) -> WaveformAnalysisResult:
    """Evaluate full-resolution waveform requirements for one real-valued vector.

    Each requirement needs `metric`, `operator`, and `target`. Supported metrics
    include scalar, time-domain, paired-signal, spectral, and AC checks. Each
    requirement can select a closed axis window. Paired and transfer-function
    metrics use the optional secondary_variable.
    Stepped raw files require an explicit zero-based step_index.
    """
    if not requirements:
        raise ValueError("requirements must be a non-empty list")
    resolved = _resolve_run_dir(run_dir)
    raw_path = _find_raw(resolved, raw_filename)
    data = raw_parser.parse_raw(raw_path)
    axis_name = axis_variable or data.variables[0]
    requested_variables = {variable, axis_name}
    if secondary_variable is not None:
        requested_variables.add(secondary_variable)
    unknown = sorted(requested_variables - set(data.variables))
    if unknown:
        raise KeyError(f"Unknown variable(s) {unknown}; available: {data.variables}")

    if data.step_count > 1:
        if step_index is None:
            raise ValueError("step_index is required for stepped waveform data")
        slices = raw_parser.step_slices(data)
        if (
            not isinstance(step_index, int)
            or isinstance(step_index, bool)
            or not 0 <= step_index < len(slices)
        ):
            raise IndexError(f"step_index must be between 0 and {len(slices) - 1}")
        selected = slices[step_index]
    else:
        invalid_type = not isinstance(step_index, int) or isinstance(step_index, bool)
        if step_index is not None and (invalid_type or step_index != 0):
            raise IndexError("step_index must be 0 for an unstepped waveform")
        selected = slice(0, data.points)

    axis = data.values[axis_name][selected]
    values = data.values[variable][selected]
    secondary_values = (
        None if secondary_variable is None else data.values[secondary_variable][selected]
    )
    resolved_axis_unit = axis_unit
    if resolved_axis_unit is None:
        resolved_axis_unit = "Hz" if axis_name.lower() == "frequency" else "s"
    results: list[waveform_metrics.RequirementResult] = []
    waveform_numeric_parameters = {
        "initial_value",
        "final_value",
        "low_fraction",
        "high_fraction",
        "settling_tolerance",
        "window_start",
        "window_end",
        "threshold_value",
        "primary_threshold",
        "secondary_threshold",
        "forbidden_min",
        "forbidden_max",
        "secondary_forbidden_min",
        "secondary_forbidden_max",
    }
    frequency_numeric_parameters = {
        "window_start",
        "window_end",
        "threshold_value",
        "frequency_min",
        "frequency_max",
        "frequency_resolution",
        "fundamental_frequency",
        "frequency_value",
        "reference_frequency",
        "cutoff_drop_db",
    }
    waveform_string_parameters = {
        "polarity",
        "primary_edge",
        "secondary_edge",
        "direction",
    }
    frequency_string_parameters = {"edge", "direction"}
    for requirement in requirements:
        try:
            metric = str(requirement["metric"])
            operator = str(requirement["operator"])
            target = float(requirement["target"])
        except KeyError as exc:
            raise ValueError(f"requirement is missing {exc.args[0]}") from exc
        all_numeric_parameters = waveform_numeric_parameters | frequency_numeric_parameters
        parameters: dict[str, float | int | str] = {
            name: float(requirement[name])
            for name in all_numeric_parameters
            if name in requirement
        }
        if "maximum_harmonic" in requirement:
            harmonic = requirement["maximum_harmonic"]
            if not isinstance(harmonic, int) or isinstance(harmonic, bool):
                raise ValueError("maximum_harmonic must be an integer")
            parameters["maximum_harmonic"] = harmonic
        all_string_parameters = waveform_string_parameters | frequency_string_parameters
        parameters.update(
            {
                name: str(requirement[name])
                for name in all_string_parameters
                if name in requirement
            }
        )
        if metric in frequency_domain_metrics.SUPPORTED_METRICS:
            accepted = (
                frequency_numeric_parameters
                | frequency_string_parameters
                | {"maximum_harmonic"}
            )
            metric_parameters = {
                name: value for name, value in parameters.items() if name in accepted
            }
            measurement = frequency_domain_metrics.measure_metric(
                axis,
                values,
                metric,
                secondary_values=secondary_values,
                signal_unit=signal_unit,
                axis_unit=resolved_axis_unit,
                **metric_parameters,
            )
        else:
            accepted = waveform_numeric_parameters | waveform_string_parameters
            metric_parameters = {
                name: value for name, value in parameters.items() if name in accepted
            }
            measurement = waveform_metrics.measure_metric(
                axis,
                values,
                metric,
                secondary_values=secondary_values,
                signal_unit=signal_unit,
                axis_unit=resolved_axis_unit,
                **metric_parameters,
            )
        results.append(waveform_metrics.evaluate_requirement(measurement, operator, target))

    response: WaveformAnalysisResult = {
        "raw_file": str(raw_path),
        "variable": variable,
        "axis_variable": axis_name,
        "step_index": 0 if step_index is None else step_index,
        "source_points": len(values),
        "analysis_resolution": "full",
        "all_passed": all(result["passed"] for result in results),
        "results": results,
        "secondary_variable": secondary_variable,
    }
    return response


@mcp.tool()
def export_waveform_csv(run_dir: str, raw_filename: str | None = None) -> dict[str, object]:
    """Export a run's full-resolution .raw waveform to CSV and return its path."""
    resolved = _resolve_run_dir(run_dir)
    raw_path = _find_raw(resolved, raw_filename)
    data = raw_parser.parse_raw(raw_path)
    csv_path = raw_path.with_suffix(".csv")
    raw_parser.export_csv(data, csv_path)
    return {"csv_path": str(csv_path), "rows": data.points, "variables": data.variables}


@mcp.tool()
def run_parameter_sweep(
    netlist_template: str,
    values: list[str],
    placeholder: str = "{value}",
    filename: str = "circuit.cir",
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """Run one LTspice simulation per value, substituting `placeholder` in the template.

    Example: netlist_template containing "R1 in out {value}" with
    values=["1k", "10k", "100k"] runs three independent simulations. Each
    point's status and .meas measurements are returned, along with a
    results.csv summarizing the whole sweep.
    """
    if not values:
        raise ValueError("values must be a non-empty list")
    if not placeholder or placeholder not in netlist_template:
        raise ValueError("placeholder must occur in netlist_template")
    _netlist_filename(filename)
    _validate_timeout(timeout_seconds)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    sweep_dir = RUNS_DIR / f"mcp-sweep-{stamp}"
    rows: list[dict[str, object]] = []
    for index, value in enumerate(values):
        rendered = netlist_template.replace(placeholder, str(value))
        point_dir = sweep_dir / f"point-{index:03d}"
        try:
            output_dir = _run_netlist_text(rendered, filename, False, timeout_seconds, point_dir)
            summary = _summarize_run(output_dir)
        except RuntimeError as exc:
            summary = {
                "run_dir": str(point_dir),
                "run_id": point_dir.name,
                "status": "failed",
                "measurements": {},
                "error": str(exc),
            }
        summary["value"] = value
        rows.append(summary)

    measurement_names = sorted({name for row in rows for name in row.get("measurements", {})})
    fieldnames = ["value", "status", "duration_seconds", "run_dir", *measurement_names]
    sweep_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sweep_dir / "results.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "value": row.get("value"),
                    "status": row.get("status"),
                    "duration_seconds": row.get("duration_seconds"),
                    "run_dir": row.get("run_dir"),
                    **row.get("measurements", {}),
                }
            )

    return {"sweep_dir": str(sweep_dir), "results_csv": str(csv_path), "points": rows}


@mcp.tool()
def run_experiment(
    netlist_template: str,
    parameters: list[ExperimentParameter],
    waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
) -> ExperimentResult:
    """Run a deterministic Cartesian experiment and reuse analyses at every point.

    Parameters are ordered records with a name, explicit string values, and an
    optional metadata-only unit. The corresponding template placeholder is
    `{name}`. Declaration order defines Cartesian order: the first parameter
    changes slowest and the last changes fastest. Execution is sequential in
    Phase 2A and is limited to 1,000 points.
    """
    normalized_filename = _netlist_filename(filename)
    _validate_timeout(timeout_seconds)
    analyses = [] if waveform_analyses is None else waveform_analyses
    parameter_order, combinations, parameter_units = _prepare_experiment(
        netlist_template,
        parameters,
        analyses,
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    experiment_id = f"mcp-experiment-{stamp}"
    experiment_dir = RUNS_DIR / experiment_id
    experiment_dir.mkdir(parents=True)
    manifest_path = experiment_dir / "experiment_manifest.json"
    results_json_path = experiment_dir / "results.json"
    results_csv_path = experiment_dir / "results.csv"
    started_at = datetime.now().astimezone()
    started_clock = time.monotonic()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "status": "running",
        "started_at": started_at.isoformat(),
        "definition": {
            "netlist_template": netlist_template,
            "parameters": parameters,
            "parameter_order": parameter_order,
            "parameter_units": parameter_units,
            "waveform_analyses": analyses,
            "filename": normalized_filename,
            "ascii_raw": ascii_raw,
            "timeout_seconds": timeout_seconds,
        },
        "point_count": len(combinations),
    }
    _write_json(manifest_path, manifest)

    points: list[ExperimentPointResult] = []
    for index, combination in enumerate(combinations):
        point_dir = experiment_dir / f"point-{index:04d}"
        point: ExperimentPointResult = {
            "index": index,
            "parameters": combination,
            "run_dir": str(point_dir),
            "simulation_status": "error",
            "duration_seconds": None,
            "measurements": {},
            "analyses": [],
            "all_passed": False,
            "error": None,
        }
        rendered = _render_experiment_netlist(netlist_template, combination)
        try:
            output_dir = _run_netlist_text(
                rendered,
                normalized_filename,
                ascii_raw,
                timeout_seconds,
                point_dir,
            )
            summary = _summarize_run(output_dir)
        except (FileNotFoundError, RuntimeError) as exc:
            point["error"] = str(exc)
            points.append(point)
            continue

        point["simulation_status"] = str(summary.get("status", "completed"))
        duration = summary.get("duration_seconds")
        point["duration_seconds"] = (
            float(duration) if isinstance(duration, (int, float)) else None
        )
        measurements = summary.get("measurements", {})
        if isinstance(measurements, dict):
            point["measurements"] = {
                str(name): float(value) for name, value in measurements.items()
            }

        for analysis in analyses:
            analysis_name = analysis["name"]
            options = {
                name: analysis[name]
                for name in (
                    "axis_variable",
                    "secondary_variable",
                    "signal_unit",
                    "axis_unit",
                    "raw_filename",
                )
                if name in analysis
            }
            try:
                analysis_result = analyze_waveform(
                    str(output_dir),
                    analysis["variable"],
                    analysis["requirements"],
                    **options,
                )
            except (FileNotFoundError, IndexError, KeyError, ValueError) as exc:
                point["analyses"].append(
                    {
                        "name": analysis_name,
                        "status": "error",
                        "error": str(exc),
                        "analysis": None,
                    }
                )
                if point["error"] is None:
                    point["error"] = f"analysis {analysis_name}: {exc}"
                continue
            point["analyses"].append(
                {
                    "name": analysis_name,
                    "status": "completed",
                    "error": None,
                    "analysis": analysis_result,
                }
            )

        point["all_passed"] = point["simulation_status"] == "completed" and all(
            result["status"] == "completed"
            and result["analysis"] is not None
            and result["analysis"]["all_passed"]
            for result in point["analyses"]
        )
        points.append(point)

    completed_points = sum(
        point["simulation_status"] == "completed" for point in points
    )
    error_points = sum(
        point["simulation_status"] != "completed"
        or any(analysis["status"] == "error" for analysis in point["analyses"])
        for point in points
    )
    passed_points = sum(point["all_passed"] for point in points)
    result: ExperimentResult = {
        "experiment_id": experiment_id,
        "experiment_dir": str(experiment_dir),
        "manifest": str(manifest_path),
        "results_json": str(results_json_path),
        "results_csv": str(results_csv_path),
        "status": "completed",
        "parameter_order": parameter_order,
        "parameter_units": parameter_units,
        "point_count": len(points),
        "completed_points": completed_points,
        "error_points": error_points,
        "passed_points": passed_points,
        "failed_points": len(points) - passed_points,
        "all_passed": passed_points == len(points),
        "points": points,
    }
    _write_experiment_csv(results_csv_path, parameter_order, points)
    _write_json(results_json_path, result)

    manifest.update(
        status="completed",
        finished_at=datetime.now().astimezone().isoformat(),
        duration_seconds=time.monotonic() - started_clock,
        completed_points=completed_points,
        error_points=error_points,
        passed_points=passed_points,
        failed_points=len(points) - passed_points,
        all_passed=result["all_passed"],
        artifacts=[results_json_path.name, results_csv_path.name],
        points=[
            {
                "index": point["index"],
                "parameters": point["parameters"],
                "run_dir": point["run_dir"],
                "simulation_status": point["simulation_status"],
                "all_passed": point["all_passed"],
                "error": point["error"],
            }
            for point in points
        ],
    )
    _write_json(manifest_path, manifest)
    return result


@mcp.tool()
def list_runs(limit: int = 20) -> list[dict[str, object]]:
    """List recent runs (newest first) with status, measurements, and artifacts."""
    return report_runs.collect_records()[: max(0, limit)]


@mcp.tool()
def build_dashboard() -> dict[str, str]:
    """Regenerate the static HTML/JSON run dashboard under runs/."""
    html_path, json_path = report_runs.write_dashboard()
    return {"html": str(html_path), "json": str(json_path)}


@mcp.tool()
def list_examples() -> list[dict[str, str]]:
    """List the bundled example netlists with a one-line description each."""
    examples = []
    for path in sorted(EXAMPLES_DIR.glob("*.cir")) + sorted(EXAMPLES_DIR.glob("*.net")):
        description = ""
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("*"):
                description = stripped.lstrip("* ").strip()
                break
        examples.append({"name": path.name, "path": str(path), "description": description})
    return examples


if __name__ == "__main__":
    mcp.run()
