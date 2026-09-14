"""Study preview, publication, launch, and experiment-job routes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import local_sensitivity
import statistical_engine
from remote_execution import build_remote_preview
from study_recipe import (
    MAX_NETLIST_BYTES,
    MAX_RECIPE_BYTES,
    create_netlist_file,
    list_netlist_files,
    load_recipe_experiments,
    preview_study_recipe,
    publish_study_recipe_plan,
    read_netlist_text,
    write_netlist_text,
)

from .common import (
    JOB_ACTION_EXCEPTIONS,
    Authorization,
    JsonBodyReader,
    add_job_crud_routes,
    json_error,
    mint_launch_token,
)


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

    add_job_crud_routes(
        router,
        prefix="/api/jobs",
        id_param="experiment_id",
        authorize_read=authorize_read,
        authorize_mutation=authorize_mutation,
        manager_getter=get_execution_manager,
        payload_builder=job_payload,
        not_found_code="job_not_found",
        cancel_failed_code="cancel_failed",
        resume_failed_code="resume_failed",
    )

    @router.post("/api/sensitivity/start")
    async def start_local_sensitivity(request: Request) -> Response:
        """Run a one-at-a-time sensitivity study around one finished sample.

        The study answers "which component actually moves this margin?" for a
        design point that already has electrical evidence. It reuses the
        durable experiment manager, so it appears in history and is cancelled
        and resumed like any other job.
        """
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=4096)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(400, "invalid_sensitivity", "request must be an object")
        experiment_id = payload.get("source_experiment_id")
        point_index = payload.get("source_point_index", 0)
        step = payload.get("relative_step", 0.01)
        concurrency = payload.get("max_concurrency", 2)
        if not isinstance(experiment_id, str) or not experiment_id:
            return json_error(
                400, "invalid_sensitivity", "source_experiment_id is required"
            )
        if not isinstance(point_index, int) or isinstance(point_index, bool):
            return json_error(
                400, "invalid_sensitivity", "source_point_index must be an integer"
            )
        if not isinstance(concurrency, int) or isinstance(concurrency, bool):
            return json_error(
                400, "invalid_sensitivity", "max_concurrency must be an integer"
            )
        try:
            prepared = local_sensitivity.prepare_local_sensitivity_study(
                workspace / "runs", experiment_id, point_index, step
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            return json_error(409, "sensitivity_failed", str(exc))
        with execution_lock:
            manager = get_execution_manager()
            try:
                snapshot = manager.define_explicit(  # type: ignore[attr-defined]
                    prepared["netlist_template"],
                    prepared["parameter_order"],
                    prepared["points"],
                    prepared["parameter_units"],
                    prepared["source"],
                    prepared["waveform_analyses"],
                    prepared["filename"],
                    prepared["ascii_raw"],
                    prepared["timeout_seconds"],
                    concurrency,
                    bool(payload.get("reuse_cache", False)),
                )
                started = manager.start(str(snapshot["experiment_id"]))  # type: ignore[attr-defined]
            except JOB_ACTION_EXCEPTIONS as exc:
                return json_error(409, "sensitivity_failed", str(exc))
            managed_jobs.add(str(snapshot["experiment_id"]))
        return JSONResponse(job_payload(started), status_code=202)

    @router.get("/api/sensitivity/{experiment_id}")
    def local_sensitivity_analysis(request: Request, experiment_id: str) -> Response:
        """Return tornado effects for a finished sensitivity study."""
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            summary = local_sensitivity.analyze_local_sensitivity(
                workspace / "runs", experiment_id
            )
            tornado = json.loads(
                Path(str(summary["tornado_json"])).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
            return json_error(409, "sensitivity_unavailable", str(exc))
        runs_root = (workspace / "runs").resolve()
        return JSONResponse(
            {
                **summary,
                "analysis": tornado,
                "csv_url": (
                    "/evidence/"
                    + Path(str(summary["tornado_csv"]))
                    .resolve()
                    .relative_to(runs_root)
                    .as_posix()
                ),
            }
        )

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

    @router.get("/api/recipe/netlists")
    def netlist_files(request: Request) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse({"files": list_netlist_files(workspace)})
        except OSError as exc:
            return json_error(409, "netlist_list_failed", str(exc))

    @router.get("/api/recipe/netlist")
    def netlist_content(request: Request, path: str) -> Response:
        denied = authorize_read(request)
        if denied is not None:
            return denied
        try:
            content = read_netlist_text(workspace, path)
        except ValueError as exc:
            return json_error(404, "netlist_not_found", str(exc))
        return JSONResponse({"path": path, "content": content})

    @router.put("/api/recipe/netlist")
    async def save_netlist_content(request: Request, path: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=MAX_NETLIST_BYTES + 4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            return json_error(
                400, "invalid_netlist_save", "save requires a string content field"
            )
        try:
            write_netlist_text(workspace, path, payload["content"])
        except ValueError as exc:
            return json_error(409, "netlist_save_failed", str(exc))
        return JSONResponse({"path": path, "content": payload["content"]})

    @router.post("/api/recipe/netlist")
    async def import_netlist_content(request: Request, path: str) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request, maximum=MAX_NETLIST_BYTES + 4096)
        if error is not None:
            return error
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            return json_error(
                400, "invalid_netlist_import", "import requires a string content field"
            )
        try:
            create_netlist_file(workspace, path, payload["content"])
        except ValueError as exc:
            return json_error(409, "netlist_import_failed", str(exc))
        return JSONResponse(
            {"path": path, "content": payload["content"]}, status_code=201
        )

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
        execution = preview_result["execution"]
        assert isinstance(execution, dict)
        launch_token = mint_launch_token(
            frozen_launches,
            {
                "state": "ready",
                "recipe_sha256": expected_recipe_sha256,
                "plan_id": expected_plan_id,
                "plan_sha256": published["plan_sha256"],
                "plan_artifact": (
                    f"runs/statistical-plans/{published['plan_id']}/"
                    "statistical_plan.json"
                ),
                "point_count": published["point_count"],
                "experiment_count": execution["experiment_count"],
                "total_run_count": execution["total_run_count"],
                "response": None,
            },
            execution_lock,
        )
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

    @router.post("/api/remote/preview")
    async def remote_preview(request: Request) -> Response:
        denied = authorize_mutation(request)
        if denied is not None:
            return denied
        payload, error = await read_json_body(request)
        if error is not None:
            return error
        if not isinstance(payload, dict):
            return json_error(
                400,
                "invalid_remote_preview",
                "remote preview request must be an object",
            )
        launch_token = payload.get("launch_token")
        confirmed_plan_id = payload.get("confirmed_plan_id")
        confirmed_run_count = payload.get("confirmed_run_count")
        if (
            not isinstance(launch_token, str)
            or not isinstance(confirmed_plan_id, str)
            or isinstance(confirmed_run_count, bool)
            or not isinstance(confirmed_run_count, int)
        ):
            return json_error(
                400,
                "invalid_remote_preview",
                "remote preview requires the frozen plan identity and run count",
            )
        with execution_lock:
            frozen = frozen_launches.get(launch_token)
            if frozen is None:
                return json_error(
                    409,
                    "freeze_required",
                    "create a fresh immutable plan before remote preview",
                )
            if frozen["state"] != "ready":
                return json_error(
                    409,
                    "remote_preview_unavailable",
                    "only an unstarted frozen plan can be previewed remotely",
                )
            if (
                confirmed_plan_id != frozen["plan_id"]
                or confirmed_run_count != frozen["total_run_count"]
            ):
                return json_error(
                    409,
                    "remote_preview_changed",
                    "confirmed identity or workload does not match the frozen plan",
                )
            frozen_snapshot = dict(frozen)
        try:
            preview = build_remote_preview(
                repository=payload.get("repository"),
                ref=payload.get("ref"),
                plan_id=frozen_snapshot["plan_id"],
                plan_sha256=frozen_snapshot["plan_sha256"],
                recipe_sha256=frozen_snapshot["recipe_sha256"],
                plan_artifact=frozen_snapshot["plan_artifact"],
                point_count=frozen_snapshot["point_count"],
                experiment_count=frozen_snapshot["experiment_count"],
                total_run_count=frozen_snapshot["total_run_count"],
            )
        except ValueError as exc:
            return json_error(422, "remote_preview_invalid", str(exc))
        return JSONResponse(preview)

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
                    "kind": "study",
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
