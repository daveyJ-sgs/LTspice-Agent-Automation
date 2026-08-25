"""Deterministic statistical point plans for LTspice experiments."""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import math
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import NotRequired, TypedDict


STATISTICAL_PLAN_SCHEMA_VERSION = 1
UNIFORM_GENERATOR_VERSION = "sha256-counter-uniform-v1"
DISTRIBUTION_GENERATOR_VERSION = "sha256-counter-distributions-v2"
CORRELATION_GENERATOR_VERSION = "sha256-counter-correlations-v3"
EMPIRICAL_GENERATOR_VERSION = "sha256-counter-empirical-v4"
CORNER_GENERATOR_VERSION = "sha256-counter-corners-v5"
STRATIFIED_GENERATOR_VERSION = "sha256-stratified-halton-v6"
STATISTICAL_GENERATOR_VERSION = UNIFORM_GENERATOR_VERSION
SUPPORTED_GENERATOR_VERSIONS = {
    UNIFORM_GENERATOR_VERSION,
    DISTRIBUTION_GENERATOR_VERSION,
    CORRELATION_GENERATOR_VERSION,
    EMPIRICAL_GENERATOR_VERSION,
    CORNER_GENERATOR_VERSION,
    STRATIFIED_GENERATOR_VERSION,
}
MAX_STATISTICAL_SAMPLES = 1_000
MAX_STATISTICAL_VARIABLES = 32
MAX_STATISTICAL_CELLS = 10_000
MAX_DISCRETE_VALUES = 256
MAX_EMPIRICAL_OBSERVATIONS = 10_000
MAX_EMPIRICAL_CSV_BYTES = 1_000_000
MAX_EMPIRICAL_CSV_COLUMNS = 256
MAX_CORNER_AXES = 8
MAX_CORNER_VALUES = 16
MAX_STATISTICAL_POINTS = 1_000
MAX_GAUSSIAN_ATTEMPTS = 4_096
MIN_GAUSSIAN_SPAN_SIGMA = Decimal("0.1")
PSD_TOLERANCE = Decimal("1e-60")
MAX_SEED = (1 << 63) - 1
SAMPLING_METHODS = {"independent", "latin_hypercube", "halton"}
HALTON_PRIMES = (
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53,
    59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131,
)


class StatisticalVariable(TypedDict):
    name: str
    distribution: str
    minimum: NotRequired[float]
    maximum: NotRequired[float]
    nominal: NotRequired[float | str]
    sigma: NotRequired[float]
    values: NotRequired[list[float | str]]
    weights: NotRequired[list[float]]
    csv_path: NotRequired[str]
    column: NotRequired[str]
    unit: NotRequired[str]


class StatisticalCorrelation(TypedDict):
    variables: list[str]
    matrix: list[list[float]]


class StatisticalCornerValue(TypedDict):
    name: str
    value: float | str


class StatisticalCornerAxis(TypedDict):
    name: str
    parameter: str
    unit: NotRequired[str]
    values: list[StatisticalCornerValue]


class StatisticalPlanPoint(TypedDict):
    index: int
    parameters: dict[str, str]
    sample_index: NotRequired[int]
    corners: NotRequired[dict[str, str]]


class StatisticalPlan(TypedDict):
    schema_version: int
    generator_version: str
    definition_hash: str
    definition: dict[str, object]
    parameter_order: list[str]
    parameter_units: dict[str, str]
    sample_count: int
    point_count: NotRequired[int]
    points: list[StatisticalPlanPoint]


class StatisticalPlanResult(TypedDict):
    plan_id: str
    plan_file: str
    plan_sha256: str
    generator_version: str
    definition_hash: str
    correlations: list[dict[str, object]]
    empirical_sources: list[dict[str, object]]
    corner_axes: list[dict[str, object]]
    corner_aggregate: bool
    sampling_method: str
    sample_count: int
    point_count: int
    parameter_order: list[str]
    parameter_units: dict[str, str]
    points: list[StatisticalPlanPoint]


