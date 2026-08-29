import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import artifact_retention


class ArtifactRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary.name) / "runs"
        self.runs.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _entry(self, name, manifest_name, manifest, payload_size=10):
        directory = self.runs / name
        directory.mkdir()
        (directory / manifest_name).write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "payload.raw").write_bytes(b"x" * payload_size)
        return directory

    def test_inventory_only_accepts_terminal_manifest_proven_entries(self):
        finished = "2026-01-01T00:00:00+00:00"
        self._entry("run-old", "run_manifest.json", {"status": "completed", "finished_at": finished})
        self._entry("run-active", "run_manifest.json", {"status": "running", "finished_at": finished})
        self._entry(
            "experiment-old",
            "experiment_manifest.json",
            {"status": "completed", "finished_at": finished, "experiment_id": "experiment-old"},
        )
        self._entry("unknown", "other.json", {"status": "completed"})

        entries, unmanaged = artifact_retention.inventory(self.runs)

        self.assertEqual({entry.path.name for entry in entries}, {"run-old", "experiment-old"})
        self.assertEqual(unmanaged, 2)

    def test_prune_is_planned_by_age_and_preserves_recent_entries(self):
        for day in range(1, 4):
            self._entry(
                f"run-{day}",
                "run_manifest.json",
                {"status": "completed", "finished_at": f"2026-01-0{day}T00:00:00+00:00"},
            )

        plan = artifact_retention.plan_prune(
            self.runs,
            scopes=("runs",),
            older_than_days=1,
            keep_recent=1,
            now=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        self.assertEqual([entry.path.name for entry in plan.entries], ["run-1", "run-2"])
        self.assertTrue((self.runs / "run-1").exists(), "planning must be non-mutating")

    def test_apply_revalidates_manifest_before_deleting(self):
        target = self._entry(
            "run-old",
            "run_manifest.json",
            {"status": "failed", "finished_at": "2026-01-01T00:00:00+00:00"},
        )
        plan = artifact_retention.plan_prune(
            self.runs,
            scopes=("runs",),
            older_than_days=1,
            keep_recent=0,
            now=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )
        (target / "run_manifest.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "changed after planning"):
            artifact_retention.apply_prune(self.runs, plan)
        self.assertTrue(target.exists())

    def test_referenced_experiment_is_not_planned_for_deletion(self):
        experiment = self._entry(
            "mcp-experiment-old",
            "experiment_manifest.json",
            {
                "status": "completed",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "experiment_id": "mcp-experiment-old",
            },
        )
        study = self.runs / "optimization-studies" / "study-one"
        study.mkdir(parents=True)
        (study / "optimization_results.json").write_text(
            json.dumps({"experiment_id": experiment.name}), encoding="utf-8"
        )

        plan = artifact_retention.plan_prune(
            self.runs,
            scopes=("experiments",),
            older_than_days=1,
            keep_recent=0,
            now=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(plan.entries, ())

    def test_apply_deletes_only_validated_target(self):
        target = self._entry(
            "run-old",
            "run_manifest.json",
            {"status": "completed", "finished_at": "2026-01-01T00:00:00+00:00"},
        )
        unknown = self._entry("unknown", "other.json", {})
        plan = artifact_retention.plan_prune(
            self.runs,
            scopes=("runs",),
            older_than_days=1,
            keep_recent=0,
            now=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        artifact_retention.apply_prune(self.runs, plan)

        self.assertFalse(target.exists())
        self.assertTrue(unknown.exists())

    def test_apply_rechecks_new_experiment_references(self):
        target = self._entry(
            "mcp-experiment-old",
            "experiment_manifest.json",
            {
                "status": "completed",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "experiment_id": "mcp-experiment-old",
            },
        )
        plan = artifact_retention.plan_prune(
            self.runs,
            scopes=("experiments",),
            older_than_days=1,
            keep_recent=0,
            now=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )
        study = self.runs / "comparison"
        study.mkdir()
        (study / "comparison.json").write_text(
            json.dumps({"candidate_experiment_id": target.name}), encoding="utf-8"
        )

        with self.assertRaisesRegex(RuntimeError, "became referenced"):
            artifact_retention.apply_prune(self.runs, plan)
        self.assertTrue(target.exists())

    def test_symlinked_runs_root_is_rejected(self):
        link = Path(self.temporary.name) / "linked-runs"
        link.symlink_to(self.runs, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            artifact_retention.inventory(link)


if __name__ == "__main__":
    unittest.main()
