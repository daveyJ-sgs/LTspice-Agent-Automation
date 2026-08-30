"""Shared primitives for deterministic, content-addressed artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path


def canonical_json(value: object, *, pretty: bool = False) -> str:
    """Serialize JSON deterministically without accepting non-finite numbers."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def canonical_bytes(
    value: object, *, pretty: bool = False, trailing_newline: bool = False
) -> bytes:
    text = canonical_json(value, pretty=pretty)
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def sha256_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def definition_hash(value: object) -> str:
    return sha256_digest(canonical_bytes(value))


def content_address(prefix: str, content: bytes) -> tuple[str, str]:
    digest = sha256_digest(content)
    return f"{prefix}-{digest[:16]}", digest


def write_once(path: Path, content: bytes) -> None:
    """Atomically create an artifact, accepting only an identical existing file."""
    if path.is_symlink():
        raise ValueError(f"artifact target must not be a symlink: {path.name}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"existing artifact differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_verified(path: Path, expected_sha256: str) -> bytes:
    """Read a regular artifact only when its complete SHA-256 digest matches."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular file: {path}")
    content = path.read_bytes()
    actual_sha256 = sha256_digest(content)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"artifact SHA-256 does not match: {path}")
    return content
