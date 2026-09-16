# PhotoVault

A photo library that lives on **your** devices, keeps **three verified copies** of every
photo, and can prove it. No cloud required.

```
  iPhone ─┐
          ├─► ingest ─► Mac (primary) ─► sync ─┬─► External HDD  (offline copy)
  iPad  ──┘                                    └─► Windows laptop
```

---

## The idea in one paragraph

Every photo gets a **fingerprint** (a hash of its bytes). PhotoVault keeps a small
database — the *catalog* — that records every photo's fingerprint and which devices
are holding a copy. Backing up means "copy whatever a device is missing." Checking
your backups means "re-read the bytes and confirm they still match the fingerprint."
If a copy has gone bad, PhotoVault fetches a good one from another device and
overwrites it. That's the whole system.

---

## Quick start

```bash
python3 -m photovault setup     # interactive: pick your drives and storage model
python3 -m photovault           # start everything
```

`photovault` with no arguments is the same as `photovault start`: it checks what's
connected, brings up Immich if you've configured it, opens the web UI, and tells you
what to do next.

```
PhotoVault

Devices
  connected      mac (primary)
  connected      hdd-a
  not plugged in hdd-b

Library  184,302 photos - needs attention
  - 12,904 photos below 3 copies

Plug in hdd-b before syncing, or those copies will not be made.

Immich
  starting containers...
  ready at http://localhost:2283

Next
  1. plug in hdd-b
  2. photovault import       # pull from your devices and back up
  3. photovault reclaim      # what is safe to delete from the phone

PhotoVault UI running at http://127.0.0.1:8723/
```

The preflight leads with missing drives on purpose. A backup run with the offline drive
still in the drawer looks successful and quietly achieves less than you think, so the
one thing `start` cannot do for you is the first thing it says.

Flags: `--watch` also imports automatically as photos arrive, `--no-immich` skips the
containers, `--port` / `--no-browser` as you'd expect. `photovault stop` brings Immich
back down; `photovault doctor` prints just the preflight and exits non-zero if something
needs attention.

To write a config by hand instead:

```bash
python3 -m photovault init
```

Edit the config it writes (`~/.config/photovault/config.toml`), then:

```bash
python3 -m photovault scan
```

`scan` reads everything and changes nothing. It tells you how many photos you have,
how much space they take, and how much you'll need for three copies. **Always run
`scan` before `ingest`.**

```bash
python3 -m photovault ingest      # import into the Mac's primary library
python3 -m photovault sync --all  # fan out copies to the HDD and Windows laptop
python3 -m photovault status      # am I actually protected?
```

---

## The commands

| Command | What it does |
|---|---|
| `init` | Write a starter config file |
| `scan` | Dry run: report what *would* be imported. Changes nothing. |
| `ingest` | Copy new photos from your sources into the primary library |
| `sync --all` | Copy missing photos out to every backup device |
| `status` | Redundancy report — the one command to run regularly |
| `scrub` | Re-read stored files, detect corruption, repair it automatically |
| `scrub --force` | Re-verify everything now, ignoring the schedule |
| `reconcile --all` | Re-learn what each device actually holds |
| `rebuild <replica>` | Recover a lost catalog by re-reading a device |
| `restore <folder>` | Rebuild a complete library into a fresh folder |
| `replicas` | Which devices are configured and reachable right now |
| `adopt <replica>` | Re-register a genuinely replaced drive |
| `setup` | Interactive setup — pick full copies or sharding |
| `plan` | Preview how the library splits across drives |
| `rebalance` | Reclaim space on a shard after the drives changed |
| `watch` | Auto-import and back up as photos land in the inbox |
| `install-agent` | Run the watcher from login (macOS) |
| `import <device>` | Pull from a device, archive it, verify |
| `reclaim <device>` | What's provably safe to delete? |
| `start` | Start everything — Immich, the UI, optionally the watcher |
| `stop` | Stop the services `start` brought up |
| `doctor` | What's connected, what's missing, what to do next |
| `duplicates` | Find near-duplicates; `--apply --yes` to act on your review |
| `delete <photo>` | Move photos to the trash |
| `trash` | List, `--restore`, or `--purge` deleted photos |
| `backup` | Copy the catalog to every device; `--restore` to recover it |
| `log` | Recent operations |
| `ui` | Open the web interface in your browser |

