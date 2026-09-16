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
from urllib.parse import parse_qs, urlparse

from .. import health, placement, sync
from ..catalog import Catalog
from ..config import Config
from . import thumbs
from .jobs import JobRunner

STATIC_DIR = Path(__file__).parent / "static"
_local = threading.local()


class VaultHandler(BaseHTTPRequestHandler):
    cfg: Config
    jobs: JobRunner
    server_version = "PhotoVault"

    # Each worker thread keeps its own SQLite connection, for the same reason
    # jobs do: connections are not safe to share across threads.
    @property
    def catalog(self) -> Catalog:
        cat = getattr(_local, "catalog", None)
        if cat is None:
            cat = _local.catalog = Catalog(self.cfg.catalog_path)
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

        if name == "timeline":
            return self._json({
                "months": [dict(r) for r in cat.timeline()],
                "undated": cat.undated_count(),
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

        if name == "log":
            return self._json({"events": [
                dict(e) for e in cat.recent_events(int(q.get("limit", 40)))]})

        return self._json({"error": "not found"}, 404)

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
          open_browser: bool = True) -> None:
    handler = type("BoundHandler", (VaultHandler,),
                   {"cfg": cfg, "jobs": JobRunner(cfg)})
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
