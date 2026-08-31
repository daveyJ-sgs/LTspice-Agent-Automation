from __future__ import annotations

import base64
import unittest
import zlib

import artifacts
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


class RemoteExecutionEnvelopeTests(unittest.TestCase):
    def _inputs(self) -> tuple[dict[str, object], dict[str, object], bytes, list[dict[str, object]]]:
        recipe = {
            "schema_version": 1,
            "kind": "statistical",
            "name": "remote test",
            "plan": {},
            "experiments": [{"name": "ac"}],
        }
        plan_artifact = artifacts.canonical_bytes(
            {"schema_version": 2, "points": [{"index": 0}]},
            pretty=True,
            trailing_newline=True,
        )
        plan_sha256 = artifacts.sha256_digest(plan_artifact)
        preview = remote_execution.build_remote_preview(
            repository=remote_execution.DEFAULT_REPOSITORY,
            ref=remote_execution.DEFAULT_REF,
            plan_id=f"statistical-plan-{plan_sha256[:16]}",
            plan_sha256=plan_sha256,
            recipe_sha256=artifacts.definition_hash(recipe),
            plan_artifact=(
                f"runs/statistical-plans/statistical-plan-{plan_sha256[:16]}/"
                "statistical_plan.json"
            ),
            point_count=1,
            experiment_count=1,
            total_run_count=1,
        )
        experiments = [
            {
                "name": "ac",
                "filename": "circuit.cir",
                "netlist_template": "V1 in 0 AC 1\n.end\n",
                "waveform_analyses": [],
            }
        ]
        return preview, recipe, plan_artifact, experiments

    def test_envelope_round_trips_with_exact_identity(self) -> None:
        preview, recipe, plan_artifact, experiments = self._inputs()
        first = remote_execution.build_remote_envelope(
            preview=preview,
            recipe=recipe,
            plan_artifact=plan_artifact,
            experiments=experiments,
        )
        second = remote_execution.build_remote_envelope(
            preview=preview,
            recipe=recipe,
            plan_artifact=plan_artifact,
            experiments=experiments,
        )

        self.assertEqual(first, second)
        self.assertLessEqual(
            first["encoded_bytes"], remote_execution.MAX_REMOTE_ENVELOPE_ENCODED_BYTES
        )
        decoded = remote_execution.decode_remote_envelope(
            first["encoded"], first["envelope_sha256"]
        )
        self.assertEqual(decoded["preview"], preview)
        self.assertEqual(decoded["recipe"], recipe)
        self.assertEqual(decoded["experiments"], experiments)

    def test_envelope_rejects_mismatches_tampering_and_expansion(self) -> None:
        preview, recipe, plan_artifact, experiments = self._inputs()
        with self.assertRaisesRegex(ValueError, "recipe does not match"):
            remote_execution.build_remote_envelope(
                preview=preview,
                recipe={**recipe, "name": "changed"},
                plan_artifact=plan_artifact,
                experiments=experiments,
            )
        with self.assertRaisesRegex(ValueError, "experiment count"):
            remote_execution.build_remote_envelope(
                preview=preview,
                recipe=recipe,
                plan_artifact=plan_artifact,
                experiments=[*experiments, experiments[0]],
            )
        envelope = remote_execution.build_remote_envelope(
            preview=preview,
            recipe=recipe,
            plan_artifact=plan_artifact,
            experiments=experiments,
        )
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            remote_execution.decode_remote_envelope(
                envelope["encoded"], "0" * 64
            )
        oversized = base64.b64encode(
            zlib.compress(b"x" * (remote_execution.MAX_REMOTE_ENVELOPE_BYTES + 1))
        ).decode("ascii")
        with self.assertRaisesRegex(ValueError, "decoded size limit"):
            remote_execution.decode_remote_envelope(
                oversized, artifacts.sha256_digest(b"irrelevant")
            )