def _canonical_json(value: object, *, pretty: bool = False) -> str:
    options: dict[str, object] = {
        "sort_keys": True,
        "ensure_ascii": False,
        "allow_nan": False,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return json.dumps(value, **options)  # type: ignore[arg-type]


def _decimal(value: object, field: str) -> Decimal:
    if (
        not isinstance(value, (int, float, str, Decimal))
        or isinstance(value, bool)
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ValueError(f"{field} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 17
        rounded = +value
    if rounded.is_zero():
        return "0"
    encoded = format(rounded.normalize(), "g").lower()
    if len(encoded) > 128:
        raise ValueError("generated statistical value is too long")
    return encoded


def _bounded_canonical_decimal(
    value: Decimal, minimum: Decimal, maximum: Decimal
) -> str:
    encoded = _canonical_decimal(value)
    rounded = Decimal(encoded)
    if rounded < minimum:
        return _canonical_decimal(minimum)
    if rounded > maximum:
        return _canonical_decimal(maximum)
    return encoded


def _cholesky_psd(matrix: list[list[Decimal]]) -> list[list[Decimal]]:
    """Return a deterministic lower factor, accepting singular PSD matrices."""
    size = len(matrix)
    factor = [[Decimal(0) for _ in range(size)] for _ in range(size)]
    with localcontext() as context:
        context.prec = 80
        for row in range(size):
            for column in range(row + 1):
                residual = matrix[row][column] - sum(
                    (
                        factor[row][index] * factor[column][index]
                        for index in range(column)
                    ),
                    Decimal(0),
                )
                if row == column:
                    if residual < -PSD_TOLERANCE:
                        raise ValueError(
                            "correlation matrix must be positive semidefinite"
                        )
                    factor[row][column] = (
                        Decimal(0) if residual <= PSD_TOLERANCE else residual.sqrt()
                    )
                elif factor[column][column] == 0:
                    if abs(residual) > PSD_TOLERANCE:
                        raise ValueError(
                            "correlation matrix must be positive semidefinite"
                        )
                else:
                    factor[row][column] = residual / factor[column][column]
    return factor


def _normalized_correlations(
    correlations: list[StatisticalCorrelation] | None,
    variables: list[dict[str, object]],
) -> list[dict[str, object]]:
    if correlations is None:
        return []
    if not isinstance(correlations, list):
        raise ValueError("correlations must be a list")
    variables_by_name = {str(variable["name"]): variable for variable in variables}
    normalized: list[dict[str, object]] = []
    used_names: set[str] = set()
    for group in correlations:
        if not isinstance(group, dict):
            raise ValueError("correlations must contain objects")
        if set(group) != {"variables", "matrix"}:
            raise ValueError("correlation groups require only variables and matrix")
        names = group.get("variables")
        matrix = group.get("matrix")
        if not isinstance(names, list) or len(names) < 2:
            raise ValueError("correlation groups require at least two variables")
        if any(not isinstance(name, str) for name in names) or len(names) != len(
            set(names)
        ):
            raise ValueError(
                "correlation group variables must be strings without duplicates"
            )
        unknown = next((name for name in names if name not in variables_by_name), None)
        if unknown is not None:
            raise ValueError(f"correlation group references unknown variable {unknown}")
        non_gaussian = next(
            (
                name
                for name in names
                if variables_by_name[name]["distribution"] != "gaussian"
            ),
            None,
        )
        if non_gaussian is not None:
            raise ValueError(
                f"correlation variable {non_gaussian} must use gaussian distribution"
            )
        if used_names.intersection(names):
            raise ValueError("correlation groups must not contain overlapping variables")
        if (
            not isinstance(matrix, list)
            or len(matrix) != len(names)
            or any(not isinstance(row, list) or len(row) != len(names) for row in matrix)
        ):
            raise ValueError("correlation matrix must be square and match its variables")
        parsed = [
            [
                _decimal(value, "correlation matrix value")
                for value in row
            ]
            for row in matrix
        ]
        if any(abs(value) > 1 for row in parsed for value in row):
            raise ValueError("correlation matrix values must be between -1 and 1")
        if any(parsed[index][index] != 1 for index in range(len(names))):
            raise ValueError("correlation matrix diagonal must contain 1")
        if any(
            parsed[row][column] != parsed[column][row]
            for row in range(len(names))
            for column in range(row)
        ):
            raise ValueError("correlation matrix must be symmetric")

        original_positions = {name: index for index, name in enumerate(names)}
        canonical_names = sorted(names)
        canonical_matrix = [
            [
                parsed[original_positions[row_name]][original_positions[column_name]]
                for column_name in canonical_names
            ]
            for row_name in canonical_names
        ]
        _cholesky_psd(canonical_matrix)
        normalized.append(
            {
                "variables": canonical_names,
                "matrix": [
                    [_canonical_decimal(value) for value in row]
                    for row in canonical_matrix
                ],
            }
        )
        used_names.update(names)
    return sorted(normalized, key=lambda group: group["variables"])


def _empirical_values(values: object, name: str) -> list[str]:
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_EMPIRICAL_OBSERVATIONS
    ):
        raise ValueError(
            f"variable {name} empirical observations must contain 1 to "
            f"{MAX_EMPIRICAL_OBSERVATIONS} values"
        )
    return [
        _canonical_decimal(_decimal(value, f"variable {name} empirical observation"))
        for value in values
    ]


def _inline_empirical_sha256(values: list[str]) -> str:
    return hashlib.sha256(
        (_canonical_json(values) + "\n").encode("utf-8")
    ).hexdigest()


def _read_empirical_csv(
    csv_path: object,
    column: object,
    source_root: Path | None,
    name: str,
) -> tuple[list[str], dict[str, object]]:
    if not isinstance(csv_path, str) or not csv_path or len(csv_path) > 1_024:
        raise ValueError(f"variable {name} csv_path must be a non-empty string")
    if not isinstance(column, str) or not column or len(column) > 128:
        raise ValueError(f"variable {name} column must be a non-empty string")
    if source_root is None:
        raise ValueError(f"variable {name} csv_path requires source_root")
    root = source_root.resolve()
    candidate = Path(csv_path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        lexical_relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"variable {name} empirical CSV must be inside source_root"
        ) from exc
    current = root
    for part in lexical_relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"variable {name} empirical CSV must not use symlinks"
            )
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"variable {name} empirical CSV must be inside source_root"
        ) from exc
    if not resolved.is_file():
        raise ValueError(f"variable {name} empirical CSV must be a regular file")
    with resolved.open("rb") as handle:
        raw = handle.read(MAX_EMPIRICAL_CSV_BYTES + 1)
    if len(raw) > MAX_EMPIRICAL_CSV_BYTES:
        raise ValueError(
            f"variable {name} empirical CSV exceeds {MAX_EMPIRICAL_CSV_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"variable {name} empirical CSV must be UTF-8") from exc
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows)
        if (
            not header
            or len(header) > MAX_EMPIRICAL_CSV_COLUMNS
            or any(not value or len(value) > 128 for value in header)
            or len(header) != len(set(header))
        ):
            raise ValueError(f"variable {name} empirical CSV header is invalid")
        if column not in header:
            raise ValueError(
                f"variable {name} empirical CSV does not contain column {column}"
            )
        column_index = header.index(column)
        observations: list[object] = []
        for row_index, row in enumerate(rows, start=2):
            if len(row) != len(header):
                raise ValueError(
                    f"variable {name} empirical CSV row {row_index} has "
                    "the wrong number of columns"
                )
            observations.append(row[column_index])
            if len(observations) > MAX_EMPIRICAL_OBSERVATIONS:
                raise ValueError(
                    f"variable {name} empirical CSV exceeds "
                    f"{MAX_EMPIRICAL_OBSERVATIONS} observations"
                )
    except csv.Error as exc:
        raise ValueError(f"variable {name} empirical CSV is malformed") from exc
    values = _empirical_values(observations, name)
    return values, {
        "kind": "csv",
        "path": relative.as_posix(),
        "column": column,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "observation_count": len(values),
        "resampling": "with_replacement",
    }


