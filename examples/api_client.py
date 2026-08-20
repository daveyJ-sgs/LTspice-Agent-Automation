#!/usr/bin/env python3
"""Submit the example circuit to the local LTspice REST bridge."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent.parent
NETLIST = PROJECT_DIR / "examples" / "rc_lowpass.cir"


def main() -> None:
    payload = json.dumps(
        {"filename": NETLIST.name, "netlist": NETLIST.read_text()}
    ).encode("utf-8")
    request = Request(
        "http://127.0.0.1:8765/simulate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
