#!/usr/bin/env python3
"""Packaged Windows entry point for LTspice System Builder."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

import system_builder

WORKSPACE_NAME = "LTspice System Builder Workspace"
STARTER_FILES = (
    Path("examples/mixed_signal_daq_ac.cir"),
    Path("examples/mixed_signal_daq_transient.cir"),
    Path("examples/mixed_signal_daq.asc"),
    Path("docs/images/mixed-signal-daq-schematic.png"),
)


def default_workspace() -> Path:
    """Return the writable workspace used by the packaged application."""
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    return profile / "Documents" / WORKSPACE_NAME


def seed_workspace(workspace: Path, resource_root: Path) -> None:
    """Copy starter circuit files without replacing user-owned files."""
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in STARTER_FILES:
        source = resource_root / relative
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source, destination)


def packaged_arguments(arguments: Sequence[str]) -> list[str]:
    """Supply and initialize the packaged default workspace when omitted."""
    resolved = list(arguments)
    if any(value == "--workspace" or value.startswith("--workspace=") for value in resolved):
        return resolved
    workspace = default_workspace()
    seed_workspace(workspace, system_builder.PROJECT_ROOT)
    return ["--workspace", str(workspace), *resolved]


def main() -> None:
    sys.argv[1:] = packaged_arguments(sys.argv[1:])
    system_builder.main()


if __name__ == "__main__":
    main()