def _validated_empirical_source(
    source: object, values: list[str], name: str
) -> dict[str, object]:
    if not isinstance(source, dict):
        raise ValueError(f"variable {name} empirical source is invalid")
    kind = source.get("kind")
    expected_fields = (
        {"kind", "sha256", "observation_count", "resampling"}
        if kind == "inline"
        else {
            "kind",
            "path",
            "column",
            "sha256",
            "observation_count",
            "resampling",
        }
        if kind == "csv"
        else set()
    )
    if set(source) != expected_fields:
        raise ValueError(f"variable {name} empirical source is invalid")
    digest = source.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"variable {name} empirical source hash is invalid")
    if source.get("observation_count") != len(values):
        raise ValueError(f"variable {name} empirical source count is invalid")
    if source.get("resampling") != "with_replacement":
        raise ValueError(f"variable {name} empirical resampling is invalid")
    if kind == "inline" and digest != _inline_empirical_sha256(values):
        raise ValueError(f"variable {name} empirical source hash is invalid")
    if kind == "csv":
        path = source.get("path")
        column = source.get("column")
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(column, str)
            or not column
            or len(column) > 128
        ):
            raise ValueError(f"variable {name} empirical CSV source is invalid")
    return dict(source)


def _corner_value(value: object, axis_name: str) -> str:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _canonical_decimal(_decimal(value, f"corner axis {axis_name} value"))
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Za-z0-9_.+\-]+", value) is None
    ):
        raise ValueError(
            f"corner axis {axis_name} values must be a single SPICE token"
        )
    try:
        return _canonical_decimal(_decimal(value, f"corner axis {axis_name} value"))
    except ValueError:
        return value


def _normalized_corner_axes(
    corner_axes: list[StatisticalCornerAxis] | None,
    variable_names: set[str],
    sample_count: int,
    corner_aggregate: bool,
) -> list[dict[str, object]]:
    if not isinstance(corner_aggregate, bool):
        raise ValueError("corner_aggregate must be a boolean")
    if corner_axes is None:
        corner_axes = []
    if not isinstance(corner_axes, list):
        raise ValueError("corner_axes must be a list")
    if not corner_axes:
        if corner_aggregate:
            raise ValueError("corner_aggregate requires corner_axes")
        return []
    if len(corner_axes) > MAX_CORNER_AXES:
        raise ValueError(f"corner plans are limited to {MAX_CORNER_AXES} axes")

    normalized: list[dict[str, object]] = []
    axis_names: set[str] = set()
    parameter_names: set[str] = set()
    corner_count = 1
    for axis in corner_axes:
        if (
            not isinstance(axis, dict)
            or not {"name", "parameter", "values"}.issubset(axis)
            or set(axis) - {"name", "parameter", "unit", "values"}
        ):
            raise ValueError(
                "corner axes require only name, parameter, unit, and values"
            )
        axis_name = axis.get("name")
        parameter = axis.get("parameter")
        if (
            not isinstance(axis_name, str)
            or len(axis_name) > 64
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", axis_name) is None
        ):
            raise ValueError("corner axis names must match [A-Za-z_][A-Za-z0-9_]*")
        if axis_name in axis_names:
            raise ValueError(f"duplicate axis name: {axis_name}")
        if (
            not isinstance(parameter, str)
            or len(parameter) > 64
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parameter) is None
        ):
            raise ValueError(
                "corner parameter names must match [A-Za-z_][A-Za-z0-9_]*"
            )
        if parameter in parameter_names:
            raise ValueError(f"duplicate corner parameter: {parameter}")
        if parameter in variable_names:
            raise ValueError(
                f"corner parameter {parameter} collides with a statistical variable"
            )
        unit = axis.get("unit", "")
        if not isinstance(unit, str) or len(unit) > 64:
            raise ValueError(
                f"corner axis {axis_name} unit must be at most 64 characters"
            )
        values = axis.get("values")
        if (
            not isinstance(values, list)
            or not values
            or len(values) > MAX_CORNER_VALUES
        ):
            raise ValueError(
                f"corner axis {axis_name} values must contain 1 to "
                f"{MAX_CORNER_VALUES} entries"
            )
        normalized_values: list[dict[str, str]] = []
        value_names: set[str] = set()
        for entry in values:
            if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
                raise ValueError(
                    f"corner axis {axis_name} values require only name and value"
                )
            value_name = entry.get("name")
            if (
                not isinstance(value_name, str)
                or not value_name
                or len(value_name) > 64
                or any(ord(character) < 32 for character in value_name)
            ):
                raise ValueError(
                    f"corner axis {axis_name} value names must be non-empty labels"
                )
            if value_name in value_names:
                raise ValueError(
                    f"corner axis {axis_name} value names must be unique"
                )
            value_names.add(value_name)
            normalized_values.append(
                {
                    "name": value_name,
                    "value": _corner_value(entry.get("value"), axis_name),
                }
            )
        corner_count *= len(normalized_values)
        if sample_count * corner_count > MAX_STATISTICAL_POINTS:
            raise ValueError(
                f"corner plans are limited to {MAX_STATISTICAL_POINTS:,} expanded points"
            )
        normalized.append(
            {
                "name": axis_name,
                "parameter": parameter,
                "unit": unit,
                "values": normalized_values,
            }
        )
        axis_names.add(axis_name)
        parameter_names.add(parameter)
    if (len(variable_names) + len(normalized)) * sample_count * corner_count > (
        MAX_STATISTICAL_CELLS
    ):
        raise ValueError(
            f"corner plans are limited to {MAX_STATISTICAL_CELLS:,} values"
        )
    return normalized


