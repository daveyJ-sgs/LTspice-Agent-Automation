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


if __name__ == "__main__":
    unittest.main()
