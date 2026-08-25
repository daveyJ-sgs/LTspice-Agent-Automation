"""Rebuildable SQLite index for structured LTspice experiment artifacts."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TypedDict

INDEX_SCHEMA_VERSION = 1
INDEX_BUILDER_VERSION = 1
INDEX_FILENAME = "experiments.sqlite3"
MAX_QUERY_LIMIT = 1000
MAX_EXPERIMENT_POINTS = 1000
SQLITE_MAX_INTEGER = 2**63 - 1
EXPERIMENT_STATUSES = {
    "defined",
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
}
EXECUTION_MODES = {"independent", "native"}
_EXPERIMENT_ID = re.compile(
    r"^mcp-experiment-(\d{8})-(\d{6})-(\d{6})(?:-[0-9a-fA-F]{8})?$"
)
_INDEX_LOCK = threading.RLock()


class ExperimentIndexIssue(TypedDict):
    artifact_path: str
    code: str
    message: str


class ExperimentIndexBuildResult(TypedDict):
    database_path: str
    scanned_experiments: int
    indexed_experiments: int
    result_experiments: int
    indexed_points: int
    issue_count: int
    issues: list[ExperimentIndexIssue]


class ExperimentIndexRecord(TypedDict):
    experiment_id: str
    status: str
    execution_mode: str
    index_state: str
    recorded_at: str
    point_count: int
    finished_points: int
    completed_points: int
    error_points: int
    passed_points: int
    failed_points: int
    all_passed: bool | None
    reuse_cache: bool
    manifest_path: str
    results_path: str | None
    parameters: list[dict[str, object]]
    measurement_names: list[str]
    requirement_metrics: list[str]


class ExperimentQueryResult(TypedDict):
    database_path: str
    total: int
    limit: int
    offset: int
    experiments: list[ExperimentIndexRecord]


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _load_json(path: Path) -> tuple[dict[str, object], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("artifact must contain a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _definition_hash(definition: dict[str, object]) -> str:
    encoded = json.dumps(
        definition,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_int(value: object, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > SQLITE_MAX_INTEGER
    ):
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _finite_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean or null")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("artifact must remain inside the runs directory") from exc


def waveform_artifact_path(
    reference: object, experiment_dir: Path, experiment_id: str
) -> tuple[Path, str]:
    """Resolve a portable waveform reference within its experiment directory."""
    if not isinstance(reference, str) or not reference:
        raise ValueError("waveform raw_file must be a non-empty string")
    parts = PurePosixPath(reference.replace("\\", "/")).parts
    if experiment_id in parts:
        parts = parts[parts.index(experiment_id) + 1 :]
    elif reference.startswith(("/", "\\")) or (parts and ":" in parts[0]):
        raise ValueError("waveform raw_file does not identify this experiment")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("waveform raw_file path is invalid")
    relative = Path(*parts)
    path = (experiment_dir / relative).resolve()
    try:
        path.relative_to(experiment_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            "waveform artifact must remain inside the experiment directory"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(f"Waveform artifact not found: {relative.as_posix()}")
    return path, relative.as_posix()


def _database_path(root: Path, database_path: Path | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / INDEX_FILENAME if database_path is None else database_path
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("experiment index must remain inside the runs directory") from exc
    return path


def _id_timestamp(experiment_id: str) -> str:
    match = _EXPERIMENT_ID.fullmatch(experiment_id)
    if match is None:
        raise ValueError("invalid experiment directory name")
    date, clock, microseconds = match.groups()
    return (
        f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
        f"{clock[0:2]}:{clock[2:4]}:{clock[4:6]}.{microseconds}+00:00"
    )


def _normalized_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _timestamp(manifest: dict[str, object], experiment_id: str) -> str:
    for name in ("created_at", "started_at", "finished_at", "updated_at"):
        value = manifest.get(name)
        if isinstance(value, str) and value:
            return _normalized_timestamp(value)
    return _id_timestamp(experiment_id)


def _definition_parameters(
    definition: dict[str, object], experiment_id: str
) -> tuple[list[dict[str, object]], dict[str, int]]:
    point_plan = definition.get("point_plan")
    if point_plan is not None:
        if not isinstance(point_plan, dict) or point_plan.get("schema_version") != 1:
            raise ValueError(f"{experiment_id} definition.point_plan is invalid")
        if definition.get("parameters", []) or definition.get("derived_parameters", []):
            raise ValueError(
                f"{experiment_id} explicit point plan cannot declare Cartesian parameters"
            )
        parameter_order = definition.get("parameter_order")
        points = point_plan.get("points")
        units = definition.get("parameter_units")
        if (
            not isinstance(parameter_order, list)
            or not parameter_order
            or any(
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                for name in parameter_order
            )
            or len(parameter_order) != len(set(parameter_order))
        ):
            raise ValueError(f"{experiment_id} definition.parameter_order is invalid")
        if not isinstance(points, list) or not points:
            raise ValueError(f"{experiment_id} definition.point_plan points are invalid")
        if (
            not isinstance(units, dict)
            or set(units) != set(parameter_order)
            or any(not isinstance(unit, str) for unit in units.values())
        ):
            raise ValueError(f"{experiment_id} definition.parameter_units is inconsistent")
        for point in points:
            if (
                not isinstance(point, dict)
                or set(point) != set(parameter_order)
                or any(not isinstance(value, str) or not value for value in point.values())
            ):
                raise ValueError(f"{experiment_id} definition.point_plan points are invalid")
        return (
            [
                {
                    "ordinal": ordinal,
                    "name": name,
                    "kind": "base",
                    "unit": units[name],
                    "declared_values_json": json.dumps(
                        [point[name] for point in points], separators=(",", ":")
                    ),
                    "template": None,
                }
                for ordinal, name in enumerate(parameter_order)
            ],
            {name: ordinal for ordinal, name in enumerate(parameter_order)},
        )
    by_kind: dict[str, dict[str, dict[str, object]]] = {"base": {}, "derived": {}}
    input_order: dict[str, list[str]] = {"base": [], "derived": []}
    seen: set[str] = set()
    for kind, field in (("base", "parameters"), ("derived", "derived_parameters")):
        values = definition.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"{experiment_id} definition.{field} must be a list")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f"{experiment_id} definition.{field} must contain objects")
            name = item.get("name")
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError(f"{experiment_id} has an invalid or duplicate parameter")
            seen.add(name)
            input_order[kind].append(name)
            unit = item.get("unit")
            if unit is not None and not isinstance(unit, str):
                raise ValueError(f"{experiment_id} parameter {name} unit must be a string")
            declared_values: list[str] | None = None
            template: str | None = None
            if kind == "base":
                raw_values = item.get("values")
                if (
                    not isinstance(raw_values, list)
                    or not raw_values
                    or any(not isinstance(value, str) for value in raw_values)
                    or len(raw_values) != len(set(raw_values))
                ):
                    raise ValueError(
                        f"{experiment_id} parameter {name} values must be strings"
                    )
                declared_values = raw_values
            else:
                template_value = item.get("template")
                if not isinstance(template_value, str):
                    raise ValueError(
                        f"{experiment_id} derived parameter {name} needs a template"
                    )
                template = template_value
            by_kind[kind][name] = {
                "name": name,
                "kind": kind,
                "unit": unit,
                "declared_values_json": None
                if declared_values is None
                else json.dumps(declared_values, separators=(",", ":")),
                "template": template,
            }
    if not input_order["base"]:
        raise ValueError(f"{experiment_id} definition.parameters must not be empty")
    orders: dict[str, list[str]] = {}
    for kind, field in (("base", "parameter_order"), ("derived", "derived_parameter_order")):
        declared_order = definition.get(field)
        if declared_order is None:
            orders[kind] = input_order[kind]
        elif (
            not isinstance(declared_order, list)
            or any(not isinstance(name, str) for name in declared_order)
            or len(declared_order) != len(set(declared_order))
            or set(declared_order) != set(input_order[kind])
        ):
            raise ValueError(f"{experiment_id} definition.{field} is inconsistent")
        else:
            orders[kind] = declared_order
    records: list[dict[str, object]] = []
    ordinals: dict[str, int] = {}
    parameter_units = definition.get("parameter_units")
    if parameter_units is not None and (
        not isinstance(parameter_units, dict)
        or set(parameter_units) != seen
        or any(not isinstance(unit, str) for unit in parameter_units.values())
    ):
        raise ValueError(f"{experiment_id} definition.parameter_units is inconsistent")
    for name in [*orders["base"], *orders["derived"]]:
        ordinal = len(records)
        ordinals[name] = ordinal
        record = by_kind["base"].get(name) or by_kind["derived"][name]
        if isinstance(parameter_units, dict):
            record = {**record, "unit": parameter_units[name]}
        records.append({"ordinal": ordinal, **record})
    return records, ordinals


def _manifest_record(
    manifest: dict[str, object],
    experiment_id: str,
    manifest_path: Path,
    root: Path,
    manifest_sha256: str,
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, int]]:
    if manifest.get("experiment_id") != experiment_id:
        raise ValueError("manifest experiment_id does not match its directory")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("unsupported experiment manifest schema_version")
    engine_version = manifest.get("engine_version")
    if engine_version is not None and (
        type(engine_version) is not int or engine_version != 1
    ):
        raise ValueError("unsupported experiment engine_version")
    status = manifest.get("status")
    if status not in EXPERIMENT_STATUSES:
        raise ValueError("unsupported experiment status")
    definition = manifest.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("experiment definition must contain an object")
    definition_hash = _optional_string(
        manifest.get("definition_hash"), "manifest definition_hash"
    )
    if schema_version == 2 and definition_hash != _definition_hash(definition):
        raise ValueError("manifest definition_hash does not match its definition")
    execution_mode = definition.get("execution_mode", "independent")
    if execution_mode not in EXECUTION_MODES:
        raise ValueError("unsupported experiment execution_mode")
    point_count = _plain_int(manifest.get("point_count"), "manifest point_count")
    parameters, ordinals = _definition_parameters(definition, experiment_id)
    point_plan = definition.get("point_plan")
    if isinstance(point_plan, dict):
        planned_points = point_plan.get("points")
        expected_point_count = len(planned_points) if isinstance(planned_points, list) else 0
    else:
        base_value_lists = [
            json.loads(str(item["declared_values_json"]))
            for item in parameters
            if item["kind"] == "base"
        ]
        expected_point_count = math.prod(len(values) for values in base_value_lists)
    if (
        point_count < 1
        or point_count > MAX_EXPERIMENT_POINTS
        or point_count != expected_point_count
    ):
        raise ValueError("manifest point_count does not match the Cartesian definition")
    reuse_cache = definition.get("reuse_cache", False)
    if not isinstance(reuse_cache, bool):
        raise ValueError("definition reuse_cache must be a boolean")
    finished_default = point_count if schema_version == 1 and status == "completed" else 0
    counts = {
        "finished_points": _plain_int(
            manifest.get("finished_points", finished_default),
            "manifest finished_points",
        ),
        **{
            name: _plain_int(manifest.get(name, 0), f"manifest {name}")
            for name in (
                "completed_points",
                "error_points",
                "passed_points",
                "failed_points",
            )
        },
    }
    if (
        counts["finished_points"] > point_count
        or counts["completed_points"] > counts["finished_points"]
        or counts["passed_points"] > counts["finished_points"]
        or counts["failed_points"] != counts["finished_points"] - counts["passed_points"]
    ):
        raise ValueError("manifest point counts are inconsistent")
    record = {
        "experiment_id": experiment_id,
        "manifest_schema_version": schema_version,
        "engine_version": engine_version,
        "status": status,
        "execution_mode": execution_mode,
        "definition_hash": definition_hash,
        "recorded_at": _timestamp(manifest, experiment_id),
        "created_at": _optional_string(manifest.get("created_at"), "manifest created_at"),
        "updated_at": _optional_string(manifest.get("updated_at"), "manifest updated_at"),
        "finished_at": _optional_string(
            manifest.get("finished_at"), "manifest finished_at"
        ),
        "point_count": point_count,
        **counts,
        "all_passed": _optional_bool(manifest.get("all_passed"), "manifest all_passed"),
        "error": manifest.get("error") if isinstance(manifest.get("error"), str) else None,
        "reuse_cache": reuse_cache,
        "index_state": "manifest_only",
        "manifest_path": _relative_path(manifest_path, root),
        "results_path": None,
        "manifest_sha256": manifest_sha256,
        "results_sha256": None,
    }
    return record, parameters, ordinals


def _requirement_row(
    experiment_id: str,
    point_index: int,
    analysis_name: str,
    requirement_index: int,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("requirement result must contain an object")
    metric = value.get("metric")
    unit = value.get("unit")
    passed = value.get("passed")
    threshold = value.get("threshold")
    parameters = value.get("parameters", {})
    if not isinstance(metric, str) or not metric:
        raise ValueError("requirement metric must be a non-empty string")
    if not isinstance(unit, str) or not isinstance(passed, bool):
        raise ValueError("requirement unit/passed fields are invalid")
    if not isinstance(threshold, dict) or not isinstance(parameters, dict):
        raise ValueError("requirement threshold/parameters fields are invalid")
    operator = threshold.get("operator")
    threshold_unit = threshold.get("unit")
    if (
        not isinstance(operator, str)
        or not operator
        or not isinstance(threshold_unit, str)
        or unit != threshold_unit
    ):
        raise ValueError("requirement threshold metadata is invalid")
    for name, parameter in parameters.items():
        if not isinstance(name, str) or not name:
            raise ValueError("requirement parameter names must be non-empty strings")
        if isinstance(parameter, bool) or not isinstance(parameter, (int, float, str)):
            raise ValueError("requirement parameters must contain scalar values")
        if isinstance(parameter, float) and not math.isfinite(parameter):
            raise ValueError("requirement parameters must be finite")
    target = _finite_number(threshold.get("target"), "requirement target")
    measured = _finite_number(value.get("value"), "requirement value")
    parameters_json = json.dumps(
        parameters,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    identity = json.dumps(
        [analysis_name, metric, operator, target, threshold_unit, parameters],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "experiment_id": experiment_id,
        "point_index": point_index,
        "check_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "requirement_index": requirement_index,
        "analysis_name": analysis_name,
        "metric": metric,
        "operator": operator,
        "target": target,
        "unit": unit,
        "value": measured,
        "passed": passed,
        "parameters_json": parameters_json,
    }


def _result_children(
    document: dict[str, object],
    record: dict[str, object],
    parameter_records: list[dict[str, object]],
    ordinals: dict[str, int],
    point_plan: object = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, int],
]:
    experiment_id = str(record["experiment_id"])
    if document.get("experiment_id") != experiment_id:
        raise ValueError("results experiment_id does not match its directory")
    if document.get("status") != record["status"]:
        raise ValueError("manifest and results status do not match")
    if document.get("execution_mode", "independent") != record["execution_mode"]:
        raise ValueError("manifest and results execution_mode do not match")
    base_order = [
        item["name"] for item in parameter_records if item["kind"] == "base"
    ]
    derived_order = [
        item["name"] for item in parameter_records if item["kind"] == "derived"
    ]
    if document.get("parameter_order") != base_order:
        raise ValueError("definition and results parameter_order do not match")
    if document.get("derived_parameter_order", []) != derived_order:
        raise ValueError("definition and results derived_parameter_order do not match")
    expected_units = {
        str(item["name"]): str(item["unit"])
        for item in parameter_records
        if item["unit"] is not None
    }
    if document.get("parameter_units", {}) != expected_units:
        raise ValueError("definition and results parameter_units do not match")
    if isinstance(point_plan, dict) and isinstance(point_plan.get("points"), list):
        expected_base_parameters = point_plan["points"]
    else:
        base_values = [
            json.loads(str(item["declared_values_json"]))
            for item in parameter_records
            if item["kind"] == "base"
        ]
        expected_base_parameters = [
            dict(zip(base_order, values)) for values in itertools.product(*base_values)
        ]
    point_count = _plain_int(document.get("point_count"), "results point_count")
    if point_count != record["point_count"]:
        raise ValueError("manifest and results point_count do not match")
    points_value = document.get("points")
    if not isinstance(points_value, list):
        raise ValueError("results points must be a list")
    if record["status"] == "completed" and len(points_value) != point_count:
        raise ValueError("completed results must contain every experiment point")
    if len(points_value) > point_count:
        raise ValueError("results contain more points than the experiment")

    points: list[dict[str, object]] = []
    point_parameters: list[dict[str, object]] = []
    measurements: list[dict[str, object]] = []
    requirements: list[dict[str, object]] = []
    seen_indexes: set[int] = set()
    seen_parameter_maps: set[tuple[tuple[str, str], ...]] = set()
    allow_duplicate_parameter_maps = isinstance(point_plan, dict)
    completed_points = 0
    error_points = 0
    passed_points = 0
    for point_value in points_value:
        if not isinstance(point_value, dict):
            raise ValueError("experiment points must contain objects")
        index = _plain_int(point_value.get("index"), "point index")
        if index >= point_count or index in seen_indexes:
            raise ValueError("point index is outside the experiment or duplicated")
        seen_indexes.add(index)
        simulation_status = point_value.get("simulation_status")
        if simulation_status not in {"completed", "error", "cancelled"}:
            raise ValueError("point simulation_status is invalid")
        all_passed = point_value.get("all_passed")
        if not isinstance(all_passed, bool):
            raise ValueError("point all_passed must be a boolean")
        error = point_value.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("point error must be a string or null")
        duration = point_value.get("duration_seconds")
        if duration is not None:
            duration = _finite_number(duration, "point duration_seconds")
            if duration < 0:
                raise ValueError("point duration_seconds must be nonnegative")
        native_step_index = point_value.get("native_step_index")
        if native_step_index is not None:
            native_step_index = _plain_int(native_step_index, "native_step_index")
        cache_hit = point_value.get("cache_hit")
        if cache_hit is not None and not isinstance(cache_hit, bool):
            raise ValueError("point cache_hit must be a boolean")
        cache_key = point_value.get("cache_key")
        if cache_key is not None and not isinstance(cache_key, str):
            raise ValueError("point cache_key must be a string or null")
        parameter_map = point_value.get("parameters")
        if (
            not isinstance(parameter_map, dict)
            or any(not isinstance(name, str) for name in parameter_map)
            or any(not isinstance(value, str) for value in parameter_map.values())
        ):
            raise ValueError("point parameters must map strings to strings")
        if ordinals and set(parameter_map) != set(ordinals):
            raise ValueError("point parameter names do not match the definition")
        if {
            name: parameter_map[name] for name in base_order
        } != expected_base_parameters[index]:
            raise ValueError("point parameters do not match the planned index order")
        parameter_identity = tuple(sorted(parameter_map.items()))
        if (
            parameter_identity in seen_parameter_maps
            and not allow_duplicate_parameter_maps
        ):
            raise ValueError("duplicate point parameter map")
        seen_parameter_maps.add(parameter_identity)
        for name, value in parameter_map.items():
            point_parameters.append(
                {
                    "experiment_id": experiment_id,
                    "point_index": index,
                    "ordinal": ordinals.get(name, len(ordinals)),
                    "name": name,
                    "value_text": value,
                }
            )
        measurement_map = point_value.get("measurements")
        if not isinstance(measurement_map, dict):
            raise ValueError("point measurements must contain an object")
        for name, value in measurement_map.items():
            if not isinstance(name, str) or not name:
                raise ValueError("measurement names must be non-empty strings")
            measurements.append(
                {
                    "experiment_id": experiment_id,
                    "point_index": index,
                    "name": name,
                    "value": _finite_number(value, f"measurement {name}"),
                }
            )
        analyses = point_value.get("analyses")
        if not isinstance(analyses, list):
            raise ValueError("point analyses must be a list")
        analysis_names: set[str] = set()
        point_check_ids: set[str] = set()
        analysis_error = False
        analyses_passed = True
        requirement_index = 0
        for analysis_value in analyses:
            if not isinstance(analysis_value, dict):
                raise ValueError("analysis result must contain an object")
            analysis_name = analysis_value.get("name")
            analysis_status = analysis_value.get("status")
            if (
                not isinstance(analysis_name, str)
                or not analysis_name
                or analysis_name in analysis_names
                or analysis_status not in {"completed", "error"}
            ):
                raise ValueError("analysis name/status is invalid or duplicated")
            analysis_names.add(analysis_name)
            if analysis_status == "error":
                analysis_error = True
                analyses_passed = False
                continue
            analysis = analysis_value.get("analysis")
            if not isinstance(analysis, dict) or not isinstance(analysis.get("results"), list):
                raise ValueError("completed analysis must contain requirement results")
            analysis_all_passed = analysis.get("all_passed")
            if not isinstance(analysis_all_passed, bool):
                raise ValueError("completed analysis all_passed must be a boolean")
            requirement_passes: list[bool] = []
            for requirement_value in analysis["results"]:
                row = _requirement_row(
                    experiment_id,
                    index,
                    analysis_name,
                    requirement_index,
                    requirement_value,
                )
                if row["check_id"] in point_check_ids:
                    raise ValueError("duplicate requirement identity")
                point_check_ids.add(str(row["check_id"]))
                requirements.append(row)
                requirement_passes.append(bool(row["passed"]))
                requirement_index += 1
            expected_analysis_passed = all(requirement_passes)
            if analysis_all_passed != expected_analysis_passed:
                raise ValueError("analysis all_passed is inconsistent with requirements")
            analyses_passed = analyses_passed and analysis_all_passed
        expected_point_passed = simulation_status == "completed" and analyses_passed
        if all_passed != expected_point_passed:
            raise ValueError("point all_passed is inconsistent with its analyses")
        points.append(
            {
                "experiment_id": experiment_id,
                "point_index": index,
                "simulation_status": simulation_status,
                "duration_seconds": duration,
                "all_passed": all_passed,
                "error": error,
                "cache_hit": cache_hit,
                "cache_key": cache_key,
                "native_step_index": native_step_index,
            }
        )
        completed_points += simulation_status == "completed"
        error_points += simulation_status not in {"completed", "cancelled"} or analysis_error
        passed_points += all_passed

    derived_counts = {
        "finished_points": len(points),
        "completed_points": completed_points,
        "error_points": error_points,
        "passed_points": passed_points,
        "failed_points": len(points) - passed_points,
    }
    for name in ("completed_points", "error_points", "passed_points", "failed_points"):
        if _plain_int(document.get(name), f"results {name}") != derived_counts[name]:
            raise ValueError(f"results {name} is inconsistent with its points")
    document_all_passed = document.get("all_passed")
    if not isinstance(document_all_passed, bool):
        raise ValueError("results all_passed must be a boolean")
    expected_all_passed = (
        record["status"] == "completed"
        and len(points) == point_count
        and passed_points == point_count
    )
    if document_all_passed != expected_all_passed:
        raise ValueError("results all_passed is inconsistent with its points")
    for name in ("completed_points", "error_points", "passed_points", "failed_points"):
        if record[name] != derived_counts[name]:
            raise ValueError(f"manifest {name} does not match results")
    if (
        record["manifest_schema_version"] == 2
        and record["finished_points"] != derived_counts["finished_points"]
    ):
        raise ValueError("manifest finished_points does not match results")
    if record["all_passed"] != document_all_passed:
        raise ValueError("manifest all_passed does not match results")
    return points, point_parameters, measurements, requirements, derived_counts


def _verify_waveform_artifacts(
    results: dict[str, object], experiment_dir: Path, experiment_id: str
) -> None:
    points = results.get("points")
    assert isinstance(points, list)
    artifact_hashes: dict[Path, tuple[str, int]] = {}
    for point in points:
        assert isinstance(point, dict)
        analyses = point.get("analyses")
        assert isinstance(analyses, list)
        for analysis_record in analyses:
            assert isinstance(analysis_record, dict)
            analysis = analysis_record.get("analysis")
            if not isinstance(analysis, dict):
                continue
            expected_hash = analysis.get("raw_sha256")
            expected_size = analysis.get("raw_size_bytes")
            if expected_hash is None and expected_size is None:
                continue
            raw_path, _ = waveform_artifact_path(
                analysis.get("raw_file"), experiment_dir, experiment_id
            )
            if raw_path not in artifact_hashes:
                artifact_hashes[raw_path] = (
                    _sha256_file(raw_path),
                    raw_path.stat().st_size,
                )
            digest, size = artifact_hashes[raw_path]
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or digest != expected_hash
            ):
                raise ValueError("waveform RAW artifact hash does not match its analysis")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
                or size != expected_size
            ):
                raise ValueError("waveform RAW artifact size does not match its analysis")


def load_completed_experiment(
    root: Path, experiment_id: str
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    """Load one completed experiment through the canonical artifact validator."""
    root = root.resolve()
    _id_timestamp(experiment_id)
    experiment_dir = (root / experiment_id).resolve()
    _relative_path(experiment_dir, root)
    if experiment_dir.name != experiment_id or not experiment_dir.is_dir():
        raise FileNotFoundError(f"Experiment not found: {experiment_id}")
    manifest_path = experiment_dir / "experiment_manifest.json"
    results_path = experiment_dir / "results.json"
    _relative_path(manifest_path, root)
    _relative_path(results_path, root)
    manifest, manifest_hash = _load_json(manifest_path)
    record, parameters, ordinals = _manifest_record(
        manifest, experiment_id, manifest_path, root, manifest_hash
    )
    if record["status"] != "completed":
        raise ValueError(f"Experiment {experiment_id} is not completed")
    results, results_hash = _load_json(results_path)
    definition = manifest.get("definition")
    point_plan = definition.get("point_plan") if isinstance(definition, dict) else None
    _result_children(results, record, parameters, ordinals, point_plan)
    _verify_waveform_artifacts(results, experiment_dir, experiment_id)
    record.update(
        index_state="results_valid",
        results_path=_relative_path(results_path, root),
        results_sha256=results_hash,
    )
    return experiment_dir, manifest, results, record


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = DELETE;
        CREATE TABLE index_metadata (
            schema_version INTEGER NOT NULL,
            builder_version INTEGER NOT NULL
        );
        CREATE TABLE experiments (
            experiment_id TEXT PRIMARY KEY,
            manifest_schema_version INTEGER NOT NULL,
            engine_version INTEGER,
            status TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            definition_hash TEXT,
            recorded_at TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            finished_at TEXT,
            point_count INTEGER NOT NULL,
            finished_points INTEGER NOT NULL,
            completed_points INTEGER NOT NULL,
            error_points INTEGER NOT NULL,
            passed_points INTEGER NOT NULL,
            failed_points INTEGER NOT NULL,
            all_passed INTEGER,
            error TEXT,
            reuse_cache INTEGER NOT NULL,
            index_state TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            results_path TEXT,
            manifest_sha256 TEXT NOT NULL,
            results_sha256 TEXT
        );
        CREATE TABLE experiment_parameters (
            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            unit TEXT,
            declared_values_json TEXT,
            template TEXT,
            PRIMARY KEY (experiment_id, name),
            UNIQUE (experiment_id, ordinal)
        );
        CREATE TABLE points (
            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
            point_index INTEGER NOT NULL,
            simulation_status TEXT NOT NULL,
            duration_seconds REAL,
            all_passed INTEGER NOT NULL,
            error TEXT,
            cache_hit INTEGER,
            cache_key TEXT,
            native_step_index INTEGER,
            PRIMARY KEY (experiment_id, point_index)
        );
        CREATE TABLE point_parameters (
            experiment_id TEXT NOT NULL,
            point_index INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            name TEXT NOT NULL,
            value_text TEXT NOT NULL,
            PRIMARY KEY (experiment_id, point_index, name),
            FOREIGN KEY (experiment_id, point_index)
                REFERENCES points(experiment_id, point_index) ON DELETE CASCADE
        );
        CREATE INDEX point_parameter_lookup
            ON point_parameters(name, value_text, experiment_id, point_index);
        CREATE TABLE measurements (
            experiment_id TEXT NOT NULL,
            point_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (experiment_id, point_index, name),
            FOREIGN KEY (experiment_id, point_index)
                REFERENCES points(experiment_id, point_index) ON DELETE CASCADE
        );
        CREATE TABLE requirements (
            experiment_id TEXT NOT NULL,
            point_index INTEGER NOT NULL,
            check_id TEXT NOT NULL,
            requirement_index INTEGER NOT NULL,
            analysis_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            operator TEXT NOT NULL,
            target REAL NOT NULL,
            unit TEXT NOT NULL,
            value REAL NOT NULL,
            passed INTEGER NOT NULL,
            parameters_json TEXT NOT NULL,
            PRIMARY KEY (experiment_id, point_index, check_id),
            FOREIGN KEY (experiment_id, point_index)
                REFERENCES points(experiment_id, point_index) ON DELETE CASCADE
        );
        CREATE TABLE index_issues (
            artifact_path TEXT NOT NULL,
            code TEXT NOT NULL,
            message TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO index_metadata(schema_version, builder_version) VALUES (?, ?)",
        (INDEX_SCHEMA_VERSION, INDEX_BUILDER_VERSION),
    )


def _insert_experiment(
    connection: sqlite3.Connection,
    record: dict[str, object],
    parameters: list[dict[str, object]],
) -> None:
    fields = list(record)
    connection.execute(
        f"INSERT INTO experiments ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        [record[field] for field in fields],
    )
    connection.executemany(
        """INSERT INTO experiment_parameters
           (experiment_id, ordinal, name, kind, unit, declared_values_json, template)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                record["experiment_id"],
                item["ordinal"],
                item["name"],
                item["kind"],
                item["unit"],
                item["declared_values_json"],
                item["template"],
            )
            for item in parameters
        ],
    )


