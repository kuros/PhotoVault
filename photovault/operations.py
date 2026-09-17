"""A described catalogue of everything PhotoVault can do.

The web UI's buttons were accumulating without explanation: Import, Back up,
Verify, with nothing saying what they touch, when to run them, or what happens
if a drive is missing. A button whose consequences you have to remember is a
button you eventually press at the wrong moment.

So each operation carries its own description, its risk level, what it needs to
be connected, and when it was last run. The UI renders that rather than
inventing its own copy - one place to keep honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .catalog import Catalog
from .config import Config

SAFE = "safe"              # reads, or only ever adds
CAREFUL = "careful"        # writes a lot, or is slow
DESTRUCTIVE = "destructive"  # can remove data


@dataclass
class Operation:
    id: str
    title: str
    group: str
    what: str                    # what it actually does
    when: str                    # when you would want it
    risk: str = SAFE
    action: str | None = None    # job action, or None for CLI-only
    command: str = ""            # the equivalent CLI invocation
    needs_all_devices: bool = False
    note: str = ""               # caveat worth reading first
    event_kinds: tuple = ()      # log kinds used to date the last run
    last_run: str | None = None
    ready: bool = True
    blocked_by: list[str] = field(default_factory=list)


CATALOGUE: list[Operation] = [
    # ---------------------------------------------------------------- routine
    Operation(
        id="import", title="Import photos", group="Routine",
        what="Reads every configured source, copies anything new into the "
             "library, then replicates it to every device that is connected.",
        when="Whenever you have added photos — after uploading from a phone, "
             "or plugging in an old drive.",
        risk=SAFE, action="ingest", command="photovault import",
        event_kinds=("ingest",),
        note="Sources are never modified. Photos already in the library are "
             "recognised by content and skipped, so running it twice is free."),
    Operation(
        id="sync", title="Back up to every device", group="Routine",
        what="Copies anything a device is missing. Nothing is deleted.",
        when="After importing, and whenever you reconnect a drive that has "
             "been away.",
        risk=SAFE, action="sync", command="photovault sync --all",
        event_kinds=("push",),
        note="Devices that are not connected are skipped, not failed. "
             "Reconnect them and run it again."),
    Operation(
        id="backup", title="Back up the catalog", group="Routine",
        what="Writes a compressed, checksummed snapshot of the catalog to "
             "every connected device.",
        when="After any session where you reviewed duplicates or deleted "
             "photos.",
        risk=SAFE, action="backup", command="photovault backup",
        event_kinds=("backup",),
        note="Your photos can be rebuilt from the files; your decisions cannot. "
             "Duplicate choices and trash state exist only in the catalog."),

    # ------------------------------------------------------------ maintenance
    Operation(
        id="scrub", title="Verify stored files", group="Maintenance",
        what="Re-reads stored photos and checks their bytes still match the "
             "hash recorded when they were imported. Damaged copies are "
             "repaired from a healthy device automatically.",
        when="Monthly, with the backup drive connected.",
        risk=CAREFUL, action="scrub", command="photovault scrub",
        event_kinds=("scrub",),
        note="Slow: it reads every file. This is the only thing that detects "
             "silent corruption — a copy tool will happily replicate rot."),
    Operation(
        id="dupscan", title="Find duplicates", group="Maintenance",
        what="Analyses photos for near-duplicates — the same picture "
             "re-compressed, resized or re-saved.",
        when="After importing an old drive, where the same photos often arrive "
             "several times in different sizes.",
        risk=SAFE, action="dupscan", command="photovault duplicates",
        event_kinds=("dup-scan",),
        note="Finds candidates only. Nothing is deleted until you review them "
             "in the Duplicates tab and choose what to keep."),
    Operation(
        id="reconcile", title="Re-check devices", group="Maintenance",
        what="Asks every connected device what it actually holds and corrects "
             "the catalog's record.",
        when="After restoring a catalog backup, or if a device's numbers look "
             "wrong.",
        risk=SAFE, action="reconcile", command="photovault reconcile --all",
        event_kinds=("reconcile",),
        note="Reads only. It changes the catalog's beliefs, never your files."),

    # --------------------------------------------------------------- clean-up
    Operation(
        id="purge", title="Empty the trash", group="Clean-up",
        what="Permanently removes trashed photos from every device.",
        when="Only when you are certain. Deleted photos are kept for "
             "trash_days first, and stay fully backed up until purged.",
        risk=DESTRUCTIVE, action=None,
        command="photovault trash --purge --yes",
        needs_all_devices=True,
        note="Refuses to run unless every device is connected: purging with a "
             "drive in a drawer would strand the file there while the catalog "
             "forgot it, and a later rebuild would resurrect it."),
    Operation(
        id="dupapply", title="Delete reviewed duplicates", group="Clean-up",
        what="Removes the duplicates you marked, keeping the one you chose.",
        when="After reviewing groups in the Duplicates tab.",
        risk=DESTRUCTIVE, action=None,
        command="photovault duplicates --apply --yes",
        note="Use the Duplicates tab — it needs your per-group choices. The "
             "photo you keep is re-read and re-hashed on enough devices first."),

    # --------------------------------------------------------------- recovery
    Operation(
        id="restore_catalog", title="Restore the catalog", group="Recovery",
        what="Replaces the catalog with the newest verified snapshot from any "
             "connected device.",
        when="The catalog is lost or corrupted, and you have a backup.",
        risk=CAREFUL, action=None, command="photovault backup --restore",
        note="Your current catalog is moved aside, not overwritten. Follow "
             "with 'Re-check devices'."),
    Operation(
        id="rebuild", title="Rebuild from the files", group="Recovery",
        what="Reconstructs the catalog by re-reading every photo on every "
             "device.",
        when="The catalog is lost and you have no backup.",
        risk=CAREFUL, action=None,
        command="photovault rebuild --all && photovault reconcile --all",
        note="Recovers your photos, but not your duplicate decisions or trash "
             "state — those exist nowhere but the catalog. Prefer a restore."),
    Operation(
        id="export", title="Copy everything to a folder", group="Recovery",
        what="Writes a complete, plain copy of the library into any folder.",
        when="Moving to a new machine, or handing the archive to someone.",
        risk=SAFE, action=None, command="photovault restore /path/to/folder",
        note="The result is ordinary dated folders. No PhotoVault needed to "
             "read them."),
]


def _last_runs(catalog: Catalog) -> dict[str, str]:
    rows = catalog.db.execute(
        "SELECT kind, MAX(at) AS at FROM event GROUP BY kind").fetchall()
    return {r["kind"]: r["at"] for r in rows}


def describe(cfg: Config, catalog: Catalog) -> list[dict]:
    """The catalogue, annotated with this vault's current state."""
    from .sync import available_replicas

    reachable, missing = set(), []
    for name, _drv, ok in available_replicas(cfg):
        (reachable.add(name) if ok else missing.append(name))

    seen = _last_runs(catalog)
    out = []
    for op in CATALOGUE:
        stamps = [seen[k] for k in op.event_kinds if k in seen]
        last = max(stamps) if stamps else None

        blocked = []
        if op.needs_all_devices and missing:
            blocked = list(missing)

        out.append({
            "id": op.id, "title": op.title, "group": op.group,
            "what": op.what, "when": op.when, "risk": op.risk,
            "action": op.action, "command": op.command, "note": op.note,
            "last_run": last,
            "days_since": _days_since(last),
            "ready": not blocked,
            "blocked_by": blocked,
        })
    return out


def _days_since(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 86400
    except ValueError:
        return None
