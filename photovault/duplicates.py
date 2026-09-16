"""Finding near-duplicate photos and removing them only after you have looked.

Exact byte-for-byte copies never reach here - content addressing collapses
those at ingest. What remains is the messy kind: the same photo re-compressed
by a messaging app, exported at half resolution, or saved again by an editor.

Every part of this module is shaped by one fact: **this is the only feature
that deletes a photo you still want.** Scrub deletes nothing, rebalance only
removes an over-replicated copy, reclaim only removes a file you already have
archived. Here the whole asset goes. So nothing happens without an explicit,
per-group human decision, and even then the survivor is re-verified first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import perceptual
from .catalog import Catalog
from .config import Config
from .sync import local_copy

DEFAULT_THRESHOLD = 5      # bits of a 64-bit hash that may differ
BANDS = 8                  # 8 bands of 8 bits; catches any distance <= 7


@dataclass
class ScanStats:
    hashed: int = 0
    skipped: int = 0
    failed: int = 0
    remaining: int = 0


@dataclass
class DupMember:
    hash: str
    rel_path: str
    size: int
    width: int | None
    height: int | None
    captured_at: str | None
    ext: str
    copies: int
    distance: int = 0
    action: str = ""          # "", "keep" or "delete", as decided by the user

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


@dataclass
class DupGroup:
    id: str
    members: list[DupMember] = field(default_factory=list)
    suggested_keep: str = ""

    @property
    def wasted_bytes(self) -> int:
        """What removing everything but the suggested keeper would free."""
        return sum(m.size for m in self.members if m.hash != self.suggested_keep)

    @property
    def reviewed(self) -> bool:
        return any(m.action for m in self.members)


def scan(cfg: Config, catalog: Catalog, *, limit: int | None = None,
         progress=None) -> ScanStats:
    """Compute perceptual hashes for images that do not have one yet."""
    stats = ScanStats()
    if not perceptual.available():
        raise RuntimeError(
            "no image decoder available for perceptual hashing "
            "(install Pillow, or run on macOS where sips is built in)")

    rows = catalog.assets_without_phash(limit)
    for i, row in enumerate(rows, 1):
        src = local_copy(cfg, catalog, row["hash"], row["rel_path"])
        if src is None:
            stats.skipped += 1
            continue
        h = perceptual.dhash(src)
        width = height = None
        if h is not None:
            width, height = _dimensions(src)
        with catalog.tx():
            # An empty string, not NULL: a file we tried and could not decode
            # must not be retried on every scan.
            catalog.set_phash(row["hash"], h if h is not None else "", width, height)
        if h is None:
            stats.failed += 1
        else:
            stats.hashed += 1
        if progress and i % 25 == 0:
            progress(i, len(rows))

    stats.remaining = len(catalog.assets_without_phash())
    catalog.log("dup-scan", f"hashed {stats.hashed}, failed {stats.failed}")
    return stats


def _dimensions(path: Path) -> tuple[int | None, int | None]:
    """Pixel dimensions, used to suggest which copy to keep."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            return im.size
    except ImportError:
        pass
    except Exception:
        return None, None

    import shutil
    import subprocess

    if not shutil.which("sips"):
        return None, None
    try:
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight",
                            str(path)], capture_output=True, text=True, timeout=30)
        vals = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                key, _, value = line.strip().partition(":")
                vals[key.strip()] = value.strip()
        return int(vals.get("pixelWidth", 0)) or None, \
               int(vals.get("pixelHeight", 0)) or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, None


