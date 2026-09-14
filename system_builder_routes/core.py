"""Static, session, example, history, and evidence routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import experiment_engine
import experiment_index
import frequency_domain_metrics
import ltspice_wrapper
import optimization_recipe
import waveform_browser
import waveform_metrics
from study_recipe import load_study_recipe, resolve_netlist_path
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

    @router.post("/api/netlist/run")
    async def quick_run(request: Request) -> Response:
        """Simulate one workspace netlist once, without defining a study.

        Every other path to LTspice goes through define, preview, freeze, and
        an acknowledgement, which is right for a qualification run and heavy
        for "does this deck even simulate?". This writes an ordinary run
        directory under runs/, so its output is inspectable in the waveform
        viewer like any other.
        """
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(400, "invalid_quick_run", "request must be an object")
        timeout = payload.get("timeout_seconds", 120)
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 3600
        ):
            return json_error(
                400, "invalid_quick_run", "timeout_seconds must be 1 to 3600"
            )
        try:
            netlist_path = resolve_netlist_path(workspace, payload.get("netlist_path"))
        except ValueError as exc:
            return json_error(400, "invalid_quick_run", str(exc))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        output_dir = workspace / "runs" / f"quick-{stamp}"
        try:
            result_dir = ltspice_wrapper.run_netlist(
                netlist_path,
                output_dir=output_dir,
                timeout_seconds=timeout,
                disable_compression=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "quick_run_failed", str(exc))
        runs_root = (workspace / "runs").resolve()
        manifest_path = result_dir / "run_manifest.json"
        manifest: dict[str, object] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
        captures = [
            {
                "filename": raw.name,
                "path": raw.resolve().relative_to(runs_root).as_posix(),
                "size_bytes": raw.stat().st_size,
            }
            for raw in sorted(result_dir.glob("*.raw"))
            if raw.is_file() and not raw.is_symlink()
        ]
        return JSONResponse(
            {
                "run_id": result_dir.name,
                "status": manifest.get("status", "unknown"),
                "duration_seconds": manifest.get("duration_seconds"),
                "netlist_path": payload.get("netlist_path"),
                "captures": captures,
            }
        )

    @router.get("/api/experiments/query")
    def experiment_query(
        request: Request,
        limit: int = 25,
        offset: int = 0,
        status: str | None = None,
        execution_mode: str | None = None,
        all_passed: bool | None = None,
        statistical: bool | None = None,
        requirement_metric: str | None = None,
    ) -> Response:
        """Filter the derived experiment index. Never rebuilds it."""
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                experiment_index.query_experiments(
                    workspace / "runs",
                    limit=limit,
                    offset=offset,
                    status=status or None,
                    execution_mode=execution_mode or None,
                    all_passed=all_passed,
                    statistical=statistical,
                    requirement_metric=requirement_metric or None,
                )
            )
        except (FileNotFoundError, ValueError) as exc:
            return json_error(400, "query_failed", str(exc))

    @router.post("/api/compare")
    async def compare(request: Request) -> Response:
        """Diff two finished experiments, writing a portable comparison artifact."""
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(400, "invalid_comparison", "request must be an object")
        baseline = payload.get("baseline_experiment_id")
        candidate = payload.get("candidate_experiment_id")
        if not isinstance(baseline, str) or not isinstance(candidate, str):
            return json_error(
                400,
                "invalid_comparison",
                "baseline_experiment_id and candidate_experiment_id are required",
            )
        if baseline == candidate:
            return json_error(
                400, "invalid_comparison", "choose two different experiments"
            )
        try:
            result = experiment_engine.compare_experiments(
                workspace / "runs", baseline, candidate
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            return json_error(409, "comparison_failed", str(exc))
        runs_root = (workspace / "runs").resolve()
        markdown = Path(str(result["comparison_markdown"])).resolve()
        return JSONResponse(
            {
                **result,
                "report_url": f"/evidence/{markdown.relative_to(runs_root).as_posix()}",
            }
        )

    @router.get("/api/runs/{experiment_id}/captures")
    def run_captures(request: Request, experiment_id: str) -> Response:
        """List the .raw waveforms a finished experiment wrote."""
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                waveform_browser.list_run_captures(workspace / "runs", experiment_id)
            )
        except ValueError as exc:
            return json_error(404, "captures_not_found", str(exc))

    @router.get("/api/waveform")
    def waveform(
        request: Request,
        path: str,
        variables: str | None = None,
        max_points: int = 1200,
    ) -> Response:
        """Return one capture's vectors, downsampled for plotting."""
        denied = authorize_read(request)
        if denied is not None:
            return denied
        selected = (
            [name for name in variables.split(",") if name] if variables else None
        )
        try:
            return JSONResponse(
                waveform_browser.read_capture(
                    workspace / "runs",
                    path,
                    variables=selected,
                    max_points=max_points,
                )
            )
        except ValueError as exc:
            return json_error(400, "waveform_unavailable", str(exc))

    @router.get("/api/waveform.csv")
    def waveform_csv(request: Request, path: str) -> Response:
        """Export one capture at full resolution as CSV."""
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            raw_path = evidence_file(workspace / "runs", path)
            body = waveform_browser.capture_csv(raw_path)
        except ValueError as exc:
            return json_error(400, "waveform_unavailable", str(exc))
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{raw_path.stem}.csv"'
                )
            },
        )

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
