"""Pulling photos off a device, archiving them, and proving it is safe to delete.

The hard part of "clear up space on my phone" is not copying files. It is
answering, with evidence, the question *is it safe to delete this now?* - because
the moment you delete from the phone, PhotoVault's copies are the only copies.

So reclaiming works from re-read bytes, never from the catalog. A placement row
saying `present` is a belief; a photo is only reclaimable when `min_copies`
replicas have been asked to hash the file and have all returned the right
answer. Drives that were not plugged in during the sync simply do not count,
which means you can only free as much phone storage as you actually earned.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Config, SourceSpec
from .hashing import hash_file
from .ingest import WANTED_EXT, ingest_source, iter_media
from .mediatime import normalize_ext
from .replicas import ReplicaError, driver_for


@dataclass
class ReclaimReport:
    device: str = ""
    checked: int = 0
    safe: list[tuple[Path, str, int]] = field(default_factory=list)   # path, hash, size
    held: list[tuple[Path, int, str]] = field(default_factory=list)   # path, copies, why
    deleted: int = 0
    bytes_freed: int = 0
    unreachable: list[str] = field(default_factory=list)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(size for _, _, size in self.safe)


@dataclass
class ImmichDrain:
    """What happened to Immich's own copies after the archive took them."""
    pulled: int = 0
    already_known: int = 0
    released: int = 0
    held: int = 0
    rescanned: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    device: str = ""
    pulled: int = 0
    imported: int = 0
    duplicates: int = 0
    replicated: dict[str, int] = field(default_factory=dict)
    unreachable: list[str] = field(default_factory=list)
    reclaim: ReclaimReport | None = None
    immich: ImmichDrain | None = None
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- Android

class AdbError(RuntimeError):
    pass


def adb_available() -> bool:
    return shutil.which("adb") is not None


def adb_devices() -> list[str]:
    if not adb_available():
        return []
    r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=30)
    return [line.split()[0] for line in r.stdout.splitlines()[1:]
            if line.strip() and line.split()[-1] == "device"]


def adb_pull(remote: str, dest: Path, *, serial: str | None = None,
             progress=None) -> list[Path]:
    """Copy media from an Android device over USB into a staging directory.

    ADB is the clean path on Android: no background restrictions to fight, no
    MTP quirks, and deletion is scriptable - which is what makes the whole
    reclaim story work end to end on Android but not on iOS.
    """
    if not adb_available():
        raise AdbError("adb not found. Install with: brew install android-platform-tools")
    found = adb_devices()
    if not found:
        raise AdbError("no Android device connected (check the cable and that "
                       "USB debugging is enabled)")
    serial = serial or found[0]

    listing = subprocess.run(
        ["adb", "-s", serial, "shell", "find", remote, "-type", "f"],
        capture_output=True, text=True, timeout=300)
    if listing.returncode != 0:
        raise AdbError(f"could not list {remote}: {listing.stderr.strip()}")

    remote_files = [ln.strip() for ln in listing.stdout.splitlines()
                    if ln.strip() and Path(ln.strip()).suffix.lower().lstrip(".")
                    in WANTED_EXT]

    dest.mkdir(parents=True, exist_ok=True)
    pulled: list[Path] = []
    for i, rf in enumerate(remote_files, 1):
        local = dest / Path(rf).name
        if local.exists():
            pulled.append(local)
            continue
        r = subprocess.run(["adb", "-s", serial, "pull", "-a", rf, str(local)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            pulled.append(local)
        if progress and i % 25 == 0:
            progress(i, len(remote_files))
    return pulled


def adb_delete(paths: list[str], *, serial: str | None = None) -> int:
    serial = serial or (adb_devices() or [None])[0]
    if serial is None:
        raise AdbError("no Android device connected")
    removed = 0
    for chunk_start in range(0, len(paths), 50):
        chunk = paths[chunk_start:chunk_start + 50]
        quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in chunk)
        r = subprocess.run(["adb", "-s", serial, "shell", f"rm -f {quoted}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            removed += len(chunk)
    return removed


# ---------------------------------------------------------------------- Immich

def _immich_seen(catalog: Catalog, device: str) -> set[str]:
    """Immich asset ids already pulled, so nothing is downloaded twice."""
    rows = catalog.db.execute(
        "SELECT abs_path FROM source_file WHERE device = ? AND abs_path LIKE 'immich:%'",
        (device,)).fetchall()
    return {r["abs_path"][len("immich:"):] for r in rows}


def pull_from_immich(cfg: Config, catalog: Catalog, spec: SourceSpec, root: Path,
                     *, report_fn=print) -> tuple[ImmichDrain, dict[str, str]]:
    """Download Immich's managed assets into a staging folder.

    Returns the drain stats and a map of {local filename: immich asset id}, so
    that once the archive has verified its copies we know exactly which assets
    Immich may release.
    """
    from .immich import ImmichClient, ImmichError

    drain = ImmichDrain()
    mapping: dict[str, str] = {}
    client = ImmichClient(spec.url, spec.api_key)

    if not client.ping():
        drain.errors.append(f"cannot reach Immich at {spec.url}")
        return drain, mapping

    seen = _immich_seen(catalog, spec.device)
    try:
        for asset in client.managed_assets():
            if asset.id in seen:
                drain.already_known += 1
                continue
            # Keep the asset id in the filename: staging is flat, and two
            # phones can easily both produce IMG_0001.jpg.
            safe = Path(asset.filename).name or f"{asset.id}.jpg"
            local = root / f"{asset.id[:8]}_{safe}"
            try:
                client.download(asset, local)
            except ImmichError as exc:
                drain.errors.append(str(exc))
                continue
            mapping[local.name] = asset.id
            drain.pulled += 1
            if drain.pulled % 25 == 0:
                report_fn(f"    pulled {drain.pulled}")
    except ImmichError as exc:
        drain.errors.append(str(exc))

    return drain, mapping


def release_from_immich(cfg: Config, catalog: Catalog, spec: SourceSpec,
                        mapping: dict[str, str], reclaim: ReclaimReport,
                        drain: ImmichDrain, *, report_fn=print) -> None:
    """Ask Immich to drop its copies of photos the archive has verified.

    Only assets whose bytes were re-read and re-hashed on `min_copies` devices
    are released, and the delete is soft - they land in Immich's own trash. A
    photo Immich still holds is a duplicate; a photo neither holds is gone, so
    every failure here errs towards the duplicate.
    """
    from .immich import ImmichClient, ImmichError

    safe_names = {path.name for path, _, _ in reclaim.safe}
    releasable = [asset_id for name, asset_id in mapping.items()
                  if name in safe_names]
    drain.held = len(mapping) - len(releasable)

    if not releasable:
        return
    try:
        client = ImmichClient(spec.url, spec.api_key)
        drain.released = client.delete(releasable, force=False)
        report_fn(f"  Immich released {drain.released} of its copies "
                  f"(recoverable from its trash)")
        drain.rescanned = client.trigger_library_scan()
    except ImmichError as exc:
        drain.errors.append(f"could not release Immich copies: {exc}")


# --------------------------------------------------------------------- reclaim

def verified_copies(cfg: Config, catalog: Catalog, hash_: str, rel_path: str,
                    drivers: dict) -> int:
    """How many replicas can right now hand back bytes that hash correctly."""
    good = 0
    for name, drv in drivers.items():
        try:
            if drv.hash_of(rel_path) == hash_:
                good += 1
        except (OSError, ReplicaError):
            continue
    return good


def reclaimable(cfg: Config, catalog: Catalog, root: Path, *, device: str = "",
                apply: bool = False, progress=None) -> ReclaimReport:
    """Which files in `root` are provably safe to delete, and optionally do it."""
    report = ReclaimReport(device=device or str(root))

    drivers = {}
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers[spec.name] = drv
            else:
                report.unreachable.append(spec.name)
        except Exception:
            report.unreachable.append(spec.name)

    files = [p for p in iter_media(root) if normalize_ext(p) in WANTED_EXT]
    for i, path in enumerate(files, 1):
        report.checked += 1
        try:
            digest, size = hash_file(path)
        except OSError as exc:
            report.held.append((path, 0, str(exc)))
            continue

        asset = catalog.asset(digest)
        if asset is None:
            report.held.append((path, 0, "not imported yet"))
            continue

        copies = verified_copies(cfg, catalog, digest, asset["rel_path"], drivers)
        if copies >= cfg.min_copies:
            report.safe.append((path, digest, size))
        else:
            why = (f"{copies} verified cop{'y' if copies == 1 else 'ies'}, "
                   f"need {cfg.min_copies}")
            if report.unreachable:
                why += f" (not connected: {', '.join(report.unreachable)})"
            report.held.append((path, copies, why))
        if progress and i % 50 == 0:
            progress(i, len(files))

    if apply:
        for path, _, size in report.safe:
            try:
                path.unlink()
                report.deleted += 1
                report.bytes_freed += size
            except OSError as exc:
                report.held.append((path, cfg.min_copies, str(exc)))
    return report


# ---------------------------------------------------------------------- import

def run_import(cfg: Config, catalog: Catalog, spec: SourceSpec, *,
               reclaim: bool = False, report_fn=print) -> ImportReport:
    """Copy from a device, archive it everywhere reachable, then verify."""
    rep = ImportReport(device=spec.device)
    staging: tempfile.TemporaryDirectory | None = None

    if spec.kind == "immich":
        staging = tempfile.TemporaryDirectory(prefix="photovault-immich-")
        root = Path(staging.name)
        report_fn(f"  pulling from Immich at {spec.url}...")
        drain, immich_map = pull_from_immich(cfg, catalog, spec, root,
                                             report_fn=report_fn)
        rep.immich = drain
        rep.pulled = drain.pulled
        rep.errors.extend(drain.errors[:10])
        if not drain.pulled:
            report_fn(f"  nothing new ({drain.already_known} already archived)")
            staging.cleanup()
            return rep
    elif spec.kind == "adb":
        immich_map = {}
        staging = tempfile.TemporaryDirectory(prefix="photovault-adb-")
        root = Path(staging.name)
        report_fn(f"  pulling from Android ({spec.path or '/sdcard/DCIM'})...")
        try:
            pulled = adb_pull(spec.path or "/sdcard/DCIM", root,
                              progress=lambda n, t: report_fn(f"    {n}/{t}"))
            rep.pulled = len(pulled)
        except AdbError as exc:
            rep.errors.append(str(exc))
            staging.cleanup()
            return rep
    else:
        immich_map = {}
        root = Path(spec.path).expanduser()
        if not root.is_dir():
            rep.errors.append(f"{root} does not exist")
            return rep

    try:
        st = ingest_source(cfg, catalog, spec.device, root)
        rep.imported, rep.duplicates = st.imported, st.duplicates
        rep.errors.extend(st.errors[:10])
        report_fn(f"  imported {st.imported}, {st.duplicates} already known")

        if spec.kind == "immich" and immich_map:
            # Record the Immich asset id, not the temporary staging path, so a
            # later run knows this asset is already archived and skips the
            # download. Staging is a temp directory - its paths are meaningless
            # once the run ends.
            with catalog.tx():
                for name, asset_id in immich_map.items():
                    staged = root / name
                    if not staged.is_file():
                        continue
                    try:
                        digest, _ = hash_file(staged)
                    except OSError:
                        continue
                    if catalog.has_asset(digest):
                        catalog.record_source(digest, spec.device,
                                              f"immich:{asset_id}")

        if spec.kind in ("immich", "adb"):
            # ingest_source() recorded where it found each file, which for these
            # sources is a temp directory that no longer exists a moment later.
            # A source row that can never be looked at again is worse than no
            # row: it makes the audit trail look complete when it is not.
            with catalog.tx():
                catalog.db.execute(
                    "DELETE FROM source_file WHERE device = ? AND abs_path LIKE ?",
                    (spec.device, f"{root}%"))

        from . import sync as sync_mod
        for replica in cfg.replicas:
            if replica.name == cfg.primary:
                continue
            try:
                pushed = sync_mod.push(cfg, catalog, replica.name)
                rep.replicated[replica.name] = pushed.copied
                report_fn(f"  {replica.name}: copied {pushed.copied}")
            except ReplicaError as exc:
                rep.unreachable.append(replica.name)
                report_fn(f"  {replica.name}: unavailable ({exc})")

        # For Immich the reclaim check always runs: releasing Immich's copy is
        # the whole point, and it must be gated on verified copies either way.
        rep.reclaim = reclaimable(cfg, catalog, root, device=spec.device,
                                  apply=reclaim or spec.kind == "immich")
        if spec.kind == "immich" and rep.immich:
            release_from_immich(cfg, catalog, spec, immich_map, rep.reclaim,
                                rep.immich, report_fn=report_fn)
        if reclaim and spec.kind == "adb" and rep.reclaim.deleted:
            # Staging is a copy; the originals still live on the phone.
            names = {p.name for p, _, _ in rep.reclaim.safe}
            base = spec.path or "/sdcard/DCIM"
            listing = subprocess.run(
                ["adb", "shell", "find", base, "-type", "f"],
                capture_output=True, text=True, timeout=300)
            targets = [ln.strip() for ln in listing.stdout.splitlines()
                       if Path(ln.strip()).name in names]
            removed = adb_delete(targets)
            report_fn(f"  deleted {removed} from the Android device")
    finally:
        if staging:
            staging.cleanup()
    return rep
