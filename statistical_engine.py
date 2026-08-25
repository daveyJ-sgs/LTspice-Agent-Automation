"""Deterministic statistical point plans for LTspice experiments."""

from __future__ import annotations

import hashlib
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
STATISTICAL_GENERATOR_VERSION = UNIFORM_GENERATOR_VERSION
SUPPORTED_GENERATOR_VERSIONS = {
    UNIFORM_GENERATOR_VERSION,
    DISTRIBUTION_GENERATOR_VERSION,
}
MAX_STATISTICAL_SAMPLES = 1_000
MAX_STATISTICAL_VARIABLES = 32
MAX_STATISTICAL_CELLS = 10_000
MAX_DISCRETE_VALUES = 256
MAX_GAUSSIAN_ATTEMPTS = 4_096
MIN_GAUSSIAN_SPAN_SIGMA = Decimal("0.1")
MAX_SEED = (1 << 63) - 1


class StatisticalVariable(TypedDict):
    name: str
    distribution: str
    minimum: NotRequired[float]
    maximum: NotRequired[float]
    nominal: NotRequired[float | str]
    sigma: NotRequired[float]
    values: NotRequired[list[str]]
    weights: NotRequired[list[float]]
    unit: NotRequired[str]


class StatisticalPlanPoint(TypedDict):
    index: int
    parameters: dict[str, str]


class StatisticalPlan(TypedDict):
    schema_version: int
    generator_version: str
    definition_hash: str
    definition: dict[str, object]
    parameter_order: list[str]
    parameter_units: dict[str, str]
    sample_count: int
    points: list[StatisticalPlanPoint]


class StatisticalPlanResult(TypedDict):
    plan_id: str
    plan_file: str
    plan_sha256: str
    sample_count: int
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


def _normalized_definition(
    variables: list[StatisticalVariable], sample_count: int, seed: int
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
        else:
            raise ValueError(
                f"variable {name} distribution must be 'uniform', "
                "'gaussian', or 'discrete'"
            )
    return {
        "variables": normalized_variables,
        "sample_count": sample_count,
        "seed": seed,
    }


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
    variable: dict[str, object], seed: int, sample_index: int
) -> str:
    name = str(variable["name"])
    values = variable["values"]
    weights = variable["weights"]
    assert isinstance(values, list) and isinstance(weights, list)
    parsed_weights = [Decimal(str(weight)) for weight in weights]
    total = sum(parsed_weights, Decimal(0))
    threshold = _distribution_fraction(
        "discrete", seed, sample_index, name
    ) * total
    cumulative = Decimal(0)
    for value, weight in zip(values, parsed_weights):
        cumulative += weight
        if threshold < cumulative:
            return str(value)
    return str(values[-1])


def build_statistical_plan(
    variables: list[StatisticalVariable], sample_count: int, seed: int
) -> StatisticalPlan:
    """Build a portable, deterministic plan without running LTspice."""
    definition = _normalized_definition(variables, sample_count, seed)
    normalized_variables = definition["variables"]
    assert isinstance(normalized_variables, list)
    generator_version = (
        UNIFORM_GENERATOR_VERSION
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
    points: list[StatisticalPlanPoint] = []
    with localcontext() as context:
        context.prec = 80
        for sample_index in range(sample_count):
            parameters: dict[str, str] = {}
            for variable in normalized_variables:
                name = str(variable["name"])
                distribution = variable["distribution"]
                if distribution == "uniform":
                    minimum = Decimal(str(variable["minimum"]))
                    maximum = Decimal(str(variable["maximum"]))
                    value = minimum + (maximum - minimum) * _uniform_fraction(
                        seed, sample_index, name
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
                else:
                    parameters[name] = _weighted_discrete(
                        variable, seed, sample_index
                    )
            points.append({"index": sample_index, "parameters": parameters})
    definition_hash = hashlib.sha256(
        _canonical_json(definition).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": STATISTICAL_PLAN_SCHEMA_VERSION,
        "generator_version": generator_version,
        "definition_hash": definition_hash,
        "definition": definition,
        "parameter_order": parameter_order,
        "parameter_units": parameter_units,
        "sample_count": sample_count,
        "points": points,
    }


def _artifact_bytes(plan: StatisticalPlan) -> bytes:
    return (_canonical_json(plan, pretty=True) + "\n").encode("utf-8")


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
        "sample_count": plan["sample_count"],
        "parameter_order": plan["parameter_order"],
        "parameter_units": plan["parameter_units"],
        "points": plan["points"],
    }


def generate_statistical_plan(
    runs_dir: Path,
    variables: list[StatisticalVariable],
    sample_count: int,
    seed: int,
) -> StatisticalPlanResult:
    return save_statistical_plan(
        runs_dir, build_statistical_plan(variables, sample_count, seed)
    )


def inspect_statistical_plan(runs_dir: Path, plan_id: str) -> StatisticalPlanResult:
    plan = load_statistical_plan(runs_dir, plan_id)
    plan_file = _plans_root(runs_dir) / plan_id / "statistical_plan.json"
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "sample_count": plan["sample_count"],
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
    if not isinstance(variables, list):
        raise ValueError("statistical plan variables are invalid")
    rebuilt = build_statistical_plan(variables, sample_count, seed)  # type: ignore[arg-type]
    if value != rebuilt:
        raise ValueError("statistical plan contents do not match its definition")
    return rebuilt