Every command is safe to run twice. That property is called **idempotency**, and it's
why you can wire these into a schedule without worrying about double-imports.

---

## The web interface

```bash
python3 -m photovault ui
```

Opens `http://127.0.0.1:8723` in your browser. Three tabs:

- **Photos** — your library as a grid, with a year/month sidebar. Click any photo to
  see when it was taken, which devices hold it, and where it was imported from.
- **Health** — the four protection checks, a redundancy bar, and a per-device table.
- **Activity** — running and past operations, with live progress and any errors.

The **Import**, **Back up** and **Verify** buttons run the same operations as the CLI
commands, in the background, with a progress bar. They're disabled while something is
already running.

### It only listens to this Mac

The server binds to `127.0.0.1` — the loopback address, which only this machine can
reach. **Don't change that casually.** These endpoints can copy, overwrite and delete
files, and there's no password. Binding to `0.0.0.0` would hand anyone on your network
— or the coffee shop wifi — full control of your photo library.

There's a `--host` flag, and it prints a warning when you use it. If you genuinely want
the UI from your iPad, the safe route is an SSH tunnel, which keeps the server on
loopback and authenticates you properly:

```bash
ssh -L 8723:127.0.0.1:8723 you@your-mac
```

### Thumbnails

Full photos are 3–10 MB each; a grid of them would be a gigabyte of downloads to show
postage stamps. PhotoVault generates small copies once and caches them under
`~/.cache/photovault/thumbs`, keyed by content hash — so a cached thumbnail can never
go stale, because different bytes mean a different key.

It uses whichever tool it finds: **Pillow** (`pip install Pillow`, fastest and works
everywhere), **sips** (built into macOS, used automatically), or **ffmpeg** for video
poster frames. Files it can't thumbnail show their file type instead of a broken image.

---

## Using more than one external drive

Replicas are just a list — add as many as you like:

```toml
[[replica]]
name = "hdd1"
kind = "local"
root = "/Volumes/Backup1/PhotoVault/library"
offline = true

[[replica]]
name = "hdd2"
kind = "local"
root = "/Volumes/Backup2/PhotoVault/library"
offline = true
```

Two good reasons to:

- **More copies.** Four devices means four independent failures before you lose
  anything. Raise `min_copies` to match, or leave it at 3 and treat the fourth as
  slack.
- **Offsite rotation.** Keep one drive at a relative's house and swap them every few
  months. This is the single biggest upgrade to a home backup, because it survives
  fire, flood and theft — the failures that destroy every copy in one building at once.

### Drives are identified by a marker, not by their path

This matters more than it sounds. macOS assigns `/Volumes/<Name>` first-come: if two
drives are both called `Backup`, the second one mounts as `/Volumes/Backup 1` — **or as
`/Volumes/Backup`, if the first isn't plugged in.**

So with path-based identification, plugging in the wrong drive means PhotoVault reads
drive 2, records the contents against drive 1's name, and starts "repairing" the wrong
disk. All four health checks stay green. You'd only find out when you needed the backup.

PhotoVault therefore writes a `.photovault-id` file into each replica root containing a
random UUID, and checks it before **every** read or write:

```
$ photovault sync hdd1
hdd1
  WRONG DRIVE - nothing was written
  hdd1: this storage is stamped as replica 'hdd2', not 'hdd1'. Refusing to touch it.
```

It also refuses to act on a path with no marker once a drive has been registered,
because an unplugged drive leaves either nothing or an empty mount point — and claiming
that would quietly rebuild your whole library onto the internal SSD.

If you genuinely replace a dead drive, say so explicitly:

```bash
photovault adopt hdd2
photovault reconcile hdd2 && photovault sync hdd2
```

### Knowing which drive to plug in next

`photovault replicas` tracks when each one was last synced, and nags about offline
drives more than 30 days behind:

```
 replica     kind    offline  status        last synced   root
*mac         local   no       reachable     never         ~/PhotoVault/library
 hdd1        local   yes      unplugged     3d ago        /Volumes/Backup1/...
 hdd2        local   yes      unplugged     47d ago       /Volumes/Backup2/...

Plug in 'hdd2' next - it is 47 days behind.
```

---

## Choosing how drives store your photos

Run the guided setup — it detects your drives, asks what you want, shows the
consequence, and writes the config:

```bash
python3 -m photovault setup
```

