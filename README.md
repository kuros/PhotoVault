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

Phones make poor backup replicas: limited storage, and iOS aggressively kills
background sync. So they feed photos *in* and read photos *out*, but they aren't
counted as one of your three copies.

The simplest reliable path is a plain cable import into an inbox folder:

```toml
[[source]]
device = "iphone"
path = "~/PhotoVault/inbox/iphone"
```

Use macOS **Image Capture** (built in) to pull the camera roll into that folder, then
run `ingest`. For hands-off syncing, install [Syncthing](https://syncthing.net) with
the *Möbius Sync* iOS app pointed at the same inbox. PhotoVault never modifies a
source folder, so it's safe to point it at a folder another tool is managing.

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

**Weekly** (or whenever you've taken photos):
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

59 tests covering ingest, deduplication, replication, corruption repair, catalog
rebuild, total loss of the primary device, the HTTP API, background jobs, and
path-traversal defence, multi-drive identity safety, sharded placement, and the delete path.

---

## Requirements

Python 3.11+ and `rsync` (both already on macOS). No other dependencies — the web
UI is plain HTML, CSS and JavaScript served by Python's standard library, with no
build step and nothing to install. Optionally
`pip install blake3` for roughly 5x faster hashing on large libraries — PhotoVault
detects it automatically.
