# Architecture — and why it's built this way

This document explains the engineering decisions in PhotoVault. If you're learning to
build software, the decisions matter more than the code: the code is just what falls
out once the decisions are made.

---

## The problem, stated precisely

Vague goal: *"don't lose my photos."*

Precise goal: *"every photo exists on at least 3 independent devices, at least one of
which is normally disconnected, and I can prove the bytes are still correct."*

The second version is buildable. It tells you exactly what to store, exactly what to
check, and exactly when to say something is wrong. **Turning a vague goal into a
checkable statement is most of the design work.** Notice `photovault status` is
literally that sentence rendered as four pass/fail checks.

---

## Decision 1: content addressing

Every photo is identified by a hash of its bytes, not by its filename or path.

```python
# photovault/hashing.py
def hash_file(path) -> tuple[str, int]:
    h = _new()
    while chunk := fh.read(CHUNK):
        h.update(chunk)
```

A hash is a short fingerprint. Change one bit of a file and the fingerprint changes
completely. This one choice gives you four features for free:

- **Deduplication.** `IMG_1234.jpg` copied into three folders over ten years has one
  fingerprint, so it's stored once.
- **Integrity checking.** Re-hash a file later; if the fingerprint changed, the file
  rotted. There's no other reliable way to detect this.
- **Safe repair.** You can verify a replacement copy is correct *before* overwriting
  the bad one.
- **Identity across devices.** The Mac and the Windows laptop agree on what "the same
  photo" means without talking to each other.

Filenames can't do any of this. `IMG_1234.jpg` on two devices might be two different
photos; two different names might be the same photo.

> **The general lesson:** pick your identifier early, and pick one derived from the
> thing itself rather than from where it happens to sit.

---

## Decision 2: the catalog is disposable

`catalog.db` is a SQLite database recording every photo and which devices hold it. It
is a **cache of derivable facts**, never the only home of anything.

Prove it to yourself:

```bash
rm ~/.config/photovault/catalog.db
python3 -m photovault rebuild hdd
python3 -m photovault reconcile --all
```

Everything comes back, because each stored file's path encodes its own date and hash
prefix, and re-hashing recovers the rest.

This is why the storage layout is plain dated folders rather than an opaque blob store.
It costs a little elegance and buys the property that **losing the database costs CPU
time, not photos**. In a backup system that trade is not close.

> **The general lesson:** when you add a database to a system, ask "what happens if
> this is deleted?" If the answer is "data loss," you've made the database load-bearing
> and you now need to back *it* up too. Prefer designs where the database can be
> regenerated.

---

## Decision 3: replicas behind an interface

```python
class Driver(ABC):
    def available(self) -> bool: ...
    def list_present(self) -> set[str]: ...
    def put(self, src, rel_path) -> None: ...
    def hash_of(self, rel_path) -> str | None: ...
    def get(self, rel_path, dest) -> None: ...
```

Five methods. An external drive (`LocalDriver`) and a laptop over SSH (`RsyncDriver`)
implement them completely differently, and *nothing else in the codebase knows the
difference*. `sync.py`, `verify.py` and `health.py` are written entirely against this
abstract shape.

That's why "add Google Cloud Storage later" is a contained change: write those five
methods, and the scheduler, the health report and the corruption repair all start
covering cloud automatically. `GCSDriver` already exists as a stub so the seam is
visible.

