"""Repairing capture dates that were read wrongly, and the paths built from them.

A photo whose metadata could not be parsed falls back to the file's mtime, and
for anything downloaded that is the moment it was downloaded. The wrong date
then propagates: it decides the canonical path, so the photo is filed under the
wrong year and its filename encodes the wrong day.

Re-reading is therefore not enough - the file has to move, on every device, and
the catalog has to follow. That is why this needs all devices connected, like
purge: a rename recorded in the catalog but not performed on an absent drive
leaves that drive holding a file nobody will look for again.

One trap this must avoid: capture_time() falls back to parsing a date out of
the *filename*, and by this point the filename is one PhotoVault generated from
the bad date. Trusting it would confidently re-derive the same wrong answer. So
only the file's own embedded metadata counts here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .ingest import canonical_rel_path
from .mediatime import _exif_date, _quicktime_date, _plausible
from .replicas import LocalDriver, ReplicaError, driver_for

# Dates from these sources are guesses and worth re-examining. 'exif' and
# 'quicktime' came from the file itself and are left alone.
WEAK_SOURCES = ("mtime", "filename", "unknown")


@dataclass
class Candidate:
    hash: str
    old_path: str
    new_path: str
    old_when: str | None
    new_when: datetime
    source: str


@dataclass
class RedateReport:
    examined: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    moved: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blocked_by


def embedded_date(path: Path, ext: str) -> tuple[datetime | None, str]:
    """Capture time from the file's own metadata only - never the filename."""
    for reader, label in ((_exif_date, "exif"), (_quicktime_date, "quicktime")):
        try:
            when = reader(path, ext)
        except Exception:
            when = None
        if when and _plausible(when):
            return when, label
    return None, ""


def find_from_originals(cfg: Config, catalog: Catalog, folder: Path,
                        *, limit: int | None = None) -> RedateReport:
    """Recover dates from a folder of original files, matched by content.

    Some photos have no embedded date at all - scans and screenshots in
    particular - so the file's own timestamp is the only date that ever
    existed. If that was lost on the way in, the stored copy cannot be
    repaired from itself: the information is simply not in the bytes.

    It is still in the originals, though, and content hashing makes the join
    exact. Every file here is matched by hash, so a file that merely looks
    similar can never contribute a date to the wrong photo.
    """
    from .hashing import hash_file
    from .ingest import WANTED_EXT, iter_media
    from .mediatime import normalize_ext

    report = RedateReport()
    weak = {r["hash"]: r for r in catalog.db.execute(
        "SELECT hash, rel_path, ext, captured_at, time_source FROM asset "
        f"WHERE time_source IN ({','.join('?' * len(WEAK_SOURCES))}) "
        "AND deleted_at IS NULL", WEAK_SOURCES).fetchall()}

    for path in iter_media(folder):
        if normalize_ext(path) not in WANTED_EXT:
            continue
        report.examined += 1
        try:
            digest, _ = hash_file(path)
        except OSError:
            continue
        row = weak.get(digest)
        if row is None:
            continue

        when, label = embedded_date(path, row["ext"])
        if when is None:
            try:
                when, label = datetime.fromtimestamp(path.stat().st_mtime), "mtime"
            except OSError:
                continue
        if not _plausible(when):
            continue

        new_rel = canonical_rel_path(digest, row["ext"], when, catalog)
        if new_rel == row["rel_path"]:
            report.unchanged += 1
            continue
        report.candidates.append(Candidate(
            hash=digest, old_path=row["rel_path"], new_path=new_rel,
            old_when=row["captured_at"], new_when=when, source=label))
        if limit and len(report.candidates) >= limit:
            break
    return report


def find(cfg: Config, catalog: Catalog, *, limit: int | None = None) -> RedateReport:
    """Which photos have a better date available than the one on record?"""
    report = RedateReport()
    primary = LocalDriver(cfg.replica(cfg.primary))
    if not primary.available():
        report.blocked_by.append(cfg.primary)
        return report

    q = ("SELECT hash, rel_path, ext, captured_at, time_source FROM asset "
         f"WHERE time_source IN ({','.join('?' * len(WEAK_SOURCES))}) "
         "AND deleted_at IS NULL ORDER BY rel_path")
    rows = catalog.db.execute(q, WEAK_SOURCES).fetchall()
    if limit:
        rows = rows[:limit]

    for row in rows:
        report.examined += 1
        src = primary.root / row["rel_path"]
        if not src.is_file():
            continue
        when, label = embedded_date(src, row["ext"])
        if when is None:
            continue
        new_rel = canonical_rel_path(row["hash"], row["ext"], when, catalog)
        if new_rel == row["rel_path"]:
            report.unchanged += 1
            continue
        report.candidates.append(Candidate(
            hash=row["hash"], old_path=row["rel_path"], new_path=new_rel,
            old_when=row["captured_at"],
            new_when=when, source=label))
    return report


def apply(cfg: Config, catalog: Catalog, report: RedateReport, *,
          dry_run: bool = True, progress=None) -> RedateReport:
    """Move the files and update the catalog, or report why it cannot."""
    drivers = {}
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers[spec.name] = drv
            else:
                report.blocked_by.append(spec.name)
        except Exception:
            report.blocked_by.append(spec.name)

    if report.blocked_by:
        return report
    if dry_run:
        return report

    for i, cand in enumerate(report.candidates, 1):
        moved_on = []
        failed = False
        for name, drv in drivers.items():
            if not isinstance(drv, LocalDriver):
                # A remote replica is re-synced rather than renamed in place:
                # simpler, and the bytes are already there to copy from.
                continue
            src = drv.root / cand.old_path
            dest = drv.root / cand.new_path
            if dest.exists():
                continue
            if not src.is_file():
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dest)
                moved_on.append(name)
                _prune_empty(src.parent, drv.root)
            except OSError as exc:
                report.errors.append(f"{name}:{cand.old_path}: {exc}")
                failed = True
        if failed and not moved_on:
            continue

        with catalog.tx():
            catalog.db.execute(
                "UPDATE asset SET rel_path = ?, captured_at = ?, time_source = ? "
                "WHERE hash = ?",
                (cand.new_path, cand.new_when.isoformat(timespec="seconds"),
                 cand.source, cand.hash))
            # Any replica we could not rename no longer holds the new path.
            for spec in cfg.replicas:
                if spec.name not in moved_on:
                    catalog.set_placement(cand.hash, spec.name, "missing")
        report.moved += 1
        if progress and i % 25 == 0:
            progress(report.moved, len(report.candidates))

    if report.moved:
        catalog.log("redate", f"re-dated and moved {report.moved} photos")
    return report


def _prune_empty(directory: Path, root: Path) -> None:
    while directory != root and directory.is_dir():
        try:
            if any(directory.iterdir()):
                return
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent
