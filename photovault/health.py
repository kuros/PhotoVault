"""Answering the only question that matters: am I actually protected?"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog
from .config import Config


@dataclass
class Health:
    total_assets: int = 0
    total_bytes: int = 0
    per_replica: dict[str, dict] = field(default_factory=dict)
    copies_histogram: dict[int, int] = field(default_factory=dict)
    underprotected: int = 0
    no_offline_copy: int = 0
    corrupt: int = 0
    at_risk_single_copy: int = 0
    never_verified: int = 0

    @property
    def ok(self) -> bool:
        return (self.underprotected == 0 and self.corrupt == 0
                and self.no_offline_copy == 0)


def assess(cfg: Config, catalog: Catalog) -> Health:
    h = Health()
    row = catalog.db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM asset "
        "WHERE deleted_at IS NULL").fetchone()
    h.total_assets, h.total_bytes = row["n"], row["b"]

    offline = {r.name for r in cfg.replicas if r.offline}

    for spec in cfg.replicas:
        r = catalog.db.execute(
            """SELECT state, COUNT(*) n, COALESCE(SUM(a.size),0) b
               FROM placement p JOIN asset a ON a.hash = p.hash
               WHERE p.replica = ? AND a.deleted_at IS NULL
               GROUP BY state""", (spec.name,)).fetchall()
        counts = {x["state"]: x["n"] for x in r}
        bytes_ = sum(x["b"] for x in r if x["state"] == "present")
        h.per_replica[spec.name] = {
            "present": counts.get("present", 0),
            "missing": counts.get("missing", 0),
            "corrupt": counts.get("corrupt", 0),
            "bytes": bytes_,
            "offline": spec.name in offline,
            "kind": spec.kind,
        }

    for a in catalog.db.execute(
        """SELECT a.hash,
                  (SELECT COUNT(*) FROM placement p
                   WHERE p.hash = a.hash AND p.state = 'present') AS copies,
                  (SELECT COUNT(*) FROM placement p
                   WHERE p.hash = a.hash AND p.state = 'present'
                     AND p.replica IN (%s)) AS offline_copies,
                  (SELECT COUNT(*) FROM placement p
                   WHERE p.hash = a.hash AND p.state = 'corrupt') AS bad
           FROM asset a WHERE a.deleted_at IS NULL"""
        % (",".join("?" * len(offline)) or "''"),
        tuple(offline),
    ):
        c = a["copies"]
        h.copies_histogram[c] = h.copies_histogram.get(c, 0) + 1
        if c < cfg.min_copies:
            h.underprotected += 1
        if c <= 1:
            h.at_risk_single_copy += 1
        if cfg.require_offline_copy and offline and a["offline_copies"] == 0:
            h.no_offline_copy += 1
        if a["bad"]:
            h.corrupt += 1

    h.never_verified = catalog.db.execute(
        "SELECT COUNT(*) n FROM placement WHERE state='present' AND verified_at IS NULL"
    ).fetchone()["n"]
    return h
