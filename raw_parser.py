#!/usr/bin/env python3
"""Minimal parser for LTspice's UTF-16LE-header binary .raw format."""

from __future__ import annotations

import csv
import re
import struct
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from ltspice_text import text_encoding

MAX_RAW_FILE_BYTES = 256 * 1024 * 1024


@dataclass
class RawData:
    flags: str
    variables: list[str]
    values: dict[str, list[float | complex]]
    step_count: int = 1
    points_per_step: int | None = None
    # The third column of each Variables line -- "voltage", "device_current",
    # "time" and so on. LTspice always writes it; a hand-made RAW may not.
    types: dict[str, str] = dataclass_field(default_factory=dict)
    # "Transient Analysis", "AC Analysis", "Operating Point", "DC transfer
    # characteristic" -- what kind of run wrote the file.
    plotname: str = ""

    @property
    def points(self) -> int:
        return len(next(iter(self.values.values())))


def step_slices(data: RawData) -> list[slice]:
    """Return each stepped block using resets in the independent axis."""
    if data.points_per_step == 1 and data.step_count == data.points:
        return [slice(index, index + 1) for index in range(data.points)]
    axis = data.values[data.variables[0]]
    boundaries = _step_boundaries(axis)
    starts = [0, *boundaries]
    stops = [*boundaries, len(axis)]
    slices = [slice(start, stop) for start, stop in zip(starts, stops)]
    if len(slices) != data.step_count:
        raise ValueError(
            f"Expected {data.step_count} stepped blocks, found {len(slices)} axis segments"
        )
    return slices


def _step_boundaries(values: list[float | complex]) -> list[int]:
    """A reset reverses the sweep's initial nonzero direction (including DC)."""
    axis = [float(value.real if isinstance(value, complex) else value) for value in values]
    direction = next((after > before for before, after in zip(axis, axis[1:]) if after != before), True)
    return [
        index for index in range(1, len(axis))
        if (axis[index] < axis[index - 1] if direction else axis[index] > axis[index - 1])
    ]


def _step_shape(values: list[float | complex]) -> tuple[int, int | None]:
    """Infer stepped blocks from resets in an ascending or descending axis."""
    boundaries = _step_boundaries(values)
    if not boundaries:
        return 1, len(values)
    starts = [0, *boundaries]
    lengths = [end - start for start, end in zip(starts, [*boundaries, len(values)])]
    return len(lengths), lengths[0] if len(set(lengths)) == 1 else None


def _header_and_data_offset(raw: bytes) -> tuple[str, int, str, str]:
    encoding = text_encoding(raw)
    for section, mode in (("Binary:", "binary"), ("Values:", "values")):
        for newline in ("\r\n", "\n"):
            marker = (section + newline).encode(encoding)
            offset = raw.find(marker)
            if offset >= 0:
                end = offset + len(marker)
                header = raw[:end].decode(encoding).removeprefix("\ufeff")
                return header, end, mode, encoding
    raise ValueError("Could not find a Binary or Values section in the .raw file")


