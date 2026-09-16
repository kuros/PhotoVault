"""Replication: making every replica hold what the catalog says it should."""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .identity import mark_synced, verify as verify_identity
from .mediatime import normalize_ext
from .placement import planned_for
from .replicas import Driver, LocalDriver, ReplicaError, driver_for


@dataclass
class SyncStats:
    replica: str = ""
    copied: int = 0
    already: int = 0
    failed: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        gb = self.bytes_copied / 1e9
        return (f"{self.replica}: copied {self.copied} ({gb:.2f} GB)  "
                f"already there {self.already}  failed {self.failed}")


def reconcile(cfg: Config, catalog: Catalog, name: str) -> int:
    """Ask a replica what it actually holds and correct the catalog's beliefs.

    This is what makes the catalog disposable: point PhotoVault at a set of
    replicas and reconcile, and the placement table rebuilds itself.
    """
    spec = cfg.replica(name)
    drv = driver_for(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")
    verify_identity(cfg, catalog, name, drv)

    present = drv.list_present()
    # A shard is only responsible for its planned subset. Without this, every
    # photo that correctly lives on another drive would be recorded as
    # "missing" here and the health report would be nonsense.
    wanted = planned_for(cfg, catalog, name)

    changed = 0
    with catalog.tx():
        for asset in catalog.all_assets():
            on_disk = asset["rel_path"] in present
            if wanted is not None and asset["hash"] not in wanted and not on_disk:
                # Not planned here and not here: simply not this drive's concern.
                catalog.db.execute(
                    "DELETE FROM placement WHERE hash = ? AND replica = ?",
                    (asset["hash"], name))
                continue
            state = "present" if on_disk else "missing"
            existing = catalog.db.execute(
                "SELECT state FROM placement WHERE hash=? AND replica=?",
                (asset["hash"], name),
            ).fetchone()
            if existing is None or existing["state"] != state:
                # Never downgrade a known-corrupt marker to a mere 'missing'.
                if existing and existing["state"] == "corrupt" and state == "missing":
                    continue
                catalog.set_placement(asset["hash"], name, state)
                changed += 1
    catalog.log("reconcile", f"{name}: {len(present)} files on disk, {changed} corrections")
    return changed


def push(cfg: Config, catalog: Catalog, name: str, *, limit: int | None = None,
         dry_run: bool = False, progress=None) -> SyncStats:
    """Copy everything this replica is missing, sourced from a healthy copy."""
    stats = SyncStats(replica=name)
    spec = cfg.replica(name)
    drv = driver_for(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")
    # Identity first: ensure_root() would otherwise create the directory for a
    # drive that is not plugged in, before anything got a chance to object.
    verify_identity(cfg, catalog, name, drv)
    if isinstance(drv, LocalDriver) and not dry_run:
        drv.ensure_root()

    missing = catalog.missing_on(name)
    # A shard holds only its planned subset, not everything.
    wanted = planned_for(cfg, catalog, name)
    if wanted is not None:
        missing = [a for a in missing if a["hash"] in wanted]
    if limit:
        missing = missing[:limit]

    for i, asset in enumerate(missing, 1):
        src = _local_source(cfg, catalog, asset["hash"], asset["rel_path"], exclude=name)
        if src is None:
            stats.failed += 1
            stats.errors.append(
                f"{asset['rel_path']}: no reachable source copy to replicate from")
            continue
        if dry_run:
            stats.copied += 1
            stats.bytes_copied += asset["size"]
            continue
        try:
            drv.put(src, asset["rel_path"])
        except (OSError, ReplicaError) as exc:
            stats.failed += 1
            stats.errors.append(f"{asset['rel_path']}: {exc}")
            continue
        with catalog.tx():
            catalog.set_placement(asset["hash"], name, "present")
        stats.copied += 1
        stats.bytes_copied += asset["size"]
        if progress and i % 25 == 0:
            progress(stats, len(missing))

    if not dry_run:
        write_recovery_kit(cfg, catalog, name)
        mark_synced(catalog, name)
        catalog.log("push", stats.summary())
    return stats


def _local_source(cfg: Config, catalog: Catalog, hash_: str, rel_path: str,
                  *, exclude: str) -> Path | None:
    """Find a locally readable copy to replicate from, primary first."""
    order = [cfg.primary] + [r.name for r in cfg.replicas if r.name != cfg.primary]
    for name in order:
        if name == exclude:
            continue
        spec = cfg.replica(name)
        if spec.kind != "local":
            continue
        row = catalog.db.execute(
            "SELECT state FROM placement WHERE hash=? AND replica=?", (hash_, name)
        ).fetchone()
        if not row or row["state"] != "present":
            continue
        candidate = Path(spec.root).expanduser() / rel_path
        if candidate.is_file():
            return candidate
    return None


def available_replicas(cfg: Config) -> list[tuple[str, Driver, bool]]:
    """(name, driver, reachable) for every configured replica."""
    out = []
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            ok = drv.available()
        except Exception:
            ok = False
        out.append((spec.name, drv, ok))
    return out


def rebuild_from(cfg: Config, catalog: Catalog, name: str, *, progress=None) -> int:
    """Reconstruct the catalog by reading a replica's files directly.

    This is the disaster-recovery path. Because every stored file is named from
    its own content hash and capture date, a replica carries everything needed
    to regenerate the catalog - so losing catalog.db costs CPU time, not photos.
    """
    from .hashing import hash_file
    from .mediatime import capture_time, media_kind, normalize_ext

    spec = cfg.replica(name)
    if spec.kind != "local":
        raise ReplicaError("rebuild currently requires a local replica "
                           "(mount the drive, or restore from it first)")
    drv = LocalDriver(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")
    verify_identity(cfg, catalog, name, drv)

    root = drv.root
    found = 0
    from .ingest import WANTED_EXT

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.suffix == ".part":
            continue
        # Only media. Without this, the recovery kit we write into every replica
        # root would itself be catalogued as a photo.
        if normalize_ext(path) not in WANTED_EXT:
            continue
        rel = str(path.relative_to(root))
        try:
            hash_, size = hash_file(path)
        except OSError:
            continue
        ext = normalize_ext(path)
        captured, time_source = capture_time(path, ext)
        with catalog.tx():
            catalog.add_asset(
                hash_=hash_, size=size, ext=ext, media_kind=media_kind(ext),
                captured_at=captured.isoformat(timespec="seconds") if captured else None,
                time_source=time_source, rel_path=rel,
            )
            catalog.set_placement(hash_, name, "present", verified=True)
        found += 1
        if progress and found % 100 == 0:
            progress(found)

    catalog.log("rebuild", f"{name}: recovered {found} assets")
    return found


def local_copy(cfg: Config, catalog: Catalog, hash_: str, rel_path: str) -> Path | None:
    """Any locally readable copy of an asset, primary preferred. Used to serve
    and thumbnail photos without caring which device they came from."""
    return _local_source(cfg, catalog, hash_, rel_path, exclude="")


SHARD_WARNING = """
!! THIS DRIVE HOLDS ONLY PART OF THE LIBRARY !!

This vault is sharded: the photos are split across several drives, and this
one is not complete on its own. It holds {count} of {total} photos. Restoring
everything needs all of these:

{siblings}

The photos that are here are ordinary files you can open directly - but do not
mistake this drive for a full backup.
"""

RECOVERY_DOC = """# How to recover these photos
{shard_notice}
The photos here are ordinary files in dated folders. You do not need PhotoVault
to read them: open `library/` in Finder or Explorer and they are right there.

To rebuild the full system on a replacement computer:

1. Install Python 3.11 or newer, and get PhotoVault:
   https://github.com/YOURNAME/photovault      <- push your code here!

2. Copy `photovault-config.toml` from this drive to
   `~/.config/photovault/config.toml` and edit the paths to match the new
   machine.

3. Rebuild the catalog from this drive, then re-learn the other devices:

       python3 -m photovault {rebuild_cmd}
       python3 -m photovault reconcile --all

4. Refill the new primary computer, and verify every byte:

       python3 -m photovault sync --all
       python3 -m photovault scrub --force
       python3 -m photovault status

Step 4 should end with four OK lines. If it does, nothing was lost.

---
This drive: {count} files, {size}
Devices in this vault: {replicas}
Written by PhotoVault on {when}
"""


def write_recovery_kit(cfg: Config, catalog: Catalog, name: str) -> bool:
    """Leave recovery instructions and a config copy on the replica itself.

    Found by running a disaster drill: the config file lived only on the Mac,
    so losing the Mac meant rewriting it from memory before recovery could
    start. A backup that cannot explain how to restore itself is only half a
    backup.
    """
    spec = cfg.replica(name)
    if spec.kind != "local":
        return False
    drv = LocalDriver(spec)
    if not drv.available():
        return False

    # A sharded drive must describe what IT holds, not the whole library, or
    # whoever finds it will think a partial drive is a complete backup.
    from .placement import planned_for
    wanted = planned_for(cfg, catalog, name)
    if wanted is None:
        row = catalog.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM asset").fetchone()
        count, size = row["n"], row["b"]
    else:
        held = catalog.db.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(a.size), 0) b FROM placement p
               JOIN asset a ON a.hash = p.hash
               WHERE p.replica = ? AND p.state = 'present'""", (name,)).fetchone()
        count, size = held["n"], held["b"]
    library_total = catalog.db.execute(
        "SELECT COUNT(*) n FROM asset").fetchone()["n"]
    row = {"n": count, "b": size}
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            break
        size /= 1024

    # The kit goes INSIDE the replica root, never beside it. Writing to
    # root.parent would escape the configured directory entirely - for a root
    # of /Volumes/Backup that means writing into /Volumes. Never write outside
    # the path the user configured. Non-media files here are ignored by
    # rebuild_from() and reconcile().
    kit = drv.root
    try:
        kit.mkdir(parents=True, exist_ok=True)
        # Only warn when this drive genuinely holds less than the whole
        # library. A shard that happens to hold everything is complete, and
        # claiming otherwise would be its own kind of lie.
        if wanted is None or count >= library_total:
            notice = ""
            rebuild_cmd = f"rebuild {name}"
        else:
            siblings = "\n".join(
                f"  - {r.name} ({r.mode})"
                + ("   <- this drive" if r.name == name else "")
                for r in cfg.replicas)
            notice = SHARD_WARNING.format(count=count, total=library_total,
                                          siblings=siblings)
            rebuild_cmd = "rebuild --all"
        (kit / "RECOVERY.md").write_text(RECOVERY_DOC.format(
            shard_notice=notice, rebuild_cmd=rebuild_cmd,
            count=row["n"], size=f"{size:.1f} {unit}",
            replicas=", ".join(r.name for r in cfg.replicas),
            when=datetime.now().strftime("%Y-%m-%d %H:%M"),
        ))
        source = _config_source_path(cfg)
        if source and source.exists():
            shutil.copy2(source, kit / "photovault-config.toml")
    except OSError:
        return False
    return True


def _config_source_path(cfg: Config) -> Path | None:
    from .config import DEFAULT_CONFIG_PATH
    return getattr(cfg, "source_path", None) or DEFAULT_CONFIG_PATH


@dataclass
class RebalanceStats:
    replica: str = ""
    removed: int = 0
    bytes_freed: int = 0
    kept_unsafe: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.replica}: removed {self.removed} "
                f"({self.bytes_freed / 1e9:.2f} GB freed), "
                f"kept {self.kept_unsafe} that could not be safely removed")


def rebalance(cfg: Config, catalog: Catalog, name: str, *, dry_run: bool = True,
              progress=None) -> RebalanceStats:
    """Remove files a shard no longer needs, once enough verified copies remain.

    This is the only operation in PhotoVault that deletes a photo, so it is
    deliberately the most paranoid one. Before removing any file it re-reads
    `min_copies` other copies and confirms their bytes still hash correctly.
    A placement row saying "present" is a belief; deleting the last good copy
    because of a stale belief is exactly the failure this whole program exists
    to prevent. It also defaults to a dry run.
    """
    from .hashing import hash_file
    from .identity import verify as verify_identity

    stats = RebalanceStats(replica=name)
    spec = cfg.replica(name)
    if not spec.is_shard:
        raise ReplicaError(f"{name} is a full replica - it is meant to hold "
                           f"everything, so there is nothing to rebalance")

    drv = driver_for(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")
    verify_identity(cfg, catalog, name, drv)

    wanted = planned_for(cfg, catalog, name) or set()
    held = catalog.db.execute(
        """SELECT p.hash, a.rel_path, a.size FROM placement p
           JOIN asset a ON a.hash = p.hash
           WHERE p.replica = ? AND p.state = 'present'""", (name,)).fetchall()
    surplus = [row for row in held if row["hash"] not in wanted]

    others = {s.name: driver_for(s) for s in cfg.replicas if s.name != name}

    for i, row in enumerate(surplus, 1):
        h, rel, size = row["hash"], row["rel_path"], row["size"]
        verified = 0
        for other_name, other in others.items():
            if verified >= cfg.min_copies:
                break
            try:
                if not other.available() or other.hash_of(rel) != h:
                    continue
            except (OSError, ReplicaError):
                continue
            verified += 1

        if verified < cfg.min_copies:
            stats.kept_unsafe += 1
            stats.notes.append(
                f"kept {rel}: only {verified} verified cop"
                f"{'y' if verified == 1 else 'ies'} elsewhere, need {cfg.min_copies}")
            continue

        if not dry_run:
            try:
                drv.delete(rel)
            except (OSError, ReplicaError) as exc:
                stats.notes.append(f"could not remove {rel}: {exc}")
                continue
            with catalog.tx():
                catalog.db.execute(
                    "DELETE FROM placement WHERE hash = ? AND replica = ?", (h, name))
        stats.removed += 1
        stats.bytes_freed += size
        if progress and i % 25 == 0:
            progress(stats, len(surplus))

    if not dry_run:
        catalog.log("rebalance", stats.summary())
    return stats
