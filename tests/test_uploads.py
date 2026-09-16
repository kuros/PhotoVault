"""Tests for browser uploads.

The filename is the dangerous input here: an HTTP client can claim any path it
likes. Most of these tests are about what must NOT end up on disk.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path

from photovault import uploads
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec

from make_fixtures import jpeg_with_exif


class TestPathSafety(unittest.TestCase):
    def test_ordinary_relative_paths_survive(self):
        self.assertEqual(str(uploads.safe_relative_path("holiday/2019/IMG_1.jpg")),
                         "holiday/2019/IMG_1.jpg")
        self.assertEqual(str(uploads.safe_relative_path("IMG_1.jpg")), "IMG_1.jpg")

    def test_traversal_is_refused(self):
        for attack in ("../../../../etc/passwd",
                       "ok/../../bad.jpg",
                       "..",
                       "a/b/../../../c.jpg"):
            with self.subTest(attack=attack):
                self.assertIsNone(uploads.safe_relative_path(attack))

    def test_absolute_and_unc_paths_are_refused(self):
        """A browser never sends these, so they are only ever a probe."""
        for attack in ("/etc/cron.d/evil.jpg", "\\\\server\\share\\x.jpg",
                       "C:\\Windows\\System32\\evil.jpg", "/tmp/x.jpg"):
            with self.subTest(attack=attack):
                self.assertIsNone(uploads.safe_relative_path(attack))

    def test_null_bytes_are_refused(self):
        self.assertIsNone(uploads.safe_relative_path("a\x00b.jpg"))

    def test_dot_segments_become_harmless_names(self):
        """`....//` is the classic way to defeat naive `..` stripping. Because
        components are rebuilt rather than cleaned, it cannot survive."""
        out = uploads.safe_relative_path("....//....//evil.jpg")
        self.assertIsNotNone(out)
        self.assertNotIn("..", out.parts)

    def test_windows_reserved_names_are_escaped(self):
        self.assertEqual(uploads.safe_component("CON.jpg"), "_CON.jpg")
        self.assertEqual(uploads.safe_component("lpt1.png"), "_lpt1.png")

    def test_long_components_are_truncated(self):
        out = uploads.safe_component("x" * 500 + ".jpg")
        self.assertLessEqual(len(out), uploads.MAX_COMPONENT)
        self.assertTrue(out.endswith(".jpg"))

    def test_depth_is_bounded(self):
        deep = "/".join(f"d{i}" for i in range(40)) + "/x.jpg"
        out = uploads.safe_relative_path(deep)
        self.assertLessEqual(len(out.parts), uploads.MAX_DEPTH)

    def test_unicode_is_transliterated_not_rejected(self):
        out = uploads.safe_relative_path("Ünïcødé/Phötö.JPG")
        self.assertIsNotNone(out)
        self.assertTrue(str(out).endswith(".JPG"))


class UploadTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-upl-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac" / "library")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd" / "library"))],
            sources=[])
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)

    def send(self, rel_path: str, payload: bytes | None = None):
        payload = payload if payload is not None else jpeg_with_exif(
            "2024:05:01 09:00:00", b"upload-payload" * 40)
        return uploads.accept(self.cfg, rel_path, io.BytesIO(payload), len(payload))


class TestAccept(UploadTestCase):
    def test_staging_is_outside_the_library_root(self):
        """Anything under a replica root is enumerated by list_present() and
        would be counted as stored content before it was ever imported."""
        staging = uploads.staging_dir(self.cfg)
        library = self.cfg.primary_root
        self.assertNotIn(library.resolve(), [staging.resolve(), *staging.resolve().parents])

    def test_a_photo_is_stored(self):
        res = self.send("holiday/IMG_1.jpg")
        self.assertTrue(res.stored)
        self.assertTrue((uploads.staging_dir(self.cfg) / "holiday/IMG_1.jpg").is_file())

    def test_non_media_is_refused(self):
        res = self.send("notes.txt", b"hello")
        self.assertFalse(res.stored)
        self.assertIn("not a photo", res.reason)

    def test_unsafe_paths_write_nothing(self):
        res = self.send("../../escape.jpg")
        self.assertFalse(res.stored)
        self.assertEqual(list(uploads.staging_dir(self.cfg).rglob("*"))
                         if uploads.staging_dir(self.cfg).exists() else [], [])

    def test_a_truncated_upload_leaves_no_partial_file(self):
        """A half-received photo would hash as a different, corrupt asset and
        then be replicated everywhere as though it were real."""
        payload = jpeg_with_exif("2024:05:01 09:00:00", b"x" * 500)
        res = uploads.accept(self.cfg, "IMG_cut.jpg",
                             io.BytesIO(payload[:100]), len(payload))
        self.assertFalse(res.stored)
        self.assertIn("truncated", res.reason)
        staging = uploads.staging_dir(self.cfg)
        leftovers = list(staging.rglob("*")) if staging.exists() else []
        self.assertEqual([p for p in leftovers if p.is_file()], [])

    def test_staged_summary_counts_what_is_waiting(self):
        for i in range(3):
            self.send(f"batch/IMG_{i}.jpg",
                      jpeg_with_exif("2024:05:01 09:00:00", f"p{i}".encode() * 80))
        summary = uploads.staged(self.cfg)
        self.assertEqual(summary.files, 3)
        self.assertGreater(summary.bytes, 0)

    def test_discard_empties_staging(self):
        self.send("a/IMG_1.jpg")
        self.assertEqual(uploads.discard(self.cfg), 1)
        self.assertEqual(uploads.staged(self.cfg).files, 0)


class TestStagedIngest(UploadTestCase):
    def stage(self, n: int = 4):
        for i in range(n):
            self.send(f"old-drive/IMG_{i}.jpg",
                      jpeg_with_exif(f"2021:0{i + 1}:0{i + 1} 12:00:00",
                                     f"scan-{i}".encode() * 90))

    def test_uploads_go_through_the_normal_import_path(self):
        self.stage()
        st = uploads.ingest_staged(self.cfg, self.cat)
        self.assertEqual(st.imported, 4)
        stored = list(self.cfg.primary_root.rglob("*.jpg"))
        self.assertEqual(len(stored), 4)
        # Dates came from EXIF, so the layout is by capture date as usual.
        self.assertTrue(any("2021/" in str(p) for p in stored))

    def test_re_uploading_the_same_photos_imports_nothing(self):
        self.stage()
        uploads.ingest_staged(self.cfg, self.cat)
        self.stage()
        st = uploads.ingest_staged(self.cfg, self.cat)
        self.assertEqual(st.imported, 0)
        self.assertEqual(st.duplicates, 4)

    def test_staging_is_cleared_only_after_replication(self):
        from photovault import sync

        self.stage()
        uploads.ingest_staged(self.cfg, self.cat)

        # Only the primary holds them so far: min_copies is 2.
        cleared, notes = uploads.clear_imported(self.cfg, self.cat)
        self.assertEqual(cleared, 0)
        self.assertEqual(uploads.staged(self.cfg).files, 4)
        self.assertTrue(notes)

        sync.push(self.cfg, self.cat, "hdd")
        cleared, _ = uploads.clear_imported(self.cfg, self.cat)
        self.assertEqual(cleared, 4)
        self.assertEqual(uploads.staged(self.cfg).files, 0)

    def test_empty_directories_are_tidied_away(self):
        from photovault import sync

        self.stage()
        uploads.ingest_staged(self.cfg, self.cat)
        sync.push(self.cfg, self.cat, "hdd")
        uploads.clear_imported(self.cfg, self.cat)
        leftover = [p for p in uploads.staging_dir(self.cfg).rglob("*")]
        self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
