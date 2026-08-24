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
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from mcp.server.mcpserver import MCPServer

import ltspice_wrapper as wrapper
import raw_parser
import report_runs
import waveform_metrics

PROJECT_DIR = wrapper.PROJECT_DIR
RUNS_DIR = wrapper.RUNS_DIR
EXAMPLES_DIR = PROJECT_DIR / "examples"

mcp = MCPServer(
    name="ltspice",
    instructions=(
        "Run LTspice circuit simulations in batch mode and inspect the "
        "results. Provide a netlist as text (.cir-style) or a path to an "
        "existing netlist file; text netlists are the reliable automation "
        "boundary. Every run returns a run_dir that later tools accept to "
        "pull measurements or waveform data."
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
    axis_unit: str = "s",
    raw_filename: str | None = None,
    secondary_variable: str | None = None,
) -> WaveformAnalysisResult:
    """Evaluate full-resolution waveform requirements for one real-valued vector.

    Each requirement needs `metric`, `operator`, and `target`. Supported metrics
    include scalar, transition, pulse, slew, ripple, monotonicity, paired-signal
    propagation delay, and forbidden-region checks. Each requirement can select
    a closed axis window. Paired metrics use the optional secondary_variable.
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
    results: list[waveform_metrics.RequirementResult] = []
    numeric_parameters = {
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
    string_parameters = {"polarity", "primary_edge", "secondary_edge", "direction"}
    for requirement in requirements:
        try:
            metric = str(requirement["metric"])
            operator = str(requirement["operator"])
            target = float(requirement["target"])
        except KeyError as exc:
            raise ValueError(f"requirement is missing {exc.args[0]}") from exc
        parameters = {
            name: float(requirement[name])
            for name in numeric_parameters
            if name in requirement
        }
        parameters.update(
            {
                name: str(requirement[name])
                for name in string_parameters
                if name in requirement
            }
        )
        measurement = waveform_metrics.measure_metric(
            axis,
            values,
            metric,
            secondary_values=secondary_values,
            signal_unit=signal_unit,
            axis_unit=axis_unit,
            **parameters,
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
