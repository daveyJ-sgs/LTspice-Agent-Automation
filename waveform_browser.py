#!/usr/bin/env python3
"""Read finished-run waveforms for the System Builder viewer.

The durable engines already write full-resolution .raw files under
runs/<experiment>/point-NNNN/attempt-NNNN/. Everything here is read-only: it
lists what a finished run captured and returns downsampled vectors for
plotting, without touching an artifact or launching LTspice.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import raw_parser
from system_builder_history import evidence_file

EXPERIMENT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
POINT_DIRECTORY = re.compile(r"point-(\d{4})")
ATTEMPT_DIRECTORY = re.compile(r"attempt-(\d{4})")
MAX_LISTED_CAPTURES = 512
MAX_VIEWER_POINTS = 4000


def list_run_captures(runs: Path, experiment_id: str) -> dict[str, object]:
    """List the .raw captures one finished experiment wrote, newest attempt first."""
    if EXPERIMENT_ID.fullmatch(experiment_id) is None:
        raise ValueError("experiment id is invalid")
    experiment_dir = runs / experiment_id
    if not experiment_dir.is_dir() or experiment_dir.is_symlink():
        raise ValueError("experiment directory is missing")

    captures: list[dict[str, object]] = []
    for point_dir in sorted(experiment_dir.glob("point-[0-9][0-9][0-9][0-9]")):
        point_match = POINT_DIRECTORY.fullmatch(point_dir.name)
        if point_match is None or point_dir.is_symlink():
            continue
        for attempt_dir in sorted(point_dir.glob("attempt-[0-9][0-9][0-9][0-9]")):
            attempt_match = ATTEMPT_DIRECTORY.fullmatch(attempt_dir.name)
            if attempt_match is None or attempt_dir.is_symlink():
                continue
            for raw_path in sorted(attempt_dir.glob("*.raw")):
                if raw_path.is_symlink() or not raw_path.is_file():
                    continue
                captures.append(
                    {
                        "point_index": int(point_match.group(1)),
                        "attempt": int(attempt_match.group(1)),
                        "filename": raw_path.name,
                        "path": raw_path.relative_to(runs).as_posix(),
                        "size_bytes": raw_path.stat().st_size,
                    }
                )
                if len(captures) >= MAX_LISTED_CAPTURES:
                    return {"experiment_id": experiment_id, "captures": captures, "truncated": True}
    return {"experiment_id": experiment_id, "captures": captures, "truncated": False}


# LTspice names each vector's quantity in the RAW header. The viewer needs
# the unit so that volts and amperes are never scaled onto one shared axis --
# a milliamp trace drawn against a 3.3 V axis is a flat line on the baseline.
_TRACE_UNITS = {
    "voltage": "V",
    "current": "A",
    "device_current": "A",
    "subckt_current": "A",
    "power": "W",
    "time": "s",
    "frequency": "Hz",
}


def trace_unit(kind: str) -> str:
    """Map a RAW variable type onto its SI unit, or "" when unrecognised."""
    return _TRACE_UNITS.get(kind.strip().casefold(), "")


def _downsampled_indices(total: int, maximum: int) -> list[int]:
    if total <= maximum:
        return list(range(total))
    if maximum == 1:
        return [0]
    return [round(index * (total - 1) / (maximum - 1)) for index in range(maximum)]


def read_capture(
    runs: Path,
    relative_path: str,
    *,
    variables: list[str] | None = None,
    max_points: int = 1200,
) -> dict[str, object]:
    """Return plottable vectors from one capture, downsampled for the browser."""
    if (
        not isinstance(max_points, int)
        or isinstance(max_points, bool)
        or not 1 <= max_points <= MAX_VIEWER_POINTS
    ):
        raise ValueError(f"max_points must be between 1 and {MAX_VIEWER_POINTS}")
    raw_path = evidence_file(runs, relative_path)
    if raw_path.suffix.lower() != ".raw":
        raise ValueError("capture must be a .raw file")
    data = raw_parser.parse_raw(raw_path)

    axis_name = data.variables[0]
    # An operating point (.op, or a stepped .op) has no sweep: LTspice writes
    # the first node where time or frequency would be. Every vector is then a
    # value to read, including that first one, and nothing is an axis.
    operating_point = trace_unit(data.types.get(axis_name, "")) not in ("", "s", "Hz")
    wanted = variables or [
        name for name in data.variables if operating_point or name != axis_name
    ]
    unknown = sorted(set(wanted) - set(data.variables))
    if unknown:
        raise ValueError(f"unknown vector(s): {', '.join(unknown)}")

    indices = _downsampled_indices(data.points, max_points)
    complex_data = "complex" in data.flags.lower()

    def magnitude(value: float | complex) -> float:
        return abs(value) if isinstance(value, complex) else float(value)

    series = {
        name: [magnitude(data.values[name][index]) for index in indices]
        for name in wanted
    }
    axis = (
        [float(index) for index in indices]
        if operating_point
        else [
            float(value.real if isinstance(value, complex) else value)
            for value in (data.values[axis_name][index] for index in indices)
        ]
    )
    return {
        "path": relative_path,
        "filename": raw_path.name,
        "flags": data.flags,
        "complex": complex_data,
        "operating_point": operating_point,
        "axis_variable": None if operating_point else axis_name,
        # An AC capture's vectors are complex; the viewer plots magnitude, and
        # the requirement engine remains the place exact gain and phase are
        # measured.
        "axis_unit": ""
        if operating_point
        else trace_unit(data.types.get(axis_name, ""))
        or ("Hz" if axis_name.casefold() == "frequency" else "s"),
        "signal_kind": "magnitude" if complex_data else "value",
        "variables": data.variables,
        "units": {name: trace_unit(data.types.get(name, "")) for name in wanted},
        "step_count": data.step_count,
        "total_points": data.points,
        "returned_points": len(indices),
        "axis": axis,
        "series": series,
    }


def capture_csv(raw_path: Path) -> str:
    """Render one capture as CSV at full resolution.

    Mirrors raw_parser.export_csv, which writes to a file; the viewer streams
    the same columns to the browser without leaving a file behind in the run.
    """
    data = raw_parser.parse_raw(raw_path)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = ["point"]
    for name in data.variables:
        if isinstance(data.values[name][0], complex):
            columns.extend([f"{name}_real", f"{name}_imag"])
        else:
            columns.append(name)
    writer.writerow(columns)
    for index in range(data.points):
        row: list[float | int] = [index]
        for name in data.variables:
            value = data.values[name][index]
            if isinstance(value, complex):
                row.extend([value.real, value.imag])
            else:
                row.append(value)
        writer.writerow(row)
    return buffer.getvalue()
