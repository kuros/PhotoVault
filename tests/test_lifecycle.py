"""End-to-end tests for the whole ingest -> replicate -> verify -> recover loop.

Each test creates a throwaway library in a temp directory, so nothing here can
touch a real photo. These are the behaviours that must never regress: a backup
tool that quietly stops backing up is worse than no backup tool at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from photovault import (health, identity, ingest, placement, sync,
                        verify)
from photovault.catalog import Catalog
from photovault.config import Config, ReplicaSpec, SourceSpec
from photovault.replicas import ReplicaError

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


class TestMultipleDrives(VaultTestCase):
    """With one external drive a path is an adequate name for it. With two it
    is not: macOS hands /Volumes/<Name> out first-come, so a second drive of
    the same name lands on the first one's path whenever that one is absent."""

    def setUp(self):
        super().setUp()
        self.cfg.replicas.append(
            ReplicaSpec("hdd2", "local", str(self.tmp / "hdd2"), offline=True))
        self.cat.upsert_replica("hdd2", "local", str(self.tmp / "hdd2"),
                                is_offline=True)

    def sync_all(self):
        for name in ("hdd", "hdd2", "win"):
            sync.push(self.cfg, self.cat, name)

    def test_four_replicas_all_reach_full_redundancy(self):
        self.ingest_all()
        self.sync_all()
        h = health.assess(self.cfg, self.cat)
        self.assertEqual(h.underprotected, 0)
        self.assertTrue(h.ok)
        for name in ("mac", "hdd", "hdd2", "win"):
            self.assertEqual(h.per_replica[name]["present"], 13)

    def test_each_drive_is_stamped_with_its_own_identity(self):
        from photovault.replicas import MARKER_NAME
        self.ingest_all()
        self.sync_all()
        ids = {}
        for name in ("hdd", "hdd2", "win"):
            marker = json.loads((self.tmp / name / MARKER_NAME).read_text())
            self.assertEqual(marker["replica"], name)
            ids[name] = marker["uuid"]
        self.assertEqual(len(set(ids.values())), 3, "uuids must be distinct")

    def test_swapped_drive_is_refused_not_silently_accepted(self):
        self.ingest_all()
        self.sync_all()
        before = sorted(p.name for p in (self.tmp / "hdd2").rglob("*.jpg"))

        # hdd goes in a drawer; hdd2 is plugged in and takes hdd's path.
        shutil.rmtree(self.tmp / "hdd")
        (self.tmp / "hdd2").rename(self.tmp / "hdd")

        with self.assertRaises(identity.IdentityMismatch):
            sync.reconcile(self.cfg, self.cat, "hdd")
        with self.assertRaises(identity.IdentityMismatch):
            sync.push(self.cfg, self.cat, "hdd")

        after = sorted(p.name for p in (self.tmp / "hdd").rglob("*.jpg"))
        self.assertEqual(before, after, "the wrong drive must not be written to")

    def test_absent_drive_is_not_conjured_into_existence(self):
        """An unplugged drive leaves no directory. Claiming that path would
        create it on the internal disk and 'restore' the library into it."""
        self.ingest_all()
        self.sync_all()
        shutil.rmtree(self.tmp / "hdd2")

        with self.assertRaises(identity.IdentityMismatch):
            sync.push(self.cfg, self.cat, "hdd2")
        self.assertFalse((self.tmp / "hdd2").exists(),
                         "no directory may be created for an absent drive")

    def test_scrub_skips_a_misidentified_drive_loudly(self):
        self.ingest_all()
        self.sync_all()
        shutil.rmtree(self.tmp / "hdd")
        (self.tmp / "hdd2").rename(self.tmp / "hdd")

        st = verify.scrub(self.cfg, self.cat, force=True)
        self.assertTrue(any("SKIPPED hdd" in p for p in st.problems))
        self.assertEqual(st.corrupt, 0)

    def test_adopt_re_registers_a_genuinely_replaced_drive(self):
        self.ingest_all()
        self.sync_all()
        old_uuid = self.cat.replica("hdd2")["uuid"]

        # The drive died and was replaced with a blank one.
        shutil.rmtree(self.tmp / "hdd2")
        (self.tmp / "hdd2").mkdir()
        with self.assertRaises(identity.IdentityMismatch):
            sync.push(self.cfg, self.cat, "hdd2")

        new_uuid = identity.adopt(self.cfg, self.cat, "hdd2")
        self.assertNotEqual(new_uuid, old_uuid)
        sync.reconcile(self.cfg, self.cat, "hdd2")
        st = sync.push(self.cfg, self.cat, "hdd2")
        self.assertEqual(st.copied, 13)
        self.assertTrue(health.assess(self.cfg, self.cat).ok)

    def test_staleness_tracks_which_drive_to_plug_in_next(self):
        self.ingest_all()
        sync.push(self.cfg, self.cat, "hdd")
        _, days = identity.staleness(self.cat, "hdd")
        self.assertEqual(days, 0)
        self.assertEqual(identity.staleness(self.cat, "hdd2"), (None, None))


