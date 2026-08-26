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
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer

import adaptive_boundary
import experiment_engine
import experiment_index
import experiment_report
import experiment_visualization
import frequency_domain_metrics
import local_sensitivity
import ltspice_wrapper as wrapper
import raw_parser
import report_runs
import sensitivity_analysis
import statistical_engine
import statistical_comparison
import statistical_results
import waveform_metrics
import worst_case_analysis

PROJECT_DIR = wrapper.PROJECT_DIR
RUNS_DIR = wrapper.RUNS_DIR
EXAMPLES_DIR = PROJECT_DIR / "examples"
MAX_EXPERIMENT_WORKERS = experiment_engine.MAX_EXPERIMENT_WORKERS
MAX_WAVEFORM_RESPONSE_POINTS = 10_000
MAX_LEGACY_SWEEP_POINTS = experiment_engine.MAX_EXPERIMENT_POINTS

# Re-export schema types and tested helper seams while keeping their
# implementation independent of MCP.
WaveformAnalysisResult = experiment_engine.WaveformAnalysisResult
ExperimentParameter = experiment_engine.ExperimentParameter
ExperimentDerivedParameter = experiment_engine.ExperimentDerivedParameter
ExperimentWaveformAnalysis = experiment_engine.ExperimentWaveformAnalysis
ExperimentAnalysisResult = experiment_engine.ExperimentAnalysisResult
ExperimentPointResult = experiment_engine.ExperimentPointResult
ExperimentResult = experiment_engine.ExperimentResult
ExperimentJobSnapshot = experiment_engine.ExperimentJobSnapshot
ExperimentComparisonResult = experiment_engine.ExperimentComparisonResult
ExperimentIndexBuildResult = experiment_index.ExperimentIndexBuildResult
ExperimentQueryResult = experiment_index.ExperimentQueryResult
ExperimentReportResult = experiment_report.ExperimentReportResult
ReportContext = experiment_report.ReportContext
ComparisonReportResult = experiment_visualization.ComparisonReportResult
ExperimentDashboardResult = experiment_visualization.ExperimentDashboardResult
StatisticalVariable = statistical_engine.StatisticalVariable
StatisticalCorrelation = statistical_engine.StatisticalCorrelation
StatisticalCornerAxis = statistical_engine.StatisticalCornerAxis
StatisticalPlanResult = statistical_engine.StatisticalPlanResult
StatisticalSamplingMethod = Literal["independent", "latin_hypercube", "halton"]
StatisticalSummaryResult = statistical_results.StatisticalSummaryResult
StatisticalComparisonResult = statistical_comparison.StatisticalComparisonResult
SensitivityAnalysisResult = sensitivity_analysis.SensitivityAnalysisResult
LocalSensitivityAnalysisResult = local_sensitivity.LocalSensitivityAnalysisResult
AdaptiveBoundaryStudyResult = adaptive_boundary.AdaptiveBoundaryStudyResult
WorstCaseAnalysisResult = worst_case_analysis.WorstCaseAnalysisResult

ExperimentIndexStatus = Literal[
    "defined",
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
]
ExperimentExecutionMode = Literal["independent", "native"]

_write_json = experiment_engine._write_json
_prepare_experiment = experiment_engine._prepare_experiment
_render_experiment_netlist = experiment_engine._render_experiment_netlist
_render_native_experiment_netlist = experiment_engine._render_native_experiment_netlist
_netlist_filename = experiment_engine._netlist_filename
_validate_timeout = experiment_engine._validate_timeout
_validate_reuse_cache = experiment_engine._validate_reuse_cache

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


def _series_to_json(values: list[float | complex]) -> list[float] | dict[str, list[float]]:
    if values and isinstance(values[0], complex):
        return {"real": [v.real for v in values], "imag": [v.imag for v in values]}
    return list(values)


