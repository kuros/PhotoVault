"""Best-effort capture-time extraction with no third-party dependencies.

Order of preference: EXIF DateTimeOriginal -> QuickTime/MP4 creation_time ->
a date parsed out of the filename -> filesystem mtime. The winning source is
recorded alongside the timestamp so a later pass can upgrade weak guesses.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "gif",
             "webp", "bmp", "dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2"}
VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "mkv", "3gp", "mts", "m2ts", "webm"}

# EXIF tag ids, in preference order.
_DATE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime

_FILENAME_PATTERNS = (
    re.compile(r"(?P<y>19\d{2}|20\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"),
    re.compile(r"(?P<y>19\d{2}|20\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])"),
)


def media_kind(ext: str) -> str:
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "other"


def normalize_ext(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return {"jpeg": "jpg", "tiff": "tif", "heif": "heic"}.get(ext, ext)


def capture_time(path: Path, ext: str) -> tuple[datetime | None, str]:
    """Return (naive local datetime, source label). Never raises on bad files."""
    for reader, label in ((_exif_date, "exif"), (_quicktime_date, "quicktime")):
        try:
            dt = reader(path, ext)
        except Exception:
            dt = None
        if dt and _plausible(dt):
            return dt, label

    dt = _filename_date(path.name)
    if dt and _plausible(dt):
        return dt, "filename"

    try:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        if _plausible(dt):
            return dt, "mtime"
    except OSError:
        pass
    return None, "unknown"


def _plausible(dt: datetime) -> bool:
    """Reject epoch-zero and far-future timestamps that would poison the layout."""
    return 1970 < dt.year <= datetime.now().year + 1


# --------------------------------------------------------------------------- EXIF

def _exif_date(path: Path, ext: str) -> datetime | None:
    """Parse a TIFF IFD0/ExifIFD out of JPEG APP1, a bare TIFF, or a HEIC meta box."""
    with open(path, "rb") as fh:
        head = fh.read(4)
        if head[:2] == b"\xff\xd8":  # JPEG
            blob = _jpeg_exif_segment(fh)
        elif head[:2] in (b"II", b"MM"):  # TIFF / many raw formats
            fh.seek(0)
            blob = fh.read(256 * 1024)
        elif ext in ("heic", "heif"):
            blob = _scan_for_exif(fh)
        else:
            return None
    if not blob:
        return None
    return _parse_tiff_dates(blob)


def _jpeg_exif_segment(fh) -> bytes | None:
    fh.seek(2)
    while True:
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        if marker[1] in (0xD8, 0xD9, 0xDA):  # SOI / EOI / start of scan
            return None
        length = struct.unpack(">H", fh.read(2))[0] - 2
        payload = fh.read(length)
        if marker[1] == 0xE1 and payload[:6] == b"Exif\x00\x00":
            return payload[6:]


def _scan_for_exif(fh, window: int = 8 * 1024 * 1024) -> bytes | None:
    """Find the TIFF block inside a HEIC/HEIF container.

    Do NOT search for "Exif\\0\\0": in HEIF that string appears in the `infe`
    box that *names* the item, typically ~1 KB in, while the payload it refers
    to sits much further along (19 KB later in a real iPhone file). Reading
    from the name gives you container structure, the TIFF parse fails, and the
    date silently falls back to the file's mtime - which for a downloaded file
    is the moment it was downloaded.

    Locating it properly means walking meta/iinf/iloc. Scanning for the TIFF
    magic instead is a fraction of the code and self-validating: a false match
    has to survive the version check, an IFD walk, and a plausibility test on
    the date before it is believed.
    """
    fh.seek(0)
    buf = fh.read(window)
    for magic in (b"MM\x00*", b"II*\x00"):
        start = 0
        while (idx := buf.find(magic, start)) != -1:
            candidate = _parse_tiff_dates(buf[idx:])
            if candidate is not None and _plausible(candidate):
                return buf[idx:]
            start = idx + 1
    return None


def _parse_tiff_dates(blob: bytes) -> datetime | None:
    if len(blob) < 8:
        return None
    endian = "<" if blob[:2] == b"II" else ">" if blob[:2] == b"MM" else None
    if endian is None or struct.unpack(endian + "H", blob[2:4])[0] != 42:
        return None

    found: dict[int, str] = {}

    def walk(offset: int, depth: int = 0) -> None:
        if depth > 2 or not (0 <= offset < len(blob) - 2):
            return
        count = struct.unpack(endian + "H", blob[offset:offset + 2])[0]
        for i in range(count):
            e = offset + 2 + i * 12
            if e + 12 > len(blob):
                return
            tag, typ, n = struct.unpack(endian + "HHI", blob[e:e + 8])
            if tag == 34665:  # ExifIFDPointer
                walk(struct.unpack(endian + "I", blob[e + 8:e + 12])[0], depth + 1)
            elif tag in _DATE_TAGS and typ == 2 and n >= 19:
                val = struct.unpack(endian + "I", blob[e + 8:e + 12])[0]
                raw = blob[val:val + 19]
                if len(raw) == 19:
                    found.setdefault(tag, raw.decode("ascii", "ignore"))

    walk(struct.unpack(endian + "I", blob[4:8])[0])

    for tag in _DATE_TAGS:
        if tag in found:
            try:
                return datetime.strptime(found[tag], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    return None


# ------------------------------------------------------------- QuickTime / MP4

_MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _quicktime_date(path: Path, ext: str) -> datetime | None:
    """Read creation_time from the movie header (mvhd) atom."""
    if ext not in VIDEO_EXT:
        return None
    with open(path, "rb") as fh:
        moov = _find_atom(fh, b"moov", 0, path.stat().st_size)
        if not moov:
            return None
        start, end = moov
        mvhd = _find_atom(fh, b"mvhd", start, end)
        if not mvhd:
            return None
        fh.seek(mvhd[0])
        version = fh.read(1)[0]
        fh.read(3)  # flags
        raw = fh.read(8 if version == 1 else 4)
        secs = struct.unpack(">Q" if version == 1 else ">I", raw)[0]

    if secs == 0:
        return None
    # mvhd times are UTC; convert to local so the on-disk layout matches
    # what the user remembers taking.
    utc = _MAC_EPOCH + timedelta(seconds=secs)
    return utc.astimezone().replace(tzinfo=None)


def _find_atom(fh, want: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Return (payload start, payload end) of the first `want` atom in a range."""
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return None
        size = struct.unpack(">I", header[:4])[0]
        name = header[4:8]
        body = pos + 8
        if size == 1:  # 64-bit extended size
            size = struct.unpack(">Q", fh.read(8))[0]
            body += 8
        elif size == 0:
            size = end - pos
        if size < 8:
            return None
        if name == want:
            return body, pos + size
        pos += size
    return None


def _filename_date(name: str) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                return datetime(int(m["y"]), int(m["m"]), int(m["d"]), 12, 0, 0)
            except ValueError:
                continue
    return None