def _normalized_definition(
    variables: list[StatisticalVariable],
    sample_count: int,
    seed: int,
    correlations: list[StatisticalCorrelation] | None = None,
    corner_axes: list[StatisticalCornerAxis] | None = None,
    corner_aggregate: bool = False,
    sampling_method: str = "independent",
    source_root: Path | None = None,
    allow_empirical_provenance: bool = False,
) -> dict[str, object]:
    if not isinstance(variables, list) or not variables:
        raise ValueError("variables must be a non-empty list")
    if len(variables) > MAX_STATISTICAL_VARIABLES:
        raise ValueError(
            f"statistical plans are limited to {MAX_STATISTICAL_VARIABLES} variables"
        )
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 1
        or sample_count > MAX_STATISTICAL_SAMPLES
    ):
        raise ValueError(
            f"sample_count must be between 1 and {MAX_STATISTICAL_SAMPLES}"
        )
    if len(variables) * sample_count > MAX_STATISTICAL_CELLS:
        raise ValueError(
            f"statistical plans are limited to {MAX_STATISTICAL_CELLS} values"
        )
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or seed < 0
        or seed > MAX_SEED
    ):
        raise ValueError(f"seed must be an integer between 0 and {MAX_SEED}")
    if sampling_method not in SAMPLING_METHODS:
        raise ValueError(
            "sampling_method must be 'independent', 'latin_hypercube', or 'halton'"
        )

    normalized_variables: list[dict[str, object]] = []
    seen: set[str] = set()
    for variable in variables:
        if not isinstance(variable, dict):
            raise ValueError("variables must contain objects")
        name = variable.get("name")
        if (
            not isinstance(name, str)
            or len(name) > 64
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise ValueError("variable names must match [A-Za-z_][A-Za-z0-9_]*")
        if name in seen:
            raise ValueError(f"duplicate statistical variable name: {name}")
        seen.add(name)
        unit = variable.get("unit", "")
        if not isinstance(unit, str) or len(unit) > 64:
            raise ValueError(
                f"variable {name} unit must be a string of at most 64 characters"
            )
        distribution = variable.get("distribution")
        allowed_fields = {
            "uniform": {
                "name",
                "distribution",
                "minimum",
                "maximum",
                "nominal",
                "unit",
            },
            "gaussian": {
                "name",
                "distribution",
                "minimum",
                "maximum",
                "nominal",
                "sigma",
                "unit",
            },
            "discrete": {
                "name",
                "distribution",
                "values",
                "weights",
                "nominal",
                "unit",
            },
            "empirical": {
                "name",
                "distribution",
                "values",
                "csv_path",
                "column",
                "unit",
                "source",
            },
        }
        if distribution in allowed_fields:
            unexpected = sorted(set(variable) - allowed_fields[distribution])
            if unexpected:
                raise ValueError(
                    f"variable {name} fields are not valid for {distribution}: "
                    f"{', '.join(unexpected)}"
                )
        if distribution in {"uniform", "gaussian"}:
            minimum = _decimal(variable.get("minimum"), f"variable {name} minimum")
            maximum = _decimal(variable.get("maximum"), f"variable {name} maximum")
            if minimum >= maximum:
                raise ValueError(f"variable {name} minimum must be less than maximum")
            nominal_default: object = (minimum + maximum) / 2
            if distribution == "gaussian" and "nominal" not in variable:
                raise ValueError(f"variable {name} gaussian nominal is required")
            nominal = _decimal(
                variable.get("nominal", nominal_default),
                f"variable {name} nominal",
            )
            if nominal < minimum or nominal > maximum:
                raise ValueError(f"variable {name} nominal must be within its bounds")
            normalized: dict[str, object] = {
                "name": name,
                "distribution": distribution,
                "minimum": _canonical_decimal(minimum),
                "maximum": _canonical_decimal(maximum),
                "nominal": _canonical_decimal(nominal),
                "unit": unit,
            }
            if distribution == "gaussian":
                sigma = _decimal(variable.get("sigma"), f"variable {name} sigma")
                if sigma <= 0:
                    raise ValueError(f"variable {name} sigma must be positive")
                if (maximum - minimum) / sigma < MIN_GAUSSIAN_SPAN_SIGMA:
                    raise ValueError(
                        f"variable {name} bounds must span at least "
                        f"{MIN_GAUSSIAN_SPAN_SIGMA} sigma"
                    )
                normalized["sigma"] = _canonical_decimal(sigma)
            normalized_variables.append(normalized)
        elif distribution == "discrete":
            values = variable.get("values")
            weights = variable.get("weights")
            if (
                not isinstance(values, list)
                or not values
                or len(values) > MAX_DISCRETE_VALUES
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > 128
                    for value in values
                )
                or len(values) != len(set(values))
            ):
                raise ValueError(
                    f"variable {name} values must be 1 to {MAX_DISCRETE_VALUES} "
                    "unique non-empty strings"
                )
            if not isinstance(weights, list) or len(weights) != len(values):
                raise ValueError(f"variable {name} weights must match its values")
            parsed_weights = [
                _decimal(weight, f"variable {name} weight") for weight in weights
            ]
            if any(weight <= 0 for weight in parsed_weights):
                raise ValueError(f"variable {name} weights must be positive")
            weight_total = sum(parsed_weights, Decimal(0))
            with localcontext() as context:
                context.prec = 80
                normalized_weights = [
                    _canonical_decimal(weight / weight_total)
                    for weight in parsed_weights
                ]
            nominal_value = variable.get("nominal", values[0])
            if not isinstance(nominal_value, str) or nominal_value not in values:
                raise ValueError(f"variable {name} nominal must be one of its values")
            normalized_variables.append(
                {
                    "name": name,
                    "distribution": "discrete",
                    "values": list(values),
                    "weights": normalized_weights,
                    "nominal": nominal_value,
                    "unit": unit,
                }
            )
        elif distribution == "empirical":
            if "source" in variable:
                if not allow_empirical_provenance:
                    raise ValueError(
                        f"variable {name} source is not valid for empirical input"
                    )
                if "csv_path" in variable or "column" in variable:
                    raise ValueError(
                        f"variable {name} normalized empirical input is invalid"
                    )
                values = _empirical_values(variable.get("values"), name)
                source = _validated_empirical_source(
                    variable.get("source"), values, name
                )
            else:
                has_values = "values" in variable
                has_csv = "csv_path" in variable or "column" in variable
                if has_values == has_csv:
                    raise ValueError(
                        f"variable {name} empirical input must use either values "
                        "or csv_path with column"
                    )
                if has_values:
                    values = _empirical_values(variable.get("values"), name)
                    source = {
                        "kind": "inline",
                        "sha256": _inline_empirical_sha256(values),
                        "observation_count": len(values),
                        "resampling": "with_replacement",
                    }
                else:
                    values, source = _read_empirical_csv(
                        variable.get("csv_path"),
                        variable.get("column"),
                        source_root,
                        name,
                    )
            normalized_variables.append(
                {
                    "name": name,
                    "distribution": "empirical",
                    "values": values,
                    "unit": unit,
                    "source": source,
                }
            )
        else:
            raise ValueError(
                f"variable {name} distribution must be 'uniform', "
                "'gaussian', 'discrete', or 'empirical'"
            )
    definition: dict[str, object] = {
        "variables": normalized_variables,
        "sample_count": sample_count,
        "seed": seed,
    }
    normalized_correlations = _normalized_correlations(
        correlations, normalized_variables
    )
    if normalized_correlations:
        definition["correlations"] = normalized_correlations
    normalized_corner_axes = _normalized_corner_axes(
        corner_axes,
        {str(variable["name"]) for variable in normalized_variables},
        sample_count,
        corner_aggregate,
    )
    if normalized_corner_axes:
        definition["corner_axes"] = normalized_corner_axes
        definition["corner_aggregate"] = corner_aggregate
    if sampling_method != "independent":
        gaussian = next(
            (
                str(variable["name"])
                for variable in normalized_variables
                if variable["distribution"] == "gaussian"
            ),
            None,
        )
        if gaussian is not None:
            raise ValueError(
                f"sampling_method {sampling_method} does not support gaussian "
                f"variable {gaussian}"
            )
        definition["sampling_method"] = sampling_method
    return definition


