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

- **No web UI.** The files are plain folders; Finder, Explorer and Apple Photos already
  browse them well.
- **No mobile replicas.** Explained in the README — iOS background execution limits
  make a phone an unreliable backup target.
- **No encryption at rest.** Worth adding before any cloud replica goes live. Not worth
  it for drives that never leave your house.

Knowing what you're *not* building, and being able to say why, is part of the design.

---

## Decision 7: the UI is a client of the same core

`cli.py` contains no business logic — check it yourself:

```bash
grep -cE 'hash_file|rglob|\.put\(|sqlite3' photovault/cli.py   # 0
```

That discipline is what made the web UI cheap. `photovault/web/server.py` calls the
exact same `ingest`, `sync`, `verify` and `health` modules the CLI calls. There is no
duplicated logic, so the two interfaces cannot drift apart or disagree about whether
your photos are safe.

If instead the ingest logic had been written *inside* the CLI command — which is the
path of least resistance — the UI would have meant either rewriting it or refactoring
first. **The layering wasn't extra work done for its own sake; it was the thing that
made the second interface a few hundred lines instead of a rewrite.**

### Jobs run in the background, and the browser polls

Importing 400 GB takes hours. An HTTP request that waits for it would time out, so
`POST /api/jobs` *starts* work and returns immediately; the browser polls
`GET /api/jobs` once a second for progress.

Polling gets criticised versus WebSockets, but for one user on localhost it's a few
bytes a second, and it removes an entire category of work: reconnection, backpressure,
and connection lifecycle. *Choose the boring mechanism until measurement says you
can't.*

Two concurrency rules keep it safe:

- **One mutating job at a time.** The work is disk-bound, so running ingest and sync
  together would contend for the same catalog rows with no speedup.
- **Every thread opens its own SQLite connection.** Connections belong to the thread
  that created them; sharing one across threads produces intermittent corruption that
  is miserable to debug. Thread-confinement costs nothing and deletes the whole
  problem class.

### Binding to loopback is a security decision, not a default

The server listens on `127.0.0.1`. It has no authentication and its endpoints can copy,
overwrite and delete files — so `0.0.0.0` would expose the library to everyone on the
network. The `--host` flag exists and prints a warning, because the right answer to
"I want this on my iPad" is an SSH tunnel, not an open port.

Static file serving normalises the request path and confirms the resolved file is
actually inside the static directory, so `/static/../../../etc/passwd` returns 404.
There is a test for it, including URL-encoded variants.

### Three bugs the browser caught that reading the code did not

Worth recording, because all three are invisible until you look at the rendered page:

1. **The photo viewer was open on page load.** `<div hidden>` is defeated by any author
   rule setting `display` — `[hidden]` is only a user-agent style, and author styles
   win. Fix: `[hidden] { display: none !important; }`.
2. **Thumbnails loaded but were invisible**, covered by the absolutely-positioned
   fallback label meant to show only when there's no image. Fix: hide the fallback once
   the image decodes.
3. **The redundancy bar rendered as nothing.** It was a `<span>`, and inline elements
   ignore `width` and `height` entirely. The tell was `getComputedStyle().width`
   returning the literal string `"100%"` instead of a pixel value — a laid-out element
   always resolves to pixels. Fix: `display: block`.

> **The general lesson:** code that produces a visual result cannot be verified by
> reading it. Run it and look.


---

## Decision 8: storage proves its own identity

With a single external drive, the filesystem path is a perfectly good name for it. Add a
second and that stops being true — and it stops being true *silently*, which is the
dangerous kind.

macOS hands out `/Volumes/<Name>` on a first-come basis. Two drives both named `Backup`:
whichever mounts first gets `/Volumes/Backup`, and the other gets `/Volumes/Backup 1`.
Plug in only the second one, and it takes the first one's path.

I demonstrated the consequence before fixing it, which is worth doing whenever you think
you have found a bug — a bug you cannot reproduce is a theory. PhotoVault read drive 2,
recorded its contents as drive 1's, decided drive 1 had "lost" six photos, and wrote
them onto drive 2. Then printed four green checks.

The fix is to stop trusting the path. Every replica root carries `.photovault-id` with a
random UUID, and the catalog remembers which UUID belongs to which replica. Every read
or write checks it first.

