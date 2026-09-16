"""Deciding which drive each photo belongs on when no single drive fits it all.

Two storage models live side by side:

* **full**  - the replica holds a complete copy (the original design)
* **shard** - the replica holds a computed subset, so several small drives can
  together hold a library none of them could hold alone

Sharding costs you a real property: no single sharded drive is independently
complete, so restoring needs all of them. In exchange a 1 TB library fits on
three 500 GB drives with room to spare. That trade is the user's to make, which
is why both modes exist rather than one replacing the other.

**Placement uses weighted rendezvous hashing.** For each photo we score every
eligible drive from the photo's own hash plus the drive's name, and take the
highest scorers. The useful properties:

* **Deterministic** - the same photo always prefers the same drives, so a plan
  can be recomputed anywhere without storing it.
* **Balanced** - scores are uniform, so drives fill in proportion to capacity.
* **Stable** - adding a fourth drive moves only about a quarter of the photos.
  Naive `hash % drive_count` would move nearly all of them, which on a 1 TB
  library means days of copying instead of hours.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Config, ReplicaSpec

# Leave headroom: filesystems slow down and misbehave when completely full, and
# a drive with zero free space cannot even write its own recovery kit.
HEADROOM = 0.10

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTP]?)B?\s*$", re.I)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(text: str) -> int | None:
    """'1.8TB' -> bytes. Returns None for 'auto'."""
    if not text or text.strip().lower() == "auto":
        return None
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"cannot understand capacity {text!r} (try '500GB' or 'auto')")
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


def usable_capacity(spec: ReplicaSpec) -> int | None:
    """How many bytes this replica may hold, or None if it cannot be measured."""
    declared = parse_size(spec.capacity)
    if declared is not None:
        return int(declared * (1 - HEADROOM))
    if spec.kind != "local":
        return None  # a remote drive's size is not knowable from here
    root = Path(spec.root).expanduser()
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    # Free space plus whatever this library already occupies there.
    already = _tree_size(root)
    return int((usage.free + already) * (1 - HEADROOM))


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _score(asset_hash: str, replica_name: str, weight: float) -> float:
    """Weighted rendezvous (highest-random-weight) score."""
    digest = hashlib.sha256(f"{asset_hash}:{replica_name}".encode()).digest()
    # Map the digest into (0, 1), avoiding exactly 0 which would divide by zero.
    unit = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
    return -weight / math.log(unit)


@dataclass
class Plan:
    """Where every photo should live, and whether that is actually achievable."""
    assignments: dict[str, set[str]] = field(default_factory=dict)   # hash -> replicas
    per_replica: dict[str, dict] = field(default_factory=dict)       # name -> stats
    unplaceable: list[tuple[str, int]] = field(default_factory=list)  # (hash, copies got)
    shard_copies_needed: int = 0
    total_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.unplaceable

    def for_replica(self, name: str) -> set[str]:
        return {h for h, names in self.assignments.items() if name in names}


def build_plan(cfg: Config, catalog: Catalog) -> Plan:
    """Compute where each photo should live.

    Full replicas hold everything, so they cover the first copies. Shard
    replicas supply whatever redundancy is still missing after that.
    """
    plan = Plan()
    full = cfg.full_replicas
    shards = cfg.shard_replicas
    plan.shard_copies_needed = max(0, cfg.min_copies - len(full))

    capacities: dict[str, int] = {}
    weights: dict[str, float] = {}
    for spec in shards:
        cap = usable_capacity(spec)
        # An unmeasurable drive should not be excluded from planning entirely;
        # assume it is average-sized rather than silently dropping it.
        capacities[spec.name] = cap if cap is not None else -1
        weights[spec.name] = float(cap) if cap and cap > 0 else 1.0

    if capacities and all(c == -1 for c in capacities.values()):
        for name in capacities:
            weights[name] = 1.0

    used = {s.name: 0 for s in shards}
    counts = {s.name: 0 for s in shards}

    # An offline copy is only required from the shards if no full replica
    # already provides one.
    need_offline_from_shards = (cfg.require_offline_copy
                                and not any(r.offline for r in full)
                                and any(r.offline for r in shards))

    assets = catalog.db.execute(
        "SELECT hash, size FROM asset ORDER BY hash").fetchall()

    for row in assets:
        h, size = row["hash"], row["size"]
        plan.total_bytes += size
        chosen: set[str] = {r.name for r in full}

        if plan.shard_copies_needed and shards:
            ranked = sorted(shards, key=lambda s: _score(h, s.name, weights[s.name]),
                            reverse=True)
            picked: list[str] = []
            for spec in ranked:
                if len(picked) >= plan.shard_copies_needed:
                    break
                cap = capacities[spec.name]
                if cap != -1 and used[spec.name] + size > cap:
                    continue  # this drive is full; the next-ranked one takes it
                picked.append(spec.name)

            if need_offline_from_shards and not any(
                    cfg.replica(n).offline for n in picked):
                # Swap the lowest-ranked pick for the best offline drive with room.
                for spec in ranked:
                    if not spec.offline or spec.name in picked:
                        continue
                    cap = capacities[spec.name]
                    if cap != -1 and used[spec.name] + size > cap:
                        continue
                    if picked:
                        picked[-1] = spec.name
                    else:
                        picked.append(spec.name)
                    break

            for name in picked:
                used[name] += size
                counts[name] += 1
            chosen |= set(picked)

            if len(picked) < plan.shard_copies_needed:
                plan.unplaceable.append((h, len(chosen)))

        plan.assignments[h] = chosen

    for spec in shards:
        cap = capacities[spec.name]
        plan.per_replica[spec.name] = {
            "bytes": used[spec.name], "files": counts[spec.name],
            "capacity": None if cap == -1 else cap,
            "offline": spec.offline, "mode": "shard",
            "fill": (used[spec.name] / cap) if cap and cap > 0 else None,
        }
    for spec in full:
        plan.per_replica[spec.name] = {
            "bytes": plan.total_bytes, "files": len(assets),
            "capacity": usable_capacity(spec), "offline": spec.offline,
            "mode": "full", "fill": None,
        }
        cap = plan.per_replica[spec.name]["capacity"]
        if cap:
            plan.per_replica[spec.name]["fill"] = plan.total_bytes / cap
    return plan


def planned_for(cfg: Config, catalog: Catalog, name: str) -> set[str] | None:
    """Hashes a replica should hold, or None when it should hold everything."""
    if not cfg.sharded or not cfg.replica(name).is_shard:
        return None
    return build_plan(cfg, catalog).for_replica(name)
