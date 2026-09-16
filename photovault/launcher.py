"""`photovault start` - bring the whole system up with one command.

The monthly ritual has several moving parts: plug in the drives, start Immich,
open the UI, import, verify, then free up phone space. Most of that is
mechanical, and mechanical steps that a human has to remember are steps that
eventually get skipped.

So `start` does the mechanical part and, just as importantly, tells you the one
thing it cannot do for you: which drives are missing. A backup run with the
offline drive still in the drawer looks successful and silently achieves less
than you think, so the preflight report leads with it.
"""

from __future__ import annotations

import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .sync import available_replicas

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


@dataclass
class Preflight:
    reachable: list[str]
    missing_offline: list[str]
    missing_online: list[str]
    total_assets: int
    healthy: bool
    issues: list[str]

    @property
    def ready(self) -> bool:
        """Enough drives connected to actually reach the redundancy target."""
        return not self.missing_online


def preflight(cfg: Config) -> Preflight:
    from . import health

    reachable, missing_offline, missing_online = [], [], []
    for name, _drv, ok in available_replicas(cfg):
        if ok:
            reachable.append(name)
        elif cfg.replica(name).offline:
            missing_offline.append(name)
        else:
            missing_online.append(name)

    cat = Catalog(cfg.catalog_path)
    try:
        h = health.assess(cfg, cat)
        issues = [c for c in (
            f"{h.at_risk_single_copy:,} photos exist in only one place"
            if h.at_risk_single_copy else "",
            f"{h.underprotected:,} photos below {cfg.min_copies} copies"
            if h.underprotected else "",
            f"{h.corrupt:,} photos have a corrupt copy" if h.corrupt else "",
        ) if c]
        return Preflight(reachable, missing_offline, missing_online,
                         h.total_assets, h.ok, issues)
    finally:
        cat.close()


def print_preflight(cfg: Config, pf: Preflight) -> None:
    # Pad before colouring: escape codes are zero-width on screen but count
    # toward format widths.
    def row(label: str, colour: str, name: str, note: str = "") -> None:
        print(f"  {colour}{label}{RESET}{' ' * (15 - len(label))}"
              f"{name}{DIM}{note}{RESET}")

    print(f"{BOLD}Devices{RESET}")
    for name in pf.reachable:
        row("connected", GREEN, name,
            " (primary)" if name == cfg.primary else "")
    for name in pf.missing_offline:
        row("not plugged in", YELLOW, name)
    for name in pf.missing_online:
        row("unreachable", RED, name)

    if pf.total_assets:
        state = (f"{GREEN}fully protected{RESET}" if pf.healthy
                 else f"{RED}needs attention{RESET}")
        print(f"\n{BOLD}Library{RESET}  {pf.total_assets:,} photos - {state}")
        for issue in pf.issues:
            print(f"  {RED}-{RESET} {issue}")
    else:
        print(f"\n{DIM}Library is empty - run 'photovault import' once a source "
              f"is configured.{RESET}")

    if pf.missing_offline:
        print(f"\n{YELLOW}Plug in {', '.join(pf.missing_offline)} before syncing, "
              f"or those copies will not be made.{RESET}")


# ---------------------------------------------------------------------- Immich

def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def compose(cfg: Config, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    path = cfg.immich.path
    return subprocess.run(
        ["docker", "compose", "-f", str(path), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(path.parent))


def immich_up(cfg: Config, *, wait: float = 120.0) -> bool:
    """Start Immich and block until it answers, so the UI never links to a
    service that is still migrating its database."""
    path = cfg.immich.path
    if path is None or not path.is_file():
        print(f"  {YELLOW}compose file not found: {path}{RESET}")
        return False
    if not docker_available():
        print(f"  {YELLOW}Docker is not running - skipping Immich{RESET}")
        return False

    print(f"  starting containers...")
    r = compose(cfg, "up", "-d")
    if r.returncode != 0:
        print(f"  {RED}docker compose failed:{RESET}\n    {r.stderr.strip()[:400]}")
        return False

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if immich_ready(cfg):
            print(f"  {GREEN}ready{RESET} at {cfg.immich.url}")
            return True
        time.sleep(2)
    print(f"  {YELLOW}started, but not answering yet at {cfg.immich.url}"
          f" - it may still be migrating{RESET}")
    return False


def immich_ready(cfg: Config) -> bool:
    for path in ("/api/server/ping", "/api/server-info/ping", "/"):
        try:
            with urllib.request.urlopen(cfg.immich.url.rstrip("/") + path,
                                        timeout=3) as r:
                if r.status < 500:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def immich_down(cfg: Config) -> bool:
    if not cfg.immich.enabled or not docker_available():
        return False
    r = compose(cfg, "down")
    return r.returncode == 0


# ----------------------------------------------------------------------- start

def start(cfg: Config, *, port: int = 8723, host: str = "127.0.0.1",
          open_browser: bool = True, watch: bool = False,
          with_immich: bool = True, config_path: Path | None = None) -> int:
    import sys

    from .web.server import serve

    # Python block-buffers stdout when it is not a terminal, so under nohup or
    # launchd the preflight would sit invisible in a buffer while the user
    # wonders whether anything started.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    print(f"\n{BOLD}PhotoVault{RESET}\n")
    pf = preflight(cfg)
    print_preflight(cfg, pf)

    if with_immich and cfg.immich.enabled:
        print(f"\n{BOLD}Immich{RESET}")
        immich_up(cfg)
    elif with_immich and not cfg.immich.enabled:
        print(f"\n{DIM}No Immich configured. Add an [immich] section with a "
              f"compose_file to start it here too.{RESET}")

    stop_watcher = threading.Event()
    if watch:
        from . import watcher
        print(f"\n{BOLD}Watcher{RESET}\n  importing automatically as photos arrive")

        def loop():
            cat = Catalog(cfg.catalog_path)
            try:
                while not stop_watcher.is_set():
                    try:
                        watcher.run_once(cfg, cat, report=lambda *_: None)
                    except Exception:
                        pass      # a transient failure must not kill the UI
                    stop_watcher.wait(30)
            finally:
                cat.close()

        threading.Thread(target=loop, daemon=True).start()

    print(f"\n{BOLD}Next{RESET}")
    if pf.missing_offline:
        print(f"  1. plug in {', '.join(pf.missing_offline)}")
    print(f"  {'2' if pf.missing_offline else '1'}. photovault import"
          f"       {DIM}# pull from your devices and back up{RESET}")
    print(f"  {'3' if pf.missing_offline else '2'}. photovault reclaim"
          f"      {DIM}# what is safe to delete from the phone{RESET}")
    print()

    try:
        serve(cfg, host=host, port=port, open_browser=open_browser,
              config_path=config_path)
    finally:
        stop_watcher.set()
        if cfg.immich.enabled:
            print(f"{DIM}Immich is still running. Stop it with: "
                  f"photovault stop{RESET}")
    return 0