def _within_directory(path: Path, root: Path, message: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{message}: {resolved}") from exc
    return resolved


def _within_runs(path: Path) -> Path:
    return _within_directory(path, RUNS_DIR, "Path must be inside the runs directory")


def _resolve_run_dir(run_dir: str) -> Path:
    """Accept an in-tree absolute path or a run_dir name relative to runs/."""
    path = Path(run_dir).expanduser()
    if not path.is_absolute():
        path = RUNS_DIR / path
    path = _within_runs(path)
    if not path.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")
    return path


def _simulation_cache_dir() -> Path:
    cache_dir = (RUNS_DIR / "cache").resolve()
    try:
        cache_dir.relative_to(RUNS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("Simulation cache must be inside the runs directory") from exc
    return cache_dir


def _find_raw(run_dir: Path, raw_filename: str | None) -> Path:
    if raw_filename:
        if Path(raw_filename).name != raw_filename or "/" in raw_filename or "\\" in raw_filename:
            raise ValueError("raw_filename must be a plain file name")
        candidate = _within_directory(
            run_dir / raw_filename,
            run_dir,
            "Raw file must remain inside the run directory",
        )
        if not candidate.is_file():
            raise FileNotFoundError(f"Raw file not found: {candidate}")
        return candidate
    raw_files = [
        _within_directory(
            path,
            run_dir,
            "Raw file must remain inside the run directory",
        )
        for path in sorted(run_dir.glob("*.raw"))
    ]
    raw_files = [path for path in raw_files if path.is_file()]
    if not raw_files:
        raise FileNotFoundError(f"No .raw file found in {run_dir}")
    # Prefer a transient/AC/step result over a bias-point (.op.raw) file.
    primary = [path for path in raw_files if not path.name.endswith(".op.raw")]
    return (primary or raw_files)[0]


def _summarize_run(output_dir: Path) -> dict[str, object]:
    manifest = json.loads((output_dir / "run_manifest.json").read_text())
    measurements: dict[str, float] = {}
    for log_path in sorted(output_dir.glob("*.log")):
        log_path = _within_directory(
            log_path,
            output_dir,
            "Log file must remain inside the run directory",
        )
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
        "execution_source": manifest.get("execution_source", "simulator"),
        "cache": manifest.get("cache"),
    }


def _run_netlist_text(
    netlist: str,
    filename: str,
    ascii_raw: bool,
    timeout_seconds: int,
    dest_dir: Path,
    reuse_cache: bool = False,
) -> Path:
    filename = _netlist_filename(filename)
    with tempfile.TemporaryDirectory(prefix="mcp-input-") as tmp:
        source_path = Path(tmp) / filename
        source_path.write_text(netlist, encoding="utf-8")
        return wrapper.run_netlist(
            source_path,
            output_dir=dest_dir,
            timeout_seconds=timeout_seconds,
            ascii_raw=ascii_raw,
            reuse_cache=reuse_cache,
            cache_dir=_simulation_cache_dir() if reuse_cache else None,
        )


def _analyze_experiment_point(
    point: ExperimentPointResult,
    output_dir: Path,
    analyses: list[ExperimentWaveformAnalysis],
    step_index: int | None = None,
) -> ExperimentPointResult:
    for analysis in analyses:
        analysis_name = analysis["name"]
        options: dict[str, object] = {
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
        if step_index is not None:
            options["step_index"] = step_index
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
    return point


def _execute_experiment_point(
    index: int,
    combination: dict[str, str],
    point_dir: Path,
    netlist_template: str,
    filename: str,
    ascii_raw: bool,
    timeout_seconds: int,
    analyses: list[ExperimentWaveformAnalysis],
    cancel_event: threading.Event | None = None,
    reuse_cache: bool = False,
) -> ExperimentPointResult:
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
    if cancel_event is not None and cancel_event.is_set():
        point["simulation_status"] = "cancelled"
        point["error"] = "experiment cancelled before simulation"
        return point

    rendered = _render_experiment_netlist(netlist_template, combination)
    try:
        arguments = (rendered, filename, ascii_raw, timeout_seconds, point_dir)
        output_dir = (
            _run_netlist_text(*arguments, True)
            if reuse_cache
            else _run_netlist_text(*arguments)
        )
        summary = _summarize_run(output_dir)
    except (FileNotFoundError, RuntimeError) as exc:
        point["error"] = str(exc)
        return point

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
    cache = summary.get("cache")
    if isinstance(cache, dict):
        point["cache_hit"] = cache.get("hit") is True
        cache_key = cache.get("key")
        point["cache_key"] = cache_key if isinstance(cache_key, str) else None

    if cancel_event is not None and cancel_event.is_set():
        point["error"] = "experiment cancelled before waveform analysis"
        return point

    return _analyze_experiment_point(point, output_dir, analyses)


def _native_error_points(
    combinations: list[dict[str, str]], batch_dir: Path, error: str
) -> list[ExperimentPointResult]:
    return [
        {
            "index": index,
            "parameters": combination,
            "run_dir": str(batch_dir),
            "simulation_status": "error",
            "duration_seconds": None,
            "measurements": {},
            "analyses": [],
            "all_passed": False,
            "error": error,
            "native_step_index": index,
        }
        for index, combination in enumerate(combinations)
    ]


def _execute_native_experiment(
    combinations: list[dict[str, str]],
    batch_dir: Path,
    netlist: str,
    filename: str,
    ascii_raw: bool,
    timeout_seconds: int,
    analyses: list[ExperimentWaveformAnalysis],
    reuse_cache: bool,
    cancel_event: threading.Event | None = None,
) -> tuple[list[ExperimentPointResult], dict[str, object]]:
    """Run one stepped deck and map its validated slices back to experiment points."""
    output_dir: Path | None = None
    summary: dict[str, object] | None = None
    try:
        arguments = (netlist, filename, ascii_raw, timeout_seconds, batch_dir)
        output_dir = (
            _run_netlist_text(*arguments, True)
            if reuse_cache
            else _run_netlist_text(*arguments)
        )
        summary = _summarize_run(output_dir)
        log_path = output_dir / Path(filename).with_suffix(".log").name
        if not log_path.is_file():
            raise FileNotFoundError(f"Native batch log not found: {log_path}")
        step_values = wrapper.parse_step_values(
            log_path, experiment_engine._NATIVE_STEP_PARAMETER
        )
        expected_steps = [float(index) for index in range(len(combinations))]
        if step_values != expected_steps:
            raise ValueError(
                "Native batch step order mismatch: "
                f"expected {expected_steps}, found {step_values}"
            )
        measurement_rows = wrapper.parse_stepped_measurement_rows(log_path)
        expected_rows = set(range(1, len(combinations) + 1))
        for name, rows in measurement_rows.items():
            unexpected_rows = sorted(set(rows) - expected_rows)
            if unexpected_rows:
                raise ValueError(
                    f"Native batch measurement {name} has invalid step rows: "
                    f"{unexpected_rows}"
                )
        if analyses:
            raw_paths: list[Path] = []
            for analysis in analyses:
                raw_path = _find_raw(output_dir, analysis.get("raw_filename"))
                if raw_path not in raw_paths:
                    raw_paths.append(raw_path)
            for raw_path in raw_paths:
                raw_data = raw_parser.parse_raw(raw_path)
                slices = raw_parser.step_slices(raw_data)
                if (
                    raw_data.step_count != len(combinations)
                    or len(slices) != len(combinations)
                ):
                    raise ValueError(
                        f"Native batch waveform count mismatch in {raw_path.name}: "
                        f"expected {len(combinations)}, found {raw_data.step_count}"
                    )
    except (FileNotFoundError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        error = str(exc)
        failed_batch: dict[str, object] = {
            "run_dir": str(batch_dir),
            "status": "error",
            "step_parameter": experiment_engine._NATIVE_STEP_PARAMETER,
            "step_count": len(combinations),
            "error": error,
        }
        run_manifest = batch_dir / "run_manifest.json"
        if run_manifest.is_file():
            failed_batch["run_manifest"] = str(run_manifest)
        if summary is not None:
            duration = summary.get("duration_seconds")
            failed_batch["duration_seconds"] = (
                float(duration) if isinstance(duration, (int, float)) else None
            )
            failed_batch["execution_source"] = summary.get(
                "execution_source", "simulator"
            )
            cache = summary.get("cache")
            if isinstance(cache, dict):
                failed_batch["cache_hit"] = cache.get("hit") is True
                failed_batch["cache_key"] = cache.get("key")
        return _native_error_points(combinations, batch_dir, error), failed_batch

    assert output_dir is not None and summary is not None
    duration = summary.get("duration_seconds")
    cache = summary.get("cache")
    batch: dict[str, object] = {
        "run_dir": str(output_dir),
        "run_manifest": str(output_dir / "run_manifest.json"),
        "status": str(summary.get("status", "completed")),
        "duration_seconds": (
            float(duration) if isinstance(duration, (int, float)) else None
        ),
        "step_parameter": experiment_engine._NATIVE_STEP_PARAMETER,
        "step_count": len(combinations),
        "validated_step_order": list(range(len(combinations))),
        "execution_source": summary.get("execution_source", "simulator"),
    }
    if isinstance(cache, dict):
        batch["cache_hit"] = cache.get("hit") is True
        batch["cache_key"] = cache.get("key")
    shared_measurements = summary.get("measurements", {})
    if not isinstance(shared_measurements, dict):
        shared_measurements = {}

    points: list[ExperimentPointResult] = []
    for index, combination in enumerate(combinations):
        point_measurements = {
            str(name): float(value) for name, value in shared_measurements.items()
        }
        point_measurements.update(
            {
                name: rows[index + 1]
                for name, rows in measurement_rows.items()
                if index + 1 in rows
            }
        )
        point: ExperimentPointResult = {
            "index": index,
            "parameters": combination,
            "run_dir": str(output_dir),
            "simulation_status": str(summary.get("status", "completed")),
            "duration_seconds": None,
            "measurements": point_measurements,
            "analyses": [],
            "all_passed": False,
            "error": None,
            "native_step_index": index,
        }
        if cancel_event is not None and cancel_event.is_set():
            point["error"] = "experiment cancelled before waveform analysis"
            points.append(point)
        else:
            points.append(_analyze_experiment_point(point, output_dir, analyses, index))
    return points, batch


class ExperimentJobManager(experiment_engine.ExperimentJobManager):
    """Bind the portable job manager to this MCP server's point executor."""

    def __init__(self, runs_dir: Path, workers: int = MAX_EXPERIMENT_WORKERS) -> None:
        super().__init__(
            runs_dir,
            workers,
            execute_point=lambda *args, **kwargs: _execute_experiment_point(
                *args, **kwargs
            ),
            execute_native=lambda *args, **kwargs: _execute_native_experiment(
                *args, **kwargs
            ),
        )


# --- tools --------------------------------------------------------------


@mcp.tool()
def run_netlist(
    netlist: str,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    reuse_cache: bool = False,
) -> dict[str, object]:
    """Run LTspice in batch mode on netlist text and return status and measurements.

    `netlist` is the full text of a .cir/.net deck (SPICE directives such as
    .ac/.tran/.step and .meas lines). Raises with LTspice's diagnostic output
    if the simulation fails or times out. The returned run_dir can be passed
    to get_measurements, get_waveform, or export_waveform_csv.
    """
    _validate_timeout(timeout_seconds)
    _validate_reuse_cache(reuse_cache)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    arguments = (
        netlist,
        filename,
        ascii_raw,
        timeout_seconds,
        RUNS_DIR / f"mcp-{stamp}",
    )
    output_dir = (
        _run_netlist_text(*arguments, True)
        if reuse_cache
        else _run_netlist_text(*arguments)
    )
    return _summarize_run(output_dir)


@mcp.tool()
def run_netlist_file(
    path: str,
    output_dir: str | None = None,
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    reuse_cache: bool = False,
) -> dict[str, object]:
    """Run an existing .cir/.net file; any explicit output_dir must be under runs/."""
    _validate_timeout(timeout_seconds)
    _validate_reuse_cache(reuse_cache)
    netlist_path = Path(path).expanduser()
    if netlist_path.suffix.lower() not in (".cir", ".net"):
        raise ValueError("path must identify a .cir or .net file")
    resolved_output = _within_runs(Path(output_dir)) if output_dir else None
    result_dir = wrapper.run_netlist(
        netlist_path,
        output_dir=resolved_output,
        timeout_seconds=timeout_seconds,
        ascii_raw=ascii_raw,
        reuse_cache=reuse_cache,
        cache_dir=_simulation_cache_dir() if reuse_cache else None,
    )
    return _summarize_run(result_dir)


@mcp.tool()
def get_measurements(run_dir: str) -> dict[str, float]:
    """Return the scalar .meas values recorded in a run's log file(s)."""
    resolved = _resolve_run_dir(run_dir)
    logs = [
        _within_directory(
            path,
            resolved,
            "Log file must remain inside the run directory",
        )
        for path in sorted(resolved.glob("*.log"))
    ]
    logs = [path for path in logs if path.is_file()]
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
    if (
        not isinstance(max_points, int)
        or isinstance(max_points, bool)
        or not 1 <= max_points <= MAX_WAVEFORM_RESPONSE_POINTS
    ):
        raise ValueError(
            f"max_points must be between 1 and {MAX_WAVEFORM_RESPONSE_POINTS}"
        )
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
    elif max_points == 1:
        indices = [0]
    else:
        indices = [
            round(index * (total_points - 1) / (max_points - 1))
            for index in range(max_points)
        ]

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
        "raw_sha256": wrapper._sha256_file(raw_path),
        "raw_size_bytes": raw_path.stat().st_size,
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
    csv_candidate = raw_path.with_suffix(".csv")
    if csv_candidate.is_symlink():
        raise ValueError("CSV export path must not be a symlink")
    csv_path = _within_directory(
        csv_candidate,
        resolved,
        "CSV export must remain inside the run directory",
    )
    temporary = csv_path.with_name(f".{csv_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        raw_parser.export_csv(data, temporary)
        os.replace(temporary, csv_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"csv_path": str(csv_path), "rows": data.points, "variables": data.variables}


@mcp.tool()
def run_parameter_sweep(
    netlist_template: str,
    values: list[str],
    placeholder: str = "{value}",
    filename: str = "circuit.cir",
    timeout_seconds: int = 120,
    reuse_cache: bool = False,
) -> dict[str, object]:
    """Run one LTspice simulation per value, substituting `placeholder` in the template.

    Example: netlist_template containing "R1 in out {value}" with
    values=["1k", "10k", "100k"] runs three independent simulations. Each
    point's status and .meas measurements are returned, along with a
    results.csv summarizing the whole sweep.
    """
    if not values:
        raise ValueError("values must be a non-empty list")
    if len(values) > MAX_LEGACY_SWEEP_POINTS:
        raise ValueError(
            f"values is limited to {MAX_LEGACY_SWEEP_POINTS} sweep points"
        )
    if not placeholder or placeholder not in netlist_template:
        raise ValueError("placeholder must occur in netlist_template")
    _netlist_filename(filename)
    _validate_timeout(timeout_seconds)
    _validate_reuse_cache(reuse_cache)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    sweep_dir = RUNS_DIR / f"mcp-sweep-{stamp}"
    rows: list[dict[str, object]] = []
    for index, value in enumerate(values):
        rendered = netlist_template.replace(placeholder, str(value))
        point_dir = sweep_dir / f"point-{index:03d}"
        try:
            arguments = (rendered, filename, False, timeout_seconds, point_dir)
            output_dir = (
                _run_netlist_text(*arguments, True)
                if reuse_cache
                else _run_netlist_text(*arguments)
            )
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
    derived_parameters: list[ExperimentDerivedParameter] | None = None,
    reuse_cache: bool = False,
    execution_mode: Literal["independent", "native"] = "independent",
) -> ExperimentResult:
    """Run a deterministic Cartesian experiment and reuse analyses at every point.

    Parameters are ordered records with a name, explicit string values, and an
    optional metadata-only unit. The corresponding template placeholder is
    `{name}`. Declaration order defines Cartesian order: the first parameter
    changes slowest and the last changes fastest. Derived parameters are safe
    textual templates resolved in dependency order; they do not increase the
    point count. Execution is sequential and is limited to 1,000 points by
    default. Set execution_mode to "native" to run the grid as one validated
    LTspice stepped deck; native values must be numeric expressions.
    """
    return experiment_engine.run_experiment_sync(
        RUNS_DIR,
        _execute_experiment_point,
        netlist_template,
        parameters,
        waveform_analyses,
        filename,
        ascii_raw,
        timeout_seconds,
        derived_parameters,
        reuse_cache,
        execution_mode,
        _execute_native_experiment,
    )


@mcp.tool()
def generate_statistical_plan(
    variables: list[StatisticalVariable],
    sample_count: int,
    seed: int,
    correlations: list[StatisticalCorrelation] | None = None,
    corner_axes: list[StatisticalCornerAxis] | None = None,
    corner_aggregate: bool = False,
    sampling_method: StatisticalSamplingMethod = "independent",
) -> StatisticalPlanResult:
    """Generate and persist a deterministic statistical point plan without LTspice.

    Supports uniform, bounded Gaussian, weighted discrete, empirical measured
    populations, explicit correlated-Gaussian groups, bounded named corner
    axes, Latin-hypercube sampling, and scrambled Halton sampling. Empirical
    values may be inline or read from a UTF-8 CSV confined to this project. The
    versioned seed produces the same canonical paired points on macOS and
    Windows. Stratified methods support uniform, bounded Gaussian, correlated
    Gaussian, discrete, and empirical variables. Set corner_aggregate only to
    request a pooled yield in addition to the mandatory per-corner results.
    """
    return statistical_engine.generate_statistical_plan(
        RUNS_DIR,
        variables,
        sample_count,
        seed,
        correlations,
        corner_axes,
        corner_aggregate,
        source_root=PROJECT_DIR,
        sampling_method=sampling_method,
    )


@mcp.tool()
def get_statistical_plan(plan_id: str) -> StatisticalPlanResult:
    """Load and verify a previously generated statistical point plan."""
    return statistical_engine.inspect_statistical_plan(RUNS_DIR, plan_id)


def _statistical_plan_source(
    plan_id: str,
    plan: dict[str, object],
    plan_result: StatisticalPlanResult,
) -> dict[str, object]:
    source: dict[str, object] = {
        "kind": "statistical",
        "plan_id": plan_id,
        "plan_sha256": plan_result["plan_sha256"],
        "runs_relative_path": f"statistical-plans/{plan_id}/statistical_plan.json",
        "generator_version": plan["generator_version"],
        "definition_hash": plan["definition_hash"],
        "sampling_method": plan_result["sampling_method"],
    }
    definition = plan["definition"]
    assert isinstance(definition, dict)
    corner_axes = definition.get("corner_axes", [])
    if corner_axes:
        points = plan["points"]
        assert isinstance(points, list)
        source.update(
            {
                "sample_count": plan["sample_count"],
                "corner_axes": corner_axes,
                "corner_aggregate": bool(
                    definition.get("corner_aggregate", False)
                ),
                "point_metadata": [
                    {
                        "index": point["index"],
                        "sample_index": point["sample_index"],
                        "corners": point["corners"],
                    }
                    for point in points
                ],
            }
        )
    return source


@mcp.tool()
def run_statistical_experiment(
    plan_id: str,
    netlist_template: str,
    waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    reuse_cache: bool = False,
) -> ExperimentResult:
    """Run every paired point in a verified statistical plan independently."""
    plan = statistical_engine.load_statistical_plan(RUNS_DIR, plan_id)
    plan_result = statistical_engine.inspect_statistical_plan(RUNS_DIR, plan_id)
    combinations = [point["parameters"] for point in plan["points"]]
    return experiment_engine.run_experiment_sync(
        RUNS_DIR,
        _execute_experiment_point,
        netlist_template,
        [],
        waveform_analyses,
        filename,
        ascii_raw,
        timeout_seconds,
        None,
        reuse_cache,
        "independent",
        _execute_native_experiment,
        explicit_points=combinations,
        explicit_parameter_order=plan["parameter_order"],
        explicit_parameter_units=plan["parameter_units"],
        source_point_plan=_statistical_plan_source(plan_id, plan, plan_result),
    )


_experiment_manager: ExperimentJobManager | None = None
_experiment_manager_lock = threading.Lock()


def _get_experiment_manager() -> ExperimentJobManager:
    global _experiment_manager
    with _experiment_manager_lock:
        if _experiment_manager is not None and _experiment_manager.runs_dir != RUNS_DIR:
            _experiment_manager.shutdown()
            _experiment_manager = None
        if _experiment_manager is None:
            _experiment_manager = ExperimentJobManager(RUNS_DIR)
        return _experiment_manager


@mcp.tool()
def define_experiment(
    netlist_template: str,
    parameters: list[ExperimentParameter],
    waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    derived_parameters: list[ExperimentDerivedParameter] | None = None,
    max_concurrency: int = 2,
    reuse_cache: bool = False,
    execution_mode: Literal["independent", "native"] = "independent",
) -> ExperimentJobSnapshot:
    """Validate and persist an independent or native experiment without starting it."""
    return _get_experiment_manager().define(
        netlist_template,
        parameters,
        waveform_analyses,
        filename,
        ascii_raw,
        timeout_seconds,
        derived_parameters,
        max_concurrency,
        reuse_cache,
        execution_mode,
    )


@mcp.tool()
def define_statistical_study(
    plan_id: str,
    netlist_template: str,
    waveform_analyses: list[ExperimentWaveformAnalysis] | None = None,
    filename: str = "circuit.cir",
    ascii_raw: bool = False,
    timeout_seconds: int = 120,
    max_concurrency: int = 2,
    reuse_cache: bool = False,
) -> ExperimentJobSnapshot:
    """Persist a verified statistical plan as a durable resumable study."""
    plan = statistical_engine.load_statistical_plan(RUNS_DIR, plan_id)
    plan_result = statistical_engine.inspect_statistical_plan(RUNS_DIR, plan_id)
    return _get_experiment_manager().define_explicit(
        netlist_template,
        plan["parameter_order"],
        [point["parameters"] for point in plan["points"]],
        plan["parameter_units"],
        _statistical_plan_source(plan_id, plan, plan_result),
        waveform_analyses,
        filename,
        ascii_raw,
        timeout_seconds,
        max_concurrency,
        reuse_cache,
    )


@mcp.tool()
def start_experiment(experiment_id: str) -> ExperimentJobSnapshot:
    """Queue a defined experiment; repeated calls are idempotent."""
    return _get_experiment_manager().start(experiment_id)


@mcp.tool()
def get_experiment(experiment_id: str) -> ExperimentJobSnapshot:
    """Return durable progress and artifact paths for an experiment."""
    return _get_experiment_manager().snapshot(experiment_id)


@mcp.tool()
def cancel_experiment(experiment_id: str) -> ExperimentJobSnapshot:
    """Request cooperative cancellation without scheduling additional points."""
    return _get_experiment_manager().cancel(experiment_id)


@mcp.tool()
def summarize_statistical_experiment(
    experiment_id: str,
) -> StatisticalSummaryResult:
    """Create bounded JSON/CSV yield evidence for a completed statistical study."""
    return statistical_results.summarize_statistical_experiment(
        RUNS_DIR, experiment_id
    )


@mcp.tool()
def analyze_statistical_worst_cases(
    experiment_id: str,
) -> WorstCaseAnalysisResult:
    """Rank worst observed requirement margins and finite named corners."""
    return worst_case_analysis.analyze_statistical_worst_cases(
        RUNS_DIR, experiment_id
    )


@mcp.tool()
def analyze_statistical_sensitivity(
    experiment_id: str,
) -> SensitivityAnalysisResult:
    """Rank global input associations with signed requirement margins."""
    return sensitivity_analysis.analyze_statistical_sensitivity(
        RUNS_DIR, experiment_id
    )


@mcp.tool()
def define_local_sensitivity_study(
    source_experiment_id: str,
    source_point_index: int,
    relative_step: float = 0.01,
    max_concurrency: int = 2,
    reuse_cache: bool = False,
) -> ExperimentJobSnapshot:
    """Define a durable baseline plus low/high one-at-a-time study."""
    prepared = local_sensitivity.prepare_local_sensitivity_study(
        RUNS_DIR,
        source_experiment_id,
        source_point_index,
        relative_step,
    )
    return _get_experiment_manager().define_explicit(
        prepared["netlist_template"],
        prepared["parameter_order"],
        prepared["points"],
        prepared["parameter_units"],
        prepared["source"],
        prepared["waveform_analyses"],
        prepared["filename"],
        prepared["ascii_raw"],
        prepared["timeout_seconds"],
        max_concurrency,
        reuse_cache,
    )


@mcp.tool()
def analyze_local_sensitivity(
    experiment_id: str,
) -> LocalSensitivityAnalysisResult:
    """Write structured local effects and tornado data for a terminal study."""
    return local_sensitivity.analyze_local_sensitivity(RUNS_DIR, experiment_id)


@mcp.tool()
def define_adaptive_boundary_study(
    source_experiment_id: str,
    first_point_index: int,
    second_point_index: int,
    check_id: str,
    variable: str,
    batch_size: int = 3,
    max_samples: int = 12,
    input_tolerance: float = 1e-6,
    max_concurrency: int = 2,
    reuse_cache: bool = True,
) -> AdaptiveBoundaryStudyResult:
    """Define a durable one-dimensional pass/fail boundary refinement."""
    return adaptive_boundary.define_adaptive_boundary_study(
        RUNS_DIR,
        source_experiment_id,
        first_point_index,
        second_point_index,
        check_id,
        variable,
        batch_size,
        max_samples,
        input_tolerance,
        max_concurrency,
        reuse_cache,
    )


@mcp.tool()
def advance_adaptive_boundary_study(
    adaptive_id: str,
) -> AdaptiveBoundaryStudyResult:
    """Incorporate one completed batch or launch the next boundary batch."""
    return adaptive_boundary.advance_adaptive_boundary_study(
        RUNS_DIR, adaptive_id, _get_experiment_manager()
    )


@mcp.tool()
def get_adaptive_boundary_study(
    adaptive_id: str,
) -> AdaptiveBoundaryStudyResult:
    """Inspect a durable adaptive-boundary study without advancing it."""
    return adaptive_boundary.get_adaptive_boundary_study(RUNS_DIR, adaptive_id)


@mcp.tool()
def compare_experiments(
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> ExperimentComparisonResult:
    """Compare two completed experiments without rerunning simulations."""
    return experiment_engine.compare_experiments(
        RUNS_DIR,
        baseline_experiment_id,
        candidate_experiment_id,
    )


@mcp.tool()
def compare_statistical_experiments(
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> StatisticalComparisonResult:
    """Compare compatible statistical evidence without rerunning LTspice."""
    return statistical_comparison.build_statistical_comparison(
        RUNS_DIR, baseline_experiment_id, candidate_experiment_id
    )


@mcp.tool()
def build_experiment_index() -> ExperimentIndexBuildResult:
    """Atomically rebuild the derived SQLite index from experiment artifacts."""
    return experiment_index.build_experiment_index(RUNS_DIR)


@mcp.tool()
def query_experiments(
    limit: int = 50,
    offset: int = 0,
    status: ExperimentIndexStatus | None = None,
    execution_mode: ExperimentExecutionMode | None = None,
    all_passed: bool | None = None,
    parameters: dict[str, str] | None = None,
    circuit_sha256: str | None = None,
    statistical: bool | None = None,
    minimum_yield: float | None = None,
    minimum_confidence_low: float | None = None,
    corner: dict[str, str] | None = None,
    variable: str | None = None,
    requirement_metric: str | None = None,
) -> ExperimentQueryResult:
    """Query ordinary or statistical experiment summaries with exact filters."""
    return experiment_index.query_experiments(
        RUNS_DIR,
        limit=limit,
        offset=offset,
        status=status,
        execution_mode=execution_mode,
        all_passed=all_passed,
        parameters=parameters,
        circuit_sha256=circuit_sha256,
        statistical=statistical,
        minimum_yield=minimum_yield,
        minimum_confidence_low=minimum_confidence_low,
        corner=corner,
        variable=variable,
        requirement_metric=requirement_metric,
    )


@mcp.tool()
def build_experiment_report(
    experiment_id: str,
    report_context: ReportContext | None = None,
) -> ExperimentReportResult:
    """Build a portable offline HTML report from completed experiment artifacts."""
    if report_context is None:
        return experiment_report.build_experiment_report(RUNS_DIR, experiment_id)
    return experiment_report.build_experiment_report(RUNS_DIR, experiment_id, report_context)


@mcp.tool()
def build_comparison_report(
    baseline_experiment_id: str,
    candidate_experiment_id: str,
) -> ComparisonReportResult:
    """Build a portable baseline-versus-candidate waveform report."""
    return experiment_visualization.build_comparison_report(
        RUNS_DIR,
        baseline_experiment_id,
        candidate_experiment_id,
    )


@mcp.tool()
def build_experiment_dashboard() -> ExperimentDashboardResult:
    """Rebuild the experiment index and its searchable offline dashboard."""
    return experiment_visualization.build_experiment_dashboard(RUNS_DIR)


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
    try:
        mcp.run()
    finally:
        if _experiment_manager is not None:
            _experiment_manager.shutdown()
