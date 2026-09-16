"""Interactive setup: choose a storage model without hand-editing TOML.

The one decision that genuinely cannot be defaulted is full copies versus
sharding, because it is a trade rather than a right answer:

* **Full copies** - every drive independently holds everything. Any single
  drive restores your whole library on its own. Needs each drive to be as big
  as the library.
* **Sharded** - drives each hold a computed subset, so small drives combine to
  hold a library none of them could hold alone. Restoring needs all of them.

This asks, shows the consequence, and writes the config.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .placement import HEADROOM

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


@dataclass
class Candidate:
    label: str
    path: Path
    total: int
    free: int


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def find_drives() -> list[Candidate]:
    """External volumes we could use, newest mounts first."""
    out: list[Candidate] = []
    roots = Path("/Volumes")
    if not roots.is_dir():
        return out
    boot_dev = Path("/").stat().st_dev
    for entry in sorted(roots.iterdir()):
        try:
            if not entry.is_dir() or entry.stat().st_dev == boot_dev:
                continue  # skip the boot disk and its firmlinks
            usage = shutil.disk_usage(entry)
        except OSError:
            continue
        out.append(Candidate(entry.name, entry, usage.total, usage.free))
    return out


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        reply = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return reply or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    reply = ask(f"{prompt} ({d})").lower()
    if not reply:
        return default
    return reply.startswith("y")


def choose(prompt: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Numbered menu. Returns a 1-based index."""
    print(f"\n{BOLD}{prompt}{RESET}")
    for i, (label, detail) in enumerate(options, 1):
        mark = "*" if i == default else " "
        print(f" {mark}{i}. {BOLD}{label}{RESET}")
        for line in detail.splitlines():
            print(f"      {DIM}{line}{RESET}")
    while True:
        reply = ask("\nChoose", str(default))
        if reply.isdigit() and 1 <= int(reply) <= len(options):
            return int(reply)
        print(f"{RED}Enter a number between 1 and {len(options)}.{RESET}")


def run(config_path: Path, *, force: bool = False) -> int:
    if config_path.exists() and not force:
        if not ask_yes(f"{config_path} already exists. Replace it?", False):
            print("cancelled")
            return 1
    if not sys.stdin.isatty():
        print(f"{RED}setup needs an interactive terminal. "
              f"Use 'photovault init' to write a config you can edit.{RESET}")
        return 1

    print(f"\n{BOLD}PhotoVault setup{RESET}")
    print(f"{DIM}Nothing is written until the end, and nothing is copied or "
          f"deleted.{RESET}")

    # ---------------------------------------------------------- 1. the library
    library_size = _ask_library_size()

    # ------------------------------------------------------------- 2. sources
    sources = _ask_sources()

    # -------------------------------------------------------------- 3. drives
    drives = find_drives()
    if drives:
        print(f"\n{BOLD}External drives found{RESET}")
        for d in drives:
            print(f"  {d.label:<22}{human(d.total):>10} total, "
                  f"{human(d.free):>10} free")
    else:
        print(f"\n{YELLOW}No external drives are mounted right now.{RESET}")
        print(f"{DIM}You can add them to the config later, or plug one in and "
              f"re-run setup.{RESET}")

    chosen: list[Candidate] = []
    for d in drives:
        if ask_yes(f"  Use '{d.label}' for backups?", True):
            chosen.append(d)

    # ------------------------------------------------------- 4. storage model
    smallest = min((d.free for d in chosen), default=0)
    fits = library_size is None or smallest >= library_size
    model = _ask_model(chosen, library_size, fits)

    # ------------------------------------------------------------- 5. copies
    total_devices = 1 + len(chosen)
    default_copies = min(3, max(2, total_devices))
    copies = int(ask(f"\nHow many copies of every photo?", str(default_copies))
                 or default_copies)

    # ------------------------------------------------------------ 6. windows
    windows = None
    if ask_yes("\nAdd a Windows laptop over the network?", False):
        host = ask("  SSH host (user@address)")
        root = ask("  Path on that machine", "/d/PhotoVault/library")
        if host:
            windows = (host, root)

    text = _render(library_root=Path.home() / "PhotoVault" / "library",
                   sources=sources, drives=chosen, model=model,
                   copies=copies, windows=windows)

    print(f"\n{BOLD}Config to be written to {config_path}{RESET}")
    print(f"{DIM}{'-' * 60}{RESET}")
    print(text.rstrip())
    print(f"{DIM}{'-' * 60}{RESET}")
    if not ask_yes("\nWrite this?", True):
        print("cancelled - nothing written")
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text)
    print(f"\n{GREEN}Written to {config_path}{RESET}\n")
    print("Next:")
    print(f"  photovault scan     {DIM}# see what is out there, change nothing{RESET}")
    if model == "shard":
        print(f"  photovault plan     {DIM}# check the split fits{RESET}")
    print(f"  photovault ingest   {DIM}# import into the primary library{RESET}")
    print(f"  photovault sync --all")
    return 0


