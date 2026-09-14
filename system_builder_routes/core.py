"""Static, session, example, history, and evidence routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import experiment_engine
import frequency_domain_metrics
import ltspice_wrapper
import optimization_recipe
import waveform_metrics
from study_recipe import load_study_recipe
from system_builder_history import evidence_file, workspace_history

from .common import Authorization, JsonBodyReader, json_error


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
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
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

    @router.get("/api/metrics")
    def metrics(request: Request) -> Response:
        """Describe the parameters each metric accepts, for the editors.

        Served from the measurement registries rather than restated in the
        browser, so a metric gaining or losing a parameter cannot leave the
        requirement form offering a field the metric will not read.
        """
        denied = authorize_read(request)
        if denied is not None:
            return denied
        common_names = {
            parameter.name
            for parameters in (
                waveform_metrics.COMMON_PARAMETERS,
                frequency_domain_metrics.COMMON_PARAMETERS,
            )
            for parameter in parameters
        }
        return JSONResponse(
            {
                "metrics": [
                    {
                        "name": metric,
                        "domain": (
                            "frequency"
                            if metric in frequency_domain_metrics.SUPPORTED_METRICS
                            else "time"
                        ),
                        "parameters": [
                            {
                                "name": parameter.name,
                                "kind": parameter.kind,
                                "required": parameter.required,
                                "choices": list(parameter.choices),
                                "default": parameter.default,
                                "unit": parameter.unit,
                                "description": parameter.description,
                                "axis_interpolated": parameter.axis_interpolated,
                                # Shared by every metric, so editors can group
                                # these apart from the metric-specific fields.
                                "common": parameter.name in common_names,
                            }
                            for parameter in parameters
                        ],
                    }
                    for metric, parameters in experiment_engine.metric_schema().items()
                ]
            }
        )

    @router.get("/api/settings/ltspice")
    def ltspice_settings(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(ltspice_wrapper.ltspice_status())

    @router.put("/api/settings/ltspice")
    async def set_ltspice_settings(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or "executable" not in payload:
            return json_error(
                400,
                "invalid_ltspice_setting",
                "request requires an executable field (a path, or null to clear)",
            )
        executable = payload["executable"]
        if executable is not None and not isinstance(executable, str):
            return json_error(
                400,
                "invalid_ltspice_setting",
                "executable must be a string path or null",
            )
        try:
            ltspice_wrapper.set_ltspice_executable(executable)
        except ValueError as exc:
            return json_error(409, "ltspice_setting_failed", str(exc))
        return JSONResponse(ltspice_wrapper.ltspice_status())

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