There are two models, and the choice is a genuine trade rather than a right answer:

| | **Full copies** | **Sharded** |
|---|---|---|
| Each drive holds | everything | a computed subset |
| To restore you need | **any one drive** | **all the drives** |
| Drive size needed | as big as the library | combined, bigger than the library |
| Good when | drives are big enough | no single drive fits the library |

Full copies are safer and simpler — pick them unless your library genuinely doesn't fit.
The setup wizard recommends sharding only when it measures that it won't.

### Sharding

Mark drives as shards and give them a capacity:

```toml
[[replica]]
name = "hdd1"
kind = "local"
root = "/Volumes/Backup1/PhotoVault/library"
offline = true
mode = "shard"
capacity = "auto"      # or "500GB"
```

Your Mac stays a `full` replica — the primary has to be, because ingest needs somewhere
to write every new photo before it's distributed. The shards then supply whatever
redundancy is still missing, so `min_copies = 3` with one full replica means each photo
lands on two shards.

Preview before you commit:

```bash
python3 -m photovault plan
```

```
Library  184,302 photos, 847.1 GB
Target   3 copies (1 full replica + 2 from shards)

replica     mode         files       size   capacity  fill
hdd1        shard        61,203   281.4 GB   450.0 GB  ############........  62.5%
hdd2        shard        61,544   283.0 GB   450.0 GB  ############........  62.9%
hdd3        shard        61,555   282.7 GB   450.0 GB  ############........  62.8%
mac         full        184,302   847.1 GB     1.6 TB  ##########..........  51.7%

  OK   every photo fits with 3 copies
```

If it doesn't fit, `plan` says so and exits non-zero rather than silently leaving photos
underprotected.

### Adding or removing a drive

Placement uses **rendezvous hashing**, which means adding a fourth drive moves only about
a quarter of your photos rather than reshuffling everything. On a 1 TB library that's the
difference between hours and days.

```bash
python3 -m photovault sync --all        # copy to the new drive first
python3 -m photovault rebalance --all   # preview what is now surplus
python3 -m photovault rebalance --all --apply
```

**`rebalance` is the only command in PhotoVault that deletes a photo**, so it defaults to
a preview and re-reads `min_copies` other copies — hashing them, not trusting the
database — before removing anything. It also, by design, can only ever remove an
*over-replicated* copy: deletion requires `min_copies` to remain. That's why you sync
first and rebalance second.

### Recovery is different when sharded

No single drive is complete, so recovery must read them all:

```bash
python3 -m photovault rebuild --all
python3 -m photovault reconcile --all
```

Each sharded drive's `RECOVERY.md` says so in capitals, lists its sibling drives, and
tells whoever finds it not to mistake it for a full backup.

---

## Configuring from the UI

Everything below can be set in the browser instead of editing TOML:

```bash
python3 -m photovault ui     # → Settings tab
```

You get the vault rules (primary device, copies, offline requirement, scrub
interval), the device list, and the source list — with detected drives offered as
one-click chips so you don't type mount paths by hand.

Saving is safe by construction: the UI's config is rendered to TOML and **loaded back
through the same parser the CLI uses** before anything is written. A config the CLI
would reject can't be produced from the browser. The previous file is kept as
`config.toml.bak`, the write is atomic, and the change applies to the running server
with no restart.

Two deliberate refusals: saving is blocked while a job is running (swapping the config
mid-operation would have that job finish against replicas that no longer exist), and an
invalid config leaves the existing file untouched rather than half-written.

---

## Immich

