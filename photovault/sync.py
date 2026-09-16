"""Replication: making every replica hold what the catalog says it should."""

from __future__ import annotations

from dataclasses import dataclass, field
import shutil
from datetime import datetime
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .mediatime import normalize_ext
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

    present = drv.list_present()
    changed = 0
    with catalog.tx():
        for asset in catalog.all_assets():
            state = "present" if asset["rel_path"] in present else "missing"
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
    if isinstance(drv, LocalDriver) and not dry_run:
        drv.ensure_root()

    missing = catalog.missing_on(name)
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


RECOVERY_DOC = """# How to recover these photos

This drive holds a complete copy of a PhotoVault photo library. The photos are
ordinary files in dated folders - you do not need PhotoVault to read them. Open
`library/` in Finder or Explorer and everything is there.

To rebuild the full system on a replacement computer:

1. Install Python 3.11 or newer, and get PhotoVault:
   https://github.com/YOURNAME/photovault      <- push your code here!

2. Copy `photovault-config.toml` from this drive to
   `~/.config/photovault/config.toml` and edit the paths to match the new
   machine.

3. Rebuild the catalog from this drive, then re-learn the other devices:

       python3 -m photovault rebuild {replica}
       python3 -m photovault reconcile --all

4. Refill the new primary computer, and verify every byte:

       python3 -m photovault sync --all
       python3 -m photovault scrub --force
       python3 -m photovault status

Step 4 should end with four OK lines. If it does, nothing was lost.

---
Library: {count} files, {size}
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

    row = catalog.db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM asset").fetchone()
    size = row["b"]
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
        (kit / "RECOVERY.md").write_text(RECOVERY_DOC.format(
            replica=name, count=row["n"], size=f"{size:.1f} {unit}",
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
