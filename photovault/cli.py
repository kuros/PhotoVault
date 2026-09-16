"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, health, ingest, sync, verify
from .catalog import Catalog
from .replicas import ReplicaError, driver_for

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _load(args) -> tuple[config.Config, Catalog]:
    cfg = config.load(Path(args.config).expanduser() if args.config else None)
    cat = Catalog(cfg.catalog_path)
    # The config file is the source of truth for which devices exist; mirror it
    # into the catalog so placement rows have something to reference.
    for spec in cfg.replicas:
        cat.upsert_replica(spec.name, spec.kind, spec.root,
                           host=spec.host, is_offline=spec.offline)
    return cfg, cat


# ------------------------------------------------------------------ commands

def cmd_init(args) -> int:
    path = Path(args.config).expanduser() if args.config else config.DEFAULT_CONFIG_PATH
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.STARTER_CONFIG)
    print(f"Wrote starter config to {BOLD}{path}{RESET}\n")
    print("Next steps:")
    print("  1. Edit that file: set your external drive path and Windows host.")
    print("  2. photovault scan          # see what is out there, change nothing")
    print("  3. photovault ingest        # import into the primary library")
    print("  4. photovault sync --all    # fan out to every other device")
    return 0


def cmd_replicas(args) -> int:
    cfg, cat = _load(args)
    print(f"{BOLD}{'replica':<12}{'kind':<8}{'offline':<9}{'status':<14}root{RESET}")
    for name, drv, ok in sync.available_replicas(cfg):
        spec = cfg.replica(name)
        if ok:
            status = f"{GREEN}reachable{RESET}"
        elif spec.offline:
            status = f"{DIM}unplugged{RESET}"
        else:
            status = f"{RED}UNREACHABLE{RESET}"
        star = "*" if name == cfg.primary else " "
        print(f"{star}{name:<11}{spec.kind:<8}{'yes' if spec.offline else 'no':<9}"
              f"{status:<23}{spec.root}")
    print(f"\n{DIM}* = primary library (ingest writes here){RESET}")
    cat.close()
    return 0


def cmd_scan(args) -> int:
    """Dry-run ingest across every source: what exists, how big, how much is new."""
    cfg, cat = _load(args)
    sources = cfg.sources if not args.device else [
        s for s in cfg.sources if s.device == args.device]
    if not sources:
        print("No sources configured. Add [[source]] entries to your config.")
        return 1

    grand = ingest.IngestStats()
    for src in sources:
        root = Path(src.path).expanduser()
        print(f"\n{BOLD}{src.device}{RESET}  {root}")
        if not root.exists():
            print(f"  {YELLOW}path does not exist - skipping{RESET}")
            continue
        st = ingest.ingest_source(cfg, cat, src.device, root, dry_run=True,
                                  progress=lambda s: print(f"  ...{s.scanned} files",
                                                           end="\r", flush=True))
        print(f"  {st.summary()}        ")
        for k in ("scanned", "imported", "duplicates", "skipped", "failed"):
            setattr(grand, k, getattr(grand, k) + getattr(st, k))
        grand.bytes_imported += st.bytes_imported

    print(f"\n{BOLD}Total{RESET}: {grand.scanned} media files, "
          f"{grand.imported} new ({human(grand.bytes_imported)}), "
          f"{grand.duplicates} already in the catalog")
    if grand.imported:
        need = grand.bytes_imported * (len(cfg.replicas))
        print(f"{DIM}Storing {cfg.min_copies} copies needs roughly "
              f"{human(grand.bytes_imported * cfg.min_copies)} across all devices.{RESET}")
    cat.close()
    return 0


def cmd_ingest(args) -> int:
    cfg, cat = _load(args)
    sources = cfg.sources if not args.device else [
        s for s in cfg.sources if s.device == args.device]
    if not sources:
        print("No matching sources configured.")
        return 1

    failed = 0
    for src in sources:
        root = Path(src.path).expanduser()
        print(f"\n{BOLD}{src.device}{RESET}  {root}")
        if not root.exists():
            print(f"  {YELLOW}path does not exist - skipping{RESET}")
            continue
        st = ingest.ingest_source(
            cfg, cat, src.device, root, dry_run=args.dry_run,
            progress=lambda s: print(f"  ...{s.scanned} scanned, {s.imported} imported",
                                     end="\r", flush=True))
        print(f"  {st.summary()}        ")
        for err in st.errors[:10]:
            print(f"  {RED}{err}{RESET}")
        failed += st.failed

    if not args.dry_run:
        print(f"\n{DIM}Imported into the primary library only. "
              f"Run 'photovault sync --all' to create backup copies.{RESET}")
    cat.close()
    return 1 if failed else 0


