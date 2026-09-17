"""Tests for the Immich integration, run against a stub Immich server.

A stub rather than a mock: the client's job is to speak HTTP correctly, so the
tests exercise real requests, real headers and real JSON over a real socket.
Built from the OpenAPI spec of the pinned Immich version.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from photovault import immich, importer, ingest, sync
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec

from make_fixtures import jpeg_with_exif

API_KEY = "test-key-123"


class StubImmich(BaseHTTPRequestHandler):
    """Implements only what PhotoVault uses, exactly as the spec describes it."""

    assets: dict = {}
    deleted: list = []
    scans: list = []
    require_key: bool = True

    def _auth_ok(self) -> bool:
        return (not self.require_key
                or self.headers.get("x-api-key") == API_KEY)

    def _send(self, code: int, payload=None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type",
                         "application/octet-stream" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            return self._send(401, {"message": "invalid api key"})
        if self.path == "/api/server/ping":
            return self._send(200, {"res": "pong"})
        if self.path == "/api/albums":
            albums = getattr(type(self), "albums", {})
            return self._send(200, [{"id": a["id"], "albumName": a["albumName"]}
                                    for a in albums.values()])
        if self.path.startswith("/api/albums/"):
            wanted = self.path.split("/")[3]
            for a in getattr(type(self), "albums", {}).values():
                if a["id"] == wanted:
                    return self._send(200, a)
            return self._send(404, {"message": "no album"})
        if self.path.startswith("/api/assets/") and self.path.endswith("/original"):
            asset_id = self.path.split("/")[3]
            asset = type(self).assets.get(asset_id)
            if not asset:
                return self._send(404, {"message": "not found"})
            return self._send(200, raw=asset["bytes"])
        self._send(404, {"message": "no route"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"message": "invalid api key"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/search/metadata":
            page = int(body.get("page", 1))
            size = int(body.get("size", 250))
            items = [a["dto"] for a in type(self).assets.values()
                     if a["id"] not in type(self).deleted]
            start = (page - 1) * size
            chunk = items[start:start + size]
            nxt = str(page + 1) if start + size < len(items) else None
            return self._send(200, {"assets": {"items": chunk, "nextPage": nxt,
                                               "total": len(items)}})
        if self.path.startswith("/api/libraries"):
            type(self).scans.append(self.path)
            return self._send(204)
        self._send(404, {"message": "no route"})

    def do_DELETE(self):
        if not self._auth_ok():
            return self._send(401, {"message": "invalid api key"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/assets":
            type(self).deleted.extend(body.get("ids", []))
            return self._send(204)
        self._send(404, {"message": "no route"})

    def log_message(self, *a):
        pass


class ImmichTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-immich-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        StubImmich.assets = {}
        StubImmich.albums = {}
        StubImmich.deleted = []
        StubImmich.scans = []
        StubImmich.require_key = True

        self.httpd = HTTPServer(("127.0.0.1", 0), StubImmich)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd"))],
            sources=[SourceSpec("immich", "", kind="immich",
                                url=self.url, api_key=API_KEY)])
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)

    def add_asset(self, n: int, *, library_id=None, trashed=False, kind="IMAGE"):
        aid = f"{n:08d}-0000-4000-8000-000000000000"
        data = jpeg_with_exif(f"2025:0{n % 9 + 1}:1{n % 9} 10:00:00",
                              f"immich-photo-{n}".encode() * 70)
        StubImmich.assets[aid] = {
            "id": aid, "bytes": data,
            "dto": {"id": aid, "originalFileName": f"IMG_{4000 + n}.jpg",
                    "originalPath": f"upload/library/admin/IMG_{4000 + n}.jpg",
                    "checksum": "abc==", "type": kind,
                    "fileCreatedAt": "2025-03-01T10:00:00.000Z",
                    "isTrashed": trashed, "libraryId": library_id},
        }
        return aid

    def client(self):
        return immich.ImmichClient(self.url, API_KEY)


class TestClient(ImmichTestCase):
    def test_ping(self):
        self.assertTrue(self.client().ping())

    def test_a_bad_key_is_reported_clearly(self):
        bad = immich.ImmichClient(self.url, "wrong")
        self.assertFalse(bad.ping())
        self.add_asset(1)
        with self.assertRaises(immich.ImmichError) as cm:
            list(bad.managed_assets())
        self.assertIn("API key", str(cm.exception))

    def test_unreachable_server_fails_soft(self):
        dead = immich.ImmichClient("http://127.0.0.1:1", API_KEY)
        self.assertFalse(dead.ping())

    def test_api_key_can_come_from_the_environment(self):
        import os
        os.environ["PV_TEST_KEY"] = API_KEY
        self.addCleanup(os.environ.pop, "PV_TEST_KEY", None)
        c = immich.ImmichClient(self.url, "env:PV_TEST_KEY")
        self.assertTrue(c.ping())

    def test_missing_env_var_is_an_error_not_an_empty_key(self):
        with self.assertRaises(immich.ImmichError):
            immich.ImmichClient(self.url, "env:DEFINITELY_NOT_SET_12345")

    def test_external_library_assets_are_never_listed(self):
        """Those files ARE PhotoVault's library. Pulling them would re-import
        the archive into itself; deleting them would ask Immich to remove a
        folder it only reads."""
        self.add_asset(1)
        self.add_asset(2, library_id="ext-lib-uuid")
        self.add_asset(3)
        ids = [a.id for a in self.client().managed_assets()]
        self.assertEqual(len(ids), 2)

    def test_trashed_assets_are_skipped(self):
        self.add_asset(1)
        self.add_asset(2, trashed=True)
        self.assertEqual(len(list(self.client().managed_assets())), 1)

    def test_pagination_is_followed(self):
        for n in range(1, 8):
            self.add_asset(n)
        self.assertEqual(len(list(self.client().managed_assets(page_size=3))), 7)

    def test_download_writes_the_original_bytes(self):
        aid = self.add_asset(1)
        dest = self.tmp / "out.jpg"
        asset = next(a for a in self.client().managed_assets() if a.id == aid)
        written = self.client().download(asset, dest)
        self.assertEqual(dest.read_bytes(), StubImmich.assets[aid]["bytes"])
        self.assertEqual(written, len(StubImmich.assets[aid]["bytes"]))

    def test_delete_is_soft_by_default(self):
        ids = [self.add_asset(n) for n in (1, 2)]
        self.client().delete(ids)
        self.assertEqual(sorted(StubImmich.deleted), sorted(ids))

    def test_delete_of_nothing_makes_no_request(self):
        self.assertEqual(self.client().delete([]), 0)
        self.assertEqual(StubImmich.deleted, [])


class TestDrain(ImmichTestCase):
    def run_import(self, reclaim=False):
        return importer.run_import(self.cfg, self.cat, self.cfg.sources[0],
                                   reclaim=reclaim, report_fn=lambda *a: None)

    def test_photos_flow_from_immich_into_the_archive(self):
        for n in range(1, 5):
            self.add_asset(n)
        rep = self.run_import()
        self.assertEqual(rep.imported, 4)
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.jpg"))), 4)
        self.assertEqual(len(list((self.tmp / "hdd").rglob("*.jpg"))), 4)

    def test_immich_releases_its_copies_once_verified(self):
        ids = [self.add_asset(n) for n in range(1, 4)]
        rep = self.run_import()
        self.assertEqual(rep.immich.released, 3)
        self.assertEqual(sorted(StubImmich.deleted), sorted(ids))
        self.assertTrue(rep.immich.rescanned, "an external rescan should follow")

    def test_nothing_is_released_when_redundancy_is_short(self):
        """If the second device is missing, the archive has one copy and Immich
        must keep its own - the duplicate is the safe failure."""
        self.cfg.replicas.append(
            ReplicaSpec("offsite", "local", "/Volumes/DefinitelyNotMounted/lib"))
        self.cfg.min_copies = 3
        for n in range(1, 3):
            self.add_asset(n)
        rep = self.run_import()
        self.assertEqual(rep.imported, 2)
        self.assertEqual(rep.immich.released, 0)
        self.assertEqual(rep.immich.held, 2)
        self.assertEqual(StubImmich.deleted, [])

    def test_a_second_run_does_not_re_download(self):
        for n in range(1, 4):
            self.add_asset(n)
        self.run_import()
        # Immich has released them, but the ids stay recorded either way.
        StubImmich.deleted = []
        again = self.run_import()
        self.assertEqual(again.pulled, 0)
        self.assertEqual(again.immich.already_known, 3)

    def test_asset_ids_are_recorded_not_temp_paths(self):
        """Staging is a temp directory; its paths are meaningless afterwards."""
        aid = self.add_asset(1)
        self.run_import()
        paths = [r["abs_path"] for r in self.cat.db.execute(
            "SELECT abs_path FROM source_file WHERE device = 'immich'").fetchall()]
        self.assertIn(f"immich:{aid}", paths)
        self.assertFalse(any("/tmp" in p or "photovault-immich" in p for p in paths))

    def test_an_unreachable_immich_imports_nothing_and_says_why(self):
        self.cfg.sources[0].url = "http://127.0.0.1:1"
        rep = self.run_import()
        self.assertEqual(rep.imported, 0)
        self.assertTrue(any("cannot reach" in e for e in rep.errors))

    def test_videos_are_pulled_too(self):
        self.add_asset(1, kind="VIDEO")
        self.add_asset(2, kind="IMAGE")
        assets = list(self.client().managed_assets())
        self.assertEqual({a.kind for a in assets}, {"VIDEO", "IMAGE"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSourceTestEndpoint(ImmichTestCase):
    """The Settings tab's Test button. Its job is to tell the user which thing
    is wrong, so the failure messages matter more than the success one."""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from photovault.web.server import AppState, VaultHandler

        handler = type("T", (VaultHandler,),
                       {"state": AppState(self.cfg, self.tmp / "config.toml")})
        self.httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd2.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd2.shutdown)
        self.port = self.httpd2.server_address[1]

    def probe(self, payload: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/sources/test",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def test_a_working_source_reports_what_is_waiting(self):
        for n in (1, 2, 3):
            self.add_asset(n)
        res = self.probe({"kind": "immich", "url": self.url, "api_key": API_KEY})
        self.assertTrue(res["ok"])
        self.assertIn("3 photos", res["detail"])

    def test_a_bad_key_says_so_rather_than_blaming_the_url(self):
        """ping() returns a bare False, so using it here made a wrong key and
        an unreachable host produce the same unhelpful message."""
        res = self.probe({"kind": "immich", "url": self.url, "api_key": "wrong"})
        self.assertFalse(res["ok"])
        self.assertIn("API key", res["detail"])

    def test_an_unreachable_host_says_that_instead(self):
        res = self.probe({"kind": "immich", "url": "http://127.0.0.1:1",
                          "api_key": API_KEY})
        self.assertFalse(res["ok"])
        self.assertIn("cannot reach", res["detail"])

    def test_a_missing_folder_is_reported(self):
        res = self.probe({"kind": "local", "path": "/definitely/not/here"})
        self.assertFalse(res["ok"])
        self.assertIn("does not exist", res["detail"])

    def test_a_real_folder_counts_its_photos(self):
        folder = self.tmp / "pics"
        folder.mkdir()
        for n in range(3):
            (folder / f"IMG_{n}.jpg").write_bytes(
                jpeg_with_exif("2024:01:01 10:00:00", b"x" * 200))
        res = self.probe({"kind": "local", "path": str(folder)})
        self.assertTrue(res["ok"])
        self.assertIn("3 photos", res["detail"])


class TestImmichBackup(ImmichTestCase):
    """Albums and Immich's database live only in Postgres, which a
    `colima delete` destroys. These are the artifacts that survive it."""

    def setUp(self):
        super().setUp()
        from photovault.config import ImmichSpec
        self.cfg.immich = ImmichSpec(
            compose_file=str(self.tmp / "docker-compose.yml"),
            url=self.url)

    def add_album(self, name: str, asset_ids: list[str], description: str = ""):
        StubImmich.albums = getattr(StubImmich, "albums", {})
        # Real Immich uses UUIDs; a name with a space would not survive a URL.
        album_id = f"a{abs(hash(name)) % 10**8:08d}-0000-4000-8000-000000000000"
        StubImmich.albums[name] = {
            "id": album_id, "albumName": name,
            "description": description, "createdAt": "2026-01-01T00:00:00.000Z",
            "assets": [{"id": a,
                        "originalFileName": f"IMG_{a[:4]}.jpg",
                        "originalPath": f"/mnt/photovault/2024/03/{a[:8]}.jpg",
                        "checksum": "abc=="} for a in asset_ids],
        }

    def test_manifest_records_library_paths_not_asset_ids(self):
        """Asset ids are meaningless once Immich is gone. A library path is the
        same string PhotoVault stores, so the manifest stays joinable forever."""
        from photovault import immich_backup

        ids = [self.add_asset(n) for n in (1, 2)]
        self.add_album("Italy 2019", ids, "a trip")
        manifest, errors = immich_backup.album_manifest(self.cfg)

        self.assertEqual(errors, [])
        self.assertEqual(len(manifest["albums"]), 1)
        album = manifest["albums"][0]
        self.assertEqual(album["name"], "Italy 2019")
        self.assertEqual(album["photo_count"], 2)
        for photo in album["photos"]:
            self.assertFalse(photo["library_path"].startswith("/mnt/"),
                             "the container prefix should be stripped")
            self.assertTrue(photo["library_path"].startswith("2024/"))

    def test_manifest_is_plain_readable_json(self):
        from photovault import immich_backup

        ids = [self.add_asset(n) for n in (1,)]
        self.add_album("Wedding", ids)
        manifest, _ = immich_backup.album_manifest(self.cfg)
        text = json.dumps(manifest, indent=2)
        self.assertIn("Wedding", text)
        self.assertIn("photovault_album_manifest", text)

    def test_no_immich_source_fails_soft(self):
        """A broken album export must never stop a photo backup."""
        from photovault import immich_backup

        self.cfg.sources = []
        manifest, errors = immich_backup.album_manifest(self.cfg)
        self.assertEqual(manifest, {})
        self.assertTrue(any("no immich source" in e for e in errors))

    def test_a_missing_compose_file_is_reported_not_raised(self):
        from photovault import immich_backup

        size, errors = immich_backup.dump_database(
            self.cfg, self.tmp / "dump.sql.gz")
        self.assertEqual(size, 0)
        self.assertTrue(errors)
        self.assertFalse((self.tmp / "dump.sql.gz").exists())

    def test_run_survives_everything_being_broken(self):
        from photovault import immich_backup

        self.cfg.sources = []
        art = immich_backup.run(self.cfg, self.tmp, "20260101-000000")
        self.assertFalse(art.any_produced)
        self.assertTrue(art.errors)

    def test_rederivable_tables_are_excluded_by_data_only(self):
        """Schema must stay so a restore is valid; only the rows are dropped,
        because Immich regenerates geodata and ML embeddings itself."""
        from photovault import immich_backup

        for table in ("geodata_places", "smart_search", "face_search"):
            self.assertIn(table, immich_backup.REDERIVABLE)


class TestBackupReplication(ImmichTestCase):
    """Immich's artifacts ride to every device with the catalog."""

    def test_artifacts_reach_every_connected_device(self):
        from photovault import backups
        from photovault.catalog import Catalog

        cat = Catalog(self.cfg.catalog_path)
        cat.close()
        res = backups.run(self.cfg)
        self.assertEqual(sorted(res.copied), ["hdd", "mac"])
        for replica in ("mac", "hdd"):
            root = self.tmp / replica / backups.BACKUP_DIR
            self.assertTrue(list(root.glob("catalog-*.db.gz")))
            self.assertTrue(list(root.glob("*.sha256")))

    def test_each_artifact_kind_rotates_separately(self):
        """One shared rotation would let a run of catalog snapshots evict the
        Immich dumps, which are written less often."""
        from photovault import backups

        root = self.tmp / "mac" / backups.BACKUP_DIR
        root.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            (root / f"catalog-2026010{i}-000000.db.gz").write_bytes(b"x")
            (root / f"immich-db-2026010{i}-000000.sql.gz").write_bytes(b"x")
            (root / f"immich-albums-2026010{i}-000000.json").write_bytes(b"x")

        spec = self.cfg.replica("mac")
        for label in ("catalog", "immich-db", "immich-albums"):
            backups.prune(spec, keep=2, label=label)
        self.assertEqual(len(list(root.glob("catalog-*.db.gz"))), 2)
        self.assertEqual(len(list(root.glob("immich-db-*.sql.gz"))), 2)
        self.assertEqual(len(list(root.glob("immich-albums-*.json"))), 2)


