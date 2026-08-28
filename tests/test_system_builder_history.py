import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from system_builder_history import evidence_file, workspace_history


class SystemBuilderHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.runs = self.workspace / "runs"
        self.runs.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_job(
        self,
        experiment_id: str = "mcp-experiment-20260827-201100-123456",
        *,
        status: str = "running",
    ) -> Path:
        experiment = self.runs / experiment_id
        experiment.mkdir()
        manifest = {
            "schema_version": 2,
            "experiment_id": experiment_id,
            "status": status,
            "created_at": "2026-08-28T03:11:00+00:00",
            "point_count": 24,
            "finished_points": 9,
            "running_points": 2,
            "completed_points": 9,
            "error_points": 0,
            "passed_points": 8,
            "failed_points": 1,
            "definition": {"execution_mode": "independent"},
        }
        (experiment / "experiment_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (experiment / "report.html").write_text("<h1>Evidence</h1>", encoding="utf-8")
        return experiment

    def test_history_reads_live_manifest_progress_without_writing(self) -> None:
        self.write_job()
        before = {
            path.relative_to(self.workspace).as_posix(): path.read_bytes()
            for path in self.workspace.rglob("*")
            if path.is_file()
        }

        result = workspace_history(self.workspace)

        after = {
            path.relative_to(self.workspace).as_posix(): path.read_bytes()
            for path in self.workspace.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(result["summary"]["total_jobs"], 1)
        self.assertEqual(result["summary"]["active_jobs"], 1)
        self.assertEqual(result["jobs"][0]["finished_points"], 9)
        self.assertEqual(result["jobs"][0]["pending_points"], 13)
        self.assertTrue(result["jobs"][0]["report_available"])
        self.assertFalse(result["index"]["available"])
        self.assertFalse(result["index"]["current"])

    def test_history_is_bounded_and_skips_invalid_manifests(self) -> None:
        self.write_job()
        invalid = self.runs / "mcp-experiment-20260827-201101-123456"
        invalid.mkdir()
        (invalid / "experiment_manifest.json").write_text("[]", encoding="utf-8")

        result = workspace_history(self.workspace, limit=1)

        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(len(result["issues"]), 1)
        with self.assertRaises(ValueError):
            workspace_history(self.workspace, limit=51)

    def test_history_reports_a_readable_but_stale_index(self) -> None:
        first = self.write_job()
        second_id = "mcp-experiment-20260827-201101-123456"
        self.write_job(second_id, status="completed")
        (self.runs / "experiments.sqlite3").write_bytes(b"index")
        indexed_record = {
            "experiment_id": first.name,
            "execution_mode": "independent",
            "statistical": True,
            "observed_yield": 1.0,
        }

        with (
            patch(
                "system_builder_history.query_experiments",
                return_value={"experiments": [indexed_record]},
            ),
            patch(
                "system_builder_history.query_studies",
                return_value={"studies": []},
            ),
        ):
            result = workspace_history(self.workspace)

        self.assertTrue(result["index"]["available"])
        self.assertFalse(result["index"]["current"])
        self.assertEqual(result["index"]["unindexed_jobs"], 1)
        self.assertIn("1 durable job", result["index"]["message"])

    def test_evidence_resolver_confines_regular_files_to_runs(self) -> None:
        experiment = self.write_job()
        report = evidence_file(self.runs, f"{experiment.name}/report.html")

        self.assertEqual(report, (experiment / "report.html").resolve())
        with self.assertRaises(ValueError):
            evidence_file(self.runs, "../secret.txt")
        with self.assertRaises(ValueError):
            evidence_file(self.runs, experiment.name)

    def test_evidence_resolver_rejects_symlinks(self) -> None:
        experiment = self.write_job()
        target = experiment / "report.html"
        link = experiment / "linked.html"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is unavailable")

        with self.assertRaises(ValueError):
            evidence_file(self.runs, f"{experiment.name}/linked.html")


if __name__ == "__main__":
    unittest.main()
