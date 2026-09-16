"""The catalog: the single source of truth about what exists and where it lives.

The catalog is deliberately rebuildable. Every fact in it can be recovered by
re-scanning the replicas, so losing catalog.db costs time, never photos.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 3

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
    added_at    TEXT NOT NULL,
    phash       TEXT,
    width       INTEGER,
    height      INTEGER
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
    added_at   TEXT NOT NULL,
    uuid           TEXT,
    last_synced_at TEXT
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

-- A user's review decisions about duplicates. Keyed by content hash, not by
-- group, because groups are recomputed from scratch each scan while a decision
-- ("I have looked at this and it should go") stays valid.
CREATE TABLE IF NOT EXISTS dup_decision (
    hash       TEXT PRIMARY KEY REFERENCES asset(hash) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

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
        self._migrate()
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a catalog was first created.

        CREATE TABLE IF NOT EXISTS silently does nothing for an existing table,
        so new columns must be added explicitly or an upgraded PhotoVault would
        fail against an older catalog.
        """
        have = {r["name"] for r in
                self.db.execute("PRAGMA table_info(replica)").fetchall()}
        for column, ddl in (("uuid", "TEXT"), ("last_synced_at", "TEXT")):
            if column not in have:
                self.db.execute(f"ALTER TABLE replica ADD COLUMN {column} {ddl}")

        have = {r["name"] for r in
                self.db.execute("PRAGMA table_info(asset)").fetchall()}
        for column, ddl in (("phash", "TEXT"), ("width", "INTEGER"),
                            ("height", "INTEGER")):
            if column not in have:
                self.db.execute(f"ALTER TABLE asset ADD COLUMN {column} {ddl}")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_asset_phash ON asset(phash)")
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

    # --------------------------------------------------------------- browsing

    def timeline(self):
        """Photo counts per year and month, for the browser's date sidebar."""
        return self.db.execute(
            """SELECT substr(captured_at, 1, 4) AS year,
                      substr(captured_at, 6, 2) AS month,
                      COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes
               FROM asset
               WHERE captured_at IS NOT NULL
               GROUP BY year, month
               ORDER BY year DESC, month DESC"""
        ).fetchall()

    def undated_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) n FROM asset WHERE captured_at IS NULL"
        ).fetchone()["n"]

    def browse(self, *, year=None, month=None, kind=None, undated=False,
               limit=200, offset=0):
        """A page of assets, newest first, with their live copy count."""
        where, params = [], []
        if undated:
            where.append("a.captured_at IS NULL")
        else:
            if year:
                where.append("substr(a.captured_at, 1, 4) = ?")
                params.append(str(year))
            if month:
                where.append("substr(a.captured_at, 6, 2) = ?")
                params.append(f"{int(month):02d}")
        if kind:
            where.append("a.media_kind = ?")
            params.append(kind)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        rows = self.db.execute(
            f"""SELECT a.*,
                       (SELECT COUNT(*) FROM placement p
                        WHERE p.hash = a.hash AND p.state = 'present') AS copies,
                       (SELECT COUNT(*) FROM placement p
                        WHERE p.hash = a.hash AND p.state = 'corrupt') AS bad
                FROM asset a {clause}
                ORDER BY a.captured_at DESC, a.rel_path DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = self.db.execute(
            f"SELECT COUNT(*) n FROM asset a {clause}", tuple(params)
        ).fetchone()["n"]
        return rows, total

    def asset_detail(self, hash_: str):
        asset = self.asset(hash_)
        if not asset:
            return None
        return {
            "asset": dict(asset),
            "placements": [dict(p) for p in self.placements(hash_)],
            "sources": [dict(s) for s in self.db.execute(
                "SELECT device, abs_path, seen_at FROM source_file WHERE hash = ?",
                (hash_,)).fetchall()],
        }


    # -------------------------------------------------------------- duplicates

    def assets_without_phash(self, limit: int | None = None):
        q = ("SELECT hash, rel_path, ext, media_kind FROM asset "
             "WHERE phash IS NULL AND media_kind = 'image' ORDER BY rel_path")
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.db.execute(q).fetchall()

    def set_phash(self, hash_: str, phash: str | None,
                  width: int | None = None, height: int | None = None) -> None:
        self.db.execute(
            "UPDATE asset SET phash = ?, width = ?, height = ? WHERE hash = ?",
            (phash, width, height, hash_))

    def assets_with_phash(self):
        return self.db.execute(
            "SELECT * FROM asset WHERE phash IS NOT NULL AND phash != ''"
        ).fetchall()

    def set_decision(self, hash_: str, action: str) -> None:
        self.db.execute(
            """INSERT INTO dup_decision(hash, action, decided_at) VALUES (?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET action = excluded.action,
                                               decided_at = excluded.decided_at""",
            (hash_, action, now()))

    def clear_decision(self, hash_: str) -> None:
        self.db.execute("DELETE FROM dup_decision WHERE hash = ?", (hash_,))

    def decisions(self) -> dict[str, str]:
        return {r["hash"]: r["action"] for r in
                self.db.execute("SELECT hash, action FROM dup_decision").fetchall()}

    def remove_asset(self, hash_: str) -> None:
        """Forget an asset entirely. Only ever called after its replicas have
        been cleared and a keeper has been verified."""
        self.db.execute("DELETE FROM asset WHERE hash = ?", (hash_,))
