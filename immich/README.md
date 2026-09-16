# Immich + PhotoVault

## Setup

```bash
cp .env.example .env     # edit UPLOAD_LOCATION and DB_PASSWORD
docker compose up -d
```

Open http://localhost:2283, create your account, then install the Immich app on
your phones and point it at your Mac's LAN address.

Set the mobile app's backup to **manual/selective** rather than automatic — you
want uploads to happen when you decide to sync, not continuously.

## Tell PhotoVault about it

Add Immich's originals directory as a source, either in the Settings tab of the
PhotoVault UI or in `config.toml`:

```toml
[[source]]
device = "immich"
path = "/Volumes/Photos/immich/library"
```

Leave `clear_after_import` **off**. Immich owns those files, and deleting them
behind its back corrupts its database. You free up space through Immich itself.

## The monthly ritual

```bash
# 1. plug in the backup drives
# 2. open Immich on the phones and upload
docker compose up -d
photovault import            # ingest from Immich, replicate everywhere
photovault status            # must show four OK lines
photovault reclaim immich    # how much is provably archived?
# 3. only now: Immich app -> Utilities -> Free up space
```

**The ordering is the whole point.** Immich's "free up space" deletes from the
phone once an asset reaches the Immich *server* — that is one copy, on one
drive, unscrubbed. `photovault reclaim` tells you which assets have
`min_copies` copies that were re-read and re-hashed, so step 3 is gated on
evidence rather than assumption.

## Storage

Immich and PhotoVault each keep their own copy, so a disk holding both carries
the library twice. That is deliberate: it keeps deletion out of the pipeline
entirely, and a 4 TB drive is cheaper than the failure mode where two systems
write the same tree.