> **The general lesson:** when you know a future requirement is coming ("maybe cloud
> later"), you don't build it — you find the *narrowest interface* the existing code
> can be written against, so adding it later touches one file. Guessing at the
> interface is cheap; guessing at the implementation is waste.

---

## Decision 4: every operation is idempotent

Running `ingest` twice imports nothing the second time. Running `sync` twice copies
nothing. This isn't an optimization — it's what makes the system *operable*.

Without it, every command needs you to remember whether it already ran. Interrupted
halfway? Unclear state. Want to run it on a schedule? Now you need locking.

With it, the recovery procedure for literally any failure is "run it again."

```python
if catalog.has_asset(hash_) or hash_ in seen_this_run:
    stats.duplicates += 1
    continue
```

> **The general lesson:** design operations so that doing them twice equals doing them
> once. It's the difference between a tool you trust unattended and one you have to
> babysit.

---

## Decision 5: fail loudly, never silently

Three places where the obvious implementation fails *quietly*, which is the worst way
to fail in a backup system — you find out years later:

**An unplugged drive.** `/Volumes/Backup` doesn't exist when the drive is out. Naive
code calls `mkdir -p` and cheerfully writes 400 GB to your internal SSD while reporting
successful backups. The guard checks that the path is a genuine mount point:

```python
# a real mount has a different device number than its parent
return mount.stat().st_dev != mount.parent.stat().st_dev
```

**Repairing from a bad copy.** When fixing corruption, the source is re-hashed *before*
use, and the downloaded bytes are re-hashed *again* before being written. Skip that and
a single act of "repair" turns one recoverable problem into permanent data loss.

**Half-written files.** `LocalDriver.put` writes to `.part` and then renames:

```python
tmp = dest.with_suffix(dest.suffix + ".part")
shutil.copy2(src, tmp)
tmp.replace(dest)   # atomic
```

Rename is atomic within a filesystem. Pull the cable mid-copy and you get either the
complete file or no file — never a half-photo that *looks* present. This is called an
**atomic write**, and it's one of the most reusable patterns in systems programming.

> **The general lesson:** for each operation, ask "what's the worst way this could fail
> *without telling me*?" Those are the failures worth writing code against. Loud
> failures mostly take care of themselves.

---

## Decision 6: sources are strictly read-only

`ingest` copies *out of* your source folders and never writes, renames or deletes
inside them. There's a test asserting it byte-for-byte:

```python
def test_sources_are_never_modified(self):
    before = {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
    self.ingest_all()
    self.assertEqual(before, after)
```

This means a bug in PhotoVault can waste disk space, but cannot destroy the originals.
Establishing a boundary the code physically cannot cross is far stronger than being
careful.

> **The general lesson:** when you can't be sure your code is correct — and you can't —
> arrange things so its blast radius is small.

---

## Module map

| File | Responsibility |
|---|---|
| `hashing.py` | Content fingerprints |
| `mediatime.py` | Extract capture date from EXIF / QuickTime / filename |
| `catalog.py` | SQLite schema and queries. **No file I/O.** |
| `config.py` | Parse and validate `config.toml` |
| `replicas.py` | Storage drivers. **No knowledge of the catalog.** |
| `ingest.py` | Source scan → primary library |
| `sync.py` | Replication, reconciliation, catalog rebuild |
| `verify.py` | Scrubbing and repair |
| `health.py` | Redundancy assessment. **Pure computation.** |
| `cli.py` | Argument parsing and terminal output. **No logic.** |

Notice what each module is forbidden from doing. `catalog.py` never touches files;
`replicas.py` never touches the database; `health.py` only reads and computes; `cli.py`
holds no business logic at all.

These constraints are what make the code testable. `health.py` can be tested by handing
it a database with no files anywhere. `replicas.py` can be tested with no catalog. If
those concerns were tangled together, every test would need the entire world set up
first — which is exactly how test suites end up too painful to write, and then not
written.

> **The general lesson:** "separation of concerns" isn't about tidiness. It's about how
> much of the universe you must construct to test one thing.

---

## What's deliberately not built

- **No perceptual duplicate detection.** Finding *visually similar* photos (different
  resolutions, re-compressed copies) is a genuinely useful feature and a much harder
  problem. Exact-content dedup is the 90% case and is exactly correct.
- **No web UI.** The files are plain folders; Finder, Explorer and Apple Photos already
  browse them well.
- **No mobile replicas.** Explained in the README — iOS background execution limits
  make a phone an unreliable backup target.
- **No encryption at rest.** Worth adding before any cloud replica goes live. Not worth
  it for drives that never leave your house.

Knowing what you're *not* building, and being able to say why, is part of the design.
