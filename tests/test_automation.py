from __future__ import annotations

import struct
import tempfile
import unittest
import json
import platform
from pathlib import Path

from checks import assert_between, assert_close, floor, peak
from ltspice_wrapper import LTSPICE, parse_measurements, parse_step_values, parse_stepped_measurements, run_netlist
from raw_parser import parse_raw
from report_runs import collect_records
from examples.design_search_rc import choose_best


class AutomationTests(unittest.TestCase):
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
        records = collect_records(Path("runs"))
        self.assertTrue(records)
        self.assertTrue(any(record["status"] == "completed" for record in records))

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
