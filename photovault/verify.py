"""Scrubbing: proving the bytes are still the bytes, and repairing them when not.

Silent corruption is the failure mode backups usually miss. A file can rot on
disk while every copy tool faithfully replicates the damage. The only defence
is periodically reading everything back and comparing it against a hash taken
when the file was known good.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .catalog import Catalog, now
from .config import Config
from .replicas import ReplicaError, driver_for


@dataclass
class ScrubStats:
    checked: int = 0
    ok: int = 0
    corrupt: int = 0
    vanished: int = 0
    repaired: int = 0
    unrepairable: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"checked {self.checked}  ok {self.ok}  corrupt {self.corrupt}  "
                f"missing {self.vanished}  repaired {self.repaired}  "
                f"unrepairable {self.unrepairable}")


def due_for_scrub(catalog: Catalog, cfg: Config, limit: int | None = None,
                  force: bool = False):
    """Placements never verified, or verified longer ago than the scrub interval.

    `force` ignores the schedule and re-reads everything. Worth having because
    a copy verified at write time is not re-checked until the interval elapses,
    and after a scare you want an answer now, not in three weeks.
    """
    cutoff = ("9999-12-31" if force else
              (datetime.now() - timedelta(days=cfg.scrub_days)).isoformat(timespec="seconds"))
    q = """SELECT p.hash, p.replica, a.rel_path, a.size
           FROM placement p JOIN asset a ON a.hash = p.hash
           WHERE p.state = 'present'
             AND (p.verified_at IS NULL OR p.verified_at < ?)
           ORDER BY COALESCE(p.verified_at, '')"""
    params: list = [cutoff]
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    return catalog.db.execute(q, params).fetchall()


def scrub(cfg: Config, catalog: Catalog, *, limit: int | None = None,
          repair: bool = True, force: bool = False, progress=None) -> ScrubStats:
    stats = ScrubStats()
    drivers = {}
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers[spec.name] = drv
        except Exception:
            continue

    rows = [r for r in due_for_scrub(catalog, cfg, limit, force)
            if r["replica"] in drivers]

    for i, row in enumerate(rows, 1):
        hash_, replica, rel = row["hash"], row["replica"], row["rel_path"]
        drv = drivers[replica]
        try:
            actual = drv.hash_of(rel)
        except (OSError, ReplicaError) as exc:
            stats.problems.append(f"{replica}:{rel}: {exc}")
            continue

        stats.checked += 1
        if actual == hash_:
            stats.ok += 1
            with catalog.tx():
                catalog.db.execute(
                    "UPDATE placement SET state='present', verified_at=? "
                    "WHERE hash=? AND replica=?", (now(), hash_, replica))
        elif actual is None:
            stats.vanished += 1
            stats.problems.append(f"{replica}: MISSING {rel}")
            with catalog.tx():
                catalog.set_placement(hash_, replica, "missing")
            if repair and _repair(cfg, catalog, drivers, hash_, rel, replica, stats):
                stats.repaired += 1
        else:
            stats.corrupt += 1
            stats.problems.append(f"{replica}: CORRUPT {rel} (hash mismatch)")
            with catalog.tx():
                catalog.set_placement(hash_, replica, "corrupt")
            if repair and _repair(cfg, catalog, drivers, hash_, rel, replica, stats):
                stats.repaired += 1

        if progress and i % 50 == 0:
            progress(stats, len(rows))

    catalog.log("scrub", stats.summary())
    return stats


def _repair(cfg, catalog, drivers, hash_, rel, broken_replica, stats) -> bool:
    """Overwrite a bad copy from a replica whose bytes still hash correctly.

    The source is re-hashed before use - repairing from a second bad copy would
    turn a recoverable problem into a permanent one.
    """
    for name, drv in drivers.items():
        if name == broken_replica:
            continue
        try:
            if drv.hash_of(rel) != hash_:
                continue
        except (OSError, ReplicaError):
            continue

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / Path(rel).name
            try:
                drv.get(rel, staged)
                from .hashing import hash_file
                if hash_file(staged)[0] != hash_:
                    continue  # damaged in transit
                drivers[broken_replica].put(staged, rel)
            except (OSError, ReplicaError) as exc:
                stats.problems.append(f"repair {rel} from {name}: {exc}")
                continue

        with catalog.tx():
            catalog.db.execute(
                "UPDATE placement SET state='present', verified_at=? "
                "WHERE hash=? AND replica=?", (now(), hash_, broken_replica))
        stats.problems.append(f"  -> repaired {rel} on {broken_replica} from {name}")
        return True

    stats.unrepairable += 1
    stats.problems.append(f"  !! NO GOOD COPY of {rel} - this photo is at risk")
    return False
