"""Tests for capture-date extraction and repairing photos filed under a guess.

The bug these exist for: in a HEIC container the string "Exif\\0\\0" appears in
the `infe` box that *names* the metadata item, roughly 1 KB in, while the TIFF
payload it refers to sits much further along — 19 KB later in a real iPhone
file. Reading from the name yields container structure, the TIFF parse fails
silently, and the date falls back to the file's mtime. For a downloaded photo
that is the moment it was downloaded, so every one landed under "today".
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from photovault import ingest, mediatime, redate, sync
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec


def tiff_block(date: str, big_endian: bool = True) -> bytes:
    """A minimal TIFF with an ExifIFD carrying DateTimeOriginal."""
    end = ">" if big_endian else "<"
    magic = b"MM\x00*" if big_endian else b"II*\x00"
    header = magic + struct.pack(end + "I", 8)
    ifd0_off = 8
    ifd0_len = 2 + 12 + 4
    exif_off = ifd0_off + ifd0_len
    value_off = exif_off + 2 + 12 + 4
    ifd0 = (struct.pack(end + "H", 1)
            + struct.pack(end + "HHII", 34665, 4, 1, exif_off)
            + struct.pack(end + "I", 0))
    exif = (struct.pack(end + "H", 1)
            + struct.pack(end + "HHII", 36867, 2, 20, value_off)
            + struct.pack(end + "I", 0))
    return header + ifd0 + exif + date.encode() + b"\x00"


def fake_heic(path: Path, date: str, gap: int = 19000) -> None:
    """A HEIC-shaped file with the decoy item name well before the real data."""
    decoy = b"\x00\x00\x29infe\x02\x00\x00\x01\x00'\x00\x00" + b"Exif\x00\x00"
    path.write_bytes(
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"
        + b"\x00" * 400
        + decoy                       # the trap: the item NAME, not the payload
        + b"\x11" * gap               # container bytes in between
        + tiff_block(date)
        + b"\x22" * 2048)


class TestHeicDates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-heic-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_decoy_item_name_does_not_defeat_extraction(self):
        p = self.tmp / "IMG_0001.heic"
        fake_heic(p, "2018:11:20 20:43:27")
        self.assertEqual(mediatime._exif_date(p, "heic"),
                         datetime(2018, 11, 20, 20, 43, 27))

    def test_it_works_regardless_of_how_far_away_the_payload_is(self):
        for gap in (0, 500, 19000, 300000):
            with self.subTest(gap=gap):
                p = self.tmp / f"g{gap}.heic"
                fake_heic(p, "2019:04:05 06:07:08", gap=gap)
                self.assertEqual(mediatime._exif_date(p, "heic"),
                                 datetime(2019, 4, 5, 6, 7, 8))

    def test_both_byte_orders_parse(self):
        for big in (True, False):
            with self.subTest(big_endian=big):
                p = self.tmp / f"e{big}.heic"
                p.write_bytes(b"\x00" * 200 + tiff_block("2020:02:02 03:04:05", big))
                self.assertEqual(mediatime._exif_date(p, "heic"),
                                 datetime(2020, 2, 2, 3, 4, 5))

    def test_a_file_with_no_metadata_still_returns_none(self):
        p = self.tmp / "blank.heic"
        p.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x55" * 50000)
        self.assertIsNone(mediatime._exif_date(p, "heic"))

    def test_an_implausible_date_is_not_believed(self):
        """A stray TIFF-looking run inside image data must not win."""
        p = self.tmp / "bogus.heic"
        p.write_bytes(b"\x00" * 100 + tiff_block("1901:01:01 00:00:00")
                      + tiff_block("2021:06:07 08:09:10"))
        got = mediatime._exif_date(p, "heic")
        self.assertEqual(got, datetime(2021, 6, 7, 8, 9, 10))

    def test_capture_time_prefers_embedded_metadata_over_mtime(self):
        import os
        p = self.tmp / "IMG_9.heic"
        fake_heic(p, "2016:03:04 05:06:07")
        os.utime(p, (0, datetime.now().timestamp()))   # mtime says "today"
        when, source = mediatime.capture_time(p, "heic")
        self.assertEqual(source, "exif")
        self.assertEqual(when.year, 2016)


class RedateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-redate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()

        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd"))],
            sources=[SourceSpec("phone", str(self.src))])
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)

    def stage_misdated(self, n: int = 3):
        """Photos whose real date is old but which import as 'today'.

        Simulated by writing the TIFF block where the old scanner could not
        reach it — exactly the shape that produced the bug.
        """
        import os
        for i in range(n):
            p = self.src / f"IMG_{i}.heic"
            fake_heic(p, f"2018:0{i + 1}:15 12:00:00")
            os.utime(p, (datetime.now().timestamp(),) * 2)
        return n


class TestRedate(RedateTestCase):
    def import_with_broken_dates(self, n=3):
        """Force the old failure mode so there is something to repair."""
        real = mediatime._exif_date
        mediatime._exif_date = lambda p, e: None
        try:
            self.stage_misdated(n)
            ingest.ingest_source(self.cfg, self.cat, "phone", self.src)
        finally:
            mediatime._exif_date = real
        sync.push(self.cfg, self.cat, "hdd")

    def test_it_finds_photos_filed_under_a_guessed_date(self):
        self.import_with_broken_dates()
        rep = redate.find(self.cfg, self.cat)
        self.assertEqual(len(rep.candidates), 3)
        for c in rep.candidates:
            self.assertEqual(c.new_when.year, 2018)
            self.assertEqual(c.source, "exif")

    def test_it_never_trusts_the_filename(self):
        """PhotoVault wrote that filename from the bad date, so believing it
        would confidently re-derive the same wrong answer."""
        self.import_with_broken_dates(1)
        asset = self.cat.all_assets()[0]
        stored = self.cfg.primary_root / asset["rel_path"]
        when, source = redate.embedded_date(stored, "heic")
        self.assertEqual(source, "exif")
        self.assertEqual(when.year, 2018)

    def test_dry_run_moves_nothing(self):
        self.import_with_broken_dates()
        before = sorted(p.name for p in self.cfg.primary_root.rglob("*.heic"))
        rep = redate.apply(self.cfg, self.cat, redate.find(self.cfg, self.cat))
        self.assertEqual(rep.moved, 0)
        self.assertEqual(sorted(p.name for p in
                                self.cfg.primary_root.rglob("*.heic")), before)

    def test_apply_moves_the_file_on_every_device(self):
        self.import_with_broken_dates()
        rep = redate.apply(self.cfg, self.cat, redate.find(self.cfg, self.cat),
                           dry_run=False)
        self.assertEqual(rep.moved, 3)
        for replica in ("mac", "hdd"):
            paths = [str(p.relative_to(self.tmp / replica))
                     for p in (self.tmp / replica).rglob("*.heic")]
            self.assertTrue(all(p.startswith("2018/") for p in paths), paths)

    def test_the_catalog_follows_the_files(self):
        self.import_with_broken_dates()
        redate.apply(self.cfg, self.cat, redate.find(self.cfg, self.cat),
                     dry_run=False)
        for asset in self.cat.all_assets():
            self.assertTrue(asset["rel_path"].startswith("2018/"))
            self.assertEqual(asset["time_source"], "exif")
            stored = self.cfg.primary_root / asset["rel_path"]
            self.assertTrue(stored.is_file(), f"{asset['rel_path']} missing")

    def test_it_refuses_while_a_device_is_absent(self):
        """A rename recorded in the catalog but not performed on an absent
        drive leaves that drive holding a file nobody will look for again."""
        self.import_with_broken_dates()
        self.cfg.replicas.append(
            ReplicaSpec("offsite", "local", "/Volumes/DefinitelyNotMounted/lib"))
        rep = redate.apply(self.cfg, self.cat, redate.find(self.cfg, self.cat),
                           dry_run=False)
        self.assertIn("offsite", rep.blocked_by)
        self.assertEqual(rep.moved, 0)

    def test_correctly_dated_photos_are_left_alone(self):
        self.stage_misdated(2)
        ingest.ingest_source(self.cfg, self.cat, "phone", self.src)
        sync.push(self.cfg, self.cat, "hdd")
        rep = redate.find(self.cfg, self.cat)
        self.assertEqual(rep.candidates, [], "already exif-dated on import")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRedateFromOriginals(RedateTestCase):
    """Some photos never had an embedded date — scans and screenshots. If the
    file's own timestamp was lost on the way in, the stored copy cannot be
    repaired from itself: the information is not in the bytes. It is still in
    the originals."""

    def stage_undated(self, n: int = 3) -> Path:
        """Files with no metadata at all, but meaningful timestamps."""
        import os
        from make_fixtures import jpeg_with_exif

        originals = self.tmp / "originals"
        originals.mkdir()
        for i in range(n):
            p = originals / f"scan_{i}.jpg"
            p.write_bytes(jpeg_with_exif(None, f"scanned-{i}".encode() * 80))
            when = datetime(2014, 6, 15 + i, 12, 0, 0).timestamp()
            os.utime(p, (when, when))
        return originals

    def import_losing_timestamps(self, originals: Path) -> None:
        """What a browser upload used to do: rewrite the file with a fresh
        mtime, destroying the only date it had."""
        import os
        import shutil as sh

        inbox = self.src
        for p in originals.iterdir():
            dest = inbox / p.name
            sh.copyfile(p, dest)                      # copyfile, not copy2
            os.utime(dest, (datetime.now().timestamp(),) * 2)
        ingest.ingest_source(self.cfg, self.cat, "phone", inbox)
        sync.push(self.cfg, self.cat, "hdd")

    def test_dates_are_recovered_from_the_original_files(self):
        originals = self.stage_undated(3)
        self.import_losing_timestamps(originals)

        for asset in self.cat.all_assets():
            self.assertTrue(asset["rel_path"].startswith(
                f"{datetime.now().year}/"), "should be misfiled under today")

        rep = redate.find_from_originals(self.cfg, self.cat, originals)
        self.assertEqual(len(rep.candidates), 3)
        redate.apply(self.cfg, self.cat, rep, dry_run=False)

        for asset in self.cat.all_assets():
            self.assertTrue(asset["rel_path"].startswith("2014/06/"),
                            asset["rel_path"])
            for replica in ("mac", "hdd"):
                self.assertTrue((self.tmp / replica / asset["rel_path"]).is_file())

    def test_matching_is_by_content_not_by_name(self):
        """A file that merely looks similar must never contribute its date to
        the wrong photo."""
        from make_fixtures import jpeg_with_exif
        import os

        originals = self.stage_undated(2)
        self.import_losing_timestamps(originals)

        impostor = originals / "scan_0_copy.jpg"       # same name shape, other bytes
        impostor.write_bytes(jpeg_with_exif(None, b"completely different" * 90))
        when = datetime(1999, 1, 1, 12, 0, 0).timestamp()
        os.utime(impostor, (when, when))

        rep = redate.find_from_originals(self.cfg, self.cat, originals)
        self.assertEqual(len(rep.candidates), 2, "the impostor must not match")
        self.assertTrue(all(c.new_when.year == 2014 for c in rep.candidates))

    def test_an_implausible_timestamp_is_ignored(self):
        import os

        originals = self.stage_undated(2)
        self.import_losing_timestamps(originals)
        for p in originals.iterdir():
            os.utime(p, (0, 0))          # epoch zero — not a real capture date

        rep = redate.find_from_originals(self.cfg, self.cat, originals)
        self.assertEqual(rep.candidates, [])

    def test_embedded_metadata_still_wins_over_the_file_timestamp(self):
        import os
        from make_fixtures import jpeg_with_exif

        originals = self.tmp / "mixed"
        originals.mkdir()
        p = originals / "photo.jpg"
        p.write_bytes(jpeg_with_exif("2009:05:06 07:08:09", b"x" * 300))
        os.utime(p, (datetime(2014, 6, 15).timestamp(),) * 2)
        self.import_losing_timestamps(originals)

        rep = redate.find_from_originals(self.cfg, self.cat, originals)
        # There is real metadata, so it is preferred over the 2014 timestamp.
        self.assertTrue(all(c.source == "exif" for c in rep.candidates))
        self.assertTrue(all(c.new_when.year == 2009 for c in rep.candidates))
