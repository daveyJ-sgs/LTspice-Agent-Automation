from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import mcp_server
    from raw_parser import RawData
except ModuleNotFoundError as exc:
    if exc.name != "mcp":
        raise
    mcp_server = None
    RawData = None


@unittest.skipIf(mcp_server is None, "optional mcp package is not installed")
class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.runs = self.root / "runs"
        self.examples = self.root / "examples"
        self.runs.mkdir()
        self.examples.mkdir()
        self.runs_patch = patch.object(mcp_server, "RUNS_DIR", self.runs)
        self.examples_patch = patch.object(mcp_server, "EXAMPLES_DIR", self.examples)
        self.runs_patch.start()
        self.examples_patch.start()

    def tearDown(self) -> None:
        self.examples_patch.stop()
        self.runs_patch.stop()
        self.temporary_directory.cleanup()

    def make_run(self, name: str = "run-001") -> Path:
        run_dir = self.runs / name
        run_dir.mkdir(parents=True)
        return run_dir

    def test_run_netlist(self) -> None:
        output_dir = self.make_run()
        summary = {"run_dir": str(output_dir), "status": "completed"}
        with (
            patch.object(mcp_server, "_run_netlist_text", return_value=output_dir) as execute,
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_netlist("* test\n.end\n", "demo.cir", True, 30)

        self.assertEqual(result, summary)
        args = execute.call_args.args
        self.assertEqual(args[:4], ("* test\n.end\n", "demo.cir", True, 30))
        self.assertEqual(args[4].parent, self.runs)
        with self.assertRaises(ValueError):
            mcp_server.run_netlist(".end", "../escape.cir")
        with self.assertRaises(ValueError):
            mcp_server.run_netlist(".end", "..\\escape.cir")

    def test_run_netlist_file(self) -> None:
        source = self.root / "source.cir"
        source.write_text(".end\n")
        output_dir = self.runs / "custom"
        summary = {"run_dir": str(output_dir), "status": "completed"}
        with (
            patch.object(mcp_server.wrapper, "run_netlist", return_value=output_dir) as execute,
            patch.object(mcp_server, "_summarize_run", return_value=summary),
        ):
            result = mcp_server.run_netlist_file(str(source), str(output_dir), timeout_seconds=15)

        self.assertEqual(result, summary)
        self.assertEqual(execute.call_args.args[0], source)
        self.assertEqual(execute.call_args.kwargs["output_dir"], output_dir.resolve())
        with self.assertRaises(ValueError):
            mcp_server.run_netlist_file(str(source), str(self.root / "outside"))

    def test_get_measurements(self) -> None:
        run_dir = self.make_run()
        (run_dir / "a.log").touch()
        (run_dir / "b.log").touch()
        with patch.object(
            mcp_server.wrapper,
            "parse_measurements",
            side_effect=({"gain": 2.0}, {"delay": 3e-6}),
        ):
            result = mcp_server.get_measurements(str(run_dir))

        self.assertEqual(result, {"gain": 2.0, "delay": 3e-6})
        with self.assertRaises(ValueError):
            mcp_server.get_measurements(str(self.root))

    def test_get_waveform(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "result.raw"
        raw_path.touch()
        data = RawData(
            flags="complex forward",
            variables=["frequency", "V(out)"],
            values={
                "frequency": [10.0, 20.0, 30.0, 40.0],
                "V(out)": [complex(1, -1), complex(2, -2), complex(3, -3), complex(4, -4)],
            },
        )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.get_waveform(
                str(run_dir), variables=["V(out)"], max_points=2
            )

        self.assertEqual(result["returned_points"], 2)
        self.assertEqual(result["data"]["V(out)"], {"real": [1.0, 3.0], "imag": [-1.0, -3.0]})
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), max_points=0)
        with self.assertRaises(ValueError):
            mcp_server.get_waveform(str(run_dir), raw_filename="../result.raw")

    def test_export_waveform_csv(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "result.raw"
        raw_path.touch()
        data = RawData(flags="real", variables=["time"], values={"time": [0.0, 1.0]})
        with (
            patch.object(mcp_server.raw_parser, "parse_raw", return_value=data),
            patch.object(mcp_server.raw_parser, "export_csv") as export,
        ):
            result = mcp_server.export_waveform_csv(str(run_dir))

        export.assert_called_once_with(data, run_dir.resolve() / "result.csv")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["csv_path"], str(run_dir.resolve() / "result.csv"))

    def test_analyze_waveform_uses_full_selected_step(self) -> None:
        run_dir = self.make_run()
        raw_path = run_dir / "stepped.raw"
        raw_path.touch()
        data = RawData(
            flags="real stepped",
            variables=["time", "V(out)"],
            values={
                "time": [0, 1, 2, 0, 1, 2, 3],
                "V(out)": [0, 1, 2, 0, 4, 5.5, 5],
            },
            step_count=2,
            points_per_step=None,
        )
        requirements = [
            {"metric": "maximum", "operator": "<=", "target": 6.0},
            {
                "metric": "overshoot",
                "operator": "<=",
                "target": 11.0,
                "initial_value": 0.0,
                "final_value": 5.0,
            },
        ]
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                requirements,
                step_index=1,
                signal_unit="V",
            )

        self.assertEqual(result["source_points"], 4)
        self.assertEqual(result["analysis_resolution"], "full")
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["evidence"]["index"], 2)
        self.assertEqual(result["results"][1]["value"], 10.0)
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            with self.assertRaisesRegex(ValueError, "step_index is required"):
                mcp_server.analyze_waveform(str(run_dir), "V(out)", requirements)
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            with self.assertRaisesRegex(IndexError, "step_index"):
                mcp_server.analyze_waveform(
                    str(run_dir), "V(out)", requirements, step_index=1.5
                )

    def test_analyze_waveform_does_not_miss_a_narrow_spike(self) -> None:
        run_dir = self.make_run()
        (run_dir / "result.raw").touch()
        values = [0.0] * 401
        values[199] = 9.0
        data = RawData(
            flags="real forward",
            variables=["time", "V(out)"],
            values={"time": list(range(401)), "V(out)": values},
        )
        with patch.object(mcp_server.raw_parser, "parse_raw", return_value=data):
            result = mcp_server.analyze_waveform(
                str(run_dir),
                "V(out)",
                [{"metric": "maximum", "operator": ">=", "target": 9.0}],
                signal_unit="V",
            )

        self.assertEqual(result["source_points"], 401)
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["results"][0]["evidence"]["index"], 199)

    def test_run_parameter_sweep(self) -> None:
        def execute(netlist: str, filename: str, ascii_raw: bool, timeout: int, dest: Path) -> Path:
            dest.mkdir(parents=True)
            return dest

        def summarize(output_dir: Path) -> dict[str, object]:
            return {
                "run_dir": str(output_dir),
                "status": "completed",
                "duration_seconds": 0.1,
                "measurements": {"gain": float(output_dir.name[-1])},
            }

        with (
            patch.object(mcp_server, "_run_netlist_text", side_effect=execute),
            patch.object(mcp_server, "_summarize_run", side_effect=summarize),
        ):
            result = mcp_server.run_parameter_sweep("R1 in out {value}", ["1k", "2k"])

        self.assertEqual(len(result["points"]), 2)
        self.assertTrue(Path(result["results_csv"]).is_file())
        with self.assertRaises(ValueError):
            mcp_server.run_parameter_sweep("R1 in out 1k", ["1k"])

    def test_history_dashboard_and_examples(self) -> None:
        (self.examples / "a.cir").write_text("* AC example\n.end\n")
        (self.examples / "b.net").write_text("* Transient example\n.end\n")
        records = [{"run": "new"}, {"run": "old"}]
        with patch.object(mcp_server.report_runs, "collect_records", return_value=records):
            self.assertEqual(mcp_server.list_runs(1), records[:1])

        html_path = self.runs / "index.html"
        json_path = self.runs / "index.json"
        with patch.object(
            mcp_server.report_runs, "write_dashboard", return_value=(html_path, json_path)
        ):
            self.assertEqual(
                mcp_server.build_dashboard(),
                {"html": str(html_path), "json": str(json_path)},
            )

        examples = mcp_server.list_examples()
        self.assertEqual([item["name"] for item in examples], ["a.cir", "b.net"])
        self.assertEqual(examples[0]["description"], "AC example")


if __name__ == "__main__":
    unittest.main()
