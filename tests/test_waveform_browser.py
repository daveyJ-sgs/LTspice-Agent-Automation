from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import waveform_browser


def write_raw(
    path: Path,
    variables: list[tuple[str, str]],
    columns: dict[str, list[float]],
    *,
    flags: str = "real forward",
) -> None:
    points = len(columns[variables[0][0]])
    header = [
        "Title: * fixture",
        "Date: Sun Sep 14 12:00:00 2026",
        "Plotname: Transient Analysis",
        f"Flags: {flags}",
        f"No. Variables: {len(variables)}",
        f"No. Points: {points}",
        "Variables:",
    ]
    for index, (name, kind) in enumerate(variables):
        header.append(f"\t{index}\t{name}\t{kind}")
    header.append("Binary:")
    body = bytearray()
    for point in range(points):
        for index, (name, _) in enumerate(variables):
            value = columns[name][point]
            body += struct.pack("<d", value) if index == 0 else struct.pack("<f", value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(header) + "\r\n").encode("utf-16-le") + bytes(body))


class WaveformBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name)
        self.capture = (
            self.runs / "demo-run" / "point-0000" / "attempt-0000" / "circuit.raw"
        )
        write_raw(
            self.capture,
            [("time", "time"), ("V(out)", "voltage"), ("V(in)", "voltage")],
            {
                "time": [0.0, 1e-6, 2e-6, 3e-6],
                "V(out)": [0.0, 1.0, 2.0, 3.0],
                "V(in)": [5.0, 5.0, 5.0, 5.0],
            },
        )

    def test_captures_are_listed_with_their_point_and_attempt(self) -> None:
        listing = waveform_browser.list_run_captures(self.runs, "demo-run")

        self.assertEqual(listing["experiment_id"], "demo-run")
        self.assertFalse(listing["truncated"])
        [capture] = listing["captures"]
        self.assertEqual(capture["point_index"], 0)
        self.assertEqual(capture["attempt"], 0)
        self.assertEqual(capture["filename"], "circuit.raw")
        self.assertEqual(
            capture["path"], "demo-run/point-0000/attempt-0000/circuit.raw"
        )

    def test_a_missing_experiment_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "experiment directory is missing"):
            waveform_browser.list_run_captures(self.runs, "no-such-run")

    def test_an_invalid_experiment_id_cannot_traverse(self) -> None:
        for identifier in ("../escape", "demo-run/point-0000", ""):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    waveform_browser.list_run_captures(self.runs, identifier)

    def test_a_capture_reads_back_as_plottable_vectors(self) -> None:
        result = waveform_browser.read_capture(
            self.runs, "demo-run/point-0000/attempt-0000/circuit.raw"
        )

        self.assertEqual(result["axis_variable"], "time")
        self.assertEqual(result["axis_unit"], "s")
        self.assertFalse(result["complex"])
        self.assertEqual(result["total_points"], 4)
        self.assertEqual(result["returned_points"], 4)
        self.assertEqual(result["axis"], [0.0, 1e-6, 2e-6, 3e-6])
        # The axis is never repeated as a trace.
        self.assertEqual(sorted(result["series"]), ["V(in)", "V(out)"])
        self.assertEqual(result["series"]["V(out)"], [0.0, 1.0, 2.0, 3.0])

    def test_requesting_fewer_points_downsamples_across_the_whole_sweep(self) -> None:
        result = waveform_browser.read_capture(
            self.runs, "demo-run/point-0000/attempt-0000/circuit.raw", max_points=2
        )

        self.assertEqual(result["returned_points"], 2)
        self.assertEqual(result["axis"], [0.0, 3e-6])
        self.assertEqual(result["series"]["V(out)"], [0.0, 3.0])

    def test_an_unknown_vector_names_itself(self) -> None:
        with self.assertRaisesRegex(ValueError, r"unknown vector\(s\): V\(mid\)"):
            waveform_browser.read_capture(
                self.runs,
                "demo-run/point-0000/attempt-0000/circuit.raw",
                variables=["V(mid)"],
            )

    def test_reads_stay_inside_the_runs_directory(self) -> None:
        outside = self.runs.parent / "outside.raw"
        outside.write_bytes(b"not a raw file")
        for path in ("../outside.raw", "demo-run/../../outside.raw"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    waveform_browser.read_capture(self.runs, path)

    def test_only_raw_files_are_served_to_the_viewer(self) -> None:
        note = self.runs / "demo-run" / "point-0000" / "attempt-0000" / "run.log"
        note.write_text("log output")

        with self.assertRaisesRegex(ValueError, "capture must be a .raw file"):
            waveform_browser.read_capture(
                self.runs, "demo-run/point-0000/attempt-0000/run.log"
            )

    def test_max_points_is_bounded(self) -> None:
        for maximum in (0, -1, waveform_browser.MAX_VIEWER_POINTS + 1, 2.5, True):
            with self.subTest(maximum=maximum):
                with self.assertRaisesRegex(ValueError, "max_points"):
                    waveform_browser.read_capture(
                        self.runs,
                        "demo-run/point-0000/attempt-0000/circuit.raw",
                        max_points=maximum,
                    )

    def test_an_ac_capture_is_reported_as_magnitude_on_a_hertz_axis(self) -> None:
        path = self.runs / "ac-run" / "point-0000" / "attempt-0000" / "ac.raw"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "Title: * ac fixture\r\nDate: Sun Sep 14 12:00:00 2026\r\n"
            "Plotname: AC Analysis\r\nFlags: complex forward\r\n"
            "No. Variables: 2\r\nNo. Points: 2\r\nVariables:\r\n"
            "\t0\tfrequency\tfrequency\r\n\t1\tV(out)\tvoltage\r\nBinary:\r\n"
        )
        body = bytearray()
        for frequency, real, imaginary in ((10.0, 3.0, 4.0), (100.0, 6.0, 8.0)):
            body += struct.pack("<dd", frequency, 0.0)
            body += struct.pack("<dd", real, imaginary)
        path.write_bytes(header.encode("utf-16-le") + bytes(body))

        result = waveform_browser.read_capture(
            self.runs, "ac-run/point-0000/attempt-0000/ac.raw"
        )

        self.assertTrue(result["complex"])
        self.assertEqual(result["axis_unit"], "Hz")
        self.assertEqual(result["signal_kind"], "magnitude")
        self.assertEqual(result["axis"], [10.0, 100.0])
        self.assertEqual(result["series"]["V(out)"], [5.0, 10.0])

    def test_csv_export_carries_every_vector_at_full_resolution(self) -> None:
        body = waveform_browser.capture_csv(self.capture)

        rows = body.splitlines()
        self.assertEqual(rows[0], "point,time,V(out),V(in)")
        self.assertEqual(len(rows), 5)
        self.assertTrue(rows[1].startswith("0,0.0,0.0,5.0"))


