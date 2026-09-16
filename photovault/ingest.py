"""Scanning sources and importing new photos into the primary library.

Ingest is strictly additive and read-only with respect to sources: it copies
files out and never renames, moves or deletes anything you point it at. Import
is idempotent - running it twice over the same folder is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .hashing import hash_file
from .mediatime import IMAGE_EXT, VIDEO_EXT, capture_time, media_kind, normalize_ext
from .replicas import LocalDriver

WANTED_EXT = IMAGE_EXT | VIDEO_EXT

# Apple Photos keeps edits, thumbnails and caches beside the originals. Only
# `originals/` holds the untouched files, so everything else is noise.
SKIP_DIR_PARTS = {
    "resources", "database", "caches", "thumbnails", "private",
    ".photoslibrary", "external", "scopes", ".git", "@eaDir",
    "#recycle", "$RECYCLE.BIN", "System Volume Information",
}
SKIP_NAME_PREFIX = (".", "._")


@dataclass
class IngestStats:
    scanned: int = 0
    skipped: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0
    bytes_imported: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        gb = self.bytes_imported / 1e9
        return (f"scanned {self.scanned}  imported {self.imported} ({gb:.2f} GB)  "
                f"duplicates {self.duplicates}  skipped {self.skipped}  failed {self.failed}")


def iter_media(root: Path):
    """Yield candidate media files under root, pruning known-junk directories."""
    if not root.exists():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(SKIP_NAME_PREFIX):
                continue
            if entry.is_dir():
                if name.lower() not in SKIP_DIR_PARTS:
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def canonical_rel_path(hash_: str, ext: str, captured, catalog: Catalog) -> str:
    """Date-organised and stable: the same content always lands at the same path.

    A hash suffix makes the name unique without needing a counter, which keeps
    the layout identical across replicas no matter what order things import in.
    """
    short = hash_[:10]
    if captured is None:
        return f"unknown/{short[:2]}/{short}.{ext}"
    return (f"{captured.year:04d}/{captured.month:02d}/"
            f"{captured:%Y%m%d-%H%M%S}_{short}.{ext}")


def ingest_source(cfg: Config, catalog: Catalog, device: str, source_root: Path,
                  *, dry_run: bool = False, progress=None) -> IngestStats:
    stats = IngestStats()
    primary = LocalDriver(cfg.replica(cfg.primary))
    if not dry_run:
        primary.ensure_root()
    # A dry run writes nothing, so the catalog cannot tell us about duplicates
    # found earlier in this same pass. Track them here instead.
    seen_this_run: set[str] = set()

    for path in iter_media(source_root):
        ext = normalize_ext(path)
        if ext not in WANTED_EXT:
            stats.skipped += 1
            continue
        stats.scanned += 1
        if progress and stats.scanned % 200 == 0:
            progress(stats)

        try:
            hash_, size = hash_file(path)
        except OSError as exc:
            stats.failed += 1
            stats.errors.append(f"{path}: {exc}")
            continue

        if catalog.has_asset(hash_) or hash_ in seen_this_run:
            stats.duplicates += 1
            if not dry_run:
                catalog.record_source(hash_, device, str(path))
            continue
        seen_this_run.add(hash_)

        captured, time_source = capture_time(path, ext)
        rel = canonical_rel_path(hash_, ext, captured, catalog)

        if dry_run:
            stats.imported += 1
            stats.bytes_imported += size
            continue

        try:
            primary.put(path, rel)
        except OSError as exc:
            stats.failed += 1
            stats.errors.append(f"{path} -> {rel}: {exc}")
            continue

        with catalog.tx():
            catalog.add_asset(
                hash_=hash_, size=size, ext=ext, media_kind=media_kind(ext),
                captured_at=captured.isoformat(timespec="seconds") if captured else None,
                time_source=time_source, rel_path=rel,
            )
            catalog.record_source(hash_, device, str(path))
            catalog.set_placement(hash_, cfg.primary, "present", verified=True)

        stats.imported += 1
        stats.bytes_imported += size

    if not dry_run:
        catalog.log("ingest", f"{device}:{source_root} -> {stats.summary()}")
    return stats