> **The general lesson:** an identifier controlled by the environment (a path, a mount
> point, a hostname, a port) is not an identity. When something must be *itself* across
> disconnections and reboots, give it an identifier it carries with it. This is the same
> instinct as [Decision 1](#decision-1-content-addressing) — identify things by
> something intrinsic — applied one level up, to devices rather than files.

### Two fixes the drill forced that the first implementation missed

Writing the check was the easy part. Running the scenario found two holes in it:

1. **Auto-claiming unmarked storage was too eager.** An unplugged drive leaves either
   nothing or an empty mount point. The first version happily stamped that as the
   replica and "restored" the library into it — recreating the exact boot-disk-fill
   failure the mount guard already defends against, through a new door. Claiming is now
   allowed only on genuine first use, when no UUID is yet on record.

2. **`ensure_root()` ran before the identity check**, so the directory for an absent
   drive was created before anything could object. Ordering is part of the guarantee,
   not an implementation detail: a check that runs after the side effect is not a check.

> **The general lesson:** a safety check has to be tested against the situation it
> guards, not just reviewed for plausibility. Both holes were invisible when reading
> the code and obvious within seconds of running the scenario.


---

## Decision 9: sharding is a second model, not a replacement

When a library outgrows any single drive, the drives have to hold subsets. That breaks
the property that made recovery so simple — *any one drive restores everything* — so
sharding was added **beside** full copies rather than instead of them, and the setup
wizard recommends it only when it measures that full copies will not fit.

> **The general lesson:** when a new requirement conflicts with a property you already
> promised, the choice is not "which is better" but "who decides". Both modes exist
> because the answer depends on hardware the program cannot see.

### Rendezvous hashing, and why not modulo

The obvious placement is `hash(photo) % drive_count`. It is balanced, deterministic and
one line long. It is also close to unusable, because adding a fourth drive changes the
answer for nearly every photo — on a 1 TB library, days of copying to add one drive.

Weighted rendezvous hashing scores every drive from the photo's hash plus the drive's
name and takes the highest scorers. Same determinism, same balance, but adding an Nth
drive moves only about 1/N of the photos. There is a test asserting exactly that, because
it is the whole reason for the more complicated algorithm:

```python
def test_adding_a_drive_moves_only_a_fraction(self):
```

> **The general lesson:** "deterministic and balanced" is table stakes. The property
> worth paying complexity for is what happens *when the inputs change* — and that is the
> one the obvious implementation gets wrong.

### Deleting is the one thing that needs paranoia

`rebalance` is the only operation in PhotoVault that removes a photo, and it makes three
concessions the rest of the program does not:

1. **It defaults to a dry run.** You have to pass `--apply`.
2. **It re-reads and re-hashes surviving copies** rather than trusting the catalog. A
   placement row saying `present` is a belief; deleting a photo because of a stale belief
   is exactly the failure this program exists to prevent.
3. **It can only remove an over-replicated copy.** Deletion requires `min_copies` to
   *remain*, so with exactly `min_copies` there is nothing to give up.

Point 3 emerged from a failing test. I had written a test asserting rebalance would
refuse an unsafe delete, set it up with exactly `min_copies` copies, and it reported
nothing to delete at all. My first instinct was that the test setup was wrong — it was —
but working out *why* surfaced the real invariant, which is now documented in the
workflow: sync first, rebalance second.

> **The general lesson:** when a test fails for a reason you did not predict, the
> explanation is worth more than the fix. This one turned an accident of the
> implementation into a stated guarantee.

---

## Decision 10: the UI writes the same file the CLI reads

The Settings tab edits `config.toml` directly rather than keeping its own settings store.
One file, one parser, no drift — the alternative is a UI database that silently disagrees
with what the CLI loads, and you find out during a restore.

That makes a bad write an outage, so `config.save()` renders to TOML and **loads it back
through the ordinary `load()` path** before touching the real file. The UI cannot produce
a config the CLI would reject, because the CLI's own parser is the validator. Then: keep
a `.bak`, write to a temp file, `rename()` into place. The same atomic-write pattern as
`LocalDriver.put` — a truncated config is as fatal as a half-copied photo.

Two refusals worth noting, both about ordering rather than validity:

- **No saving while a job runs.** A sync that started against three replicas should not
  finish against two.
- **A rejected config leaves the old file untouched**, rather than being half-applied.

> **The general lesson:** when two interfaces share state, make one of them the format of
> record and validate through the real consumer. A second source of truth that "should"
> stay in sync is a bug with a delay on it.

---

## Decision 11: evidence, not belief, gates deletion

Three operations now delete data — `rebalance`, inbox clearing, and `reclaim` — and all
three answer the same question: *is it safe to remove this copy?* They share one rule:

**Count only copies that have been re-read and re-hashed right now.** A `placement` row
saying `present` is a belief formed at some point in the past. Deleting the last good
copy because of a stale belief is precisely the failure this program exists to prevent.

This has a consequence users feel directly, and it is the correct one: **you can only
free as much phone storage as you actually earned.** If you didn't plug in the offline
drive, those photos have two verified copies rather than three, and `reclaim` holds them
back. The tool doesn't let you spend redundancy you don't have.

### An ordering bug this discipline exposed

Inbox clearing originally verified only the primary copy. Tightening it to the shared
`min_copies` rule immediately broke the watcher — and the failure was *correct*:

```
run_once():  ingest → clear → replicate      # clear ran before the evidence existed
```

At clear time only the primary held a copy, so a rule demanding three could never be
satisfied and everything was held back. The fix is the obvious reordering, but the bug
was invisible under the weaker rule, because with a one-copy bar the ordering didn't
matter.

> **The general lesson:** strengthening a check often reveals that some *sequence* was
> only ever correct by accident. The test that caught this was asserting a behaviour
> ("the inbox empties"), not an implementation — which is why it noticed.


---

## Decision 12: the launcher reports what it cannot do

`photovault start` brings up Immich, the web UI and optionally the watcher. The
mechanical part is easy. The part worth designing is the preflight, and it exists
because of a specific failure mode:

**A backup run with the offline drive still in a drawer looks exactly like a successful
one.** Every command exits zero, the log says files were copied, and you have achieved
two copies instead of three without being told. So the first thing `start` prints is
which drives are missing — before any service starts, above the library summary.

It also distinguishes two states that a naive implementation would collapse:

- an **offline** replica that is absent is *expected* — it lives unplugged by design
- an **online** replica that is absent is a *fault* — something is broken

Reporting both as "unreachable" would train you to ignore the line that matters.
`Preflight.ready` is therefore defined as "no *online* device missing", not "everything
present".

Two smaller decisions that came out of running it rather than writing it:

- **A failed Immich does not block the UI.** When the container start failed (registry
  unreachable), the right behaviour was to print the error and carry on serving — the
  archive does not depend on Immich, and a photo manager that refuses to open because an
  optional service is down has its priorities backwards.
- **stdout is set line-buffered.** Python block-buffers when stdout is not a terminal, so
  under `nohup` or launchd the entire preflight sat invisible in a buffer while the user
  wondered whether anything had started. Caught by running it under a log file, which is
  how it will actually be run.

> **The general lesson:** a status display earns its place by what it makes *impossible
> to miss*, not by how much it shows. The redundancy numbers were already available from
> `status`; what was missing was putting the one actionable line where a human could not
> scroll past it.


---

## Decision 13: two kinds of "same photo"

Content hashing answers *are these the same bytes?* and collapses exact copies at ingest
for free. It says nothing about a photo re-compressed by a messaging app — different
bytes, and to a content hash, an unrelated file.

dHash answers the other question. Shrink to 9×8 grey pixels, record for each adjacent
pair whether the left is brighter, and you have 64 bits describing the *shape* of the
brightness gradient. Re-compression and resizing barely touch it because it encodes no
pixel values at all.

The two hashes are not competitors. Content hashing is exact and load-bearing —
identity, integrity, repair all rest on it. Perceptual hashing is a fuzzy *hint*, used
only to put candidates in front of a human. Keeping that distinction sharp is what makes
the feature safe: nothing in the replication or verification path ever consults a dHash.

> **The general lesson:** when you add a second notion of identity to a system, be
> explicit about which one is authoritative. A fuzzy match that quietly leaks into the
> exact-match paths is how "find similar photos" becomes "lost photos".

### Scaling the comparison

Comparing every pair is O(n²) — about 20 billion comparisons for 200k photos. Instead
each hash is split into 8 bands of 8 bits and bucketed by band. By pigeonhole, two hashes
differing in at most 7 bits must agree *exactly* on at least one band, so only same-bucket
pairs need checking. Exact for the thresholds in use, and linear in practice.

Buckets with hundreds of members are skipped: that is a near-uniform image class, not a
duplicate set, and clustering it would propose mass deletion. Hashes that are nearly all
zeros or all ones are excluded for the same reason — a blank wall matches every other
blank wall perfectly while having nothing in common with it.

### Deletion is gated on re-read bytes, like everything else

`duplicates apply` is the fourth destructive operation, and it reuses the rule the other
three established: **count only copies re-read and re-hashed right now.** Before removing
a duplicate, the photo being kept must reach `min_copies` verified copies.

The failure this prevents is specific and quiet: you delete a duplicate because the
catalog says its twin is safely backed up, but that twin's only drive has silently
rotted. You have then destroyed the last good version of that photo, and the catalog
still claims everything is fine.

Two further refusals, both about the user's intent rather than the data:

- **Nothing is deleted without an explicit per-group decision.** The suggested keeper is
  only ever a suggestion; it is never acted on by itself.
- **A group is never emptied.** If every member is marked for deletion, none are.

> **The general lesson:** a destructive feature's design is mostly its refusals. The
> deletion itself is three lines.