class ShardTestCase(VaultTestCase):
    """A library split across drives too small to each hold all of it."""

    CAPACITY = "1MB"

    def setUp(self):
        super().setUp()
        self.cfg.replicas = [
            ReplicaSpec("mac", "local", str(self.tmp / "mac")),
            ReplicaSpec("s1", "local", str(self.tmp / "s1"), offline=True,
                        mode="shard", capacity=self.CAPACITY),
            ReplicaSpec("s2", "local", str(self.tmp / "s2"), offline=True,
                        mode="shard", capacity=self.CAPACITY),
            ReplicaSpec("s3", "local", str(self.tmp / "s3"), offline=True,
                        mode="shard", capacity=self.CAPACITY),
        ]
        self.cfg.min_copies = 3
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)

    def sync_all(self):
        for name in ("s1", "s2", "s3"):
            sync.push(self.cfg, self.cat, name)


class TestSharding(ShardTestCase):
    def test_every_photo_still_reaches_min_copies(self):
        self.ingest_all()
        self.sync_all()
        h = health.assess(self.cfg, self.cat)
        self.assertEqual(h.underprotected, 0)
        self.assertEqual(h.no_offline_copy, 0)
        self.assertTrue(h.ok)

    def test_shards_hold_subsets_not_the_whole_library(self):
        self.ingest_all()
        self.sync_all()
        held = {n: len(list((self.tmp / n).rglob("*.jpg")))
                for n in ("s1", "s2", "s3")}
        self.assertTrue(all(0 < v < 13 for v in held.values()),
                        f"each shard should hold a proper subset, got {held}")
        # mac is full, plus two shard copies each = 13 * 2 across the shards.
        self.assertEqual(sum(held.values()), 26)

    def test_placement_is_deterministic(self):
        self.ingest_all()
        first = placement.build_plan(self.cfg, self.cat).assignments
        second = placement.build_plan(self.cfg, self.cat).assignments
        self.assertEqual(first, second)

    def test_adding_a_drive_moves_only_a_fraction(self):
        """Rendezvous hashing's real payoff: `hash % n` would move nearly
        everything, which on a 1 TB library is days of copying."""
        self.ingest_all()
        before = placement.build_plan(self.cfg, self.cat).assignments

        self.cfg.replicas.append(
            ReplicaSpec("s4", "local", str(self.tmp / "s4"), offline=True,
                        mode="shard", capacity=self.CAPACITY))
        after = placement.build_plan(self.cfg, self.cat).assignments

        moved = sum(1 for h in before if before[h] != after[h])
        self.assertLess(moved, len(before) * 0.75,
                        "adding a 4th drive should not reshuffle everything")

    def test_plan_reports_when_drives_are_too_small(self):
        for spec in self.cfg.shard_replicas:
            spec.capacity = "2KB"
        self.ingest_all()
        plan = placement.build_plan(self.cfg, self.cat)
        self.assertFalse(plan.ok)
        self.assertTrue(plan.unplaceable)

    def test_primary_may_not_be_a_shard(self):
        """Ingest needs somewhere to write every new photo."""
        import tomllib
        from photovault import config as cfgmod
        path = self.tmp / "bad.toml"
        path.write_text('''
[vault]
primary = "mac"
[[replica]]
name = "mac"
kind = "local"
root = "/tmp/x"
mode = "shard"
''')
        with self.assertRaises(ValueError):
            cfgmod.load(path)