def _sampler_digest(*parts: object) -> bytes:
    return hashlib.sha256(
        b"\0".join(
            [STRATIFIED_GENERATOR_VERSION.encode("ascii")]
            + [str(part).encode("utf-8") for part in parts]
        )
    ).digest()


def _sampler_unit_fraction(*parts: object) -> Decimal:
    integer = int.from_bytes(_sampler_digest(*parts)[:8], "big")
    return Decimal(integer) / Decimal(1 << 64)


def _latin_hypercube_fractions(
    seed: int, name: str, sample_count: int
) -> list[Decimal]:
    strata = sorted(
        range(sample_count),
        key=lambda index: (
            _sampler_digest("latin_hypercube", seed, name, index),
            index,
        ),
    )
    count = Decimal(sample_count)
    return [
        (
            Decimal(strata[sample_index])
            + _sampler_unit_fraction(
                "latin_hypercube", seed, name, sample_index, "jitter"
            )
        )
        / count
        for sample_index in range(sample_count)
    ]


def _halton_fractions(
    seed: int, name: str, sample_count: int, dimension: int
) -> list[Decimal]:
    base = HALTON_PRIMES[dimension]
    nonzero_digits = sorted(
        range(1, base),
        key=lambda digit: (
            _sampler_digest("halton", seed, name, "digit", digit),
            digit,
        ),
    )
    permutation = [0, *nonzero_digits]
    shift = _sampler_unit_fraction("halton", seed, name, "shift")
    fractions: list[Decimal] = []
    for sample_index in range(sample_count):
        index = sample_index + 1
        denominator = Decimal(base)
        radical_inverse = Decimal(0)
        while index:
            index, digit = divmod(index, base)
            radical_inverse += Decimal(permutation[digit]) / denominator
            denominator *= base
        fractions.append((radical_inverse + shift) % Decimal(1))
    return fractions


def _uniform_fraction(seed: int, sample_index: int, name: str) -> Decimal:
    material = b"\0".join(
        (
            UNIFORM_GENERATOR_VERSION.encode("ascii"),
            str(seed).encode("ascii"),
            str(sample_index).encode("ascii"),
            name.encode("utf-8"),
        )
    )
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return Decimal(integer) / Decimal(1 << 64)


def _distribution_fraction(
    distribution: str,
    seed: int,
    sample_index: int,
    name: str,
    attempt: int = 0,
    coordinate: int = 0,
) -> Decimal:
    material = b"\0".join(
        (
            DISTRIBUTION_GENERATOR_VERSION.encode("ascii"),
            distribution.encode("ascii"),
            str(seed).encode("ascii"),
            str(sample_index).encode("ascii"),
            name.encode("utf-8"),
            str(attempt).encode("ascii"),
            str(coordinate).encode("ascii"),
        )
    )
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return Decimal(integer) / Decimal(1 << 64)


