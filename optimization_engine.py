"""Deterministic coarse optimization plans and Pareto evidence."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import itertools
import json
import math
import re
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import NotRequired, TypedDict

import artifacts
import experiment_index

OPTIMIZATION_PLAN_SCHEMA_VERSION = 1
OPTIMIZATION_RESULT_SCHEMA_VERSION = 1
OPTIMIZATION_GENERATOR_VERSION = "deterministic-cartesian-v1"
OPTIMIZATION_REFINEMENT_GENERATOR_VERSION = "deterministic-pareto-refinement-v1"
OPTIMIZATION_RESULT_GENERATOR_VERSION = "pareto-evidence-v3"
SELECTION_POLICY = "equal-weight-normalized-regret-v1"
TOLERANCE_SELECTION_POLICY = "tolerance-aware-normalized-regret-v2"
MAX_OPTIMIZATION_PARAMETERS = 16
MAX_DOMAIN_VALUES = 64
MAX_OPTIMIZATION_CANDIDATES = 512
MAX_OPTIMIZATION_POINTS = 1_000
NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
OPERATORS = {"<", "<=", ">", ">="}
E_SERIES_VALUES = {
    "E6": ("1", "1.5", "2.2", "3.3", "4.7", "6.8"),
    "E12": (
        "1", "1.2", "1.5", "1.8", "2.2", "2.7",
        "3.3", "3.9", "4.7", "5.6", "6.8", "8.2",
    ),
    "E24": (
        "1", "1.1", "1.2", "1.3", "1.5", "1.6", "1.8", "2",
        "2.2", "2.4", "2.7", "3", "3.3", "3.6", "3.9", "4.3",
        "4.7", "5.1", "5.6", "6.2", "6.8", "7.5", "8.2", "9.1",
    ),
}


class OptimizationParameter(TypedDict):
    name: str
    kind: str
    unit: NotRequired[str]
    minimum: NotRequired[float]
    maximum: NotRequired[float]
    count: NotRequired[int]
    step: NotRequired[int]
    values: NotRequired[list[float | str]]
    series: NotRequired[str]


class OptimizationObjective(TypedDict):
    name: str
    experiment: str
    analysis: str
    metric: str
    goal: str
    weight: NotRequired[float]
    absolute_tolerance: NotRequired[float]
    relative_tolerance: NotRequired[float]
    metric_parameters: NotRequired[dict[str, float | str]]


class OptimizationConstraint(TypedDict):
    name: str
    experiment: str
    analysis: str
    metric: str
    operator: str
    target: float
    metric_parameters: NotRequired[dict[str, float | str]]


class OptimizationCornerValue(TypedDict):
    name: str
    value: float


class OptimizationCornerAxis(TypedDict):
    name: str
    parameter: str
    unit: NotRequired[str]
    values: list[OptimizationCornerValue]


class OptimizationPlanPoint(TypedDict):
    index: int
    candidate_index: int
    parameters: dict[str, str]
    corners: NotRequired[dict[str, str]]


class OptimizationExplicitCandidate(TypedDict):
    parameters: dict[str, str]
    parent_candidate_indices: list[int]


class OptimizationPlan(TypedDict):
    schema_version: int
    generator_version: str
    definition_hash: str
    definition: dict[str, object]
    parameter_order: list[str]
    parameter_units: dict[str, str]
    candidate_count: int
    point_count: int
    points: list[OptimizationPlanPoint]


class OptimizationPlanResult(TypedDict):
    plan_id: str
    plan_file: str
    plan_sha256: str
    generator_version: str
    definition_hash: str
    selection_policy: str
    candidate_count: int
    point_count: int
    parameter_order: list[str]
    parameter_units: dict[str, str]
    points: list[OptimizationPlanPoint]


class OptimizationRefinementPlanResult(OptimizationPlanResult):
    parent_plan_id: str
    parent_study_id: str
    parent_candidate_indices: list[int]
    refinement_policy: str
    max_candidates: int
    max_points: int


class OptimizationStudyResult(TypedDict):
    study_id: str
    plan_id: str
    study_dir: str
    results_json: str
    results_csv: str
    report_html: str
    candidate_count: int
    feasible_candidates: int
    constraint_failed_candidates: int
    invalid_candidates: int
    pareto_candidates: int
    selected_candidate_index: int | None
    selection_explanation: str


_canonical_json = artifacts.canonical_json


def _humanize(value: object) -> str:
    return str(value).replace("_", " ").strip().title()


def _engineering(value: float, unit: str) -> str:
    scales = {
        "F": [(1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")],
        "ohm": [(1e6, "MΩ"), (1e3, "kΩ"), (1, "Ω")],
        "s": [(1, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns")],
        "Hz": [(1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1, "Hz")],
    }
    magnitude = abs(value)
    for scale, label in scales.get(unit, []):
        if magnitude >= scale:
            return f"{value / scale:.4g} {label}"
    return f"{value:.4g}{f' {unit}' if unit else ''}"


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{field} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return parsed


def _number(value: object, field: str) -> float:
    parsed = float(_decimal(value, field))
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be a finite number")
    return parsed


def _encoded(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 17
        rounded = +value
    if rounded.is_zero():
        return "0"
    return format(rounded.normalize(), "g").lower()


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a parameter-style name")
    return value


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _metric_parameters(value: object, field: str) -> dict[str, float | str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError(f"{field} must be an object")
    normalized: dict[str, float | str] = {}
    for key in sorted(value):
        name = _name(key, f"{field} name")
        item = value[key]
        if isinstance(item, str):
            normalized[name] = _label(item, f"{field}.{name}")
        else:
            normalized[name] = _number(item, f"{field}.{name}")
    return normalized


def _generated_e_series(
    series: str, minimum: Decimal, maximum: Decimal, name: str
) -> list[str]:
    if series not in E_SERIES_VALUES:
        raise ValueError(f"parameter {name} series must be E6, E12, or E24")
    if minimum <= 0 or minimum >= maximum:
        raise ValueError(
            f"parameter {name} minimum must be positive and below maximum"
        )
    generated = {
        _encoded(Decimal(base) * (Decimal(10) ** exponent))
        for exponent in range(minimum.adjusted() - 1, maximum.adjusted() + 2)
        for base in E_SERIES_VALUES[series]
        if minimum <= Decimal(base) * (Decimal(10) ** exponent) <= maximum
    }
    values = sorted(generated, key=Decimal)
    if len(values) < 2:
        raise ValueError(
            f"parameter {name} generated range must contain at least 2 values"
        )
    if len(values) > MAX_DOMAIN_VALUES:
        raise ValueError(
            f"parameter {name} generated range exceeds {MAX_DOMAIN_VALUES} values"
        )
    return values


def _normalized_parameters(
    parameters: list[OptimizationParameter],
) -> tuple[list[dict[str, object]], dict[str, list[str]], dict[str, str]]:
    if (
        not isinstance(parameters, list)
        or not parameters
        or len(parameters) > MAX_OPTIMIZATION_PARAMETERS
    ):
        raise ValueError(
            f"parameters must contain 1 to {MAX_OPTIMIZATION_PARAMETERS} entries"
        )
    normalized: list[dict[str, object]] = []
    values_by_name: dict[str, list[str]] = {}
    units: dict[str, str] = {}
    seen: set[str] = set()
    for raw in parameters:
        if not isinstance(raw, dict):
            raise ValueError("each optimization parameter must be an object")
        name = _name(raw.get("name"), "parameter name")
        if name in seen:
            raise ValueError(f"duplicate optimization parameter: {name}")
        seen.add(name)
        kind = raw.get("kind")
        unit = raw.get("unit", "")
        if not isinstance(unit, str) or len(unit) > 32:
            raise ValueError(f"parameter {name} unit must be a string")
        if kind == "continuous":
            minimum = _decimal(raw.get("minimum"), f"parameter {name} minimum")
            maximum = _decimal(raw.get("maximum"), f"parameter {name} maximum")
            count = raw.get("count")
            if minimum >= maximum:
                raise ValueError(f"parameter {name} minimum must be below maximum")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 2
                or count > MAX_DOMAIN_VALUES
            ):
                raise ValueError(
                    f"parameter {name} count must be between 2 and {MAX_DOMAIN_VALUES}"
                )
            with localcontext() as context:
                context.prec = 80
                step = (maximum - minimum) / Decimal(count - 1)
                values = [_encoded(minimum + step * index) for index in range(count)]
            normalized.append(
                {
                    "name": name,
                    "kind": kind,
                    "unit": unit,
                    "minimum": _encoded(minimum),
                    "maximum": _encoded(maximum),
                    "count": count,
                }
            )
        elif kind == "integer":
            minimum = raw.get("minimum")
            maximum = raw.get("maximum")
            step = raw.get("step", 1)
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in (minimum, maximum, step)
            ):
                raise ValueError(
                    f"parameter {name} integer bounds and step must be integers"
                )
            assert isinstance(minimum, int)
            assert isinstance(maximum, int)
            assert isinstance(step, int)
            if minimum >= maximum:
                raise ValueError(f"parameter {name} minimum must be below maximum")
            if step <= 0:
                raise ValueError(f"parameter {name} step must be positive")
            if (maximum - minimum) % step:
                raise ValueError(
                    f"parameter {name} step must land exactly on maximum"
                )
            integer_values = list(range(minimum, maximum + 1, step))
            if len(integer_values) > MAX_DOMAIN_VALUES:
                raise ValueError(
                    f"parameter {name} integer range exceeds {MAX_DOMAIN_VALUES} values"
                )
            values = [str(value) for value in integer_values]
            normalized.append(
                {
                    "name": name,
                    "kind": kind,
                    "unit": unit,
                    "minimum": minimum,
                    "maximum": maximum,
                    "step": step,
                }
            )
        elif kind == "categorical":
            raw_values = raw.get("values")
            if (
                not isinstance(raw_values, list)
                or len(raw_values) < 2
                or len(raw_values) > MAX_DOMAIN_VALUES
                or any(not isinstance(value, str) for value in raw_values)
            ):
                raise ValueError(
                    f"parameter {name} categorical values must contain 2 to "
                    f"{MAX_DOMAIN_VALUES} strings"
                )
            values = sorted(
                {_label(value, f"parameter {name} value") for value in raw_values}
            )
            if len(values) != len(raw_values):
                raise ValueError(f"parameter {name} values must be unique")
            normalized.append(
                {
                    "name": name,
                    "kind": kind,
                    "unit": unit,
                    "values": values,
                }
            )
        elif kind == "preferred_values":
            raw_values = raw.get("values")
            series = _label(raw.get("series"), f"parameter {name} series")
            if (
                not isinstance(raw_values, list)
                or len(raw_values) < 2
                or len(raw_values) > MAX_DOMAIN_VALUES
            ):
                raise ValueError(
                    f"parameter {name} values must contain 2 to {MAX_DOMAIN_VALUES} entries"
                )
            values = sorted(
                {
                    _encoded(_decimal(value, f"parameter {name} value"))
                    for value in raw_values
                },
                key=Decimal,
            )
            if len(values) != len(raw_values):
                raise ValueError(f"parameter {name} values must be unique")
            normalized.append(
                {
                    "name": name,
                    "kind": kind,
                    "unit": unit,
                    "series": series,
                    "values": values,
                }
            )
        elif kind == "preferred_series":
            series = _label(raw.get("series"), f"parameter {name} series").upper()
            minimum = _decimal(raw.get("minimum"), f"parameter {name} minimum")
            maximum = _decimal(raw.get("maximum"), f"parameter {name} maximum")
            values = _generated_e_series(series, minimum, maximum, name)
            normalized.append(
                {
                    "name": name,
                    "kind": kind,
                    "unit": unit,
                    "series": series,
                    "minimum": _encoded(minimum),
                    "maximum": _encoded(maximum),
                }
            )
        else:
            raise ValueError(
                f"parameter {name} kind must be continuous, integer, categorical, "
                "preferred_values, or preferred_series"
            )
        values_by_name[name] = values
        units[name] = unit
    normalized.sort(key=lambda item: str(item["name"]))
    return normalized, values_by_name, units


def _normalized_fixed(
    fixed_parameters: dict[str, float | str] | None,
    used_names: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    if fixed_parameters is None:
        return {}, {}
    if not isinstance(fixed_parameters, dict) or len(fixed_parameters) > 64:
        raise ValueError("fixed_parameters must be a bounded object")
    values: dict[str, str] = {}
    units: dict[str, str] = {}
    for raw_name in sorted(fixed_parameters):
        name = _name(raw_name, "fixed parameter name")
        if name in used_names:
            raise ValueError(f"duplicate parameter name: {name}")
        used_names.add(name)
        value = fixed_parameters[raw_name]
        values[name] = (
            _label(value, f"fixed parameter {name}")
            if isinstance(value, str)
            else _encoded(_decimal(value, f"fixed parameter {name}"))
        )
        units[name] = ""
    return values, units


def _normalized_corners(
    corner_axes: list[OptimizationCornerAxis] | None,
    used_names: set[str],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    if corner_axes is None:
        return [], {}
    if not isinstance(corner_axes, list) or len(corner_axes) > 8:
        raise ValueError("corner_axes must contain at most 8 entries")
    normalized: list[dict[str, object]] = []
    units: dict[str, str] = {}
    axis_names: set[str] = set()
    for raw in corner_axes:
        if not isinstance(raw, dict):
            raise ValueError("each corner axis must be an object")
        axis_name = _name(raw.get("name"), "corner axis name")
        parameter = _name(raw.get("parameter"), f"corner {axis_name} parameter")
        if axis_name in axis_names:
            raise ValueError(f"duplicate corner axis: {axis_name}")
        if parameter in used_names:
            raise ValueError(f"duplicate parameter name: {parameter}")
        axis_names.add(axis_name)
        used_names.add(parameter)
        unit = raw.get("unit", "")
        if not isinstance(unit, str) or len(unit) > 32:
            raise ValueError(f"corner {axis_name} unit must be a string")
        entries = raw.get("values")
        if not isinstance(entries, list) or not entries or len(entries) > 16:
            raise ValueError(f"corner {axis_name} values must contain 1 to 16 entries")
        corner_names: set[str] = set()
        values: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"corner {axis_name} values must be objects")
            entry_name = _name(entry.get("name"), f"corner {axis_name} value name")
            if entry_name in corner_names:
                raise ValueError(f"corner {axis_name} value names must be unique")
            corner_names.add(entry_name)
            values.append(
                {
                    "name": entry_name,
                    "value": _encoded(
                        _decimal(entry.get("value"), f"corner {axis_name} value")
                    ),
                }
            )
        values.sort(key=lambda item: item["name"])
        normalized.append(
            {
                "name": axis_name,
                "parameter": parameter,
                "unit": unit,
                "values": values,
            }
        )
        units[parameter] = unit
    normalized.sort(key=lambda item: str(item["name"]))
    return normalized, units


def _normalized_objectives(
    objectives: list[OptimizationObjective],
) -> list[dict[str, object]]:
    if not isinstance(objectives, list) or not objectives or len(objectives) > 8:
        raise ValueError("objectives must contain 1 to 8 entries")
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for raw in objectives:
        if not isinstance(raw, dict):
            raise ValueError("each objective must be an object")
        name = _name(raw.get("name"), "objective name")
        if name in names:
            raise ValueError(f"duplicate objective: {name}")
        names.add(name)
        goal = raw.get("goal")
        if goal not in {"minimize", "maximize"}:
            raise ValueError(f"objective {name} goal must be minimize or maximize")
        weight = _number(raw.get("weight", 1.0), f"objective {name} weight")
        if weight <= 0:
            raise ValueError(f"objective {name} weight must be positive")
        record: dict[str, object] = {
            "name": name,
            "experiment": _name(raw.get("experiment"), f"objective {name} experiment"),
            "analysis": _label(raw.get("analysis"), f"objective {name} analysis"),
            "metric": _label(raw.get("metric"), f"objective {name} metric"),
            "goal": goal,
            "weight": weight,
            "metric_parameters": _metric_parameters(
                raw.get("metric_parameters"),
                f"objective {name} metric_parameters",
            ),
        }
        if "absolute_tolerance" in raw or "relative_tolerance" in raw:
            absolute = _number(
                raw.get("absolute_tolerance", 0.0),
                f"objective {name} absolute_tolerance",
            )
            relative = _number(
                raw.get("relative_tolerance", 0.0),
                f"objective {name} relative_tolerance",
            )
            if absolute < 0 or relative < 0:
                raise ValueError(f"objective {name} tolerances must be nonnegative")
            if absolute == 0 and relative == 0:
                raise ValueError(f"objective {name} tolerances must not both be zero")
            record["absolute_tolerance"] = absolute
            record["relative_tolerance"] = relative
        normalized.append(record)
    normalized.sort(key=lambda item: str(item["name"]))
    return normalized


def _normalized_constraints(
    constraints: list[OptimizationConstraint],
) -> list[dict[str, object]]:
    if not isinstance(constraints, list) or not constraints or len(constraints) > 32:
        raise ValueError("constraints must contain 1 to 32 entries")
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for raw in constraints:
        if not isinstance(raw, dict):
            raise ValueError("each constraint must be an object")
        name = _name(raw.get("name"), "constraint name")
        if name in names:
            raise ValueError(f"duplicate constraint: {name}")
        names.add(name)
        operator = raw.get("operator")
        if operator not in OPERATORS:
            raise ValueError(f"constraint {name} operator is invalid")
        normalized.append(
            {
                "name": name,
                "experiment": _name(raw.get("experiment"), f"constraint {name} experiment"),
                "analysis": _label(raw.get("analysis"), f"constraint {name} analysis"),
                "metric": _label(raw.get("metric"), f"constraint {name} metric"),
                "operator": operator,
                "target": _number(raw.get("target"), f"constraint {name} target"),
                "metric_parameters": _metric_parameters(
                    raw.get("metric_parameters"),
                    f"constraint {name} metric_parameters",
                ),
            }
        )
    normalized.sort(key=lambda item: str(item["name"]))
    return normalized


def _normalized_refinement_source(
    source: object,
) -> dict[str, object]:
    if not isinstance(source, dict):
        raise ValueError("refinement source must be an object")
    expected = {
        "kind",
        "policy",
        "parent_plan_id",
        "parent_plan_sha256",
        "parent_study_id",
        "parent_results_sha256",
        "parent_candidate_indices",
        "max_candidates",
        "max_points",
    }
    if set(source) != expected:
        raise ValueError("refinement source fields are invalid")
    if source.get("kind") != "pareto_neighborhood_refinement":
        raise ValueError("refinement source kind is invalid")
    if source.get("policy") != "adjacent-domain-midpoint-v1":
        raise ValueError("refinement policy is invalid")
    parent_plan_id = source.get("parent_plan_id")
    parent_study_id = source.get("parent_study_id")
    if (
        not isinstance(parent_plan_id, str)
        or re.fullmatch(r"optimization-plan-[0-9a-f]{16}", parent_plan_id) is None
    ):
        raise ValueError("refinement parent plan identity is invalid")
    if (
        not isinstance(parent_study_id, str)
        or re.fullmatch(r"optimization-study-[0-9a-f]{16}", parent_study_id) is None
    ):
        raise ValueError("refinement parent study identity is invalid")
    for name in ("parent_plan_sha256", "parent_results_sha256"):
        value = source.get(name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"refinement {name} is invalid")
    parent_indices = source.get("parent_candidate_indices")
    if (
        not isinstance(parent_indices, list)
        or not parent_indices
        or any(
            not isinstance(index, int) or isinstance(index, bool) or index < 0
            for index in parent_indices
        )
        or len(set(parent_indices)) != len(parent_indices)
    ):
        raise ValueError("refinement parent candidate indices are invalid")
    max_candidates = source.get("max_candidates")
    max_points = source.get("max_points")
    if (
        not isinstance(max_candidates, int)
        or isinstance(max_candidates, bool)
        or not 1 <= max_candidates <= MAX_OPTIMIZATION_CANDIDATES
    ):
        raise ValueError("refinement max_candidates is invalid")
    if (
        not isinstance(max_points, int)
        or isinstance(max_points, bool)
        or not 1 <= max_points <= MAX_OPTIMIZATION_POINTS
    ):
        raise ValueError("refinement max_points is invalid")
    return {
        "kind": "pareto_neighborhood_refinement",
        "policy": "adjacent-domain-midpoint-v1",
        "parent_plan_id": parent_plan_id,
        "parent_plan_sha256": source["parent_plan_sha256"],
        "parent_study_id": parent_study_id,
        "parent_results_sha256": source["parent_results_sha256"],
        "parent_candidate_indices": sorted(parent_indices),
        "max_candidates": max_candidates,
        "max_points": max_points,
    }


def _normalized_explicit_candidates(
    candidates: object,
    parameters: list[dict[str, object]],
    domain_values: dict[str, list[str]],
) -> list[OptimizationExplicitCandidate]:
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("explicit candidates must be a non-empty list")
    if len(candidates) > MAX_OPTIMIZATION_CANDIDATES:
        raise ValueError(
            f"explicit candidates exceed {MAX_OPTIMIZATION_CANDIDATES} candidates"
        )
    design_names = [str(parameter["name"]) for parameter in parameters]
    parameter_by_name = {
        str(parameter["name"]): parameter for parameter in parameters
    }
    normalized: list[OptimizationExplicitCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for raw in candidates:
        if not isinstance(raw, dict) or set(raw) != {
            "parameters",
            "parent_candidate_indices",
        }:
            raise ValueError("explicit candidate fields are invalid")
        raw_parameters = raw.get("parameters")
        if not isinstance(raw_parameters, dict) or set(raw_parameters) != set(
            design_names
        ):
            raise ValueError("explicit candidate parameters do not match the design")
        values: dict[str, str] = {}
        for name in design_names:
            parameter = parameter_by_name[name]
            kind = parameter["kind"]
            raw_value = raw_parameters[name]
            if kind == "categorical":
                value = _label(raw_value, f"explicit candidate {name}")
            else:
                value = _encoded(
                    _decimal(raw_value, f"explicit candidate {name}")
                )
            if kind == "continuous":
                numeric = Decimal(value)
                if not (
                    Decimal(str(parameter["minimum"]))
                    <= numeric
                    <= Decimal(str(parameter["maximum"]))
                ):
                    raise ValueError(f"explicit candidate {name} is out of domain")
            elif value not in domain_values[name]:
                raise ValueError(f"explicit candidate {name} is out of domain")
            values[name] = value
        key = tuple(values[name] for name in design_names)
        if key in seen:
            raise ValueError("explicit candidates must be unique")
        seen.add(key)
        parent_indices = raw.get("parent_candidate_indices")
        if (
            not isinstance(parent_indices, list)
            or not parent_indices
            or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in parent_indices
            )
            or len(set(parent_indices)) != len(parent_indices)
        ):
            raise ValueError("explicit candidate parent indices are invalid")
        normalized.append(
            {
                "parameters": {name: values[name] for name in design_names},
                "parent_candidate_indices": sorted(parent_indices),
            }
        )
    normalized.sort(
        key=lambda candidate: tuple(
            Decimal(candidate["parameters"][name])
            if parameter_by_name[name]["kind"] == "continuous"
            else domain_values[name].index(candidate["parameters"][name])
            for name in design_names
        )
    )
    return normalized


def build_optimization_plan(
    parameters: list[OptimizationParameter],
    objectives: list[OptimizationObjective],
    constraints: list[OptimizationConstraint],
    fixed_parameters: dict[str, float | str] | None = None,
    corner_axes: list[OptimizationCornerAxis] | None = None,
    explicit_candidates: list[OptimizationExplicitCandidate] | None = None,
    refinement_source: dict[str, object] | None = None,
) -> OptimizationPlan:
    """Build a bounded deterministic candidate plan without running LTspice."""
    normalized_parameters, domain_values, units = _normalized_parameters(parameters)
    design_names = [str(item["name"]) for item in normalized_parameters]
    used_names = set(design_names)
    fixed, fixed_units = _normalized_fixed(fixed_parameters, used_names)
    normalized_corners, corner_units = _normalized_corners(corner_axes, used_names)
    normalized_objectives = _normalized_objectives(objectives)
    normalized_constraints = _normalized_constraints(constraints)
    experiment_names = {
        str(item["experiment"])
        for item in [*normalized_objectives, *normalized_constraints]
    }
    if (explicit_candidates is None) != (refinement_source is None):
        raise ValueError(
            "explicit_candidates and refinement_source must be provided together"
        )
    normalized_candidates = (
        None
        if explicit_candidates is None
        else _normalized_explicit_candidates(
            explicit_candidates, normalized_parameters, domain_values
        )
    )
    normalized_source = (
        None
        if refinement_source is None
        else _normalized_refinement_source(refinement_source)
    )
    candidate_count = (
        math.prod(len(domain_values[name]) for name in design_names)
        if normalized_candidates is None
        else len(normalized_candidates)
    )
    corner_count = math.prod(
        len(axis["values"]) for axis in normalized_corners
    ) if normalized_corners else 1
    point_count = candidate_count * corner_count
    if candidate_count > MAX_OPTIMIZATION_CANDIDATES:
        raise ValueError(
            f"candidate grid exceeds {MAX_OPTIMIZATION_CANDIDATES} candidates"
        )
    if point_count > MAX_OPTIMIZATION_POINTS:
        raise ValueError(f"expanded plan exceeds {MAX_OPTIMIZATION_POINTS} points")
    tolerance_aware = any(
        "absolute_tolerance" in objective for objective in normalized_objectives
    )
    definition: dict[str, object] = {
        "parameters": normalized_parameters,
        "fixed_parameters": fixed,
        "corner_axes": normalized_corners,
        "objectives": normalized_objectives,
        "constraints": normalized_constraints,
        "experiments": sorted(experiment_names),
        "selection_policy": (
            TOLERANCE_SELECTION_POLICY if tolerance_aware else SELECTION_POLICY
        ),
    }
    if normalized_candidates is not None and normalized_source is not None:
        if candidate_count > int(normalized_source["max_candidates"]):
            raise ValueError("refinement candidate budget exceeded")
        if point_count > int(normalized_source["max_points"]):
            raise ValueError("refinement point budget exceeded")
        parent_indices = sorted(
            {
                index
                for candidate in normalized_candidates
                for index in candidate["parent_candidate_indices"]
            }
        )
        if parent_indices != normalized_source["parent_candidate_indices"]:
            raise ValueError("refinement parent provenance does not match candidates")
        definition["candidate_mode"] = "explicit_refinement"
        definition["candidates"] = normalized_candidates
        definition["refinement_source"] = normalized_source
    points: list[OptimizationPlanPoint] = []
    domain_product = (
        itertools.product(*(domain_values[name] for name in design_names))
        if normalized_candidates is None
        else (
            tuple(candidate["parameters"][name] for name in design_names)
            for candidate in normalized_candidates
        )
    )
    corner_products = list(
        itertools.product(*(axis["values"] for axis in normalized_corners))
    ) if normalized_corners else [()]
    for candidate_index, design_values in enumerate(domain_product):
        design = dict(zip(design_names, design_values))
        for corner_values in corner_products:
            point_parameters = {**design, **fixed}
            corners: dict[str, str] = {}
            for axis, entry in zip(normalized_corners, corner_values):
                assert isinstance(entry, dict)
                point_parameters[str(axis["parameter"])] = str(entry["value"])
                corners[str(axis["name"])] = str(entry["name"])
            point: OptimizationPlanPoint = {
                "index": len(points),
                "candidate_index": candidate_index,
                "parameters": {
                    name: point_parameters[name] for name in sorted(point_parameters)
                },
            }
            if corners:
                point["corners"] = corners
            points.append(point)
    parameter_units = {**units, **fixed_units, **corner_units}
    parameter_order = sorted(parameter_units)
    definition_hash = artifacts.definition_hash(definition)
    return {
        "schema_version": OPTIMIZATION_PLAN_SCHEMA_VERSION,
        "generator_version": (
            OPTIMIZATION_GENERATOR_VERSION
            if normalized_candidates is None
            else OPTIMIZATION_REFINEMENT_GENERATOR_VERSION
        ),
        "definition_hash": definition_hash,
        "definition": definition,
        "parameter_order": parameter_order,
        "parameter_units": {
            name: parameter_units[name] for name in parameter_order
        },
        "candidate_count": candidate_count,
        "point_count": point_count,
        "points": points,
    }


def _plan_bytes(plan: OptimizationPlan) -> bytes:
    return (_canonical_json(plan, pretty=True) + "\n").encode("utf-8")


def _optimization_plan_identity(artifact: bytes) -> tuple[str, str]:
    return artifacts.content_address("optimization-plan", artifact)


def optimization_plan_identity(plan: OptimizationPlan) -> tuple[str, str]:
    """Return the content address a plan would receive without writing it."""
    return _optimization_plan_identity(_plan_bytes(plan))


def _confined_root(runs_dir: Path, name: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    resolved_runs = runs_dir.resolve()
    root = runs_dir / name
    if root.is_symlink():
        raise ValueError(f"{name} must not be a symlink")
    root.mkdir(exist_ok=True)
    resolved = root.resolve()
    if resolved.parent != resolved_runs:
        raise ValueError(f"{name} must remain inside runs")
    return resolved


_write_once = artifacts.write_once


def _plan_result(
    plan_id: str, plan_file: Path, plan: OptimizationPlan
) -> OptimizationPlanResult:
    return {
        "plan_id": plan_id,
        "plan_file": str(plan_file),
        "plan_sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(),
        "generator_version": plan["generator_version"],
        "definition_hash": plan["definition_hash"],
        "selection_policy": str(plan["definition"]["selection_policy"]),
        "candidate_count": plan["candidate_count"],
        "point_count": plan["point_count"],
        "parameter_order": plan["parameter_order"],
        "parameter_units": plan["parameter_units"],
        "points": plan["points"],
    }


def save_optimization_plan(
    runs_dir: Path, plan: OptimizationPlan
) -> OptimizationPlanResult:
    artifact = _plan_bytes(plan)
    plan_id, digest = _optimization_plan_identity(artifact)
    root = _confined_root(runs_dir, "optimization-plans")
    plan_dir = root / plan_id
    try:
        plan_dir.mkdir()
    except FileExistsError:
        if plan_dir.is_symlink() or not plan_dir.is_dir():
            raise ValueError("optimization plan output is not a real directory")
    if plan_dir.resolve().parent != root:
        raise ValueError("optimization plan output must remain inside runs")
    plan_file = plan_dir / "optimization_plan.json"
    _write_once(plan_file, artifact)
    return _plan_result(plan_id, plan_file, plan)


def generate_optimization_plan(
    runs_dir: Path,
    parameters: list[OptimizationParameter],
    objectives: list[OptimizationObjective],
    constraints: list[OptimizationConstraint],
    fixed_parameters: dict[str, float | str] | None = None,
    corner_axes: list[OptimizationCornerAxis] | None = None,
) -> OptimizationPlanResult:
    return save_optimization_plan(
        runs_dir,
        build_optimization_plan(
            parameters,
            objectives,
            constraints,
            fixed_parameters,
            corner_axes,
        ),
    )


def load_optimization_plan(runs_dir: Path, plan_id: str) -> OptimizationPlan:
    if (
        not isinstance(plan_id, str)
        or re.fullmatch(r"optimization-plan-[0-9a-f]{16}", plan_id) is None
    ):
        raise ValueError("invalid optimization plan_id")
    root = _confined_root(runs_dir, "optimization-plans")
    plan_dir = root / plan_id
    if plan_dir.is_symlink() or not plan_dir.is_dir():
        raise FileNotFoundError(f"optimization plan not found: {plan_id}")
    if plan_dir.resolve().parent != root:
        raise ValueError("optimization plan must remain inside runs")
    plan_file = plan_dir / "optimization_plan.json"
    if plan_file.is_symlink() or not plan_file.is_file():
        raise ValueError("optimization plan artifact is not a regular file")
    artifact = plan_file.read_bytes()
    if plan_id != f"optimization-plan-{hashlib.sha256(artifact).hexdigest()[:16]}":
        raise ValueError("optimization plan content address does not match")
    try:
        value = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("optimization plan is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("optimization plan must be an object")
    if (
        value.get("schema_version") != OPTIMIZATION_PLAN_SCHEMA_VERSION
        or value.get("generator_version")
        not in {
            OPTIMIZATION_GENERATOR_VERSION,
            OPTIMIZATION_REFINEMENT_GENERATOR_VERSION,
        }
    ):
        raise ValueError("unsupported optimization plan schema or generator")
    definition = value.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("optimization plan definition is invalid")
    if definition.get("selection_policy") not in {
        SELECTION_POLICY,
        TOLERANCE_SELECTION_POLICY,
    }:
        raise ValueError("unsupported optimization selection policy")
    expected_hash = artifacts.definition_hash(definition)
    if value.get("definition_hash") != expected_hash:
        raise ValueError("optimization definition hash does not match")
    rebuilt = build_optimization_plan(
        definition.get("parameters"),  # type: ignore[arg-type]
        definition.get("objectives"),  # type: ignore[arg-type]
        definition.get("constraints"),  # type: ignore[arg-type]
        definition.get("fixed_parameters"),  # type: ignore[arg-type]
        definition.get("corner_axes"),  # type: ignore[arg-type]
        definition.get("candidates"),  # type: ignore[arg-type]
        definition.get("refinement_source"),  # type: ignore[arg-type]
    )
    if value != rebuilt:
        raise ValueError("optimization plan does not match its definition")
    return value  # type: ignore[return-value]


def inspect_optimization_plan(
    runs_dir: Path, plan_id: str
) -> OptimizationPlanResult:
    plan = load_optimization_plan(runs_dir, plan_id)
    plan_file = (
        _confined_root(runs_dir, "optimization-plans")
        / plan_id
        / "optimization_plan.json"
    )
    return _plan_result(plan_id, plan_file, plan)


def _same_metric_parameter(actual: object, expected: object) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=0.0)
        except (TypeError, ValueError):
            return False
    return actual == expected


def _metric(point: dict[str, object], selector: dict[str, object]) -> tuple[float, str]:
    analysis_name = selector["analysis"]
    matches = [
        analysis
        for analysis in point.get("analyses", [])
        if isinstance(analysis, dict) and analysis.get("name") == analysis_name
    ]
    if len(matches) != 1:
        raise ValueError(f"analysis {analysis_name} is missing or ambiguous")
    analysis = matches[0]
    if analysis.get("status") != "completed" or not isinstance(analysis.get("analysis"), dict):
        raise ValueError(f"analysis {analysis_name} did not complete")
    requested_parameters = selector.get("metric_parameters", {})
    assert isinstance(requested_parameters, dict)
    results = analysis["analysis"].get("results", [])  # type: ignore[union-attr]
    candidates = []
    for result in results:
        if not isinstance(result, dict) or result.get("metric") != selector["metric"]:
            continue
        actual_parameters = result.get("parameters", {})
        if not isinstance(actual_parameters, dict):
            continue
        if all(
            key in actual_parameters
            and _same_metric_parameter(actual_parameters[key], expected)
            for key, expected in requested_parameters.items()
        ):
            candidates.append(result)
    if not candidates:
        raise ValueError(
            f"metric {analysis_name}.{selector['metric']} was not found"
        )
    values = {
        (_number(result.get("value"), "metric value"), str(result.get("unit", "")))
        for result in candidates
    }
    if len(values) != 1:
        raise ValueError(
            f"metric {analysis_name}.{selector['metric']} is ambiguous"
        )
    return next(iter(values))


def _constraint_margin(value: float, operator: str, target: float) -> tuple[bool, float]:
    if operator == "<=":
        return value <= target, target - value
    if operator == "<":
        return value < target, target - value
    if operator == ">=":
        return value >= target, value - target
    return value > target, value - target


def _dominates(
    left: dict[str, object],
    right: dict[str, object],
    objectives: list[dict[str, object]],
) -> bool:
    no_worse = True
    strictly_better = False
    left_values = left["objectives"]
    right_values = right["objectives"]
    assert isinstance(left_values, dict) and isinstance(right_values, dict)
    for objective in objectives:
        name = str(objective["name"])
        left_value = float(left_values[name]["value"])  # type: ignore[index]
        right_value = float(right_values[name]["value"])  # type: ignore[index]
        tolerance = float(objective.get("absolute_tolerance", 0.0)) + float(
            objective.get("relative_tolerance", 0.0)
        ) * max(abs(left_value), abs(right_value))
        if objective["goal"] == "minimize":
            no_worse &= left_value <= right_value + tolerance
            strictly_better |= left_value < right_value - tolerance
        else:
            no_worse &= left_value >= right_value - tolerance
            strictly_better |= left_value > right_value + tolerance
    return no_worse and strictly_better


def _select_candidate(
    feasible: list[dict[str, object]],
    pareto: list[dict[str, object]],
    objectives: list[dict[str, object]],
) -> dict[str, object]:
    ranges: dict[str, tuple[float, float]] = {}
    for objective in objectives:
        name = str(objective["name"])
        values = [float(candidate["objectives"][name]["value"]) for candidate in feasible]  # type: ignore[index]
        ranges[name] = (min(values), max(values))
    total_weight = sum(float(objective["weight"]) for objective in objectives)
    score_tolerance = 0.0
    for objective in objectives:
        name = str(objective["name"])
        low, high = ranges[name]
        if high != low:
            numeric_tolerance = float(
                objective.get("absolute_tolerance", 0.0)
            ) + float(objective.get("relative_tolerance", 0.0)) * max(
                abs(low), abs(high)
            )
            score_tolerance += (
                numeric_tolerance
                / (high - low)
                * float(objective["weight"])
                / total_weight
            )
    for candidate in pareto:
        regrets: dict[str, float] = {}
        score = 0.0
        for objective in objectives:
            name = str(objective["name"])
            low, high = ranges[name]
            value = float(candidate["objectives"][name]["value"])  # type: ignore[index]
            regret = 0.0 if high == low else (
                (value - low) / (high - low)
                if objective["goal"] == "minimize"
                else (high - value) / (high - low)
            )
            regrets[name] = regret
            score += regret * float(objective["weight"]) / total_weight
        candidate["normalized_regret"] = regrets
        candidate["selection_score"] = score
    best_score = min(float(candidate["selection_score"]) for candidate in pareto)
    equivalent = [
        candidate
        for candidate in pareto
        if float(candidate["selection_score"]) <= best_score + score_tolerance
    ]
    return min(equivalent, key=lambda candidate: int(candidate["candidate_index"]))


def _study_csv(result: dict[str, object], objectives: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    names = [str(objective["name"]) for objective in objectives]
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "candidate_index",
            "status",
            "pareto",
            "selected",
            "selection_score",
            *names,
            "parameters_json",
            "errors_json",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for candidate in result["candidates"]:  # type: ignore[union-attr]
        writer.writerow(
            {
                "candidate_index": candidate["candidate_index"],
                "status": candidate["status"],
                "pareto": candidate["pareto"],
                "selected": candidate["selected"],
                "selection_score": candidate.get("selection_score", ""),
                **{
                    name: candidate.get("objectives", {}).get(name, {}).get("value", "")
                    for name in names
                },
                "parameters_json": _canonical_json(candidate["parameters"]),
                "errors_json": _canonical_json(candidate["errors"]),
            }
        )
    return output.getvalue().encode("utf-8")


def _pareto_svg(
    candidates: list[dict[str, object]], objectives: list[dict[str, object]]
) -> str:
    if len(objectives) != 2:
        return "<p class=\"muted\">The Pareto plot is available for two-objective studies.</p>"
    available = [candidate for candidate in candidates if candidate["status"] == "feasible"]
    if not available:
        return "<p class=\"muted\">No feasible candidates are available to plot.</p>"
    x_name, y_name = (str(objective["name"]) for objective in objectives)
    x_values = [float(candidate["objectives"][x_name]["value"]) for candidate in available]  # type: ignore[index]
    y_values = [float(candidate["objectives"][y_name]["value"]) for candidate in available]  # type: ignore[index]

    def extent(values: list[float]) -> tuple[float, float]:
        low, high = min(values), max(values)
        if low == high:
            pad = max(abs(low) * 0.05, 1.0)
            return low - pad, high + pad
        pad = (high - low) * 0.08
        return low - pad, high + pad

    x_low, x_high = extent(x_values)
    y_low, y_high = extent(y_values)
    left, top, width, height = 88.0, 28.0, 750.0, 330.0
    first_objectives = available[0]["objectives"]
    assert isinstance(first_objectives, dict)
    x_unit = str(first_objectives[x_name]["unit"])  # type: ignore[index]
    y_unit = str(first_objectives[y_name]["unit"])  # type: ignore[index]
    grid = []
    for index in range(6):
        fraction = index / 5
        x = left + fraction * width
        y = top + fraction * height
        x_value = x_low + fraction * (x_high - x_low)
        y_value = y_high - fraction * (y_high - y_low)
        grid.extend(
            [
                f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + height}"/>',
                f'<text class="tick" x="{x:.2f}" y="{top + height + 22}" text-anchor="middle">{html.escape(_engineering(x_value, x_unit))}</text>',
                f'<line class="grid" x1="{left}" y1="{y:.2f}" x2="{left + width}" y2="{y:.2f}"/>',
                f'<text class="tick" x="{left - 10}" y="{y + 4:.2f}" text-anchor="end">{html.escape(_engineering(y_value, y_unit))}</text>',
            ]
        )
    circles = []
    for candidate in available:
        x_value = float(candidate["objectives"][x_name]["value"])  # type: ignore[index]
        y_value = float(candidate["objectives"][y_name]["value"])  # type: ignore[index]
        x = left + (x_value - x_low) * width / (x_high - x_low)
        y = top + (y_high - y_value) * height / (y_high - y_low)
        css = "selected" if candidate["selected"] else "pareto" if candidate["pareto"] else "feasible"
        title = html.escape(
            f"Candidate {candidate['candidate_index']}: "
            f"{_humanize(x_name)}={_engineering(x_value, x_unit)}, "
            f"{_humanize(y_name)}={_engineering(y_value, y_unit)}"
        )
        circles.append(
            f'<circle class="{css}" cx="{x:.2f}" cy="{y:.2f}" r="{7 if candidate["selected"] else 5}"><title>{title}</title></circle>'
        )
    return f"""
