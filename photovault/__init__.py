"""PhotoVault - a distributed, cloud-free photo library with verifiable backups."""

import sys

__version__ = "0.1.0"

MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    # Without this the first thing a newcomer sees is "No module named
    # 'tomllib'", which says nothing about the actual problem. macOS still
    # ships 3.9, so this is the most likely first experience of the program.
    sys.exit(
        f"PhotoVault needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, "
        f"but this is {sys.version.split()[0]} ({sys.executable}).\n"
        f"\n"
        f"macOS still ships Python 3.9, which is too old. Install a current one:\n"
        f"    brew install python\n"
        f"then run PhotoVault with that interpreter, for example:\n"
        f"    /opt/homebrew/bin/python3 -m photovault status\n"
    )