class TestUiImportPath(ImmichTestCase):
    """The UI's Import job and the CLI's import must be the same code.

    They were not: the job did Path(src.path) unconditionally, so an immich
    source produced "http:/localhost:2283 does not exist" — Path having
    collapsed the double slash — while the CLI imported fine.
    """

    def setUp(self):
        super().setUp()
        from photovault.web.jobs import JobRunner
        self.runner = JobRunner(self.cfg)

    def wait(self):
        import time
        for _ in range(200):
            jobs = self.runner.list()
            if jobs and jobs[0]["state"] != "running":
                return jobs[0]
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_the_ui_job_imports_from_an_immich_source(self):
        for n in range(1, 4):
            self.add_asset(n)
        self.runner.start("ingest")
        job = self.wait()
        self.assertEqual(job["state"], "done", job["errors"])
        self.assertEqual(job["result"]["imported"], 3)
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.jpg"))), 3)

    def test_the_ui_job_replicates_and_releases_like_the_cli(self):
        ids = [self.add_asset(n) for n in range(1, 4)]
        self.runner.start("ingest")
        job = self.wait()
        self.assertEqual(job["result"]["replicated"], 3)
        self.assertEqual(job["result"]["released"], 3)
        self.assertEqual(sorted(StubImmich.deleted), sorted(ids))

    def test_a_url_in_a_local_source_path_is_rejected_at_save(self):
        """The mistake that produced the original error: a URL typed into the
        path field of a folder source."""
        from photovault import config as configmod

        data = configmod.to_dict(self.cfg)
        data["source"] = [{"device": "photos", "kind": "local",
                           "path": "http://localhost:2283"}]
        with self.assertRaises(ValueError) as cm:
            configmod.save(data, self.tmp / "bad.toml")
        self.assertIn("URL in its path", str(cm.exception))

    def test_a_stray_path_on_an_immich_source_is_dropped(self):
        from photovault import config as configmod

        data = configmod.to_dict(self.cfg)
        data["source"] = [{"device": "photos", "kind": "immich",
                           "url": self.url, "api_key": API_KEY,
                           "path": "http://localhost:2283/"}]
        cfg = configmod.save(data, self.tmp / "ok.toml")
        self.assertEqual(cfg.sources[0].path, "")
