"""Static, session, example, history, and evidence routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import optimization_recipe
from study_recipe import load_study_recipe
from system_builder_history import evidence_file, workspace_history

from .common import Authorization, json_error


def create_core_router(
    *,
    workspace: Path,
    session_token: str,
    static_root: Path,
    project_root: Path,
    session_cookie: str,
    font_assets: set[str],
    example_recipe: Path,
    example_optimization_recipe: Path,
    authorize_read: Authorization,
) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def index() -> Response:
        body = (static_root / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(body)
        response.set_cookie(
            session_cookie,
            session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @router.get("/assets/app.css")
    def stylesheet() -> FileResponse:
        return FileResponse(static_root / "app.css", media_type="text/css")

    @router.get("/assets/app.js")
    def javascript() -> FileResponse:
        return FileResponse(
            static_root / "app.js", media_type="text/javascript; charset=utf-8"
        )

    @router.get("/assets/optimization.js")
    def optimization_javascript() -> FileResponse:
        return FileResponse(
            static_root / "optimization.js",
            media_type="text/javascript; charset=utf-8",
        )

    @router.get("/assets/fonts/{font_name}")
    def font(font_name: str) -> Response:
        if font_name not in font_assets:
            return json_error(404, "font_not_found", "font asset was not found")
        return FileResponse(
            static_root / "fonts" / font_name,
            media_type="font/woff2",
        )

    @router.get("/assets/daq-schematic.png")
    def schematic() -> FileResponse:
        return FileResponse(
            project_root / "docs/images/mixed-signal-daq-schematic.png",
            media_type="image/png",
        )

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "local-only"}

    @router.get("/api/session")
    def session(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(
            {
                "product": "LTspice System Builder",
                "mode": "local-only",
                "remote_execution": True,
                "remote_default": "disabled",
                "workspace": str(workspace),
            }
        )

    @router.get("/api/examples/mixed-signal-daq")
    def mixed_signal_daq(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(load_study_recipe(example_recipe))

    @router.get("/api/examples/mixed-signal-daq-optimization")
    def mixed_signal_daq_optimization(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(
            optimization_recipe.load_optimization_recipe(
                example_optimization_recipe
            )
        )

    @router.get("/api/history")
    def history(request: Request, limit: int = 12) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(workspace_history(workspace, limit=limit))
        except ValueError as exc:
            return json_error(400, "history_limit", str(exc))

    @router.get("/evidence/{artifact_path:path}")
    def evidence(request: Request, artifact_path: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            path = evidence_file(workspace / "runs", artifact_path)
        except ValueError:
            return json_error(
                404, "evidence_not_found", "evidence file was not found"
            )
        return FileResponse(path)

    return router