def _bounded_gaussian(
    variable: dict[str, object], seed: int, sample_index: int
) -> Decimal:
    name = str(variable["name"])
    minimum = Decimal(str(variable["minimum"]))
    maximum = Decimal(str(variable["maximum"]))
    nominal = Decimal(str(variable["nominal"]))
    sigma = Decimal(str(variable["sigma"]))
    with localcontext() as context:
        context.prec = 80
        for attempt in range(MAX_GAUSSIAN_ATTEMPTS):
            x = 2 * _distribution_fraction(
                "gaussian", seed, sample_index, name, attempt, 0
            ) - 1
            y = 2 * _distribution_fraction(
                "gaussian", seed, sample_index, name, attempt, 1
            ) - 1
            radius_squared = x * x + y * y
            if radius_squared <= 0 or radius_squared >= 1:
                continue
            standard_normal = x * (
                (-2 * radius_squared.ln() / radius_squared).sqrt()
            )
            value = nominal + sigma * standard_normal
            if minimum <= value <= maximum:
                return value
    raise ValueError(
        f"variable {name} could not draw a bounded gaussian value within "
        f"{MAX_GAUSSIAN_ATTEMPTS} attempts"
    )


def _weighted_discrete(
    variable: dict[str, object],
    seed: int,
    sample_index: int,
    fraction: Decimal | None = None,
) -> str:
    name = str(variable["name"])
    values = variable["values"]
    weights = variable["weights"]
    assert isinstance(values, list) and isinstance(weights, list)
    parsed_weights = [Decimal(str(weight)) for weight in weights]
    total = sum(parsed_weights, Decimal(0))
    threshold = (
        _distribution_fraction("discrete", seed, sample_index, name)
        if fraction is None
        else fraction
    ) * total
    cumulative = Decimal(0)
    for value, weight in zip(values, parsed_weights):
        cumulative += weight
        if threshold < cumulative:
            return str(value)
    return str(values[-1])


def _correlation_fraction(
    seed: int,
    sample_index: int,
    group_key: str,
    attempt: int,
    coordinate: int,
) -> Decimal:
    material = b"\0".join(
        (
            CORRELATION_GENERATOR_VERSION.encode("ascii"),
            str(seed).encode("ascii"),
            str(sample_index).encode("ascii"),
            group_key.encode("utf-8"),
            str(attempt).encode("ascii"),
            str(coordinate).encode("ascii"),
        )
    )
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return Decimal(integer) / Decimal(1 << 64)


def _empirical_fraction(seed: int, sample_index: int, name: str) -> Decimal:
    material = b"\0".join(
        (
            EMPIRICAL_GENERATOR_VERSION.encode("ascii"),
            str(seed).encode("ascii"),
            str(sample_index).encode("ascii"),
            name.encode("utf-8"),
        )
    )
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return Decimal(integer) / Decimal(1 << 64)


def _empirical_value(
    variable: dict[str, object],
    seed: int,
    sample_index: int,
    fraction: Decimal | None = None,
) -> str:
    values = variable["values"]
    assert isinstance(values, list)
    index = int(
        (
            _empirical_fraction(seed, sample_index, str(variable["name"]))
            if fraction is None
            else fraction
        )
        * len(values)
    )
    return str(values[index])


def _correlated_gaussians(
    group: dict[str, object],
    factor: list[list[Decimal]],
    variables_by_name: dict[str, dict[str, object]],
    seed: int,
    sample_index: int,
) -> dict[str, Decimal]:
    names = group["variables"]
    assert isinstance(names, list)
    group_key = ",".join(str(name) for name in names)
    with localcontext() as context:
        context.prec = 80
        for attempt in range(MAX_GAUSSIAN_ATTEMPTS):
            independent: list[Decimal] = []
            for coordinate in range(len(names)):
                x = 2 * _correlation_fraction(
                    seed, sample_index, group_key, attempt, coordinate * 2
                ) - 1
                y = 2 * _correlation_fraction(
                    seed, sample_index, group_key, attempt, coordinate * 2 + 1
                ) - 1
                radius_squared = x * x + y * y
                if radius_squared <= 0 or radius_squared >= 1:
                    break
                independent.append(
                    x * ((-2 * radius_squared.ln() / radius_squared).sqrt())
                )
            if len(independent) != len(names):
                continue
            values: dict[str, Decimal] = {}
            for row, name in enumerate(names):
                variable = variables_by_name[str(name)]
                standard_normal = sum(
                    (
                        factor[row][column] * independent[column]
                        for column in range(row + 1)
                    ),
                    Decimal(0),
                )
                value = Decimal(str(variable["nominal"])) + Decimal(
                    str(variable["sigma"])
                ) * standard_normal
                if not (
                    Decimal(str(variable["minimum"]))
                    <= value
                    <= Decimal(str(variable["maximum"]))
                ):
                    break
                values[str(name)] = value
            if len(values) == len(names):
                return values
    raise ValueError(
        f"correlation group {group_key} could not draw bounded gaussian values "
        f"within {MAX_GAUSSIAN_ATTEMPTS} attempts"
    )


