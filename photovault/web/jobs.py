"""Background jobs for the web UI.

Ingesting 400 GB takes hours. An HTTP request that waits for it would time out,
so the browser asks us to *start* a job and then polls for progress.

Two rules make this safe:

1. **One mutating job at a time.** Running ingest and sync concurrently would
   have them fighting over the same catalog rows for no speedup - the work is
   disk-bound, not CPU-bound.
2. **Every job opens its own database connection.** SQLite connections belong to
   the thread that created them. Sharing one across threads corrupts state in
   ways that surface as baffling intermittent errors. Confining a connection to
   a single thread costs nothing and removes the whole category of bug.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import backups, duplicates, ingest, sync, uploads, verify
from ..catalog import Catalog
from ..config import Config

MAX_HISTORY = 40


@dataclass
class Job:
    id: int
    action: str
    label: str
    state: str = "running"          # running | done | failed | cancelled
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    done: int = 0
    total: int | None = None
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict:
        pct = None
        if self.total:
            pct = min(100, round(100 * self.done / self.total))
        elif self.state == "done":
            pct = 100
        return {
            "id": self.id, "action": self.action, "label": self.label,
            "state": self.state, "started_at": self.started_at,
            "finished_at": self.finished_at, "done": self.done,
            "total": self.total, "percent": pct, "message": self.message,
            "result": self.result, "errors": self.errors[:20],
        }


class JobRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: list[Job] = []
        self._next_id = 1
        self._lock = threading.Lock()          # guards the job list
        self._busy = threading.Lock()          # guards "one mutating job at a time"

    # --------------------------------------------------------------- queries

    def list(self) -> list[dict]:
        with self._lock:
            return [j.as_dict() for j in reversed(self._jobs)]

    def get(self, job_id: int) -> dict | None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    return j.as_dict()
        return None

    @property
    def running(self) -> bool:
        with self._lock:
            return any(j.state == "running" for j in self._jobs)

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id and j.state == "running":
                    j._cancel.set()
                    j.message = "cancelling..."
                    return True
        return False

    # ---------------------------------------------------------------- launch

    def start(self, action: str, **kwargs) -> dict:
        if self._busy.locked():
            return {"error": "another operation is already running"}
        handler = getattr(self, f"_run_{action}", None)
        if handler is None:
            return {"error": f"unknown action {action!r}"}

        with self._lock:
            job = Job(id=self._next_id, action=action,
                      label=_label(action, kwargs))
            self._next_id += 1
            self._jobs.append(job)
            del self._jobs[:-MAX_HISTORY]

        threading.Thread(target=self._wrap, args=(job, handler, kwargs),
                         daemon=True).start()
        return job.as_dict()

    def _wrap(self, job: Job, handler: Callable, kwargs: dict) -> None:
        with self._busy:
            cat = Catalog(self.cfg.catalog_path)   # this thread's own connection
            try:
                handler(job, cat, **kwargs)
                job.state = "cancelled" if job._cancel.is_set() else "done"
            except Exception as exc:
                job.state = "failed"
                job.message = str(exc)
                job.errors.append(traceback.format_exc(limit=3))
            finally:
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                cat.close()

    # -------------------------------------------------------------- handlers

    def _run_ingest(self, job: Job, cat: Catalog, device: str | None = None,
                    dry_run: bool = False) -> None:
        sources = [s for s in self.cfg.sources if not device or s.device == device]
        if not sources:
            raise ValueError("no matching sources are configured")

        totals = {"imported": 0, "duplicates": 0, "skipped": 0,
                  "failed": 0, "bytes": 0}
        for src in sources:
            if job._cancel.is_set():
                break
            root = Path(src.path).expanduser()
            job.message = f"scanning {src.device}"
            if not root.exists():
                job.errors.append(f"{src.device}: {root} does not exist")
                continue

            def progress(st, _dev=src.device):
                job.done = st.scanned
                job.message = f"{_dev}: {st.scanned} scanned, {st.imported} imported"

            st = ingest.ingest_source(self.cfg, cat, src.device, root,
                                      dry_run=dry_run, progress=progress)
            totals["imported"] += st.imported
            totals["duplicates"] += st.duplicates
            totals["skipped"] += st.skipped
            totals["failed"] += st.failed
            totals["bytes"] += st.bytes_imported
            job.errors.extend(st.errors[:10])

        job.result = totals
        job.message = (f"imported {totals['imported']}, "
                       f"{totals['duplicates']} already known")

    def _run_sync(self, job: Job, cat: Catalog, replica: str | None = None) -> None:
        targets = ([replica] if replica else
                   [r.name for r in self.cfg.replicas if r.name != self.cfg.primary])
        copied = failed = skipped = 0
        for name in targets:
            if job._cancel.is_set():
                break
            job.message = f"syncing {name}"
            try:
                def progress(st, total, _n=name):
                    job.done, job.total = st.copied, total
                    job.message = f"{_n}: {st.copied}/{total} copied"

                st = sync.push(self.cfg, cat, name, progress=progress)
                copied += st.copied
                failed += st.failed
                job.errors.extend(st.errors[:10])
            except Exception as exc:
                spec = next((r for r in self.cfg.replicas if r.name == name), None)
                if spec and spec.offline:
                    skipped += 1
                    job.errors.append(f"{name}: not connected (offline replica)")
                else:
                    job.errors.append(f"{name}: {exc}")
                    failed += 1
        job.result = {"copied": copied, "failed": failed, "skipped_offline": skipped}
        job.message = f"copied {copied} files"

    def _run_scrub(self, job: Job, cat: Catalog, limit: int | None = 2000,
                   force: bool = False) -> None:
        job.message = "verifying stored copies"

        def progress(st, total):
            job.done, job.total = st.checked, total
            job.message = f"verified {st.checked}/{total}"

        st = verify.scrub(self.cfg, cat, limit=limit, force=force, progress=progress)
        job.result = {"checked": st.checked, "ok": st.ok, "corrupt": st.corrupt,
                      "missing": st.vanished, "repaired": st.repaired,
                      "unrepairable": st.unrepairable}
        job.errors.extend(st.problems[:40])
        job.message = (f"checked {st.checked}, repaired {st.repaired}, "
                       f"{st.unrepairable} unrepairable")

    def _run_reconcile(self, job: Job, cat: Catalog, replica: str | None = None) -> None:
        targets = [replica] if replica else [r.name for r in self.cfg.replicas]
        changed = 0
        for name in targets:
            job.message = f"reconciling {name}"
            try:
                changed += sync.reconcile(self.cfg, cat, name)
            except Exception as exc:
                job.errors.append(f"{name}: {exc}")
        job.result = {"corrections": changed}
        job.message = f"{changed} placement corrections"


    def _run_dupscan(self, job: Job, cat: Catalog, limit: int | None = None) -> None:
        job.message = "analysing photos"

        def progress(done, total):
            job.done, job.total = done, total
            job.message = f"analysed {done}/{total}"

        st = duplicates.scan(self.cfg, cat, limit=limit, progress=progress)
        groups = duplicates.find_groups(self.cfg, cat)
        job.result = {"analysed": st.hashed, "undecodable": st.failed,
                      "remaining": st.remaining, "groups": len(groups)}
        job.message = f"{len(groups)} duplicate groups found"

    def _run_dupapply(self, job: Job, cat: Catalog, confirm: bool = False) -> None:
        if not confirm:
            raise ValueError("refusing to delete without an explicit confirmation")
        job.message = "removing reviewed duplicates"
        rep = duplicates.apply(self.cfg, cat, dry_run=False,
                               progress=lambda n: setattr(job, "done", n))
        job.result = {"deleted": rep.deleted, "bytes_freed": rep.bytes_freed,
                      "refused": len(rep.refused)}
        job.errors.extend([f"kept {p}: {why}" for p, why in rep.refused[:20]])
        job.errors.extend(rep.errors[:10])
        job.message = f"deleted {rep.deleted}, kept {len(rep.refused)} as unsafe"


    def _run_upload_ingest(self, job: Job, cat: Catalog,
                           clear: bool = True) -> None:
        """Import what the browser staged, replicate it, then clear staging.

        Clearing happens last and on the same evidence bar as everything else:
        staging holds the only copy of a just-uploaded photo until replication
        has actually happened, so it is emptied only for files that reach
        min_copies verified copies.
        """
        def progress(st):
            job.done = st.scanned
            job.message = f"{st.scanned} scanned, {st.imported} imported"

        job.message = "importing uploaded photos"
        st = uploads.ingest_staged(self.cfg, cat, progress=progress)
        job.errors.extend(st.errors[:10])

        replicated = 0
        for spec in self.cfg.replicas:
            if spec.name == self.cfg.primary:
                continue
            job.message = f"backing up to {spec.name}"
            try:
                replicated += sync.push(self.cfg, cat, spec.name).copied
            except Exception as exc:
                if not spec.offline:
                    job.errors.append(f"{spec.name}: {exc}")

        cleared, notes = (0, [])
        if clear:
            job.message = "clearing staged files that are safely stored"
            cleared, notes = uploads.clear_imported(self.cfg, cat)
            job.errors.extend(notes)

        job.result = {"imported": st.imported, "duplicates": st.duplicates,
                      "failed": st.failed, "replicated": replicated,
                      "cleared": cleared}
        job.message = (f"imported {st.imported}, "
                       f"{st.duplicates} already in the library")


    def _run_backup(self, job: Job, cat: Catalog, keep: int | None = None) -> None:
        job.message = "snapshotting the catalog"
        res = backups.run(self.cfg, keep=keep or self.cfg.backup_keep)
        job.result = {"name": res.name, "size": res.size,
                      "copied": len(res.copied), "pruned": res.pruned,
                      "unreachable": len(res.unreachable)}
        job.errors.extend(res.errors[:10])
        for name in res.unreachable:
            job.errors.append(f"{name}: not connected")
        cat.log("backup", f"{res.name} -> {', '.join(res.copied) or 'nowhere'}")
        job.message = (f"copied to {len(res.copied)} device"
                       f"{'' if len(res.copied) == 1 else 's'}")
        if not res.copied:
            raise RuntimeError("no device was reachable to back up to")


def _label(action: str, kwargs: dict) -> str:
    target = kwargs.get("replica") or kwargs.get("device")
    base = {"ingest": "Import photos", "sync": "Back up",
            "scrub": "Verify integrity", "reconcile": "Re-check devices",
            "dupscan": "Find duplicates",
            "dupapply": "Delete reviewed duplicates",
            "upload_ingest": "Import uploaded photos",
            "backup": "Back up the catalog"}.get(action, action)
    if kwargs.get("force"):
        base += " (full)"
    if kwargs.get("dry_run"):
        base += " (preview)"
    return f"{base} - {target}" if target else base
