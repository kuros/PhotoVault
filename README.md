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
| `log` | Recent operations |

Every command is safe to run twice. That property is called **idempotency**, and it's
why you can wire these into a schedule without worrying about double-imports.

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

19 tests covering ingest, deduplication, replication, corruption repair, catalog
rebuild, and total loss of the primary device.

---

## Requirements

Python 3.11+ and `rsync` (both already on macOS). No other dependencies. Optionally
`pip install blake3` for roughly 5x faster hashing on large libraries — PhotoVault
detects it automatically.