def build_statistical_plan(
    variables: list[StatisticalVariable],
    sample_count: int,
    seed: int,
    correlations: list[StatisticalCorrelation] | None = None,
    corner_axes: list[StatisticalCornerAxis] | None = None,
    corner_aggregate: bool = False,
    source_root: Path | None = None,
    _allow_empirical_provenance: bool = False,
    sampling_method: str = "independent",
) -> StatisticalPlan:
    """Build a portable, deterministic plan without running LTspice."""
    definition = _normalized_definition(
        variables,
        sample_count,
        seed,
        correlations,
        corner_axes,
        corner_aggregate,
        sampling_method,
        source_root,
        _allow_empirical_provenance,
    )
    normalized_variables = definition["variables"]
    assert isinstance(normalized_variables, list)
    normalized_correlations = definition.get("correlations", [])
    assert isinstance(normalized_correlations, list)
    normalized_corner_axes = definition.get("corner_axes", [])
    assert isinstance(normalized_corner_axes, list)
    normalized_sampling_method = str(
        definition.get("sampling_method", "independent")
    )
    generator_version = (
        STRATIFIED_GENERATOR_VERSION
        if normalized_sampling_method != "independent"
        else CORNER_GENERATOR_VERSION
        if normalized_corner_axes
        else EMPIRICAL_GENERATOR_VERSION
        if any(
            variable.get("distribution") == "empirical"
            for variable in normalized_variables
        )
        else CORRELATION_GENERATOR_VERSION
        if normalized_correlations
        else UNIFORM_GENERATOR_VERSION
        if all(
            variable.get("distribution") == "uniform"
            for variable in normalized_variables
        )
        else DISTRIBUTION_GENERATOR_VERSION
    )
    parameter_order = [str(variable["name"]) for variable in normalized_variables]
    parameter_units = {
        str(variable["name"]): str(variable["unit"])
        for variable in normalized_variables
    }
    for axis in normalized_corner_axes:
        parameter_order.append(str(axis["parameter"]))
        parameter_units[str(axis["parameter"])] = str(axis["unit"])
    sample_points: list[StatisticalPlanPoint] = []
    variables_by_name = {
        str(variable["name"]): variable for variable in normalized_variables
    }
    correlated_names = {
        str(name)
        for group in normalized_correlations
        for name in group["variables"]
    }
    correlation_factors = {
        ",".join(str(name) for name in group["variables"]): _cholesky_psd(
            [
                [Decimal(str(value)) for value in row]
                for row in group["matrix"]
            ]
        )
        for group in normalized_correlations
    }
    sampling_fractions: dict[str, list[Decimal]] = {}
    if normalized_sampling_method == "latin_hypercube":
        sampling_fractions = {
            str(variable["name"]): _latin_hypercube_fractions(
                seed, str(variable["name"]), sample_count
            )
            for variable in normalized_variables
        }
    elif normalized_sampling_method == "halton":
        dimensions = {
            name: dimension
            for dimension, name in enumerate(
                sorted(str(variable["name"]) for variable in normalized_variables)
            )
        }
        sampling_fractions = {
            name: _halton_fractions(seed, name, sample_count, dimension)
            for name, dimension in dimensions.items()
        }
    with localcontext() as context:
        context.prec = 80
        for sample_index in range(sample_count):
            parameters: dict[str, str] = {}
            correlated_values: dict[str, Decimal] = {}
            for group in normalized_correlations:
                correlated_values.update(
                    _correlated_gaussians(
                        group,
                        correlation_factors[
                            ",".join(str(name) for name in group["variables"])
                        ],
                        variables_by_name,
                        seed,
                        sample_index,
                    )
                )
            for variable in normalized_variables:
                name = str(variable["name"])
                distribution = variable["distribution"]
                fraction = (
                    sampling_fractions[name][sample_index]
                    if sampling_fractions
                    else None
                )
                if name in correlated_names:
                    parameters[name] = _bounded_canonical_decimal(
                        correlated_values[name],
                        Decimal(str(variable["minimum"])),
                        Decimal(str(variable["maximum"])),
                    )
                elif distribution == "uniform":
                    minimum = Decimal(str(variable["minimum"]))
                    maximum = Decimal(str(variable["maximum"]))
                    value = minimum + (maximum - minimum) * (
                        _uniform_fraction(seed, sample_index, name)
                        if fraction is None
                        else fraction
                    )
                    parameters[name] = _bounded_canonical_decimal(
                        value, minimum, maximum
                    )
                elif distribution == "gaussian":
                    minimum = Decimal(str(variable["minimum"]))
                    maximum = Decimal(str(variable["maximum"]))
                    parameters[name] = _bounded_canonical_decimal(
                        _bounded_gaussian(variable, seed, sample_index),
                        minimum,
                        maximum,
                    )
                elif distribution == "discrete":
                    parameters[name] = _weighted_discrete(
                        variable, seed, sample_index, fraction
                    )
                else:
                    parameters[name] = _empirical_value(
                        variable, seed, sample_index, fraction
                    )
            sample_points.append({"index": sample_index, "parameters": parameters})
    points: list[StatisticalPlanPoint]
    if normalized_corner_axes:
        corner_values = [axis["values"] for axis in normalized_corner_axes]
        assert all(isinstance(values, list) for values in corner_values)
        points = []
        for sample_point in sample_points:
            for combination in itertools.product(*corner_values):
                parameters = dict(sample_point["parameters"])
                corners: dict[str, str] = {}
                for axis, entry in zip(normalized_corner_axes, combination):
                    assert isinstance(entry, dict)
                    parameters[str(axis["parameter"])] = str(entry["value"])
                    corners[str(axis["name"])] = str(entry["name"])
                points.append(
                    {
                        "index": len(points),
                        "sample_index": sample_point["index"],
                        "corners": corners,
                        "parameters": parameters,
                    }
                )
    else:
        points = sample_points
    definition_hash = hashlib.sha256(
        _canonical_json(definition).encode("utf-8")
    ).hexdigest()
    plan: StatisticalPlan = {
        "schema_version": STATISTICAL_PLAN_SCHEMA_VERSION,
        "generator_version": generator_version,
        "definition_hash": definition_hash,
        "definition": definition,
        "parameter_order": parameter_order,
        "parameter_units": parameter_units,
        "sample_count": sample_count,
        "points": points,
    }
    if normalized_corner_axes:
        plan["point_count"] = len(points)
    return plan


def _artifact_bytes(plan: StatisticalPlan) -> bytes:
    return (_canonical_json(plan, pretty=True) + "\n").encode("utf-8")


def _empirical_sources(plan: StatisticalPlan) -> list[dict[str, object]]:
    variables = plan["definition"]["variables"]
    assert isinstance(variables, list)
    return [
        {
            "name": str(variable["name"]),
            "unit": str(variable["unit"]),
            **variable["source"],
        }
        for variable in variables
        if variable["distribution"] == "empirical"
    ]


