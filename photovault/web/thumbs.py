"""Thumbnail generation and caching.

Full-size photos are 3-10 MB each; a grid of 200 of them is a gigabyte the
browser has to download to show postage stamps. So we generate small copies
once and cache them, keyed by content hash. Because the hash IS the identity,
a cached thumbnail can never go stale - different bytes mean a different key.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "photovault" / "thumbs"
SIZE = 512

# Backends in order of preference. Pillow is fastest and cross-platform; sips
# ships with macOS; ffmpeg is the only one that can pull a frame from a video.
try:
    from PIL import Image, ImageOps  # type: ignore

    _HAVE_PILLOW = True
except ImportError:
    _HAVE_PILLOW = False

_HAVE_SIPS = shutil.which("sips") is not None
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def backends() -> dict[str, bool]:
    return {"pillow": _HAVE_PILLOW, "sips": _HAVE_SIPS, "ffmpeg": _HAVE_FFMPEG}


def available() -> bool:
    return _HAVE_PILLOW or _HAVE_SIPS


def cache_path(hash_: str) -> Path:
    # Two-level fan-out: a single directory with 300k files is slow to list on
    # most filesystems, and painful to inspect by hand.
    return CACHE_DIR / hash_[:2] / f"{hash_}.jpg"


def get_or_make(hash_: str, src: Path, kind: str = "image") -> Path | None:
    """Return a cached thumbnail path, generating it on first request."""
    dest = cache_path(hash_)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if not src.is_file():
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.jpg")
    try:
        ok = _make_video(src, tmp) if kind == "video" else _make_image(src, tmp)
        if ok and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dest)  # atomic: a half-written thumb is never served
            return dest
    except Exception:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return None


def _make_image(src: Path, dest: Path) -> bool:
    if _HAVE_PILLOW:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)  # honour the camera's rotation flag
            im.thumbnail((SIZE, SIZE))
            im.convert("RGB").save(dest, "JPEG", quality=82, optimize=True)
        return True
    if _HAVE_SIPS:
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", str(SIZE),
             str(src), "--out", str(dest)],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    return False


def _make_video(src: Path, dest: Path) -> bool:
    """Grab a frame one second in - frame zero is often a black fade-in."""
    if not _HAVE_FFMPEG:
        return False
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(src),
         "-frames:v", "1", "-vf", f"scale={SIZE}:-1", str(dest)],
        capture_output=True, timeout=120,
    )
    return r.returncode == 0


def cache_stats() -> dict:
    if not CACHE_DIR.exists():
        return {"count": 0, "bytes": 0}
    count = total = 0
    for p in CACHE_DIR.rglob("*.jpg"):
        count += 1
        total += p.stat().st_size
    return {"count": count, "bytes": total}


def clear_cache() -> int:
    n = cache_stats()["count"]
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    return n
