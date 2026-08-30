"""Study preview, publication, launch, and experiment-job routes."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import statistical_engine
from study_recipe import (
    MAX_RECIPE_BYTES,
    load_recipe_experiments,
    preview_study_recipe,
    publish_study_recipe_plan,
)

from .common import Authorization, JsonBodyReader, json_error


def create_study_router(
    *,
    workspace: Path,
    authorize_read: Authorization,
    authorize_mutation: Authorization,
    read_json_body: JsonBodyReader,
    get_execution_manager: Callable[[], object],
    job_payload: Callable[[dict[str, object]], dict[str, object]],
    build_completed_report: Callable[[str], dict[str, object]],
    postprocess_states: dict[str, dict[str, str]],
    execution_lock: threading.Lock,
    frozen_launches: dict[str, dict[str, object]],
    managed_jobs: set[str],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/{experiment_id}")
    def job(request: Request, experiment_id: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().snapshot(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(404, "job_not_found", str(exc))

    @router.post("/api/jobs/{experiment_id}/cancel")
    def cancel_job(request: Request, experiment_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().cancel(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "cancel_failed", str(exc))

    @router.post("/api/jobs/{experiment_id}/resume")
    def resume_job(request: Request, experiment_id: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        try:
            snapshot = get_execution_manager().resume(experiment_id)  # type: ignore[attr-defined]
            return JSONResponse(job_payload(snapshot), status_code=202)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            return json_error(409, "resume_failed", str(exc))

    @router.post("/api/jobs/{experiment_id}/finalize")
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
            return json_error(409, "finalize_failed", str(exc))

    @router.post("/api/preview")
    async def preview(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        recipe, error = await read_json_body(request, maximum=MAX_RECIPE_BYTES)
        if error is not None:
            return error
        return JSONResponse(preview_study_recipe(recipe, workspace))

    @router.post("/api/freeze")
    async def freeze(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(400, "invalid_freeze", "freeze request must be an object")
        recipe = payload.get("recipe")
        expected_recipe_sha256 = payload.get("expected_recipe_sha256")
        expected_plan_id = payload.get("expected_plan_id")
        if not isinstance(expected_recipe_sha256, str) or not isinstance(
            expected_plan_id, str
        ):
            return json_error(
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
            return json_error(
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
            return json_error(409, "freeze_failed", str(exc))
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

    @router.post("/api/start")
    async def start(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(400, "invalid_start", "start request must be an object")
        launch_token = payload.get("launch_token")
        recipe = payload.get("recipe")
        confirmed_runs = payload.get("confirmed_run_count")
        if (
            not isinstance(launch_token, str)
            or not isinstance(confirmed_runs, int)
            or isinstance(confirmed_runs, bool)
        ):
            return json_error(
                400,
                "invalid_start",
                "start requires a launch token and confirmed integer run count",
            )
        with execution_lock:
            frozen = frozen_launches.get(launch_token)
            if frozen is None:
                return json_error(
                    409,
                    "freeze_required",
                    "create a fresh immutable plan before starting",
                )
            if frozen["state"] == "started":
                response = frozen["response"]
                assert isinstance(response, dict)
                return JSONResponse(response)
            if frozen["state"] != "ready":
                return json_error(
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
                return json_error(
                    409,
                    "frozen_recipe_changed",
                    "the recipe no longer matches the immutable plan",
                )
            if (
                confirmed_runs != frozen["total_run_count"]
                or confirmed_runs != current_execution.get("total_run_count")
            ):
                return json_error(
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
                return json_error(409, "launch_failed", str(exc))

    return router