def _insert_children(
    connection: sqlite3.Connection,
    points: list[dict[str, object]],
    parameters: list[dict[str, object]],
    measurements: list[dict[str, object]],
    requirements: list[dict[str, object]],
) -> None:
    connection.executemany(
        """INSERT INTO points
           (experiment_id, point_index, simulation_status, duration_seconds,
            all_passed, error, cache_hit, cache_key, native_step_index)
           VALUES (:experiment_id, :point_index, :simulation_status,
                   :duration_seconds, :all_passed, :error, :cache_hit,
                   :cache_key, :native_step_index)""",
        points,
    )
    connection.executemany(
        """INSERT INTO point_parameters
           (experiment_id, point_index, ordinal, name, value_text)
           VALUES (:experiment_id, :point_index, :ordinal, :name, :value_text)""",
        parameters,
    )
    connection.executemany(
        """INSERT INTO measurements
           (experiment_id, point_index, name, value)
           VALUES (:experiment_id, :point_index, :name, :value)""",
        measurements,
    )
    connection.executemany(
        """INSERT INTO requirements
           (experiment_id, point_index, check_id, requirement_index,
            analysis_name, metric, operator, target, unit, value, passed,
            parameters_json)
           VALUES (:experiment_id, :point_index, :check_id,
                   :requirement_index, :analysis_name, :metric, :operator,
                   :target, :unit, :value, :passed, :parameters_json)""",
        requirements,
    )