def find_groups(cfg: Config, catalog: Catalog, *,
                threshold: int = DEFAULT_THRESHOLD) -> list[DupGroup]:
    """Cluster assets whose perceptual hashes are within `threshold` bits.

    Comparing every pair would be O(n^2) - 20 billion comparisons on a 200k
    library. Instead each hash is split into 8 bands of 8 bits and bucketed by
    band: two hashes differing in at most 7 bits must agree exactly on at least
    one band (pigeonhole), so only same-bucket pairs need checking. That turns
    the scan linear in practice while staying exact for the thresholds we use.
    """
    rows = [r for r in catalog.assets_with_phash()
            if r["phash"] and not perceptual.is_degenerate(r["phash"])]
    if not rows:
        return []

    by_hash = {r["hash"]: r for r in rows}
    buckets: dict[tuple[int, str], list[str]] = {}
    for r in rows:
        for band in range(BANDS):
            key = (band, r["phash"][band * 2:band * 2 + 2])
            buckets.setdefault(key, []).append(r["hash"])

    # Union-find over candidate pairs.
    parent = {h: h for h in by_hash}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for members in buckets.values():
        if len(members) < 2 or len(members) > 400:
            # A bucket with hundreds of members is a near-uniform image class,
            # not a duplicate set; clustering it would propose mass deletion.
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if find(a) == find(b):
                    continue
                if perceptual.distance(by_hash[a]["phash"],
                                       by_hash[b]["phash"]) <= threshold:
                    union(a, b)

    clusters: dict[str, list[str]] = {}
    for h in by_hash:
        clusters.setdefault(find(h), []).append(h)

    decisions = catalog.decisions()
    copies = _copy_counts(catalog)
    groups: list[DupGroup] = []
    for root, hashes in clusters.items():
        if len(hashes) < 2:
            continue
        members = []
        for h in hashes:
            r = by_hash[h]
            members.append(DupMember(
                hash=h, rel_path=r["rel_path"], size=r["size"],
                width=r["width"], height=r["height"],
                captured_at=r["captured_at"], ext=r["ext"],
                copies=copies.get(h, 0), action=decisions.get(h, "")))
        anchor = by_hash[members[0].hash]["phash"]
        for m in members:
            m.distance = perceptual.distance(anchor, by_hash[m.hash]["phash"])
        members.sort(key=_keeper_rank)
        groups.append(DupGroup(id=root[:12], members=members,
                               suggested_keep=members[0].hash))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def _keeper_rank(m: DupMember):
    """Best candidate first: most pixels, then largest file, then earliest.

    Resolution beats file size because a large file can simply be a badly
    compressed small image, and the earliest capture date breaks ties towards
    the original rather than a later re-save.
    """
    return (-m.pixels, -m.size, m.captured_at or "9999", m.hash)


def _copy_counts(catalog: Catalog) -> dict[str, int]:
    return {r["hash"]: r["n"] for r in catalog.db.execute(
        "SELECT hash, COUNT(*) n FROM placement WHERE state='present' "
        "GROUP BY hash").fetchall()}


# ---------------------------------------------------------------------- apply

@dataclass
class ApplyReport:
    deleted: int = 0
    bytes_freed: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply(cfg: Config, catalog: Catalog, *, dry_run: bool = True,
          progress=None) -> ApplyReport:
    """Remove assets marked 'delete', but only where a keeper is provably safe.

    Three conditions, all required:

    1. The asset is marked 'delete' by an explicit human decision.
    2. Another asset in the same group is marked or suggested 'keep'.
    3. That keeper has `min_copies` copies that hash correctly *right now*.

    Condition 3 is the one that makes this survivable. Deleting a duplicate
    because the catalog says its twin is backed up, when the twin's only drive
    has silently rotted, destroys the last good version of that photo.
    """
    from .replicas import ReplicaError, driver_for

    report = ApplyReport()
    groups = find_groups(cfg, catalog)
    drivers = {}
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers[spec.name] = drv
        except Exception:
            continue

    for group in groups:
        doomed = [m for m in group.members if m.action == "delete"]
        if not doomed:
            continue
        keepers = [m for m in group.members if m.action != "delete"]
        if not keepers:
            for m in doomed:
                report.refused.append(
                    (m.rel_path, "every photo in the group was marked for deletion"))
            continue

        keeper = keepers[0]
        verified = 0
        for drv in drivers.values():
            try:
                if drv.hash_of(keeper.rel_path) == keeper.hash:
                    verified += 1
            except (OSError, ReplicaError):
                continue
        if verified < cfg.min_copies:
            for m in doomed:
                report.refused.append(
                    (m.rel_path,
                     f"the photo being kept has only {verified} verified "
                     f"cop{'y' if verified == 1 else 'ies'}, need {cfg.min_copies}"))
            continue

        for m in doomed:
            if dry_run:
                report.deleted += 1
                report.bytes_freed += m.size
                continue
            failed = False
            for name, drv in drivers.items():
                try:
                    drv.delete(m.rel_path)
                except (OSError, ReplicaError) as exc:
                    report.errors.append(f"{name}:{m.rel_path}: {exc}")
                    failed = True
            if failed:
                continue
            with catalog.tx():
                catalog.remove_asset(m.hash)
            catalog.log("dup-delete",
                        f"{m.rel_path} (kept {keeper.rel_path})")
            report.deleted += 1
            report.bytes_freed += m.size
        if progress:
            progress(report.deleted)

    if not dry_run:
        catalog.log("dup-apply",
                    f"deleted {report.deleted}, refused {len(report.refused)}")
    return report


def decide(catalog: Catalog, hashes: dict[str, str]) -> int:
    """Record review decisions: {asset hash: 'keep' | 'delete' | ''}."""
    n = 0
    with catalog.tx():
        for h, action in hashes.items():
            if action in ("keep", "delete"):
                catalog.set_decision(h, action)
            else:
                catalog.clear_decision(h)
            n += 1
    return n
