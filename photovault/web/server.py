"""A small local web server for PhotoVault.

Deliberately built on Python's standard library. A photo manager that stops
working because a frontend dependency was unpublished is a bad photo manager,
and this way there is no build step to maintain.

SECURITY: the server binds to 127.0.0.1 - the loopback address, reachable only
from this machine. That is not a detail to change casually. These endpoints can
copy, overwrite and delete files, and there is no authentication, so binding to
0.0.0.0 would hand anyone on the network (or the coffee shop wifi) full control
of your photo library.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import config as configmod
from .. import duplicates, operations, trash, uploads
from .. import health, placement, sync
from ..catalog import Catalog
from ..config import Config
from . import thumbs
from .jobs import JobRunner

STATIC_DIR = Path(__file__).parent / "static"
_local = threading.local()


def _all_backup_files(cfg) -> list[dict]:
    """Every backup artifact on every reachable local device."""
    from ..backups import BACKUP_DIR
    from ..replicas import LocalDriver

    out = []
    for spec in cfg.replicas:
        if spec.kind != "local":
            continue
        drv = LocalDriver(spec)
        try:
            if not drv.available():
                continue
        except Exception:
            continue
        root = Path(spec.root).expanduser() / BACKUP_DIR
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.suffix == ".sha256" or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({"replica": spec.name, "name": path.name,
                        "size": stat.st_size, "mtime": stat.st_mtime,
                        "verified": (path.with_suffix(path.suffix
                                                      + ".sha256")).is_file()})
    return out


class AppState:
    """Holds the live config. Editing it from the UI swaps `cfg` and bumps
    `generation`, which each worker thread notices and reopens its catalog
    against - the catalog path itself can change."""

    def __init__(self, cfg: Config, config_path: Path):
        self.cfg = cfg
        self.config_path = config_path
        self.jobs = JobRunner(cfg)
        self.generation = 0
        self.lock = threading.Lock()

    def replace(self, cfg: Config) -> None:
        with self.lock:
            self.cfg = cfg
            self.jobs.cfg = cfg
            self.generation += 1


class VaultHandler(BaseHTTPRequestHandler):
    state: AppState
    server_version = "PhotoVault"

    @property
    def cfg(self) -> Config:
        return self.state.cfg

    @property
    def jobs(self) -> JobRunner:
        return self.state.jobs

    # Each worker thread keeps its own SQLite connection, for the same reason
    # jobs do: connections are not safe to share across threads.
    @property
    def catalog(self) -> Catalog:
        cat = getattr(_local, "catalog", None)
        gen = getattr(_local, "generation", -1)
        if cat is None or gen != self.state.generation:
            if cat is not None:
                cat.close()
            cat = _local.catalog = Catalog(self.cfg.catalog_path)
            _local.generation = self.state.generation
            for spec in self.cfg.replicas:
                cat.upsert_replica(spec.name, spec.kind, spec.root,
                                   host=spec.host, is_offline=spec.offline)
        return cat

    def log_message(self, fmt, *args):
        pass  # the default logger prints a line per thumbnail; far too noisy

    # ------------------------------------------------------------------ verbs

    def do_GET(self):
        url = urlparse(self.path)
        route = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if route == "/" or route == "/index.html":
                return self._static("index.html")
            if route.startswith("/static/"):
                return self._static(route[len("/static/"):])
            if route.startswith("/api/"):
                return self._api_get(route[len("/api/"):], q)
        except BrokenPipeError:
            return  # the browser navigated away mid-response; harmless
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)

        # Uploads carry raw bytes, not JSON, and must be streamed to disk -
        # reading a 500 MB video into memory to parse it would be absurd.
        if url.path == "/api/upload":
            return self._accept_upload()

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "invalid JSON body"}, 400)

        try:
            if url.path == "/api/jobs":
                action = body.pop("action", "")
                return self._json(self.jobs.start(action, **body))
            if url.path == "/api/jobs/cancel":
                ok = self.jobs.cancel(int(body.get("id", 0)))
                return self._json({"cancelled": ok})
            if url.path == "/api/thumbs/clear":
                return self._json({"cleared": thumbs.clear_cache()})
            if url.path == "/api/config":
                return self._save_config(body)
            if url.path == "/api/photos/delete":
                hashes = body.get("hashes") or []
                if not isinstance(hashes, list) or not hashes:
                    return self._json({"error": "expected a 'hashes' list"}, 400)
                st = trash.delete(self.catalog, hashes)
                return self._json({"moved": st.moved, "bytes": st.bytes,
                                   "retention_days": self.cfg.trash_days})
            if url.path == "/api/trash/restore":
                hashes = body.get("hashes") or []
                return self._json({"restored": trash.restore(self.catalog, hashes)})
            if url.path == "/api/trash/purge":
                if not body.get("confirm"):
                    return self._json({"error": "purging needs an explicit "
                                                "confirmation"}, 400)
                rep = trash.purge(self.cfg, self.catalog,
                                  hashes=body.get("hashes"),
                                  expired_only=bool(body.get("expired_only")),
                                  dry_run=False)
                if rep.skipped and not rep.purged:
                    return self._json({"error": rep.skipped[0][1]}, 409)
                return self._json({"purged": rep.purged,
                                   "bytes_freed": rep.bytes_freed,
                                   "errors": rep.errors[:10]})
            if url.path == "/api/sources/test":
                return self._test_source(body)
            if url.path == "/api/uploads/discard":
                return self._json({"discarded": uploads.discard(self.cfg)})
            if url.path == "/api/duplicates/decide":
                decisions = body.get("decisions")
                if not isinstance(decisions, dict):
                    return self._json({"error": "expected a 'decisions' object"}, 400)
                n = duplicates.decide(self.catalog, decisions)
                return self._json({"recorded": n})
        except TypeError as exc:
            return self._json({"error": f"bad parameters: {exc}"}, 400)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)
        self._json({"error": "not found"}, 404)

    # -------------------------------------------------------------- GET routes

    def _api_get(self, name: str, q: dict):
        cat = self.catalog

        if name == "status":
            h = health.assess(self.cfg, cat)
            reach = {n: ok for n, _, ok in sync.available_replicas(self.cfg)}
            return self._json({
                "total_assets": h.total_assets, "total_bytes": h.total_bytes,
                "min_copies": self.cfg.min_copies,
                "require_offline_copy": self.cfg.require_offline_copy,
                "primary": self.cfg.primary,
                "replicas": [
                    {"name": n, "reachable": reach.get(n, False), **s}
                    for n, s in h.per_replica.items()
                ],
                "copies_histogram": h.copies_histogram,
                "checks": [
                    {"id": "single", "label": "No single-copy photos",
                     "ok": h.at_risk_single_copy == 0, "count": h.at_risk_single_copy},
                    {"id": "redundancy",
                     "label": f"At least {self.cfg.min_copies} copies",
                     "ok": h.underprotected == 0, "count": h.underprotected},
                    {"id": "offline", "label": "An offline copy exists",
                     "ok": h.no_offline_copy == 0, "count": h.no_offline_copy},
                    {"id": "integrity", "label": "No corruption detected",
                     "ok": h.corrupt == 0, "count": h.corrupt},
                ],
                "never_verified": h.never_verified,
                "healthy": h.ok,
                "busy": self.jobs.running,
                "thumbs": {**thumbs.backends(), "usable": thumbs.available()},
            })

        if name == "plan":
            if not self.cfg.sharded:
                return self._json({"sharded": False,
                                   "replicas": [r.name for r in self.cfg.replicas]})
            p = placement.build_plan(self.cfg, cat)
            return self._json({
                "sharded": True, "ok": p.ok,
                "total_bytes": p.total_bytes,
                "total_photos": len(p.assignments),
                "shard_copies_needed": p.shard_copies_needed,
                "unplaceable": len(p.unplaceable),
                "replicas": [{"name": n, **s} for n, s in p.per_replica.items()],
            })

        if name == "uploads":
            summary = uploads.staged(self.cfg)
            return self._json({
                "files": summary.files, "bytes": summary.bytes,
                "samples": summary.samples,
                "staging": str(uploads.staging_dir(self.cfg)),
            })

        if name == "duplicates":
            threshold = max(0, min(16, int(q.get("threshold",
                                                 duplicates.DEFAULT_THRESHOLD))))
            groups = duplicates.find_groups(self.cfg, cat, threshold=threshold)
            limit = int(q.get("limit", 60))
            offset = int(q.get("offset", 0))
            page = groups[offset:offset + limit]
            pending = int(cat.db.execute(
                "SELECT COUNT(*) n FROM asset WHERE phash IS NULL "
                "AND media_kind='image'").fetchone()["n"])
            return self._json({
                "total_groups": len(groups),
                "recoverable_bytes": sum(g.wasted_bytes for g in groups),
                "unanalysed": pending,
                "threshold": threshold,
                "can_analyse": __import__("photovault.perceptual",
                                          fromlist=["x"]).available(),
                "groups": [
                    {"id": g.id, "suggested_keep": g.suggested_keep,
                     "wasted_bytes": g.wasted_bytes,
                     "members": [vars(m) for m in g.members]}
                    for g in page
                ],
            })

        if name == "timeline":
            return self._json({
                "months": [dict(r) for r in cat.timeline()],
                "undated": cat.undated_count(),
            })

        if name == "backups":
            from .. import backups as bk
            snaps = bk.available(self.cfg)
            by_kind = {}
            for label, pattern in (("catalog", "catalog-"),
                                   ("immich-db", "immich-db-"),
                                   ("immich-albums", "immich-albums-")):
                items = [s for s in _all_backup_files(self.cfg)
                         if s["name"].startswith(pattern)]
                items.sort(key=lambda x: x["name"], reverse=True)
                by_kind[label] = items
            return self._json({
                "age_days": bk.age_days(self.cfg),
                "immich_configured": self.cfg.immich.enabled,
                "keep": self.cfg.backup_keep,
                "kinds": by_kind,
            })

        if name == "operations":
            from ..sync import available_replicas
            devices = [{"name": n, "reachable": ok,
                        "offline": self.cfg.replica(n).offline}
                       for n, _d, ok in available_replicas(self.cfg)]
            return self._json({
                "operations": operations.describe(self.cfg, cat),
                "devices": devices,
                "trash_days": self.cfg.trash_days,
                "scrub_days": self.cfg.scrub_days,
            })

        if name == "trash":
            rows, total = cat.browse(trashed=True,
                                     limit=min(500, int(q.get("limit", 120))),
                                     offset=int(q.get("offset", 0)))
            return self._json({
                "total": total, **trash.summary(self.cfg, cat),
                "photos": [
                    {"hash": r["hash"], "rel_path": r["rel_path"],
                     "captured_at": r["captured_at"], "size": r["size"],
                     "kind": r["media_kind"], "ext": r["ext"],
                     "deleted_at": r["deleted_at"], "copies": r["copies"]}
                    for r in rows],
            })

        if name == "photos":
            rows, total = cat.browse(
                year=q.get("year"), month=q.get("month"), kind=q.get("kind"),
                undated=q.get("undated") == "1",
                limit=min(500, int(q.get("limit", 120))),
                offset=int(q.get("offset", 0)),
            )
            return self._json({
                "total": total,
                "photos": [
                    {"hash": r["hash"], "rel_path": r["rel_path"],
                     "captured_at": r["captured_at"], "time_source": r["time_source"],
                     "size": r["size"], "kind": r["media_kind"], "ext": r["ext"],
                     "copies": r["copies"], "bad": r["bad"]}
                    for r in rows
                ],
            })

        if name.startswith("photo/"):
            parts = name.split("/")
            hash_ = parts[1]
            detail = cat.asset_detail(hash_)
            if not detail:
                return self._json({"error": "unknown photo"}, 404)
            rel = detail["asset"]["rel_path"]

            if len(parts) == 2:
                return self._json(detail)
            if parts[2] == "thumb":
                src = sync.local_copy(self.cfg, cat, hash_, rel)
                if src:
                    thumb = thumbs.get_or_make(hash_, src,
                                               detail["asset"]["media_kind"])
                    if thumb:
                        return self._file(thumb, "image/jpeg", cacheable=True)
                return self._json({"error": "no thumbnail"}, 404)
            if parts[2] == "full":
                src = sync.local_copy(self.cfg, cat, hash_, rel)
                if not src:
                    return self._json({"error": "no local copy reachable"}, 404)
                mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
                return self._file(src, mime, cacheable=True)

        if name == "jobs":
            return self._json({"jobs": self.jobs.list()})

        if name == "replicas":
            return self._json({"replicas": [
                {"name": n, "kind": self.cfg.replica(n).kind,
                 "root": self.cfg.replica(n).root,
                 "offline": self.cfg.replica(n).offline, "reachable": ok}
                for n, _, ok in sync.available_replicas(self.cfg)
            ], "primary": self.cfg.primary,
                "sources": [{"device": s.device, "path": s.path}
                            for s in self.cfg.sources]})

        if name == "config":
            return self._json({
                "path": str(self.state.config_path),
                "config": configmod.to_dict(self.cfg),
                "toml": configmod.render(configmod.to_dict(self.cfg)),
            })

        if name == "drives":
            from ..wizard import find_drives
            from ..placement import usable_capacity
            drives = [{"label": d.label, "path": str(d.path),
                       "total": d.total, "free": d.free} for d in find_drives()]
            return self._json({
                "drives": drives,
                "home": str(Path.home()),
                "configured": [r.root for r in self.cfg.replicas],
            })

        if name == "log":
            return self._json({"events": [
                dict(e) for e in cat.recent_events(int(q.get("limit", 40)))]})

        return self._json({"error": "not found"}, 404)

    def _accept_upload(self):
        """Stream one uploaded file into staging.

        The relative path arrives in a header rather than the URL so that
        spaces, slashes and non-ASCII names survive intact; it is treated as
        hostile input and rebuilt, never merely cleaned.
        """
        raw_path = self.headers.get("X-PV-Path", "")
        try:
            raw_path = unquote(raw_path)
            length = int(self.headers.get("Content-Length") or 0)
            # The browser knows when the file was last written; without it a
            # scan or screenshot has no date at all and lands under "today".
            modified_ms = int(self.headers.get("X-PV-Modified") or 0) or None
        except ValueError:
            return self._json({"error": "bad headers"}, 400)
        if length <= 0:
            return self._json({"error": "empty upload"}, 400)

        result = uploads.accept(self.cfg, raw_path, self.rfile, length,
                                modified_ms=modified_ms)
        if not result.stored:
            # Drain whatever is left so the connection stays usable.
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
            return self._json({"error": result.reason, "path": raw_path}, 400)
        return self._json({"stored": True, "path": result.path,
                           "size": result.size})

    def _test_source(self, body: dict):
        """Check a source is reachable before the user commits to it."""
        kind = body.get("kind", "local")
        if kind == "immich":
            from ..immich import ImmichClient, ImmichError
            try:
                client = ImmichClient(body.get("url", ""), body.get("api_key", ""))
            except ImmichError as exc:
                return self._json({"ok": False, "detail": str(exc)})
            # Deliberately not ping(): it returns a bare False, so a wrong
            # API key and an unreachable host looked identical. The real call
            # carries a reason the user can act on.
            try:
                managed = sum(1 for _ in client.managed_assets(page_size=100))
            except ImmichError as exc:
                return self._json({"ok": False, "detail": str(exc)})
            return self._json({
                "ok": True,
                "detail": f"connected — {managed:,} photo"
                          f"{'' if managed == 1 else 's'} waiting to be archived"})

        if kind == "adb":
            from ..importer import adb_available, adb_devices
            if not adb_available():
                return self._json({"ok": False, "detail":
                                   "adb not installed (brew install "
                                   "android-platform-tools)"})
            found = adb_devices()
            return self._json({"ok": bool(found), "detail":
                               f"{len(found)} device(s) connected" if found
                               else "no Android device connected"})

        path = Path(body.get("path", "")).expanduser()
        if not path.is_dir():
            return self._json({"ok": False, "detail": "folder does not exist"})
        from ..ingest import WANTED_EXT, iter_media
        from ..mediatime import normalize_ext
        n = sum(1 for p in iter_media(path) if normalize_ext(p) in WANTED_EXT)
        return self._json({"ok": True,
                           "detail": f"{n:,} photo{'' if n == 1 else 's'} found"})

    def _save_config(self, body: dict):
        """Validate and persist a config edited in the UI.

        configmod.save() renders to TOML and loads it back through the ordinary
        parser before writing anything, so the UI cannot produce a file the CLI
        would reject. It also keeps a .bak and writes atomically - a truncated
        config would take the whole system down.
        """
        if self.jobs.running:
            return self._json(
                {"error": "an operation is running; wait for it to finish"}, 409)
        data = body.get("config")
        if not isinstance(data, dict):
            return self._json({"error": "expected a 'config' object"}, 400)
        try:
            cfg = configmod.save(data, self.state.config_path)
        except (ValueError, KeyError, TypeError) as exc:
            return self._json({"error": f"invalid config: {exc}"}, 400)
        except OSError as exc:
            return self._json({"error": f"could not write config: {exc}"}, 500)

        self.state.replace(cfg)
        return self._json({"saved": True, "path": str(self.state.config_path),
                           "config": configmod.to_dict(cfg)})

    # ------------------------------------------------------------- responders

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, mime: str, cacheable: bool = False):
        try:
            data = path.read_bytes()
        except OSError:
            return self._json({"error": "unreadable"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # Content-addressed URLs can never change meaning, so they are safe to
        # cache forever - that is another dividend of hashing by content.
        self.send_header("Cache-Control",
                         "public, max-age=31536000, immutable" if cacheable
                         else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str):
        # Reject path traversal (../../etc/passwd) before touching the disk.
        safe = posixpath.normpath("/" + rel).lstrip("/")
        path = (STATIC_DIR / safe).resolve()
        if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
            return self._json({"error": "not found"}, 404)
        mime = mimetypes.guess_type(path.name)[0] or "text/plain"
        return self._file(path, mime + ("; charset=utf-8"
                                        if mime.startswith("text/") else ""))


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8723,
          open_browser: bool = True, config_path: Path | None = None) -> None:
    state = AppState(cfg, config_path or cfg.source_path
                     or configmod.DEFAULT_CONFIG_PATH)
    handler = type("BoundHandler", (VaultHandler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"

    print(f"PhotoVault UI running at \033[1m{url}\033[0m")
    if host == "127.0.0.1":
        print("\033[2mReachable only from this Mac. Press Ctrl+C to stop.\033[0m")
    else:
        print(f"\033[33mWarning: bound to {host} - anyone on your network can "
              f"control this library.\033[0m")
    if not thumbs.available():
        print("\033[2mNo thumbnailer found; install Pillow for image previews.\033[0m")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
