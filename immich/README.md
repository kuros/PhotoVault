# Immich + PhotoVault

Immich is the browsing layer: mobile apps with background upload, search, faces,
albums. PhotoVault is the archival layer: replication across drives, integrity
scrubbing, disaster recovery. They do not overlap — Immich has no multi-drive
replication or bitrot detection, and its own backup story is "back this up yourself".

## The important bit: your existing library is not copied

Your 57 GB archive stays exactly where it is. Immich indexes it as a **read-only
External Library**, so there is no second copy and Immich physically cannot delete
your photos — the `:ro` on the mount makes that impossible, not merely discouraged.

```
~/PhotoVault/library         ← 57 GB, PhotoVault owns it, Immich reads it
~/PhotoVault-immich/upload   ← thumbnails + new phone uploads (small)
~/PhotoVault-immich/db       ← Postgres
```

Keep `db` on an always-available disk. Immich cannot start without its database, so
never put it on a drive you unplug.

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

## 5. Install the mobile apps

Immich for [iOS](https://apps.apple.com/app/id1613945652) and
[Android](https://play.google.com/store/apps/details?id=app.alextran.immich). Point
them at your Mac's LAN address, e.g. `http://192.168.1.x:2283`.

Set backup to **manual/selective** rather than automatic — you want uploads when you
decide to sync, not continuously.

## 6. Tell PhotoVault about Immich's uploads

New photos from your phone land in Immich's own storage, not your archive. Add it as
a PhotoVault source — in the UI under **Settings → Sources**, or in `config.toml`:

```toml
[[source]]
device = "immich"
path = "~/PhotoVault-immich/upload/library"
```

Leave `clear_after_import` **off**. Immich owns those files and deleting them behind
its back corrupts its database; you free space through Immich itself.

---

## The monthly routine

```bash
# plug in the backup drive, open Immich on your phones, upload
cd immich && docker compose up -d
photovault import            # pull new photos in, replicate everywhere
photovault status            # must show four OK lines
photovault backup            # snapshot the catalog to every device
photovault reclaim immich    # what is provably archived?
# only now: Immich app → Utilities → Free up space
```

**The order matters.** Immich's "free up space" deletes from your phone once an asset
reaches the Immich *server* — one copy, on one drive, unscrubbed. `photovault reclaim`
tells you which assets have `min_copies` copies that were re-read and re-hashed, so
step 5 is gated on evidence rather than assumption.

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
