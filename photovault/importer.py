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
class ImportReport:
    device: str = ""
    pulled: int = 0
    imported: int = 0
    duplicates: int = 0
    replicated: dict[str, int] = field(default_factory=dict)
    unreachable: list[str] = field(default_factory=list)
    reclaim: ReclaimReport | None = None
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

    if spec.kind == "adb":
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
        root = Path(spec.path).expanduser()
        if not root.is_dir():
            rep.errors.append(f"{root} does not exist")
            return rep

    try:
        st = ingest_source(cfg, catalog, spec.device, root)
        rep.imported, rep.duplicates = st.imported, st.duplicates
        rep.errors.extend(st.errors[:10])
        report_fn(f"  imported {st.imported}, {st.duplicates} already known")

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

        rep.reclaim = reclaimable(cfg, catalog, root, device=spec.device,
                                  apply=reclaim)
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