def cmd_sync(args) -> int:
    cfg, cat = _load(args)
    targets = ([r.name for r in cfg.replicas if r.name != cfg.primary]
               if args.all else args.replica)
    if not targets:
        print("Name a replica, or pass --all.")
        return 1

    rc = 0
    for name in targets:
        print(f"\n{BOLD}{name}{RESET}")
        try:
            st = sync.push(cfg, cat, name, limit=args.limit, dry_run=args.dry_run,
                           progress=lambda s, total: print(
                               f"  ...{s.copied}/{total} copied", end="\r", flush=True))
        except (ReplicaError, KeyError) as exc:
            spec = next((r for r in cfg.replicas if r.name == name), None)
            tone = YELLOW if spec and spec.offline else RED
            print(f"  {tone}{exc}{RESET}")
            if not (spec and spec.offline):
                rc = 1
            continue
        print(f"  {st.summary()}        ")
        for err in st.errors[:10]:
            print(f"  {RED}{err}{RESET}")
        if st.failed:
            rc = 1
    cat.close()
    return rc


def cmd_reconcile(args) -> int:
    cfg, cat = _load(args)
    targets = [r.name for r in cfg.replicas] if args.all else args.replica
    rc = 0
    for name in targets:
        try:
            changed = sync.reconcile(cfg, cat, name)
            print(f"{name}: {changed} placement corrections")
        except (ReplicaError, KeyError) as exc:
            print(f"{name}: {YELLOW}{exc}{RESET}")
            rc = 1
    cat.close()
    return rc


def cmd_scrub(args) -> int:
    cfg, cat = _load(args)
    window = "everything" if args.force else f"due every {cfg.scrub_days} days"
    print(f"Verifying up to {args.limit or 'all'} stored copies ({window})...")
    st = verify.scrub(cfg, cat, limit=args.limit, repair=not args.no_repair,
                      force=args.force,
                      progress=lambda s, total: print(
                          f"  ...{s.checked}/{total}", end="\r", flush=True))
    print(f"  {st.summary()}        \n")
    for p in st.problems[:50]:
        colour = RED if ("CORRUPT" in p or "!!" in p) else YELLOW if "MISSING" in p else GREEN
        print(f"  {colour}{p}{RESET}")
    cat.close()
    return 1 if (st.corrupt or st.unrepairable) else 0


def cmd_status(args) -> int:
    cfg, cat = _load(args)
    h = health.assess(cfg, cat)

    print(f"{BOLD}Library{RESET}  {h.total_assets:,} photos and videos, "
          f"{human(h.total_bytes)}")
    if h.total_assets == 0:
        print(f"\n{DIM}Nothing ingested yet. Run 'photovault scan' to see "
              f"what is out there.{RESET}")
        cat.close()
        return 0

    print(f"\n{BOLD}{'replica':<12}{'holds':>10}{'missing':>10}"
          f"{'corrupt':>10}{'size':>12}{RESET}")
    for name, s in h.per_replica.items():
        tag = f" {DIM}(offline){RESET}" if s["offline"] else ""
        bad = f"{RED}{s['corrupt']:>10}{RESET}" if s["corrupt"] else f"{s['corrupt']:>10}"
        print(f"{name:<12}{s['present']:>10,}{s['missing']:>10,}{bad}"
              f"{human(s['bytes']):>12}{tag}")

    print(f"\n{BOLD}Redundancy{RESET}  (target: {cfg.min_copies} copies"
          f"{', one offline' if cfg.require_offline_copy else ''})")
    for copies in sorted(h.copies_histogram):
        n = h.copies_histogram[copies]
        colour = RED if copies <= 1 else YELLOW if copies < cfg.min_copies else GREEN
        bar = "#" * min(40, max(1, round(40 * n / h.total_assets)))
        label = "copy" if copies == 1 else "copies"
        print(f"  {colour}{copies} {label:<7}{n:>9,}  {bar}{RESET}")

    print()
    checks = [
        (h.at_risk_single_copy == 0,
         f"{h.at_risk_single_copy:,} photos exist in only one place", "single copy"),
        (h.underprotected == 0,
         f"{h.underprotected:,} photos below {cfg.min_copies} copies", "redundancy"),
        (h.no_offline_copy == 0,
         f"{h.no_offline_copy:,} photos have no offline copy", "offline copy"),
        (h.corrupt == 0, f"{h.corrupt:,} photos have a corrupt copy", "integrity"),
    ]
    for ok, bad_msg, label in checks:
        if ok:
            print(f"  {GREEN}OK{RESET}   {label}")
        else:
            print(f"  {RED}FAIL{RESET} {bad_msg}")
    if h.never_verified:
        print(f"  {YELLOW}...{RESET}  {h.never_verified:,} copies never verified "
              f"- run 'photovault scrub'")

    if not h.ok:
        print(f"\n{DIM}Fix with: photovault sync --all{RESET}")
    cat.close()
    return 0 if h.ok else 2


