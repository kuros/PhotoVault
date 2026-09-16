"""Tests for the web layer: HTTP API, background jobs, and safety guards.

These run a real server on a real socket against a real temporary library.
Mocking the HTTP layer would leave the routing, JSON encoding and path-safety
logic untested - which is precisely the part most likely to be wrong.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from photovault import ingest, sync
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec
from photovault.web.jobs import JobRunner
from photovault.web.server import VaultHandler

from make_fixtures import build as build_fixtures


class WebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-web-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "source"
        build_fixtures(self.source)

        self.cfg = Config(
            primary="mac", min_copies=3, require_offline_copy=True,
            catalog_path=self.tmp / "catalog.db",
            replicas=[
                ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                ReplicaSpec("hdd", "local", str(self.tmp / "hdd"), offline=True),
                ReplicaSpec("win", "local", str(self.tmp / "win")),
            ],
            sources=[SourceSpec("phone", str(self.source))],
        )
        cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            cat.upsert_replica(spec.name, spec.kind, spec.root,
                               is_offline=spec.offline)
        ingest.ingest_source(self.cfg, cat, "phone", self.source)
        sync.push(self.cfg, cat, "hdd")
        cat.close()

        handler = type("T", (VaultHandler,),
                       {"cfg": self.cfg, "jobs": JobRunner(self.cfg)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def get(self, path: str):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read())

    def raw(self, path: str):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()

    def post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def wait_idle(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(j["state"] == "running"
                       for j in self.get("/api/jobs")["jobs"]):
                return
            time.sleep(0.05)
        self.fail("job did not finish in time")


class TestApi(WebTestCase):
    def test_status_reports_redundancy_shortfall(self):
        s = self.get("/api/status")
        self.assertEqual(s["total_assets"], 13)
        self.assertFalse(s["healthy"])
        checks = {c["id"]: c for c in s["checks"]}
        self.assertTrue(checks["single"]["ok"])        # two copies exist
        self.assertFalse(checks["redundancy"]["ok"])   # but three are required
        self.assertEqual(checks["redundancy"]["count"], 13)

    def test_photos_pagination_and_filtering(self):
        page = self.get("/api/photos?limit=5")
        self.assertEqual(page["total"], 13)
        self.assertEqual(len(page["photos"]), 5)

        second = self.get("/api/photos?limit=5&offset=5")
        self.assertNotEqual({p["hash"] for p in page["photos"]},
                            {p["hash"] for p in second["photos"]})

        self.assertEqual(self.get("/api/photos?kind=video")["total"], 0)
        self.assertEqual(self.get("/api/photos?year=2011")["total"], 1)

    def test_timeline_groups_by_month(self):
        t = self.get("/api/timeline")
        self.assertTrue(t["months"])
        self.assertEqual(sum(m["n"] for m in t["months"]) + t["undated"], 13)

    def test_photo_detail_lists_every_copy(self):
        first = self.get("/api/photos?limit=1")["photos"][0]
        d = self.get(f"/api/photo/{first['hash']}")
        self.assertEqual({p["replica"] for p in d["placements"]}, {"mac", "hdd"})
        self.assertEqual(d["sources"][0]["device"], "phone")

    def test_unknown_photo_is_404_not_a_crash(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/photo/" + "0" * 64)
        self.assertEqual(cm.exception.code, 404)

    def test_full_image_serves_real_bytes(self):
        first = self.get("/api/photos?limit=1")["photos"][0]
        status, headers, body = self.raw(f"/api/photo/{first['hash']}/full")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), first["size"])
        # Content-addressed URLs are immutable, so they must be cacheable.
        self.assertIn("immutable", headers["Cache-Control"])


class TestSafety(WebTestCase):
    def test_path_traversal_is_blocked(self):
        for attack in ("/static/../../../../etc/passwd",
                       "/static/....//....//etc/passwd",
                       "/static/%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
            with self.subTest(attack=attack):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self.raw(attack)
                self.assertEqual(cm.exception.code, 404)

    def test_static_files_are_still_served(self):
        for name in ("/", "/static/app.css", "/static/app.js"):
            with self.subTest(name=name):
                status, _, body = self.raw(name)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_malformed_json_gets_400_not_500(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)


class TestJobs(WebTestCase):
    def test_sync_job_reaches_full_redundancy(self):
        job = self.post("/api/jobs", {"action": "sync"})
        self.assertEqual(job["state"], "running")
        self.wait_idle()

        done = self.get("/api/jobs")["jobs"][0]
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["result"]["copied"], 13)
        self.assertTrue(self.get("/api/status")["healthy"])

    def test_only_one_mutating_job_runs_at_a_time(self):
        """Two concurrent writers would contend over the same catalog rows for
        no gain, since the work is disk-bound."""
        self.post("/api/jobs", {"action": "sync"})
        second = self.post("/api/jobs", {"action": "scrub"})
        self.assertIn("error", second)
        self.wait_idle()

    def test_unknown_action_is_rejected(self):
        self.assertIn("error", self.post("/api/jobs", {"action": "rm -rf"}))

    def test_failed_job_is_reported_not_swallowed(self):
        res = self.post("/api/jobs", {"action": "ingest", "device": "nope"})
        self.assertEqual(res["state"], "running")
        self.wait_idle()
        job = self.get("/api/jobs")["jobs"][0]
        self.assertEqual(job["state"], "failed")
        self.assertIn("no matching sources", job["message"])

    def test_scrub_job_reports_counts(self):
        self.post("/api/jobs", {"action": "scrub", "force": True})
        self.wait_idle()
        job = self.get("/api/jobs")["jobs"][0]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["corrupt"], 0)
        self.assertEqual(job["result"]["checked"], 26)  # 13 photos x 2 replicas


class TestThumbnails(unittest.TestCase):
    def test_cache_path_fans_out_by_hash_prefix(self):
        from photovault.web import thumbs
        p = thumbs.cache_path("abcdef123456")
        self.assertEqual(p.parent.name, "ab")
        self.assertTrue(p.name.startswith("abcdef"))

    def test_missing_source_returns_none_rather_than_raising(self):
        from photovault.web import thumbs
        self.assertIsNone(
            thumbs.get_or_make("deadbeef" * 8, Path("/nonexistent/x.jpg")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
