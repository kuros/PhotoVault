# Immich + PhotoVault

Immich is the browsing layer: mobile apps with background upload, search, faces,
albums. PhotoVault is the archival layer: replication across drives, integrity
scrubbing, disaster recovery. They do not overlap — Immich has no multi-drive
replication or bitrot detection, and its own backup story is "back this up yourself".

## One folder, one writer

Both systems share a single photo tree. The rule that makes that safe is that
**exactly one of them writes to it**:

```
              ~/PhotoVault/library          ← the only photo tree
                   ↑ writes                      ↓ reads (:ro)
               PhotoVault                     Immich (External Library)
```

Immich indexes your library in place as a read-only External Library. There is no
second copy, no hardlinks, and the `:ro` mount means Immich physically *cannot*
delete your archive — a property, not a policy.

```
~/PhotoVault/library         ← every photo, PhotoVault owns it
~/PhotoVault-immich/upload   ← thumbnails only (see below)
~/PhotoVault-immich/db       ← Postgres
```

Keep `db` on an always-available disk. Immich cannot start without its database, so
never put it on a drive you unplug.

### Do not upload through the Immich mobile app

This is the one thing that breaks the single-copy property. Managed uploads land in
Immich's own storage, and External Libraries are read-only by design, so you cannot
upload *into* one. A photo sent through the app would exist twice: once in Immich's
upload tree and again in your library after PhotoVault imported it.

Bring phone photos in through PhotoVault instead — Image Capture on iOS, `adb` on
Android, or any folder you point a source at. Immich's nightly external scan picks
them up.

```
iPhone  ──Image Capture──┐
Android ──adb────────────┤→ inbox → photovault import → ~/PhotoVault/library
                                                              ↓
                                          Immich external scan → browse, search, faces
```

You give up the Immich app's uploader. You do **not** give up freeing space on your
phone: `photovault reclaim` does that job on stricter evidence than Immich's "free up
space", which releases a photo once it reaches the Immich server — one copy, on one
drive, never scrubbed.

### Why not the other way round

Pointing PhotoVault's primary at Immich's managed library would put two writers on one
tree. `reconcile` would treat files Immich deleted as missing and push them back,
fighting Immich's own deletions, while `ingest` dropped PhotoVault-named files into a
directory Immich's database believes it knows the contents of. That is the
configuration the `:ro` mount exists to prevent.

---

## 1. Set a database password

```bash
cd immich
$EDITOR .env      # replace CHANGE_THIS_BEFORE_STARTING
```

## 2. Start it

```bash
docker compose up -d
```

First run pulls several GB and initialises Postgres — give it a few minutes. Watch
with `docker compose logs -f immich-server`.

## 3. Create your account

Open <http://localhost:2283>. The first account created is the admin.

## 4. Add your library as an External Library

**Administration → External Libraries → Create Library**, then under *Folders* add:

```
/mnt/photovault
```

**That is the path inside the container, not on your Mac.** Immich sees your
`~/PhotoVault/library` mounted there. Entering the host path will silently find
nothing.

Then **Scan** it. Immich reads EXIF, builds thumbnails and runs face detection —
expect a while for 7,875 photos, and the ML container will use significant CPU.

## 5. Install the mobile apps — for viewing

Immich for [iOS](https://apps.apple.com/app/id1613945652) and
[Android](https://play.google.com/store/apps/details?id=app.alextran.immich). Point
them at your Mac's LAN address, e.g. `http://192.168.1.x:2283`.

**Leave backup switched off.** See "Do not upload through the Immich mobile app"
above — uploading is what would give you two copies of every new photo. Use the apps
to browse and search your archive from the sofa.

## 6. Bring new photos in through PhotoVault

```bash
photovault import          # from your configured sources
photovault reclaim         # what is provably archived and safe to delete
```

Then trigger an Immich scan (Administration → External Libraries → Scan) or wait for
the nightly job, and the new photos appear in Immich too.

---

## The monthly routine

```bash
# plug in the backup drive; copy photos off the phone (Image Capture / adb)
cd immich && docker compose up -d
photovault import            # pull new photos in, replicate everywhere
photovault status            # must show four OK lines
photovault backup            # snapshot the catalog to every device
photovault reclaim           # what is provably archived and safe to delete
# Immich: Administration → External Libraries → Scan (or wait for the nightly job)
```

**The order matters.** Nothing leaves your phone until `photovault reclaim` confirms
`min_copies` copies that were re-read and re-hashed. That is the whole point of doing
the import through PhotoVault rather than through Immich.

---

## Things worth knowing

**Pin the version.** Immich ships roughly every 3–4 days. `IMMICH_VERSION` is pinned
in `.env` on purpose — update deliberately, after reading the release notes for
migrations. You are running an archive, not a lab.

**Deleting in Immich does not delete your archive.** With `:ro`, external assets
cannot be removed from disk through Immich. If you delete a photo from PhotoVault,
Immich moves its entry to trash on the next rescan.

**Albums live only in Postgres.** They are the one thing here with no backup story
yet — see the album-manifest discussion. Until that is built, `docker compose down`
plus a copy of `~/PhotoVault-immich/db` is your only protection, and it must be done
with the stack stopped.

**Stopping it:**

```bash
cd immich && docker compose down
```
