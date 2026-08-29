#!/usr/bin/env python3
"""Local-only web entry point for LTspice System Builder."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import experiment_report
import optimization_engine
import optimization_recipe
import optimization_study
import schematic_capture
import statistical_engine
from system_builder_history import evidence_file, workspace_history
from study_recipe import (
    MAX_RECIPE_BYTES,
    load_recipe_experiments,
    load_study_recipe,
    preview_study_recipe,
    publish_study_recipe_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "system_builder_static"
EXAMPLE_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json"
EXAMPLE_OPTIMIZATION_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltopt.json"
SESSION_COOKIE = "ltspice_system_builder_session"
HOST_PATTERN = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")
FONT_ASSETS = {
    "IBMPlexSans-Regular.woff2",
    "IBMPlexSans-SemiBold.woff2",
    "IBMPlexMono-Regular.woff2",
    "IBMPlexMono-SemiBold.woff2",
}
MAX_MUTATION_BYTES = MAX_RECIPE_BYTES + 8192


def _default_manager_factory(runs_dir: Path) -> object:
    import mcp_server

    return mcp_server.ExperimentJobManager(runs_dir)


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def create_app(
    workspace_root: Path = PROJECT_ROOT,
    *,
    testing: bool = False,
    manager_factory: Callable[[Path], object] | None = None,
    schematic_capturer: Callable[[Path, object], dict[str, object]] | None = None,
) -> FastAPI:
    """Create one session-scoped loopback application."""
    workspace = workspace_root.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace_root must be a directory")
    session_token = secrets.token_urlsafe(32)
    manager_builder = manager_factory or _default_manager_factory
    capture_builder = schematic_capturer or schematic_capture.capture_schematic
    execution_manager: object | None = None
    optimization_manager: optimization_study.OptimizationStudyManager | None = None
    execution_lock = threading.Lock()
    report_lock = threading.Lock()
    postprocess_stop = threading.Event()
    postprocess_thread: threading.Thread | None = None
    postprocess_states: dict[str, dict[str, str]] = {}
    managed_jobs: set[str] = set()
    frozen_launches: dict[str, dict[str, object]] = {}
    frozen_optimization_launches: dict[str, dict[str, object]] = {}

    def get_execution_manager() -> object:
        nonlocal execution_manager
        if execution_manager is None:
            execution_manager = manager_builder(workspace / "runs")
        return execution_manager

    def get_optimization_manager() -> optimization_study.OptimizationStudyManager:
        nonlocal optimization_manager
        if optimization_manager is None:
            optimization_manager = optimization_study.OptimizationStudyManager(
                workspace / "runs", get_execution_manager()  # type: ignore[arg-type]
            )
        return optimization_manager

    def shutdown_execution_manager() -> None:
        nonlocal execution_manager
        if execution_manager is not None:
            shutdown = getattr(execution_manager, "shutdown", None)
            if callable(shutdown):
                shutdown()
            execution_manager = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        nonlocal postprocess_thread
        try:
            if not testing:
                get_execution_manager()
                discover_managed_jobs()
                postprocess_thread = threading.Thread(
                    target=postprocess_managed_jobs,
                    name="ltspice-system-builder-postprocess",
                    daemon=True,
                )
                postprocess_thread.start()
            yield
        finally:
            postprocess_stop.set()
            if postprocess_thread is not None:
                postprocess_thread.join()
            shutdown_execution_manager()

    app = FastAPI(
        title="LTspice System Builder",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def valid_host(request: Request) -> bool:
        host = request.headers.get("host", "")
        return bool(HOST_PATTERN.fullmatch(host) or (testing and host == "testserver"))

    def has_session(request: Request) -> bool:
        supplied = request.cookies.get(SESSION_COOKIE, "")
        return bool(supplied) and hmac.compare_digest(supplied, session_token)

    def authorize_read(request: Request) -> JSONResponse | None:
        if not has_session(request):
            return _json_error(401, "session_required", "open System Builder first")
        return None

    def authorize_mutation(request: Request) -> JSONResponse | None:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        host = request.headers.get("host", "")
        expected_origin = f"http://{host}"
        if request.headers.get("origin") != expected_origin:
            return _json_error(403, "origin_rejected", "request origin is not local")
        if request.headers.get("x-ltspice-system-builder") != "1":
            return _json_error(
                403,
                "request_marker_required",
                "System Builder request marker is missing",
            )
        return None

    async def read_json_body(
        request: Request,
        *,
        maximum: int = MAX_MUTATION_BYTES,
    ) -> tuple[object | None, JSONResponse | None]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return None, _json_error(
                415, "json_required", "Content-Type must be application/json"
            )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= maximum:
            return None, _json_error(
                413,
                "request_size",
                f"request must contain 1 to {maximum} bytes",
            )
        body = await request.body()
        if len(body) != content_length:
            return None, _json_error(
                400,
                "body_length_mismatch",
                "request body does not match Content-Length",
            )
        try:
            value = json.loads(
                body,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value: {constant}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, _json_error(
                400, "invalid_json", "body must be finite UTF-8 JSON"
            )
        return value, None

    def system_builder_report_context(experiment_id: str) -> dict[str, str]:
        path = evidence_file(
            workspace / "runs", f"{experiment_id}/experiment_manifest.json"
        )
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("experiment manifest exceeds the read budget")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        definition = manifest.get("definition") if isinstance(manifest, dict) else None
        point_plan = (
            definition.get("point_plan") if isinstance(definition, dict) else None
        )
        source = point_plan.get("source") if isinstance(point_plan, dict) else None
        builder = source.get("system_builder") if isinstance(source, dict) else None
        context = builder.get("report_context") if isinstance(builder, dict) else None
        if not isinstance(context, dict):
            return {}
        return {
            str(name): str(value)
            for name, value in context.items()
            if name in experiment_report.ReportContext.__annotations__
            and isinstance(value, str)
        }

    def discover_managed_jobs() -> None:
        runs = workspace / "runs"
        if not runs.is_dir() or runs.is_symlink():
            return
        for manifest_path in runs.glob("mcp-experiment-*/experiment_manifest.json"):
            experiment_id = manifest_path.parent.name
            try:
                context = system_builder_report_context(experiment_id)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            if context:
                managed_jobs.add(experiment_id)

    def build_completed_report(experiment_id: str) -> dict[str, object]:
        manager = get_execution_manager()
        snapshot = manager.snapshot(experiment_id)  # type: ignore[attr-defined]
        if snapshot["status"] != "completed":
            raise ValueError("only completed experiments can be finalized")
        with report_lock:
            postprocess_states[experiment_id] = {"state": "building"}
            context = system_builder_report_context(experiment_id)
            result = experiment_report.build_experiment_report(
                workspace / "runs",
                experiment_id,
                context or None,
            )
            postprocess_states[experiment_id] = {"state": "complete"}
        return result

    def postprocess_managed_jobs() -> None:
        while not postprocess_stop.wait(0.5):
            for experiment_id in tuple(managed_jobs):
                state = postprocess_states.get(experiment_id, {}).get("state")
                report = workspace / "runs" / experiment_id / "report.html"
                if state in {"building", "complete", "failed"}:
                    continue
                if report.is_file() and not report.is_symlink():
                    postprocess_states[experiment_id] = {"state": "complete"}
                    continue
                try:
                    snapshot = get_execution_manager().snapshot(experiment_id)  # type: ignore[attr-defined]
                    if snapshot["status"] == "completed":
                        build_completed_report(experiment_id)
                except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                    postprocess_states[experiment_id] = {
                        "state": "failed",
                        "error": str(exc),
                    }

    def job_payload(snapshot: dict[str, object]) -> dict[str, object]:
        experiment_id = str(snapshot["experiment_id"])
        report = workspace / "runs" / experiment_id / "report.html"
        postprocess = postprocess_states.get(experiment_id, {"state": "pending"})
        return {
            "experiment_id": experiment_id,
            "status": snapshot["status"],
            "point_count": snapshot["point_count"],
            "finished_points": snapshot.get("finished_points", 0),
            "running_points": snapshot.get("running_points", 0),
            "pending_points": snapshot.get("pending_points", 0),
            "passed_points": snapshot.get("passed_points", 0),
            "failed_points": snapshot.get("failed_points", 0),
            "all_passed": snapshot.get("all_passed"),
            "report_available": report.is_file() and not report.is_symlink(),
            "report_url": f"/evidence/{experiment_id}/report.html"
            if report.is_file() and not report.is_symlink()
            else None,
            "resumable": snapshot["status"] == "cancelled",
            "postprocess": postprocess,
        }

    def optimization_experiments() -> tuple[dict[str, dict[str, object]], dict[str, object], str]:
        from examples.mixed_signal_daq_study import TRANSIENT_ANALYSES
        from examples.optimize_mixed_signal_daq import AC_ANALYSES

        execution_recipe = load_study_recipe(EXAMPLE_RECIPE)
        definitions = load_recipe_experiments(execution_recipe, workspace)
        execution = execution_recipe.get("execution", {})
        if not isinstance(execution, dict):
            raise ValueError("optimization execution settings are invalid")
        experiments = {
            str(definition["name"]): {
                "netlist_template": definition["netlist_template"],
                "waveform_analyses": (
                    AC_ANALYSES
                    if definition["name"] == "ac"
                    else TRANSIENT_ANALYSES
                ),
                "filename": definition["filename"],
                "max_concurrency": execution.get("max_concurrency", 2),
                "reuse_cache": execution.get("reuse_cache", False),
            }
            for definition in definitions
        }
        artifact = json.dumps(
            experiments,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return experiments, execution, hashlib.sha256(artifact).hexdigest()

    def validate_optimization_experiments(
        recipe: object, experiments: dict[str, dict[str, object]]
    ) -> None:
        if not isinstance(recipe, dict):
            raise ValueError("optimization recipe must be an object")
        selectors = [*recipe.get("objectives", []), *recipe.get("constraints", [])]
        for selector in selectors:
            if not isinstance(selector, dict):
                raise ValueError("optimization selectors must be objects")
            experiment_name = selector.get("experiment")
            analysis_name = selector.get("analysis")
            experiment = experiments.get(str(experiment_name))
            analyses = experiment.get("waveform_analyses") if experiment else None
            available = {
                analysis.get("name")
                for analysis in analyses
                if isinstance(analysis, dict)
            } if isinstance(analyses, list) else set()
            if analysis_name not in available:
                raise ValueError(
                    f"analysis {experiment_name}.{analysis_name} is not defined by "
                    "the paired circuit study"
                )

    def optimization_job_payload(snapshot: dict[str, object]) -> dict[str, object]:
        experiments = snapshot.get("experiments", {})
        if not isinstance(experiments, dict):
            raise ValueError("optimization job experiments are invalid")
        children = []
        totals = {
            "total_runs": 0,
            "finished_points": 0,
            "running_points": 0,
            "pending_points": 0,
            "passed_points": 0,
            "failed_points": 0,
            "error_points": 0,
        }
        for name, child in sorted(experiments.items()):
            if not isinstance(child, dict):
                raise ValueError("optimization child snapshot is invalid")
            item = {
                "name": name,
                "experiment_id": child.get("experiment_id"),
                "status": child.get("status"),
                "point_count": child.get("point_count", 0),
                "finished_points": child.get("finished_points", 0),
                "running_points": child.get("running_points", 0),
                "pending_points": child.get("pending_points", 0),
                "passed_points": child.get("passed_points", 0),
                "failed_points": child.get("failed_points", 0),
                "error_points": child.get("error_points", 0),
            }
            children.append(item)
            totals["total_runs"] += int(item["point_count"])
            for key in set(totals) - {"total_runs"}:
                value = item.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += value
        plan = optimization_engine.load_optimization_plan(
            workspace / "runs", str(snapshot["plan_id"])
        )
        candidate_count = int(plan["candidate_count"])
        point_count = int(plan["point_count"])
        return {
            "optimization_job_id": snapshot["optimization_job_id"],
            "plan_id": snapshot["plan_id"],
            "status": snapshot["status"],
            "experiments": children,
            "progress": {
                **totals,
                "candidate_count": candidate_count,
                "corner_count": point_count // candidate_count,
                "point_count": point_count,
                "evaluation": (
                    "complete"
                    if snapshot.get("optimization_study_id")
                    else "failed"
                    if snapshot["status"] == "failed"
                    else "pending"
                ),
            },
            "optimization_study_id": snapshot.get("optimization_study_id"),
            "resumable": snapshot["status"] == "cancelled",
            "error": snapshot.get("error"),
        }

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not valid_host(request):
            return _json_error(400, "host_rejected", "host must be loopback-only")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/evidence/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index() -> Response:
        body = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(body)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/assets/app.css")
    def stylesheet() -> FileResponse:
        return FileResponse(STATIC_ROOT / "app.css", media_type="text/css")

    @app.get("/assets/app.js")
    def javascript() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "app.js", media_type="text/javascript; charset=utf-8"
        )

    @app.get("/assets/optimization.js")
    def optimization_javascript() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "optimization.js",
            media_type="text/javascript; charset=utf-8",
        )

    @app.get("/assets/fonts/{font_name}")
    def font(font_name: str) -> Response:
        if font_name not in FONT_ASSETS:
            return _json_error(404, "font_not_found", "font asset was not found")
        return FileResponse(
            STATIC_ROOT / "fonts" / font_name,
            media_type="font/woff2",
        )

    @app.get("/assets/daq-schematic.png")
    def schematic() -> FileResponse:
        return FileResponse(
            PROJECT_ROOT / "docs/images/mixed-signal-daq-schematic.png",
            media_type="image/png",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "local-only"}

    @app.get("/api/session")
    def session(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(
            {
                "product": "LTspice System Builder",
                "mode": "local-only",
                "remote_execution": False,
                "workspace": str(workspace),
            }
        )

    @app.get("/api/examples/mixed-signal-daq")
    def mixed_signal_daq(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(load_study_recipe(EXAMPLE_RECIPE))

    @app.get("/api/examples/mixed-signal-daq-optimization")
    def mixed_signal_daq_optimization(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        return JSONResponse(
            optimization_recipe.load_optimization_recipe(EXAMPLE_OPTIMIZATION_RECIPE)
        )

    @app.get("/api/history")
    def history(request: Request, limit: int = 12) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(workspace_history(workspace, limit=limit))
        except ValueError as exc:
            return _json_error(400, "history_limit", str(exc))

    @app.get("/api/schematic/files")
    def schematic_files(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(schematic_capture.list_schematic_files(workspace))
        except (OSError, ValueError) as exc:
            return _json_error(409, "schematic_files_failed", str(exc))

    @app.get("/api/schematic/image")
    def schematic_image(request: Request, path: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            image = schematic_capture.resolve_schematic_image(workspace, path)
        except ValueError:
            return _json_error(
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

    @app.post("/api/schematic/capture")
    async def capture_schematic_image(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or set(payload) != {"source_path"}:
            return _json_error(
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
            return _json_error(409, "schematic_capture_failed", str(exc))

    @app.get("/api/jobs/{experiment_id}")
    def job(request: Request, experiment_id: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().snapshot(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(404, "job_not_found", str(exc))

    @app.post("/api/jobs/{experiment_id}/cancel")
    def cancel_job(request: Request, experiment_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().cancel(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(409, "cancel_failed", str(exc))

    @app.post("/api/jobs/{experiment_id}/resume")
    def resume_job(request: Request, experiment_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().resume(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot), status_code=202)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(409, "resume_failed", str(exc))

    @app.post("/api/jobs/{experiment_id}/finalize")
    def finalize_job(request: Request, experiment_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            postprocess_states.pop(experiment_id, None)
            result = build_completed_report(experiment_id)
            return JSONResponse(
                {
                    "status": "complete",
                    "experiment_id": experiment_id,
                    "report_url": f"/evidence/{experiment_id}/report.html",
                    "plot_count": result["plot_count"],
                    "trace_count": result["trace_count"],
                }
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(409, "finalize_failed", str(exc))

    @app.get("/evidence/{artifact_path:path}")
    def evidence(request: Request, artifact_path: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            path = evidence_file(workspace / "runs", artifact_path)
        except ValueError:
            return _json_error(404, "evidence_not_found", "evidence file was not found")
        return FileResponse(path)

    @app.post("/api/preview")
    async def preview(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        recipe, error = await read_json_body(request, maximum=MAX_RECIPE_BYTES)
        if error is not None:
            return error
        return JSONResponse(preview_study_recipe(recipe, workspace))

    @app.post("/api/optimization/preview")
    async def preview_optimization(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        recipe, error = await read_json_body(
            request,
            maximum=optimization_recipe.MAX_OPTIMIZATION_RECIPE_BYTES,
        )
        if error is not None:
            return error
        return JSONResponse(optimization_recipe.preview_optimization_recipe(recipe))

    @app.post("/api/optimization/freeze")
    async def freeze_optimization(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(
            request,
            maximum=optimization_recipe.MAX_OPTIMIZATION_RECIPE_BYTES + 8192,
        )
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return _json_error(
                400, "invalid_optimization_freeze", "freeze request must be an object"
            )
        recipe = payload.get("recipe")
        expected_recipe_sha256 = payload.get("expected_recipe_sha256")
        expected_plan_id = payload.get("expected_plan_id")
        expected_point_count = payload.get("expected_point_count")
        expected_total_run_count = payload.get("expected_total_run_count")
        if (
            not isinstance(expected_recipe_sha256, str)
            or not isinstance(expected_plan_id, str)
            or not isinstance(expected_point_count, int)
            or isinstance(expected_point_count, bool)
            or not isinstance(expected_total_run_count, int)
            or isinstance(expected_total_run_count, bool)
        ):
            return _json_error(
                400,
                "invalid_optimization_freeze",
                "freeze requires the previewed recipe, plan, point, and run identities",
            )
        current = optimization_recipe.preview_optimization_recipe(recipe)
        if not current.get("valid"):
            return JSONResponse(current, status_code=422)
        try:
            experiments, execution, execution_sha256 = optimization_experiments()
            validate_optimization_experiments(recipe, experiments)
            preview_experiments = current["execution"]
            assert isinstance(preview_experiments, dict)
            if set(experiments) != set(preview_experiments["experiments"]):
                raise ValueError(
                    "optimization objectives do not match the paired circuit analyses"
                )
            preview_result, published = (
                optimization_recipe.publish_optimization_recipe_plan(
                    recipe,
                    workspace / "runs",
                    expected_recipe_sha256,
                    expected_plan_id,
                    expected_point_count,
                    expected_total_run_count,
                )
            )
        except (OSError, ValueError) as exc:
            return _json_error(409, "optimization_freeze_failed", str(exc))
        launch_token = secrets.token_urlsafe(32)
        with execution_lock:
            while len(frozen_optimization_launches) >= 32:
                frozen_optimization_launches.pop(
                    next(iter(frozen_optimization_launches))
                )
            frozen_optimization_launches[launch_token] = {
                "state": "ready",
                "recipe_sha256": expected_recipe_sha256,
                "plan_id": expected_plan_id,
                "point_count": expected_point_count,
                "total_run_count": expected_total_run_count,
                "execution_sha256": execution_sha256,
                "response": None,
            }
        return JSONResponse(
            {
                "status": "frozen",
                "launch_token": launch_token,
                "recipe_sha256": expected_recipe_sha256,
                "plan": {
                    "plan_id": published["plan_id"],
                    "plan_sha256": published["plan_sha256"],
                    "candidate_count": published["candidate_count"],
                    "point_count": published["point_count"],
                    "artifact": (
                        f"runs/optimization-plans/{published['plan_id']}/"
                        "optimization_plan.json"
                    ),
                },
                "execution": {
                    **preview_result["execution"],  # type: ignore[dict-item]
                    "max_concurrency": execution.get("max_concurrency", 2),
                    "reuse_cache": execution.get("reuse_cache", False),
                },
            }
        )

    @app.post("/api/optimization/start")
    async def start_optimization(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(
            request,
            maximum=optimization_recipe.MAX_OPTIMIZATION_RECIPE_BYTES + 8192,
        )
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return _json_error(
                400, "invalid_optimization_start", "start request must be an object"
            )
        launch_token = payload.get("launch_token")
        recipe = payload.get("recipe")
        confirmed_point_count = payload.get("confirmed_point_count")
        confirmed_run_count = payload.get("confirmed_run_count")
        acknowledged = payload.get("acknowledged")
        if (
            not isinstance(launch_token, str)
            or not isinstance(confirmed_point_count, int)
            or isinstance(confirmed_point_count, bool)
            or not isinstance(confirmed_run_count, int)
            or isinstance(confirmed_run_count, bool)
            or acknowledged is not True
        ):
            return _json_error(
                400,
                "invalid_optimization_start",
                "start requires the launch token, exact workload, and acknowledgement",
            )
        with execution_lock:
            frozen = frozen_optimization_launches.get(launch_token)
            if frozen is None:
                return _json_error(
                    409,
                    "optimization_freeze_required",
                    "publish a fresh immutable optimization plan before starting",
                )
            if frozen["state"] == "started":
                response = frozen["response"]
                assert isinstance(response, dict)
                return JSONResponse(response)
            if frozen["state"] != "ready":
                return _json_error(
                    409,
                    "optimization_launch_unavailable",
                    "this frozen optimization launch is already in progress or failed",
                )
            current = optimization_recipe.preview_optimization_recipe(recipe)
            if not current.get("valid"):
                return JSONResponse(current, status_code=422)
            current_recipe = current["recipe"]
            current_plan = current["plan"]
            current_execution = current["execution"]
            assert isinstance(current_recipe, dict)
            assert isinstance(current_plan, dict)
            assert isinstance(current_execution, dict)
            if (
                current_recipe.get("sha256") != frozen["recipe_sha256"]
                or current_plan.get("plan_id") != frozen["plan_id"]
            ):
                return _json_error(
                    409,
                    "frozen_optimization_changed",
                    "the recipe no longer matches the immutable optimization plan",
                )
            if (
                confirmed_point_count != frozen["point_count"]
                or confirmed_point_count != current_plan.get("point_count")
                or confirmed_run_count != frozen["total_run_count"]
                or confirmed_run_count != current_execution.get("total_run_count")
            ):
                return _json_error(
                    409,
                    "optimization_workload_changed",
                    "confirmed workload does not match the frozen optimization plan",
                )
            frozen["state"] = "starting"
            try:
                experiments, _execution, execution_sha256 = optimization_experiments()
                validate_optimization_experiments(recipe, experiments)
                if execution_sha256 != frozen["execution_sha256"]:
                    raise ValueError(
                        "paired circuit analyses changed after plan publication"
                    )
                manager = get_optimization_manager()
                defined = manager.define(str(frozen["plan_id"]), experiments)
                started = manager.start(defined["optimization_job_id"])
                response = optimization_job_payload(started)
                frozen["state"] = "started"
                frozen["response"] = response
                return JSONResponse(response, status_code=202)
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                frozen["state"] = "failed"
                return _json_error(409, "optimization_launch_failed", str(exc))

    @app.get("/api/optimization/jobs")
    def optimization_jobs(request: Request, limit: int = 8) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        if not isinstance(limit, int) or not 1 <= limit <= 32:
            return _json_error(400, "optimization_job_limit", "limit must be 1 to 32")
        root = workspace / "runs" / "optimization-jobs"
        if not root.is_dir() or root.is_symlink():
            return JSONResponse({"jobs": []})
        paths = sorted(
            (
                path
                for path in root.glob("optimization-job-*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:limit]
        jobs = []
        for path in paths:
            try:
                jobs.append(
                    optimization_job_payload(
                        get_optimization_manager().snapshot(path.name)
                    )
                )
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
        return JSONResponse({"jobs": jobs})

    @app.get("/api/optimization/jobs/{optimization_job_id}")
    def optimization_job(request: Request, optimization_job_id: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                optimization_job_payload(
                    get_optimization_manager().snapshot(optimization_job_id)
                )
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(404, "optimization_job_not_found", str(exc))

    @app.post("/api/optimization/jobs/{optimization_job_id}/cancel")
    def cancel_optimization_job(
        request: Request, optimization_job_id: str
    ) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(
                optimization_job_payload(
                    get_optimization_manager().cancel(optimization_job_id)
                )
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(409, "optimization_cancel_failed", str(exc))

    @app.post("/api/optimization/jobs/{optimization_job_id}/resume")
    def resume_optimization_job(
        request: Request, optimization_job_id: str
    ) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_optimization_manager().resume(optimization_job_id)
            return JSONResponse(optimization_job_payload(snapshot), status_code=202)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return _json_error(409, "optimization_resume_failed", str(exc))

    @app.post("/api/freeze")
    async def freeze(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return _json_error(400, "invalid_freeze", "freeze request must be an object")
        recipe = payload.get("recipe")
        expected_recipe_sha256 = payload.get("expected_recipe_sha256")
        expected_plan_id = payload.get("expected_plan_id")
        if not isinstance(expected_recipe_sha256, str) or not isinstance(
            expected_plan_id, str
        ):
            return _json_error(
                400,
                "invalid_freeze",
                "freeze requires the previewed recipe hash and plan ID",
            )
        current = preview_study_recipe(recipe, workspace)
        if not current.get("valid"):
            return JSONResponse(current, status_code=422)
        current_recipe = current["recipe"]
        current_plan = current["plan"]
        assert isinstance(current_recipe, dict)
        assert isinstance(current_plan, dict)
        if (
            current_recipe.get("sha256") != expected_recipe_sha256
            or current_plan.get("plan_id") != expected_plan_id
        ):
            return _json_error(
                409,
                "preview_stale",
                "the recipe changed; resolve a fresh Preview before freezing",
            )
        try:
            preview_result, published = publish_study_recipe_plan(
                recipe,
                workspace,
                expected_recipe_sha256,
                expected_plan_id,
            )
        except (OSError, ValueError) as exc:
            return _json_error(409, "freeze_failed", str(exc))
        launch_token = secrets.token_urlsafe(32)
        execution = preview_result["execution"]
        assert isinstance(execution, dict)
        with execution_lock:
            while len(frozen_launches) >= 32:
                frozen_launches.pop(next(iter(frozen_launches)))
            frozen_launches[launch_token] = {
                "state": "ready",
                "recipe_sha256": expected_recipe_sha256,
                "plan_id": expected_plan_id,
                "total_run_count": execution["total_run_count"],
                "response": None,
            }
        return JSONResponse(
            {
                "status": "frozen",
                "launch_token": launch_token,
                "recipe_sha256": expected_recipe_sha256,
                "plan": {
                    "plan_id": published["plan_id"],
                    "plan_sha256": published["plan_sha256"],
                    "point_count": published["point_count"],
                    "artifact": (
                        f"runs/statistical-plans/{published['plan_id']}/"
                        "statistical_plan.json"
                    ),
                },
                "execution": execution,
            }
        )

    @app.post("/api/start")
    async def start(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return _json_error(400, "invalid_start", "start request must be an object")
        launch_token = payload.get("launch_token")
        recipe = payload.get("recipe")
        confirmed_runs = payload.get("confirmed_run_count")
        if (
            not isinstance(launch_token, str)
            or not isinstance(confirmed_runs, int)
            or isinstance(confirmed_runs, bool)
        ):
            return _json_error(
                400,
                "invalid_start",
                "start requires a launch token and confirmed integer run count",
            )
        with execution_lock:
            frozen = frozen_launches.get(launch_token)
            if frozen is None:
                return _json_error(
                    409,
                    "freeze_required",
                    "create a fresh immutable plan before starting",
                )
            if frozen["state"] == "started":
                response = frozen["response"]
                assert isinstance(response, dict)
                return JSONResponse(response)
            if frozen["state"] != "ready":
                return _json_error(
                    409,
                    "launch_unavailable",
                    "this frozen launch is already in progress or failed",
                )
            current = preview_study_recipe(recipe, workspace)
            if not current.get("valid"):
                return JSONResponse(current, status_code=422)
            current_recipe = current["recipe"]
            current_plan = current["plan"]
            current_execution = current["execution"]
            assert isinstance(current_recipe, dict)
            assert isinstance(current_plan, dict)
            assert isinstance(current_execution, dict)
            if (
                current_recipe.get("sha256") != frozen["recipe_sha256"]
                or current_plan.get("plan_id") != frozen["plan_id"]
            ):
                return _json_error(
                    409,
                    "frozen_recipe_changed",
                    "the recipe no longer matches the immutable plan",
                )
            if (
                confirmed_runs != frozen["total_run_count"]
                or confirmed_runs != current_execution.get("total_run_count")
            ):
                return _json_error(
                    409,
                    "run_count_changed",
                    "confirmed run count does not match the frozen workload",
                )
            frozen["state"] = "starting"
            try:
                plan_id = str(frozen["plan_id"])
                plan = statistical_engine.load_statistical_plan(
                    workspace / "runs", plan_id
                )
                plan_result = statistical_engine.inspect_statistical_plan(
                    workspace / "runs", plan_id
                )
                from mcp_server import _statistical_plan_source

                source = _statistical_plan_source(plan_id, plan, plan_result)
                manager = get_execution_manager()
                experiments = load_recipe_experiments(recipe, workspace)
                snapshots: list[dict[str, object]] = []
                execution_definition = recipe.get("execution", {})
                assert isinstance(execution_definition, dict)
                for experiment in experiments:
                    report_context = dict(recipe.get("report_context", {}))
                    report_context.setdefault(
                        "simulation_summary",
                        f"The {experiment['name']} analysis evaluates every immutable "
                        "statistical point and named operating corner in this recipe.",
                    )
                    report_context.setdefault(
                        "mcp_context",
                        "System Builder uses the same immutable plan, durable execution, "
                        "waveform analysis, and portable evidence contracts as the MCP.",
                    )
                    experiment_source = dict(source)
                    experiment_source["system_builder"] = {
                        "recipe_sha256": frozen["recipe_sha256"],
                        "experiment_name": experiment["name"],
                        "report_context": report_context,
                    }
                    snapshot = manager.define_explicit(  # type: ignore[attr-defined]
                        experiment["netlist_template"],
                        plan["parameter_order"],
                        [point["parameters"] for point in plan["points"]],
                        plan["parameter_units"],
                        experiment_source,
                        experiment["waveform_analyses"],
                        experiment["filename"],
                        False,
                        120,
                        execution_definition.get("max_concurrency", 2),
                        execution_definition.get("reuse_cache", False),
                    )
                    snapshots.append(snapshot)
                    managed_jobs.add(str(snapshot["experiment_id"]))
                started = [
                    manager.start(str(snapshot["experiment_id"]))  # type: ignore[attr-defined]
                    for snapshot in snapshots
                ]
                response = {
                    "status": "queued",
                    "plan_id": plan_id,
                    "total_run_count": confirmed_runs,
                    "experiments": [
                        {
                            "name": definition["name"],
                            "experiment_id": snapshot["experiment_id"],
                            "status": snapshot["status"],
                            "point_count": snapshot["point_count"],
                        }
                        for definition, snapshot in zip(experiments, started)
                    ],
                }
                frozen["state"] = "started"
                frozen["response"] = response
                return JSONResponse(response, status_code=202)
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                frozen["state"] = "failed"
                return _json_error(409, "launch_failed", str(exc))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT,
        help="allowed circuit workspace (defaults to this project)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start without opening the default browser",
    )
    args = parser.parse_args()

    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    app = create_app(args.workspace)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"LTspice System Builder: {url}")
    print(f"Workspace: {args.workspace.resolve()}")
    try:
        uvicorn.Server(
            uvicorn.Config(app, log_level="info", access_log=False)
        ).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