class TraceUnitTests(unittest.TestCase):
    """The viewer needs a unit per trace so volts and amps get separate axes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name)

    def test_every_raw_variable_kind_maps_onto_its_si_unit(self) -> None:
        self.assertEqual(waveform_browser.trace_unit("voltage"), "V")
        self.assertEqual(waveform_browser.trace_unit("device_current"), "A")
        self.assertEqual(waveform_browser.trace_unit("subckt_current"), "A")
        self.assertEqual(waveform_browser.trace_unit("power"), "W")
        self.assertEqual(waveform_browser.trace_unit("  Voltage  "), "V")

    def test_an_unrecognised_kind_is_unitless_rather_than_an_error(self) -> None:
        self.assertEqual(waveform_browser.trace_unit("something_new"), "")

    def test_a_capture_reports_the_unit_of_each_returned_trace(self) -> None:
        capture = self.runs / "units-run" / "point-0000" / "attempt-0000" / "c.raw"
        write_raw(
            capture,
            [("time", "time"), ("V(out)", "voltage"), ("I(R1)", "device_current")],
            {"time": [0.0, 1e-6], "V(out)": [0.0, 3.3], "I(R1)": [0.0, 1e-3]},
        )

        result = waveform_browser.read_capture(
            self.runs, "units-run/point-0000/attempt-0000/c.raw"
        )

        self.assertEqual(result["units"], {"V(out)": "V", "I(R1)": "A"})
        self.assertEqual(result["axis_unit"], "s")

    def test_an_operating_point_reads_every_node_as_a_value(self) -> None:
        capture = self.runs / "op-run" / "point-0000" / "attempt-0000" / "c.op.raw"
        write_raw(
            capture,
            [("V(vp)", "voltage"), ("V(out)", "voltage"), ("I(R1)", "device_current")],
            {"V(vp)": [15.0], "V(out)": [1.25], "I(R1)": [2e-3]},
        )

        result = waveform_browser.read_capture(
            self.runs, "op-run/point-0000/attempt-0000/c.op.raw"
        )

        self.assertTrue(result["operating_point"])
        self.assertIsNone(result["axis_variable"])
        self.assertEqual(list(result["series"]), ["V(vp)", "V(out)", "I(R1)"])
        self.assertEqual(result["series"]["V(vp)"], [15.0])
        self.assertEqual(result["units"]["I(R1)"], "A")

    def test_a_transient_capture_is_not_an_operating_point(self) -> None:
        capture = self.runs / "tran-run" / "point-0000" / "attempt-0000" / "c.raw"
        write_raw(
            capture,
            [("time", "time"), ("V(out)", "voltage")],
            {"time": [0.0], "V(out)": [3.3]},
        )

        result = waveform_browser.read_capture(
            self.runs, "tran-run/point-0000/attempt-0000/c.raw"
        )

        self.assertFalse(result["operating_point"])
        self.assertEqual(result["axis_variable"], "time")

    def test_a_raw_without_variable_kinds_still_reads(self) -> None:
        capture = self.runs / "bare-run" / "point-0000" / "attempt-0000" / "c.raw"
        header = (
            "Title: * bare\r\nDate: Sun Sep 14 12:00:00 2026\r\n"
            "Plotname: Transient Analysis\r\nFlags: real forward\r\n"
            "No. Variables: 2\r\nNo. Points: 2\r\nVariables:\r\n"
            "\t0\ttime\r\n\t1\tV(out)\r\nBinary:\r\n"
        )
        body = bytearray()
        for moment, value in ((0.0, 0.0), (1e-6, 3.3)):
            body += struct.pack("<d", moment) + struct.pack("<f", value)
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_bytes(header.encode("utf-16-le") + bytes(body))

        result = waveform_browser.read_capture(
            self.runs, "bare-run/point-0000/attempt-0000/c.raw"
        )

        self.assertEqual(result["units"], {"V(out)": ""})
        # Falls back to the axis name when the header carries no kind.
        self.assertEqual(result["axis_unit"], "s")


if __name__ == "__main__":
    unittest.main()
