"""Non-mutating human recipe adapter for deterministic optimization plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import optimization_engine


MAX_OPTIMIZATION_RECIPE_BYTES = 256 * 1024
RECIPE_FIELDS = {
    "schema_version",
    "kind",
    "title",
    "description",
    "parameters",
    "fixed_parameters",
    "corner_axes",
    "objectives",
    "constraints",
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def load_optimization_recipe(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("optimization recipe must be a regular file")
    if path.stat().st_size > MAX_OPTIMIZATION_RECIPE_BYTES:
        raise ValueError("optimization recipe exceeds the size limit")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("optimization recipe must be finite UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("optimization recipe must be an object")
    return value


def preview_optimization_recipe(recipe: object) -> dict[str, object]:
    """Validate and resolve a recipe without publishing artifacts."""
    try:
        if not isinstance(recipe, dict):
            raise ValueError("optimization recipe must be an object")
        unknown = set(recipe) - RECIPE_FIELDS
        missing = RECIPE_FIELDS - set(recipe)
        if unknown:
            raise ValueError(f"unknown optimization recipe fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing optimization recipe fields: {sorted(missing)}")
        if recipe.get("schema_version") != 1 or recipe.get("kind") != "optimization":
            raise ValueError("unsupported optimization recipe schema or kind")
        for field in ("title", "description"):
            value = recipe.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"optimization recipe {field} is invalid")

        plan = optimization_engine.build_optimization_plan(
            recipe.get("parameters"),  # type: ignore[arg-type]
            recipe.get("objectives"),  # type: ignore[arg-type]
            recipe.get("constraints"),  # type: ignore[arg-type]
            recipe.get("fixed_parameters"),  # type: ignore[arg-type]
            recipe.get("corner_axes"),  # type: ignore[arg-type]
        )
        plan_id, plan_sha256 = optimization_engine.optimization_plan_identity(plan)
        definition = plan["definition"]
        experiments = definition["experiments"]
        assert isinstance(experiments, list)
        candidate_count = plan["candidate_count"]
        point_count = plan["point_count"]
        domain_sizes = {
            name: len(
                {
                    point["parameters"][name]
                    for point in plan["points"]
                    if name in point["parameters"]
                }
            )
            for name in (str(item["name"]) for item in definition["parameters"])
        }
        recipe_sha256 = hashlib.sha256(
            _canonical_json(recipe).encode("utf-8")
        ).hexdigest()
        return {
            "valid": True,
            "recipe": {"sha256": recipe_sha256},
            "plan": {
                "plan_id": plan_id,
                "plan_sha256": plan_sha256,
                "definition_hash": plan["definition_hash"],
                "generator_version": plan["generator_version"],
                "selection_policy": definition["selection_policy"],
                "candidate_count": candidate_count,
                "corner_count": point_count // candidate_count,
                "point_count": point_count,
                "domain_sizes": domain_sizes,
                "objective_count": len(definition["objectives"]),
                "constraint_count": len(definition["constraints"]),
            },
            "execution": {
                "experiments": experiments,
                "experiment_count": len(experiments),
                "total_run_count": point_count * len(experiments),
            },
            "limits": {
                "maximum_candidates": optimization_engine.MAX_OPTIMIZATION_CANDIDATES,
                "maximum_points": optimization_engine.MAX_OPTIMIZATION_POINTS,
            },
            "errors": [],
        }
    except (ArithmeticError, TypeError, ValueError) as exc:
        return {
            "valid": False,
            "recipe": None,
            "plan": None,
            "execution": None,
            "limits": {
                "maximum_candidates": optimization_engine.MAX_OPTIMIZATION_CANDIDATES,
                "maximum_points": optimization_engine.MAX_OPTIMIZATION_POINTS,
            },
            "errors": [
                {"path": "optimization", "code": "invalid_plan", "message": str(exc)}
            ],
        }
