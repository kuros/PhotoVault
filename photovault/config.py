"""Configuration loading and defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "photovault" / "config.toml"


@dataclass
class ReplicaSpec:
    name: str
    kind: str            # local | rsync | gcs
    root: str
    host: str | None = None
    offline: bool = False  # a drive that normally lives unplugged in a drawer
    mode: str = "full"     # full = a complete copy; shard = a computed subset
    capacity: str = "auto"  # "1.8TB", "500GB", or "auto" to measure the disk

    @property
    def is_shard(self) -> bool:
        return self.mode == "shard"


@dataclass
class SourceSpec:
    device: str
    path: str


@dataclass
class Config:
    primary: str
    min_copies: int = 3
    require_offline_copy: bool = True
    scrub_days: int = 30
    catalog_path: Path = field(default_factory=lambda: Path.home() / ".config" / "photovault" / "catalog.db")
    replicas: list[ReplicaSpec] = field(default_factory=list)
    sources: list[SourceSpec] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def shard_replicas(self) -> list[ReplicaSpec]:
        return [r for r in self.replicas if r.is_shard]

    @property
    def full_replicas(self) -> list[ReplicaSpec]:
        return [r for r in self.replicas if not r.is_shard]

    @property
    def sharded(self) -> bool:
        return bool(self.shard_replicas)

    def replica(self, name: str) -> ReplicaSpec:
        for r in self.replicas:
            if r.name == name:
                return r
        raise KeyError(f"no replica named {name!r} in config")

    @property
    def primary_root(self) -> Path:
        return Path(self.replica(self.primary).root).expanduser()


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run 'photovault init' to create a starter file."
        )
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    vault = raw.get("vault", {})
    replicas = [
        ReplicaSpec(
            name=r["name"], kind=r.get("kind", "local"), root=r["root"],
            host=r.get("host"), offline=bool(r.get("offline", False)),
            mode=r.get("mode", "full"), capacity=str(r.get("capacity", "auto")),
        )
        for r in raw.get("replica", [])
    ]
    if not replicas:
        raise ValueError("config defines no replicas")

    cfg = Config(
        primary=vault.get("primary", replicas[0].name),
        min_copies=int(vault.get("min_copies", 3)),
        require_offline_copy=bool(vault.get("require_offline_copy", True)),
        scrub_days=int(vault.get("scrub_days", 30)),
        replicas=replicas,
        sources=[SourceSpec(device=s["device"], path=s["path"])
                 for s in raw.get("source", [])],
    )
    if "catalog" in vault:
        cfg.catalog_path = Path(vault["catalog"]).expanduser()
    cfg.replica(cfg.primary)  # fail fast if primary points at nothing
    for r in cfg.replicas:
        if r.mode not in ("full", "shard"):
            raise ValueError(f"replica {r.name!r}: mode must be 'full' or 'shard'")
    if cfg.replica(cfg.primary).is_shard:
        raise ValueError(
            f"the primary replica {cfg.primary!r} cannot be a shard - ingest "
            f"needs somewhere to write every new photo before it is distributed")
    cfg.source_path = path    # so recovery kits can copy the real file
    return cfg


STARTER_CONFIG = """\
# PhotoVault configuration.
#
# The safety rule this file encodes: every photo must exist on `min_copies`
# independent devices, and at least one of them must be a drive that is
# normally unplugged (immune to ransomware, a bad `rm`, and power surges).

[vault]
primary = "mac"
min_copies = 3
require_offline_copy = true
scrub_days = 30          # re-read and re-hash every file at least this often

# ---------------------------------------------------------------- replicas

[[replica]]
name = "mac"
kind = "local"
root = "~/PhotoVault/library"

[[replica]]
name = "hdd"
kind = "local"
root = "/Volumes/CHANGE_ME/PhotoVault/library"
offline = true           # normally unplugged; absence is expected, not an error

[[replica]]
name = "win"
kind = "rsync"
host = "CHANGE_ME@192.168.1.50"     # needs an SSH server on the Windows laptop
root = "/d/PhotoVault/library"

# If one drive cannot hold the whole library, mark drives as shards and they
# will each hold a computed subset instead. Capacity may be "auto" or a size
# like "1.8TB". Run 'photovault plan' to preview the split before syncing.
#
# [[replica]]
# name = "hdd2"
# kind = "local"
# root = "/Volumes/Backup2/PhotoVault/library"
# offline = true
# mode = "shard"
# capacity = "auto"

# Later, when you want offsite:
# [[replica]]
# name = "gcs"
# kind = "gcs"
# root = "gs://your-bucket/library"

# ----------------------------------------------------------------- sources
# Folders that are scanned for new photos. Sources are read-only; PhotoVault
# copies out of them and never modifies or deletes anything inside.

[[source]]
device = "mac"
path = "~/Pictures/Photos Library.photoslibrary/originals"

[[source]]
device = "iphone"
path = "~/PhotoVault/inbox/iphone"

[[source]]
device = "ipad"
path = "~/PhotoVault/inbox/ipad"
"""