def build_experiment_index(
    root: Path,
    database_path: Path | None = None,
) -> ExperimentIndexBuildResult:
    """Rebuild the derived experiment index and atomically publish it."""
    root = root.resolve()
    path = _database_path(root, database_path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    issues: list[ExperimentIndexIssue] = []
    scanned = 0
    indexed = 0
    result_experiments = 0
    indexed_points = 0
    connection: sqlite3.Connection | None = None
    with _INDEX_LOCK:
        try:
            connection = sqlite3.connect(temporary)
            _create_schema(connection)
            for manifest_path in sorted(root.glob("mcp-experiment-*/experiment_manifest.json")):
                scanned += 1
                experiment_dir = manifest_path.parent
                artifact_path = manifest_path.relative_to(root).as_posix()
                try:
                    _relative_path(experiment_dir, root)
                    _relative_path(manifest_path, root)
                    experiment_id = experiment_dir.name
                    _id_timestamp(experiment_id)
                    manifest, manifest_hash = _load_json(manifest_path)
                    record, parameters, ordinals = _manifest_record(
                        manifest,
                        experiment_id,
                        manifest_path,
                        root,
                        manifest_hash,
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    issues.append(
                        {
                            "artifact_path": artifact_path,
                            "code": "invalid_manifest",
                            "message": str(exc),
                        }
                    )
                    continue

                child_rows: tuple[
                    list[dict[str, object]],
                    list[dict[str, object]],
                    list[dict[str, object]],
                    list[dict[str, object]],
                    dict[str, int],
                ] | None = None
                results_path = experiment_dir / "results.json"
                if record["status"] in {"completed", "cancelled"}:
                    if results_path.is_file():
                        try:
                            _relative_path(results_path, root)
                            results, results_hash = _load_json(results_path)
                            definition = manifest.get("definition")
                            point_plan = (
                                definition.get("point_plan")
                                if isinstance(definition, dict)
                                else None
                            )
                            child_rows = _result_children(
                                results, record, parameters, ordinals, point_plan
                            )
                            _verify_waveform_artifacts(
                                results, experiment_dir, experiment_id
                            )
                            record.update(
                                index_state="results_valid",
                                results_path=results_path.relative_to(root).as_posix(),
                                results_sha256=results_hash,
                                **child_rows[-1],
                                all_passed=results["all_passed"],
                            )
                        except (
                            OSError,
                            UnicodeError,
                            json.JSONDecodeError,
                            ValueError,
                        ) as exc:
                            child_rows = None
                            record["index_state"] = "invalid_results"
                            record["all_passed"] = None
                            issues.append(
                                {
                                    "artifact_path": results_path.relative_to(root).as_posix(),
                                    "code": "invalid_results",
                                    "message": str(exc),
                                }
                            )
                    elif record["status"] == "completed":
                        record["index_state"] = "invalid_results"
                        record["all_passed"] = None
                        issues.append(
                            {
                                "artifact_path": results_path.relative_to(root).as_posix(),
                                "code": "missing_results",
                                "message": "completed experiment has no results.json",
                            }
                        )

                _insert_experiment(connection, record, parameters)
                indexed += 1
                if child_rows is not None:
                    _insert_children(connection, *child_rows[:-1])
                    result_experiments += 1
                    indexed_points += len(child_rows[0])

            issues.sort(key=lambda item: (item["artifact_path"], item["code"], item["message"]))
            connection.executemany(
                "INSERT INTO index_issues(artifact_path, code, message) VALUES (?, ?, ?)",
                [
                    (issue["artifact_path"], issue["code"], issue["message"])
                    for issue in issues
                ],
            )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("experiment index foreign-key check failed")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError("experiment index integrity check failed")
            connection.close()
            connection = None
            os.replace(temporary, path)
        except Exception:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)
            raise
    return {
        "database_path": str(path),
        "scanned_experiments": scanned,
        "indexed_experiments": indexed,
        "result_experiments": result_experiments,
        "indexed_points": indexed_points,
        "issue_count": len(issues),
        "issues": issues,
    }


def _validate_query(
    limit: int,
    offset: int,
    status: str | None,
    execution_mode: str | None,
    all_passed: bool | None,
    parameters: dict[str, str] | None,
) -> dict[str, str]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if status is not None and status not in EXPERIMENT_STATUSES:
        raise ValueError("status is not supported")
    if execution_mode is not None and execution_mode not in EXECUTION_MODES:
        raise ValueError("execution_mode is not supported")
    if all_passed is not None and not isinstance(all_passed, bool):
        raise ValueError("all_passed must be a boolean or null")
    if parameters is None:
        return {}
    if (
        not isinstance(parameters, dict)
        or not parameters
        or any(not isinstance(name, str) or not name for name in parameters)
        or any(not isinstance(value, str) for value in parameters.values())
    ):
        raise ValueError("parameters must be a non-empty string-to-string object")
    return parameters


def query_experiments(
    root: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    execution_mode: str | None = None,
    all_passed: bool | None = None,
    parameters: dict[str, str] | None = None,
    database_path: Path | None = None,
) -> ExperimentQueryResult:
    """Query indexed experiment summaries with exact same-point parameter filters."""
    root = root.resolve()
    path = _database_path(root, database_path)
    parameter_filter = _validate_query(
        limit, offset, status, execution_mode, all_passed, parameters
    )
    if not path.is_file():
        raise FileNotFoundError("experiment index not found; build it first")
    clauses: list[str] = []
    arguments: list[object] = []
    if status is not None:
        clauses.append("e.status = ?")
        arguments.append(status)
    if execution_mode is not None:
        clauses.append("e.execution_mode = ?")
        arguments.append(execution_mode)
    if all_passed is not None:
        clauses.append("e.all_passed = ?")
        arguments.append(int(all_passed))
    if parameter_filter:
        pairs = list(parameter_filter.items())
        alternatives = " OR ".join("(pp.name = ? AND pp.value_text = ?)" for _ in pairs)
        clauses.append(
            "e.experiment_id IN ("
            "SELECT pp.experiment_id FROM point_parameters pp "
            f"WHERE {alternatives} "
            "GROUP BY pp.experiment_id, pp.point_index "
            "HAVING COUNT(*) = ?"
            ")"
        )
        for name, value in pairs:
            arguments.extend((name, value))
        arguments.append(len(pairs))
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    select = (
        "SELECT e.experiment_id, e.status, e.execution_mode, e.index_state, "
        "e.recorded_at, e.point_count, e.finished_points, e.completed_points, "
        "e.error_points, e.passed_points, e.failed_points, e.all_passed, "
        "e.reuse_cache, e.manifest_path, e.results_path FROM experiments e"
    )
    with _INDEX_LOCK:
        connection = sqlite3.connect(path)
        try:
            metadata = connection.execute(
                "SELECT schema_version, builder_version FROM index_metadata"
            ).fetchone()
            if metadata != (INDEX_SCHEMA_VERSION, INDEX_BUILDER_VERSION):
                raise ValueError("experiment index version is not supported; rebuild it")
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM (" + select + where + ")",
                    arguments,
                ).fetchone()[0]
            )
            rows = connection.execute(
                select
                + where
                + " ORDER BY e.recorded_at DESC, e.experiment_id DESC LIMIT ? OFFSET ?",
                [*arguments, limit, offset],
            ).fetchall()
            experiments: list[ExperimentIndexRecord] = []
            for row in rows:
                experiment_id = str(row[0])
                parameter_rows = connection.execute(
                    """SELECT ordinal, name, kind, unit, declared_values_json, template
                       FROM experiment_parameters WHERE experiment_id = ?
                       ORDER BY ordinal""",
                    (experiment_id,),
                ).fetchall()
                measurement_names = [
                    str(item[0])
                    for item in connection.execute(
                        """SELECT DISTINCT name FROM measurements
                           WHERE experiment_id = ? ORDER BY name""",
                        (experiment_id,),
                    )
                ]
                requirement_metrics = [
                    str(item[0])
                    for item in connection.execute(
                        """SELECT DISTINCT metric FROM requirements
                           WHERE experiment_id = ? ORDER BY metric""",
                        (experiment_id,),
                    )
                ]
                experiments.append(
                    {
                        "experiment_id": experiment_id,
                        "status": str(row[1]),
                        "execution_mode": str(row[2]),
                        "index_state": str(row[3]),
                        "recorded_at": str(row[4]),
                        "point_count": int(row[5]),
                        "finished_points": int(row[6]),
                        "completed_points": int(row[7]),
                        "error_points": int(row[8]),
                        "passed_points": int(row[9]),
                        "failed_points": int(row[10]),
                        "all_passed": None if row[11] is None else bool(row[11]),
                        "reuse_cache": bool(row[12]),
                        "manifest_path": str(row[13]),
                        "results_path": None if row[14] is None else str(row[14]),
                        "parameters": [
                            {
                                "ordinal": int(item[0]),
                                "name": str(item[1]),
                                "kind": str(item[2]),
                                "unit": None if item[3] is None else str(item[3]),
                                "values": None
                                if item[4] is None
                                else json.loads(str(item[4])),
                                "template": None if item[5] is None else str(item[5]),
                            }
                            for item in parameter_rows
                        ],
                        "measurement_names": measurement_names,
                        "requirement_metrics": requirement_metrics,
                    }
                )
        finally:
            connection.close()
    return {
        "database_path": str(path),
        "total": total,
        "limit": limit,
        "offset": offset,
        "experiments": experiments,
    }
