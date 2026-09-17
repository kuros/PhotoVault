"""Backing up the parts of Immich that PhotoVault cannot rebuild.

Your photos are safe without this: they live in PhotoVault's library, which
Immich only reads. What lives *only* in Immich's Postgres is the judgement you
applied to them - albums, the names you gave to faces, favourites, archive
state. A `colima delete` takes all of it.

Two artifacts, answering two different questions.

**The database dump** answers *"how do I get my Immich back?"* It restores
everything, but only into a compatible Postgres and a compatible Immich schema.
Two years and a hundred releases later it may simply not apply.

**The album manifest** answers *"what did I decide belonged together?"* It is a
few kilobytes of JSON naming albums and the library paths of their photos. It
outlives Immich entirely - readable by any tool, or by a human rebuilding albums
by hand.

Neither replaces the other, and the manifest is the one that survives.
"""

from __future__ import annotations

import gzip
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config

# Excluded from the dump because Immich regenerates them. geodata_places alone
# is ~119 MB of static reverse-geocoding reference data, and the ML embeddings
# are re-derived by re-running the jobs against photos you still have. Schema is
# kept (exclude-table-DATA, not exclude-table) so a restore stays valid.
REDERIVABLE = (
    "geodata_places",
    "naturalearth_countries",
    "smart_search",
    "face_search",
)

CONTAINER_LIBRARY_PREFIX = "/mnt/photovault/"


@dataclass
class ImmichArtifacts:
    dump_name: str = ""
    dump_bytes: int = 0
    manifest_name: str = ""
    albums: int = 0
    tracked_assets: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def any_produced(self) -> bool:
        return bool(self.dump_name or self.manifest_name)


def _compose(cfg: Config, *args: str, timeout: int = 900, capture=True):
    path = cfg.immich.path
    return subprocess.run(
        ["docker", "compose", "-f", str(path), *args],
        capture_output=capture, timeout=timeout, cwd=str(path.parent))


def dump_database(cfg: Config, dest: Path) -> tuple[int, list[str]]:
    """Write a compressed pg_dump of Immich's database.

    Runs pg_dump *inside* the database container, so it works whether Postgres
    lives in a named volume, a bind mount, or another host - and needs no
    credentials on this side, since they are already the container's env.
    """
    errors: list[str] = []
    if not cfg.immich.enabled:
        return 0, ["no [immich] compose_file configured"]
    if cfg.immich.path is None or not cfg.immich.path.is_file():
        return 0, [f"compose file not found: {cfg.immich.path}"]

    excludes = " ".join(f"--exclude-table-data={t}" for t in REDERIVABLE)
    command = (f'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
               f'--no-owner --no-privileges {excludes}')

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        proc = _compose(cfg, "exec", "-T", "database", "sh", "-c", command,
                        capture=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 0, [f"pg_dump failed: {exc}"]

    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"")[:300].decode("utf-8", "replace").strip()
        return 0, [f"pg_dump failed: {detail or 'no output'}"]

    # A dump that does not end with PostgreSQL's completion marker was
    # truncated, and a truncated dump restores as a silently partial database.
    if b"PostgreSQL database dump complete" not in proc.stdout[-4000:]:
        errors.append("dump did not end cleanly - keeping it, but treat as suspect")

    with gzip.open(part, "wb", compresslevel=6) as gz:
        gz.write(proc.stdout)
    part.replace(dest)
    return dest.stat().st_size, errors


def album_manifest(cfg: Config) -> tuple[dict, list[str]]:
    """Record albums and the library paths of their photos.

    Assets are identified by their path *inside PhotoVault's library*, not by
    Immich asset id. Ids are meaningless once Immich is gone; a library path is
    the same string PhotoVault stores in `asset.rel_path`, so the manifest stays
    joinable to the archive forever.
    """
    from .immich import ImmichClient, ImmichError

    errors: list[str] = []
    source = next((s for s in cfg.sources if s.kind == "immich"), None)
    if source is None:
        return {}, ["no immich source configured - cannot read albums"]

    try:
        client = ImmichClient(source.url, source.api_key)
        albums = client._request("GET", "/albums") or []
    except ImmichError as exc:
        return {}, [f"could not list albums: {exc}"]

    out = {
        "photovault_album_manifest": 1,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "immich_url": source.url,
        "albums": [],
    }
    for album in albums:
        try:
            full = client._request("GET", f"/albums/{album['id']}") or {}
        except ImmichError as exc:
            errors.append(f"album {album.get('albumName', '?')}: {exc}")
            continue
        photos = []
        for asset in full.get("assets", []):
            path = asset.get("originalPath", "")
            photos.append({
                "file": asset.get("originalFileName", ""),
                # Relative to PhotoVault's library when it came from the
                # external library; absolute container path otherwise.
                "library_path": (path[len(CONTAINER_LIBRARY_PREFIX):]
                                 if path.startswith(CONTAINER_LIBRARY_PREFIX)
                                 else path),
                "checksum": asset.get("checksum", ""),
            })
        out["albums"].append({
            "name": full.get("albumName") or album.get("albumName", ""),
            "description": full.get("description", ""),
            "created_at": full.get("createdAt", ""),
            "photo_count": len(photos),
            "photos": photos,
        })
    return out, errors


def run(cfg: Config, dest_dir: Path, stamp: str) -> ImmichArtifacts:
    """Produce both artifacts into a staging directory."""
    art = ImmichArtifacts()
    if not cfg.immich.enabled:
        return art

    dump = dest_dir / f"immich-db-{stamp}.sql.gz"
    size, errors = dump_database(cfg, dump)
    art.errors.extend(errors)
    if size:
        art.dump_name, art.dump_bytes = dump.name, size

    manifest, errors = album_manifest(cfg)
    # Failing to read albums must never stop a photo backup: the photos are the
    # part that cannot be regenerated.
    art.errors.extend(errors)
    if manifest:
        path = dest_dir / f"immich-albums-{stamp}.json"
        path.write_text(json.dumps(manifest, indent=2))
        art.manifest_name = path.name
        art.albums = len(manifest["albums"])
        art.tracked_assets = sum(a["photo_count"] for a in manifest["albums"])
    return art