def cmd_restore(args) -> int:
    """Rebuild a full library from whichever replicas are reachable."""
    cfg, cat = _load(args)
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    drivers = []
    for spec in cfg.replicas:
        drv = driver_for(spec)
        try:
            if drv.available():
                drivers.append((spec.name, drv))
        except Exception:
            continue
    if not drivers:
        print(f"{RED}No replica is reachable.{RESET}")
        return 1
    print(f"Restoring to {dest} from: {', '.join(n for n, _ in drivers)}")

    done = failed = 0
    for asset in cat.all_assets():
        out = dest / asset["rel_path"]
        if out.exists():
            done += 1
            continue
        for name, drv in drivers:
            try:
                if drv.hash_of(asset["rel_path"]) != asset["hash"]:
                    continue
                drv.get(asset["rel_path"], out)
                done += 1
                break
            except (OSError, ReplicaError):
                continue
        else:
            failed += 1
            print(f"  {RED}unrecoverable: {asset['rel_path']}{RESET}")
        if (done + failed) % 100 == 0:
            print(f"  ...{done} restored", end="\r", flush=True)

    print(f"\nRestored {done:,} files, {failed:,} unrecoverable")
    cat.close()
    return 1 if failed else 0


def cmd_rebuild(args) -> int:
    """Recover a lost or damaged catalog from the files on a replica."""
    cfg, cat = _load(args)
    try:
        n = sync.rebuild_from(cfg, cat, args.replica,
                              progress=lambda f: print(f"  ...{f} recovered",
                                                       end="\r", flush=True))
    except (ReplicaError, KeyError) as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    print(f"Recovered {n:,} assets from {args.replica}        ")
    others = [r.name for r in cfg.replicas if r.name != args.replica]
    if others:
        print(f"{DIM}Now run 'photovault reconcile --all' to re-learn what "
              f"{', '.join(others)} hold.{RESET}")
    cat.close()
    return 0


def cmd_ui(args) -> int:
    """Serve the local web interface."""
    from .web.server import serve
    cfg, cat = _load(args)
    cat.close()  # the server opens its own per-thread connections
    serve(cfg, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_log(args) -> int:
    cfg, cat = _load(args)
    for e in reversed(cat.recent_events(args.limit)):
        print(f"{DIM}{e['at']}{RESET}  {e['kind']:<10} {e['detail']}")
    cat.close()
    return 0


# -------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photovault",
        description="A distributed, cloud-free photo library with verifiable backups.")
    p.add_argument("--version", action="version", version=f"photovault {__version__}")
    p.add_argument("-c", "--config", help="path to config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a starter config file")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("replicas", help="list configured devices and reachability")
    s.set_defaults(func=cmd_replicas)

    s = sub.add_parser("scan", help="report what would be imported, change nothing")
    s.add_argument("--device", help="only this source device")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("ingest", help="import new photos into the primary library")
    s.add_argument("--device", help="only this source device")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("sync", help="copy missing photos to backup replicas")
    s.add_argument("replica", nargs="*", help="replica names")
    s.add_argument("--all", action="store_true", help="every non-primary replica")
    s.add_argument("--limit", type=int, help="stop after N files")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("reconcile",
                       help="rebuild catalog beliefs by listing what a replica holds")
    s.add_argument("replica", nargs="*")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("scrub", help="re-hash stored copies, detect and repair rot")
    s.add_argument("--limit", type=int, default=2000)
    s.add_argument("--no-repair", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="re-verify everything, ignoring the scrub schedule")
    s.set_defaults(func=cmd_scrub)

    s = sub.add_parser("status", help="am I actually protected?")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("restore", help="rebuild a full library into a folder")
    s.add_argument("dest")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("rebuild",
                       help="recover a lost catalog by re-reading a replica")
    s.add_argument("replica")
    s.set_defaults(func=cmd_rebuild)

    s = sub.add_parser("ui", help="open the web interface in your browser")
    s.add_argument("--port", type=int, default=8723)
    s.add_argument("--host", default="127.0.0.1",
                   help="default 127.0.0.1 - this Mac only. Changing this "
                        "exposes your library to the network with no password.")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("log", help="recent operations")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_log)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
