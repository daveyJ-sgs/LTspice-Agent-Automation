from __future__ import annotations

import unittest

import remote_execution


class RemoteExecutionPreviewTests(unittest.TestCase):
    def _preview(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "repository": remote_execution.DEFAULT_REPOSITORY,
            "ref": remote_execution.DEFAULT_REF,
            "plan_id": "statistical-plan-0123456789abcdef",
            "plan_sha256": "1" * 64,
            "recipe_sha256": "2" * 64,
            "plan_artifact": (
                "runs/statistical-plans/statistical-plan-0123456789abcdef/"
                "statistical_plan.json"
            ),
            "point_count": 24,
            "experiment_count": 2,
            "total_run_count": 48,
        }
        values.update(overrides)
        return remote_execution.build_remote_preview(**values)

    def test_preview_is_deterministic_and_explicitly_non_dispatchable(self) -> None:
        first = self._preview()
        second = self._preview()

        self.assertEqual(first, second)
        self.assertRegex(str(first["preview_id"]), r"remote-preview-[0-9a-f]{16}")
        self.assertRegex(str(first["preview_sha256"]), r"[0-9a-f]{64}")
        self.assertEqual(first["target"]["runner"], "windows-latest")
        self.assertEqual(first["evidence"]["retention_days"], 7)
        self.assertEqual(first["workload"]["total_run_count"], 48)
        self.assertEqual(
            first["safety"],
            {
                "dispatch_enabled": False,
                "external_request_made": False,
                "credentials_requested": False,
                "local_plan_modified": False,
            },
        )
        self.assertNotEqual(
            first["preview_sha256"],
            self._preview(ref="qualification-v1")["preview_sha256"],
        )

    def test_invalid_repository_ref_identity_and_workload_fail_closed(self) -> None:
        invalid = (
            {"repository": "https://github.com/owner/repo"},
            {"repository": "owner/repo/extra"},
            {"ref": "../main"},
            {"ref": "feature branch"},
            {"plan_id": "optimization-plan-0123456789abcdef"},
            {"plan_sha256": "not-a-hash"},
            {"plan_artifact": "../statistical_plan.json"},
            {"total_run_count": 0},
            {"point_count": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self._preview(**overrides)