def _plans_root(runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    resolved_runs = runs_dir.resolve()
    candidate = runs_dir / "statistical-plans"
    if candidate.is_symlink():
        raise ValueError("statistical plans directory must not be a symlink")
    candidate.mkdir(exist_ok=True)
    resolved = candidate.resolve()
    if resolved.parent != resolved_runs:
        raise ValueError("statistical plans directory must be inside runs")
    return resolved


def save_statistical_plan(runs_dir: Path, plan: StatisticalPlan) -> StatisticalPlanResult:
    """Publish a plan in a content-addressed, immutable directory."""
    artifact = _artifact_bytes(plan)
    digest = hashlib.sha256(artifact).hexdigest()
    plan_id = f"statistical-plan-{digest[:16]}"
    root = _plans_root(runs_dir)
    plan_dir = root / plan_id
    try:
        plan_dir.mkdir()
    except FileExistsError:
        if plan_dir.is_symlink() or not plan_dir.is_dir():
            raise ValueError("statistical plan output is not a real directory")
    if plan_dir.resolve().parent != root or plan_dir.resolve().name != plan_id:
        raise ValueError("statistical plan output must remain inside runs")
    plan_file = plan_dir / "statistical_plan.json"
    if plan_file.exists() or plan_file.is_symlink():
        if plan_file.is_symlink() or not plan_file.is_file():
            raise ValueError("statistical plan artifact is not a regular file")
        if plan_file.read_bytes() != artifact:
            raise ValueError("statistical plan artifact does not match its content address")
    else:
        temporary = plan_dir / f".statistical_plan.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(artifact)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, plan_file)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": digest,
        "generator_version": plan["generator_version"],
        "definition_hash": plan["definition_hash"],
        "correlations": plan["definition"].get("correlations", []),
        "empirical_sources": _empirical_sources(plan),
        "corner_axes": plan["definition"].get("corner_axes", []),
        "corner_aggregate": bool(plan["definition"].get("corner_aggregate", False)),
        "sampling_method": str(
            plan["definition"].get("sampling_method", "independent")
        ),
        "sample_count": plan["sample_count"],
        "point_count": len(plan["points"]),
        "parameter_order": plan["parameter_order"],
        "parameter_units": plan["parameter_units"],
        "points": plan["points"],
    }


def generate_statistical_plan(
    runs_dir: Path,
    variables: list[StatisticalVariable],
    sample_count: int,
    seed: int,
    correlations: list[StatisticalCorrelation] | None = None,
    corner_axes: list[StatisticalCornerAxis] | None = None,
    corner_aggregate: bool = False,
    source_root: Path | None = None,
    sampling_method: str = "independent",
) -> StatisticalPlanResult:
    return save_statistical_plan(
        runs_dir,
        build_statistical_plan(
            variables,
            sample_count,
            seed,
            correlations,
            corner_axes,
            corner_aggregate,
            source_root=source_root,
            sampling_method=sampling_method,
        ),
    )


def inspect_statistical_plan(runs_dir: Path, plan_id: str) -> StatisticalPlanResult:
    plan = load_statistical_plan(runs_dir, plan_id)
    plan_file = _plans_root(runs_dir) / plan_id / "statistical_plan.json"
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "generator_version": plan["generator_version"],
        "definition_hash": plan["definition_hash"],
        "correlations": plan["definition"].get("correlations", []),
        "empirical_sources": _empirical_sources(plan),
        "corner_axes": plan["definition"].get("corner_axes", []),
        "corner_aggregate": bool(plan["definition"].get("corner_aggregate", False)),
        "sampling_method": str(
            plan["definition"].get("sampling_method", "independent")
        ),
        "sample_count": plan["sample_count"],
        "point_count": len(plan["points"]),
        "parameter_order": plan["parameter_order"],
        "parameter_units": plan["parameter_units"],
        "points": plan["points"],
    }


def load_statistical_plan(runs_dir: Path, plan_id: str) -> StatisticalPlan:
    """Load and validate a content-addressed plan by its plain identifier."""
    if (
        not isinstance(plan_id, str)
        or re.fullmatch(r"statistical-plan-[0-9a-f]{16}", plan_id) is None
    ):
        raise ValueError("invalid statistical plan_id")
    root = _plans_root(runs_dir)
    plan_dir = root / plan_id
    if plan_dir.is_symlink() or not plan_dir.is_dir():
        raise FileNotFoundError(f"statistical plan not found: {plan_id}")
    resolved_dir = plan_dir.resolve()
    if resolved_dir.parent != root or resolved_dir.name != plan_id:
        raise ValueError("statistical plan must remain inside runs")
    plan_file = plan_dir / "statistical_plan.json"
    if plan_file.is_symlink() or not plan_file.is_file():
        raise ValueError("statistical plan artifact is not a regular file")
    artifact = plan_file.read_bytes()
    digest = hashlib.sha256(artifact).hexdigest()
    if plan_id != f"statistical-plan-{digest[:16]}":
        raise ValueError("statistical plan content address does not match")
    try:
        value = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid statistical plan artifact") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid statistical plan artifact")
    if value.get("schema_version") != STATISTICAL_PLAN_SCHEMA_VERSION:
        raise ValueError("unsupported statistical plan schema_version")
    if value.get("generator_version") not in SUPPORTED_GENERATOR_VERSIONS:
        raise ValueError("unsupported statistical generator_version")
    definition = value.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("statistical plan definition is missing")
    variables = definition.get("variables")
    sample_count = definition.get("sample_count")
    seed = definition.get("seed")
    correlations = definition.get("correlations")
    corner_axes = definition.get("corner_axes")
    corner_aggregate = definition.get("corner_aggregate", False)
    sampling_method = definition.get("sampling_method", "independent")
    if not isinstance(variables, list):
        raise ValueError("statistical plan variables are invalid")
    rebuilt = build_statistical_plan(  # type: ignore[arg-type]
        variables,
        sample_count,
        seed,
        correlations,
        corner_axes,
        corner_aggregate,
        _allow_empirical_provenance=True,
        sampling_method=sampling_method,
    )
    if value != rebuilt:
        raise ValueError("statistical plan contents do not match its definition")
    return rebuilt
