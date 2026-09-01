"""Workspace-confined project discovery and scaffolding for System Builder.

A "project" is a first-level subdirectory of the current workspace that
contains at least one study or optimization recipe. Listing, creation, and
"opening" (loading a project's recipe into the current editor) are all
confined to the current workspace, so none of it needs a restart. Opening a
project that lives in a *different* workspace is a separate, unimplemented
feature (see ROADMAP.md GUI-D5) -- for now, that still means relaunching
System Builder with a different --workspace.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import study_recipe

RECIPE_SUFFIXES = (".ltstudy.json", ".ltopt.json")
RESERVED_NAMES = {"runs", "examples"}
MAX_PROJECT_NAME_LENGTH = 80
MAX_PROJECTS_LISTED = 200

_SLUG_DISALLOWED = re.compile(r"[^a-z0-9]+")


class ProjectExistsError(ValueError):
    """Raised when the requested project slug already exists in the workspace."""


def slugify_project_name(name: object) -> str:
    """Turn a display name into a safe, filesystem-confined directory slug."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("project name must be a non-empty string")
    if len(name) > MAX_PROJECT_NAME_LENGTH:
        raise ValueError(f"project name must be at most {MAX_PROJECT_NAME_LENGTH} characters")
    slug = _SLUG_DISALLOWED.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("project name must contain at least one letter or digit")
    if slug in RESERVED_NAMES:
        raise ValueError(f"'{slug}' is a reserved name; choose another")
    return slug


def _first_recipe_file(directory: Path) -> Path | None:
    candidates = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and any(path.name.endswith(suffix) for suffix in RECIPE_SUFFIXES)
    )
    return candidates[0] if candidates else None


def _project_summary(directory: Path, root: Path) -> dict[str, object] | None:
    recipe_file = _first_recipe_file(directory)
    if recipe_file is None:
        return None
    summary: dict[str, object] = {
        "slug": directory.name,
        "path": directory.relative_to(root).as_posix(),
        "recipe_file": recipe_file.relative_to(root).as_posix(),
        "name": directory.name,
        "description": "",
        "kind": None,
        "valid": True,
    }
    try:
        recipe = json.loads(recipe_file.read_text(encoding="utf-8"))
        if isinstance(recipe, dict):
            if isinstance(recipe.get("name"), str):
                summary["name"] = recipe["name"]
            if isinstance(recipe.get("description"), str):
                summary["description"] = recipe["description"]
            if isinstance(recipe.get("kind"), str):
                summary["kind"] = recipe["kind"]
    except (OSError, ValueError):
        summary["valid"] = False
    return summary


def _project_directory(workspace_root: Path, slug: object) -> Path:
    if (
        not isinstance(slug, str)
        or not slug
        or "/" in slug
        or "\\" in slug
        or slug in (".", "..")
    ):
        raise ValueError("project slug must be a single, plain directory name")
    root = workspace_root.resolve(strict=True)
    directory = root / slug
    if directory.is_symlink():
        raise ValueError("project must not be a symbolic link")
    try:
        resolved = directory.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise FileNotFoundError(f"project '{slug}' was not found") from exc
    if not resolved.is_dir():
        raise FileNotFoundError(f"project '{slug}' was not found")
    return resolved


def project_recipe(workspace_root: Path, slug: object) -> dict[str, object]:
    """Load a project's recipe file content, exactly as saved on disk."""
    directory = _project_directory(workspace_root, slug)
    recipe_file = _first_recipe_file(directory)
    if recipe_file is None:
        raise FileNotFoundError(f"project '{slug}' has no recipe file")
    recipe = json.loads(recipe_file.read_text(encoding="utf-8"))
    if not isinstance(recipe, dict):
        raise ValueError(f"project '{slug}' recipe is not a JSON object")
    return recipe


def list_projects(workspace_root: Path) -> list[dict[str, object]]:
    """List first-level workspace subdirectories that contain a recipe file."""
    root = workspace_root.resolve(strict=True)
    projects: list[dict[str, object]] = []
    for entry in sorted(root.iterdir()):
        if (
            not entry.is_dir()
            or entry.is_symlink()
            or entry.name.startswith(".")
            or entry.name in RESERVED_NAMES
        ):
            continue
        summary = _project_summary(entry, root)
        if summary is not None:
            projects.append(summary)
        if len(projects) >= MAX_PROJECTS_LISTED:
            break
    return projects


def _template_recipe(display_name: str) -> str:
    """Return recipe_json_text for a new, empty-shell project.

    No netlist is generated or assumed -- the recipe intentionally points at
    no file yet (Preview will say so) so the picker in the Study panel is the
    one place a real netlist gets wired in, whether that's a file exported
    from your own .asc schematic or one you write by hand. The example
    variable and requirement below just show the expected shape; replace them
    to match your own circuit's parameters and pass criteria.
    """
    recipe = {
        "schema_version": study_recipe.STUDY_RECIPE_SCHEMA_VERSION,
        "kind": "statistical",
        "name": display_name,
        "description": "New project: pick a netlist above, then replace this example variable and requirement with your own.",
        "plan": {
            "variables": [
                {
                    "name": "R_VAL",
                    "distribution": "gaussian",
                    "nominal": 1000,
                    "sigma": 10,
                    "minimum": 950,
                    "maximum": 1050,
                    "unit": "ohm",
                },
            ],
            "sample_count": 8,
            "seed": 1,
        },
        "experiments": [
            {
                "name": "ac",
                "netlist_path": "",
                "filename": "",
                "waveform_analyses": [
                    {
                        "name": "response",
                        "variable": "V(out)",
                        "secondary_variable": "V(in)",
                        "requirements": [
                            {
                                "metric": "ac_gain_db",
                                "operator": ">=",
                                "target": -0.5,
                                "frequency_value": 10,
                            },
                        ],
                    }
                ],
            }
        ],
        "execution": {"max_concurrency": 2, "reuse_cache": True},
        "report_context": {"title": display_name},
    }
    return json.dumps(recipe, indent=2) + "\n"


def create_project(workspace_root: Path, name: object) -> dict[str, object]:
    """Scaffold a new, empty-shell project subdirectory with just a recipe file.

    No netlist is created -- bring your own (e.g. exported from an .asc
    schematic) and wire it in via the netlist picker in the Study panel.
    """
    if not isinstance(name, str):
        raise ValueError("project name must be a string")
    slug = slugify_project_name(name)
    root = workspace_root.resolve(strict=True)
    project_dir = root / slug
    if project_dir.exists() or project_dir.is_symlink():
        raise ProjectExistsError(f"a project named '{slug}' already exists")

    recipe_text = _template_recipe(name.strip())
    project_dir.mkdir()
    recipe_filename = f"{slug}.ltstudy.json"
    (project_dir / recipe_filename).write_text(recipe_text, encoding="utf-8")

    summary = _project_summary(project_dir, root)
    assert summary is not None
    return summary


def delete_project(workspace_root: Path, slug: object) -> None:
    """Permanently delete a project directory and everything inside it.

    Confined the same way every other project operation is: the resolved
    directory must be a real, non-symlinked subdirectory of the workspace
    (see _project_directory). Reserved names are rejected as defense in
    depth -- the UI never lists runs/ or examples/ as a project to delete,
    but this keeps a stray or crafted request from ever reaching them.
    """
    if isinstance(slug, str) and slug in RESERVED_NAMES:
        raise ValueError(f"'{slug}' is a reserved name and cannot be deleted")
    directory = _project_directory(workspace_root, slug)
    shutil.rmtree(directory)
