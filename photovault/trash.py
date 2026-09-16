"""Deleting photos, and the safety net that makes it survivable.

Every other destructive operation in PhotoVault removes a *redundant* copy:
rebalance drops an over-replicated one, reclaim clears a phone original the
archive already holds, duplicates removes a near-twin while a keeper survives.
Each is guarded by proving enough copies remain.

This one is different, and the difference matters. Here the user asks for the
photo itself to go. There is no surviving copy to verify against, and no
technical check can distinguish "delete this, I meant it" from "delete this,
I misclicked". So the guard cannot be a proof - it has to be *time*.

Deleting therefore marks the asset and leaves every file exactly where it is.
The photo vanishes from the library, from the health report and from the
duplicate scanner, but all its copies remain intact and replicated until it is
purged. Restoring is a flag flip, not a recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .catalog import Catalog
from .config import Config
from .replicas import ReplicaError, driver_for


@dataclass
class TrashStats:
    moved: int = 0
    bytes: int = 0
    missing: list[str] = field(default_factory=list)


@dataclass
class PurgeReport:
    purged: int = 0
    bytes_freed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def delete(catalog: Catalog, hashes: list[str]) -> TrashStats:
    """Move photos to the trash. Nothing on disk changes."""
    stats = TrashStats()
    with catalog.tx():
        for h in hashes:
            asset = catalog.asset(h)
            if asset is None:
                stats.missing.append(h)
                continue
            if asset["deleted_at"]:
                continue
            catalog.soft_delete(h)
            stats.moved += 1
            stats.bytes += asset["size"]
    if stats.moved:
        catalog.log("trash", f"moved {stats.moved} photos to the trash")
    return stats


def restore(catalog: Catalog, hashes: list[str]) -> int:
    restored = 0
    with catalog.tx():
        for h in hashes:
            asset = catalog.asset(h)
            if asset is not None and asset["deleted_at"]:
                catalog.undelete(h)
                restored += 1
    if restored:
        catalog.log("restore", f"restored {restored} photos from the trash")
    return restored


def due_for_purge(cfg: Config, catalog: Catalog):
    """Trashed photos older than the retention window."""
    cutoff = (datetime.now() - timedelta(days=cfg.trash_days)).isoformat(
        timespec="seconds")
    return catalog.trashed(older_than=cutoff)


def purge(cfg: Config, catalog: Catalog, *, hashes: list[str] | None = None,
          expired_only: bool = True, dry_run: bool = True,
          progress=None) -> PurgeReport:
    """Permanently remove trashed photos from every replica.

    This is the point of no return, so it refuses to act on a replica it cannot
    currently reach. Purging while the offline drive is in a drawer would leave
    the file on that drive while the catalog forgets it exists - a photo that
    is neither in your library nor cleanly gone, which would reappear as an
    orphan the next time you rebuilt the catalog from that drive.
    """
    report = PurgeReport()

    if hashes is not None:
        rows = [catalog.asset(h) for h in hashes]
        rows = [r for r in rows if r is not None and r["deleted_at"]]
    else:
        rows = due_for_purge(cfg, catalog) if expired_only else catalog.trashed()
    if not rows:
        return report

    drivers, unreachable = {}, []
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers[spec.name] = drv
            else:
                unreachable.append(spec.name)
        except Exception:
            unreachable.append(spec.name)

    if unreachable:
        for row in rows:
            report.skipped.append(
                (row["rel_path"],
                 f"not every device is connected ({', '.join(unreachable)})"))
        return report

    for i, row in enumerate(rows, 1):
        if dry_run:
            report.purged += 1
            report.bytes_freed += row["size"]
            continue

        failed = False
        for name, drv in drivers.items():
            try:
                drv.delete(row["rel_path"])
            except (OSError, ReplicaError) as exc:
                report.errors.append(f"{name}:{row['rel_path']}: {exc}")
                failed = True
        if failed:
            continue

        with catalog.tx():
            catalog.remove_asset(row["hash"])
        catalog.log("purge", row["rel_path"])
        report.purged += 1
        report.bytes_freed += row["size"]
        if progress and i % 25 == 0:
            progress(i, len(rows))

    if not dry_run and report.purged:
        catalog.log("purge-run", f"permanently deleted {report.purged} photos")
    return report


def summary(cfg: Config, catalog: Catalog) -> dict:
    s = catalog.trash_summary()
    expiring = len(due_for_purge(cfg, catalog))
    s["expiring"] = expiring
    s["retention_days"] = cfg.trash_days
    return s
