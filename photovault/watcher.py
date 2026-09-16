"""Watching inbox folders and importing photos the moment they land.

iOS will not let a background process copy your camera roll to a Mac
unattended - that is a platform restriction, not something a program can work
around. So getting photos OFF the phone always involves a deliberate act:
plugging in a cable, or opening a sync app.

Everything after that can be automatic, and this is the piece that makes it so.
Point macOS Image Capture (or Syncthing, or PhotoSync) at an inbox folder, and
this watcher notices new files, imports them, and fans them out to every
backup device without you doing anything else.

The one subtlety is not importing a file that is still being written. A photo
copied halfway would hash as a different, corrupt asset - and PhotoVault would
then dutifully replicate that corruption everywhere. So the watcher waits for
the folder to stop changing before touching anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .ingest import WANTED_EXT, ingest_source, iter_media
from .mediatime import normalize_ext
from .replicas import ReplicaError

# How long a folder must stop changing before we trust it. Generous, because
# the cost of waiting is seconds and the cost of being wrong is a corrupt import.
SETTLE_SECONDS = 4.0


@dataclass
class WatchStats:
    cycles: int = 0
    imported: int = 0
    replicated: int = 0
    cleared: int = 0
    errors: list[str] = field(default_factory=list)


def snapshot(root: Path) -> dict[str, tuple[int, float]]:
    """Size and mtime of every media file under root."""
    out: dict[str, tuple[int, float]] = {}
    for path in iter_media(root):
        if normalize_ext(path) not in WANTED_EXT:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        out[str(path)] = (st.st_size, st.st_mtime)
    return out


def wait_until_settled(root: Path, *, settle: float = SETTLE_SECONDS,
                       timeout: float = 600.0) -> bool:
    """Block until nothing under root has changed for `settle` seconds.

    Returns False if the folder never went quiet within `timeout` - a phone
    still copying a large video, say. The caller simply tries again later
    rather than importing a partial file.
    """
    deadline = time.monotonic() + timeout
    previous = snapshot(root)
    while time.monotonic() < deadline:
        time.sleep(settle)
        current = snapshot(root)
        if current == previous:
            return True
        previous = current
    return False


def clear_imported(cfg: Config, catalog: Catalog, root: Path) -> tuple[int, list[str]]:
    """Delete inbox files that provably have `min_copies` verified copies.

    Deleting an inbox original removes the last copy outside PhotoVault, so
    this uses exactly the same evidence bar as clearing a phone: replicas are
    asked to re-hash the stored bytes, and drives that were not connected
    simply do not count.
    """
    from .importer import reclaimable

    report = reclaimable(cfg, catalog, root, apply=True)
    notes = [f"kept {p.name}: {why}" for p, _, why in report.held[:10]]
    return report.deleted, notes


def run_once(cfg: Config, catalog: Catalog, *, clear: bool = False,
             report=print) -> WatchStats:
    """One pass: import anything new from every source, then replicate."""
    stats = WatchStats(cycles=1)
    pending_clear: list[tuple] = []

    for src in cfg.sources:
        root = Path(src.path).expanduser()
        if not root.is_dir():
            continue
        pending = [p for p in iter_media(root) if normalize_ext(p) in WANTED_EXT]
        if not pending:
            continue

        report(f"  {src.device}: {len(pending)} file(s) waiting")
        if not wait_until_settled(root):
            report(f"  {src.device}: still receiving files, will retry")
            continue

        st = ingest_source(cfg, catalog, src.device, root)
        stats.imported += st.imported
        stats.errors.extend(st.errors[:5])
        if st.imported:
            report(f"  {src.device}: imported {st.imported}")
        pending_clear.append((src, root))

    # Replicate BEFORE clearing. Clearing requires min_copies verified copies,
    # and at ingest time only the primary has one - so clearing first would
    # always hold everything back. The evidence has to exist before the
    # deletion step asks for it.
    if stats.imported:
        from . import sync as sync_mod
        for spec in cfg.replicas:
            if spec.name == cfg.primary:
                continue
            try:
                pushed = sync_mod.push(cfg, catalog, spec.name)
                stats.replicated += pushed.copied
                if pushed.copied:
                    report(f"  {spec.name}: backed up {pushed.copied}")
            except ReplicaError as exc:
                if not spec.offline:
                    stats.errors.append(f"{spec.name}: {exc}")

    for src, root in pending_clear:
        if clear or src.clear_after_import:
            n, notes = clear_imported(cfg, catalog, root)
            stats.cleared += n
            stats.errors.extend(notes[:5])
            if n:
                report(f"  {src.device}: cleared {n} from the inbox")
    return stats


def watch(cfg: Config, catalog_path: Path, *, interval: float = 20.0,
          clear: bool = False, once: bool = False, report=print) -> int:
    """Poll the inbox folders forever, importing and replicating as photos land.

    Polling rather than filesystem events: a poll every few seconds costs
    nothing on an idle folder, works identically on macOS and Windows, and has
    no event-queue overflow to handle when a phone dumps 2,000 photos at once.
    """
    catalog = Catalog(catalog_path)
    total = WatchStats()
    try:
        while True:
            stats = run_once(cfg, catalog, clear=clear, report=report)
            total.cycles += 1
            total.imported += stats.imported
            total.replicated += stats.replicated
            total.cleared += stats.cleared
            for err in stats.errors:
                report(f"  ! {err}")
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        report("\nstopped")
    finally:
        catalog.close()
    return total.imported
