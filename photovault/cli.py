"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (__version__, config, duplicates, health, importer, ingest,
               placement, sync, trash, verify)
from .catalog import Catalog
from .identity import IdentityMismatch, adopt, staleness
from .replicas import ReplicaError, driver_for

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def pad(text: str, width: int, colour: str = "") -> str:
    """Left-align to a visible width. Colour codes are zero-width on screen but
    count toward str.format's width, so padding must be computed before them."""
    return f"{colour}{text}{RESET if colour else ''}" + " " * max(0, width - len(text))


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


def cmd_setup(args) -> int:
    """Interactive setup: choose a storage model and write the config."""
    from . import wizard
    path = Path(args.config).expanduser() if args.config else config.DEFAULT_CONFIG_PATH
    return wizard.run(path, force=args.force)


def cmd_start(args) -> int:
    """Bring up everything: Immich, the web UI, and optionally the watcher."""
    from .launcher import start
    cfg, cat = _load(args)
    cat.close()          # the server opens its own per-thread connections
    return start(cfg, port=args.port, host=args.host,
                 open_browser=not args.no_browser, watch=args.watch,
                 with_immich=not args.no_immich,
                 config_path=Path(args.config).expanduser() if args.config
                 else config.DEFAULT_CONFIG_PATH)


def cmd_stop(args) -> int:
    """Stop the background services that `start` brought up."""
    from .launcher import immich_down
    cfg, cat = _load(args)
    cat.close()
    if not cfg.immich.enabled:
        print("Nothing to stop - no [immich] section configured.")
        return 0
    print("Stopping Immich...")
    ok = immich_down(cfg)
    print(f"  {GREEN}stopped{RESET}" if ok
          else f"  {YELLOW}could not stop it (is Docker running?){RESET}")
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    """What is connected, what is missing, and what should happen next."""
    from .launcher import preflight, print_preflight
    cfg, cat = _load(args)
    cat.close()
    pf = preflight(cfg)
    print()
    print_preflight(cfg, pf)
    print()
    return 0 if (pf.ready and pf.healthy) else 2


def cmd_replicas(args) -> int:
    cfg, cat = _load(args)
    print(f"{BOLD}{'replica':<12}{'kind':<8}{'offline':<9}{'status':<14}"
          f"{'last synced':<14}root{RESET}")
    stale_offline = []
    for name, drv, ok in sync.available_replicas(cfg):
        spec = cfg.replica(name)
        if ok:
            status = pad("reachable", 14, GREEN)
        elif spec.offline:
            status = pad("unplugged", 14, DIM)
        else:
            status = pad("UNREACHABLE", 14, RED)

        last, days = staleness(cat, name)
        if days is None:
            age = pad("never", 14, DIM)
        elif days == 0:
            age = pad("today", 14)
        elif spec.offline and days >= 30:
            age = pad(f"{days}d ago", 14, YELLOW)
            stale_offline.append((name, days))
        else:
            age = pad(f"{days}d ago", 14)

        star = "*" if name == cfg.primary else " "
        print(f"{star}{name:<11}{spec.kind:<8}{'yes' if spec.offline else 'no':<9}"
              f"{status}{age}{spec.root}")

    print(f"\n{DIM}* = primary library (ingest writes here){RESET}")
    if stale_offline:
        worst = max(stale_offline, key=lambda x: x[1])
        print(f"{YELLOW}Plug in '{worst[0]}' next - it is {worst[1]} days "
              f"behind.{RESET}")
    cat.close()
    return 0