def _ask_library_size() -> int | None:
    print(f"\n{BOLD}How big is your photo collection?{RESET}")
    print(f"{DIM}A rough number is fine - it only decides which storage models "
          f"are sensible.\\nLeave blank if you do not know.{RESET}")
    reply = ask("  Approximate size (e.g. 400GB, 1.2TB)")
    if not reply:
        return None
    from .placement import parse_size
    try:
        return parse_size(reply)
    except ValueError:
        print(f"{YELLOW}  Could not read that; continuing without it.{RESET}")
        return None


def _ask_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    photos = Path.home() / "Pictures" / "Photos Library.photoslibrary" / "originals"
    if photos.exists() and ask_yes(
            f"\nImport from your Apple Photos library?", True):
        sources.append(("mac", "~/Pictures/Photos Library.photoslibrary/originals"))

    if ask_yes("Create inbox folders for your iPhone and iPad?", True):
        sources.append(("iphone", "~/PhotoVault/inbox/iphone"))
        sources.append(("ipad", "~/PhotoVault/inbox/ipad"))

    while ask_yes("Add another folder of photos?", False):
        device = ask("  A short name for where these came from", "other")
        path = ask("  Folder path")
        if path:
            sources.append((device, path))
    return sources


def _ask_model(drives: list[Candidate], library_size: int | None,
               fits: bool) -> str:
    if len(drives) < 2:
        return "full"   # sharding needs at least two drives to be meaningful

    detail_full = ("Each drive holds your entire library.\n"
                   "Any single drive can restore everything on its own.\n"
                   "Needs every drive to be as big as the library.")
    detail_shard = ("Drives each hold a portion, so small drives combine.\n"
                    "Restoring needs all of them - no single drive is complete.\n"
                    "Use this when no one drive is big enough.")

    if library_size and not fits:
        smallest = min(d.free for d in drives)
        print(f"\n{YELLOW}Your smallest selected drive has {human(smallest)} free, "
              f"but the library is about {human(library_size)}.{RESET}")
        print(f"{DIM}Full copies will not fit; sharding is suggested below.{RESET}")
        default = 2
    else:
        default = 1

    pick = choose("How should the drives store your photos?",
                  [("Full copies on every drive (safest)", detail_full),
                   ("Split across drives (sharded)", detail_shard)],
                  default=default)
    return "full" if pick == 1 else "shard"


def _render(*, library_root: Path, sources, drives, model, copies, windows) -> str:
    lines = [
        "# PhotoVault configuration, written by 'photovault setup'.",
        "#",
        "# Every photo must exist on `min_copies` independent devices, at least",
        "# one of them normally unplugged.",
        "",
        "[vault]",
        f'primary = "mac"',
        f"min_copies = {copies}",
        "require_offline_copy = true",
        "scrub_days = 30",
        "",
        "# ---------------------------------------------------------- replicas",
        "",
        "[[replica]]",
        'name = "mac"',
        'kind = "local"',
        f'root = "~/PhotoVault/library"',
    ]
    for i, d in enumerate(drives, 1):
        lines += [
            "",
            "[[replica]]",
            f'name = "hdd{i}"',
            'kind = "local"',
            f'root = "{d.path}/PhotoVault/library"',
            "offline = true",
        ]
        if model == "shard":
            lines += ['mode = "shard"', 'capacity = "auto"']
    if windows:
        host, root = windows
        lines += ["", "[[replica]]", 'name = "win"', 'kind = "rsync"',
                  f'host = "{host}"', f'root = "{root}"']

    lines += ["", "# ----------------------------------------------------------- sources",
              "# Read-only: PhotoVault copies out of these and never modifies them.", ""]
    if not sources:
        lines += ["# [[source]]", '# device = "mac"', '# path = "~/Pictures"']
    for device, path in sources:
        lines += ["[[source]]", f'device = "{device}"', f'path = "{path}"', ""]
    return "\n".join(lines).rstrip() + "\n"
