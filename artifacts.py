"""Shared primitives for deterministic, content-addressed artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import time
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
        # Publish a complete file without replacing a concurrent writer's result.
        try:
            os.link(temporary, path)
        except FileExistsError:
            _accept_existing(path, content)
        except OSError:
            # exFAT, FAT32 and some network shares have no hard links.
            _publish_without_link(temporary, path, content)
    finally:
        temporary.unlink(missing_ok=True)


# Windows os.rename never replaces an existing file (it raises
# FileExistsError), which makes it an atomic no-overwrite publication. POSIX
# rename silently replaces, so it cannot be used there.
_RENAME_REFUSES_OVERWRITE = os.name == "nt"
_CONCURRENT_WRITER_WAIT_SECONDS = 5.0


def _accept_existing(path: Path, content: bytes, *, wait_for_writer: bool = False) -> None:
    """Accept an existing artifact only when it is a regular, identical file."""
    deadline = time.monotonic() + (_CONCURRENT_WRITER_WAIT_SECONDS if wait_for_writer else 0)
    while True:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"existing artifact differs: {path}")
        existing = path.read_bytes()
        if existing == content:
            return
        # Without hard links a concurrent writer's file is visible while it is
        # still being written; wait briefly while it could still become ours.
        if (
            len(existing) < len(content)
            and content.startswith(existing)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            continue
        raise ValueError(f"existing artifact differs: {path}")


def _publish_without_link(temporary: Path, path: Path, content: bytes) -> None:
    """Publish without replacement on a filesystem that refuses hard links."""
    if _RENAME_REFUSES_OVERWRITE:
        try:
            os.rename(temporary, path)
        except FileExistsError:
            _accept_existing(path, content)
        return
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o666)
    except FileExistsError:
        _accept_existing(path, content, wait_for_writer=True)
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Never leave a truncated artifact that would poison later writes.
        path.unlink(missing_ok=True)
        raise


def read_verified(path: Path, expected_sha256: str) -> bytes:
    """Read a regular artifact only when its complete SHA-256 digest matches."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular file: {path}")
    content = path.read_bytes()
    actual_sha256 = sha256_digest(content)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"artifact SHA-256 does not match: {path}")
    return content
