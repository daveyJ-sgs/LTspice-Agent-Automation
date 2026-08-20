#!/usr/bin/env python3
"""Local REST bridge for submitting text netlists to LTspice."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist


MAX_BODY_BYTES = 2 * 1024 * 1024
INPUT_DIR = RUNS_DIR / "api-inputs"
JOB_DB = RUNS_DIR / "api_jobs.sqlite3"


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:80] or "request"


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _execute_simulation(payload: dict[str, object], run_id: str) -> dict[str, object]:
    netlist = str(payload["netlist"])
    filename = _safe_filename(str(payload.get("filename", "request.cir")))
    ascii_raw = bool(payload.get("ascii", False))
    timeout = int(payload.get("timeout", 120))
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = INPUT_DIR / f"{run_id}-{filename}"
    output_dir = RUNS_DIR / run_id
    input_path.write_text(netlist)
    run_netlist(input_path, output_dir=output_dir, timeout_seconds=timeout, ascii_raw=ascii_raw)
    log_path = output_dir / f"{input_path.stem}.log"
    measurements = parse_measurements(log_path) if log_path.is_file() else {}
    artifacts = [str(item) for item in sorted(output_dir.iterdir())]
    return {"run_id": run_id, "measurements": measurements, "artifacts": artifacts}


class JobManager:
    """Owns asynchronous simulations and exposes JSON-safe job snapshots."""

    def __init__(self, workers: int = 1, db_path: Path = JOB_DB) -> None:
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ltspice")
        self.workers = workers
        self.db_path = db_path
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, object]] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                )
                """
            )
            connection.execute(
                "UPDATE jobs SET status='interrupted', updated_at=datetime('now'), error=? WHERE status IN ('queued', 'running')",
                ("server restarted before job completed",),
            )

    def _persist(self, record: dict[str, object]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        record["updated_at"] = now
        result = record.get("result")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_id, status, created_at, updated_at, result_json, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    result_json=excluded.result_json, error=excluded.error
                """,
                (
                    record["job_id"], record["status"], record["created_at"], now,
                    json.dumps(result) if result is not None else None,
                    record.get("error"),
                ),
            )

    def submit(self, payload: dict[str, object]) -> str:
        job_id = f"job-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record: dict[str, object] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.lock:
            self.jobs[job_id] = record
            self._persist(record)
        future = self.executor.submit(self._run, job_id, payload)
        record["future"] = future
        return job_id

    def _run(self, job_id: str, payload: dict[str, object]) -> None:
        with self.lock:
            self.jobs[job_id]["status"] = "running"
            self._persist(self.jobs[job_id])
        try:
            result = _execute_simulation(payload, job_id.replace("job-", "api-", 1))
            with self.lock:
                self.jobs[job_id].update(status="completed", result=result)
                self._persist(self.jobs[job_id])
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(status="failed", error=str(exc))
                self._persist(self.jobs[job_id])

    def snapshot(self, job_id: str) -> dict[str, object] | None:
        with self.lock:
            record = self.jobs.get(job_id)
            if record is not None:
                return {key: value for key, value in record.items() if key != "future"}
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT job_id, status, created_at, updated_at, result_json, error FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    @staticmethod
    def _row_to_snapshot(row: tuple[object, ...]) -> dict[str, object]:
        job_id, status, created_at, updated_at, result_json, error = row
        snapshot: dict[str, object] = {
            "job_id": job_id, "status": status, "created_at": created_at, "updated_at": updated_at,
        }
        if result_json:
            snapshot["result"] = json.loads(str(result_json))
        if error:
            snapshot["error"] = error
        return snapshot

    def list_jobs(self) -> list[dict[str, object]]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT job_id, status, created_at, updated_at, result_json, error FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def wait(self, job_id: str) -> dict[str, object]:
        with self.lock:
            future = self.jobs[job_id]["future"]
        assert isinstance(future, Future)
        future.result()
        return self.snapshot(job_id) or {"job_id": job_id, "status": "failed"}

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)


class SimulationHandler(BaseHTTPRequestHandler):
    server_version = "LTspiceAutomation/1.1"

    @property
    def manager(self) -> JobManager:
        return self.server.job_manager  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            _json_response(self, HTTPStatus.OK, {"status": "ok", "workers": self.manager.workers})
            return
        if path == "/runs":
            runs = []
            if RUNS_DIR.is_dir():
                for directory in sorted(RUNS_DIR.glob("api-*")):
                    if directory.is_dir():
                        runs.append({"run_id": directory.name, "artifacts": sorted(item.name for item in directory.iterdir())})
            _json_response(self, HTTPStatus.OK, {"runs": runs})
            return
        if path == "/jobs":
            _json_response(self, HTTPStatus.OK, {"jobs": self.manager.list_jobs()})
            return
        if path.startswith("/jobs/"):
            job = self.manager.snapshot(path.removeprefix("/jobs/"))
            if job is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "job not found"})
            else:
                _json_response(self, HTTPStatus.OK, job)
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

    def _read_payload(self) -> dict[str, object] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            _json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body must be 1 byte to 2 MiB"})
            return None
        try:
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            netlist = payload["netlist"]
            if not isinstance(netlist, str) or not netlist.strip():
                raise ValueError("netlist must be a non-empty string")
            timeout = int(payload.get("timeout", 120))
            if timeout < 1 or timeout > 3600:
                raise ValueError("timeout must be between 1 and 3600 seconds")
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return None

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/simulate", "/simulate/async"):
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        payload = self._read_payload()
        if payload is None:
            return

        job_id = self.manager.submit(payload)
        if path == "/simulate/async":
            _json_response(self, HTTPStatus.ACCEPTED, {"job_id": job_id, "status": "queued", "poll": f"/jobs/{job_id}"})
            return

        job = self.manager.wait(job_id)
        if job.get("status") == "completed":
            _json_response(self, HTTPStatus.OK, job["result"])
        else:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, job)


def create_server(host: str = "127.0.0.1", port: int = 8765, workers: int = 1) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), SimulationHandler)
    server.job_manager = JobManager(workers)  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; keep the default for local-only access.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent LTspice jobs; default 1 is safest.")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    server = create_server(args.host, args.port, args.workers)
    print(f"LTspice API listening at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.job_manager.shutdown()  # type: ignore[attr-defined]
        server.server_close()


if __name__ == "__main__":
    main()
