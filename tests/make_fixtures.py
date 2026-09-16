"""Build synthetic media files for testing, including real EXIF headers."""

from __future__ import annotations

import struct
from pathlib import Path


def jpeg_with_exif(date: str | None, payload: bytes) -> bytes:
    """A structurally valid JPEG whose APP1 segment carries DateTimeOriginal."""
    out = bytearray(b"\xff\xd8")
    if date:
        tiff = _tiff_with_datetime(date)
        seg = b"Exif\x00\x00" + tiff
        out += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    # A comment segment stands in for image data; content differs per file so
    # each fixture gets a distinct hash.
    out += b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload
    out += b"\xff\xd9"
    return bytes(out)


def _tiff_with_datetime(date: str) -> bytes:
    """Little-endian TIFF: IFD0 holds one ExifIFD pointer; ExifIFD holds tag 36867."""
    header = b"II" + struct.pack("<HI", 42, 8)
    ifd0_off = 8
    ifd0 = struct.pack("<H", 1) + struct.pack("<HHII", 34665, 4, 1, 0) + struct.pack("<I", 0)
    exif_off = ifd0_off + len(ifd0)
    value_off = exif_off + 2 + 12 + 4
    exif_ifd = (struct.pack("<H", 1)
                + struct.pack("<HHII", 36867, 2, 20, value_off)
                + struct.pack("<I", 0))
    ifd0 = struct.pack("<H", 1) + struct.pack("<HHII", 34665, 4, 1, exif_off) + struct.pack("<I", 0)
    return header + ifd0 + exif_ifd + date.encode() + b"\x00"


def build(root: Path, count: int = 12) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(count):
        date = f"2024:0{i % 9 + 1}:1{i % 9} 1{i % 9}:30:00"
        p = root / f"IMG_{1000 + i}.jpg"
        p.write_bytes(jpeg_with_exif(date, f"photo-payload-{i}".encode() * 40))
        made.append(p)

    # A file with no EXIF but a date in the name.
    p = root / "scan_2011-07-04_beach.jpg"
    p.write_bytes(jpeg_with_exif(None, b"no-exif-dated-filename" * 40))
    made.append(p)

    # An exact duplicate under a different name, to exercise dedup.
    dup = root / "copies" / "IMG_1000_copy.jpg"
    dup.parent.mkdir(exist_ok=True)
    dup.write_bytes((root / "IMG_1000.jpg").read_bytes())
    made.append(dup)

    # Junk that must be ignored.
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "notes.txt").write_text("not a photo")
    (root / "Thumbnails").mkdir(exist_ok=True)
    (root / "Thumbnails" / "IMG_1000.jpg").write_bytes(b"thumb")
    return made


if __name__ == "__main__":
    import sys
    made = build(Path(sys.argv[1]))
    print(f"created {len(made)} media files")
