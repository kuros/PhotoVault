"""End-to-end tests for the whole ingest -> replicate -> verify -> recover loop.

Each test creates a throwaway library in a temp directory, so nothing here can
touch a real photo. These are the behaviours that must never regress: a backup
tool that quietly stops backing up is worse than no backup tool at all.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from photovault import health, ingest, sync, verify
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec

from make_fixtures import build as build_fixtures

CONFIG_TEMPLATE = None


class VaultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "source"
        build_fixtures(self.source)

        self.cfg = Config(
            primary="mac",
            min_copies=3,
            require_offline_copy=True,
            scrub_days=30,
            catalog_path=self.tmp / "catalog.db",
            replicas=[
                ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                ReplicaSpec("hdd", "local", str(self.tmp / "hdd"), offline=True),
                ReplicaSpec("win", "local", str(self.tmp / "win")),
            ],
            sources=[SourceSpec("mac", str(self.source))],
        )
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)

    def ingest_all(self) -> ingest.IngestStats:
        return ingest.ingest_source(self.cfg, self.cat, "mac", self.source)

    def sync_all(self) -> None:
        for name in ("hdd", "win"):
            sync.push(self.cfg, self.cat, name)

    def replica_path(self, replica: str, rel: str) -> Path:
        return self.tmp / replica / rel

    def any_rel_path(self) -> str:
        return self.cat.all_assets()[0]["rel_path"]


class TestIngest(VaultTestCase):
    def test_imports_media_and_ignores_junk(self):
        st = self.ingest_all()
        self.assertEqual(st.imported, 13)
        self.assertEqual(st.failed, 0)
        # notes.txt is skipped; .DS_Store and Thumbnails/ never even enumerated.
        stored = list((self.tmp / "mac").rglob("*.jpg"))
        self.assertEqual(len(stored), 13)
        self.assertFalse((self.tmp / "mac" / "notes.txt").exists())

    def test_identical_content_is_stored_once(self):
        st = self.ingest_all()
        self.assertEqual(st.duplicates, 1, "the copied file should collapse")

    def test_ingest_is_idempotent(self):
        self.ingest_all()
        second = self.ingest_all()
        self.assertEqual(second.imported, 0)
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.jpg"))), 13)

    def test_sources_are_never_modified(self):
        before = {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.ingest_all()
        after = {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "ingest must not touch source files")

    def test_layout_uses_capture_date(self):
        self.ingest_all()
        paths = {a["rel_path"] for a in self.cat.all_assets()}
        self.assertTrue(any(p.startswith("2024/01/") for p in paths))
        # The EXIF-less file is dated from its filename, not today.
        self.assertTrue(any(p.startswith("2011/07/") for p in paths))

    def test_dry_run_writes_nothing(self):
        ingest.ingest_source(self.cfg, self.cat, "mac", self.source, dry_run=True)
        self.assertEqual(len(self.cat.all_assets()), 0)
        self.assertFalse((self.tmp / "mac").exists())


class TestReplication(VaultTestCase):
    def test_sync_reaches_full_redundancy(self):
        self.ingest_all()
        self.sync_all()
        h = health.assess(self.cfg, self.cat)
        self.assertEqual(h.underprotected, 0)
        self.assertEqual(h.no_offline_copy, 0)
        self.assertTrue(h.ok)

    def test_sync_is_idempotent(self):
        self.ingest_all()
        self.sync_all()
        again = sync.push(self.cfg, self.cat, "hdd")
        self.assertEqual(again.copied, 0)

    def test_replicas_are_byte_identical(self):
        self.ingest_all()
        self.sync_all()
        for asset in self.cat.all_assets():
            for replica in ("mac", "hdd", "win"):
                data = self.replica_path(replica, asset["rel_path"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), asset["hash"])

    def test_health_flags_missing_offline_copy(self):
        self.ingest_all()
        sync.push(self.cfg, self.cat, "win")  # online copies only
        h = health.assess(self.cfg, self.cat)
        self.assertEqual(h.no_offline_copy, 13)
        self.assertFalse(h.ok)


class TestIntegrity(VaultTestCase):
    def test_force_rechecks_recently_verified_copies(self):
        """Without --force, a copy verified at write time is skipped."""
        self.ingest_all()
        self.sync_all()
        self.assertEqual(len(verify.due_for_scrub(self.cat, self.cfg)), 26)
        self.assertEqual(len(verify.due_for_scrub(self.cat, self.cfg, force=True)), 39)

    def test_corruption_on_the_primary_is_caught(self):
        self.ingest_all()
        self.sync_all()
        rel = self.any_rel_path()
        victim = self.replica_path("mac", rel)
        good = victim.read_bytes()
        victim.write_bytes(good[:20] + b"\x00" + good[21:])

        st = verify.scrub(self.cfg, self.cat, force=True)
        self.assertEqual(st.corrupt, 1)
        self.assertEqual(st.repaired, 1)
        self.assertEqual(victim.read_bytes(), good)

    def test_scrub_detects_and_repairs_corruption(self):
        self.ingest_all()
        self.sync_all()
        rel = self.any_rel_path()
        victim = self.replica_path("hdd", rel)
        good = victim.read_bytes()

        data = bytearray(good)
        data[30] ^= 0xFF  # flip a bit, exactly like real disk rot
        victim.write_bytes(bytes(data))

        st = verify.scrub(self.cfg, self.cat)
        self.assertEqual(st.corrupt, 1)
        self.assertEqual(st.repaired, 1)
        self.assertEqual(st.unrepairable, 0)
        self.assertEqual(victim.read_bytes(), good, "bytes should be restored")

    def test_scrub_restores_a_deleted_file(self):
        self.ingest_all()
        self.sync_all()
        rel = self.any_rel_path()
        self.replica_path("win", rel).unlink()

        st = verify.scrub(self.cfg, self.cat)
        self.assertEqual(st.vanished, 1)
        self.assertEqual(st.repaired, 1)
        self.assertTrue(self.replica_path("win", rel).exists())

    def test_last_good_copy_is_never_overwritten(self):
        """If every other copy is already bad, repair must refuse rather than
        propagate damage - the failure has to stay loud and visible."""
        self.ingest_all()
        self.sync_all()
        rel = self.any_rel_path()
        for replica in ("mac", "hdd", "win"):
            p = self.replica_path(replica, rel)
            p.write_bytes(p.read_bytes() + b"damaged")

        # force=True because the copy written during ingest is marked verified
        # and would otherwise not be due for another scrub_days.
        st = verify.scrub(self.cfg, self.cat, force=True)
        self.assertEqual(st.repaired, 0)
        self.assertEqual(st.unrepairable, 3)
        h = health.assess(self.cfg, self.cat)
        self.assertGreater(h.corrupt, 0)
        self.assertFalse(h.ok)

    def test_clean_library_scrubs_clean(self):
        self.ingest_all()
        self.sync_all()
        st = verify.scrub(self.cfg, self.cat, force=True)
        self.assertEqual(st.corrupt, 0)
        self.assertEqual(st.vanished, 0)
        self.assertEqual(st.ok, st.checked)


class TestDisasterRecovery(VaultTestCase):
    def test_catalog_can_be_rebuilt_from_a_single_replica(self):
        self.ingest_all()
        self.sync_all()
        expected = {a["hash"] for a in self.cat.all_assets()}

        # Total catalog loss.
        self.cat.close()
        self.cfg.catalog_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.cfg.catalog_path) + suffix).unlink(missing_ok=True)

        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)
        self.assertEqual(len(self.cat.all_assets()), 0)

        found = sync.rebuild_from(self.cfg, self.cat, "hdd")
        self.assertEqual(found, 13)
        for name in ("mac", "win"):
            sync.reconcile(self.cfg, self.cat, name)

        self.assertEqual({a["hash"] for a in self.cat.all_assets()}, expected)
        self.assertTrue(health.assess(self.cfg, self.cat).ok)

    def test_survives_losing_the_primary_entirely(self):
        self.ingest_all()
        self.sync_all()
        expected = {a["hash"] for a in self.cat.all_assets()}

        shutil.rmtree(self.tmp / "mac")  # the Mac's drive dies
        sync.reconcile(self.cfg, self.cat, "mac")
        h = health.assess(self.cfg, self.cat)
        self.assertEqual(h.per_replica["mac"]["present"], 0)
        self.assertEqual(h.underprotected, 13)   # down to 2 copies, correctly flagged

        sync.push(self.cfg, self.cat, "mac")     # rebuild from a surviving replica
        h = health.assess(self.cfg, self.cat)
        self.assertTrue(h.ok)
        for asset in self.cat.all_assets():
            data = self.replica_path("mac", asset["rel_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), asset["hash"])
        self.assertEqual({a["hash"] for a in self.cat.all_assets()}, expected)


class TestRecoveryKit(VaultTestCase):
    """A backup that cannot explain how to restore itself is half a backup."""

    def test_sync_leaves_instructions_on_the_replica(self):
        self.ingest_all()
        self.sync_all()
        doc = self.tmp / "hdd" / "RECOVERY.md"   # inside the root, never beside it
        self.assertTrue(doc.exists())
        text = doc.read_text()
        self.assertIn("photovault rebuild hdd", text)
        self.assertIn("reconcile --all", text)
        self.assertIn("13", text)          # the file count is recorded

    def test_kit_never_writes_outside_the_replica_root(self):
        """A root of /Volumes/Backup must not cause writes into /Volumes."""
        self.ingest_all()
        self.sync_all()
        before = set(self.tmp.iterdir())
        sync.write_recovery_kit(self.cfg, self.cat, "hdd")
        self.assertEqual(set(self.tmp.iterdir()), before,
                         "nothing may appear outside the configured root")
        self.assertTrue((self.tmp / "hdd" / "RECOVERY.md").exists())

    def test_recovery_kit_is_not_catalogued_as_photos(self):
        """The kit sits next to the library and must never be mistaken for media."""
        self.ingest_all()
        self.sync_all()
        (self.tmp / "hdd" / "stray-note.txt").write_text("not a photo")

        self.cat.close()
        self.cfg.catalog_path.unlink()
        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)
        self.assertEqual(sync.rebuild_from(self.cfg, self.cat, "hdd"), 13)

    def test_total_loss_of_the_mac_loses_nothing(self):
        """The full drill: library, catalog and config all gone at once."""
        self.ingest_all()
        self.sync_all()
        expected = {a["hash"]: a["rel_path"] for a in self.cat.all_assets()}
        originals = {rel: (self.tmp / "hdd" / rel).read_bytes()
                     for rel in expected.values()}

        # The Mac dies: primary library and catalog are destroyed together.
        self.cat.close()
        shutil.rmtree(self.tmp / "mac")
        self.cfg.catalog_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.cfg.catalog_path) + suffix).unlink(missing_ok=True)

        # Recovery on a replacement machine, starting from the drive alone.
        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)
        sync.rebuild_from(self.cfg, self.cat, "hdd")
        for name in ("mac", "win"):
            sync.reconcile(self.cfg, self.cat, name)
        sync.push(self.cfg, self.cat, "mac")

        self.assertEqual({a["hash"]: a["rel_path"]
                          for a in self.cat.all_assets()}, expected)
        for rel, data in originals.items():
            self.assertEqual((self.tmp / "mac" / rel).read_bytes(), data,
                             f"{rel} did not come back byte-identical")
        st = verify.scrub(self.cfg, self.cat, force=True)
        self.assertEqual(st.corrupt, 0)
        self.assertEqual(st.unrepairable, 0)
        self.assertTrue(health.assess(self.cfg, self.cat).ok)


class TestSafetyGuards(VaultTestCase):
    def test_unmounted_volume_is_refused(self):
        """The dangerous failure: an unplugged drive silently becoming a folder
        on the boot disk, so backups 'succeed' into the wrong place."""
        from photovault.replicas import LocalDriver, ReplicaError

        spec = ReplicaSpec("ghost", "local", "/Volumes/DefinitelyNotMounted/lib",
                           offline=True)
        drv = LocalDriver(spec)
        self.assertFalse(drv.available())
        with self.assertRaises(ReplicaError):
            drv.ensure_root()
        self.assertFalse(Path("/Volumes/DefinitelyNotMounted").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