If you want a proper mobile app — background upload on both iOS and Android, browsing,
search, and a "free up space" that clears the phone — run [Immich](https://immich.app)
alongside PhotoVault. See [immich/README.md](immich/README.md) for a pinned
`docker-compose.yml` and the setup.

The division of labour:

| | Immich | PhotoVault |
|---|---|---|
| Mobile upload, browse, search | ✅ | ✗ |
| Multi-drive replication | ✗ | ✅ |
| Bitrot detection and repair | ✗ | ✅ |
| Rebuildable catalog, drive identity | ✗ | ✅ |

Add Immich's originals folder as a source and PhotoVault archives everything it
ingests. **Leave `clear_after_import` off** — Immich owns those files and deleting them
behind its back corrupts its database.

---

## Freeing up phone storage safely

The hard part of "clear space on my phone" isn't copying files — it's knowing when it's
safe to delete. Once you delete from the phone, PhotoVault's copies are the only copies.

```bash
python3 -m photovault import android --reclaim
```

```
android (adb)
  pulling from Android (/sdcard/DCIM)...
  imported 412, 39 already known
  hdd1: copied 412
  hdd2: unavailable (not connected)
  win: copied 412

  389 files (4.9 GB) have 3 verified copies - safe to delete
  23 held back
    IMG_8821.jpg: 2 verified copies, need 3 (not connected: hdd2)
```

`reclaim` counts only copies it has **re-read and re-hashed right now**. A catalog row
saying `present` is a belief, and a belief isn't sufficient evidence for deleting
someone's only remaining file. Drives that weren't plugged in don't count — so you can
only free as much phone storage as you actually earned, which is what makes the ritual
*plug in both drives first*.

Use `photovault reclaim <device>` on its own to ask the question without importing.

### Android vs iOS

| | Path | Deletion |
|---|---|---|
| **Android** | `adb pull` — set `kind = "adb"` on the source | Automated, via `adb shell rm` |
| **iOS** | Image Capture or Immich → a folder | Manual, or through Immich's free-up-space |

`brew install android-platform-tools` for the Android path. iOS can't be automated
without macFUSE (a kernel extension needing reduced security on Apple Silicon), which
isn't worth it for a monthly task.

---

## Adding photos from the browser

For consolidating old folders — a drive full of scans, an exported album — without
wiring them up as a permanent source.

```bash
python3 -m photovault ui     # → Photos → Add photos
```

Pick a folder (sub-folders included) or drop one anywhere on the window. Files upload,
land in a staging area, and a **Waiting to be imported** panel appears with
`Import them` / `Discard`. Importing runs the ordinary pipeline: hash, dedup, date from
EXIF, replicate to every device, then clear staging.

Nothing special happens to an uploaded photo — a second import path would be a second
set of bugs, so there is only one. Re-upload a folder you already imported and it
reports `0 imported, N already in the library`.

**Staging is emptied only after replication.** It holds the only copy of a
just-uploaded photo until the replicas have it, so it is cleared on the same evidence
bar as everything else: `min_copies` copies, re-read and re-hashed.

### On filenames

The relative path of each file comes from the browser, and an HTTP client can claim any
path it likes. Every component is **rebuilt from a safe character set** rather than
cleaned — sanitising by removal is a game you lose to inputs like `....//`. Traversal,
absolute paths, UNC and drive-letter prefixes, and null bytes are refused outright; a
browser never sends them, so they are only ever a probe. Windows reserved names
(`CON`, `LPT1`) are escaped, and the resolved path is checked to be inside staging
before a byte is written.

Files stream to disk rather than being buffered, so a 500 MB video does not become
500 MB of server memory. A truncated upload is discarded rather than left as a partial
file — a half-received photo hashes as a different, corrupt asset, and PhotoVault would
then faithfully replicate that corruption everywhere.

---

## Backing up the catalog

For most of its life the catalog was purely derived data — lose it, run `rebuild` and
`reconcile`, get it back. **That is no longer entirely true.** It now also holds things
nothing else records:

- **duplicate review decisions** — which photo you chose to keep
- **trash state** — what you deleted, and when
- drive identities and last-synced times

None of that can be reconstructed by reading the files. It's judgement, not data, and
`rebuild` cannot recreate judgement.

```bash
python3 -m photovault backup
```

```
catalog-20260917-035340.db.gz  2.5 MB  sha256 49a9eb2ffdb32308
  copied      mac
  copied      hdd1
```

A compressed snapshot goes to `.photovault-backups/` on every reachable device, with a
`.sha256` beside it, keeping the newest `backup_keep` (default 10). `status` nags when
the newest backup is over two weeks old.

```bash
python3 -m photovault backup --list
python3 -m photovault backup --restore            # newest verified snapshot
python3 -m photovault backup --restore 20260917   # or one matching a name
```

Restoring **moves the current catalog aside** rather than overwriting it, so restoring
the wrong snapshot costs you a rename. A snapshot that fails its checksum is refused.

### Why not just copy catalog.db?

Because it doesn't work. The catalog runs in **WAL mode**, where committed data lives
partly in the `-wal` file. A plain `cp` of the main file can lose not merely recent rows
but entire tables:

```
plain cp of live.db      : UNUSABLE — no such table: t
sqlite3 backup API       : 5000 rows
```

`backup` uses SQLite's own backup API, which takes a consistent snapshot of a live
database while PhotoVault is still running.

### Why backups aren't stored as photos

Backups rotate; the library never forgets. Putting a fresh snapshot into a
content-addressed store every week means a brand-new asset each time — no dedup possible,
and unbounded growth. They live in their own tree on each replica instead: replicated and
hash-verified by the same machinery, but outside the catalog.

---

## Deleting photos

Every other removal in PhotoVault takes away a *redundant* copy and can be justified by
proving enough copies remain. Deleting a photo is different: you are asking for the
thing itself to go, and no check can tell "I meant it" from "I misclicked". So the guard
is **time**, not proof.

In the UI: **Photos → Select**, tick the photos, **Move to trash**. Or:

```bash
python3 -m photovault delete 2019/07          # a path fragment or hash prefix
```

Deleting **changes nothing on disk**. The photo leaves your library, the health report
and the duplicate scanner, but every copy stays intact and replicated on every device.
Restoring is a flag flip, not a recovery:

```bash
python3 -m photovault trash                   # what is in there
python3 -m photovault trash --restore 2019/07
```

### Emptying the trash

This is the point of no return.

```bash
python3 -m photovault trash --purge           # previews expired items
python3 -m photovault trash --purge --yes     # actually deletes them
python3 -m photovault trash --purge --all --yes   # empty it completely
```

Photos are kept for `trash_days` (default 30) before `--purge` will touch them.

**Purging refuses to run while any device is disconnected**, and that refusal is the
interesting one. Purging with the offline drive in a drawer would delete the file from
the devices you have, remove it from the catalog, and leave a copy stranded on the drive
you didn't — a photo that is neither in your library nor cleanly gone, which reappears
as an orphan the next time you rebuild the catalog from that drive.

---

## Removing duplicate photos

Byte-identical copies never need removing — content addressing collapses them at import,
so the same photo arriving from your phone, an old laptop and a backup drive is stored
once. What this handles is the messier kind: the same photo re-compressed by a messaging
app, exported at half resolution, or re-saved by an editor. Different bytes, same
picture.

```bash
python3 -m photovault duplicates
```

```
6 groups of near-duplicate photos, 487.1 KB recoverable

group 4ac11aaefab7  3 copies, 87.6 KB recoverable
  keep     640x480   453.5 KB  2 copies  2024-03-15  2024/03/20240315-101100_5e940fd072.png
  dup      320x240    81.7 KB  2 copies  2024-03-15  2024/03/20240315-101100_b2629e32e5.png
  dup      320x240     5.9 KB  2 copies  2024-03-15  2024/03/20240315-101100_4ac11aaefa.jpg
```

### Review them visually

```bash
python3 -m photovault ui     # → Duplicates tab
```

Each group shows its photos side by side. **Click the one you want to keep** and the
rest are marked for deletion; `Keep the best` accepts the suggestion, `Skip` leaves the
group alone. Your decisions are saved as you go, so you can review a few hundred groups
over several sittings.

Nothing is deleted until you press **Delete marked**.

### How it decides what's a duplicate

A **dHash**: the image is shrunk to 9×8 grey pixels and each adjacent pair is recorded as
"is the left one brighter?" — 64 bits describing the *shape* of the brightness gradient.
That survives re-compression and resizing, because it ignores pixel values. Two photos
are near-duplicates when fewer than 5 of those 64 bits differ (`--threshold` to change).

It is deliberately **not** a similarity search. Burst shots of the same scene from
slightly different angles will not collapse together, because the consequence here is
deletion and "these look similar" is not good enough grounds for it.

No dependency needed: it uses Pillow if installed, otherwise macOS's built-in `sips`.

### Three refusals

This is the only feature that deletes a photo you still want, so:

- **Nothing without a decision.** No auto-delete, ever. The suggested keeper is a
  suggestion.
- **Never the last of a group.** If everything in a group is marked, nothing is removed.
- **The keeper is re-verified first.** Before deleting a duplicate, the photo you kept is
  re-read and re-hashed on every device, and must reach `min_copies`. Deleting a
  duplicate because the catalog *claims* its twin is backed up — when that twin's only
  drive has silently rotted — would destroy the last good version.

Every deletion is written to the log with the photo that was kept in its place:

```
dup-delete  2024/03/…_5e940fd072.png (kept 2024/03/…_b2629e32e5.png)
```

---

## Setting up your four devices

### 1. Mac — the primary library

The primary is the one PhotoVault writes to during `ingest`. Everything else is a
copy of it.

```toml
[[replica]]
name = "mac"
kind = "local"
root = "~/PhotoVault/library"
```

### 2. External hard drive — the offline copy

Mark it `offline = true`. This tells PhotoVault two things: it's normal for this drive
to be unplugged (don't report an error), and it satisfies the "at least one offline
copy" safety rule.

```toml
[[replica]]
name = "hdd"
kind = "local"
root = "/Volumes/YourDriveName/PhotoVault/library"
offline = true
```

> **Why offline matters.** Ransomware, a buggy script, and `rm -rf` typed in the wrong
> window all destroy *connected* copies simultaneously. A drive sitting unplugged in a
> drawer is immune to every one of those. Three copies that are all always-connected is
> really one failure away from zero.

PhotoVault refuses to write to `/Volumes/Something` unless that path is a **real mount
point**. Without that check, backing up to an unplugged drive would quietly fill your
Mac's internal SSD while reporting success.

Format the drive **exFAT** if you want both the Mac and Windows to read it directly.

### 3. Windows laptop — over the network

This needs an SSH server on the Windows side. In an **Administrator** PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

Install [Git for Windows](https://git-scm.com/download/win) — it provides `rsync`,
`sha256sum` and a POSIX-style shell, which is what PhotoVault talks to. Then set the
default SSH shell to Git Bash:

```powershell
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Program Files\Git\bin\bash.exe" -PropertyType String -Force
```

Copy your Mac's SSH key over so no password is needed, then:

```toml
[[replica]]
name = "win"
kind = "rsync"
host = "yourname@192.168.1.50"
root = "/d/PhotoVault/library"      # Git Bash writes D:\ as /d/
```

Give the laptop a **static IP or DHCP reservation** in your router, or it'll move and
break the config.

### 4. iPhone and iPad — sources, not backups

Phones make poor backup replicas: limited storage, and iOS kills background sync. So
they feed photos *in* and read photos *out*, but they aren't counted as one of your
three copies.

**The honest constraint:** iOS does not allow a background process to copy your camera
roll to a Mac unattended. That's a platform restriction, not something a program can
work around. Truly automatic, phone-in-your-pocket sync requires iCloud Photos — which
is the cloud you said you don't want. Without it, getting photos *off* the phone always
involves a deliberate act: plugging in a cable, or opening an app.

Everything *after* that is automatic. Pick one of these:

#### Option A — cable + Image Capture (recommended, nothing to install)

`Image Capture` ships with macOS and can auto-import to a folder:

1. Plug in the iPhone, open **Image Capture**, select the device.
2. Set **Import To** → `~/PhotoVault/inbox/iphone`.
3. Tick **Delete after import** if you want the camera roll cleared.
4. Click the ⚙ and set **Connecting this iPhone opens: Image Capture**.

Now plugging in the cable copies new photos into the inbox. Nothing else to do.

#### Option B — Syncthing (wireless, no cloud)

Install [Syncthing](https://syncthing.net) on the Mac and **Möbius Sync** on the iPhone,
and share a folder pointed at `~/PhotoVault/inbox/iphone`. Peer-to-peer over your own
wifi, nothing leaves the house. iOS limits background time, so in practice it syncs
when you open the app and for a while afterwards — not continuously.

#### Option C — PhotoSync (wireless, paid, best background behaviour)

The [PhotoSync](https://www.photosync-app.com) iOS app transfers to an SMB or SFTP
target and has the most reliable background transfer of any non-cloud option. Point it
at a shared folder that maps to your inbox.

### Then let PhotoVault do the rest

```bash
python3 -m photovault watch
```

This watches the inbox folders and, whenever photos arrive, imports them and fans them
out to every backup device — no commands from you:

```
Watching 2 source folder(s) every 20.0s:
  iphone    ~/PhotoVault/inbox/iphone  [ok] (clears after import)
  ipad      ~/PhotoVault/inbox/ipad    [ok] (clears after import)

  iphone: 5 file(s) waiting
  iphone: imported 5
  iphone: cleared 5 from the inbox
  hdd: backed up 5
```

Make it run from login so you never think about it again:

```bash
python3 -m photovault install-agent
```

(macOS only; on Windows use Task Scheduler to run `photovault watch` at logon. Remove it
with `--uninstall`.)

**Two safety behaviours worth knowing about:**

*It waits for copying to finish.* A photo imported halfway would hash as a different,
corrupt asset — and PhotoVault would then faithfully replicate that corruption to every
drive. So the watcher waits until the folder stops changing before touching anything.

*It empties inboxes only after verifying.* `clear_after_import = true` deletes an inbox
file once the library copy has been **re-read and re-hashed** — not merely recorded in
the catalog. This deletes originals, so a catalog row is not good enough evidence.
Clearing is opt-in per source, so a folder you also browse (your Apple Photos library)
is never touched:

```toml
[[source]]
device = "iphone"
path = "~/PhotoVault/inbox/iphone"
clear_after_import = true      # an inbox: empty it once safely stored

[[source]]
device = "mac"
path = "~/Pictures/Photos Library.photoslibrary/originals"
                               # no flag: read-only, never modified
```

---

## How photos are stored

```
library/2024/03/20240315-143022_a1b2c3d4e5.jpg
        │    │  │               └─ first 10 characters of the content hash
        │    │  └─ capture date and time, from EXIF
        └────┴─ year / month folders
```

Two deliberate choices:

**Plain folders and original filenames-by-date.** If PhotoVault disappeared tomorrow,
your photos are still just files in dated folders. Any tool can read them. A backup
format only you can open is a liability.

**The hash in the filename.** The same photo always lands at the same path on every
device, no matter what order things were imported in. That's what lets PhotoVault
compare devices by listing filenames instead of re-hashing terabytes.

---

## Your routine

**Continuously**, if you installed the watcher — plug in the phone and it handles
itself. Otherwise, whenever you've taken photos:
```bash
python3 -m photovault ingest && python3 -m photovault sync --all
```

**Monthly** — plug in the external drive:
```bash
python3 -m photovault sync hdd
python3 -m photovault scrub
python3 -m photovault status
```

**`status` is the one to actually read.** It ends with four checks:

```
  OK   single copy        no photo exists in only one place
  OK   redundancy         every photo has at least 3 copies
  OK   offline copy       every photo exists on an unplugged drive
  OK   integrity          no copy has failed a hash check
```

If all four are green, you are genuinely protected. `status` exits with code `2` when
they aren't, so you can wire it into an alert.

---

## Disaster recovery

| What broke | What to do |
|---|---|
| A file got corrupted | `scrub` finds and repairs it automatically |
| The catalog database is lost | `rebuild hdd` then `reconcile --all` — recovers everything from the files themselves |
| The Mac's drive died | `reconcile mac` then `sync mac` — refills from the surviving copies |
| You need a copy elsewhere | `restore /path/to/folder` |

Every one of these is covered by a test in `tests/test_lifecycle.py`.

---

## Adding Google Cloud Storage later

Cloud is treated as just another replica. `photovault/replicas.py` defines a `Driver`
with five methods — `available`, `list_present`, `put`, `hash_of`, `get`. `GCSDriver`
is a stub with those five methods raising `NotImplementedError`. Fill them in with
`google-cloud-storage` calls, add a `kind = "gcs"` replica to the config, and the
scheduler, health report and scrubber all start covering it with no other changes.

That separation is the point: *nothing above the driver layer knows what a replica is
made of.*

---

## Running the tests

```bash
PYTHONPATH="$PWD:$PWD/tests" python3 -m unittest discover -s tests -v
```

164 tests covering ingest, deduplication, replication, corruption repair, catalog
rebuild, total loss of the primary device, the HTTP API, background jobs, and
path-traversal defence, multi-drive identity safety, sharded placement, the delete path, inbox watching, config round-tripping, reclaim safety, launcher preflight, duplicate review, upload path safety, the trash lifecycle, and catalog backup and restore.

---

## Requirements

Python 3.11+ and `rsync` (both already on macOS). No other dependencies — the web
UI is plain HTML, CSS and JavaScript served by Python's standard library, with no
build step and nothing to install. Optionally
`pip install blake3` for roughly 5x faster hashing on large libraries — PhotoVault
detects it automatically.