<svg class="pareto-plot" viewBox="0 0 870 410" role="img" aria-label="Pareto objective plot">
  {''.join(grid)}
  <line class="axis" x1="{left}" y1="{top + height}" x2="{left + width}" y2="{top + height}"/>
  <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + height}"/>
  {''.join(circles)}
  <text class="label" x="{left + width / 2}" y="402" text-anchor="middle">{html.escape(_humanize(x_name))} · {html.escape(str(objectives[0]['goal']))}</text>
  <text class="label" transform="translate(18 {top + height / 2}) rotate(-90)" text-anchor="middle">{html.escape(_humanize(y_name))} · {html.escape(str(objectives[1]['goal']))}</text>
</svg>"""


def _study_html(result: dict[str, object], plan: OptimizationPlan) -> bytes:
    objectives = plan["definition"]["objectives"]
    assert isinstance(objectives, list)
    parameter_units = plan["parameter_units"]
    parameter_definitions = plan["definition"]["parameters"]
    assert isinstance(parameter_definitions, list)
    parameter_kinds = {
        str(parameter["name"]): str(parameter["kind"])
        for parameter in parameter_definitions
    }
    refinement_source = plan["definition"].get("refinement_source")
    is_refinement = isinstance(refinement_source, dict)
    study_title = (
        "Local multi-objective refinement"
        if is_refinement
        else "Coarse multi-objective search"
    )
    proof_note = (
        "Nominal local refinement is not a tolerance-yield proof."
        if is_refinement
        else "Nominal coarse optimization is not a tolerance-yield proof."
    )

    def parameter_text(candidate: dict[str, object]) -> str:
        parameters = candidate["parameters"]
        assert isinstance(parameters, dict)
        return ", ".join(
            f"{name}="
            + (
                str(value)
                if parameter_kinds[name] == "categorical"
                else _engineering(float(value), parameter_units.get(name, ""))
            )
            for name, value in parameters.items()
        )

    rows = []
    for candidate in result["candidates"]:  # type: ignore[union-attr]
        candidate_objectives = candidate["objectives"]
        assert isinstance(candidate_objectives, dict)
        objective_text = "<br>".join(
            f"{html.escape(_humanize(objective['name']))}: "
            f"{html.escape(_engineering(float(candidate_objectives[str(objective['name'])]['value']), str(candidate_objectives[str(objective['name'])]['unit'])))}"  # type: ignore[index]
            for objective in objectives
            if str(objective["name"]) in candidate_objectives
        ) or "—"
        constraint_records = candidate["constraints"]
        assert isinstance(constraint_records, dict)
        failed_constraints = [
            _humanize(name)
            for name, record in constraint_records.items()
            if not bool(record["passed"])  # type: ignore[index]
        ]
        constraint_text = ", ".join(failed_constraints) if failed_constraints else (
            "All pass" if constraint_records else "Unavailable"
        )
        rows.append(
            "<tr>"
            f"<td>{candidate['candidate_index']}</td>"
            f"<td><span class=\"badge {candidate['status']}\">{html.escape(str(candidate['status']))}</span></td>"
            f"<td>{'★' if candidate['selected'] else 'Yes' if candidate['pareto'] else '—'}</td>"
            f"<td>{html.escape(parameter_text(candidate))}</td>"
            f"<td>{objective_text}</td>"
            f"<td>{html.escape(constraint_text)}</td>"
            "</tr>"
        )
    selected = next(
        (candidate for candidate in result["candidates"] if candidate["selected"]),  # type: ignore[union-attr]
        None,
    )
    selected_panel = ""
    if selected is not None:
        selected_objectives = selected["objectives"]
        assert isinstance(selected_objectives, dict)
        objective_summary = " · ".join(
            f"{_humanize(name)}: {_engineering(float(record['value']), str(record['unit']))}"  # type: ignore[index]
            for name, record in selected_objectives.items()
        )
        selected_panel = (
            '<section class="panel selected-design"><h2>Selected '
            f'{"refined" if is_refinement else "coarse"} design</h2>'
            f'<p class="selected-parameters">{html.escape(parameter_text(selected))}</p>'
            f'<p>{html.escape(objective_summary)}</p>'
            '<p class="muted">All hard constraints pass at every planned operating point. '
            'Final verification must still apply manufacturing distributions and yield requirements.</p></section>'
        )
    experiment_links = " · ".join(
        f'<a href="../../{html.escape(str(item["experiment_id"]))}/results.json">{html.escape(str(name))} experiment results</a>'
        for name, item in result["experiments"].items()  # type: ignore[union-attr]
    )
    parent_link = ""
    refinement_panel = ""
    if is_refinement:
        assert isinstance(refinement_source, dict)
        parent_id = html.escape(str(refinement_source["parent_study_id"]))
        parent_link = f' · <a href="../{parent_id}/report.html">parent study</a>'
        parent_indices = ", ".join(
            str(index) for index in refinement_source["parent_candidate_indices"]  # type: ignore[union-attr]
        )
        refinement_panel = (
            '\n<section class="panel"><h2>Refinement provenance</h2>'
            f'<p>Generated around feasible Pareto candidate(s) {parent_indices} '
            f'from <a href="../{parent_id}/report.html">{parent_id}</a> using '
            f'{html.escape(str(refinement_source["policy"]))}.</p>'
            '<p class="muted">This child plan contains only new candidates. Its '
            'selected marker ranks the child candidates against each other; the '
            'parent remains part of the final engineering comparison.</p></section>'
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Optimization {html.escape(str(result['study_id']))}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--orange:#f0883e;--red:#f85149}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}}main{{max-width:1280px;margin:auto;padding:34px 24px}}h1{{font-size:34px;margin:.2em 0}}h2{{margin-top:0}}a{{color:var(--blue)}}.eyebrow{{color:var(--blue);font-weight:750;letter-spacing:.08em;text-transform:uppercase}}.muted{{color:var(--muted)}}.panel{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:22px;margin:20px 0}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.card{{background:var(--panel);border:1px solid var(--border);border-radius:9px;padding:16px}}.card strong{{display:block;font-size:28px}}.selected-design{{border-color:#9e6a03}}.selected-parameters{{font:600 17px ui-monospace,monospace}}table{{border-collapse:collapse;width:100%;min-width:980px}}th,td{{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}}th{{color:var(--muted)}}.table-wrap{{overflow:auto}}.badge{{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700}}.badge.feasible{{background:#1b4721}}.badge.constraint_failed,.badge.invalid{{background:#5a1e1e}}.pareto-plot{{display:block;width:100%;background:#0b0f14;border:1px solid var(--border);border-radius:8px}}.grid{{stroke:#242b35;stroke-width:1}}.axis{{stroke:#768390;stroke-width:1.5}}.tick{{fill:var(--muted);font-size:10px}}.label{{fill:var(--text);font-size:13px}}circle.feasible{{fill:var(--muted)}}circle.pareto{{fill:var(--blue)}}circle.selected{{fill:var(--orange);stroke:#fff;stroke-width:2}}@media(max-width:760px){{main{{padding:20px 12px}}.cards{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<div class="eyebrow">LTspice constrained optimization</div><h1>{study_title}</h1>
<p>{html.escape(str(result['selection_explanation']))}</p>{refinement_panel}
<div class="cards"><div class="card"><span class="muted">Candidates</span><strong>{result['candidate_count']}</strong></div><div class="card"><span class="muted">Feasible</span><strong>{result['feasible_candidates']}</strong></div><div class="card"><span class="muted">Pareto</span><strong>{result['pareto_candidates']}</strong></div><div class="card"><span class="muted">Selected</span><strong>{result['selected_candidate_index'] if result['selected_candidate_index'] is not None else '—'}</strong></div></div>
{selected_panel}
<section class="panel"><h2>Objective tradeoff</h2><p class="muted">Axis labels state whether each objective is minimized or maximized. Gray candidates are feasible, blue candidates are Pareto-optimal, and the orange candidate is the deterministic equal-weight selection.</p>{_pareto_svg(result['candidates'], objectives)}</section>
<section class="panel"><h2>Candidate evidence</h2><div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Status</th><th>Pareto</th><th>Design parameters</th><th>Worst-corner objectives</th><th>Hard constraints</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section class="panel"><h2>Portable evidence</h2><p><a href="../../optimization-plans/{html.escape(str(result['plan_id']))}/optimization_plan.json">candidate plan</a>{parent_link} · <a href="optimization_results.json">results JSON</a> · <a href="optimization_results.csv">results CSV</a> · {experiment_links}</p><p class="muted">{proof_note} Finalists remain subject to Phase 3 corner and Monte Carlo verification.</p></section>
</main></body></html>"""
    return document.encode("utf-8")