def cmd_adopt(args) -> int:
    """Re-register the storage at a replica's path as that replica."""
    cfg, cat = _load(args)
    spec = next((r for r in cfg.replicas if r.name == args.replica), None)
    if spec is None:
        print(f"{RED}no replica named {args.replica!r} in your config{RESET}")
        return 1
    print(f"This will stamp {BOLD}{spec.root}{RESET} as replica "
          f"{BOLD}{args.replica}{RESET}.")
    if not args.yes:
        reply = input("Only do this if you deliberately replaced the drive. "
                      "Continue? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1
    try:
        uid = adopt(cfg, cat, args.replica)
    except ReplicaError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    print(f"{GREEN}Registered.{RESET} New identity {uid[:8]}.")
    print(f"{DIM}Run 'photovault reconcile {args.replica}' to learn what it "
          f"holds, then 'photovault sync {args.replica}'.{RESET}")
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
        except IdentityMismatch as exc:
            print(f"  {RED}WRONG DRIVE - nothing was written{RESET}")
            print(f"  {RED}{exc}{RESET}")
            rc = 1
            continue
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
        except IdentityMismatch as exc:
            print(f"{name}: {RED}WRONG DRIVE - {exc}{RESET}")
            rc = 1
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
    for asset in cat.all_assets():   # trashed photos are not restored
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
    """Recover a lost or damaged catalog from the files on one or more replicas.

    With sharding no single drive is complete, so recovery must read every
    drive it can reach. --all does that.
    """
    cfg, cat = _load(args)
    if args.all:
        targets = [r.name for r in cfg.replicas if r.kind == "local"]
    elif args.replica:
        targets = [args.replica]
    else:
        print("Name a replica, or pass --all to read every drive you can reach.")
        return 1

    total, reached, skipped = 0, [], []
    for name in targets:
        try:
            n = sync.rebuild_from(cfg, cat, name,
                                  progress=lambda f, _n=name: print(
                                      f"  {_n}: ...{f} recovered", end="\r",
                                      flush=True))
        except (ReplicaError, KeyError) as exc:
            skipped.append((name, str(exc)))
            continue
        print(f"  {name}: {n:,} assets              ")
        total += n
        reached.append(name)

    if not reached:
        print(f"{RED}No replica could be read.{RESET}")
        for name, why in skipped:
            print(f"  {name}: {why}")
        return 1

    unique = cat.db.execute("SELECT COUNT(*) n FROM asset").fetchone()["n"]
    print(f"\nRecovered {unique:,} distinct photos from {', '.join(reached)}")
    for name, why in skipped:
        print(f"  {YELLOW}skipped {name}: {why}{RESET}")

    if cfg.sharded and skipped:
        print(f"{YELLOW}Some drives were unreadable. With sharding no single "
              f"drive is complete,\nso photos that lived only on those may be "
              f"missing. Connect them and re-run.{RESET}")
    print(f"{DIM}Now run 'photovault reconcile --all'.{RESET}")
    cat.close()
    return 0


def cmd_ui(args) -> int:
    """Serve the local web interface."""
    from .web.server import serve
    cfg, cat = _load(args)
    cat.close()  # the server opens its own per-thread connections
    serve(cfg, host=args.host, port=args.port, open_browser=not args.no_browser,
          config_path=Path(args.config).expanduser() if args.config
          else config.DEFAULT_CONFIG_PATH)
    return 0


def cmd_plan(args) -> int:
    """Show where each photo would live, and whether the drives are big enough."""
    cfg, cat = _load(args)
    if not cfg.sharded:
        print(f"Every replica holds a {BOLD}complete copy{RESET}. "
              f"Nothing to split.\n")
        print(f"{DIM}To spread the library across drives that are each too small "
              f"for it,\nmark them mode = \"shard\" in your config, or run "
              f"'photovault setup'.{RESET}")
        cat.close()
        return 0

    plan = placement.build_plan(cfg, cat)
    print(f"{BOLD}Library{RESET}  {len(plan.assignments):,} photos, "
          f"{human(plan.total_bytes)}")
    print(f"{BOLD}Target{RESET}   {cfg.min_copies} copies "
          f"({len(cfg.full_replicas)} full replica"
          f"{'s' if len(cfg.full_replicas) != 1 else ''} + "
          f"{plan.shard_copies_needed} from shards)\n")

    print(f"{BOLD}{'replica':<12}{'mode':<8}{'files':>10}{'size':>11}"
          f"{'capacity':>11}  fill{RESET}")
    for name, s in plan.per_replica.items():
        cap = human(s["capacity"]) if s["capacity"] else "unknown"
        if s["fill"] is None:
            bar = f"{DIM}?{RESET}"
        else:
            pct = s["fill"] * 100
            colour = RED if pct > 95 else YELLOW if pct > 80 else GREEN
            filled = min(20, round(min(pct, 100) / 5))
            bar = f"{colour}{'#' * filled}{'.' * (20 - filled)} {pct:5.1f}%{RESET}"
        tag = f" {DIM}(offline){RESET}" if s["offline"] else ""
        print(f"{name:<12}{s['mode']:<8}{s['files']:>10,}{human(s['bytes']):>11}"
              f"{cap:>11}  {bar}{tag}")

    print()
    if plan.ok:
        print(f"  {GREEN}OK{RESET}   every photo fits with "
              f"{cfg.min_copies} copies")
    else:
        short = len(plan.unplaceable)
        print(f"  {RED}FAIL{RESET} {short:,} photos cannot reach "
              f"{cfg.min_copies} copies - not enough space")
        need = plan.total_bytes * plan.shard_copies_needed
        have = sum(s["capacity"] or 0 for n, s in plan.per_replica.items()
                   if s["mode"] == "shard")
        print(f"       shards hold {human(have)}, need about {human(need)}")
        print(f"{DIM}       Add another drive, or lower min_copies.{RESET}")
    cat.close()
    return 0 if plan.ok else 2


def cmd_rebalance(args) -> int:
    """Reclaim space on a shard after the drive line-up changed."""
    cfg, cat = _load(args)
    targets = ([r.name for r in cfg.shard_replicas] if args.all else args.replica)
    if not targets:
        print("Name a shard replica, or pass --all.")
        return 1
    if not cfg.sharded:
        print("No shard replicas configured - nothing to rebalance.")
        return 1

    rc = 0
    for name in targets:
        print(f"\n{BOLD}{name}{RESET}")
        try:
            st = sync.rebalance(cfg, cat, name, dry_run=not args.apply)
        except (ReplicaError, KeyError) as exc:
            print(f"  {RED}{exc}{RESET}")
            rc = 1
            continue
        verb = "would remove" if not args.apply else "removed"
        print(f"  {verb} {st.removed:,} files, freeing {human(st.bytes_freed)}")
        if st.kept_unsafe:
            print(f"  {YELLOW}kept {st.kept_unsafe:,} that could not be safely "
                  f"removed{RESET}")
        for note in st.notes[:10]:
            print(f"    {DIM}{note}{RESET}")
    if not args.apply:
        print(f"\n{DIM}This was a preview. Re-run with --apply to actually "
              f"delete.{RESET}")
    cat.close()
    return rc


def cmd_watch(args) -> int:
    """Import and back up automatically as photos land in the inbox folders."""
    from . import watcher
    cfg, cat = _load(args)
    cat.close()  # the watcher opens its own connection

    inboxes = [s for s in cfg.sources if s.clear_after_import] or cfg.sources
    print(f"Watching {len(cfg.sources)} source folder(s) every {args.interval}s:")
    for src in cfg.sources:
        root = Path(src.path).expanduser()
        mark = GREEN + "ok" + RESET if root.is_dir() else YELLOW + "missing" + RESET
        clears = " (clears after import)" if src.clear_after_import else ""
        print(f"  {src.device:<10}{root}  [{mark}]{DIM}{clears}{RESET}")
    print(f"\n{DIM}New photos are imported and backed up automatically. "
          f"Ctrl+C to stop.{RESET}\n")

    watcher.watch(cfg, cfg.catalog_path, interval=args.interval,
                  clear=args.clear, once=args.once)
    return 0


def cmd_install_agent(args) -> int:
    """Install a launchd agent so the watcher runs from login."""
    import subprocess
    import sys as _sys

    if _sys.platform != "darwin":
        print(f"{RED}install-agent is macOS only. On Windows, use Task "
              f"Scheduler to run 'photovault watch' at logon.{RESET}")
        return 1

    label = "com.photovault.watch"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist = agents / f"{label}.plist"
    logs = Path.home() / "Library" / "Logs" / "PhotoVault"
    logs.mkdir(parents=True, exist_ok=True)

    cfg_path = Path(args.config).expanduser() if args.config else config.DEFAULT_CONFIG_PATH
    argv = [_sys.executable, "-m", "photovault", "-c", str(cfg_path),
            "watch", "--interval", str(args.interval)]
    entries = "".join(f"\n        <string>{a}</string>" for a in argv)

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>{entries}
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>WorkingDirectory</key><string>{Path.home()}</string>
    <key>StandardOutPath</key><string>{logs / 'watch.log'}</string>
    <key>StandardErrorPath</key><string>{logs / 'watch.err'}</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""
    if args.uninstall:
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                       capture_output=True)
        plist.unlink(missing_ok=True)
        print(f"{GREEN}Removed.{RESET} The watcher will not start at login.")
        return 0

    agents.mkdir(parents=True, exist_ok=True)
    plist.write_text(body)
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"{YELLOW}Wrote {plist} but launchctl refused to load it:{RESET}")
        print(f"  {r.stderr.strip()}")
        print(f"{DIM}Log out and back in, or run: "
              f"launchctl bootstrap gui/{_uid()} {plist}{RESET}")
        return 1
    print(f"{GREEN}Installed.{RESET} The watcher now runs from login.")
    print(f"  plist : {plist}")
    print(f"  log   : {logs / 'watch.log'}")
    print(f"{DIM}Remove it with: photovault install-agent --uninstall{RESET}")
    return 0


