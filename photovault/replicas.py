"""Replica drivers.

A driver knows how to do five things to a copy of the library: say whether it
is reachable, list what it holds, accept new files, hash a file it holds, and
hand a file back for repair. Everything above this layer is driver-agnostic,
which is what makes adding Google Cloud Storage later a contained change.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .config import ReplicaSpec
from .hashing import ALGO, hash_file

# Remote hashing must agree with whatever we use locally.
_REMOTE_HASH_CMD = {"sha256": "sha256sum", "blake3": "b3sum"}

# Written into every replica root so a drive can prove which replica it is.
MARKER_NAME = ".photovault-id"


class ReplicaError(RuntimeError):
    pass


class Driver(ABC):
    def __init__(self, spec: ReplicaSpec):
        self.spec = spec
        self.name = spec.name

    @abstractmethod
    def available(self) -> bool:
        """Is this replica reachable right now?"""

    @abstractmethod
    def list_present(self) -> set[str]:
        """Relative paths currently stored."""

    @abstractmethod
    def put(self, src: Path, rel_path: str) -> None:
        """Copy a local file in, creating parent directories."""

    @abstractmethod
    def hash_of(self, rel_path: str) -> str | None:
        """Hash the stored bytes, or None if the file is absent."""

    @abstractmethod
    def get(self, rel_path: str, dest: Path) -> None:
        """Copy a stored file out to a local path (used for repair)."""

    @abstractmethod
    def delete(self, rel_path: str) -> None:
        """Remove a stored file. Only ever called by rebalance, and only after
        the caller has proved enough verified copies survive elsewhere."""

    @abstractmethod
    def read_marker(self) -> str | None:
        """Raw contents of the identity marker, or None if unmarked."""

    @abstractmethod
    def write_marker(self, payload: str) -> None:
        """Stamp this storage with an identity marker."""


class LocalDriver(Driver):
    """A path on this machine: internal disk, external HDD, or a mounted share."""

    @property
    def root(self) -> Path:
        return Path(self.spec.root).expanduser()

    def available(self) -> bool:
        return _mount_guard_ok(self.root)

    def ensure_root(self) -> None:
        if not _mount_guard_ok(self.root):
            raise ReplicaError(
                f"{self.name}: {self.root} is not on a mounted volume - "
                f"refusing to create it on the boot disk")
        self.root.mkdir(parents=True, exist_ok=True)

    def list_present(self) -> set[str]:
        root = self.root
        if not root.is_dir():
            return set()
        return {
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        }

    def put(self, src: Path, rel_path: str) -> None:
        dest = self.root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copy2(src, tmp)
        tmp.replace(dest)  # atomic within a filesystem: no half-written photos

    def hash_of(self, rel_path: str) -> str | None:
        path = self.root / rel_path
        if not path.is_file():
            return None
        return hash_file(path)[0]

    def get(self, rel_path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / rel_path, dest)

    def delete(self, rel_path: str) -> None:
        (self.root / rel_path).unlink(missing_ok=True)
        # Tidy the now-empty date folders so the tree stays browsable.
        parent = (self.root / rel_path).parent
        while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def read_marker(self) -> str | None:
        try:
            return (self.root / MARKER_NAME).read_text()
        except OSError:
            return None

    def write_marker(self, payload: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MARKER_NAME).write_text(payload)


class RsyncDriver(Driver):
    """Another machine over SSH - the Windows laptop, or a second Mac.

    On Windows this expects an OpenSSH server plus Git Bash or WSL so that
    rsync and a hashing binary exist. Setup notes are in the README.
    """

    def __init__(self, spec: ReplicaSpec):
        super().__init__(spec)
        if not spec.host:
            raise ReplicaError(f"replica {spec.name!r} is kind=rsync but has no host")
        self.host = spec.host
        self.root = spec.root.rstrip("/")

    def _ssh(self, command: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, command],
            capture_output=True, text=True, timeout=timeout,
        )

    def available(self) -> bool:
        try:
            return self._ssh("echo ok", timeout=15).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def list_present(self) -> set[str]:
        r = self._ssh(f"cd {_q(self.root)} 2>/dev/null && find . -type f ! -name '.*'",
                      timeout=600)
        if r.returncode != 0:
            return set()
        return {
            line[2:] for line in r.stdout.splitlines()
            if line.startswith("./") and not line.endswith(".part")
        }

    def put(self, src: Path, rel_path: str) -> None:
        target = f"{self.root}/{rel_path}"
        mk = self._ssh(f"mkdir -p {_q(str(Path(target).parent))}")
        if mk.returncode != 0:
            raise ReplicaError(f"{self.name}: mkdir failed: {mk.stderr.strip()}")
        r = subprocess.run(
            ["rsync", "-a", "--partial", "--inplace", str(src),
             f"{self.host}:{target}"],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            raise ReplicaError(f"{self.name}: rsync failed: {r.stderr.strip()}")

    def hash_of(self, rel_path: str) -> str | None:
        cmd = _REMOTE_HASH_CMD.get(ALGO)
        if not cmd:
            raise ReplicaError(f"no remote hasher known for {ALGO}")
        r = self._ssh(f"{cmd} {_q(self.root + '/' + rel_path)} 2>/dev/null", timeout=300)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return r.stdout.split()[0]

    def get(self, rel_path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["rsync", "-a", f"{self.host}:{self.root}/{rel_path}", str(dest)],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            raise ReplicaError(f"{self.name}: rsync pull failed: {r.stderr.strip()}")

    def delete(self, rel_path: str) -> None:
        target = self.root + "/" + rel_path
        if self._ssh(f"rm -f {_q(target)}").returncode != 0:
            raise ReplicaError(f"{self.name}: could not delete {rel_path}")

    def read_marker(self) -> str | None:
        r = self._ssh(f"cat {_q(self.root + '/' + MARKER_NAME)} 2>/dev/null", timeout=30)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None

    def write_marker(self, payload: str) -> None:
        import base64
        blob = base64.b64encode(payload.encode()).decode()
        cmd = (f"mkdir -p {_q(self.root)} && echo {_q(blob)} | "
               f"base64 -d > {_q(self.root + '/' + MARKER_NAME)}")
        if self._ssh(cmd).returncode != 0:
            raise ReplicaError(f"{self.name}: could not write identity marker")


class GCSDriver(Driver):
    """Offsite copy in Google Cloud Storage. Not wired up yet - the interface
    is here so the rest of the system already treats it as just another
    replica, and enabling it is a matter of filling in these five methods."""

    def available(self) -> bool:
        return False

    def list_present(self) -> set[str]:
        raise NotImplementedError("GCS replica is not enabled yet")

    def put(self, src: Path, rel_path: str) -> None:
        raise NotImplementedError("GCS replica is not enabled yet")

    def hash_of(self, rel_path: str) -> str | None:
        raise NotImplementedError("GCS replica is not enabled yet")

    def get(self, rel_path: str, dest: Path) -> None:
        raise NotImplementedError("GCS replica is not enabled yet")

    def delete(self, rel_path: str) -> None:
        raise NotImplementedError("GCS replica is not enabled yet")

    def read_marker(self) -> str | None:
        return None

    def write_marker(self, payload: str) -> None:
        raise NotImplementedError("GCS replica is not enabled yet")


_DRIVERS = {"local": LocalDriver, "rsync": RsyncDriver, "gcs": GCSDriver}


def driver_for(spec: ReplicaSpec) -> Driver:
    try:
        return _DRIVERS[spec.kind](spec)
    except KeyError:
        raise ReplicaError(f"unknown replica kind {spec.kind!r}") from None


def _q(path: str) -> str:
    """Single-quote a path for a remote shell."""
    return "'" + path.replace("'", "'\\''") + "'"


def _mount_guard_ok(root: Path) -> bool:
    """Refuse to treat an unplugged external drive as if it were present.

    On macOS an unmounted drive leaves /Volumes/<Name> either absent or, worse,
    as an ordinary empty folder on the boot disk. Writing there would silently
    fill up the internal SSD while reporting a healthy backup. A real mount has
    a different st_dev from its parent, so that is what we check.
    """
    root = root.expanduser()
    parts = root.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        mount = Path(parts[0], parts[1], parts[2])
        if not mount.is_dir():
            return False
        try:
            return mount.stat().st_dev != mount.parent.stat().st_dev
        except OSError:
            return False

    # Anywhere else (home, /tmp, an SMB mount): it is enough that some existing
    # ancestor is a directory we could create the rest of the tree under.
    p = root
    while not p.exists() and p != p.parent:
        p = p.parent
    return p.is_dir()
