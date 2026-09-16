"""Staging photos uploaded through the browser.

Uploads land in a staging directory and are then fed through the ordinary
ingest path - the same hashing, dedup, date extraction and replication as a
folder on disk. There is no separate "uploaded photo" concept, because a second
import path is a second set of bugs.

The one genuinely dangerous input here is the *filename*. A browser sends the
relative path of each file in a chosen folder, and that string is attacker-
controlled in the general case: `../../../../.ssh/authorized_keys` is a valid
thing for an HTTP client to claim. Every component is therefore rebuilt from
scratch rather than cleaned, because sanitising by removal is a game you lose
to inputs like `....//`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .ingest import WANTED_EXT, ingest_source, iter_media
from .mediatime import normalize_ext

MAX_COMPONENT = 100
MAX_DEPTH = 12
CHUNK = 1024 * 1024

# Everything outside this set is replaced, rather than a blacklist of things to
# strip. Reserved Windows device names are handled separately.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_component(name: str) -> str:
    """Rebuild one path segment from characters known to be safe."""
    name = unicodedata.normalize("NFKD", name)
    name = _SAFE.sub("_", name).strip("._") or "file"
    if name.split(".")[0].lower() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > MAX_COMPONENT:
        stem, _, ext = name.rpartition(".")
        keep = MAX_COMPONENT - len(ext) - 1
        name = f"{stem[:keep]}.{ext}" if stem and keep > 0 else name[:MAX_COMPONENT]
    return name


def safe_relative_path(raw: str) -> Path | None:
    """Turn a browser-supplied relative path into something safe, or None.

    `..` and absolute paths are dropped rather than rewritten: a request that
    contains them is not a request we want to guess the intent of.
    """
    if not raw or "\x00" in raw:
        return None
    # A browser always sends a path relative to the chosen folder. An absolute
    # path, a Windows drive letter or a UNC prefix is therefore never something
    # a real client produces - only something a prober sends. Refuse rather
    # than reinterpret: it would land inside staging and be harmless, but
    # silently accepting a probe is how the next one goes unnoticed.
    if raw[0] in "/\\" or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [p for p in re.split(r"[\\/]+", raw) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    parts = [safe_component(p) for p in parts][-MAX_DEPTH:]
    if not parts:
        return None
    return Path(*parts)


def staging_dir(cfg: Config) -> Path:
    """Where uploads are held before import.

    Deliberately a sibling of the primary library, never inside it: anything
    under a replica root is enumerated by list_present() and would be mistaken
    for stored content before it has even been imported.
    """
    return cfg.primary_root.parent / "uploads"


@dataclass
class UploadResult:
    stored: bool = False
    path: str = ""
    size: int = 0
    reason: str = ""


def accept(cfg: Config, rel_path: str, stream, length: int) -> UploadResult:
    """Stream one uploaded file to staging. Never buffers the whole file."""
    safe = safe_relative_path(rel_path)
    if safe is None:
        return UploadResult(reason="unsafe path")
    if normalize_ext(safe) not in WANTED_EXT:
        return UploadResult(reason="not a photo or video")

    root = staging_dir(cfg)
    dest = root / safe
    try:
        dest.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        # Belt and braces: even after rebuilding the path, confirm the result
        # actually lands inside the staging directory.
        return UploadResult(reason="path escapes the staging directory")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            remaining = length
            while remaining > 0:
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    break
                fh.write(block)
                written += len(block)
                remaining -= len(block)
        if written != length:
            tmp.unlink(missing_ok=True)
            return UploadResult(reason="upload was truncated")
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return UploadResult(reason=str(exc))
    return UploadResult(stored=True, path=str(safe), size=written)


@dataclass
class StagedSummary:
    files: int = 0
    bytes: int = 0
    samples: list[str] = field(default_factory=list)


def staged(cfg: Config) -> StagedSummary:
    root = staging_dir(cfg)
    summary = StagedSummary()
    if not root.is_dir():
        return summary
    for path in iter_media(root):
        if normalize_ext(path) not in WANTED_EXT:
            continue
        summary.files += 1
        try:
            summary.bytes += path.stat().st_size
        except OSError:
            continue
        if len(summary.samples) < 5:
            summary.samples.append(str(path.relative_to(root)))
    return summary


def discard(cfg: Config) -> int:
    """Throw away everything staged but not yet imported."""
    import shutil

    root = staging_dir(cfg)
    n = staged(cfg).files
    shutil.rmtree(root, ignore_errors=True)
    return n


def ingest_staged(cfg: Config, catalog: Catalog, *, device: str = "upload",
                  progress=None):
    """Import everything staged, using the normal pipeline."""
    root = staging_dir(cfg)
    if not root.is_dir():
        from .ingest import IngestStats
        return IngestStats()
    return ingest_source(cfg, catalog, device, root, progress=progress)


def clear_imported(cfg: Config, catalog: Catalog) -> tuple[int, list[str]]:
    """Remove staged files that now have min_copies verified copies.

    Uses the same evidence bar as clearing a phone: replicas are asked to
    re-hash the stored bytes. Staging holds the only copy of a just-uploaded
    photo until replication has actually happened.
    """
    from .importer import reclaimable

    report = reclaimable(cfg, catalog, staging_dir(cfg), device="upload", apply=True)
    _prune_empty(staging_dir(cfg))
    return report.deleted, [f"{p.name}: {why}" for p, _, why in report.held[:10]]


def _prune_empty(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
