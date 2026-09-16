"""Proving that the drive you plugged in is the drive you think it is.

With one external drive, a filesystem path is an adequate name for it. With
two, it stops being one: macOS assigns /Volumes/<Name> on a first-come basis,
so an identically-named second drive lands on the first one's path whenever
that one is unplugged. PhotoVault would then read one drive's contents, record
them against the other's name, and start "repairing" the wrong disk - while
reporting four green checks.

The fix is to stop trusting the path. Each replica is stamped with a random
identifier stored on the storage itself. Before any operation, the stamp on the
disk must match the stamp the catalog remembers. A path can lie; the marker
cannot, because it travels with the bytes.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from datetime import datetime

from .catalog import Catalog
from .config import Config
from .replicas import Driver, ReplicaError, driver_for


class IdentityMismatch(ReplicaError):
    """The storage at this path is not the replica we expected."""


def _payload(name: str, uid: str) -> str:
    return json.dumps({
        "photovault": 1,
        "replica": name,
        "uuid": uid,
        "stamped_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2) + "\n"


def _parse(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) and "uuid" in data else None
    except (ValueError, TypeError):
        return None


def verify(cfg: Config, catalog: Catalog, name: str,
           drv: Driver | None = None, *, claim_if_unmarked: bool = True) -> str:
    """Confirm the storage for `name` really is that replica.

    Returns the replica's uuid. Raises IdentityMismatch if the storage belongs
    to a different replica, which is the case that must never pass silently.
    """
    spec = cfg.replica(name)
    drv = drv or driver_for(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")

    row = catalog.replica(name)
    known = row["uuid"] if row and "uuid" in row.keys() else None

    marker = _parse(drv.read_marker() or "")

    if marker is None:
        # Claiming unstamped storage is only safe the FIRST time we ever see
        # this replica. Once a uuid is on record, an unmarked path means the
        # real drive is absent - and an absent external drive leaves either
        # nothing or an empty mount point behind. Claiming that would create a
        # directory on the internal disk and quietly "restore" the whole
        # library into it while reporting success.
        if known:
            raise IdentityMismatch(
                f"{name}: the storage at this path carries no identity marker, "
                f"but {name!r} is registered as drive {known[:8]}. The drive is "
                f"probably not plugged in. If you really replaced it, run "
                f"'photovault adopt {name}'.")
        if not claim_if_unmarked:
            raise IdentityMismatch(f"{name}: storage carries no identity marker")
        # Genuine first use: a new drive, or a library predating markers.
        uid = str(uuidlib.uuid4())
        drv.write_marker(_payload(name, uid))
        _remember(catalog, name, uid)
        return uid

    if marker.get("replica") != name:
        raise IdentityMismatch(
            f"{name}: this storage is stamped as replica "
            f"{marker.get('replica')!r}, not {name!r}. Refusing to touch it - "
            f"check which drive is plugged in, or fix the path in your config.")

    if known and marker["uuid"] != known:
        raise IdentityMismatch(
            f"{name}: this is a different physical drive than the one "
            f"registered as {name!r} (marker {marker['uuid'][:8]}, expected "
            f"{known[:8]}). If you genuinely replaced the drive, run "
            f"'photovault adopt {name}' to re-register it.")

    _remember(catalog, name, marker["uuid"])
    return marker["uuid"]


def adopt(cfg: Config, catalog: Catalog, name: str) -> str:
    """Deliberately re-register the storage at this path as `name`.

    The escape hatch for a genuinely replaced drive. Separate from verify() on
    purpose: overwriting a replica's identity should be something you asked
    for, never something that happens because you plugged in the wrong disk.
    """
    spec = cfg.replica(name)
    drv = driver_for(spec)
    if not drv.available():
        raise ReplicaError(f"replica {name!r} is not available")
    uid = str(uuidlib.uuid4())
    drv.write_marker(_payload(name, uid))
    _remember(catalog, name, uid)
    catalog.log("adopt", f"{name}: re-registered as {uid}")
    return uid


def _remember(catalog: Catalog, name: str, uid: str) -> None:
    catalog.db.execute("UPDATE replica SET uuid = ? WHERE name = ?", (uid, name))
    catalog.db.commit()


def mark_synced(catalog: Catalog, name: str) -> None:
    catalog.db.execute(
        "UPDATE replica SET last_synced_at = ? WHERE name = ?",
        (datetime.now().isoformat(timespec="seconds"), name))
    catalog.db.commit()


def staleness(catalog: Catalog, name: str) -> tuple[str | None, int | None]:
    """(last synced ISO timestamp, whole days ago) - the rotation question."""
    row = catalog.replica(name)
    if not row or "last_synced_at" not in row.keys() or not row["last_synced_at"]:
        return None, None
    last = row["last_synced_at"]
    days = (datetime.now() - datetime.fromisoformat(last)).days
    return last, days