def _uid() -> int:
    import os
    return os.getuid()


def _source_by_device(cfg, device: str | None):
    if device:
        matches = [s for s in cfg.sources if s.device == device]
        if not matches:
            names = ", ".join(s.device for s in cfg.sources) or "none configured"
            raise KeyError(f"no source named {device!r} (have: {names})")
        return matches
    return cfg.sources


def cmd_import(args) -> int:
    """Pull from a device, archive it everywhere reachable, then verify."""
    cfg, cat = _load(args)
    try:
        sources = _source_by_device(cfg, args.device)
    except KeyError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    if not sources:
        print("No sources configured. Run 'photovault setup'.")
        return 1

    rc = 0
    for spec in sources:
        print(f"\n{BOLD}{spec.device}{RESET} {DIM}({spec.kind}){RESET}")
        rep = importer.run_import(cfg, cat, spec, reclaim=args.reclaim)
        for err in rep.errors[:10]:
            print(f"  {RED}{err}{RESET}")
            rc = 1
        if rep.reclaim:
            _print_reclaim(cfg, rep.reclaim, applied=args.reclaim)
    cat.close()
    return rc


def cmd_reclaim(args) -> int:
    """Report which files are provably safe to delete, and optionally delete."""
    cfg, cat = _load(args)
    try:
        sources = _source_by_device(cfg, args.device)
    except KeyError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1

    for spec in sources:
        if spec.kind == "adb":
            print(f"{DIM}{spec.device}: run 'photovault import {spec.device} "
                  f"--reclaim' for Android devices{RESET}")
            continue
        root = Path(spec.path).expanduser()
        if not root.is_dir():
            continue
        print(f"\n{BOLD}{spec.device}{RESET}  {root}")
        rep = importer.reclaimable(cfg, cat, root, device=spec.device,
                                   apply=args.apply)
        _print_reclaim(cfg, rep, applied=args.apply)
    cat.close()
    return 0


