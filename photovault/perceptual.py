"""Perceptual hashing: recognising the same photo through a different encoding.

Content hashing (hashing.py) answers "are these the same bytes?". That already
collapses exact copies at ingest. It says nothing about a photo that was
re-compressed by WhatsApp, exported at half resolution, or saved again by an
editor - those are different bytes and, to a content hash, unrelated files.

dHash (difference hash) answers the other question. Shrink the image to 9x8
grey pixels and record, for each of the 64 adjacent pairs, whether the left
pixel is brighter than the right. That survives re-compression, resizing and
mild colour shifts, because it encodes the *shape* of the brightness gradient
rather than any pixel value. Two photos are near-duplicates when their hashes
differ in only a few bits.

It is deliberately not a similarity search: dHash says "this is the same
picture", not "this is a similar picture". Burst shots of the same scene from
slightly different angles will not collapse, which is what you want when the
consequence is deletion.
"""

from __future__ import annotations

import struct
import subprocess
import zlib
from pathlib import Path

WIDTH, HEIGHT = 9, 8          # 9 columns gives 8 horizontal comparisons per row

try:
    from PIL import Image, ImageOps  # type: ignore

    _HAVE_PILLOW = True
except ImportError:
    _HAVE_PILLOW = False

import shutil

_HAVE_SIPS = shutil.which("sips") is not None


def backends() -> dict[str, bool]:
    return {"pillow": _HAVE_PILLOW, "sips": _HAVE_SIPS}


def available() -> bool:
    return _HAVE_PILLOW or _HAVE_SIPS


def dhash(path: Path) -> str | None:
    """64-bit difference hash as 16 hex characters, or None if undecodable."""
    try:
        grey = _grey_grid(path)
    except Exception:
        return None
    if grey is None or len(grey) != WIDTH * HEIGHT:
        return None

    bits = 0
    for row in range(HEIGHT):
        base = row * WIDTH
        for col in range(WIDTH - 1):
            bits = (bits << 1) | int(grey[base + col] > grey[base + col + 1])
    return f"{bits:016x}"


def is_degenerate(hex_hash: str) -> bool:
    """True for hashes carrying almost no information.

    A blank wall, a solid colour, or a smooth gradient produces a hash of all
    zeros or all ones - and those match each other perfectly while the images
    have nothing in common. Since the consequence here is deletion, such
    photos are excluded from grouping rather than silently clustered.
    """
    bits = bin(int(hex_hash, 16)).count("1")
    return bits <= 4 or bits >= 60


def distance(a: str, b: str) -> int:
    """Hamming distance between two hex dhashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _grey_grid(path: Path) -> list[int] | None:
    if _HAVE_PILLOW:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("L").resize(
                (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
            return list(im.getdata())
    if _HAVE_SIPS:
        return _grey_via_sips(path)
    return None


def _grey_via_sips(path: Path) -> list[int] | None:
    """macOS fallback: let sips do the decode and resize, then read the pixels.

    Keeps perceptual hashing working with no third-party dependency at all,
    which matters because this feature deletes photos - a user should not have
    to install a wheel to get the review UI.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tiny.png"
        r = subprocess.run(
            ["sips", "-s", "format", "png",
             "--resampleHeightWidth", str(HEIGHT), str(WIDTH),
             str(path), "--out", str(out)],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not out.exists():
            return None
        pixels = decode_png(out.read_bytes())
    if pixels is None:
        return None
    width, height, channels, data = pixels
    if width != WIDTH or height != HEIGHT:
        return None

    grey = []
    for i in range(0, len(data), channels):
        if channels == 1:
            grey.append(data[i])
        else:   # ITU-R 601 luma, the same weighting Pillow's "L" mode uses
            grey.append((data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) // 1000)
    return grey


def decode_png(blob: bytes) -> tuple[int, int, int, bytes] | None:
    """Minimal 8-bit PNG decoder: returns (width, height, channels, pixels).

    Only what sips emits needs supporting - 8-bit greyscale, RGB or RGBA, no
    interlacing - but all five scanline filters must be handled, because an
    encoder picks them per row and guessing wrong silently corrupts the hash.
    """
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, width = 8, bytearray(), None
    height = depth = ctype = interlace = 0

    while pos < len(blob) - 8:
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body[:13])
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break

    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if width is None or depth != 8 or channels is None or interlace:
        return None

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)

    for y in range(height):
        start = y * (stride + 1)
        ftype = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if ftype == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                up = prev[i]
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            return None
        out[y * stride:(y + 1) * stride] = line
        prev = line

    return width, height, channels, bytes(out)
