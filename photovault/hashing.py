"""Content hashing. sha256 by default; blake3 when the optional wheel is present."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024

try:  # optional, ~5x faster on large libraries
    import blake3  # type: ignore

    ALGO = "blake3"

    def _new():
        return blake3.blake3()

except ImportError:
    ALGO = "sha256"

    def _new():
        return hashlib.sha256()


def hash_file(path: Path) -> tuple[str, int]:
    """Return (hex digest, byte size). One pass, constant memory."""
    h = _new()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def quick_signature(path: Path) -> str:
    """Cheap pre-filter: size + first and last 64KiB.

    Used to skip full hashing of files we have almost certainly seen before.
    Never used as an identity on its own.
    """
    st = path.stat()
    h = _new()
    h.update(str(st.st_size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(65536))
        if st.st_size > 131072:
            fh.seek(-65536, 2)
            h.update(fh.read(65536))
    return h.hexdigest()
