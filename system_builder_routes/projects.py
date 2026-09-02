"""Project discovery and scaffolding routes, confined to the current workspace.

See project_scaffold.py's module docstring: a "project" is a first-level
workspace subdirectory containing a recipe file. Opening a *different*
workspace is not implemented yet -- ROADMAP.md GUI-D5 tracks live workspace
switching; for now, the frontend shows the launch command to relaunch
System Builder against a different --workspace.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import project_scaffold

from .common import Authorization, JsonBodyReader, json_error


def create_projects_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    def list_projects(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            projects = project_scaffold.list_projects(workspace)
        except OSError as exc:
            return json_error(409, "project_list_failed", str(exc))
        return JSONResponse({"projects": projects})

    @router.post("/api/projects")
    async def create_project(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            return json_error(
                400, "invalid_project_name", "create requires a string name field"
            )
        try:
            project = project_scaffold.create_project(workspace, payload["name"])
        except project_scaffold.ProjectExistsError as exc:
            return json_error(409, "project_exists", str(exc))
        except (OSError, ValueError) as exc:
            return json_error(400, "invalid_project_name", str(exc))
        return JSONResponse({"project": project}, status_code=201)

    @router.get("/api/projects/{slug}/recipe")
    def project_recipe(request: Request, slug: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            recipe = project_scaffold.project_recipe(workspace, slug)
        except FileNotFoundError as exc:
            return json_error(404, "project_not_found", str(exc))
        except (OSError, ValueError) as exc:
            return json_error(409, "project_recipe_invalid", str(exc))
        return JSONResponse(recipe)

    @router.put("/api/projects/{slug}/recipe")
    async def save_project_recipe(request: Request, slug: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        recipe, error = await read_json_body(request)
        if error is not None:
            return error
        try:
            summary = project_scaffold.save_project_recipe(workspace, slug, recipe)
        except FileNotFoundError as exc:
            return json_error(404, "project_not_found", str(exc))
        except (OSError, ValueError) as exc:
            return json_error(409, "project_recipe_save_failed", str(exc))
        return JSONResponse({"project": summary})

    @router.delete("/api/projects/{slug}")
    def delete_project(request: Request, slug: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            project_scaffold.delete_project(workspace, slug)
        except FileNotFoundError as exc:
            return json_error(404, "project_not_found", str(exc))
        except (OSError, ValueError) as exc:
            return json_error(409, "project_delete_failed", str(exc))
        return Response(status_code=204)

    return router