def evaluate_optimization_study(
    runs_dir: Path,
    plan_id: str,
    experiments: dict[str, str],
) -> OptimizationStudyResult:
    """Evaluate completed experiment evidence and publish a Pareto study."""
    plan = load_optimization_plan(runs_dir, plan_id)
    definition = plan["definition"]
    required_experiments = definition["experiments"]
    if (
        not isinstance(experiments, dict)
        or set(experiments) != set(required_experiments)  # type: ignore[arg-type]
        or any(not isinstance(value, str) for value in experiments.values())
    ):
        raise ValueError("experiments must exactly match the plan experiment names")
    loaded: dict[str, dict[str, object]] = {}
    evidence: dict[str, dict[str, str]] = {}
    for name in sorted(experiments):
        experiment_id = experiments[name]
        experiment_dir, manifest, results, _ = experiment_index.load_terminal_experiment(
            runs_dir, experiment_id
        )
        if manifest.get("status") != "completed" or results.get("status") != "completed":
            raise ValueError(f"experiment {name} is not completed")
        result_points = results.get("points")
        if not isinstance(result_points, list) or len(result_points) != plan["point_count"]:
            raise ValueError(f"experiment {name} point count does not match the plan")
        for plan_point, result_point in zip(plan["points"], result_points):
            if (
                not isinstance(result_point, dict)
                or result_point.get("index") != plan_point["index"]
                or result_point.get("parameters") != plan_point["parameters"]
            ):
                raise ValueError(f"experiment {name} points do not match the plan")
        results_file = experiment_dir / "results.json"
        evidence[name] = {
            "experiment_id": experiment_id,
            "results_sha256": hashlib.sha256(results_file.read_bytes()).hexdigest(),
        }
        loaded[name] = results
    objectives = definition["objectives"]
    constraints = definition["constraints"]
    parameters = definition["parameters"]
    assert isinstance(objectives, list) and isinstance(constraints, list)
    assert isinstance(parameters, list)
    design_names = [str(parameter["name"]) for parameter in parameters]
    candidate_records: list[dict[str, object]] = []
    for candidate_index in range(plan["candidate_count"]):
        plan_points = [
            point for point in plan["points"] if point["candidate_index"] == candidate_index
        ]
        errors: list[str] = []
        objective_records: dict[str, dict[str, object]] = {}
        constraint_records: dict[str, dict[str, object]] = {}
        for objective in objectives:
            values: list[tuple[float, str, int]] = []
            for plan_point in plan_points:
                result_point = loaded[str(objective["experiment"])]["points"][plan_point["index"]]  # type: ignore[index]
                assert isinstance(result_point, dict)
                if result_point.get("simulation_status") != "completed" or result_point.get("error"):
                    errors.append(
                        f"{objective['experiment']} point {plan_point['index']} simulation failed"
                    )
                    continue
                try:
                    value, unit = _metric(result_point, objective)
                    values.append((value, unit, plan_point["index"]))
                except ValueError as exc:
                    errors.append(
                        f"{objective['experiment']} point {plan_point['index']}: {exc}"
                    )
            if len(values) == len(plan_points):
                worst = (
                    max(values, key=lambda item: (item[0], item[2]))
                    if objective["goal"] == "minimize"
                    else min(values, key=lambda item: (item[0], item[2]))
                )
                objective_records[str(objective["name"])] = {
                    "value": worst[0],
                    "unit": worst[1],
                    "worst_point_index": worst[2],
                }
        for constraint in constraints:
            values: list[tuple[float, str, int, bool, float]] = []
            for plan_point in plan_points:
                result_point = loaded[str(constraint["experiment"])]["points"][plan_point["index"]]  # type: ignore[index]
                assert isinstance(result_point, dict)
                if result_point.get("simulation_status") != "completed" or result_point.get("error"):
                    errors.append(
                        f"{constraint['experiment']} point {plan_point['index']} simulation failed"
                    )
                    continue
                try:
                    value, unit = _metric(result_point, constraint)
                    passed, margin = _constraint_margin(
                        value, str(constraint["operator"]), float(constraint["target"])
                    )
                    values.append((value, unit, plan_point["index"], passed, margin))
                except ValueError as exc:
                    errors.append(
                        f"{constraint['experiment']} point {plan_point['index']}: {exc}"
                    )
            if len(values) == len(plan_points):
                worst = min(values, key=lambda item: (item[4], item[2]))
                constraint_records[str(constraint["name"])] = {
                    "passed": all(item[3] for item in values),
                    "worst_value": worst[0],
                    "unit": worst[1],
                    "worst_point_index": worst[2],
                    "margin": worst[4],
                    "operator": constraint["operator"],
                    "target": constraint["target"],
                }
        unique_errors = list(dict.fromkeys(errors))
        constraints_complete = len(constraint_records) == len(constraints)
        objectives_complete = len(objective_records) == len(objectives)
        feasible = (
            not unique_errors
            and constraints_complete
            and objectives_complete
            and all(bool(record["passed"]) for record in constraint_records.values())
        )
        status = (
            "invalid"
            if unique_errors or not constraints_complete or not objectives_complete
            else "feasible"
            if feasible
            else "constraint_failed"
        )
        first_parameters = plan_points[0]["parameters"]
        candidate_records.append(
            {
                "candidate_index": candidate_index,
                "parameters": {name: first_parameters[name] for name in design_names},
                "status": status,
                "objectives": objective_records,
                "constraints": constraint_records,
                "errors": unique_errors,
                "pareto": False,
                "selected": False,
            }
        )
    feasible = [candidate for candidate in candidate_records if candidate["status"] == "feasible"]
    pareto = [
        candidate
        for candidate in feasible
        if not any(
            other is not candidate and _dominates(other, candidate, objectives)
            for other in feasible
        )
    ]
    for candidate in pareto:
        candidate["pareto"] = True
    selected: dict[str, object] | None = None
    if pareto:
        selected = _select_candidate(feasible, pareto, objectives)
        selected["selected"] = True
    selected_index = None if selected is None else int(selected["candidate_index"])
    selection_policy = str(definition["selection_policy"])
    is_refinement = isinstance(definition.get("refinement_source"), dict)
    explanation = (
        "No feasible candidate satisfied every hard constraint; no design was selected."
        if selected is None
        else (
            f"Candidate {selected_index} was selected from {len(pareto)} Pareto-optimal "
            f"candidate{'s' if len(pareto) != 1 else ''} by {selection_policy}. "
            "Each objective uses its worst named-corner value. This "
            f"{'local refined' if is_refinement else 'coarse'} nominal selection "
            "still requires Phase 3 tolerance and yield verification."
        )
    )
    plan_result = inspect_optimization_plan(runs_dir, plan_id)
    identity = {
        "schema_version": OPTIMIZATION_RESULT_SCHEMA_VERSION,
        "generator_version": OPTIMIZATION_RESULT_GENERATOR_VERSION,
        "plan_id": plan_id,
        "plan_sha256": plan_result["plan_sha256"],
        "experiments": evidence,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    study_id = f"optimization-study-{digest[:16]}"
    result: dict[str, object] = {
        **identity,
        "study_id": study_id,
        "selection_policy": selection_policy,
        "candidate_count": plan["candidate_count"],
        "feasible_candidates": len(feasible),
        "constraint_failed_candidates": sum(
            candidate["status"] == "constraint_failed" for candidate in candidate_records
        ),
        "invalid_candidates": sum(
            candidate["status"] == "invalid" for candidate in candidate_records
        ),
        "pareto_candidates": len(pareto),
        "selected_candidate_index": selected_index,
        "selection_explanation": explanation,
        "candidates": candidate_records,
    }
    root = _confined_root(runs_dir, "optimization-studies")
    study_dir = root / study_id
    try:
        study_dir.mkdir()
    except FileExistsError:
        if study_dir.is_symlink() or not study_dir.is_dir():
            raise ValueError("optimization study output is not a real directory")
    if study_dir.resolve().parent != root:
        raise ValueError("optimization study output must remain inside runs")
    results_json = study_dir / "optimization_results.json"
    results_csv = study_dir / "optimization_results.csv"
    report_html = study_dir / "report.html"
    _write_once(results_json, (_canonical_json(result, pretty=True) + "\n").encode("utf-8"))
    _write_once(results_csv, _study_csv(result, objectives))
    _write_once(report_html, _study_html(result, plan))
    return {
        "study_id": study_id,
        "plan_id": plan_id,
        "study_dir": str(study_dir),
        "results_json": str(results_json),
        "results_csv": str(results_csv),
        "report_html": str(report_html),
        "candidate_count": plan["candidate_count"],
        "feasible_candidates": len(feasible),
        "constraint_failed_candidates": int(result["constraint_failed_candidates"]),
        "invalid_candidates": int(result["invalid_candidates"]),
        "pareto_candidates": len(pareto),
        "selected_candidate_index": selected_index,
        "selection_explanation": explanation,
    }


def _load_verified_optimization_study(
    runs_dir: Path, study_id: str
) -> tuple[dict[str, object], bytes]:
    if (
        not isinstance(study_id, str)
        or re.fullmatch(r"optimization-study-[0-9a-f]{16}", study_id) is None
    ):
        raise ValueError("invalid optimization study_id")
    root = _confined_root(runs_dir, "optimization-studies")
    study_dir = root / study_id
    if study_dir.is_symlink() or not study_dir.is_dir():
        raise FileNotFoundError(f"optimization study not found: {study_id}")
    if study_dir.resolve().parent != root:
        raise ValueError("optimization study must remain inside runs")
    results_file = study_dir / "optimization_results.json"
    if results_file.is_symlink() or not results_file.is_file():
        raise ValueError("optimization study result is not a regular file")
    artifact = results_file.read_bytes()
    try:
        result = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("optimization study result is not valid UTF-8 JSON") from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != OPTIMIZATION_RESULT_SCHEMA_VERSION
        or result.get("generator_version") != OPTIMIZATION_RESULT_GENERATOR_VERSION
        or result.get("study_id") != study_id
        or not isinstance(result.get("plan_id"), str)
        or not isinstance(result.get("experiments"), dict)
    ):
        raise ValueError("optimization study result identity is invalid")
    experiments: dict[str, str] = {}
    for name, evidence in result["experiments"].items():  # type: ignore[union-attr]
        if (
            not isinstance(name, str)
            or not isinstance(evidence, dict)
            or not isinstance(evidence.get("experiment_id"), str)
        ):
            raise ValueError("optimization study experiment evidence is invalid")
        experiments[name] = evidence["experiment_id"]
    verified = evaluate_optimization_study(
        runs_dir, str(result["plan_id"]), experiments
    )
    if verified["study_id"] != study_id:
        raise ValueError("optimization study identity does not reproduce")
    return result, artifact


