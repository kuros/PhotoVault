"""A small client for Immich's REST API.

Immich is the front door: it has authenticated apps on every platform, so any
device can put a photo into it. PhotoVault then drains that intake into the
archive and asks Immich to release its own copy, leaving exactly one permanent
copy of every photo.

Two safety properties are load-bearing here.

**Only managed assets are ever touched.** An asset carrying a `libraryId` came
from an External Library - which, in this setup, *is* PhotoVault's library. Those
files already are the archive; pulling them would re-import what we already have,
and deleting them would ask Immich to remove the thing it is only reading. So
every operation filters to assets with no `libraryId`.

**Deletion is soft by default.** `DELETE /assets` takes a `force` flag; leaving
it false moves the asset to Immich's own trash rather than erasing it. PhotoVault
has already verified `min_copies` before it gets here, so this is belt and
braces - but it costs nothing and buys a second chance.

Written against the OpenAPI spec for the pinned Immich version. The API moves
quickly, so every call fails soft: a broken endpoint must leave a duplicate,
never a gap.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 60
PAGE_SIZE = 250


class ImmichError(RuntimeError):
    pass


@dataclass
class ImmichAsset:
    id: str
    filename: str
    original_path: str
    checksum: str
    kind: str                 # IMAGE | VIDEO | OTHER
    created_at: str | None
    trashed: bool

    @property
    def is_media(self) -> bool:
        return self.kind in ("IMAGE", "VIDEO")


def resolve_key(raw: str) -> str:
    """Allow `env:VAR` so an API key need not sit in a config file."""
    if raw.startswith("env:"):
        value = os.environ.get(raw[4:], "")
        if not value:
            raise ImmichError(f"environment variable {raw[4:]} is not set")
        return value
    return raw


class ImmichClient:
    def __init__(self, url: str, api_key: str):
        self.base = url.rstrip("/")
        if not self.base.endswith("/api"):
            self.base += "/api"
        self.key = resolve_key(api_key)

    # ------------------------------------------------------------- transport

    def _request(self, method: str, path: str, body: dict | None = None,
                 raw: bool = False, timeout: int = TIMEOUT):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"x-api-key": self.key, "Accept": "application/json",
                     **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if raw:
                    return resp.read()
                payload = resp.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise ImmichError(
                    f"Immich rejected the API key ({exc.code}). Create one under "
                    f"Account Settings -> API Keys.") from None
            raise ImmichError(f"{method} {path} failed: {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise ImmichError(f"cannot reach Immich at {self.base}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise ImmichError(f"{method} {path}: {exc}") from None

    # --------------------------------------------------------------- queries

    def ping(self) -> bool:
        try:
            return bool(self._request("GET", "/server/ping", timeout=10))
        except ImmichError:
            return False

    def managed_assets(self, *, page_size: int = PAGE_SIZE):
        """Yield assets Immich itself stores - never External Library ones.

        An asset with a libraryId lives in a folder Immich only reads, which in
        this setup is PhotoVault's own library. Draining those would re-import
        the archive into itself.
        """
        page = 1
        while page:
            body = {"page": page, "size": page_size, "withDeleted": False}
            result = self._request("POST", "/search/metadata", body)
            block = (result or {}).get("assets") or {}
            for item in block.get("items", []):
                if item.get("libraryId"):
                    continue          # External Library: not ours to move
                if item.get("isTrashed"):
                    continue
                asset = ImmichAsset(
                    id=item.get("id", ""),
                    filename=item.get("originalFileName") or item.get("id", ""),
                    original_path=item.get("originalPath", ""),
                    checksum=item.get("checksum", ""),
                    kind=(item.get("type") or "").upper(),
                    created_at=item.get("fileCreatedAt"),
                    trashed=bool(item.get("isTrashed")),
                )
                if asset.id and asset.is_media:
                    yield asset
            nxt = block.get("nextPage")
            page = int(nxt) if nxt else None

    def download(self, asset: ImmichAsset, dest: Path) -> int:
        """Stream one original to disk. Returns bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            f"{self.base}/assets/{asset.id}/original",
            headers={"x-api-key": self.key})
        tmp = dest.with_suffix(dest.suffix + ".part")
        written = 0
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT * 5) as resp, \
                    open(tmp, "wb") as fh:
                while block := resp.read(1024 * 1024):
                    fh.write(block)
                    written += len(block)
            tmp.replace(dest)
            self._stamp_mtime(dest, asset)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise ImmichError(f"downloading {asset.filename}: {exc}") from None
        return written

    @staticmethod
    def _stamp_mtime(path: Path, asset: ImmichAsset) -> None:
        """Set the staged file's mtime to when the photo was taken.

        A freshly downloaded file's mtime is the moment it was downloaded, so
        if metadata extraction ever fails the date fallback lands on today.
        Immich already parsed the capture time; carrying it across makes the
        last-resort fallback correct instead of actively wrong.
        """
        import os
        from datetime import datetime

        if not asset.created_at:
            return
        try:
            when = datetime.fromisoformat(asset.created_at.replace("Z", "+00:00"))
            stamp = when.timestamp()
            os.utime(path, (stamp, stamp))
        except (ValueError, OSError):
            pass

    def delete(self, asset_ids: list[str], *, force: bool = False) -> int:
        """Release Immich's own copies. Soft by default: they land in its trash.

        PhotoVault has already proved `min_copies` verified copies exist before
        calling this, so the soft delete is a second net rather than the only
        one - but it means a mistake here is recoverable from inside Immich.
        """
        if not asset_ids:
            return 0
        done = 0
        for start in range(0, len(asset_ids), 100):
            chunk = asset_ids[start:start + 100]
            self._request("DELETE", "/assets", {"ids": chunk, "force": force})
            done += len(chunk)
        return done

    def trigger_library_scan(self, library_id: str | None = None) -> bool:
        """Ask Immich to re-index external libraries, so drained photos reappear.

        Best effort: the endpoint has moved between versions, and a failed scan
        only means the photos show up on Immich's next nightly job instead.
        """
        for path in (f"/libraries/{library_id}/scan" if library_id else None,
                     "/libraries/scan"):
            if not path:
                continue
            try:
                self._request("POST", path, {})
                return True
            except ImmichError:
                continue
        return False
