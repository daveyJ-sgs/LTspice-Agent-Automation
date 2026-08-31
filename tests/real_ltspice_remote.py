#!/usr/bin/env python3
"""Verify or execute one System Builder remote-study envelope."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import remote_execution
import remote_study
import statistical_engine


def _document() -> dict[str, object]:
    encoded = os.environ.get("REMOTE_ENVELOPE")
    expected_sha256 = os.environ.get("REMOTE_ENVELOPE_SHA256")
    document = remote_execution.decode_remote_envelope(encoded, expected_sha256)
    return remote_study.validate_remote_document(document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()
    document = _document()
    preview = document["preview"]
    plan = document["plan"]
    assert isinstance(preview, dict)
    assert isinstance(plan, dict)
    if arguments.validate_only:
        with tempfile.TemporaryDirectory() as temporary:
            runs_dir = Path(temporary)
            published = statistical_engine.save_statistical_plan(
                runs_dir, plan  # type: ignore[arg-type]
            )
            statistical_engine.load_statistical_plan(
                runs_dir, str(published["plan_id"])
            )
        print(
            json.dumps(
                {
                    "status": "valid",
                    "preview_id": preview["preview_id"],
                    "plan_id": published["plan_id"],
                },
                sort_keys=True,
            )
        )
        return
    evidence_value = os.environ.get("REAL_LTSPICE_REMOTE_EVIDENCE_DIR")
    if not evidence_value:
        raise RuntimeError("REAL_LTSPICE_REMOTE_EVIDENCE_DIR is required")
    summary = remote_study.run_remote_study(document, Path(evidence_value).resolve())
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