def _candidate_parameters(
    plan: OptimizationPlan,
) -> dict[int, dict[str, str]]:
    parameters = plan["definition"]["parameters"]
    assert isinstance(parameters, list)
    design_names = [str(parameter["name"]) for parameter in parameters]
    candidates: dict[int, dict[str, str]] = {}
    for point in plan["points"]:
        index = point["candidate_index"]
        values = {name: point["parameters"][name] for name in design_names}
        if index in candidates and candidates[index] != values:
            raise ValueError("optimization plan candidate parameters are inconsistent")
        candidates[index] = values
    if set(candidates) != set(range(plan["candidate_count"])):
        raise ValueError("optimization plan candidate indices are incomplete")
    return candidates


def _ancestor_candidate_keys(
    runs_dir: Path,
    plan_id: str,
    plan: OptimizationPlan,
    design_names: list[str],
    seen: set[str] | None = None,
) -> set[tuple[str, ...]]:
    visited = set() if seen is None else seen
    if plan_id in visited or len(visited) >= 16:
        raise ValueError("optimization refinement provenance is cyclic or too deep")
    visited.add(plan_id)
    keys = {
        tuple(parameters[name] for name in design_names)
        for parameters in _candidate_parameters(plan).values()
    }
    source = plan["definition"].get("refinement_source")
    if isinstance(source, dict):
        parent_id = str(source["parent_plan_id"])
        parent = load_optimization_plan(runs_dir, parent_id)
        parent_result = inspect_optimization_plan(runs_dir, parent_id)
        if parent_result["plan_sha256"] != source["parent_plan_sha256"]:
            raise ValueError("optimization refinement parent plan hash does not match")
        keys.update(
            _ancestor_candidate_keys(
                runs_dir, parent_id, parent, design_names, visited
            )
        )
    return keys


