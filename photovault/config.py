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
    kind: str = "local"   # local = a folder; adb = an Android device over USB
    # Inboxes should empty once their photos are safely in the library;
    # a folder you also browse (an Apple Photos library) must never be touched.
    clear_after_import: bool = False


@dataclass
class ImmichSpec:
    """Optional Immich stack that `photovault start` brings up alongside the UI."""
    compose_file: str = ""
    url: str = "http://localhost:2283"

    @property
    def enabled(self) -> bool:
        return bool(self.compose_file)

    @property
    def path(self) -> Path | None:
        return Path(self.compose_file).expanduser() if self.compose_file else None


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
    immich: ImmichSpec = field(default_factory=ImmichSpec)

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
        sources=[SourceSpec(device=s["device"], path=s.get("path", ""),
                            kind=s.get("kind", "local"),
                            clear_after_import=bool(s.get("clear_after_import", False)))
                 for s in raw.get("source", [])],
    )
    immich = raw.get("immich", {})
    cfg.immich = ImmichSpec(
        compose_file=immich.get("compose_file", ""),
        url=immich.get("url", "http://localhost:2283"),
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

# Inbox folders: point Image Capture or a sync app here. clear_after_import
# empties them once each photo is verified present in the library, so the
# inbox does not grow into a second copy of everything.

[[source]]
device = "iphone"
path = "~/PhotoVault/inbox/iphone"
clear_after_import = true

[[source]]
device = "ipad"
path = "~/PhotoVault/inbox/ipad"
clear_after_import = true
"""



# --------------------------------------------------------------------- writing

def _toml_str(value: str) -> str:
    """TOML basic string. Paths with backslashes (Windows) must be escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(data: dict) -> str:
    """Render a config dict back to TOML.

    Hand-rolled rather than using a TOML writer: the file is small, the shape
    is fixed, and this keeps the comments that make the file readable by a
    human who opens it in six months.
    """
    vault = data.get("vault", {})
    lines = [
        "# PhotoVault configuration.",
        "#",
        "# Every photo must exist on `min_copies` independent devices, at least",
        "# one of them normally unplugged.",
        "",
        "[vault]",
        f'primary = {_toml_str(vault.get("primary", "mac"))}',
        f'min_copies = {int(vault.get("min_copies", 3))}',
        f'require_offline_copy = {str(bool(vault.get("require_offline_copy", True))).lower()}',
        f'scrub_days = {int(vault.get("scrub_days", 30))}',
    ]
    if vault.get("catalog"):
        lines.append(f'catalog = {_toml_str(vault["catalog"])}')

    immich = data.get("immich") or {}
    if immich.get("compose_file"):
        lines += ["", "# Optional: `photovault start` brings this up with the UI.",
                  "[immich]",
                  f'compose_file = {_toml_str(immich["compose_file"])}',
                  f'url = {_toml_str(immich.get("url", "http://localhost:2283"))}']

    lines += ["", "# ---------------------------------------------------------- replicas"]
    for r in data.get("replica", []):
        lines += ["", "[[replica]]",
                  f'name = {_toml_str(r["name"])}',
                  f'kind = {_toml_str(r.get("kind", "local"))}',
                  f'root = {_toml_str(r["root"])}']
        if r.get("host"):
            lines.append(f'host = {_toml_str(r["host"])}')
        if r.get("offline"):
            lines.append("offline = true")
        if r.get("mode", "full") != "full":
            lines.append(f'mode = {_toml_str(r["mode"])}')
        if r.get("capacity") and r["capacity"] != "auto":
            lines.append(f'capacity = {_toml_str(r["capacity"])}')

    lines += ["", "# ----------------------------------------------------------- sources",
              "# Read-only: PhotoVault copies out of these and never modifies them,",
              "# unless clear_after_import is set on an inbox folder."]
    for src in data.get("source", []):
        lines += ["", "[[source]]",
                  f'device = {_toml_str(src["device"])}']
        if src.get("kind", "local") != "local":
            lines.append(f'kind = {_toml_str(src["kind"])}')
        if src.get("path"):
            lines.append(f'path = {_toml_str(src["path"])}')
        if src.get("clear_after_import"):
            lines.append("clear_after_import = true")
    return "\n".join(lines).rstrip() + "\n"


def to_dict(cfg: "Config") -> dict:
    """The inverse of load(), for handing the current config to the UI."""
    return {
        "immich": {"compose_file": cfg.immich.compose_file, "url": cfg.immich.url},
        "vault": {
            "primary": cfg.primary, "min_copies": cfg.min_copies,
            "require_offline_copy": cfg.require_offline_copy,
            "scrub_days": cfg.scrub_days, "catalog": str(cfg.catalog_path),
        },
        "replica": [
            {"name": r.name, "kind": r.kind, "root": r.root, "host": r.host,
             "offline": r.offline, "mode": r.mode, "capacity": r.capacity}
            for r in cfg.replicas
        ],
        "source": [
            {"device": s.device, "kind": s.kind, "path": s.path,
             "clear_after_import": s.clear_after_import}
            for s in cfg.sources
        ],
    }


def save(data: dict, path: Path) -> Config:
    """Validate a config dict, then write it atomically, keeping one backup.

    Validation happens by rendering to TOML and loading it back through the
    ordinary load() path, so the UI cannot produce a config the CLI would
    reject. A config that fails to parse would take the whole system down, so
    nothing is written until a full round-trip succeeds.
    """
    import tempfile

    text = render(data)
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(text)
        probe = Path(fh.name)
    try:
        cfg = load(probe)          # raises on anything malformed
    finally:
        probe.unlink(missing_ok=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(text)
    tmp.replace(path)              # atomic: never a half-written config
    cfg.source_path = path
    return cfg