def parse_raw(path: Path) -> RawData:
    if path.stat().st_size > MAX_RAW_FILE_BYTES:
        raise ValueError(f"RAW file exceeds {MAX_RAW_FILE_BYTES} bytes")
    raw = path.read_bytes()
    header, data_offset, data_mode, text_encoding_name = _header_and_data_offset(raw)
    lines = header.splitlines()

    def header_value(prefix: str) -> str:
        for line in lines:
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        raise ValueError(f"Missing {prefix} header in {path}")

    flags = header_value("Flags")
    plotname = next(
        (line.split(":", 1)[1].strip() for line in lines if line.startswith("Plotname:")),
        "",
    )
    point_steps = (
        "stepped" in flags.lower().split()
        and plotname.casefold() == "operating point"
    )
    variable_count = int(header_value("No. Variables"))
    point_count = int(header_value("No. Points"))
    if variable_count < 1 or point_count < 1:
        raise ValueError("RAW variable and point counts must be positive")

    variables: list[str] = []
    variable_types: dict[str, str] = {}
    in_variables = False
    for line in lines:
        if line == "Variables:":
            in_variables = True
            continue
        if in_variables and line.strip():
            parts = re.split(r"\s+", line.strip(), maxsplit=2)
            if len(parts) >= 2 and parts[0].isdigit():
                if int(parts[0]) != len(variables):
                    raise ValueError("RAW variable indexes must be consecutive from zero")
                variables.append(parts[1])
                if len(parts) >= 3:
                    variable_types[parts[1]] = parts[2].strip()
    if len(variables) != variable_count:
        raise ValueError(
            f"Expected {variable_count} variables, found {len(variables)} in {path}"
        )
    if len({name.casefold() for name in variables}) != variable_count:
        raise ValueError("RAW variable names must be unique")

    if data_mode == "values":
        text = raw[data_offset:].decode(text_encoding_name)
        rows = [line for line in text.splitlines() if line.strip()]
        if len(rows) != point_count * variable_count:
            raise ValueError("Values row count does not match RAW dimensions")
        values = {name: [] for name in variables}
        is_complex = "complex" in flags.lower()

        def ascii_value(token: str) -> float | complex:
            token = token.strip().strip("()")
            if not is_complex:
                return float(token)
            parts = token.split(",")
            if len(parts) != 2:
                raise ValueError(f"Invalid complex Values entry in {path}")
            return complex(float(parts[0]), float(parts[1]))

        cursor = 0
        for point in range(point_count):
            if cursor >= len(rows):
                raise ValueError(f"Unexpected end of Values data in {path}")
            for variable_index, name in enumerate(variables):
                if cursor >= len(rows):
                    raise ValueError(f"Unexpected end of Values data in {path}")
                parts = rows[cursor].split()
                if variable_index == 0:
                    if not parts or parts[0] != str(point):
                        raise ValueError("Values point indexes must be consecutive from zero")
                    parts = parts[1:]
                if not parts:
                    raise ValueError(f"Missing value at point {point} in {path}")
                values[name].append(ascii_value(parts[-1]))
                cursor += 1
        step_count, points_per_step = (point_count, 1) if point_steps else _step_shape(values[variables[0]])
        return RawData(flags=flags, variables=variables, values=values, step_count=step_count, points_per_step=points_per_step, types=variable_types, plotname=plotname)

    is_complex = "complex" in flags.lower()
    fast_access = "fastaccess" in flags.lower()
    remaining = len(raw) - data_offset
    if is_complex:
        precision = "double"
        point_bytes = variable_count * 16
        expected_bytes = point_count * point_bytes
    else:
        # LTspice normally stores the axis as float64 and traces as float32.
        # .options numdgt>6 can make all real vectors float64 instead.
        compact_bytes = point_count * (8 + (variable_count - 1) * 4)
        double_bytes = point_count * variable_count * 8
        if remaining == double_bytes:
            precision = "double"
            point_bytes = variable_count * 8
            expected_bytes = double_bytes
        elif remaining == compact_bytes:
            precision = "compact"
            point_bytes = 8 + (variable_count - 1) * 4
            expected_bytes = compact_bytes
        else:
            expected_bytes = compact_bytes
            point_bytes = 8 + (variable_count - 1) * 4
            precision = "compact"
    data = memoryview(raw)[data_offset : data_offset + expected_bytes]
    if remaining != expected_bytes:
        raise ValueError(
            f"Expected {expected_bytes} data bytes, found {remaining} in {path}"
        )

    if fast_access:
        columns = _decode_fast_access(data, point_count, variable_count, is_complex, precision)
    else:
        columns = _decode_point_major(data, point_count, variable_count, is_complex, precision)
    values = dict(zip(variables, columns))

    # LTspice may use the sign bit on binary transient-axis samples while
    # compressing a RAW file. The physical time coordinate is the magnitude;
    # leaving the sign intact creates false step boundaries.
    if variables[0].casefold() == "time":
        values[variables[0]] = [abs(value) for value in values[variables[0]]]

    step_count, points_per_step = (point_count, 1) if point_steps else _step_shape(values[variables[0]])
    return RawData(flags=flags, variables=variables, values=values, step_count=step_count, points_per_step=points_per_step, types=variable_types, plotname=plotname)


# Rows decoded per struct.iter_unpack chunk; bounds the transient row tuples
# held alongside the output columns for large point-major payloads.
_ROW_CHUNK = 4096


def _decode_point_major(
    data: memoryview,
    point_count: int,
    variable_count: int,
    is_complex: bool,
    precision: str,
) -> list[list[float | complex]]:
    """Decode point-major rows (every vector's value for point 0, then 1, ...)."""
    if is_complex:
        row_format = "<" + "dd" * variable_count
    elif precision == "double":
        row_format = "<" + "d" * variable_count
    else:
        row_format = "<d" + "f" * (variable_count - 1)
    row = struct.Struct(row_format)
    field_count = variable_count * 2 if is_complex else variable_count
    fields: list[list[float | complex]] = [[] for _ in range(field_count)]
    chunk_bytes = row.size * _ROW_CHUNK
    for start in range(0, point_count * row.size, chunk_bytes):
        chunk = data[start : start + chunk_bytes]
        for field, chunk_values in zip(fields, zip(*row.iter_unpack(chunk))):
            field.extend(chunk_values)
    if not is_complex:
        return fields
    return [
        [complex(real, imaginary) for real, imaginary in zip(fields[index], fields[index + 1])]
        for index in range(0, len(fields), 2)
    ]


def _decode_fast_access(
    data: memoryview,
    point_count: int,
    variable_count: int,
    is_complex: bool,
    precision: str,
) -> list[list[float | complex]]:
    """Decode vector-major FastAccess data (all of vector 0, then vector 1, ...)."""
    columns: list[list[float | complex]] = []
    offset = 0
    for variable_index in range(variable_count):
        if is_complex:
            pairs = struct.unpack_from(f"<{2 * point_count}d", data, offset)
            columns.append(
                [complex(real, imaginary) for real, imaginary in zip(pairs[0::2], pairs[1::2])]
            )
            offset += point_count * 16
        elif precision == "double" or variable_index == 0:
            columns.append(list(struct.unpack_from(f"<{point_count}d", data, offset)))
            offset += point_count * 8
        else:
            columns.append(list(struct.unpack_from(f"<{point_count}f", data, offset)))
            offset += point_count * 4
    return columns


def export_csv(data: RawData, path: Path) -> None:
    """Write each vector as real/imaginary columns in a portable CSV file."""
    def spreadsheet_safe(value: str) -> str:
        return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value

    columns = ["point"]
    for name in data.variables:
        sample = data.values[name][0]
        if isinstance(sample, complex):
            columns.extend(
                [spreadsheet_safe(f"{name}_real"), spreadsheet_safe(f"{name}_imag")]
            )
        else:
            columns.append(spreadsheet_safe(name))

    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
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
