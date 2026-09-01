"""Portable, non-mutating study-recipe validation for System Builder."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import artifacts
import experiment_engine
import statistical_engine
from ltspice_text import decode_text

STUDY_RECIPE_SCHEMA_VERSION = 1
MAX_RECIPE_BYTES = 1024 * 1024
MAX_NETLIST_BYTES = 2 * 1024 * 1024
ALLOWED_RECIPE_KEYS = {
    "schema_version",
    "kind",
    "name",
    "description",
    "plan",
    "experiments",
    "execution",
    "report_context",
}
ALLOWED_PLAN_KEYS = {
    "variables",
    "sample_count",
    "seed",
    "correlations",
    "corner_axes",
    "corner_aggregate",
    "sampling_method",
}
ALLOWED_EXPERIMENT_KEYS = {
    "name",
    "netlist_path",
    "filename",
    "waveform_analyses",
}
ALLOWED_REPORT_CONTEXT_KEYS = {
    "title",
    "circuit_summary",
    "simulation_summary",
    "mcp_context",
    "schematic_path",
    "schematic_source_path",
    "schematic_caption",
}


def _error(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


_canonical_json = artifacts.canonical_json


def _plan_error_path(message: str, variables: object) -> str:
    lowered = message.lower()
    if "sample_count" in lowered or "sample count" in lowered:
        return "plan.sample_count"
    if "seed" in lowered:
        return "plan.seed"
    if "sampling_method" in lowered or "sampling method" in lowered:
        return "plan.sampling_method"
    if "correlation" in lowered or "matrix" in lowered:
        return "plan.correlations"
    if "corner" in lowered:
        return "plan.corner_axes"
    if isinstance(variables, list):
        for index, variable in enumerate(variables):
            if isinstance(variable, dict):
                name = variable.get("name")
                if isinstance(name, str) and name.lower() in lowered:
                    return f"plan.variables[{index}]"
    return "plan"


def _confined_file(
    workspace_root: Path,
    relative_path: object,
    field_path: str,
) -> tuple[Path | None, dict[str, str] | None]:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
    ):
        return None, _error(
            field_path,
            "invalid_path",
            "netlist_path must be a non-empty portable path using forward slashes",
        )
    portable = PurePosixPath(relative_path)
    if portable.is_absolute() or ".." in portable.parts:
        return None, _error(
            field_path,
            "escaped_workspace",
            "netlist_path must remain inside the selected workspace",
        )
    root = workspace_root.resolve()
    unresolved = root.joinpath(*portable.parts)
    cursor = root
    for part in portable.parts:
        cursor /= part
        if cursor.is_symlink():
            return None, _error(
                field_path,
                "symlink_rejected",
                "netlist_path must not traverse a symbolic link",
            )
    try:
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError):
        return None, _error(
            field_path,
            "netlist_not_found",
            "netlist_path does not identify a file inside the selected workspace",
        )
    if not resolved.is_file() or resolved.suffix.lower() not in {".cir", ".net"}:
        return None, _error(
            field_path,
            "invalid_netlist",
            "netlist_path must identify a .cir or .net file",
        )
    if resolved.stat().st_size > MAX_NETLIST_BYTES:
        return None, _error(
            field_path,
            "netlist_too_large",
            f"netlists are limited to {MAX_NETLIST_BYTES} bytes",
        )
    return resolved, None


def load_study_recipe(path: Path) -> dict[str, object]:
    """Load one bounded UTF-8 recipe without accepting non-finite JSON values."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("study recipe must be a regular file")
    if path.stat().st_size > MAX_RECIPE_BYTES:
        raise ValueError(f"study recipes are limited to {MAX_RECIPE_BYTES} bytes")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("study recipe must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("study recipe must be a JSON object")
    return value


def preview_study_recipe(
    recipe: object,
    workspace_root: Path,
) -> dict[str, object]:
    """Validate and resolve a recipe without publishing plans or running LTspice."""
    errors: list[dict[str, str]] = []
    if not isinstance(recipe, dict):
        return {
            "valid": False,
            "errors": [
                _error("$", "invalid_recipe", "study recipe must be a JSON object")
            ],
        }
    unknown = sorted(set(recipe) - ALLOWED_RECIPE_KEYS)
    for key in unknown:
        errors.append(
            _error(key, "unknown_field", f"unknown study recipe field: {key}")
        )
    if recipe.get("schema_version") != STUDY_RECIPE_SCHEMA_VERSION:
        errors.append(
            _error(
                "schema_version",
                "unsupported_schema",
                f"schema_version must be {STUDY_RECIPE_SCHEMA_VERSION}",
            )
        )
    if recipe.get("kind") != "statistical":
        errors.append(
            _error(
                "kind",
                "unsupported_kind",
                "GUI-A1 supports statistical study recipes",
            )
        )
    name = recipe.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        errors.append(
            _error("name", "invalid_name", "name must contain 1 to 120 characters")
        )
    description = recipe.get("description", "")
    if not isinstance(description, str) or len(description) > 1000:
        errors.append(
            _error(
                "description",
                "invalid_description",
                "description must contain at most 1,000 characters",
            )
        )

    plan_definition = recipe.get("plan")
    plan: statistical_engine.StatisticalPlan | None = None
    if not isinstance(plan_definition, dict):
        errors.append(_error("plan", "invalid_plan", "plan must be an object"))
    else:
        for key in sorted(set(plan_definition) - ALLOWED_PLAN_KEYS):
            errors.append(
                _error(
                    f"plan.{key}",
                    "unknown_field",
                    f"unknown statistical plan field: {key}",
                )
            )
        if not errors:
            try:
                plan = statistical_engine.build_statistical_plan(
                    plan_definition.get("variables"),  # type: ignore[arg-type]
                    plan_definition.get("sample_count"),  # type: ignore[arg-type]
                    plan_definition.get("seed"),  # type: ignore[arg-type]
                    plan_definition.get("correlations"),  # type: ignore[arg-type]
                    plan_definition.get("corner_axes"),  # type: ignore[arg-type]
                    plan_definition.get("corner_aggregate", False),  # type: ignore[arg-type]
                    source_root=workspace_root,
                    sampling_method=plan_definition.get(  # type: ignore[arg-type]
                        "sampling_method", "independent"
                    ),
                )
            except (TypeError, ValueError) as exc:
                errors.append(
                    _error(
                        _plan_error_path(str(exc), plan_definition.get("variables")),
                        "invalid_plan",
                        str(exc),
                    )
                )

    experiments = recipe.get("experiments")
    experiment_previews: list[dict[str, object]] = []
    if not isinstance(experiments, list) or not 1 <= len(experiments) <= 8:
        errors.append(
            _error(
                "experiments",
                "invalid_experiments",
                "experiments must contain between 1 and 8 entries",
            )
        )
    elif plan is not None:
        names: set[str] = set()
        for index, experiment in enumerate(experiments):
            base = f"experiments[{index}]"
            if not isinstance(experiment, dict):
                errors.append(
                    _error(base, "invalid_experiment", "experiment must be an object")
                )
                continue
            for key in sorted(set(experiment) - ALLOWED_EXPERIMENT_KEYS):
                errors.append(
                    _error(
                        f"{base}.{key}",
                        "unknown_field",
                        f"unknown experiment field: {key}",
                    )
                )
            experiment_name = experiment.get("name")
            if (
                not isinstance(experiment_name, str)
                or not experiment_name
                or experiment_name in names
            ):
                errors.append(
                    _error(
                        f"{base}.name",
                        "invalid_name",
                        "experiment name must be non-empty and unique",
                    )
                )
                continue
            names.add(experiment_name)
            netlist_path, path_error = _confined_file(
                workspace_root,
                experiment.get("netlist_path"),
                f"{base}.netlist_path",
            )
            if path_error is not None:
                errors.append(path_error)
                continue
            assert netlist_path is not None
            try:
                netlist = decode_text(netlist_path.read_bytes())
            except (OSError, UnicodeError):
                errors.append(
                    _error(
                        f"{base}.netlist_path",
                        "invalid_encoding",
                        "netlist must be readable UTF-8 or UTF-16 text",
                    )
                )
                continue
            filename = experiment.get("filename")
            if (
                not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or "/" in filename
                or "\\" in filename
                or Path(filename).suffix.lower() not in {".cir", ".net"}
            ):
                errors.append(
                    _error(
                        f"{base}.filename",
                        "invalid_filename",
                        "filename must be a plain .cir or .net file name",
                    )
                )
                continue
            analyses = experiment.get("waveform_analyses")
            try:
                experiment_engine._prepare_explicit_experiment(
                    netlist,
                    plan["parameter_order"],
                    [point["parameters"] for point in plan["points"]],
                    plan["parameter_units"],
                    analyses,  # type: ignore[arg-type]
                )
            except (TypeError, ValueError) as exc:
                message = str(exc)
                error_path = (
                    f"{base}.netlist_path"
                    if "placeholder" in message or "netlist_template" in message
                    else f"{base}.waveform_analyses"
                )
                errors.append(
                    _error(
                        error_path,
                        "invalid_experiment",
                        message,
                    )
                )
                continue
            assert isinstance(analyses, list)
            experiment_previews.append(
                {
                    "name": experiment_name,
                    "filename": filename,
                    "netlist_path": experiment["netlist_path"],
                    "netlist_sha256": hashlib.sha256(
                        netlist.encode("utf-8")
                    ).hexdigest(),
                    "analysis_count": len(analyses),
                    "requirement_count": sum(
                        len(analysis["requirements"]) for analysis in analyses
                    ),
                }
            )

    execution = recipe.get("execution", {})
    if not isinstance(execution, dict):
        errors.append(
            _error("execution", "invalid_execution", "execution must be an object")
        )
    else:
        unknown_execution = sorted(set(execution) - {"max_concurrency", "reuse_cache"})
        for key in unknown_execution:
            errors.append(
                _error(
                    f"execution.{key}",
                    "unknown_field",
                    f"unknown execution field: {key}",
                )
            )
        concurrency = execution.get("max_concurrency", 2)
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or not 1 <= concurrency <= 8
        ):
            errors.append(
                _error(
                    "execution.max_concurrency",
                    "invalid_concurrency",
                    "max_concurrency must be an integer from 1 through 8",
                )
            )
        if not isinstance(execution.get("reuse_cache", False), bool):
            errors.append(
                _error(
                    "execution.reuse_cache",
                    "invalid_reuse_cache",
                    "reuse_cache must be a boolean",
                )
            )

    report_context = recipe.get("report_context", {})
    if not isinstance(report_context, dict):
        errors.append(
            _error(
                "report_context",
                "invalid_report_context",
                "report_context must be an object",
            )
        )
    else:
        for key in sorted(set(report_context) - ALLOWED_REPORT_CONTEXT_KEYS):
            errors.append(
                _error(
                    f"report_context.{key}",
                    "unknown_field",
                    f"unknown report context field: {key}",
                )
            )
        for key, value in report_context.items():
            if key in ALLOWED_REPORT_CONTEXT_KEYS and (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 1_200
            ):
                errors.append(
                    _error(
                        f"report_context.{key}",
                        "invalid_report_context",
                        "report context values must contain 1 to 1,200 characters",
                    )
                )
        schematic_path = report_context.get("schematic_path")
        if isinstance(schematic_path, str):
            portable = PurePosixPath(schematic_path)
            if (
                "\\" in schematic_path
                or portable.is_absolute()
                or ".." in portable.parts
                or portable.suffix.lower() not in {".png", ".jpg", ".jpeg"}
            ):
                errors.append(
                    _error(
                        "report_context.schematic_path",
                        "invalid_path",
                        "schematic_path must be a portable PNG or JPEG workspace path",
                    )
                )
        schematic_source_path = report_context.get("schematic_source_path")
        if isinstance(schematic_source_path, str):
            portable = PurePosixPath(schematic_source_path)
            if (
                "\\" in schematic_source_path
                or portable.is_absolute()
                or ".." in portable.parts
                or portable.suffix.lower() != ".asc"
            ):
                errors.append(
                    _error(
                        "report_context.schematic_source_path",
                        "invalid_path",
                        "schematic_source_path must be a portable LTspice .asc workspace path",
                    )
                )
    try:
        canonical_recipe = _canonical_json(recipe)
    except (TypeError, ValueError):
        errors.append(
            _error(
                "$",
                "non_portable_json",
                "study recipe must contain only finite JSON-compatible values",
            )
        )
        canonical_recipe = ""

    if errors or plan is None:
        return {"valid": False, "errors": errors}

    plan_artifact = statistical_engine._artifact_bytes(plan)
    plan_sha256 = hashlib.sha256(plan_artifact).hexdigest()
    point_count = len(plan["points"])
    experiment_count = len(experiment_previews)
    return {
        "valid": True,
        "errors": [],
        "recipe": {
            "name": name,
            "description": description,
            "schema_version": STUDY_RECIPE_SCHEMA_VERSION,
            "kind": "statistical",
            "sha256": hashlib.sha256(canonical_recipe.encode("utf-8")).hexdigest(),
        },
        "plan": {
            "plan_id": f"statistical-plan-{plan_sha256[:16]}",
            "plan_sha256": plan_sha256,
            "definition_hash": plan["definition_hash"],
            "generator_version": plan["generator_version"],
            "sampling_method": plan["definition"].get(
                "sampling_method", "independent"
            ),
            "sample_count": plan["sample_count"],
            "corner_combination_count": point_count // plan["sample_count"],
            "point_count": point_count,
            "parameter_count": len(plan["parameter_order"]),
            "variables": plan["definition"]["variables"],
            "corner_axes": plan["definition"].get("corner_axes", []),
        },
        "execution": {
            "experiment_count": experiment_count,
            "total_run_count": point_count * experiment_count,
            "max_concurrency": execution.get("max_concurrency", 2),
            "reuse_cache": execution.get("reuse_cache", False),
        },
        "experiments": experiment_previews,
    }