class TestShardRecovery(ShardTestCase):
    def test_one_shard_alone_cannot_rebuild_the_catalog(self):
        self.ingest_all()
        self.sync_all()
        self.cat.close()
        self.cfg.catalog_path.unlink()
        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)
        recovered = sync.rebuild_from(self.cfg, self.cat, "s1")
        self.assertLess(recovered, 13, "a shard is not a complete backup")

    def test_all_shards_together_rebuild_everything(self):
        self.ingest_all()
        self.sync_all()
        expected = {a["hash"] for a in self.cat.all_assets()}

        # The Mac dies: full copy and catalog gone at once.
        self.cat.close()
        shutil.rmtree(self.tmp / "mac")
        self.cfg.catalog_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.cfg.catalog_path) + suffix).unlink(missing_ok=True)

        self.cat = Catalog(self.cfg.catalog_path)
        for spec in self.cfg.replicas:
            self.cat.upsert_replica(spec.name, spec.kind, spec.root,
                                    is_offline=spec.offline)
        for name in ("s1", "s2", "s3"):
            sync.rebuild_from(self.cfg, self.cat, name)
        self.assertEqual({a["hash"] for a in self.cat.all_assets()}, expected)

        for name in ("mac", "s1", "s2", "s3"):
            sync.reconcile(self.cfg, self.cat, name)
        sync.push(self.cfg, self.cat, "mac")
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.jpg"))), 13)
        self.assertTrue(health.assess(self.cfg, self.cat).ok)

    def test_shard_recovery_kit_says_it_is_partial(self):
        self.ingest_all()
        self.sync_all()
        text = (self.tmp / "s1" / "RECOVERY.md").read_text()
        self.assertIn("ONLY PART", text)
        self.assertIn("rebuild --all", text)
        # The full replica must NOT carry that warning.
        sync.push(self.cfg, self.cat, "mac")
        self.assertNotIn("ONLY PART", (self.tmp / "mac" / "RECOVERY.md").read_text())


class TestRebalanceSafety(ShardTestCase):
    def test_rebalance_defaults_to_a_preview(self):
        self.ingest_all()
        self.sync_all()
        before = len(list((self.tmp / "s1").rglob("*.jpg")))
        self.cfg.replicas.append(
            ReplicaSpec("s4", "local", str(self.tmp / "s4"), offline=True,
                        mode="shard", capacity=self.CAPACITY))
        self.cat.upsert_replica("s4", "local", str(self.tmp / "s4"), is_offline=True)
        sync.push(self.cfg, self.cat, "s4")

        sync.rebalance(self.cfg, self.cat, "s1")   # dry run by default
        self.assertEqual(len(list((self.tmp / "s1").rglob("*.jpg"))), before,
                         "a dry run must not delete anything")

    def test_rebalance_removes_only_surplus_and_keeps_totals_right(self):
        self.ingest_all()
        self.sync_all()
        self.cfg.replicas.append(
            ReplicaSpec("s4", "local", str(self.tmp / "s4"), offline=True,
                        mode="shard", capacity=self.CAPACITY))
        self.cat.upsert_replica("s4", "local", str(self.tmp / "s4"), is_offline=True)
        sync.push(self.cfg, self.cat, "s4")
        for name in ("s1", "s2", "s3", "s4"):
            sync.rebalance(self.cfg, self.cat, name, dry_run=False)

        total = sum(len(list((self.tmp / n).rglob("*.jpg")))
                    for n in ("s1", "s2", "s3", "s4"))
        self.assertEqual(total, 26, "13 photos x 2 shard copies")
        self.assertTrue(health.assess(self.cfg, self.cat).ok)

    def test_never_deletes_when_copies_cannot_be_verified(self):
        """The only destructive operation must refuse on stale beliefs.

        A placement row saying "present" is a belief. Deleting a photo because
        of a belief that is no longer true is exactly the failure this whole
        program exists to prevent, so surviving copies are re-read and
        re-hashed before anything is removed.
        """
        self.ingest_all()
        self.sync_all()

        # Add a drive and sync it, so some photos now have a spare copy. Only
        # an over-replicated photo can ever be removed: deletion requires
        # min_copies to REMAIN, so with exactly min_copies there is nothing to
        # give up. This is why the workflow is sync first, rebalance second.
        self.cfg.replicas.append(
            ReplicaSpec("s4", "local", str(self.tmp / "s4"), offline=True,
                        mode="shard", capacity=self.CAPACITY))
        self.cat.upsert_replica("s4", "local", str(self.tmp / "s4"), is_offline=True)
        sync.push(self.cfg, self.cat, "s4")

        preview = sync.rebalance(self.cfg, self.cat, "s1")
        self.assertGreater(preview.removed, 0, "test needs surplus to exist")

        # Destroy every other copy while the catalog still believes in them.
        shutil.rmtree(self.tmp / "mac")
        for name in ("s2", "s3", "s4"):
            if (self.tmp / name).exists():
                shutil.rmtree(self.tmp / name)
        still_believed = self.cat.db.execute(
            "SELECT COUNT(*) n FROM placement WHERE state='present' "
            "AND replica != 's1'").fetchone()["n"]
        self.assertGreater(still_believed, 0,
                           "the catalog should still believe those copies exist")

        before = sorted(p.name for p in (self.tmp / "s1").rglob("*.jpg"))
        st = sync.rebalance(self.cfg, self.cat, "s1", dry_run=False)
        after = sorted(p.name for p in (self.tmp / "s1").rglob("*.jpg"))

        self.assertEqual(st.removed, 0, "nothing may be deleted")
        self.assertGreater(st.kept_unsafe, 0)
        self.assertEqual(before, after, "no photo may be deleted unverified")

    def test_rebalance_refuses_on_a_full_replica(self):
        self.ingest_all()
        with self.assertRaises(ReplicaError):
            sync.rebalance(self.cfg, self.cat, "mac")


