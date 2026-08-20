#!/usr/bin/env python3
"""Submit the transient example to the local API and poll it to completion."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent.parent
NETLIST = PROJECT_DIR / "examples" / "transient_rc.cir"
BASE_URL = "http://127.0.0.1:8765"


def request_json(url: str, method: str = "GET", payload: object | None = None) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    submitted = request_json(
        f"{BASE_URL}/simulate/async",
        method="POST",
        payload={"filename": NETLIST.name, "netlist": NETLIST.read_text(), "ascii": True, "timeout": 120},
    )
    job_id = str(submitted["job_id"])
    print(f"Submitted {job_id}")
    for attempt in range(1, 31):
        job = request_json(f"{BASE_URL}{submitted['poll']}")
        print(f"  poll {attempt}: {job['status']}")
        if job["status"] in ("completed", "failed", "interrupted"):
            if job["status"] != "completed":
                raise SystemExit(f"Job did not complete: {job}")
            print(json.dumps(job["result"], indent=2))
            return
        time.sleep(min(0.25 * attempt, 2.0))
    raise TimeoutError(f"Job did not finish after 30 polls: {job_id}")


if __name__ == "__main__":
    main()
