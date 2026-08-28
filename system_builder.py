#!/usr/bin/env python3
"""Local-only web entry point for LTspice System Builder."""

from __future__ import annotations

import argparse
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

from system_builder_history import evidence_file, workspace_history
from study_recipe import (
    MAX_RECIPE_BYTES,
    load_recipe_experiments,
    load_study_recipe,
    preview_study_recipe,
    publish_study_recipe_plan,
)

import statistical_engine


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "system_builder_static"
EXAMPLE_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json"
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
) -> FastAPI:
    """Create one session-scoped loopback application."""
    workspace = workspace_root.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace_root must be a directory")
    session_token = secrets.token_urlsafe(32)
    manager_builder = manager_factory or _default_manager_factory
    execution_manager: object | None = None
    execution_lock = threading.Lock()
    frozen_launches: dict[str, dict[str, object]] = {}

    def get_execution_manager() -> object:
        nonlocal execution_manager
        if execution_manager is None:
            execution_manager = manager_builder(workspace / "runs")
        return execution_manager

    def shutdown_execution_manager() -> None:
        nonlocal execution_manager
        if execution_manager is not None:
            shutdown = getattr(execution_manager, "shutdown", None)
            if callable(shutdown):
                shutdown()
            execution_manager = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
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

    @app.get("/api/history")
    def history(request: Request, limit: int = 12) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(workspace_history(workspace, limit=limit))
        except ValueError as exc:
            return _json_error(400, "history_limit", str(exc))

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
                    snapshot = manager.define_explicit(  # type: ignore[attr-defined]
                        experiment["netlist_template"],
                        plan["parameter_order"],
                        [point["parameters"] for point in plan["points"]],
                        plan["parameter_units"],
                        source,
                        experiment["waveform_analyses"],
                        experiment["filename"],
                        False,
                        120,
                        execution_definition.get("max_concurrency", 2),
                        execution_definition.get("reuse_cache", False),
                    )
                    snapshots.append(snapshot)
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
