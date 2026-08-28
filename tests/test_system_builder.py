import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import system_builder


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
        self.assertNotIn('fetch("/api/start"', javascript)
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
