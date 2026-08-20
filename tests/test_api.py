from __future__ import annotations

import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api_server import create_server


class ApiTests(unittest.TestCase):
    def test_health_and_simulate(self) -> None:
        server = create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base_url}/health", timeout=10) as response:
                self.assertEqual(json.load(response)["status"], "ok")

            netlist = """* API test
V1 in 0 AC 1
R1 in out 10k
C1 out 0 1u
.ac dec 10 10 1Meg
.meas ac gain_at_1k FIND mag(V(out)) AT=1k
.end
"""
            request = Request(
                f"{base_url}/simulate",
                data=json.dumps({"filename": "api_test.cir", "netlist": netlist}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
            self.assertIn("gain_at_1k", result["measurements"])
            self.assertTrue(any(path.endswith(".raw") for path in result["artifacts"]))
            self.assertTrue(any(path.endswith("run_manifest.json") for path in result["artifacts"]))

            async_request = Request(
                f"{base_url}/simulate/async",
                data=json.dumps({"filename": "async_api_test.cir", "netlist": netlist}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(async_request, timeout=10) as response:
                queued = json.load(response)
            self.assertEqual(queued["status"], "queued")
            for _ in range(100):
                with urlopen(f"{base_url}{queued['poll']}", timeout=10) as response:
                    job = json.load(response)
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "completed")
            self.assertIn("gain_at_1k", job["result"]["measurements"])
            with urlopen(f"{base_url}/jobs", timeout=10) as response:
                jobs = json.load(response)["jobs"]
            self.assertTrue(any(item["job_id"] == queued["job_id"] for item in jobs))

            invalid_request = Request(
                f"{base_url}/simulate",
                data=json.dumps({"filename": "invalid.cir", "netlist": "not a valid deck"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as error_context:
                urlopen(invalid_request, timeout=30)
            error = error_context.exception
            self.assertEqual(error.code, 500)
            failed = json.loads(error.read())
            error.close()
            self.assertEqual(failed["status"], "failed")
            self.assertIn("error", failed)
        finally:
            server.shutdown()
            server.job_manager.shutdown()
            server.server_close()
            thread.join(timeout=5)

        restarted = create_server(port=0)
        restarted_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
        restarted_thread.start()
        restarted_url = f"http://127.0.0.1:{restarted.server_address[1]}"
        try:
            with urlopen(f"{restarted_url}/jobs/{queued['job_id']}", timeout=10) as response:
                persisted = json.load(response)
            self.assertEqual(persisted["status"], "completed")
            self.assertIn("gain_at_1k", persisted["result"]["measurements"])
        finally:
            restarted.shutdown()
            restarted.job_manager.shutdown()
            restarted.server_close()
            restarted_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
