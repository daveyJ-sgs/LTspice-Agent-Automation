"""Optimization preview, publication, launch, job, and result routes."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import optimization_recipe

from .common import Authorization, JsonBodyReader, json_error


def create_optimization_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
    optimization_experiments: Callable[..., object],
    validate_optimization_experiments: Callable[..., None],
    get_optimization_manager: Callable[[], object],
    optimization_job_payload: Callable[..., dict[str, object]],
    optimization_results_payload: Callable[..., dict[str, object]],
    execution_lock: threading.Lock,
    frozen_optimization_launches: dict[str, dict[str, object]],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/optimization/preview")
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

    @router.post("/api/optimization/freeze")
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
            return json_error(
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
            return json_error(
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
            return json_error(409, "optimization_freeze_failed", str(exc))
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

    @router.post("/api/optimization/start")
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
            return json_error(
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
            return json_error(
                400,
                "invalid_optimization_start",
                "start requires the launch token, exact workload, and acknowledgement",
            )
        with execution_lock:
            frozen = frozen_optimization_launches.get(launch_token)
            if frozen is None:
                return json_error(
                    409,
                    "optimization_freeze_required",
                    "publish a fresh immutable optimization plan before starting",
                )
            if frozen["state"] == "started":
                response = frozen["response"]
                assert isinstance(response, dict)
                return JSONResponse(response)
            if frozen["state"] != "ready":
                return json_error(
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
                return json_error(
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
                return json_error(
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
                return json_error(409, "optimization_launch_failed", str(exc))

    @router.get("/api/optimization/jobs")
    def optimization_jobs(request: Request, limit: int = 8) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        if not isinstance(limit, int) or not 1 <= limit <= 32:
            return json_error(400, "optimization_job_limit", "limit must be 1 to 32")
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

    @router.get("/api/optimization/jobs/{optimization_job_id}")
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
            return json_error(404, "optimization_job_not_found", str(exc))

    @router.get("/api/optimization/jobs/{optimization_job_id}/results")
    def optimization_job_results(
        request: Request, optimization_job_id: str
    ) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_optimization_manager().snapshot(optimization_job_id)
            return JSONResponse(optimization_results_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(404, "optimization_results_not_found", str(exc))

    @router.post("/api/optimization/jobs/{optimization_job_id}/cancel")
    def cancel_optimization_job(request: Request, optimization_job_id: str) -> Response:
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
            return json_error(409, "optimization_cancel_failed", str(exc))

    @router.post("/api/optimization/jobs/{optimization_job_id}/resume")
    def resume_optimization_job(request: Request, optimization_job_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_optimization_manager().resume(optimization_job_id)
            return JSONResponse(optimization_job_payload(snapshot), status_code=202)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "optimization_resume_failed", str(exc))

    return router
