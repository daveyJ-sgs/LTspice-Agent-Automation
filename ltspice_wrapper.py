#!/usr/bin/env python3
"""Small LTspice batch-simulation wrapper for macOS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _default_ltspice() -> Path:
    configured = os.environ.get("LTSPICE_EXECUTABLE")
    if configured:
        return Path(configured).expanduser()

    candidates = {
        "darwin": [Path("/Applications/LTspice.app/Contents/MacOS/LTspice")],
        "win32": [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "ADI/LTspice/LTspice.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "LTC/LTspiceXVII/XVIIx64.exe",
        ],
    }.get(sys.platform, [Path("ltspice")])
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


LTSPICE = _default_ltspice()
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_NETLIST = PROJECT_DIR / "examples" / "rc_lowpass.cir"
RUNS_DIR = PROJECT_DIR / "runs"


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def run_netlist(
    netlist_path: Path,
    output_dir: Path | None = None,
    timeout_seconds: int = 120,
    ascii_raw: bool = False,
) -> Path:
    """Run one netlist and return the directory containing LTspice outputs."""
    if not LTSPICE.is_file():
        raise FileNotFoundError(f"LTspice executable not found: {LTSPICE}")

    netlist_path = netlist_path.expanduser().resolve()
    if not netlist_path.is_file():
        raise FileNotFoundError(f"Netlist not found: {netlist_path}")

    if output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output_dir = RUNS_DIR / stamp
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    run_netlist_path = output_dir / netlist_path.name
    shutil.copy2(netlist_path, run_netlist_path)
    manifest_path = output_dir / "run_manifest.json"
    started_at = datetime.now().astimezone()
    started_clock = time.monotonic()

    command = [str(LTSPICE)]
    if ascii_raw:
        command.append("-ascii")
    command.extend(["-b", str(run_netlist_path)])
    manifest: dict[str, object] = {
        "status": "running",
        "started_at": started_at.isoformat(),
        "source_netlist": str(netlist_path),
        "run_netlist": str(run_netlist_path),
        "netlist_sha256": hashlib.sha256(netlist_path.read_bytes()).hexdigest(),
        "ltspice": str(LTSPICE),
        "command": command,
        "working_directory": str(output_dir),
        "timeout_seconds": timeout_seconds,
        "ascii_raw": ascii_raw,
    }
    _write_manifest(manifest_path, manifest)

    try:
        completed = subprocess.run(
            command,
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        manifest.update(
            status="timeout",
            finished_at=datetime.now().astimezone().isoformat(),
            duration_seconds=time.monotonic() - started_clock,
            error=f"LTspice exceeded {timeout_seconds} seconds",
        )
        _write_manifest(manifest_path, manifest)
        raise RuntimeError(manifest["error"]) from exc

    if completed.returncode != 0:
        manifest.update(
            status="failed",
            finished_at=datetime.now().astimezone().isoformat(),
            duration_seconds=time.monotonic() - started_clock,
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )
        _write_manifest(manifest_path, manifest)
        details = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        )
        log_path = run_netlist_path.with_suffix(".log")
        if log_path.is_file():
            details = "\n".join(
                part for part in (details, f"LTspice log: {log_path}") if part
            )
        raise RuntimeError(
            f"LTspice failed with exit code {completed.returncode}\n{details}"
        )

    manifest.update(
        status="completed",
        finished_at=datetime.now().astimezone().isoformat(),
        duration_seconds=time.monotonic() - started_clock,
        returncode=completed.returncode,
        stdout=completed.stdout[-4000:],
        stderr=completed.stderr[-4000:],
        result_files=sorted(item.name for item in output_dir.iterdir()),
    )
    _write_manifest(manifest_path, manifest)
    return output_dir


def parse_measurements(log_path: Path) -> dict[str, float]:
    """Extract scalar .meas values from an LTspice log file."""
    raw_log = log_path.read_bytes()
    for encoding in ("utf-16le", "utf-8"):
        try:
            text = raw_log.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnicodeDecodeError("unknown", raw_log, 0, len(raw_log), "unsupported log encoding")

    measurements: dict[str, float] = {}
    pattern = re.compile(
        r"^\s*([A-Za-z_][\w]*)\s*(?::.*?=\s*|=\s*)\(?\s*"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(?:[A-Za-z°]+)?",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        measurements[match.group(1)] = float(match.group(2))
    return measurements


def _decode_log(log_path: Path) -> str:
    raw_log = log_path.read_bytes()
    for encoding in ("utf-16le", "utf-8"):
        try:
            return raw_log.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", raw_log, 0, len(raw_log), "unsupported log encoding")


def parse_stepped_measurements(log_path: Path, name: str) -> list[float]:
    """Read the step table emitted for a named .meas result."""
    text = _decode_log(log_path)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"Measurement: {name}":
            values: list[float] = []
            for row in lines[index + 1 :]:
                parts = row.split()
                if not parts or not parts[0].isdigit():
                    if values:
                        break
                    continue
                if len(parts) < 2:
                    continue
                match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", parts[1])
                if match:
                    values.append(float(match.group(0)))
            return values
    raise KeyError(f"Measurement not found: {name}")


def parse_step_values(log_path: Path, parameter: str) -> list[float]:
    """Read numeric `.step parameter=value` values from an LTspice log."""
    text = _decode_log(log_path)
    pattern = re.compile(rf"^\.step\s+{re.escape(parameter)}=([-+0-9.eE]+)\s*$", re.MULTILINE | re.IGNORECASE)
    return [float(match.group(1)) for match in pattern.finditer(text)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "netlist",
        nargs="?",
        type=Path,
        default=DEFAULT_NETLIST,
        help="Path to a .cir or .net file; defaults to the example circuit.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional directory for the generated .raw and .log files.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Maximum simulation time in seconds (default: 120).",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Ask LTspice for an ASCII raw file; useful for transient decoding.",
    )
    args = parser.parse_args()

    output_dir = run_netlist(args.netlist, args.output_dir, args.timeout, args.ascii)
    print(f"Simulation complete: {output_dir}")
    for artifact in sorted(output_dir.iterdir()):
        print(f"  {artifact.name}")
    log_path = output_dir / f"{args.netlist.stem}.log"
    if log_path.is_file():
        for name, value in parse_measurements(log_path).items():
            print(f"  {name} = {value}")


if __name__ == "__main__":
    main()
