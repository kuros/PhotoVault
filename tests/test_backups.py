"""Tests for catalog backups.

The catalog was once purely derived data. It no longer is: duplicate decisions
and trash state exist nowhere else, and `rebuild` cannot recreate judgement.
These tests are mostly about that distinction, and about the fact that a plain
copy of a WAL-mode SQLite file is not a backup.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from photovault import backups, ingest, sync
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec

from make_fixtures import build as build_fixtures


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-bkp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        build_fixtures(self.src)

        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            backup_keep=3, catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd"))],
            sources=[SourceSpec("old", str(self.src))])
        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)
        ingest.ingest_source(self.cfg, self.cat, "old", self.src)
        sync.push(self.cfg, self.cat, "hdd")

    def seed_judgement(self):
        """The parts of the catalog no rebuild can reconstruct."""
        hashes = [a["hash"] for a in self.cat.all_assets()]
        self.cat.set_decision(hashes[0], "delete")
        self.cat.set_decision(hashes[1], "keep")
        self.cat.soft_delete(hashes[2])
        self.cat.db.commit()

    def counts(self, path: Path) -> tuple[int, int, int]:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return (db.execute("SELECT COUNT(*) FROM asset").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM dup_decision").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM asset "
                               "WHERE deleted_at IS NOT NULL").fetchone()[0])
        finally:
            db.close()


class TestSnapshot(BackupTestCase):
    def test_a_plain_copy_of_a_wal_database_is_not_a_backup(self):
        """The reason snapshot() exists. In WAL mode the committed state lives
        partly in the -wal file, so copying only the main file can lose whole
        tables - not merely recent rows."""
        self.seed_judgement()
        naive = self.tmp / "naive.db"
        shutil.copy2(self.cfg.catalog_path, naive)

        proper = self.tmp / "proper.db.gz"
        backups.snapshot(self.cfg.catalog_path, proper)
        with gzip.open(proper, "rb") as gz:
            (self.tmp / "proper.db").write_bytes(gz.read())

        live = self.counts(self.cfg.catalog_path)
        self.assertEqual(self.counts(self.tmp / "proper.db"), live)
        try:
            naive_counts = self.counts(naive)
        except sqlite3.DatabaseError:
            naive_counts = None
        self.assertNotEqual(naive_counts, live,
                            "a plain copy should not match a live WAL database")

    def test_snapshot_decompresses_to_a_working_database(self):
        self.seed_judgement()
        dest = self.tmp / "snap.db.gz"
        size, digest = backups.snapshot(self.cfg.catalog_path, dest)
        self.assertEqual(size, dest.stat().st_size)
        self.assertEqual(len(digest), 64)

        out = self.tmp / "unpacked.db"
        with gzip.open(dest, "rb") as gz:
            out.write_bytes(gz.read())
        self.assertEqual(self.counts(out), self.counts(self.cfg.catalog_path))

    def test_snapshot_compresses_a_realistic_catalog(self):
        """Gzip overhead exceeds the saving on a near-empty database, so size
        is only meaningful once there is real content.

        Note what this must NOT compare against: catalog.db's own size. In WAL
        mode those 20,000 rows sit in the -wal file and the main database is
        still 4 KB - the very confusion snapshot() exists to avoid. The honest
        comparison is compressed versus uncompressed snapshot.
        """
        self.cat.db.executemany(
            "INSERT INTO event(at, kind, detail) VALUES ('2026-01-01', 'x', ?)",
            [(f"a repetitive log line number {i}",) for i in range(20000)])
        self.cat.db.commit()

        dest = self.tmp / "big.db.gz"
        compressed, _ = backups.snapshot(self.cfg.catalog_path, dest)
        out = self.tmp / "big.db"
        with gzip.open(dest, "rb") as gz:
            out.write_bytes(gz.read())
        self.assertLess(compressed, out.stat().st_size * 0.6,
                        "a catalog with real content should compress well")


class TestRun(BackupTestCase):
    def test_backup_reaches_every_reachable_replica(self):
        res = backups.run(self.cfg)
        self.assertEqual(sorted(res.copied), ["hdd", "mac"])
        self.assertEqual(res.errors, [])
        for replica in ("mac", "hdd"):
            root = self.tmp / replica / backups.BACKUP_DIR
            self.assertEqual(len(list(root.glob("*.db.gz"))), 1)
            self.assertEqual(len(list(root.glob("*.sha256"))), 1)

    def test_an_absent_device_is_reported_not_fatal(self):
        self.cfg.replicas.append(
            ReplicaSpec("offsite", "local", "/Volumes/DefinitelyNotMounted/lib",
                        offline=True))
        res = backups.run(self.cfg)
        self.assertIn("offsite", res.unreachable)
        self.assertIn("mac", res.copied)

    def test_backups_live_outside_the_photo_catalogue(self):
        """Backups rotate; the library never forgets. Mixing them would either
        break that promise or grow without bound."""
        backups.run(self.cfg)
        before = len(self.cat.all_assets())
        st = ingest.ingest_source(self.cfg, self.cat, "old", self.src)
        self.assertEqual(st.imported, 0)
        self.assertEqual(len(self.cat.all_assets()), before)
        # And a rebuild must not pick them up as photos either.
        recovered = sync.rebuild_from(self.cfg, self.cat, "hdd")
        self.assertEqual(recovered, 13)

    def test_old_snapshots_are_rotated_away(self):
        for _ in range(5):
            backups.run(self.cfg, keep=3)
            time.sleep(1.05)   # snapshot names are second-resolution
        for replica in ("mac", "hdd"):
            snaps = list((self.tmp / replica / backups.BACKUP_DIR).glob("*.db.gz"))
            self.assertEqual(len(snaps), 3)

    def test_available_lists_newest_first(self):
        backups.run(self.cfg)
        time.sleep(1.05)
        backups.run(self.cfg)
        snaps = backups.available(self.cfg)
        self.assertGreaterEqual(len(snaps), 2)
        self.assertGreaterEqual(snaps[0].when, snaps[-1].when)

    def test_age_days_reports_freshness(self):
        self.assertIsNone(backups.age_days(self.cfg))
        backups.run(self.cfg)
        self.assertLess(backups.age_days(self.cfg), 1)


class TestRestore(BackupTestCase):
    def test_restore_brings_back_what_rebuild_cannot(self):
        self.seed_judgement()
        expected = self.counts(self.cfg.catalog_path)
        backups.run(self.cfg)

        self.cat.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.cfg.catalog_path) + suffix).unlink(missing_ok=True)

        snap = backups.available(self.cfg)[0]
        backups.restore(self.cfg, snap, dry_run=False)
        self.assertEqual(self.counts(self.cfg.catalog_path), expected)

    def test_rebuild_alone_loses_the_judgement(self):
        """The contrast that justifies this whole module."""
        self.seed_judgement()
        self.cat.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.cfg.catalog_path) + suffix).unlink(missing_ok=True)

        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)
        sync.rebuild_from(self.cfg, self.cat, "hdd")

        assets, decisions, trashed = self.counts(self.cfg.catalog_path)
        self.assertEqual(assets, 13, "photos come back")
        self.assertEqual(decisions, 0, "decisions do not")
        self.assertEqual(trashed, 0, "nor does trash state")

    def test_the_previous_catalog_is_kept_aside(self):
        backups.run(self.cfg)
        snap = backups.available(self.cfg)[0]
        self.cat.close()
        aside = backups.restore(self.cfg, snap, dry_run=False)
        self.assertIsNotNone(aside)
        self.assertTrue(Path(aside).exists(),
                        "restoring the wrong snapshot should cost a rename")

    def test_a_corrupted_snapshot_is_refused_not_crashed_on(self):
        """zlib raises its own error type, not OSError - catching only OSError
        turned 'this backup is damaged' into a traceback."""
        backups.run(self.cfg)
        snap = backups.available(self.cfg)[0]
        data = bytearray(snap.path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        snap.path.write_bytes(bytes(data))

        self.assertFalse(backups.verify(snap))
        with self.assertRaises(ValueError):
            backups.restore(self.cfg, snap, dry_run=False)

    def test_a_truncated_snapshot_is_refused(self):
        backups.run(self.cfg)
        snap = backups.available(self.cfg)[0]
        snap.path.write_bytes(snap.path.read_bytes()[:50])
        self.assertFalse(backups.verify(snap))

    def test_a_missing_checksum_is_refused(self):
        backups.run(self.cfg)
        snap = backups.available(self.cfg)[0]
        snap.path.with_suffix(snap.path.suffix + ".sha256").unlink()
        self.assertFalse(backups.verify(snap),
                         "an unverifiable backup is not a usable backup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
