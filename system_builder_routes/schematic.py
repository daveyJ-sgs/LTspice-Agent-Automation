"""Workspace-confined schematic discovery and capture routes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import schematic_capture

from .common import Authorization, JsonBodyReader, json_error


def create_schematic_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
    capture_builder: Callable[[Path, object], dict[str, object]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/schematic/files")
    def schematic_files(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(schematic_capture.list_schematic_files(workspace))
        except (OSError, ValueError) as exc:
            return json_error(409, "schematic_files_failed", str(exc))

    @router.get("/api/schematic/image")
    def schematic_image(request: Request, path: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            image = schematic_capture.resolve_schematic_image(workspace, path)
        except ValueError:
            return json_error(
                404,
                "schematic_image_not_found",
                "schematic image was not found inside the workspace",
            )
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }[image.suffix.lower()]
        return FileResponse(image, media_type=media_type)

    @router.post("/api/schematic/capture")
    async def capture_schematic_image(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or set(payload) != {"source_path"}:
            return json_error(
                400,
                "invalid_schematic_capture",
                "capture requires exactly one source_path field",
            )
        try:
            result = await run_in_threadpool(
                capture_builder,
                workspace,
                payload["source_path"],
            )
            return JSONResponse(result)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "schematic_capture_failed", str(exc))

    return router
