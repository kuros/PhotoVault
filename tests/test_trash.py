"""Tests for deleting photos.

Every other destructive operation removes a redundant copy and can be guarded
by proving enough copies remain. This one removes the photo itself, so the
guard is time instead: deletion is reversible until it is purged.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from photovault import duplicates, health, ingest, sync, trash
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec

from make_fixtures import build as build_fixtures


class TrashTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-trash-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        build_fixtures(self.src)

        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            trash_days=30, catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd"))],
            sources=[SourceSpec("old", str(self.src))])
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)
        ingest.ingest_source(self.cfg, self.cat, "old", self.src)
        sync.push(self.cfg, self.cat, "hdd")

    def hashes(self, n=2):
        return [a["hash"] for a in self.cat.all_assets()[:n]]

    def files_on(self, replica):
        return [p for p in (self.tmp / replica).rglob("*.jpg") if p.is_file()]

    def age_trash(self, days: int):
        """Backdate every trashed item, to test the retention window."""
        when = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        self.cat.db.execute(
            "UPDATE asset SET deleted_at = ? WHERE deleted_at IS NOT NULL", (when,))
        self.cat.db.commit()


class TestDelete(TrashTestCase):
    def test_deleting_removes_nothing_from_disk(self):
        """The whole point of the safety net: a misclick costs nothing."""
        before = len(self.files_on("mac")), len(self.files_on("hdd"))
        trash.delete(self.cat, self.hashes(2))
        self.assertEqual((len(self.files_on("mac")), len(self.files_on("hdd"))),
                         before)

    def test_deleted_photos_leave_the_library(self):
        trash.delete(self.cat, self.hashes(2))
        self.assertEqual(len(self.cat.all_assets()), 11)
        self.assertEqual(len(self.cat.trashed()), 2)

    def test_health_ignores_trashed_photos(self):
        """A photo in the trash is not 'underprotected' - it is on its way out."""
        h_before = health.assess(self.cfg, self.cat)
        trash.delete(self.cat, self.hashes(3))
        h_after = health.assess(self.cfg, self.cat)
        self.assertEqual(h_after.total_assets, h_before.total_assets - 3)
        self.assertTrue(h_after.ok)

    def test_trashed_photos_are_not_replicated_to_a_new_device(self):
        trash.delete(self.cat, self.hashes(4))
        self.cfg.replicas.append(ReplicaSpec("hdd2", "local", str(self.tmp / "hdd2")))
        self.cat.upsert_replica("hdd2", "local", str(self.tmp / "hdd2"))
        st = sync.push(self.cfg, self.cat, "hdd2")
        self.assertEqual(st.copied, 9)

    def test_trashed_photos_are_excluded_from_duplicate_scanning(self):
        trash.delete(self.cat, self.hashes(13))
        self.assertEqual(len(self.cat.assets_with_phash()), 0)
        self.assertEqual(duplicates.find_groups(self.cfg, self.cat), [])

    def test_reconcile_still_tracks_trashed_files(self):
        """They are still on disk, so their placements must stay accurate -
        otherwise purge would not know where to look."""
        doomed = self.hashes(2)
        trash.delete(self.cat, doomed)
        sync.reconcile(self.cfg, self.cat, "hdd")
        rows = self.cat.db.execute(
            "SELECT state FROM placement WHERE hash = ? AND replica = 'hdd'",
            (doomed[0],)).fetchall()
        self.assertEqual([r["state"] for r in rows], ["present"])

    def test_deleting_twice_is_harmless(self):
        doomed = self.hashes(2)
        trash.delete(self.cat, doomed)
        again = trash.delete(self.cat, doomed)
        self.assertEqual(again.moved, 0)
        self.assertEqual(len(self.cat.trashed()), 2)

    def test_unknown_hashes_are_reported_not_ignored(self):
        st = trash.delete(self.cat, ["0" * 64])
        self.assertEqual(st.moved, 0)
        self.assertEqual(st.missing, ["0" * 64])


class TestRestore(TrashTestCase):
    def test_restoring_brings_a_photo_back_intact(self):
        doomed = self.hashes(2)
        trash.delete(self.cat, doomed)
        self.assertEqual(trash.restore(self.cat, doomed), 2)
        self.assertEqual(len(self.cat.all_assets()), 13)
        self.assertEqual(len(self.cat.trashed()), 0)
        self.assertTrue(health.assess(self.cfg, self.cat).ok)

    def test_restore_needs_no_file_recovery(self):
        """Because nothing was ever removed, restoring is a flag flip and the
        photo is immediately back on every device."""
        doomed = self.hashes(1)
        rel = self.cat.asset(doomed[0])["rel_path"]
        trash.delete(self.cat, doomed)
        trash.restore(self.cat, doomed)
        for replica in ("mac", "hdd"):
            self.assertTrue((self.tmp / replica / rel).is_file())


class TestPurge(TrashTestCase):
    def test_purge_is_a_preview_by_default(self):
        trash.delete(self.cat, self.hashes(2))
        rep = trash.purge(self.cfg, self.cat, expired_only=False)
        self.assertEqual(rep.purged, 2)
        self.assertEqual(len(self.files_on("mac")), 13)
        self.assertEqual(len(self.cat.trashed()), 2)

    def test_purge_removes_from_every_replica(self):
        doomed = self.hashes(2)
        rels = [self.cat.asset(h)["rel_path"] for h in doomed]
        trash.delete(self.cat, doomed)
        rep = trash.purge(self.cfg, self.cat, expired_only=False, dry_run=False)
        self.assertEqual(rep.purged, 2)
        for replica in ("mac", "hdd"):
            for rel in rels:
                self.assertFalse((self.tmp / replica / rel).exists())
        self.assertEqual(len(self.cat.trashed()), 0)
        orphans = self.cat.db.execute(
            "SELECT COUNT(*) n FROM placement p LEFT JOIN asset a ON a.hash = p.hash "
            "WHERE a.hash IS NULL").fetchone()["n"]
        self.assertEqual(orphans, 0)

    def test_purge_refuses_while_a_device_is_disconnected(self):
        """Purging with a drive in a drawer leaves the file on that drive while
        the catalog forgets it - an orphan that reappears on the next rebuild."""
        trash.delete(self.cat, self.hashes(2))
        self.cfg.replicas.append(
            ReplicaSpec("offsite", "local", "/Volumes/DefinitelyNotMounted/lib",
                        offline=True))
        rep = trash.purge(self.cfg, self.cat, expired_only=False, dry_run=False)
        self.assertEqual(rep.purged, 0)
        self.assertTrue(rep.skipped)
        self.assertIn("offsite", rep.skipped[0][1])
        self.assertEqual(len(self.files_on("mac")), 13)

    def test_only_expired_items_are_purged_by_default(self):
        trash.delete(self.cat, self.hashes(4))
        rep = trash.purge(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.purged, 0, "nothing is expired yet")

        self.age_trash(days=45)
        rep = trash.purge(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.purged, 4)

    def test_purging_named_photos_leaves_the_rest(self):
        doomed = self.hashes(4)
        trash.delete(self.cat, doomed)
        rep = trash.purge(self.cfg, self.cat, hashes=doomed[:2], dry_run=False)
        self.assertEqual(rep.purged, 2)
        self.assertEqual(len(self.cat.trashed()), 2)

    def test_a_live_photo_is_never_purged(self):
        """purge() works from the trash, so an un-deleted photo cannot be
        removed by naming it."""
        live = self.hashes(2)
        rep = trash.purge(self.cfg, self.cat, hashes=live, dry_run=False)
        self.assertEqual(rep.purged, 0)
        self.assertEqual(len(self.cat.all_assets()), 13)

    def test_summary_reports_what_is_expiring(self):
        trash.delete(self.cat, self.hashes(3))
        s = trash.summary(self.cfg, self.cat)
        self.assertEqual(s["files"], 3)
        self.assertEqual(s["expiring"], 0)
        self.age_trash(days=40)
        self.assertEqual(trash.summary(self.cfg, self.cat)["expiring"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
