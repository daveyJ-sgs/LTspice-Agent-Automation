import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

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

    def shutdown(self) -> None:
        self.shutdown_called = True


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
        self.assertFalse(session.json()["remote_execution"])

    def test_api_requires_the_browser_session(self) -> None:
        response = self.client.get("/api/session")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "session_required")

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
        fonts = PROJECT_ROOT / "system_builder_static/fonts"

        self.assertIn('id="theme-toggle"', html)
        self.assertIn('class="tool-intro"', html)
        self.assertIn('class="security-tooltip"', html)
        self.assertNotIn('class="security-card"', html)
        self.assertIn(':root[data-theme="light"]', css)
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
        self.assertIn('id="execution-acknowledgement"', html)
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


if __name__ == "__main__":
    unittest.main()