def _print_reclaim(cfg, rep, *, applied: bool) -> None:
    if not rep.checked:
        return
    if rep.unreachable:
        print(f"  {YELLOW}not connected: {', '.join(rep.unreachable)}{RESET}"
              f"{DIM} - photos needing those drives are held back{RESET}")
    if applied:
        print(f"  {GREEN}deleted {rep.deleted:,} files, freed "
              f"{human(rep.bytes_freed)}{RESET}")
    elif rep.safe:
        print(f"  {GREEN}{len(rep.safe):,} files ({human(rep.reclaimable_bytes)}) "
              f"have {cfg.min_copies} verified copies - safe to delete{RESET}")
    if rep.held:
        print(f"  {YELLOW}{len(rep.held):,} held back{RESET}")
        for path, _, why in rep.held[:5]:
            print(f"    {DIM}{path.name}: {why}{RESET}")
    if rep.safe and not applied:
        print(f"{DIM}  Re-run with --apply (or --reclaim) to delete them.{RESET}")


def cmd_duplicates(args) -> int:
    """Find near-duplicate photos. Deletes nothing without an explicit review."""
    cfg, cat = _load(args)

    if args.apply:
        rep = duplicates.apply(cfg, cat, dry_run=not args.yes)
        verb = "would delete" if not args.yes else "deleted"
        print(f"{verb} {rep.deleted:,} photos, freeing {human(rep.bytes_freed)}")
        for path, why in rep.refused[:15]:
            print(f"  {YELLOW}kept {Path(path).name}: {why}{RESET}")
        for err in rep.errors[:10]:
            print(f"  {RED}{err}{RESET}")
        if not args.yes and rep.deleted:
            print(f"\n{DIM}This was a preview. Re-run with --yes to delete.{RESET}")
        cat.close()
        return 0

    try:
        st = duplicates.scan(cfg, cat, limit=args.limit,
                             progress=lambda n, t: print(f"  hashing {n}/{t}",
                                                         end="\r", flush=True))
    except RuntimeError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    if st.hashed or st.failed:
        print(f"  analysed {st.hashed:,} photos"
              f"{f', {st.failed:,} could not be decoded' if st.failed else ''}"
              f"{f', {st.remaining:,} still to do' if st.remaining else ''}        ")

    groups = duplicates.find_groups(cfg, cat, threshold=args.threshold)
    if not groups:
        print(f"{GREEN}No near-duplicates found.{RESET}")
        cat.close()
        return 0

    total = sum(g.wasted_bytes for g in groups)
    print(f"\n{BOLD}{len(groups):,} groups{RESET} of near-duplicate photos, "
          f"{human(total)} recoverable\n")

    for group in groups[:args.show]:
        marked = sum(1 for m in group.members if m.action == "delete")
        tag = f"  {YELLOW}{marked} marked for deletion{RESET}" if marked else ""
        print(f"{BOLD}group {group.id}{RESET}  {len(group.members)} copies, "
              f"{human(group.wasted_bytes)} recoverable{tag}")
        for m in group.members:
            keep = m.hash == group.suggested_keep
            dims = f"{m.width}x{m.height}" if m.width else "?"
            mark = (f"{GREEN}keep{RESET}" if keep else f"{DIM}dup {RESET}")
            if m.action == "delete":
                mark = f"{RED}DEL {RESET}"
            print(f"  {mark} {dims:>11}  {human(m.size):>9}  "
                  f"{DIM}{m.copies} copies  {m.captured_at or 'no date'}{RESET}  "
                  f"{m.rel_path}")
        print()

    if len(groups) > args.show:
        print(f"{DIM}...and {len(groups) - args.show:,} more groups.{RESET}\n")
    print(f"{DIM}Review them visually with 'photovault ui' -> Duplicates, then\n"
          f"apply with 'photovault duplicates --apply --yes'.{RESET}")
    cat.close()
    return 0


