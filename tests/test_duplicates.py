"""Tests for near-duplicate detection and the review-then-delete flow.

This is the only feature that removes a photo you still want, so the tests
lean hard on the refusals: what must NOT be deleted, and why.
"""

from __future__ import annotations

import math
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from photovault import duplicates, ingest, perceptual, sync
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec


def write_png(path: Path, w: int, h: int, fn, filter_type: int = 0) -> None:
    """Encode an RGB PNG, optionally using a specific scanline filter."""
    rows = [[fn(x / w, y / h) for x in range(w)] for y in range(h)]
    raw = bytearray()
    prev = [(0, 0, 0)] * w
    for row in rows:
        raw.append(filter_type)
        for x, px in enumerate(row):
            left = row[x - 1] if x else (0, 0, 0)
            up = prev[x]
            upleft = prev[x - 1] if x else (0, 0, 0)
            for c in range(3):
                v = px[c]
                if filter_type == 1:
                    v -= left[c]
                elif filter_type == 2:
                    v -= up[c]
                elif filter_type == 3:
                    v -= (left[c] + up[c]) >> 1
                elif filter_type == 4:
                    p = left[c] + up[c] - upleft[c]
                    pa, pb, pc = abs(p - left[c]), abs(p - up[c]), abs(p - upleft[c])
                    v -= left[c] if (pa <= pb and pa <= pc) else (
                        up[c] if pb <= pc else upleft[c])
                raw.append(v & 0xFF)
        prev = row

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b""))


def textured_scene(seed: int):
    """A photo-like image: blobs plus fine texture, not a smooth gradient."""
    rnd = random.Random(seed * 77)
    blobs = [(rnd.random(), rnd.random(), rnd.random() * .25 + .05) for _ in range(7)]

    def fn(u, v):
        r = g = b = 20
        for cx, cy, rad in blobs:
            d = math.hypot(u - cx, v - cy)
            if d < rad:
                f = 1 - d / rad
                r += int(230 * f * abs(math.sin(cx * 9 + seed)))
                g += int(230 * f * abs(math.cos(cy * 7 + seed)))
                b += int(230 * f * abs(math.sin((cx + cy) * 5 + seed)))
        t = int(28 * math.sin(u * 70) * math.cos(v * 70))
        return tuple(max(0, min(255, c + t)) for c in (r, g, b))
    return fn


