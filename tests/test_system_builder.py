import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import ltspice_wrapper
import system_builder

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeExperimentManager:
    def __init__(self) -> None:
        self.defined: list[dict[str, object]] = []
        self.started: list[str] = []
        self.shutdown_called = False
        self.jobs: dict[str, dict[str, object]] = {}

    def define_explicit(self, *args: object) -> dict[str, object]:
        experiment_id = f"mcp-experiment-test-{len(self.defined):04d}"
        point_count = len(args[2])
        snapshot = {
            "experiment_id": experiment_id,
            "status": "defined",
            "point_count": point_count,
        }
        self.defined.append({"args": args, "snapshot": snapshot})
        self.jobs[experiment_id] = snapshot
        return dict(snapshot)

    def start(self, experiment_id: str) -> dict[str, object]:
        self.started.append(experiment_id)
        definition = next(
            item["snapshot"]
            for item in self.defined
            if item["snapshot"]["experiment_id"] == experiment_id
        )
        definition["status"] = "queued"
        return dict(definition)

    def snapshot(self, experiment_id: str) -> dict[str, object]:
        if experiment_id not in self.jobs:
            raise FileNotFoundError(experiment_id)
        return dict(self.jobs[experiment_id])

    def cancel(self, experiment_id: str) -> dict[str, object]:
        self.jobs[experiment_id]["status"] = "cancelled"
        return self.snapshot(experiment_id)

    def resume(self, experiment_id: str) -> dict[str, object]:
        self.jobs[experiment_id]["status"] = "queued"
        return self.snapshot(experiment_id)

    def definition_hash(self, experiment_id: str) -> str:
        return f"definition-{experiment_id}"

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeOptimizationManager:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self._snapshot = snapshot

    def snapshot(self, _optimization_job_id: str) -> dict[str, object]:
        return dict(self._snapshot)


class FakeRemoteClient:
    def __init__(self) -> None:
        self.dispatched: list[tuple[dict[str, object], dict[str, object]]] = []
        self.record: dict[str, object] = {
            "remote_job_id": "remote-job-0123456789abcdef",
            "status": "queued",
            "state": "submitted",
            "run_id": "123",
            "run_url": "https://github.com/owner/repo/actions/runs/123",
            "evidence_available": False,
            "reports": [],
        }

    def auth_status(self) -> dict[str, object]:
        return {"available": True, "provider": "github_cli"}

    def dispatch(
        self, preview: dict[str, object], envelope: dict[str, object]
    ) -> dict[str, object]:
        self.dispatched.append((preview, envelope))
        return dict(self.record)

    def list_jobs(self) -> list[dict[str, object]]:
        return [dict(self.record)]

    def refresh(self, _remote_job_id: str) -> dict[str, object]:
        return {**self.record, "status": "in_progress", "state": "running"}

    def download(self, _remote_job_id: str) -> dict[str, object]:
        return {
            **self.record,
            "status": "completed",
            "state": "evidence_verified",
            "evidence_available": True,
        }


class SystemBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(
            system_builder.create_app(PROJECT_ROOT, testing=True),
            base_url="http://testserver",
        )

    def _open(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def _headers(self, origin: str = "http://testserver") -> dict[str, str]:
        return {
            "origin": origin,
            "x-ltspice-system-builder": "1",
        }

    def _workspace_recipe(self, root: Path) -> dict[str, object]:
        recipe = json.loads(
            (PROJECT_ROOT / "examples/mixed_signal_daq.ltstudy.json").read_text(
                encoding="utf-8"
            )
        )
        for experiment in recipe["experiments"]:
            source = PROJECT_ROOT / experiment["netlist_path"]
            target = root / source.name
            shutil.copyfile(source, target)
            experiment["netlist_path"] = source.name
        return recipe

    def _copy_optimization_netlists(self, root: Path) -> None:
        examples = root / "examples"
        examples.mkdir()
        for name in ("mixed_signal_daq_ac.cir", "mixed_signal_daq_transient.cir"):
            shutil.copyfile(PROJECT_ROOT / "examples" / name, examples / name)

    def test_route_inventory_is_stable(self) -> None:
        paths = self.client.app.openapi()["paths"]
        actual = {
            (method.upper(), path)
            for path, operations in paths.items()
            for method in operations
        }
        expected = {
            ("DELETE", "/api/projects/{slug}"),
            ("GET", "/"),
            ("GET", "/api/examples/mixed-signal-daq"),
            ("GET", "/api/examples/mixed-signal-daq-optimization"),
            ("GET", "/api/history"),
            ("GET", "/api/jobs/{experiment_id}"),
            ("GET", "/api/optimization/jobs"),
            ("GET", "/api/optimization/jobs/{optimization_job_id}"),
            ("GET", "/api/optimization/jobs/{optimization_job_id}/results"),
            ("GET", "/api/projects"),
            ("GET", "/api/projects/{slug}/recipe"),
            ("GET", "/api/qualification/jobs"),
            ("GET", "/api/qualification/jobs/{job_id}"),
            ("GET", "/api/qualification/jobs/{job_id}/results"),
            ("GET", "/api/recipe/netlist"),
            ("GET", "/api/recipe/netlists"),
            ("GET", "/api/remote/jobs"),
            ("GET", "/api/schematic/files"),
            ("GET", "/api/schematic/image"),
            ("GET", "/api/session"),
            ("GET", "/api/settings/ltspice"),
            ("GET", "/assets/app.css"),
            ("GET", "/assets/app.js"),
            ("GET", "/assets/daq-schematic.png"),
            ("GET", "/assets/fonts/{font_name}"),
            ("GET", "/assets/optimization.js"),
            ("GET", "/evidence/{artifact_path}"),
            ("GET", "/health"),
            ("POST", "/api/freeze"),
            ("POST", "/api/jobs/{experiment_id}/cancel"),
            ("POST", "/api/jobs/{experiment_id}/finalize"),
            ("POST", "/api/jobs/{experiment_id}/resume"),
            ("POST", "/api/optimization/freeze"),
            ("POST", "/api/optimization/jobs/{optimization_job_id}/cancel"),
            ("POST", "/api/optimization/jobs/{optimization_job_id}/resume"),
            ("POST", "/api/optimization/preview"),
            ("POST", "/api/optimization/start"),
            ("POST", "/api/preview"),
            ("POST", "/api/projects"),
            ("POST", "/api/qualification/freeze"),
            ("POST", "/api/qualification/jobs/{job_id}/cancel"),
            ("POST", "/api/qualification/jobs/{job_id}/resume"),
            ("POST", "/api/qualification/preview"),
            ("POST", "/api/qualification/start"),
            ("POST", "/api/recipe/netlist"),
            ("POST", "/api/remote/auth"),
            ("POST", "/api/remote/dispatch"),
            ("POST", "/api/remote/jobs/{remote_job_id}/download"),
            ("POST", "/api/remote/jobs/{remote_job_id}/refresh"),
            ("POST", "/api/remote/preview"),
            ("POST", "/api/schematic/capture"),
            ("POST", "/api/start"),
            ("PUT", "/api/projects/{slug}/recipe"),
            ("PUT", "/api/recipe/netlist"),
            ("PUT", "/api/settings/ltspice"),
        }
        self.assertEqual(actual, expected)

    def test_root_establishes_a_local_session_and_security_headers(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("LTspice System Builder", response.text)
        self.assertIn(system_builder.SESSION_COOKIE, response.cookies)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        session = self.client.get("/api/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["mode"], "local-only")
        self.assertTrue(session.json()["remote_execution"])
        self.assertEqual(session.json()["remote_default"], "disabled")

    def test_api_requires_the_browser_session(self) -> None:
        response = self.client.get("/api/session")
        optimization = self.client.get(
            "/api/optimization/jobs/optimization-job-0123456789abcdef/results"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "session_required")
        self.assertEqual(optimization.status_code, 401)

    def test_non_loopback_host_is_rejected(self) -> None:
        client = TestClient(
            system_builder.create_app(PROJECT_ROOT, testing=True),
            base_url="http://example.com",
        )
        response = client.get("/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "host_rejected")

    def test_preview_rejects_cross_origin_and_unmarked_requests(self) -> None:
        self._open()
        recipe = self.client.get("/api/examples/mixed-signal-daq").json()

        cross_origin = self.client.post(
            "/api/preview",
            json=recipe,
            headers=self._headers("https://example.com"),
        )
        unmarked = self.client.post(
            "/api/preview",
            json=recipe,
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(cross_origin.json()["error"]["code"], "origin_rejected")
        self.assertEqual(unmarked.status_code, 403)
        self.assertEqual(
            unmarked.json()["error"]["code"], "request_marker_required"
        )

    def test_preview_returns_the_resolved_daq_run_count(self) -> None:
        self._open()
        recipe = self.client.get("/api/examples/mixed-signal-daq").json()
        response = self.client.post(
            "/api/preview", json=recipe, headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])
        self.assertEqual(response.json()["plan"]["point_count"], 24)
        self.assertEqual(response.json()["execution"]["total_run_count"], 48)

    def test_optimization_preview_matches_existing_phase_4_daq_plan(self) -> None:
        self._open()
        recipe = self.client.get(
            "/api/examples/mixed-signal-daq-optimization"
        ).json()

        response = self.client.post(
            "/api/optimization/preview", json=recipe, headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["valid"])
        self.assertEqual(
            result["plan"]["plan_id"], "optimization-plan-2b6f2d62d7ca7c14"
        )
        self.assertEqual(result["plan"]["candidate_count"], 16)
        self.assertEqual(result["plan"]["point_count"], 32)
        self.assertEqual(result["execution"]["total_run_count"], 64)

    def test_optimization_preview_is_guarded_and_rejects_invalid_domains(self) -> None:
        self._open()
        recipe = self.client.get(
            "/api/examples/mixed-signal-daq-optimization"
        ).json()
        recipe["parameters"][0]["values"] = [1000]

        denied = self.client.post("/api/optimization/preview", json=recipe)
        invalid = self.client.post(
            "/api/optimization/preview", json=recipe, headers=self._headers()
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(invalid.json()["valid"])

    def test_optimization_freeze_rejects_stale_workload_and_publishes_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )
            client.get("/")
            recipe = client.get(
                "/api/examples/mixed-signal-daq-optimization"
            ).json()
            preview = client.post(
                "/api/optimization/preview", json=recipe, headers=self._headers()
            ).json()
            payload = {
                "recipe": recipe,
                "expected_recipe_sha256": preview["recipe"]["sha256"],
                "expected_plan_id": preview["plan"]["plan_id"],
                "expected_point_count": preview["plan"]["point_count"],
                "expected_total_run_count": preview["execution"]["total_run_count"],
            }

            stale = client.post(
                "/api/optimization/freeze",
                json={**payload, "expected_total_run_count": 63},
                headers=self._headers(),
            )
            frozen = client.post(
                "/api/optimization/freeze", json=payload, headers=self._headers()
            )

            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                stale.json()["error"]["code"], "optimization_freeze_failed"
            )
            self.assertEqual(frozen.status_code, 200)
            self.assertEqual(frozen.json()["plan"]["point_count"], 32)
            self.assertTrue((workspace / frozen.json()["plan"]["artifact"]).is_file())
            self.assertEqual(list((workspace / "runs").glob("optimization-job-*")), [])

    def test_optimization_freeze_rejects_missing_selector_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )
            client.get("/")
            recipe = client.get(
                "/api/examples/mixed-signal-daq-optimization"
            ).json()
            preview = client.post(
                "/api/optimization/preview", json=recipe, headers=self._headers()
            ).json()
            with patch(
                "examples.optimize_mixed_signal_daq.AC_ANALYSES",
                [{"name": "wrong_ac_analysis", "signal": "V(afe)"}],
            ):
                response = client.post(
                    "/api/optimization/freeze",
                    json={
                        "recipe": recipe,
                        "expected_recipe_sha256": preview["recipe"]["sha256"],
                        "expected_plan_id": preview["plan"]["plan_id"],
                        "expected_point_count": preview["plan"]["point_count"],
                        "expected_total_run_count": preview["execution"]["total_run_count"],
                    },
                    headers=self._headers(),
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn("ac.analog_performance", response.json()["error"]["message"])
            self.assertFalse((workspace / "runs/optimization-plans").exists())

    def test_optimization_start_requires_acknowledgement_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            manager = FakeExperimentManager()
            app = system_builder.create_app(
                workspace,
                testing=True,
                manager_factory=lambda _runs: manager,
            )
            with TestClient(app, base_url="http://testserver") as client:
                client.get("/")
                recipe = client.get(
                    "/api/examples/mixed-signal-daq-optimization"
                ).json()
                preview = client.post(
                    "/api/optimization/preview",
                    json=recipe,
                    headers=self._headers(),
                ).json()
                frozen = client.post(
                    "/api/optimization/freeze",
                    json={
                        "recipe": recipe,
                        "expected_recipe_sha256": preview["recipe"]["sha256"],
                        "expected_plan_id": preview["plan"]["plan_id"],
                        "expected_point_count": preview["plan"]["point_count"],
                        "expected_total_run_count": preview["execution"]["total_run_count"],
                    },
                    headers=self._headers(),
                ).json()
                payload = {
                    "launch_token": frozen["launch_token"],
                    "recipe": recipe,
                    "confirmed_point_count": frozen["plan"]["point_count"],
                    "confirmed_run_count": frozen["execution"]["total_run_count"],
                }
                denied = client.post(
                    "/api/optimization/start",
                    json={**payload, "acknowledged": False},
                    headers=self._headers(),
                )
                started = client.post(
                    "/api/optimization/start",
                    json={**payload, "acknowledged": True},
                    headers=self._headers(),
                )
                repeated = client.post(
                    "/api/optimization/start",
                    json={**payload, "acknowledged": True},
                    headers=self._headers(),
                )

            self.assertEqual(denied.status_code, 400)
            self.assertEqual(started.status_code, 202)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(started.json(), repeated.json())
            self.assertEqual(started.json()["status"], "queued")
            self.assertEqual(
                {item["name"] for item in started.json()["experiments"]},
                {"ac", "transient"},
            )
            self.assertEqual(len(manager.defined), 2)
            self.assertEqual(len(manager.started), 2)

    def test_optimization_job_cancel_resume_and_discovery_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            manager = FakeExperimentManager()
            app = system_builder.create_app(
                workspace,
                testing=True,
                manager_factory=lambda _runs: manager,
            )
            with TestClient(app, base_url="http://testserver") as client:
                client.get("/")
                recipe = client.get(
                    "/api/examples/mixed-signal-daq-optimization"
                ).json()
                preview = client.post(
                    "/api/optimization/preview",
                    json=recipe,
                    headers=self._headers(),
                ).json()
                frozen = client.post(
                    "/api/optimization/freeze",
                    json={
                        "recipe": recipe,
                        "expected_recipe_sha256": preview["recipe"]["sha256"],
                        "expected_plan_id": preview["plan"]["plan_id"],
                        "expected_point_count": preview["plan"]["point_count"],
                        "expected_total_run_count": preview["execution"]["total_run_count"],
                    },
                    headers=self._headers(),
                ).json()
                started = client.post(
                    "/api/optimization/start",
                    json={
                        "launch_token": frozen["launch_token"],
                        "recipe": recipe,
                        "confirmed_point_count": frozen["plan"]["point_count"],
                        "confirmed_run_count": frozen["execution"]["total_run_count"],
                        "acknowledged": True,
                    },
                    headers=self._headers(),
                ).json()
                job_id = started["optimization_job_id"]
                cancelled = client.post(
                    f"/api/optimization/jobs/{job_id}/cancel",
                    headers=self._headers(),
                )
                resumed = client.post(
                    f"/api/optimization/jobs/{job_id}/resume",
                    headers=self._headers(),
                )
                discovered = client.get("/api/optimization/jobs")

            self.assertEqual(cancelled.json()["status"], "cancelled")
            self.assertTrue(cancelled.json()["resumable"])
            self.assertEqual(resumed.status_code, 202)
            self.assertEqual(resumed.json()["status"], "queued")
            self.assertEqual(discovered.json()["jobs"][0]["optimization_job_id"], job_id)

    def test_completed_optimization_results_are_sanitized_and_linked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            client = TestClient(
                system_builder.create_app(
                    workspace,
                    testing=True,
                    manager_factory=lambda _runs: FakeExperimentManager(),
                ),
                base_url="http://testserver",
            )
            client.get("/")
            recipe = client.get(
                "/api/examples/mixed-signal-daq-optimization"
            ).json()
            preview = client.post(
                "/api/optimization/preview", json=recipe, headers=self._headers()
            ).json()
            client.post(
                "/api/optimization/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": preview["recipe"]["sha256"],
                    "expected_plan_id": preview["plan"]["plan_id"],
                    "expected_point_count": preview["plan"]["point_count"],
                    "expected_total_run_count": preview["execution"]["total_run_count"],
                },
                headers=self._headers(),
            )
            study_id = "optimization-study-0123456789abcdef"
            study = workspace / "runs/optimization-studies" / study_id
            study.mkdir(parents=True)
            candidates = [
                {
                    "candidate_index": index,
                    "status": "feasible",
                    "parameters": {"CAA1": "1e-10", "ROUT": "65"},
                    "objectives": {
                        "alias_gain": {"value": -28.65, "unit": "dB"},
                        "settling_time": {"value": 1.115e-6, "unit": "s"},
                    },
                    "constraints": {},
                    "errors": [],
                    "pareto": index == 15,
                    "selected": index == 15,
                    "selection_score": 0.5 if index == 15 else None,
                    "local_path": str(workspace / "runs/private"),
                }
                for index in range(16)
            ]
            (study / "optimization_results.json").write_text(
                json.dumps(
                    {
                        "study_id": study_id,
                        "plan_id": preview["plan"]["plan_id"],
                        "selection_policy": "fixture-policy",
                        "selection_explanation": "Candidate 15 selected.",
                        "candidate_count": 16,
                        "feasible_candidates": 16,
                        "constraint_failed_candidates": 0,
                        "invalid_candidates": 0,
                        "pareto_candidates": 1,
                        "selected_candidate_index": 15,
                        "candidates": candidates,
                    }
                ),
                encoding="utf-8",
            )
            snapshot = {
                "optimization_job_id": "optimization-job-0123456789abcdef",
                "plan_id": preview["plan"]["plan_id"],
                "status": "completed",
                "optimization_study_id": study_id,
            }
            with patch(
                "system_builder.optimization_study.OptimizationStudyManager",
                return_value=FakeOptimizationManager(snapshot),
            ):
                response = client.get(
                    "/api/optimization/jobs/optimization-job-0123456789abcdef/results"
                )

            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["selected_candidate_index"], 15)
            self.assertEqual(result["candidates"][15]["parameters"]["ROUT"], "65")
            self.assertEqual(result["parameter_units"]["ROUT"], "ohm")
            self.assertNotIn("local_path", result["candidates"][15])
            self.assertEqual(
                result["evidence"]["report"],
                f"/evidence/optimization-studies/{study_id}/report.html",
            )
            self.assertNotIn(str(workspace), response.text)

    def test_optimization_results_reject_mismatched_study_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self._copy_optimization_netlists(workspace)
            client = TestClient(
                system_builder.create_app(
                    workspace,
                    testing=True,
                    manager_factory=lambda _runs: FakeExperimentManager(),
                ),
                base_url="http://testserver",
            )
            client.get("/")
            recipe = client.get(
                "/api/examples/mixed-signal-daq-optimization"
            ).json()
            preview = client.post(
                "/api/optimization/preview", json=recipe, headers=self._headers()
            ).json()
            client.post(
                "/api/optimization/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": preview["recipe"]["sha256"],
                    "expected_plan_id": preview["plan"]["plan_id"],
                    "expected_point_count": preview["plan"]["point_count"],
                    "expected_total_run_count": preview["execution"]["total_run_count"],
                },
                headers=self._headers(),
            )
            study_id = "optimization-study-0123456789abcdef"
            study = workspace / "runs/optimization-studies" / study_id
            study.mkdir(parents=True)
            (study / "optimization_results.json").write_text(
                json.dumps({"study_id": "optimization-study-wrong", "candidates": []}),
                encoding="utf-8",
            )
            snapshot = {
                "optimization_job_id": "optimization-job-0123456789abcdef",
                "plan_id": preview["plan"]["plan_id"],
                "status": "completed",
                "optimization_study_id": study_id,
            }
            with patch(
                "system_builder.optimization_study.OptimizationStudyManager",
                return_value=FakeOptimizationManager(snapshot),
            ):
                response = client.get(
                    "/api/optimization/jobs/optimization-job-0123456789abcdef/results"
                )

            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                response.json()["error"]["code"], "optimization_results_not_found"
            )

    def test_preview_accepts_gui_b1_variable_corner_and_requirement_edits(self) -> None:
        self._open()
        recipe = self.client.get("/api/examples/mixed-signal-daq").json()
        recipe["plan"]["sample_count"] = 4
        recipe["plan"]["variables"][5]["nominal"] = 55
        recipe["plan"]["corner_axes"][0]["values"][0]["value"] = 3e-11
        recipe["experiments"][0]["waveform_analyses"][0]["requirements"][0][
            "target"
        ] = 3.4

        response = self.client.post(
            "/api/preview", json=recipe, headers=self._headers()
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])
        self.assertEqual(response.json()["plan"]["point_count"], 8)
        self.assertEqual(response.json()["execution"]["total_run_count"], 16)

    def test_preview_accepts_discrete_empirical_and_correlation_edits(self) -> None:
        self._open()
        recipe = self.client.get("/api/examples/mixed-signal-daq").json()
        recipe["plan"]["variables"][0] = {
            "name": "RAA1",
            "distribution": "discrete",
            "values": ["980", "1k"],
            "weights": [1, 3],
            "nominal": "1k",
            "unit": "ohm",
        }
        recipe["plan"]["variables"][1] = {
            "name": "RAA2",
            "distribution": "empirical",
            "values": [980, 1000, 1020],
            "unit": "ohm",
        }
        recipe["plan"]["correlations"] = [recipe["plan"]["correlations"][1]]

        mixed = self.client.post(
            "/api/preview", json=recipe, headers=self._headers()
        )
        self.assertEqual(mixed.status_code, 200)
        self.assertTrue(mixed.json()["valid"])

        recipe = self.client.get("/api/examples/mixed-signal-daq").json()
        recipe["plan"]["correlations"] = [
            {
                "variables": ["RAA1", "RAA2", "GAIN"],
                "matrix": [[1, 0.5, 0.2], [0.5, 1, 0.1], [0.2, 0.1, 1]],
            },
            recipe["plan"]["correlations"][1],
        ]
        correlated = self.client.post(
            "/api/preview", json=recipe, headers=self._headers()
        )
        self.assertEqual(correlated.status_code, 200)
        self.assertTrue(correlated.json()["valid"])

    def test_freeze_rejects_stale_preview_and_publishes_only_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            recipe = self._workspace_recipe(workspace)
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )
            client.get("/")
            preview = client.post(
                "/api/preview", json=recipe, headers=self._headers()
            ).json()
            stale = client.post(
                "/api/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": "0" * 64,
                    "expected_plan_id": preview["plan"]["plan_id"],
                },
                headers=self._headers(),
            )
            frozen = client.post(
                "/api/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": preview["recipe"]["sha256"],
                    "expected_plan_id": preview["plan"]["plan_id"],
                },
                headers=self._headers(),
            )

            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["error"]["code"], "preview_stale")
            self.assertEqual(frozen.status_code, 200)
            artifact = workspace / frozen.json()["plan"]["artifact"]
            self.assertTrue(artifact.is_file())
            self.assertEqual(list((workspace / "runs").glob("mcp-experiment-*")), [])

    def test_remote_preview_is_frozen_local_and_side_effect_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            recipe = self._workspace_recipe(workspace)
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )
            client.get("/")
            preview = client.post(
                "/api/preview", json=recipe, headers=self._headers()
            ).json()
            frozen = client.post(
                "/api/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": preview["recipe"]["sha256"],
                    "expected_plan_id": preview["plan"]["plan_id"],
                },
                headers=self._headers(),
            ).json()
            before = {
                path.relative_to(workspace): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            payload = {
                "launch_token": frozen["launch_token"],
                "confirmed_plan_id": frozen["plan"]["plan_id"],
                "confirmed_run_count": frozen["execution"]["total_run_count"],
                "repository": "daveyJ-sgs/LTspice-Agent-Automation",
                "ref": "main",
            }
            with patch(
                "socket.create_connection",
                side_effect=AssertionError("remote preview attempted a network call"),
            ), patch(
                "subprocess.Popen",
                side_effect=AssertionError("remote preview attempted a process launch"),
            ):
                response = client.post(
                    "/api/remote/preview", json=payload, headers=self._headers()
                )
            after = {
                path.relative_to(workspace): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }

            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["plan"]["plan_id"], frozen["plan"]["plan_id"])
            self.assertEqual(
                result["plan"]["plan_sha256"], frozen["plan"]["plan_sha256"]
            )
            self.assertEqual(result["workload"]["total_run_count"], 48)
            self.assertFalse(result["safety"]["dispatch_enabled"])
            self.assertFalse(result["safety"]["external_request_made"])
            self.assertFalse(result["safety"]["credentials_requested"])
            self.assertEqual(before, after)

            changed = client.post(
                "/api/remote/preview",
                json={**payload, "confirmed_run_count": 47},
                headers=self._headers(),
            )
            invalid_target = client.post(
                "/api/remote/preview",
                json={**payload, "repository": "https://github.com/owner/repo"},
                headers=self._headers(),
            )
            unknown = client.post(
                "/api/remote/preview",
                json={**payload, "launch_token": "unknown"},
                headers=self._headers(),
            )

            self.assertEqual(changed.status_code, 409)
            self.assertEqual(changed.json()["error"]["code"], "remote_preview_changed")
            self.assertEqual(invalid_target.status_code, 422)
            self.assertEqual(
                invalid_target.json()["error"]["code"], "remote_preview_invalid"
            )
            self.assertEqual(unknown.status_code, 409)
            self.assertEqual(unknown.json()["error"]["code"], "freeze_required")

    def test_remote_dispatch_requires_exact_preview_and_explicit_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            recipe = self._workspace_recipe(workspace)
            remote = FakeRemoteClient()
            client = TestClient(
                system_builder.create_app(
                    workspace, testing=True, remote_client=remote
                ),
                base_url="http://testserver",
            )
            client.get("/")
            preview = client.post(
                "/api/preview", json=recipe, headers=self._headers()
            ).json()
            frozen = client.post(
                "/api/freeze",
                json={
                    "recipe": recipe,
                    "expected_recipe_sha256": preview["recipe"]["sha256"],
                    "expected_plan_id": preview["plan"]["plan_id"],
                },
                headers=self._headers(),
            ).json()
            remote_preview = client.post(
                "/api/remote/preview",
                json={
                    "launch_token": frozen["launch_token"],
                    "confirmed_plan_id": frozen["plan"]["plan_id"],
                    "confirmed_run_count": frozen["execution"]["total_run_count"],
                    "repository": "daveyJ-sgs/LTspice-Agent-Automation",
                    "ref": "main",
                },
                headers=self._headers(),
            ).json()
            payload = {
                "launch_token": frozen["launch_token"],
                "confirmed_plan_id": frozen["plan"]["plan_id"],
                "confirmed_run_count": frozen["execution"]["total_run_count"],
                "confirmed_preview_id": remote_preview["preview_id"],
                "confirmed_preview_sha256": remote_preview["preview_sha256"],
                "repository": "daveyJ-sgs/LTspice-Agent-Automation",
                "ref": "main",
                "recipe": recipe,
                "acknowledged": False,
            }

            denied = client.post(
                "/api/remote/dispatch", json=payload, headers=self._headers()
            )
            submitted = client.post(
                "/api/remote/dispatch",
                json={**payload, "acknowledged": True},
                headers=self._headers(),
            )
            repeated = client.post(
                "/api/remote/dispatch",
                json={**payload, "acknowledged": True},
                headers=self._headers(),
            )

            self.assertEqual(denied.status_code, 400)
            self.assertEqual(submitted.status_code, 202)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(len(remote.dispatched), 1)
            dispatched_preview, envelope = remote.dispatched[0]
            self.assertEqual(
                dispatched_preview["preview_sha256"], remote_preview["preview_sha256"]
            )
            self.assertLessEqual(envelope["encoded_bytes"], 24 * 1024)
            self.assertEqual(
                client.get("/api/remote/jobs").json()["jobs"][0]["run_id"], "123"
            )
            refreshed = client.post(
                "/api/remote/jobs/remote-job-0123456789abcdef/refresh",
                headers=self._headers(),
            )
            downloaded = client.post(
                "/api/remote/jobs/remote-job-0123456789abcdef/download",
                headers=self._headers(),
            )
            self.assertEqual(refreshed.json()["state"], "running")
            self.assertTrue(downloaded.json()["evidence_available"])

    def test_start_requires_exact_confirmation_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            recipe = self._workspace_recipe(workspace)
            recipe["plan"]["sample_count"] = 1
            manager = FakeExperimentManager()
            app = system_builder.create_app(
                workspace,
                testing=True,
                manager_factory=lambda _runs: manager,
            )
            with TestClient(app, base_url="http://testserver") as client:
                client.get("/")
                preview = client.post(
                    "/api/preview", json=recipe, headers=self._headers()
                ).json()
                frozen = client.post(
                    "/api/freeze",
                    json={
                        "recipe": recipe,
                        "expected_recipe_sha256": preview["recipe"]["sha256"],
                        "expected_plan_id": preview["plan"]["plan_id"],
                    },
                    headers=self._headers(),
                ).json()
                payload = {
                    "launch_token": frozen["launch_token"],
                    "recipe": recipe,
                    "confirmed_run_count": frozen["execution"]["total_run_count"],
                }
                wrong_count = client.post(
                    "/api/start",
                    json={**payload, "confirmed_run_count": 999},
                    headers=self._headers(),
                )
                started = client.post(
                    "/api/start", json=payload, headers=self._headers()
                )
                repeated = client.post(
                    "/api/start", json=payload, headers=self._headers()
                )

            self.assertEqual(wrong_count.status_code, 409)
            self.assertEqual(wrong_count.json()["error"]["code"], "run_count_changed")
            self.assertEqual(started.status_code, 202)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(started.json(), repeated.json())
            self.assertEqual(len(manager.defined), 2)
            self.assertEqual(len(manager.started), 2)
            self.assertTrue(manager.shutdown_called)

    def test_job_cancel_resume_and_finalize_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            recipe = self._workspace_recipe(workspace)
            recipe["plan"]["sample_count"] = 1
            manager = FakeExperimentManager()
            app = system_builder.create_app(
                workspace,
                testing=True,
                manager_factory=lambda _runs: manager,
            )
            with TestClient(app, base_url="http://testserver") as client:
                client.get("/")
                preview = client.post(
                    "/api/preview", json=recipe, headers=self._headers()
                ).json()
                frozen = client.post(
                    "/api/freeze",
                    json={
                        "recipe": recipe,
                        "expected_recipe_sha256": preview["recipe"]["sha256"],
                        "expected_plan_id": preview["plan"]["plan_id"],
                    },
                    headers=self._headers(),
                ).json()
                started = client.post(
                    "/api/start",
                    json={
                        "launch_token": frozen["launch_token"],
                        "recipe": recipe,
                        "confirmed_run_count": frozen["execution"]["total_run_count"],
                    },
                    headers=self._headers(),
                ).json()
                experiment_id = started["experiments"][0]["experiment_id"]
                cancelled = client.post(
                    f"/api/jobs/{experiment_id}/cancel", headers=self._headers()
                )
                resumed = client.post(
                    f"/api/jobs/{experiment_id}/resume", headers=self._headers()
                )
                inspected = client.get(f"/api/jobs/{experiment_id}")

                self.assertEqual(cancelled.json()["status"], "cancelled")
                self.assertTrue(cancelled.json()["resumable"])
                self.assertEqual(resumed.status_code, 202)
                self.assertEqual(inspected.json()["status"], "queued")

                manager.jobs[experiment_id].update(
                    status="completed",
                    finished_points=2,
                    passed_points=2,
                    failed_points=0,
                    all_passed=True,
                )
                experiment_dir = workspace / "runs" / experiment_id
                experiment_dir.mkdir()
                source = manager.defined[0]["args"][4]
                (experiment_dir / "experiment_manifest.json").write_text(
                    json.dumps(
                        {
                            "experiment_id": experiment_id,
                            "definition": {"point_plan": {"source": source}},
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(
                    system_builder.experiment_report,
                    "build_experiment_report",
                    return_value={"plot_count": 1, "trace_count": 2},
                ) as build:
                    finalized = client.post(
                        f"/api/jobs/{experiment_id}/finalize",
                        headers=self._headers(),
                    )

                self.assertEqual(finalized.status_code, 200)
                self.assertEqual(finalized.json()["plot_count"], 1)
                self.assertEqual(
                    build.call_args.args[2]["title"],
                    recipe["report_context"]["title"],
                )

    def test_completed_managed_job_is_postprocessed_without_browser_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            experiment_id = "mcp-experiment-20260828-090000-000001-deadbeef"
            experiment_dir = workspace / "runs" / experiment_id
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "experiment_manifest.json").write_text(
                json.dumps(
                    {
                        "experiment_id": experiment_id,
                        "definition": {
                            "point_plan": {
                                "source": {
                                    "kind": "statistical",
                                    "system_builder": {
                                        "report_context": {"title": "Recovered DAQ"}
                                    },
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            manager = FakeExperimentManager()
            manager.jobs[experiment_id] = {
                "experiment_id": experiment_id,
                "status": "completed",
                "point_count": 2,
                "finished_points": 2,
                "passed_points": 2,
                "failed_points": 0,
                "all_passed": True,
            }
            with patch.object(
                system_builder.experiment_report,
                "build_experiment_report",
                return_value={"plot_count": 1, "trace_count": 2},
            ) as build:
                app = system_builder.create_app(
                    workspace,
                    manager_factory=lambda _runs: manager,
                )
                with TestClient(app, base_url="http://127.0.0.1"):
                    deadline = time.monotonic() + 2
                    while build.call_count == 0 and time.monotonic() < deadline:
                        time.sleep(0.02)

            build.assert_called_once()
            self.assertEqual(build.call_args.args[2]["title"], "Recovered DAQ")
            self.assertTrue(manager.shutdown_called)

    def test_preview_rejects_non_json_and_oversized_bodies(self) -> None:
        self._open()
        wrong_type = self.client.post(
            "/api/preview",
            content="recipe",
            headers={**self._headers(), "content-type": "text/plain"},
        )
        oversized = self.client.post(
            "/api/preview",
            content=b"{" + b" " * system_builder.MAX_RECIPE_BYTES + b"}",
            headers={**self._headers(), "content-type": "application/json"},
        )
        mismatched = self.client.post(
            "/api/preview",
            content=b"{}",
            headers={
                **self._headers(),
                "content-type": "application/json",
                "content-length": "1",
            },
        )

        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(mismatched.status_code, 400)

    def test_assets_are_fixed_and_available(self) -> None:
        self._open()
        self.assertEqual(self.client.get("/assets/app.css").status_code, 200)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/optimization.js").status_code, 200)
        self.assertEqual(
            self.client.get("/assets/daq-schematic.png").status_code, 200
        )
        font = self.client.get("/assets/fonts/IBMPlexMono-Regular.woff2")
        self.assertEqual(font.status_code, 200)
        self.assertEqual(font.headers["content-type"], "font/woff2")
        self.assertEqual(
            self.client.get("/assets/fonts/../../README.md").status_code,
            404,
        )

    def test_schematic_files_capture_and_image_routes_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "design.asc"
            source.write_text("Version 4\nSHEET 1 880 680\n", encoding="utf-8")
            image = workspace / "reference.png"
            shutil.copyfile(
                PROJECT_ROOT / "docs/images/mixed-signal-daq-schematic.png",
                image,
            )

            def capture(root: Path, source_path: object) -> dict[str, object]:
                self.assertEqual(root, workspace.resolve())
                self.assertEqual(source_path, "design.asc")
                assets = root / "runs/system-builder-assets"
                assets.mkdir(parents=True)
                output = assets / "captured.png"
                shutil.copyfile(image, output)
                return {
                    "source_path": "design.asc",
                    "source_sha256": "1" * 64,
                    "schematic_path": "runs/system-builder-assets/captured.png",
                    "capture_method": "test-native-window",
                    "captured_at": "2026-08-28T12:00:00+00:00",
                    "width": 1200,
                    "height": 700,
                }

            client = TestClient(
                system_builder.create_app(
                    workspace,
                    testing=True,
                    schematic_capturer=capture,
                ),
                base_url="http://testserver",
            )
            self.assertEqual(client.get("/api/schematic/files").status_code, 401)
            client.get("/")
            files = client.get("/api/schematic/files")
            existing = client.get("/api/schematic/image?path=reference.png")
            escaped = client.get("/api/schematic/image?path=../outside.png")
            unmarked = client.post(
                "/api/schematic/capture", json={"source_path": "design.asc"}
            )
            captured = client.post(
                "/api/schematic/capture",
                json={"source_path": "design.asc"},
                headers=self._headers(),
            )
            captured_image = client.get(
                f"/api/schematic/image?path={captured.json()['schematic_path']}"
            )

            self.assertEqual(files.status_code, 200)
            self.assertEqual(files.json()["sources"], ["design.asc"])
            self.assertEqual(existing.status_code, 200)
            self.assertEqual(existing.headers["content-type"], "image/png")
            self.assertEqual(escaped.status_code, 404)
            self.assertEqual(unmarked.status_code, 403)
            self.assertEqual(captured.status_code, 200)
            self.assertEqual(captured.json()["capture_method"], "test-native-window")
            self.assertEqual(captured_image.status_code, 200)

    def test_engineering_theme_is_offline_and_gradient_free(self) -> None:
        html = (PROJECT_ROOT / "system_builder_static/index.html").read_text(
            encoding="utf-8"
        )
        css = (PROJECT_ROOT / "system_builder_static/app.css").read_text(
            encoding="utf-8"
        )
        javascript = (PROJECT_ROOT / "system_builder_static/app.js").read_text(
            encoding="utf-8"
        )
        optimization_javascript = (
            PROJECT_ROOT / "system_builder_static/optimization.js"
        ).read_text(encoding="utf-8")
        fonts = PROJECT_ROOT / "system_builder_static/fonts"

        self.assertIn('id="theme-select"', html)
        self.assertIn('class="tool-intro"', html)
        self.assertIn('class="security-tooltip"', html)
        self.assertNotIn('class="security-card"', html)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn(':root[data-theme="wiregrid"]', css)
        self.assertIn('font-family: "IBM Plex Mono"', css)
        self.assertNotIn("gradient(", css.lower())
        self.assertNotIn("https://", css)
        self.assertIn("ltspice-system-builder-theme", javascript)
        self.assertIn('id="add-variable"', html)
        self.assertIn('id="add-corner"', html)
        self.assertIn('id="requirements"', html)
        self.assertIn("schedulePreview", javascript)
        self.assertIn("renderScopedErrors", javascript)
        self.assertIn('fetch("/api/freeze"', javascript)
        self.assertIn('fetch("/api/remote/preview"', javascript)
        self.assertIn('fetch("/api/remote/dispatch"', javascript)
        self.assertIn('fetch("/api/remote/auth"', javascript)
        self.assertIn('fetch("/api/start"', javascript)
        self.assertIn('mutateJob(job.experiment_id, "finalize")', javascript)
        self.assertIn('mutateJob(job.experiment_id, "cancel")', javascript)
        self.assertIn('mutateJob(job.experiment_id, "resume")', javascript)
        self.assertIn('"discrete", "Discrete"', javascript)
        self.assertIn('"empirical", "Empirical"', javascript)
        self.assertIn('id="correlations"', html)
        self.assertIn('id="capture-schematic"', html)
        self.assertIn('id="schematic-source-path"', html)
        self.assertIn('fetch("/api/schematic/capture"', javascript)
        self.assertIn('"Ω"', javascript)
        self.assertIn('"MΩ"', javascript)
        self.assertIn('"pF"', javascript)
        self.assertIn("toPrecision(15)", javascript)
        self.assertIn("function studyUnitFactor", javascript)
        self.assertNotIn("function unitFactor(", javascript)
        self.assertIn('id="execution-acknowledgement"', html)
        self.assertIn('id="remote-preview-controls"', html)
        self.assertIn('id="remote-repository"', html)
        self.assertIn('id="remote-ref"', html)
        self.assertIn('id="remote-acknowledgement"', html)
        self.assertIn('id="remote-dispatch-button"', html)
        self.assertIn('id="remote-jobs-panel"', html)
        self.assertIn('id="optimization-domains"', html)
        self.assertIn('id="optimization-file"', html)
        self.assertIn('id="optimization-save"', html)
        self.assertIn('id="optimization-objectives"', html)
        self.assertIn('id="optimization-constraints"', html)
        self.assertIn('id="optimization-plan-id"', html)
        self.assertIn('fetch("/api/optimization/preview"', optimization_javascript)
        self.assertIn('id="optimization-freeze"', html)
        self.assertIn('id="optimization-acknowledgement"', html)
        self.assertIn('id="optimization-job"', html)
        self.assertIn('id="optimization-results"', html)
        self.assertIn('id="optimization-pareto-plot"', html)
        self.assertIn('id="optimization-selected-constraints"', html)
        self.assertIn('id="optimization-candidate-rows"', html)
        self.assertIn('fetch("/api/optimization/freeze"', optimization_javascript)
        self.assertIn('fetch("/api/optimization/start"', optimization_javascript)
        self.assertIn("recoverOptimizationJob", optimization_javascript)
        self.assertIn("renderOptimizationResults", optimization_javascript)
        self.assertIn("optimizationEngineeringValue", optimization_javascript)
        self.assertIn(
            '"preferred_series", "Generated E-series"', optimization_javascript
        )
        self.assertNotIn("GUI-C1 is preview-only", html)
        self.assertTrue((fonts / "LICENSE.txt").is_file())
        for name in system_builder.FONT_ASSETS:
            self.assertEqual((fonts / name).read_bytes()[:4], b"wOF2")

    def test_history_and_evidence_require_session_and_remain_confined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            experiment_id = "mcp-experiment-20260827-201100-123456"
            experiment = workspace / "runs" / experiment_id
            experiment.mkdir(parents=True)
            (experiment / "experiment_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "experiment_id": experiment_id,
                        "status": "completed",
                        "point_count": 1,
                        "finished_points": 1,
                        "completed_points": 1,
                    }
                ),
                encoding="utf-8",
            )
            (experiment / "report.html").write_text(
                "<style>body{color:white}</style><h1>Report</h1>", encoding="utf-8"
            )
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )

            self.assertEqual(client.get("/api/history").status_code, 401)
            client.get("/")
            history = client.get("/api/history")
            report = client.get(f"/evidence/{experiment_id}/report.html")
            escaped = client.get("/evidence/%2E%2E/secret.txt")

            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()["summary"]["total_jobs"], 1)
            self.assertEqual(report.status_code, 200)
            self.assertIn(
                "script-src 'unsafe-inline'",
                report.headers["content-security-policy"],
            )
            self.assertEqual(escaped.status_code, 404)

    def test_history_limit_is_bounded(self) -> None:
        self._open()
        response = self.client.get("/api/history?limit=1000")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "history_limit")

    def test_projects_routes_are_authorized_listed_created_and_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "existing").mkdir()
            (workspace / "existing" / "existing.ltstudy.json").write_text(
                json.dumps({"name": "Existing", "description": "d", "kind": "statistical"})
            )
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )

            self.assertEqual(client.get("/api/projects").status_code, 401)
            client.get("/")

            listing = client.get("/api/projects").json()
            self.assertEqual([p["slug"] for p in listing["projects"]], ["existing"])

            denied_create = client.post("/api/projects", json={"name": "New One"})
            created = client.post(
                "/api/projects", json={"name": "New One"}, headers=self._headers()
            )
            duplicate = client.post(
                "/api/projects", json={"name": "new one"}, headers=self._headers()
            )
            bad_name = client.post(
                "/api/projects", json={"name": ""}, headers=self._headers()
            )

            self.assertEqual(denied_create.status_code, 403)
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["project"]["slug"], "new-one")
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(duplicate.json()["error"]["code"], "project_exists")
            self.assertEqual(bad_name.status_code, 400)

            recipe = client.get("/api/projects/existing/recipe")
            missing = client.get("/api/projects/missing-project/recipe")

            self.assertEqual(recipe.status_code, 200)
            self.assertEqual(recipe.json()["name"], "Existing")
            self.assertEqual(missing.status_code, 404)

            edited = dict(recipe.json())
            edited["description"] = "edited via the study panel"
            denied_save = client.put("/api/projects/existing/recipe", json=edited)
            self.assertEqual(denied_save.status_code, 403)
            self.assertEqual(
                json.loads(
                    (workspace / "existing" / "existing.ltstudy.json").read_text()
                )["description"],
                "d",
            )

            saved = client.put(
                "/api/projects/existing/recipe", json=edited, headers=self._headers()
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(
                json.loads(
                    (workspace / "existing" / "existing.ltstudy.json").read_text()
                )["description"],
                "edited via the study panel",
            )
            reread = client.get("/api/projects/existing/recipe")
            self.assertEqual(reread.json()["description"], "edited via the study panel")

            missing_save = client.put(
                "/api/projects/missing-project/recipe",
                json=edited,
                headers=self._headers(),
            )
            self.assertEqual(missing_save.status_code, 404)

            not_object_save = client.put(
                "/api/projects/existing/recipe", json="not an object", headers=self._headers()
            )
            self.assertEqual(not_object_save.status_code, 409)

            denied_delete = client.delete("/api/projects/existing")
            self.assertEqual(denied_delete.status_code, 403)
            self.assertTrue((workspace / "existing").is_dir())

            deleted = client.delete("/api/projects/existing", headers=self._headers())
            self.assertEqual(deleted.status_code, 204)
            self.assertFalse((workspace / "existing").exists())

            missing_delete = client.delete(
                "/api/projects/existing", headers=self._headers()
            )
            self.assertEqual(missing_delete.status_code, 404)

            reserved_delete = client.delete("/api/projects/runs", headers=self._headers())
            self.assertEqual(reserved_delete.status_code, 409)

    def test_netlist_files_route_is_authorized_and_lists_workspace_netlists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "sensor").mkdir()
            (workspace / "sensor" / "sensor.cir").write_text(".end\n")
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )

            self.assertEqual(client.get("/api/recipe/netlists").status_code, 401)
            client.get("/")

            listed = client.get("/api/recipe/netlists")

            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["files"], ["sensor/sensor.cir"])

    def test_netlist_content_route_reads_writes_and_stays_confined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "sensor").mkdir()
            # newline="" pins the exact bytes on disk regardless of platform --
            # otherwise Path.write_text() silently turns \n into \r\n on
            # Windows, and read_netlist_text() (by design) reads raw bytes
            # with no newline translation, so the assertion below would only
            # fail on the Windows CI runner.
            (workspace / "sensor" / "sensor.cir").write_text("R1 in out 1000\n.end\n", newline="")
            (workspace / "outside.cir").write_text("R1 in out 1\n.end\n", newline="")
            client = TestClient(
                system_builder.create_app(workspace, testing=True),
                base_url="http://testserver",
            )

            self.assertEqual(
                client.get("/api/recipe/netlist", params={"path": "sensor/sensor.cir"}).status_code,
                401,
            )
            client.get("/")

            read = client.get("/api/recipe/netlist", params={"path": "sensor/sensor.cir"})
            self.assertEqual(read.status_code, 200)
            self.assertEqual(read.json()["content"], "R1 in out 1000\n.end\n")

            missing = client.get("/api/recipe/netlist", params={"path": "sensor/missing.cir"})
            self.assertEqual(missing.status_code, 404)

            escape = client.get("/api/recipe/netlist", params={"path": "../outside.cir"})
            self.assertEqual(escape.status_code, 404)

            denied_save = client.put(
                "/api/recipe/netlist",
                params={"path": "sensor/sensor.cir"},
                json={"content": "R1 in out {R_VAL}\n.end\n"},
            )
            self.assertEqual(denied_save.status_code, 403)

            saved = client.put(
                "/api/recipe/netlist",
                params={"path": "sensor/sensor.cir"},
                json={"content": "R1 in out {R_VAL}\n.end\n"},
                headers=self._headers(),
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(
                (workspace / "sensor" / "sensor.cir").read_text(),
                "R1 in out {R_VAL}\n.end\n",
            )

            bad_save = client.put(
                "/api/recipe/netlist",
                params={"path": "../outside.cir"},
                json={"content": "anything"},
                headers=self._headers(),
            )
            self.assertEqual(bad_save.status_code, 409)
            self.assertEqual((workspace / "outside.cir").read_text(), "R1 in out 1\n.end\n")

            denied_import = client.post(
                "/api/recipe/netlist",
                params={"path": "sensor/imported.net"},
                json={"content": "R1 in out {R_VAL}\n.end\n"},
            )
            self.assertEqual(denied_import.status_code, 403)

            imported = client.post(
                "/api/recipe/netlist",
                params={"path": "sensor/imported.net"},
                json={"content": "R1 in out {R_VAL}\n.end\n"},
                headers=self._headers(),
            )
            self.assertEqual(imported.status_code, 201)
            self.assertEqual(
                (workspace / "sensor" / "imported.net").read_text(),
                "R1 in out {R_VAL}\n.end\n",
            )

            duplicate_import = client.post(
                "/api/recipe/netlist",
                params={"path": "sensor/imported.net"},
                json={"content": "anything else"},
                headers=self._headers(),
            )
            self.assertEqual(duplicate_import.status_code, 409)
            self.assertEqual(
                (workspace / "sensor" / "imported.net").read_text(),
                "R1 in out {R_VAL}\n.end\n",
            )

            escaped_import = client.post(
                "/api/recipe/netlist",
                params={"path": "../escaped.cir"},
                json={"content": "anything"},
                headers=self._headers(),
            )
            self.assertEqual(escaped_import.status_code, 409)
            self.assertFalse((workspace.parent / "escaped.cir").exists())

    def test_ltspice_settings_route_is_authorized_persists_and_rejects_missing_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            settings_path = workspace / "settings.json"
            original_ltspice = ltspice_wrapper.LTSPICE
            with patch.object(
                ltspice_wrapper, "_settings_path", return_value=settings_path
            ):
                try:
                    client = TestClient(
                        system_builder.create_app(workspace, testing=True),
                        base_url="http://testserver",
                    )

                    self.assertEqual(
                        client.get("/api/settings/ltspice").status_code, 401
                    )
                    client.get("/")

                    initial = client.get("/api/settings/ltspice")
                    self.assertEqual(initial.status_code, 200)
                    self.assertIn("executable", initial.json())
                    self.assertIn("source", initial.json())

                    with tempfile.NamedTemporaryFile(suffix=".exe") as fake:
                        denied = client.put(
                            "/api/settings/ltspice", json={"executable": fake.name}
                        )
                        self.assertEqual(denied.status_code, 403)

                        updated = client.put(
                            "/api/settings/ltspice",
                            json={"executable": fake.name},
                            headers=self._headers(),
                        )
                        self.assertEqual(updated.status_code, 200)
                        self.assertEqual(updated.json()["source"], "configured")
                        self.assertEqual(
                            Path(updated.json()["executable"]),
                            Path(fake.name).expanduser(),
                        )
                        # Applied immediately, no restart needed.
                        self.assertEqual(
                            ltspice_wrapper.LTSPICE, Path(fake.name).expanduser()
                        )

                    missing = client.put(
                        "/api/settings/ltspice",
                        json={"executable": "/definitely/not/real/LTspice.exe"},
                        headers=self._headers(),
                    )
                    self.assertEqual(missing.status_code, 409)

                    cleared = client.put(
                        "/api/settings/ltspice",
                        json={"executable": None},
                        headers=self._headers(),
                    )
                    self.assertEqual(cleared.status_code, 200)
                    self.assertEqual(cleared.json()["source"], "discovered")
                finally:
                    ltspice_wrapper.LTSPICE = original_ltspice


if __name__ == "__main__":
    unittest.main()
