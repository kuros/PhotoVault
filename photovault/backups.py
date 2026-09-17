"""Replicating the catalog itself, and anything else that is not a photo.

For most of its life the catalog was purely derived data: lose it, run `rebuild`
and `reconcile`, get it back. That is no longer wholly true. It now also holds
things nothing else records -

    * duplicate review decisions - which photo you chose to keep
    * trash state - what you deleted, and when
    * drive identities and last-synced times

- and none of that can be reconstructed by reading the files. It is judgement,
not data, in exactly the way album membership is.

So the catalog gets replicated like a photo. Not *as* a photo: backups rotate
and the library never forgets, so mixing them would either break that promise
or grow without bound. They live in their own tree on each replica.

The one thing you must not do is copy `catalog.db` with `cp`. In WAL mode the
committed state is split between the database and its write-ahead log, so a
plain copy of the main file can be missing recent transactions - or, as it
turns out, every table you ever created. SQLite's backup API takes a consistent
snapshot of a live database; that is what this uses.
"""

from __future__ import annotations

import gzip
import hashlib
import zlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .replicas import LocalDriver, ReplicaError, driver_for

# Inside the replica root, not beside it: the same rule the recovery kit
# follows, so nothing is ever written outside the path the user configured.
BACKUP_DIR = ".photovault-backups"
KEEP = 10


@dataclass
class BackupResult:
    name: str = ""
    size: int = 0
    digest: str = ""
    copied: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    pruned: int = 0
    errors: list[str] = field(default_factory=list)
    # Immich's database dump and album manifest, when it is configured.
    immich: object | None = None
    extra_bytes: int = 0


def snapshot(catalog_path: Path, dest: Path) -> tuple[int, str]:
    """Write a consistent, compressed copy of a live catalog. Returns (size, sha256)."""
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(raw)
            try:
                src.backup(out)        # consistent even while the app is writing
            finally:
                out.close()
        finally:
            src.close()

        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        part = dest.with_suffix(dest.suffix + ".part")
        with open(raw, "rb") as fh, gzip.open(part, "wb", compresslevel=6) as gz:
            while block := fh.read(1024 * 1024):
                digest.update(block)
                gz.write(block)
        part.replace(dest)
    return dest.stat().st_size, digest.hexdigest()


def backup_root(spec) -> Path:
    return Path(spec.root).expanduser() / BACKUP_DIR


def run(cfg: Config, *, keep: int = KEEP, label: str = "catalog") -> BackupResult:
    """Snapshot the catalog and copy it to every replica that is reachable."""
    result = BackupResult()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result.name = f"{label}-{stamp}.db.gz"

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / result.name
        result.size, result.digest = snapshot(cfg.catalog_path, staged)
        (Path(tmp) / f"{result.name}.sha256").write_text(
            f"{result.digest}  {result.name}\n")

        # Immich's database and albums ride along in the same tree, on the same
        # devices, with the same checksums and the same rotation. A failure here
        # never stops the catalog backup: the catalog is the part that cannot be
        # regenerated.
        artifacts = [result.name]
        if cfg.immich.enabled:
            from . import immich_backup
            art = immich_backup.run(cfg, Path(tmp), stamp)
            result.immich = art
            result.errors.extend(art.errors[:10])
            for name in (art.dump_name, art.manifest_name):
                if not name:
                    continue
                path = Path(tmp) / name
                digest = _sha256_file(path)
                (Path(tmp) / f"{name}.sha256").write_text(f"{digest}  {name}\n")
                artifacts.append(name)
                result.extra_bytes += path.stat().st_size

        for spec in cfg.replicas:
            drv = driver_for(spec)
            try:
                if not drv.available():
                    result.unreachable.append(spec.name)
                    continue
            except Exception:
                result.unreachable.append(spec.name)
                continue

            try:
                for name in artifacts:
                    rel = f"{BACKUP_DIR}/{name}"
                    drv.put(Path(tmp) / name, rel)
                    drv.put(Path(tmp) / f"{name}.sha256", rel + ".sha256")
                result.copied.append(spec.name)
            except (OSError, ReplicaError) as exc:
                result.errors.append(f"{spec.name}: {exc}")
                continue

            if isinstance(drv, LocalDriver):
                for prefix in (label, "immich-db", "immich-albums"):
                    result.pruned += prune(spec, keep=keep, label=prefix)

    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def prune(spec, *, keep: int = KEEP, label: str = "catalog") -> int:
    """Keep the newest `keep` snapshots on one local replica.

    Rotation is why backups are not stored as library assets: the library's
    contract is that nothing ever leaves it, and a backup set that never
    rotates grows without limit.
    """
    root = backup_root(spec)
    if not root.is_dir():
        return 0
    pattern = {"catalog": "catalog-*.db.gz",
               "immich-db": "immich-db-*.sql.gz",
               "immich-albums": "immich-albums-*.json"}.get(label,
                                                           f"{label}-*.db.gz")
    snaps = sorted(root.glob(pattern), reverse=True)
    removed = 0
    for old in snaps[keep:]:
        try:
            old.unlink()
            old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


@dataclass
class Snapshot:
    replica: str
    path: Path
    name: str
    size: int
    when: datetime


def available(cfg: Config, *, label: str = "catalog") -> list[Snapshot]:
    """Every catalog snapshot on every reachable local replica, newest first."""
    found: list[Snapshot] = []
    for spec in cfg.replicas:
        if spec.kind != "local":
            continue
        drv = LocalDriver(spec)
        try:
            if not drv.available():
                continue
        except Exception:
            continue
        for path in backup_root(spec).glob(f"{label}-*.db.gz"):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append(Snapshot(
                replica=spec.name, path=path, name=path.name, size=stat.st_size,
                when=datetime.fromtimestamp(stat.st_mtime)))
    return sorted(found, key=lambda s: s.when, reverse=True)


def age_days(cfg: Config) -> float | None:
    snaps = available(cfg)
    if not snaps:
        return None
    return (datetime.now() - snaps[0].when).total_seconds() / 86400


def verify(snap: Snapshot) -> bool:
    """Confirm a snapshot still matches the digest written beside it."""
    sidecar = snap.path.with_suffix(snap.path.suffix + ".sha256")
    if not sidecar.is_file():
        return False
    expected = sidecar.read_text().split()[0]
    digest = hashlib.sha256()
    try:
        with gzip.open(snap.path, "rb") as gz:
            while block := gz.read(1024 * 1024):
                digest.update(block)
    except (OSError, EOFError, zlib.error):
        # A corrupted archive raises zlib.error, which is NOT an OSError - so
        # catching OSError alone turned "this backup is damaged" into a crash.
        # Verification must never be the thing that fails loudly.
        return False
    return digest.hexdigest() == expected


def restore(cfg: Config, snap: Snapshot, *, dry_run: bool = True) -> Path | None:
    """Replace the live catalog with a snapshot, keeping the current one aside.

    The existing catalog is moved rather than overwritten. Restoring the wrong
    snapshot should cost you a rename, not your review decisions.
    """
    if not verify(snap):
        raise ValueError(f"{snap.name} failed its checksum - refusing to restore")
    if dry_run:
        return None

    target = cfg.catalog_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        aside = target.with_name(
            f"{target.name}.replaced-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.move(str(target), aside)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(target) + suffix)
            if extra.exists():
                shutil.move(str(extra), str(aside) + suffix)
    else:
        aside = None

    with gzip.open(snap.path, "rb") as gz, open(target, "wb") as out:
        shutil.copyfileobj(gz, out)
    return aside