class TestInboxWatcher(VaultTestCase):
    """Getting photos off a phone needs a deliberate act; everything after
    that should need none."""

    def setUp(self):
        super().setUp()
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()
        self.cfg.sources = [SourceSpec("iphone", str(self.inbox),
                                       clear_after_import=True)]

    def drop(self, n: int = 4) -> list[Path]:
        """Simulate Image Capture depositing photos."""
        from make_fixtures import jpeg_with_exif
        made = []
        for i in range(n):
            p = self.inbox / f"IMG_{7000 + i}.jpg"
            p.write_bytes(jpeg_with_exif(f"2025:03:1{i} 09:0{i}:00",
                                         f"inbox-{i}".encode() * 50))
            made.append(p)
        return made

    def test_one_pass_imports_and_replicates(self):
        from photovault import watcher
        self.drop(4)
        watcher.SETTLE_SECONDS = 0.01
        stats = watcher.run_once(self.cfg, self.cat, report=lambda *_: None)
        self.assertEqual(stats.imported, 4)
        self.assertEqual(len(list((self.tmp / "mac").rglob("*.jpg"))), 4)
        # Every backup replica got them too, without a separate command.
        for name in ("hdd", "win"):
            self.assertEqual(len(list((self.tmp / name).rglob("*.jpg"))), 4)

    def test_inbox_is_emptied_only_after_the_library_copy_verifies(self):
        from photovault import watcher
        self.drop(4)
        watcher.SETTLE_SECONDS = 0.01
        watcher.run_once(self.cfg, self.cat, report=lambda *_: None)
        self.assertEqual(list(self.inbox.rglob("*.jpg")), [],
                         "inbox should empty once photos are safely stored")

    def test_inbox_is_kept_when_the_library_copy_is_damaged(self):
        """Clearing an inbox deletes originals, so a corrupt library copy must
        stop it - otherwise a bad import quietly destroys the only good file."""
        from photovault import watcher
        self.drop(3)
        watcher.SETTLE_SECONDS = 0.01
        ingest.ingest_source(self.cfg, self.cat, "iphone", self.inbox)

        for stored in (self.tmp / "mac").rglob("*.jpg"):
            stored.write_bytes(stored.read_bytes() + b"corrupted")

        removed, notes = watcher.clear_imported(self.cfg, self.cat, self.inbox)
        self.assertEqual(removed, 0)
        self.assertEqual(len(list(self.inbox.rglob("*.jpg"))), 3)
        self.assertTrue(any("did not verify" in n for n in notes))

    def test_sources_without_the_flag_are_never_emptied(self):
        """An Apple Photos library is a source you also browse. Deleting from
        it would be catastrophic, so clearing is strictly opt-in."""
        from photovault import watcher
        self.cfg.sources = [SourceSpec("mac", str(self.inbox))]  # no flag
        self.drop(3)
        watcher.SETTLE_SECONDS = 0.01
        watcher.run_once(self.cfg, self.cat, report=lambda *_: None)
        self.assertEqual(len(list(self.inbox.rglob("*.jpg"))), 3)

    def test_a_file_still_being_written_is_not_imported(self):
        """A half-copied photo hashes as a different, corrupt asset - and
        PhotoVault would then faithfully replicate that corruption."""
        from photovault import watcher
        growing = self.inbox / "IMG_9999.jpg"
        growing.write_bytes(b"\xff\xd8" + b"\x00" * 1000)

        calls = {"n": 0}
        real_snapshot = watcher.snapshot

        def changing(root):
            # Pretend the file grows between the two samples.
            calls["n"] += 1
            snap = dict(real_snapshot(root))
            if calls["n"] > 1:
                snap[str(growing)] = (5000 + calls["n"] * 100, 1.0)
            return snap

        watcher.snapshot = changing
        watcher.SETTLE_SECONDS = 0.01
        try:
            settled = watcher.wait_until_settled(self.inbox, settle=0.01, timeout=0.2)
        finally:
            watcher.snapshot = real_snapshot
        self.assertFalse(settled, "a growing folder must not be reported settled")
        self.assertEqual(len(self.cat.all_assets()), 0)

    def test_settled_folder_is_recognised(self):
        from photovault import watcher
        self.drop(2)
        self.assertTrue(watcher.wait_until_settled(self.inbox, settle=0.01,
                                                   timeout=5))