def _refinement_values(
    parameter: dict[str, object],
    parent_value: str,
    domain_values: list[str],
    observed_values: list[str],
) -> list[str]:
    if parameter["kind"] != "continuous":
        index = domain_values.index(parent_value)
        return domain_values[max(0, index - 1) : index + 2]
    ordered = sorted(
        {
            Decimal(str(parameter["minimum"])),
            Decimal(str(parameter["maximum"])),
            *(Decimal(value) for value in observed_values),
        }
    )
    parent = Decimal(parent_value)
    index = ordered.index(parent)
    values = {parent}
    with localcontext() as context:
        context.prec = 80
        if index > 0:
            values.add((ordered[index - 1] + parent) / 2)
        if index + 1 < len(ordered):
            values.add((parent + ordered[index + 1]) / 2)
    return [_encoded(value) for value in sorted(values)]


def generate_optimization_refinement_plan(
    runs_dir: Path,
    parent_study_id: str,
    max_candidates: int = 64,
    max_points: int = 256,
) -> OptimizationRefinementPlanResult:
    """Freeze new candidates in feasible Pareto neighborhoods without simulation."""
    if (
        not isinstance(max_candidates, int)
        or isinstance(max_candidates, bool)
        or not 1 <= max_candidates <= MAX_OPTIMIZATION_CANDIDATES
    ):
        raise ValueError(
            f"max_candidates must be between 1 and {MAX_OPTIMIZATION_CANDIDATES}"
        )
    if (
        not isinstance(max_points, int)
        or isinstance(max_points, bool)
        or not 1 <= max_points <= MAX_OPTIMIZATION_POINTS
    ):
        raise ValueError(f"max_points must be between 1 and {MAX_OPTIMIZATION_POINTS}")
    result, results_artifact = _load_verified_optimization_study(
        runs_dir, parent_study_id
    )
    parent_plan_id = str(result["plan_id"])
    parent_plan = load_optimization_plan(runs_dir, parent_plan_id)
    parent_plan_result = inspect_optimization_plan(runs_dir, parent_plan_id)
    if result.get("plan_sha256") != parent_plan_result["plan_sha256"]:
        raise ValueError("optimization study plan hash does not match")
    parameters = parent_plan["definition"]["parameters"]
    assert isinstance(parameters, list)
    normalized_parameters, domain_values, _ = _normalized_parameters(parameters)  # type: ignore[arg-type]
    design_names = [str(parameter["name"]) for parameter in normalized_parameters]
    parent_parameters = _candidate_parameters(parent_plan)
    records = result.get("candidates")
    if not isinstance(records, list) or len(records) != parent_plan["candidate_count"]:
        raise ValueError("optimization study candidates do not match the plan")
    pareto_records: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("candidate_index"), int
        ):
            raise ValueError("optimization study candidate is invalid")
        index = int(record["candidate_index"])
        if index not in parent_parameters or record.get("parameters") != parent_parameters[index]:
            raise ValueError("optimization study candidate parameters do not match")
        if record.get("status") == "feasible" and record.get("pareto") is True:
            pareto_records.append(record)
    if not pareto_records:
        raise ValueError("optimization study has no feasible Pareto candidates")
    pareto_records.sort(key=lambda record: int(record["candidate_index"]))
    observed = {
        name: [candidate[name] for candidate in parent_parameters.values()]
        for name in design_names
    }
    existing = _ancestor_candidate_keys(
        runs_dir, parent_plan_id, parent_plan, design_names
    )
    generated: dict[tuple[str, ...], set[int]] = {}
    parameter_by_name = {
        str(parameter["name"]): parameter for parameter in normalized_parameters
    }
    for record in pareto_records:
        parent_index = int(record["candidate_index"])
        parent = parent_parameters[parent_index]
        neighborhood = [
            _refinement_values(
                parameter_by_name[name],
                parent[name],
                domain_values[name],
                observed[name],
            )
            for name in design_names
        ]
        for values in itertools.product(*neighborhood):
            key = tuple(values)
            if key not in existing:
                generated.setdefault(key, set()).add(parent_index)
    if not generated:
        raise ValueError("Pareto neighborhoods contain no new in-domain candidates")
    corner_axes = parent_plan["definition"]["corner_axes"]
    assert isinstance(corner_axes, list)
    corner_count = math.prod(len(axis["values"]) for axis in corner_axes) if corner_axes else 1  # type: ignore[index]
    if len(generated) > max_candidates:
        raise ValueError(
            f"refinement requires {len(generated)} candidates, exceeding budget {max_candidates}"
        )
    if len(generated) * corner_count > max_points:
        raise ValueError(
            f"refinement requires {len(generated) * corner_count} points, exceeding budget {max_points}"
        )
    explicit: list[OptimizationExplicitCandidate] = [
        {
            "parameters": dict(zip(design_names, key)),
            "parent_candidate_indices": sorted(parent_indices),
        }
        for key, parent_indices in generated.items()
    ]
    pareto_indices = [int(record["candidate_index"]) for record in pareto_records]
    source = {
        "kind": "pareto_neighborhood_refinement",
        "policy": "adjacent-domain-midpoint-v1",
        "parent_plan_id": parent_plan_id,
        "parent_plan_sha256": parent_plan_result["plan_sha256"],
        "parent_study_id": parent_study_id,
        "parent_results_sha256": hashlib.sha256(results_artifact).hexdigest(),
        "parent_candidate_indices": pareto_indices,
        "max_candidates": max_candidates,
        "max_points": max_points,
    }
    plan = build_optimization_plan(
        parameters,  # type: ignore[arg-type]
        parent_plan["definition"]["objectives"],  # type: ignore[arg-type]
        parent_plan["definition"]["constraints"],  # type: ignore[arg-type]
        parent_plan["definition"]["fixed_parameters"],  # type: ignore[arg-type]
        corner_axes,  # type: ignore[arg-type]
        explicit,
        source,
    )
    saved = save_optimization_plan(runs_dir, plan)
    return {
        **saved,
        "parent_plan_id": parent_plan_id,
        "parent_study_id": parent_study_id,
        "parent_candidate_indices": pareto_indices,
        "refinement_policy": "adjacent-domain-midpoint-v1",
        "max_candidates": max_candidates,
        "max_points": max_points,
    }