def publish_study_recipe_plan(
    recipe: object,
    workspace_root: Path,
    expected_recipe_sha256: str,
    expected_plan_id: str,
) -> tuple[dict[str, object], statistical_engine.StatisticalPlanResult]:
    """Revalidate and publish exactly the plan identified by Preview."""
    preview = preview_study_recipe(recipe, workspace_root)
    if not preview.get("valid"):
        raise ValueError("study recipe is not valid")
    preview_recipe = preview["recipe"]
    preview_plan = preview["plan"]
    assert isinstance(preview_recipe, dict)
    assert isinstance(preview_plan, dict)
    if preview_recipe.get("sha256") != expected_recipe_sha256:
        raise ValueError("recipe changed after its last valid preview")
    if preview_plan.get("plan_id") != expected_plan_id:
        raise ValueError("resolved plan changed after its last valid preview")
    assert isinstance(recipe, dict)
    definition = recipe["plan"]
    assert isinstance(definition, dict)
    plan = statistical_engine.build_statistical_plan(
        definition["variables"],  # type: ignore[arg-type]
        definition["sample_count"],  # type: ignore[arg-type]
        definition["seed"],  # type: ignore[arg-type]
        definition.get("correlations"),  # type: ignore[arg-type]
        definition.get("corner_axes"),  # type: ignore[arg-type]
        definition.get("corner_aggregate", False),  # type: ignore[arg-type]
        source_root=workspace_root,
        sampling_method=definition.get("sampling_method", "independent"),  # type: ignore[arg-type]
    )
    published = statistical_engine.save_statistical_plan(
        workspace_root / "runs", plan
    )
    if published["plan_id"] != expected_plan_id:
        raise ValueError("published plan does not match the previewed plan")
    return preview, published


