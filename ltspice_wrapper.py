#!/usr/bin/env python3
"""Small cross-platform LTspice batch-simulation wrapper."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ltspice_text import decode_text


def _default_ltspice() -> Path:
    configured = os.environ.get("LTSPICE_EXECUTABLE")
    if configured:
        return Path(configured).expanduser()

    candidates = {
        "darwin": [Path("/Applications/LTspice.app/Contents/MacOS/LTspice")],
        # Windows installs land in several places. winget's default for
        # AnalogDevices.LTspice is a PER-USER install under LOCALAPPDATA, not
        # Program Files, so a Program-Files-only search misses the most common
        # scripted install. Machine-wide paths stay first so existing setups
        # keep resolving exactly as before.
        "win32": [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "ADI/LTspice/LTspice.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "LTC/LTspiceXVII/XVIIx64.exe",
            Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"))
            / "Programs/ADI/LTspice/LTspice.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "LTC/LTspiceIV/scad3.exe",
        ],
    }.get(sys.platform, [Path("ltspice")])
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


LTSPICE = _default_ltspice()
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_NETLIST = PROJECT_DIR / "examples" / "rc_lowpass.cir"
RUNS_DIR = PROJECT_DIR / "runs"
CACHE_SCHEMA_VERSION = 1
MAX_LOG_FILE_BYTES = 64 * 1024 * 1024
MAX_RUN_OUTPUT_BYTES = 512 * 1024 * 1024


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=None)
def _simulator_metadata_cached(
    executable: str,
    executable_size: int | None,
    modified_time_ns: int | None,
    changed_time_ns: int | None,
    file_id: int | None,
) -> dict[str, object]:
    path = Path(executable)
    executable_sha256 = None
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        executable_sha256 = digest.hexdigest()
    except OSError:
        pass

    version = os.environ.get("LTSPICE_VERSION")
    if version is None and sys.platform == "darwin":
        info_plist = path.parent.parent / "Info.plist"
        if info_plist.is_file():
            try:
                with info_plist.open("rb") as handle:
                    bundle = plistlib.load(handle)
                version = bundle.get("CFBundleShortVersionString") or bundle.get("CFBundleVersion")
            except (OSError, plistlib.InvalidFileException):
                pass

    return {
        "executable": str(path),
        "executable_sha256": executable_sha256,
        "executable_size_bytes": executable_size,
        "version": version,
    }


def _simulator_metadata(executable: str) -> dict[str, object]:
    try:
        status = Path(executable).stat()
        signature = (
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
            status.st_ino,
        )
    except OSError:
        signature = (None, None, None, None)
    return _simulator_metadata_cached(executable, *signature)


def _fresh_simulator_metadata(executable: str) -> dict[str, object]:
    try:
        status = Path(executable).stat()
        signature = (
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
            status.st_ino,
        )
    except OSError:
        signature = (None, None, None, None)
    return _simulator_metadata_cached.__wrapped__(executable, *signature)


def _runtime_metadata() -> dict[str, object]:
    return {
        "operating_system": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "packages": {"mcp": _package_version("mcp")},
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_artifacts(output_dir: Path, netlist_name: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(output_dir.iterdir()):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name in {"run_manifest.json", netlist_name}
        ):
            continue
        records.append(
            {
                "name": path.name,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def _result_size_bytes(output_dir: Path, netlist_name: str) -> int:
    return sum(
        path.stat().st_size
        for path in output_dir.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name not in {"run_manifest.json", netlist_name}
    )


_DEPENDENCY_DIRECTIVE = re.compile(
    r"^\s*\.(?:include|inc|lib)\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    re.IGNORECASE | re.MULTILINE,
)
_UNSUPPORTED_EXTERNAL_INPUT = re.compile(
    r"(?:\bfile\s*(?:=|\()|^\s*\.(?:loadbias|savebias|wave)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _stage_netlist(source: Path, destination: Path) -> None:
    """Stage a deck while keeping resolvable relative include paths meaningful."""
    text = source.read_text(encoding="utf-8")

    def absolute_reference(match: re.Match[str]) -> str:
        group = next(index for index in range(1, 4) if match.group(index) is not None)
        reference = match.group(group)
        assert reference is not None
        candidate = Path(reference).expanduser()
        if not candidate.is_absolute():
            candidate = source.parent / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return match.group(0)
        if not resolved.is_file() or Path(reference).is_absolute():
            return match.group(0)
        replacement = str(resolved)
        if '"' in replacement:
            raise ValueError("relative include path cannot contain a double quote")
        start, end = match.span(group)
        relative_start = start - match.start()
        relative_end = end - match.start()
        if group in {1, 2}:
            relative_start -= 1
            relative_end += 1
        return (
            match.group(0)[:relative_start]
            + f'"{replacement}"'
            + match.group(0)[relative_end:]
        )

    destination.write_text(
        _DEPENDENCY_DIRECTIVE.sub(absolute_reference, text),
        encoding="utf-8",
        newline="",
    )


def _unsupported_cache_input(text: str, source: Path) -> str | None:
    if _UNSUPPORTED_EXTERNAL_INPUT.search(text):
        return f"external or dynamic file input prevents safe cache reuse: {source}"
    model_dependent = set("ADJMOPQSUWXYZ")
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if (
            stripped
            and stripped[0] not in "*;. +"
            and stripped[0].upper() in model_dependent
        ):
            return (
                "model-dependent device prevents safe cache reuse "
                f"at {source}:{line_number}"
            )
    return None


def _simulation_dependencies(netlist_path: Path) -> tuple[list[dict[str, object]], str | None]:
    dependencies: list[dict[str, object]] = []
    visited: set[Path] = {netlist_path.resolve()}

    def collect(path: Path) -> str | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return f"dependency cannot be read as UTF-8: {path}: {exc}"
        unsupported = _unsupported_cache_input(text, path)
        if unsupported is not None:
            return unsupported
        for match in _DEPENDENCY_DIRECTIVE.finditer(text):
            reference = next(value for value in match.groups() if value is not None)
            candidate = Path(reference).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                return f"dependency cannot be resolved: {reference}"
            if not resolved.is_file():
                return f"dependency is not a regular file: {reference}"
            if resolved in visited:
                continue
            visited.add(resolved)
            try:
                dependencies.append(
                    {
                        "path": str(resolved),
                        "sha256": _sha256_file(resolved),
                        "size_bytes": resolved.stat().st_size,
                    }
                )
            except OSError as exc:
                return f"dependency cannot be fingerprinted: {resolved}: {exc}"
            problem = collect(resolved)
            if problem is not None:
                return problem
        return None

    problem = collect(netlist_path)
    dependencies.sort(key=lambda item: str(item["path"]))
    return dependencies, problem


def _simulation_cache_request(
    netlist_path: Path,
    timeout_seconds: int,
    ascii_raw: bool,
) -> tuple[str | None, dict[str, object], str | None]:
    dependencies, problem = _simulation_dependencies(netlist_path)
    simulator = _fresh_simulator_metadata(str(LTSPICE))
    if not simulator.get("executable_sha256"):
        problem = problem or "simulator executable cannot be fingerprinted"
    operating_system = _runtime_metadata()["operating_system"]
    request = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "netlist_filename": netlist_path.name,
        "netlist_sha256": _sha256_file(netlist_path),
        "timeout_seconds": timeout_seconds,
        "ascii_raw": ascii_raw,
        "simulator": simulator,
        "operating_system": operating_system,
        "dependencies": dependencies,
    }
    if problem is not None:
        return None, request, problem
    encoded = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), request, None


def _validated_cache_artifacts(
    cache_entry: Path,
    cache_key: str,
    request: dict[str, object],
) -> list[tuple[Path, str, str, int]] | None:
    manifest_path = cache_entry / "cache_manifest.json"
    try:
        if manifest_path.is_symlink() or (cache_entry / "artifacts").is_symlink():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != CACHE_SCHEMA_VERSION
            or manifest.get("status") != "completed"
            or manifest.get("cache_key") != cache_key
            or manifest.get("request") != request
            or not isinstance(manifest.get("artifacts"), list)
            or not manifest["artifacts"]
        ):
            return None
        artifacts: list[tuple[Path, str, str, int]] = []
        total_size = 0
        names: set[str] = set()
        reserved_names = {
            "run_manifest.json",
            "cache_manifest.json",
            str(request.get("netlist_filename", "")),
        }
        reserved_names = {name.casefold() for name in reserved_names}
        for record in manifest["artifacts"]:
            if not isinstance(record, dict):
                return None
            name = record.get("name")
            digest = record.get("sha256")
            size = record.get("size_bytes")
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or "/" in name
                or "\\" in name
                or not isinstance(digest, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or name.casefold() in reserved_names
            ):
                return None
            if name in names:
                return None
            names.add(name)
            total_size += size
            if total_size > MAX_RUN_OUTPUT_BYTES:
                return None
            artifact = cache_entry / "artifacts" / name
            if (
                not artifact.is_file()
                or artifact.is_symlink()
                or artifact.stat().st_size != size
                or _sha256_file(artifact) != digest
            ):
                return None
            artifacts.append((artifact, name, digest, size))
        if not any(name.lower().endswith(".log") for _, name, _, _ in artifacts):
            return None
        return artifacts
    except (OSError, json.JSONDecodeError):
        return None


def _restore_cache_entry(
    cache_entry: Path,
    cache_key: str,
    request: dict[str, object],
    output_dir: Path,
    input_is_unchanged: Callable[[], bool],
) -> bool:
    artifacts = _validated_cache_artifacts(cache_entry, cache_key, request)
    if artifacts is None:
        return False
    staging_dir = output_dir / f".cache-restore-{uuid.uuid4().hex}.tmp"
    moved: list[Path] = []
    try:
        staging_dir.mkdir()
        for source, name, digest, size in artifacts:
            staged = staging_dir / name
            shutil.copy2(source, staged)
            if staged.stat().st_size != size or _sha256_file(staged) != digest:
                return False
        if not input_is_unchanged():
            return False
        for _, name, _, _ in artifacts:
            destination = output_dir / name
            os.replace(staging_dir / name, destination)
            moved.append(destination)
        return True
    except OSError as exc:
        cleanup_error: OSError | None = None
        for destination in moved:
            try:
                destination.unlink()
            except OSError as unlink_error:
                cleanup_error = unlink_error
        if cleanup_error is not None:
            raise RuntimeError(
                "cache restore failed and partial artifacts could not be removed"
            ) from exc
        return False
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


def _publish_cache_entry(
    cache_dir: Path,
    cache_key: str,
    request: dict[str, object],
    output_dir: Path,
    netlist_name: str,
) -> bool:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_entry = cache_dir / f"simulation-{cache_key}"
    if cache_entry.exists():
        return _validated_cache_artifacts(cache_entry, cache_key, request) is not None
    temporary_entry = cache_dir / f".simulation-{cache_key}.{uuid.uuid4().hex}.tmp"
    artifact_dir = temporary_entry / "artifacts"
    artifact_dir.mkdir(parents=True)
    records: list[dict[str, object]] = []
    try:
        for source in sorted(output_dir.iterdir()):
            if (
                not source.is_file()
                or source.is_symlink()
                or source.name in {"run_manifest.json", netlist_name}
            ):
                continue
            destination = artifact_dir / source.name
            shutil.copy2(source, destination)
            records.append(
                {
                    "name": source.name,
                    "sha256": _sha256_file(destination),
                    "size_bytes": destination.stat().st_size,
                }
            )
        if not any(str(record["name"]).lower().endswith(".log") for record in records):
            return False
        _write_manifest(
            temporary_entry / "cache_manifest.json",
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "status": "completed",
                "cache_key": cache_key,
                "created_at": datetime.now().astimezone().isoformat(),
                "request": request,
                "artifacts": records,
            },
        )
        try:
            os.replace(temporary_entry, cache_entry)
        except OSError:
            if not cache_entry.exists():
                raise
        return _validated_cache_artifacts(cache_entry, cache_key, request) is not None
    finally:
        if temporary_entry.exists():
            shutil.rmtree(temporary_entry)


def run_netlist(
    netlist_path: Path,
    output_dir: Path | None = None,
    timeout_seconds: int = 120,
    ascii_raw: bool = False,
    reuse_cache: bool = False,
    cache_dir: Path | None = None,
) -> Path:
    """Run one netlist and return the directory containing LTspice outputs."""
    if not isinstance(reuse_cache, bool):
        raise ValueError("reuse_cache must be a boolean")
    if not LTSPICE.is_file():
        raise FileNotFoundError(f"LTspice executable not found: {LTSPICE}")

    netlist_path = netlist_path.expanduser().resolve()
    if not netlist_path.is_file():
        raise FileNotFoundError(f"Netlist not found: {netlist_path}")

    if output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output_dir = RUNS_DIR / stamp
    output_dir = output_dir.expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError("output directory must not already exist") from exc

    run_netlist_path = output_dir / netlist_path.name
    _stage_netlist(netlist_path, run_netlist_path)
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
        "simulator": _simulator_metadata(str(LTSPICE)),
        "runtime": _runtime_metadata(),
        "command": command,
        "working_directory": str(output_dir),
        "timeout_seconds": timeout_seconds,
        "ascii_raw": ascii_raw,
        "cache": {
            "requested": reuse_cache,
            "eligible": False,
            "hit": False,
            "key": None,
            "entry": None,
            "reason": None if reuse_cache else "cache reuse was not requested",
            "stored": False,
        },
    }
    cache_key: str | None = None
    cache_request: dict[str, object] | None = None
    cache_problem: str | None = None
    cache_entry: Path | None = None
    if reuse_cache:
        resolved_cache_dir = (
            RUNS_DIR / "cache" if cache_dir is None else cache_dir.expanduser()
        ).resolve()
        cache_key, cache_request, cache_problem = _simulation_cache_request(
            run_netlist_path,
            timeout_seconds,
            ascii_raw,
        )
        if cache_key is not None and cache_problem is None:
            cache_entry = resolved_cache_dir / f"simulation-{cache_key}"
            if cache_entry.is_dir() and not cache_entry.is_symlink() and _restore_cache_entry(
                cache_entry,
                cache_key,
                cache_request,
                output_dir,
                lambda: _simulation_cache_request(
                    run_netlist_path,
                    timeout_seconds,
                    ascii_raw,
                )[0]
                == cache_key,
            ):
                output_size_bytes = _result_size_bytes(
                    output_dir, netlist_path.name
                )
                if output_size_bytes > MAX_RUN_OUTPUT_BYTES:
                    manifest.update(
                        status="failed",
                        finished_at=datetime.now().astimezone().isoformat(),
                        duration_seconds=time.monotonic() - started_clock,
                        output_size_bytes=output_size_bytes,
                        error=(
                            "restored LTspice artifacts exceed "
                            f"{MAX_RUN_OUTPUT_BYTES} bytes"
                        ),
                    )
                    _write_manifest(manifest_path, manifest)
                    raise RuntimeError(manifest["error"])
                manifest.update(
                    status="completed",
                    finished_at=datetime.now().astimezone().isoformat(),
                    duration_seconds=time.monotonic() - started_clock,
                    returncode=0,
                    stdout="",
                    stderr="",
                    execution_source="cache",
                    cache={
                        "requested": True,
                        "eligible": True,
                        "hit": True,
                        "key": cache_key,
                        "entry": str(cache_entry),
                        "reason": None,
                        "stored": False,
                    },
                    result_files=sorted(
                        {item.name for item in output_dir.iterdir()}
                        | {"run_manifest.json"}
                    ),
                    result_artifacts=_result_artifacts(output_dir, netlist_path.name),
                    output_size_bytes=output_size_bytes,
                )
                _write_manifest(manifest_path, manifest)
                return output_dir
            if cache_entry.exists():
                cache_problem = "cache entry failed integrity validation"
        manifest["cache"] = {
            "requested": True,
            "eligible": cache_key is not None and cache_problem is None,
            "hit": False,
            "key": cache_key,
            "entry": None if cache_entry is None else str(cache_entry),
            "reason": cache_problem,
            "stored": False,
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

    output_size_bytes = _result_size_bytes(output_dir, netlist_path.name)
    if output_size_bytes > MAX_RUN_OUTPUT_BYTES:
        manifest.update(
            status="failed",
            finished_at=datetime.now().astimezone().isoformat(),
            duration_seconds=time.monotonic() - started_clock,
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
            output_size_bytes=output_size_bytes,
            error=f"LTspice artifacts exceed {MAX_RUN_OUTPUT_BYTES} bytes",
        )
        _write_manifest(manifest_path, manifest)
        raise RuntimeError(manifest["error"])

    manifest.update(
        status="completed",
        finished_at=datetime.now().astimezone().isoformat(),
        duration_seconds=time.monotonic() - started_clock,
        returncode=completed.returncode,
        stdout=completed.stdout[-4000:],
        stderr=completed.stderr[-4000:],
        result_files=sorted(item.name for item in output_dir.iterdir()),
        result_artifacts=_result_artifacts(output_dir, netlist_path.name),
        output_size_bytes=output_size_bytes,
        execution_source="simulator",
    )
    if reuse_cache and cache_key is not None and cache_request is not None:
        resolved_cache_dir = (
            RUNS_DIR / "cache" if cache_dir is None else cache_dir.expanduser()
        ).resolve()
        final_cache_key, _, final_problem = _simulation_cache_request(
            run_netlist_path,
            timeout_seconds,
            ascii_raw,
        )
        if cache_problem is None and (
            final_problem is not None or final_cache_key != cache_key
        ):
            cache_problem = "cache inputs changed during simulation"
        cache_stored = False
        if cache_problem is None:
            try:
                cache_stored = _publish_cache_entry(
                    resolved_cache_dir,
                    cache_key,
                    cache_request,
                    output_dir,
                    netlist_path.name,
                )
            except OSError as exc:
                cache_problem = f"cache entry could not be stored: {exc}"
        manifest["cache"] = {
            "requested": True,
            "eligible": cache_problem is None,
            "hit": False,
            "key": cache_key,
            "entry": str(resolved_cache_dir / f"simulation-{cache_key}"),
            "reason": cache_problem,
            "stored": cache_stored,
        }
    _write_manifest(manifest_path, manifest)
    return output_dir


def parse_measurements(log_path: Path) -> dict[str, float]:
    """Extract scalar .meas values from an LTspice log file."""
    text = _decode_log(log_path)

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
    if log_path.stat().st_size > MAX_LOG_FILE_BYTES:
        raise ValueError(f"LTspice log exceeds {MAX_LOG_FILE_BYTES} bytes")
    return decode_text(log_path.read_bytes())


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


def parse_stepped_measurement_rows(log_path: Path) -> dict[str, dict[int, float]]:
    """Read stepped `.meas` tables while preserving LTspice's row numbers."""
    text = _decode_log(log_path)
    tables: dict[str, dict[int, float]] = {}
    current_name: str | None = None
    measurement_pattern = re.compile(r"^Measurement:\s*([A-Za-z_][\w]*)\s*$")
    row_pattern = re.compile(
        r"^\s*(\d+)\s+\(?\s*"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
        r"(?=\s|[A-Za-z°),]|$)"
    )
    for line in text.splitlines():
        measurement = measurement_pattern.match(line.strip())
        if measurement is not None:
            current_name = measurement.group(1)
            if current_name in tables:
                raise ValueError(f"Duplicate stepped measurement table: {current_name}")
            tables[current_name] = {}
            continue
        if current_name is None:
            continue
        row = row_pattern.match(line)
        if row is None:
            continue
        step_number = int(row.group(1))
        if step_number in tables[current_name]:
            raise ValueError(
                f"Duplicate row {step_number} for stepped measurement {current_name}"
            )
        value = float(row.group(2))
        if not math.isfinite(value):
            raise ValueError(
                f"Non-finite row {step_number} for stepped measurement {current_name}"
            )
        tables[current_name][step_number] = value
    return tables


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
