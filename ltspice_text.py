"""Encoding detection for LTspice text artifacts."""

from __future__ import annotations


def text_encoding(raw: bytes) -> str:
    """Identify UTF-8 or little-endian UTF-16 from BOMs and NUL placement."""
    if raw.startswith(b"\xff\xfe"):
        return "utf-16le"
    if raw.startswith(b"\xfe\xff"):
        raise UnicodeError("big-endian UTF-16 LTspice text is not supported")
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8"

    # The prefix is enough to distinguish LTspice's ASCII-style header while
    # avoiding NUL-heavy binary RAW payloads that may begin later in the file.
    probe = raw[:64]
    pair_count = len(probe) // 2
    if pair_count >= 4:
        even_nuls = probe[0 : pair_count * 2 : 2].count(0)
        odd_nuls = probe[1 : pair_count * 2 : 2].count(0)
        if odd_nuls >= 4 and odd_nuls >= max(1, even_nuls) * 4:
            return "utf-16le"
    return "utf-8"


def decode_text(raw: bytes) -> str:
    """Decode supported LTspice text and remove an optional Unicode BOM."""
    encoding = text_encoding(raw)
    return raw.decode(encoding).removeprefix("\ufeff")