class TestPngDecoder(unittest.TestCase):
    """The sips fallback decodes PNG in pure Python. An encoder picks a filter
    per scanline, so getting any one of the five wrong silently corrupts the
    hash rather than failing loudly."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-png-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_every_scanline_filter_round_trips(self):
        fn = textured_scene(1)
        expected = None
        for ftype in range(5):
            path = self.tmp / f"f{ftype}.png"
            write_png(path, 16, 12, fn, filter_type=ftype)
            decoded = perceptual.decode_png(path.read_bytes())
            self.assertIsNotNone(decoded, f"filter {ftype} failed to decode")
            w, h, channels, data = decoded
            self.assertEqual((w, h, channels), (16, 12, 3))
            if expected is None:
                expected = data
            else:
                self.assertEqual(data, expected,
                                 f"filter {ftype} decoded differently")

    def test_garbage_is_rejected_not_crashed_on(self):
        self.assertIsNone(perceptual.decode_png(b"not a png at all"))
        self.assertIsNone(perceptual.decode_png(b""))


@unittest.skipUnless(perceptual.available(), "no image decoder available")
class TestPerceptualHash(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-ph-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def variants(self, seed: int) -> dict[str, Path]:
        original = self.tmp / f"s{seed}_original.png"
        write_png(original, 480, 360, textured_scene(seed))
        half = self.tmp / f"s{seed}_half.png"
        subprocess.run(["sips", "-Z", "240", str(original), "--out", str(half)],
                       capture_output=True)
        jpeg = self.tmp / f"s{seed}_recompressed.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "30",
                        str(half), "--out", str(jpeg)], capture_output=True)
        return {"original": original, "half": half, "jpeg": jpeg}

    def test_survives_resize_and_recompression(self):
        v = self.variants(3)
        base = perceptual.dhash(v["original"])
        self.assertIsNotNone(base)
        for name in ("half", "jpeg"):
            d = perceptual.distance(base, perceptual.dhash(v[name]))
            self.assertLessEqual(d, duplicates.DEFAULT_THRESHOLD,
                                 f"{name} should still look like the original")

    def test_different_photos_are_far_apart(self):
        a = perceptual.dhash(self.variants(4)["original"])
        b = perceptual.dhash(self.variants(9)["original"])
        self.assertGreater(perceptual.distance(a, b),
                           duplicates.DEFAULT_THRESHOLD * 2)

    def test_flat_images_are_treated_as_degenerate(self):
        """A blank wall matches every other blank wall perfectly while having
        nothing in common with them. Grouping on that would propose deleting
        unrelated photos."""
        blank = self.tmp / "blank.png"
        write_png(blank, 200, 200, lambda u, v: (128, 128, 128))
        h = perceptual.dhash(blank)
        self.assertTrue(perceptual.is_degenerate(h))
        self.assertFalse(perceptual.is_degenerate("1c3f5a7b9d2e4f61"))

    def test_undecodable_file_returns_none(self):
        junk = self.tmp / "broken.jpg"
        junk.write_bytes(b"\xff\xd8 not really a jpeg")
        self.assertIsNone(perceptual.dhash(junk))


@unittest.skipUnless(perceptual.available(), "no image decoder available")
class DupTestCase(unittest.TestCase):
    """A library of distinct scenes, each present three times: original, half
    resolution, and a low-quality re-encode."""

    SCENES = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-dup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()

        for i in range(self.SCENES):
            original = self.src / f"scene{i}_original.png"
            write_png(original, 480, 360, textured_scene(i))
            half = self.src / f"scene{i}_half.png"
            subprocess.run(["sips", "-Z", "240", str(original), "--out", str(half)],
                           capture_output=True)
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions",
                            "30", str(half), "--out",
                            str(self.src / f"scene{i}_sent.jpg")],
                           capture_output=True)

        self.cfg = Config(
            primary="mac", min_copies=2, require_offline_copy=False,
            catalog_path=self.tmp / "catalog.db",
            replicas=[ReplicaSpec("mac", "local", str(self.tmp / "mac")),
                      ReplicaSpec("hdd", "local", str(self.tmp / "hdd"))],
            sources=[SourceSpec("old", str(self.src))])
        self.cat = Catalog(self.cfg.catalog_path)
        self.addCleanup(self.cat.close)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root)
        ingest.ingest_source(self.cfg, self.cat, "old", self.src)
        sync.push(self.cfg, self.cat, "hdd")
        duplicates.scan(self.cfg, self.cat)

    def groups(self):
        return duplicates.find_groups(self.cfg, self.cat)


class TestGrouping(DupTestCase):
    def test_each_scene_forms_exactly_one_group(self):
        groups = self.groups()
        self.assertEqual(len(groups), self.SCENES)
        for g in groups:
            self.assertEqual(len(g.members), 3)

    def test_unrelated_scenes_are_never_merged(self):
        seen = set()
        for g in self.groups():
            ids = {m.rel_path for m in g.members}
            self.assertTrue(seen.isdisjoint(ids))
            seen |= ids

    def test_the_highest_resolution_copy_is_suggested(self):
        for g in self.groups():
            keeper = next(m for m in g.members if m.hash == g.suggested_keep)
            self.assertEqual(keeper.pixels, max(m.pixels for m in g.members))

    def test_exact_duplicates_never_reach_this_stage(self):
        """Byte-identical copies are collapsed at ingest by content hashing."""
        shutil.copy2(self.src / "scene0_original.png", self.src / "copy.png")
        st = ingest.ingest_source(self.cfg, self.cat, "old", self.src)
        self.assertEqual(st.imported, 0)
        self.assertGreaterEqual(st.duplicates, 1)

    def test_rescanning_does_not_redo_work(self):
        st = duplicates.scan(self.cfg, self.cat)
        self.assertEqual(st.hashed, 0, "already-hashed photos must be skipped")


class TestApplySafety(DupTestCase):
    def mark_all_groups(self):
        decisions = {}
        for g in self.groups():
            for m in g.members:
                decisions[m.hash] = "keep" if m.hash == g.suggested_keep else "delete"
        duplicates.decide(self.cat, decisions)

    def test_nothing_happens_without_a_decision(self):
        rep = duplicates.apply(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.deleted, 0)
        self.assertEqual(len(self.cat.all_assets()), self.SCENES * 3)

    def test_dry_run_is_the_default_and_deletes_nothing(self):
        self.mark_all_groups()
        before = len(list((self.tmp / "mac").rglob("*.*")))
        rep = duplicates.apply(self.cfg, self.cat)
        self.assertEqual(rep.deleted, self.SCENES * 2)
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.*"))), before)
        self.assertEqual(len(self.cat.all_assets()), self.SCENES * 3)

    def test_apply_removes_from_every_replica_and_the_catalog(self):
        self.mark_all_groups()
        rep = duplicates.apply(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.deleted, self.SCENES * 2)
        self.assertEqual(rep.refused, [])
        self.assertEqual(len(self.cat.all_assets()), self.SCENES)
        # Count media only: a replica also carries its recovery kit.
        from photovault.ingest import WANTED_EXT
        from photovault.mediatime import normalize_ext
        for replica in ("mac", "hdd"):
            photos = [p for p in (self.tmp / replica).rglob("*")
                      if p.is_file() and normalize_ext(p) in WANTED_EXT]
            self.assertEqual(len(photos), self.SCENES,
                             f"{replica} should hold one photo per scene")
        orphans = self.cat.db.execute(
            "SELECT COUNT(*) n FROM placement p LEFT JOIN asset a ON a.hash = p.hash "
            "WHERE a.hash IS NULL").fetchone()["n"]
        self.assertEqual(orphans, 0)

    def test_refuses_when_the_kept_photo_cannot_be_verified(self):
        """The failure this guards: deleting a duplicate because the catalog
        says its twin is stored, when the twin's bytes have silently rotted."""
        self.mark_all_groups()
        for replica in ("mac", "hdd"):
            for p in (self.tmp / replica).rglob("*.png"):
                p.write_bytes(p.read_bytes() + b"rot")

        rep = duplicates.apply(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.deleted, 0)
        self.assertTrue(rep.refused)
        self.assertTrue(any("verified" in why for _, why in rep.refused))
        self.assertEqual(len(self.cat.all_assets()), self.SCENES * 3)

    def test_refuses_to_empty_a_whole_group(self):
        decisions = {m.hash: "delete" for g in self.groups() for m in g.members}
        duplicates.decide(self.cat, decisions)
        rep = duplicates.apply(self.cfg, self.cat, dry_run=False)
        self.assertEqual(rep.deleted, 0)
        self.assertTrue(any("every photo" in why for _, why in rep.refused))
        self.assertEqual(len(self.cat.all_assets()), self.SCENES * 3)

    def test_the_user_choice_overrides_the_suggestion(self):
        groups = self.groups()
        g = groups[0]
        chosen = next(m for m in g.members if m.hash != g.suggested_keep)
        duplicates.decide(self.cat, {
            m.hash: ("keep" if m.hash == chosen.hash else "delete")
            for m in g.members})

        duplicates.apply(self.cfg, self.cat, dry_run=False)
        surviving = {a["hash"] for a in self.cat.all_assets()}
        self.assertIn(chosen.hash, surviving)
        self.assertNotIn(g.suggested_keep, surviving)

    def test_decisions_persist_across_sessions(self):
        self.mark_all_groups()
        self.cat.close()
        self.cat = Catalog(self.cfg.catalog_path)
        marked = [h for h, a in self.cat.decisions().items() if a == "delete"]
        self.assertEqual(len(marked), self.SCENES * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
