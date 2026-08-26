#!/usr/bin/env python3
"""Minimal parser for LTspice's UTF-16LE-header binary .raw format."""

from __future__ import annotations

import csv
import re
import struct
from dataclasses import dataclass
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

    @property
    def points(self) -> int:
        return len(next(iter(self.values.values())))


def step_slices(data: RawData) -> list[slice]:
    """Return each stepped block using resets in the independent axis."""
    axis = data.values[data.variables[0]]
    real_axis = [float(value.real if isinstance(value, complex) else value) for value in axis]
    boundaries = [
        index
        for index in range(1, len(real_axis))
        if real_axis[index] < real_axis[index - 1]
    ]
    starts = [0, *boundaries]
    stops = [*boundaries, len(real_axis)]
    slices = [slice(start, stop) for start, stop in zip(starts, stops)]
    if len(slices) != data.step_count:
        raise ValueError(
            f"Expected {data.step_count} stepped blocks, found {len(slices)} axis segments"
        )
    return slices


def _step_shape(values: list[float | complex]) -> tuple[int, int | None]:
    """Infer stepped blocks from a reset in the first, monotonic axis."""
    if len(values) < 2:
        return 1, len(values)
    axis = [float(value.real if isinstance(value, complex) else value) for value in values]
    boundaries = [index for index in range(1, len(axis)) if axis[index] < axis[index - 1]]
    if not boundaries:
        return 1, len(axis)
    starts = [0, *boundaries]
    lengths = [end - start for start, end in zip(starts, [*boundaries, len(axis)])]
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
    variable_count = int(header_value("No. Variables"))
    point_count = int(header_value("No. Points"))

    variables: list[str] = []
    in_variables = False
    for line in lines:
        if line == "Variables:":
            in_variables = True
            continue
        if in_variables and line.strip():
            parts = re.split(r"\s+", line.strip(), maxsplit=2)
            if len(parts) >= 2 and parts[0].isdigit():
                variables.append(parts[1])
    if len(variables) != variable_count:
        raise ValueError(
            f"Expected {variable_count} variables, found {len(variables)} in {path}"
        )

    if data_mode == "values":
        text = raw[data_offset:].decode(text_encoding_name)
        rows = [line for line in text.splitlines() if line.strip()]
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
                    parts = parts[1:]
                if not parts:
                    raise ValueError(f"Missing value at point {point} in {path}")
                values[name].append(ascii_value(parts[-1]))
                cursor += 1
        step_count, points_per_step = _step_shape(values[variables[0]])
        return RawData(flags=flags, variables=variables, values=values, step_count=step_count, points_per_step=points_per_step)

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
        elif remaining >= compact_bytes:
            precision = "compact"
            point_bytes = 8 + (variable_count - 1) * 4
            expected_bytes = compact_bytes
        else:
            expected_bytes = compact_bytes
            point_bytes = 8 + (variable_count - 1) * 4
            precision = "compact"
    data = raw[data_offset : data_offset + expected_bytes]
    if len(data) != expected_bytes:
        raise ValueError(
            f"Expected {expected_bytes} data bytes, found {len(data)} in {path}"
        )

    values = {name: [] for name in variables}
    def value_offset(point: int, variable_index: int) -> tuple[int, str]:
        if fast_access:
            offset = 0
            for prior_index in range(variable_index):
                if is_complex or precision == "double" or prior_index == 0:
                    offset += point_count * (16 if is_complex else 8)
                else:
                    offset += point_count * 4
            if is_complex:
                return offset + point * 16, "complex"
            if precision == "double" or variable_index == 0:
                return offset + point * 8, "double"
            return offset + point * 4, "float"

        if is_complex:
            return point * point_bytes + variable_index * 16, "complex"
        if precision == "double":
            return point * point_bytes + variable_index * 8, "double"
        if variable_index == 0:
            return point * point_bytes, "double"
        return point * point_bytes + 8 + (variable_index - 1) * 4, "float"

    for point in range(point_count):
        for variable_index, name in enumerate(variables):
            offset, value_type = value_offset(point, variable_index)
            if value_type == "complex":
                real, imaginary = struct.unpack_from("<dd", data, offset)
                values[name].append(complex(real, imaginary))
            elif value_type == "double":
                values[name].append(struct.unpack_from("<d", data, offset)[0])
            else:
                values[name].append(struct.unpack_from("<f", data, offset)[0])

    step_count, points_per_step = _step_shape(values[variables[0]])
    return RawData(flags=flags, variables=variables, values=values, step_count=step_count, points_per_step=points_per_step)


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