def load_recipe_experiments(
    recipe: object,
    workspace_root: Path,
) -> list[dict[str, object]]:
    """Return validated experiment definitions with confined netlist text."""
    preview = preview_study_recipe(recipe, workspace_root)
    if not preview.get("valid"):
        raise ValueError("study recipe is not valid")
    assert isinstance(recipe, dict)
    experiments = recipe["experiments"]
    assert isinstance(experiments, list)
    resolved: list[dict[str, object]] = []
    for index, experiment in enumerate(experiments):
        assert isinstance(experiment, dict)
        path, error = _confined_file(
            workspace_root,
            experiment["netlist_path"],
            f"experiments[{index}].netlist_path",
        )
        if error is not None or path is None:
            raise ValueError("experiment netlist is no longer available")
        try:
            netlist_template = decode_text(path.read_bytes())
        except (OSError, UnicodeError) as exc:
            raise ValueError("experiment netlist is no longer available") from exc
        resolved.append(
            {
                "name": experiment["name"],
                "filename": experiment["filename"],
                "netlist_template": netlist_template,
                "waveform_analyses": experiment["waveform_analyses"],
            }
        )
    return resolved


def list_netlist_files(workspace_root: Path, *, maximum: int = 250) -> list[str]:
    """List bounded workspace .cir/.net netlist files without following links."""
    if not 1 <= maximum <= 1_000:
        raise ValueError("netlist file limit must be between 1 and 1,000")
    root = workspace_root.resolve(strict=True)
    files: list[str] = []
    skipped_directories = {".git", ".venv", "__pycache__", "node_modules", "runs"}
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in skipped_directories
            and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() not in {".cir", ".net"}:
                continue
            files.append(path.relative_to(root).as_posix())
            if len(files) >= maximum:
                return files
    return files


def read_netlist_text(workspace_root: Path, relative_path: object) -> str:
    """Read and decode a workspace-confined .cir/.net file's text content."""
    path, error = _confined_file(workspace_root, relative_path, "netlist_path")
    if error is not None or path is None:
        raise ValueError(error["message"] if error else "netlist was not found")
    try:
        return decode_text(path.read_bytes())
    except (OSError, UnicodeError) as exc:
        raise ValueError("netlist must be readable UTF-8 or UTF-16 text") from exc


def write_netlist_text(workspace_root: Path, relative_path: object, content: str) -> None:
    """Atomically overwrite a workspace-confined .cir/.net file's text content."""
    path, error = _confined_file(workspace_root, relative_path, "netlist_path")
    if error is not None or path is None:
        raise ValueError(error["message"] if error else "netlist was not found")
    if not isinstance(content, str):
        raise ValueError("netlist content must be a string")
    if len(content.encode("utf-8")) > MAX_NETLIST_BYTES:
        raise ValueError(f"netlists are limited to {MAX_NETLIST_BYTES} bytes")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
