from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import artifacts

FIXTURE = Path(__file__).with_name("fixtures") / "canonical_artifact.json"


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_canonical_representations_match_committed_fixture(self) -> None:
        value = self.fixture["value"]
        compact = artifacts.canonical_json(value)
        pretty = artifacts.canonical_json(value, pretty=True)

        self.assertEqual(compact, self.fixture["compact"])
        self.assertEqual(
            artifacts.sha256_digest(compact.encode("utf-8")),
            self.fixture["compact_sha256"],
        )
        self.assertEqual(
            artifacts.sha256_digest(pretty.encode("utf-8")),
            self.fixture["pretty_sha256"],
        )
        self.assertEqual(artifacts.definition_hash(value), self.fixture["compact_sha256"])
        self.assertEqual(
            artifacts.content_address("artifact", compact.encode("utf-8")),
            ("artifact-ae4ed71bf3c06b36", self.fixture["compact_sha256"]),
        )

    def test_write_once_and_strict_verified_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            content = artifacts.canonical_bytes(self.fixture["value"], pretty=True)
            digest = artifacts.sha256_digest(content)

            artifacts.write_once(path, content)
            artifacts.write_once(path, content)
            self.assertEqual(artifacts.read_verified(path, digest), content)

            with self.assertRaisesRegex(ValueError, "existing artifact differs"):
                artifacts.write_once(path, b"different")

    def test_spaced_noncanonical_form_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            spaced = json.dumps(
                self.fixture["value"], sort_keys=True, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            path.write_bytes(spaced)

            with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
                artifacts.read_verified(path, self.fixture["compact_sha256"])


if __name__ == "__main__":
    unittest.main()
