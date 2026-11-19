"""
The one lock that keeps an update of the installed components and a configuration apply from running at the same time.

It is an advisory `flock` on `<update state folder>/lock` (default `/var/lib/ginnungagap/update/lock`, the folder
`install.sh` creates; `GINNUNGAGAP_UPDATE_DIR` moves it, which tests use). `ginnungagap update` holds it for the whole
run and `apply-staged` (the setup wizard's hand-off) waits for it, so a wizard submit never restarts services in the middle
of an update. The kernel drops the lock when the process ends, so a crashed run never leaves a stale lock behind.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_UPDATE_DIR = "/var/lib/ginnungagap/update"
LOCK_NAME = "lock"


class LockTimeout(RuntimeError):
    """Another update or apply still held the lock when the waiting time ran out."""


def update_dir() -> Path:
    return Path(os.environ.get("GINNUNGAGAP_UPDATE_DIR", DEFAULT_UPDATE_DIR))


def lock_path() -> Path:
    return update_dir() / LOCK_NAME


@contextmanager
def hold(
    path: str | Path | None = None,
    timeout: float = 540.0,
    out: Callable[[str], None] = print,
    poll: float = 1.0,
) -> Iterator[bool]:
    """
    Holds the lock for the `with` block and yields True. Waits up to `timeout` seconds for another holder.
    When the state folder can't be created (a machine without the update state, for example a developer's checkout), the
    block runs without a lock and yields False: there is no update to collide with there.
    :raises LockTimeout: the lock was still taken after `timeout` seconds.
    """
    target = Path(path) if path is not None else lock_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        yield False
        return
    try:
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"another update or apply holds {target}; try again when it has finished"
                    ) from None
                if not announced:
                    out(
                        "an update or another apply is running - waiting for it to finish"
                    )
                    announced = True
                time.sleep(poll)
        yield True
    finally:
        os.close(fd)  # closing the descriptor releases the lock
