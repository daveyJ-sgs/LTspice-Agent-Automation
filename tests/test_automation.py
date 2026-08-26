from __future__ import annotations

import struct
import tempfile
import unittest
import json
import platform
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from checks import assert_between, assert_close, floor, peak
import ltspice_wrapper
import experiment_engine
from ltspice_wrapper import (
    LTSPICE,
    parse_measurements,
    parse_step_values,
    parse_stepped_measurement_rows,
    parse_stepped_measurements,
    run_netlist,
)
from raw_parser import RawData, parse_raw, step_slices
from report_runs import collect_records, write_dashboard
from examples.design_search_rc import choose_best


class AutomationTests(unittest.TestCase):
    def test_experiment_manager_lock_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.lock"
            lock = experiment_engine._RunsProcessLock(path)
            probe = (
                "from pathlib import Path\n"
                "from experiment_engine import _RunsProcessLock\n"
                f"path = Path({str(path)!r})\n"
                "try:\n"
                "    lock = _RunsProcessLock(path)\n"
                "except RuntimeError:\n"
                "    raise SystemExit(0)\n"
                "lock.release()\n"
                "raise SystemExit(1)\n"
            )
            try:
                blocked = subprocess.run(
                    [sys.executable, "-c", probe],
                    cwd=Path(__file__).parents[1],
                    check=False,
                )
            finally:
                lock.release()
            self.assertEqual(blocked.returncode, 0)

            available = subprocess.run(
                [sys.executable, "-c", probe.replace("raise SystemExit(0)", "raise SystemExit(2)"),],
                cwd=Path(__file__).parents[1],
                check=False,
            )
            self.assertEqual(available.returncode, 1)

    def test_simulation_cache_is_disabled_by_default_and_never_stores_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                first = run_netlist(source, root / "first", cache_dir=cache)
                run_netlist(source, root / "second", cache_dir=cache)

            manifest = json.loads((first / "run_manifest.json").read_text())
            self.assertEqual(calls, 2)
            self.assertFalse(manifest["cache"]["requested"])
            self.assertFalse(cache.exists())
            self.assertEqual(manifest["result_artifacts"][0]["name"], "filter.log")
            self.assertEqual(len(manifest["result_artifacts"][0]["sha256"]), 64)

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(
                    ltspice_wrapper.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, "", "failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                run_netlist(
                    source,
                    root / "failed",
                    reuse_cache=True,
                    cache_dir=cache,
                )
            self.assertFalse(cache.exists())

    def test_run_rejects_oversized_output_before_hashing_or_cache_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"too large")
                run_netlist_path.with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper, "MAX_RUN_OUTPUT_BYTES", 8),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
                patch.object(
                    ltspice_wrapper,
                    "_result_artifacts",
                    side_effect=AssertionError("oversized artifacts must not be hashed"),
                ),
                self.assertRaisesRegex(RuntimeError, "artifacts exceed 8 bytes"),
            ):
                run_netlist(
                    source,
                    root / "oversized",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            manifest = json.loads(
                (root / "oversized" / "run_manifest.json").read_text()
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertGreater(manifest["output_size_bytes"], 8)
            self.assertFalse(cache.exists())

    def test_cache_integrity_rejects_oversized_artifacts_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_entry = Path(directory) / "simulation-key"
            artifact_dir = cache_entry / "artifacts"
            artifact_dir.mkdir(parents=True)
            artifact = artifact_dir / "filter.log"
            artifact.write_bytes(b"too large")
            request = {"netlist_filename": "filter.cir"}
            (cache_entry / "cache_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": ltspice_wrapper.CACHE_SCHEMA_VERSION,
                        "status": "completed",
                        "cache_key": "key",
                        "request": request,
                        "artifacts": [
                            {
                                "name": "filter.log",
                                "sha256": "unused",
                                "size_bytes": artifact.stat().st_size,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(ltspice_wrapper, "MAX_RUN_OUTPUT_BYTES", 8),
                patch.object(
                    ltspice_wrapper,
                    "_sha256_file",
                    side_effect=AssertionError("oversized cache must not be hashed"),
                ),
            ):
                validated = ltspice_wrapper._validated_cache_artifacts(
                    cache_entry, "key", request
                )

            self.assertIsNone(validated)

    def test_simulation_cache_reuses_verified_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"waveform")
                run_netlist_path.with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                first = run_netlist(
                    source,
                    root / "first",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                second = run_netlist(
                    source,
                    root / "second",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            first_manifest = json.loads((first / "run_manifest.json").read_text())
            second_manifest = json.loads((second / "run_manifest.json").read_text())
            self.assertEqual(calls, 1)
            self.assertFalse(first_manifest["cache"]["hit"])
            self.assertTrue(first_manifest["cache"]["stored"])
            self.assertEqual(first_manifest["execution_source"], "simulator")
            self.assertTrue(second_manifest["cache"]["hit"])
            self.assertFalse(second_manifest["cache"]["stored"])
            self.assertEqual(second_manifest["execution_source"], "cache")
            self.assertEqual((second / "filter.raw").read_bytes(), b"waveform")
            self.assertEqual(
                first_manifest["cache"]["key"], second_manifest["cache"]["key"]
            )

    def test_simulation_cache_invalidates_changed_inputs_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            model = root / "device.lib"
            model.write_text(".param RV=1k\n", encoding="utf-8")
            source = root / "filter.cir"
            source.write_text(
                f'.include "{model}"\nR1 in out {{RV}}\n.end\n', encoding="utf-8"
            )
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(str(calls).encode())
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                run_netlist(source, root / "one", reuse_cache=True, cache_dir=cache)
                model.write_text(".param RV=2k\n", encoding="utf-8")
                run_netlist(source, root / "two", reuse_cache=True, cache_dir=cache)
                run_netlist(
                    source,
                    root / "three",
                    ascii_raw=True,
                    reuse_cache=True,
                    cache_dir=cache,
                )
                executable.write_bytes(b"simulator-version-two")
                run_netlist(
                    source,
                    root / "four",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            self.assertEqual(calls, 4)
            self.assertEqual(len(list(cache.glob("simulation-*"))), 4)

    def test_simulation_cache_bypasses_unresolved_or_corrupt_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(b"waveform")
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                source.write_text('.include "missing.lib"\n.end\n', encoding="utf-8")
                unresolved = run_netlist(
                    source,
                    root / "unresolved",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                source.write_text("D1 in 0 DTEST\n.end\n", encoding="utf-8")
                model_dependent = run_netlist(
                    source,
                    root / "model-dependent",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
                first = run_netlist(
                    source,
                    root / "first",
                    reuse_cache=True,
                    cache_dir=cache,
                )
                first_manifest = json.loads((first / "run_manifest.json").read_text())
                cache_entry = cache / f"simulation-{first_manifest['cache']['key']}"
                next((cache_entry / "artifacts").iterdir()).write_bytes(b"corrupt")
                corrupt = run_netlist(
                    source,
                    root / "corrupt",
                    reuse_cache=True,
                    cache_dir=cache,
                )

            unresolved_manifest = json.loads(
                (unresolved / "run_manifest.json").read_text()
            )
            model_manifest = json.loads(
                (model_dependent / "run_manifest.json").read_text()
            )
            corrupt_manifest = json.loads((corrupt / "run_manifest.json").read_text())
            self.assertEqual(calls, 4)
            self.assertFalse(unresolved_manifest["cache"]["eligible"])
            self.assertIn("cannot be resolved", unresolved_manifest["cache"]["reason"])
            self.assertFalse(model_manifest["cache"]["eligible"])
            self.assertIn("model-dependent", model_manifest["cache"]["reason"])
            self.assertFalse(corrupt_manifest["cache"]["hit"])
            self.assertIn("integrity", corrupt_manifest["cache"]["reason"])

    def test_simulation_cache_rechecks_inputs_after_staging_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            dependency = root / "values.inc"
            dependency.write_text(".param RV=1k\n", encoding="utf-8")
            source = root / "filter.cir"
            source.write_text(
                f'.include "{dependency}"\nR1 in out {{RV}}\n.end\n',
                encoding="utf-8",
            )
            cache = root / "cache"
            calls = 0

            def simulate(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                run_netlist_path = Path(command[-1])
                run_netlist_path.with_suffix(".raw").write_bytes(str(calls).encode())
                run_netlist_path.with_suffix(".log").write_text("gain=1\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            ltspice_wrapper._simulator_metadata_cached.cache_clear()
            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                run_netlist(source, root / "first", reuse_cache=True, cache_dir=cache)
                original_copy = ltspice_wrapper.shutil.copy2
                dependency_changed = False

                def copy_and_change_dependency(
                    source_path: Path, destination_path: Path
                ) -> Path:
                    nonlocal dependency_changed
                    copied = original_copy(source_path, destination_path)
                    if "artifacts" in Path(source_path).parts and not dependency_changed:
                        dependency.write_text(".param RV=2k\n", encoding="utf-8")
                        dependency_changed = True
                    return copied

                with patch.object(
                    ltspice_wrapper.shutil,
                    "copy2",
                    side_effect=copy_and_change_dependency,
                ):
                    second = run_netlist(
                        source,
                        root / "second",
                        reuse_cache=True,
                        cache_dir=cache,
                    )

            manifest = json.loads((second / "run_manifest.json").read_text())
            self.assertTrue(dependency_changed)
            self.assertEqual(calls, 2)
            self.assertEqual(manifest["execution_source"], "simulator")
            self.assertFalse(manifest["cache"]["hit"])

    def test_parse_complex_raw_file(self) -> None:
        header = """Title: test
Flags: complex forward
No. Variables: 2
No. Points: 2
Variables:
\t0\tfrequency\tfrequency
\t1\tV(out)\tvoltage
Binary:
"""
        binary = b"".join(
            struct.pack("<dd", *pair)
            for pair in ((10.0, 0.0), (1.0, -2.0), (20.0, 0.0), (3.0, -4.0))
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.points, 2)
        self.assertEqual(data.variables, ["frequency", "V(out)"])
        self.assertEqual(data.values["V(out)"], [complex(1, -2), complex(3, -4)])

    def test_parse_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            path.write_text(
                "gain: mag(v(out))=(-35.5dB,0°) at 1000\n"
                "tpd=2.5e-9 FROM 1e-6 TO 1.0025e-6\n",
                encoding="utf-16le",
            )
            self.assertEqual(parse_measurements(path), {"gain": -35.5, "tpd": 2.5e-9})

    def test_raw_and_log_parsers_reject_oversized_artifacts_before_read(self) -> None:
        import ltspice_wrapper
        import raw_parser

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "oversized.raw"
            log_path = Path(directory) / "oversized.log"
            with raw_path.open("wb") as handle:
                handle.truncate(raw_parser.MAX_RAW_FILE_BYTES + 1)
            with log_path.open("wb") as handle:
                handle.truncate(ltspice_wrapper.MAX_LOG_FILE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "RAW file exceeds"):
                parse_raw(raw_path)
            with self.assertRaisesRegex(ValueError, "LTspice log exceeds"):
                parse_measurements(log_path)

    def test_log_parsers_accept_utf8_and_utf16le_with_or_without_bom(self) -> None:
        log = ".step rval=1000\nMeasurement: gain\n step value\n 1 2.5\ngain=2.5\n"
        encoded_logs = {
            "utf8": log.encode("utf-8"),
            "utf8-bom": b"\xef\xbb\xbf" + log.encode("utf-8"),
            "utf16le": log.encode("utf-16le"),
            "utf16le-bom": b"\xff\xfe" + log.encode("utf-16le"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "encoding.log"
            for name, payload in encoded_logs.items():
                with self.subTest(encoding=name):
                    path.write_bytes(payload)
                    self.assertEqual(parse_measurements(path), {"gain": 2.5})
                    self.assertEqual(parse_step_values(path, "rval"), [1000.0])
                    self.assertEqual(
                        parse_stepped_measurement_rows(path), {"gain": {1: 2.5}}
                    )

    def test_parse_stepped_log(self) -> None:
        log = """.step rval=1000
.step rval=2200
Measurement: gain_at_1k
  step\tmag(v(out))\tat
     1\t(-16.0722dB,0°)\t1000
     2\t(-22.8347dB,0°)\t1000
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "step.log"
            path.write_text(log, encoding="utf-16le")
            self.assertEqual(parse_step_values(path, "rval"), [1000.0, 2200.0])
            self.assertEqual(parse_stepped_measurements(path, "gain_at_1k"), [-16.0722, -22.8347])
            self.assertEqual(
                parse_stepped_measurement_rows(path),
                {"gain_at_1k": {1: -16.0722, 2: -22.8347}},
            )

    def test_step_slices_preserve_nonuniform_blocks(self) -> None:
        data = RawData(
            flags="real stepped",
            variables=["time", "V(out)"],
            values={
                "time": [0, 1, 2, 0, 0.5, 1.5, 2.5],
                "V(out)": [0, 1, 2, 10, 11, 12, 13],
            },
            step_count=2,
            points_per_step=None,
        )

        segments = step_slices(data)
        self.assertEqual([(part.start, part.stop) for part in segments], [(0, 3), (3, 7)])

    def test_repeated_axis_sample_does_not_create_a_false_step(self) -> None:
        data = RawData(
            flags="real",
            variables=["time", "V(out)"],
            values={"time": [0, 1, 1, 2], "V(out)": [0, 1, 1.5, 2]},
            step_count=1,
            points_per_step=4,
        )

        segments = step_slices(data)

        self.assertEqual([(part.start, part.stop) for part in segments], [(0, 4)])

    def test_stepped_measurement_rows_never_accept_numeric_prefixes_or_infinity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "step.log"
            path.write_text(
                "Measurement: gain\n step value\n 1 (1.#INF,0°)\n",
                encoding="utf-16le",
            )
            self.assertEqual(parse_stepped_measurement_rows(path), {"gain": {}})
            path.write_text(
                "Measurement: gain\n step value\n 1 1e999\n",
                encoding="utf-16le",
            )
            with self.assertRaisesRegex(ValueError, "Non-finite row 1"):
                parse_stepped_measurement_rows(path)

    def test_parse_real_compact_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dfdf", 0.0, 1.5, 0.1, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertAlmostEqual(data.values["V(out)"][1], 2.5)

    def test_parse_ascii_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Values:
"""
        rows = "0\t0.0\n\t1.5\n1\t0.1\n\t2.5\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascii.raw"
            path.write_bytes((header + rows).encode("utf-16le"))
            data = parse_raw(path)

        self.assertEqual(data.variables, ["time", "V(out)"])
        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_parse_ascii_raw_preserves_utf8_or_utf16le_values_encoding(self) -> None:
        document = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Values:
0\t0.0
\t1.5
1\t0.1
\t2.5
"""
        payloads = {
            "utf8": document.encode("utf-8"),
            "utf8-bom": b"\xef\xbb\xbf" + document.encode("utf-8"),
            "utf16le": document.encode("utf-16le"),
            "utf16le-bom": b"\xff\xfe" + document.encode("utf-16le"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascii.raw"
            for name, payload in payloads.items():
                with self.subTest(encoding=name):
                    path.write_bytes(payload)
                    data = parse_raw(path)
                    self.assertEqual(data.values["time"], [0.0, 0.1])
                    self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_parse_complex_ascii_raw_values(self) -> None:
        document = """Title: test
Flags: complex forward
No. Variables: 2
No. Points: 2
Variables:
\t0\tfrequency\tfrequency
\t1\tV(out)\tvoltage
Values:
0\t10,0
\t1,-2
1\t20,0
\t3,-4
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "complex-ascii.raw"
            path.write_bytes(document.encode("utf-8"))
            data = parse_raw(path)

        self.assertEqual(data.values["frequency"], [complex(10, 0), complex(20, 0)])
        self.assertEqual(data.values["V(out)"], [complex(1, -2), complex(3, -4)])

    def test_run_netlist_rejects_existing_explicit_output_before_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            output = root / "run"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run") as simulate,
                self.assertRaisesRegex(ValueError, "must not already exist"),
            ):
                run_netlist(source, output_dir=output)

            simulate.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_concurrent_runs_cannot_share_an_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source = root / "filter.cir"
            source.write_text("R1 in out 1k\n.end\n", encoding="utf-8")
            output = root / "run"
            barrier = threading.Barrier(2)

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                Path(command[-1]).with_suffix(".log").write_text(
                    "gain=1\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            def invoke() -> str:
                barrier.wait()
                try:
                    run_netlist(source, output_dir=output)
                except ValueError as exc:
                    return str(exc)
                return "completed"

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                outcomes = sorted(executor.map(lambda _: invoke(), range(2)))

            self.assertEqual(
                outcomes,
                ["completed", "output directory must not already exist"],
            )

    def test_run_netlist_preserves_relative_include_and_lib_paths_when_staged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LTspice.exe"
            executable.write_bytes(b"simulator")
            source_dir = root / "source"
            models = source_dir / "models"
            models.mkdir(parents=True)
            include = models / "values.inc"
            library = models / "device.lib"
            include.write_text(".param RV=1k\n", encoding="utf-8")
            library.write_text(".model DTEST D\n", encoding="utf-8")
            source = source_dir / "filter.cir"
            source.write_text(
                '.include "models/values.inc"\n.lib \'models/device.lib\' DTEST\n.end\n',
                encoding="utf-8",
            )

            def simulate(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                staged = Path(command[-1])
                text = staged.read_text(encoding="utf-8")
                self.assertIn(f'.include "{include.resolve()}"', text)
                self.assertIn(f'.lib "{library.resolve()}" DTEST', text)
                staged.with_suffix(".log").write_text("gain=1\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(ltspice_wrapper, "LTSPICE", executable),
                patch.object(ltspice_wrapper.subprocess, "run", side_effect=simulate),
            ):
                result = run_netlist(source, output_dir=root / "run")

            self.assertTrue((result / "filter.cir").is_file())

    def test_parse_real_double_precision_raw_file(self) -> None:
        header = """Title: test
Flags: real forward
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dddd", 0.0, 1.5, 0.1, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_binary_transient_time_sign_bits_do_not_create_false_steps(self) -> None:
        header = """Title: compressed transient
Flags: real forward
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<dfdfdf", 0.0, 1.0, -0.1, 2.0, -0.2, 3.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compressed.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1, 0.2])
        self.assertEqual(data.step_count, 1)
        self.assertEqual(data.points_per_step, 3)

    def test_parse_fast_access_raw_file(self) -> None:
        header = """Title: test
Flags: real forward FastAccess
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Binary:
"""
        binary = struct.pack("<ddff", 0.0, 0.1, 1.5, 2.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.raw"
            path.write_bytes(header.encode("utf-16le") + binary)
            data = parse_raw(path)

        self.assertEqual(data.values["time"], [0.0, 0.1])
        self.assertEqual(data.values["V(out)"], [1.5, 2.5])

    def test_checks(self) -> None:
        assert_close("gain", -35.0, -35.0, 0.1)
        assert_between("peak", 5.0, 4.9, 5.1)
        self.assertEqual(peak([1, 5, 2]), 5.0)
        self.assertEqual(floor([1, 5, 2]), 1.0)

    def test_choose_best_design_trial(self) -> None:
        rows = [
            {"gain_at_1k_db": -29.0},
            {"gain_at_1k_db": -30.1},
            {"gain_at_1k_db": -31.0},
        ]
        self.assertEqual(choose_best(rows, -30.0), rows[1])

    def test_manifest_dashboard_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "completed-run"
            run_dir.mkdir()
            (run_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "started_at": "2026-08-23T12:00:00+00:00",
                        "duration_seconds": 0.1,
                        "result_files": [],
                    }
                )
            )
            records = collect_records(Path(directory))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")

    def test_manifest_dashboard_rejects_escaped_artifact_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            run_dir = root / "completed-run"
            run_dir.mkdir(parents=True)
            (run_dir / "result.raw").touch()
            outside = root.parent / "outside.raw"
            outside.touch()
            try:
                (run_dir / "escape.raw").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            (run_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "result_files": [
                            "result.raw",
                            "../../outside.raw",
                            "escape.raw",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            records = collect_records(root)

        self.assertEqual(
            records[0]["artifacts"],
            [{"name": "result.raw", "href": "completed-run/result.raw"}],
        )

    def test_manifest_dashboard_does_not_follow_output_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            root.mkdir()
            outside_html = root.parent / "outside.html"
            outside_json = root.parent / "outside.json"
            outside_html.write_text("html sentinel", encoding="utf-8")
            outside_json.write_text("json sentinel", encoding="utf-8")
            try:
                (root / "index.html").symlink_to(outside_html)
                (root / "index.json").symlink_to(outside_json)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                write_dashboard(root)

            self.assertEqual(outside_html.read_text(encoding="utf-8"), "html sentinel")
            self.assertEqual(outside_json.read_text(encoding="utf-8"), "json sentinel")

    @unittest.skipUnless(LTSPICE.is_file(), "LTspice integration test requires an installed simulator")
    def test_failed_run_writes_failed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.cir"
            output = Path(directory) / "run"
            source.write_text("this is not a valid LTspice deck\n")
            with self.assertRaises(RuntimeError):
                run_netlist(source, output_dir=output)
            manifest = json.loads((output / "run_manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            # LTspice's failure exit code is platform-specific: 255 on the
            # macOS build, 1 on LTspice 26.0.2 for Windows. The contract worth
            # asserting is "non-zero", not one platform's particular value.
            self.assertNotEqual(manifest["returncode"], 0)
            self.assertEqual(manifest["runtime"]["operating_system"]["system"], platform.system())
            self.assertIn("python", manifest["runtime"])
            self.assertEqual(manifest["simulator"]["executable"], str(LTSPICE))
            self.assertEqual(len(manifest["simulator"]["executable_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