def _resolve(cat, needles: list[str], *, trashed: bool = False) -> list[str]:
    """Accept a hash prefix or a path fragment; return matching asset hashes."""
    rows = cat.trashed() if trashed else cat.all_assets()
    found, unmatched = [], []
    for needle in needles:
        hits = [r["hash"] for r in rows
                if r["hash"].startswith(needle) or needle in r["rel_path"]]
        if hits:
            found.extend(hits)
        else:
            unmatched.append(needle)
    for needle in unmatched:
        print(f"  {YELLOW}nothing matched {needle!r}{RESET}")
    return sorted(set(found))


def cmd_delete(args) -> int:
    """Move photos to the trash. Files stay on disk until purged."""
    cfg, cat = _load(args)
    hashes = _resolve(cat, args.photo)
    if not hashes:
        cat.close()
        return 1

    print(f"{BOLD}Moving {len(hashes)} photo(s) to the trash:{RESET}")
    for h in hashes[:20]:
        a = cat.asset(h)
        print(f"  {a['rel_path']}  {DIM}{human(a['size'])}{RESET}")
    if len(hashes) > 20:
        print(f"  {DIM}...and {len(hashes) - 20} more{RESET}")

    if not args.yes:
        reply = input(f"\nMove these to the trash? They stay recoverable for "
                      f"{cfg.trash_days} days. [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("cancelled")
            cat.close()
            return 1

    st = trash.delete(cat, hashes)
    print(f"{GREEN}Moved {st.moved} photo(s) to the trash{RESET} "
          f"({human(st.bytes)} recoverable for {cfg.trash_days} days)")
    print(f"{DIM}Restore with 'photovault trash --restore <name>', or free the "
          f"space with 'photovault trash --purge'.{RESET}")
    cat.close()
    return 0


def cmd_trash(args) -> int:
    """List, restore or permanently remove trashed photos."""
    cfg, cat = _load(args)

    if args.restore:
        hashes = _resolve(cat, args.restore, trashed=True)
        n = trash.restore(cat, hashes)
        print(f"{GREEN}Restored {n} photo(s){RESET}")
        cat.close()
        return 0

    if args.purge:
        rep = trash.purge(cfg, cat, expired_only=not args.all,
                          dry_run=not args.yes)
        window = ("everything in the trash" if args.all
                  else f"items older than {cfg.trash_days} days")
        if rep.skipped:
            print(f"{YELLOW}Refusing to purge: {rep.skipped[0][1]}{RESET}")
            print(f"{DIM}Purging is permanent, so every device must be "
                  f"connected first.{RESET}")
            cat.close()
            return 1
        verb = "would permanently delete" if not args.yes else "permanently deleted"
        print(f"{verb} {rep.purged:,} photo(s) ({window}), "
              f"freeing {human(rep.bytes_freed)}")
        for err in rep.errors[:10]:
            print(f"  {RED}{err}{RESET}")
        if not args.yes and rep.purged:
            print(f"\n{DIM}This was a preview. Re-run with --yes to delete "
                  f"for good.{RESET}")
        cat.close()
        return 0

    s = trash.summary(cfg, cat)
    if not s["files"]:
        print("The trash is empty.")
        cat.close()
        return 0

    print(f"{BOLD}Trash{RESET}  {s['files']:,} photos, {human(s['bytes'])}")
    print(f"{DIM}Kept for {s['retention_days']} days after deletion.{RESET}\n")
    for row in cat.trashed()[:args.show]:
        print(f"  {row['deleted_at'][:10]}  {human(row['size']):>9}  "
              f"{row['rel_path']}")
    if s["files"] > args.show:
        print(f"  {DIM}...and {s['files'] - args.show:,} more{RESET}")
    if s["expiring"]:
        print(f"\n{YELLOW}{s['expiring']:,} are past the {s['retention_days']}-day "
              f"window and will be removed by 'photovault trash --purge'.{RESET}")
    cat.close()
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
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="write a starter config file")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("start",
                       help="start everything: Immich, the web UI, the watcher")
    s.add_argument("--port", type=int, default=8723)
    s.add_argument("--host", default="127.0.0.1",
                   help="default 127.0.0.1 - this machine only")
    s.add_argument("--watch", action="store_true",
                   help="also import automatically as photos arrive")
    s.add_argument("--no-immich", action="store_true", help="skip Immich")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="stop the services 'start' brought up")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("doctor", help="what is connected and what is missing")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="interactive setup - pick how drives store photos")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_setup)

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
                       help="recover a lost catalog by re-reading replicas")
    s.add_argument("replica", nargs="?")
    s.add_argument("--all", action="store_true",
                   help="read every local replica (required when sharded)")
    s.set_defaults(func=cmd_rebuild)

    s = sub.add_parser("ui", help="open the web interface in your browser")
    s.add_argument("--port", type=int, default=8723)
    s.add_argument("--host", default="127.0.0.1",
                   help="default 127.0.0.1 - this Mac only. Changing this "
                        "exposes your library to the network with no password.")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("adopt",
                       help="re-register a replaced drive under a replica name")
    s.add_argument("replica")
    s.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("plan", help="preview how the library splits across drives")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("rebalance",
                       help="reclaim space on a shard after the drives changed")
    s.add_argument("replica", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--apply", action="store_true",
                   help="actually delete; without this it only previews")
    s.set_defaults(func=cmd_rebalance)

    s = sub.add_parser("watch",
                       help="auto-import and back up as photos land in the inbox")
    s.add_argument("--interval", type=float, default=20.0,
                   help="seconds between checks (default 20)")
    s.add_argument("--clear", action="store_true",
                   help="empty every inbox after import, not just configured ones")
    s.add_argument("--once", action="store_true", help="one pass, then exit")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("install-agent",
                       help="run the watcher automatically from login (macOS)")
    s.add_argument("--interval", type=float, default=20.0)
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(func=cmd_install_agent)

    s = sub.add_parser("import",
                       help="pull from a device, archive it, and verify")
    s.add_argument("device", nargs="?", help="source device name")
    s.add_argument("--reclaim", action="store_true",
                   help="delete files that reach min_copies verified copies")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("reclaim",
                       help="which files are safe to delete from a device?")
    s.add_argument("device", nargs="?")
    s.add_argument("--apply", action="store_true", help="actually delete")
    s.set_defaults(func=cmd_reclaim)

    s = sub.add_parser("duplicates", help="find near-duplicate photos")
    s.add_argument("--threshold", type=int, default=duplicates.DEFAULT_THRESHOLD,
                   help="how many of 64 bits may differ (default 5)")
    s.add_argument("--limit", type=int, help="analyse at most N new photos")
    s.add_argument("--show", type=int, default=10, help="groups to print")
    s.add_argument("--apply", action="store_true",
                   help="act on decisions made in the UI")
    s.add_argument("--yes", action="store_true",
                   help="with --apply, actually delete instead of previewing")
    s.set_defaults(func=cmd_duplicates)

    s = sub.add_parser("delete", help="move photos to the trash")
    s.add_argument("photo", nargs="+",
                   help="hash prefix or part of a path")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("trash", help="list, restore or permanently remove trash")
    s.add_argument("--restore", nargs="+", metavar="PHOTO")
    s.add_argument("--purge", action="store_true",
                   help="permanently delete (previews unless --yes)")
    s.add_argument("--all", action="store_true",
                   help="with --purge, empty the trash rather than only expired items")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--show", type=int, default=20)
    s.set_defaults(func=cmd_trash)

    s = sub.add_parser("log", help="recent operations")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_log)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # No subcommand: do the thing someone typing `photovault` almost
        # certainly wants, rather than printing usage at them.
        args = parser.parse_args([*(["-c", args.config] if args.config else []),
                                  "start"])
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
