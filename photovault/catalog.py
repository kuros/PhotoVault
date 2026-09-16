"""The catalog: the single source of truth about what exists and where it lives.

The catalog is deliberately rebuildable. Every fact in it can be recovered by
re-scanning the replicas, so losing catalog.db costs time, never photos.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per unique piece of content. The hash is the identity; the same
-- photo arriving from the phone, a laptop and an old backup collapses here.
CREATE TABLE IF NOT EXISTS asset (
    hash        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    ext         TEXT NOT NULL,
    media_kind  TEXT NOT NULL,
    captured_at TEXT,
    time_source TEXT NOT NULL,
    rel_path    TEXT NOT NULL UNIQUE,
    added_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_captured ON asset(captured_at);

-- Audit trail of every original sighting, so you can answer "where did this
-- come from?" and safely delete a source folder once it is fully ingested.
CREATE TABLE IF NOT EXISTS source_file (
    id       INTEGER PRIMARY KEY,
    hash     TEXT NOT NULL REFERENCES asset(hash) ON DELETE CASCADE,
    device   TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    UNIQUE(device, abs_path)
);
CREATE INDEX IF NOT EXISTS idx_source_hash ON source_file(hash);

CREATE TABLE IF NOT EXISTS replica (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    root       TEXT NOT NULL,
    host       TEXT,
    is_offline INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    added_at   TEXT NOT NULL
);

-- What each replica is believed to hold. 'present' is a claim; verified_at
-- says when that claim was last backed by actually reading the bytes.
CREATE TABLE IF NOT EXISTS placement (
    hash        TEXT NOT NULL REFERENCES asset(hash) ON DELETE CASCADE,
    replica     TEXT NOT NULL REFERENCES replica(name) ON DELETE CASCADE,
    state       TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (hash, replica)
);
CREATE INDEX IF NOT EXISTS idx_placement_replica ON placement(replica, state);
CREATE INDEX IF NOT EXISTS idx_placement_verified ON placement(verified_at);

CREATE TABLE IF NOT EXISTS event (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Catalog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # ----------------------------------------------------------------- assets

    def has_asset(self, hash_: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM asset WHERE hash = ?", (hash_,)
        ).fetchone() is not None

    def add_asset(self, *, hash_, size, ext, media_kind, captured_at,
                  time_source, rel_path) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO asset
               (hash, size, ext, media_kind, captured_at, time_source, rel_path, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (hash_, size, ext, media_kind, captured_at, time_source, rel_path, now()),
        )

    def rel_path_taken(self, rel_path: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM asset WHERE rel_path = ?", (rel_path,)
        ).fetchone() is not None

    def asset(self, hash_: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM asset WHERE hash = ?", (hash_,)).fetchone()

    def all_assets(self):
        return self.db.execute("SELECT * FROM asset ORDER BY captured_at").fetchall()

    def record_source(self, hash_: str, device: str, abs_path: str) -> None:
        self.db.execute(
            """INSERT INTO source_file(hash, device, abs_path, seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(device, abs_path) DO UPDATE SET hash=excluded.hash,
                                                           seen_at=excluded.seen_at""",
            (hash_, device, abs_path, now()),
        )

    # --------------------------------------------------------------- replicas

    def upsert_replica(self, name, kind, root, host=None, is_offline=False) -> None:
        self.db.execute(
            """INSERT INTO replica(name, kind, root, host, is_offline, enabled, added_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, root=excluded.root,
                                               host=excluded.host,
                                               is_offline=excluded.is_offline""",
            (name, kind, root, host, int(is_offline), now()),
        )
        self.db.commit()

    def replicas(self, enabled_only: bool = True):
        q = "SELECT * FROM replica"
        if enabled_only:
            q += " WHERE enabled = 1"
        return self.db.execute(q + " ORDER BY name").fetchall()

    def replica(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM replica WHERE name = ?", (name,)).fetchone()

    # -------------------------------------------------------------- placement

    def set_placement(self, hash_: str, replica: str, state: str,
                      verified: bool = False) -> None:
        self.db.execute(
            """INSERT INTO placement(hash, replica, state, verified_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(hash, replica) DO UPDATE SET
                   state = excluded.state,
                   verified_at = COALESCE(excluded.verified_at, placement.verified_at)""",
            (hash_, replica, state, now() if verified else None),
        )

    def placements(self, hash_: str):
        return self.db.execute(
            "SELECT * FROM placement WHERE hash = ?", (hash_,)
        ).fetchall()

    def missing_on(self, replica: str):
        """Assets the catalog knows about that this replica does not hold."""
        return self.db.execute(
            """SELECT a.* FROM asset a
               LEFT JOIN placement p ON p.hash = a.hash AND p.replica = ?
               WHERE p.hash IS NULL OR p.state != 'present'
               ORDER BY a.captured_at""",
            (replica,),
        ).fetchall()

    def log(self, kind: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO event(at, kind, detail) VALUES (?, ?, ?)", (now(), kind, detail)
        )
        self.db.commit()

    def recent_events(self, limit: int = 20):
        return self.db.execute(
            "SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
