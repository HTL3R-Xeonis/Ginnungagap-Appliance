"""
The host side of the setup wizard's hand-off (see ginnungagap/setup_wizard.py for the container side, ginnungagap/
bootstrap.py for how the appliance is reachable before any of this has ever run).

The wizard's container writes a candidate `ginnungagap.yml` and a marker file into a folder only it and this module
touch; this module is run by install.sh's `ginnungagap-setup-apply.path`/`.service` (a systemd path unit, triggered the
moment the marker appears - no polling, no daemon). It never trusts the container's own validation: the file is
re-checked here, on the host, with the exact same `config.normalise` the command line uses, before it is allowed to
become the live configuration. An invalid staged file never touches `ginnungagap.yml` - the appliance keeps running
whatever it was running before, exactly like a bad `ginnungagap apply` from the command line would.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ginnungagap import config as config_module
from ginnungagap import operate as operate_module
from ginnungagap import statelock

MARKER = "apply.request"
STAGED = "ginnungagap.yml"
REJECTED = "ginnungagap.yml.rejected"

Apply = Callable[..., int]

# the staging folder is writable by the wizard's container, so everything in it is untrusted input to a root process:
# no symlinks (they could point at any host file), and a size cap
MAX_STAGED_BYTES = 1_000_000


def _read_regular(path: Path, limit: int = MAX_STAGED_BYTES) -> str | None:
    """The file's text, or None when it is a symlink, not a regular file or larger than `limit` - never follows a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        try:
            raw = handle.read(limit + 1)
            return raw.decode("utf-8") if len(raw) <= limit else None
        except UnicodeDecodeError:
            return None


def _write_private(path: Path, text: str) -> None:
    """Replaces whatever is at `path` (a planted symlink included, which is removed, not followed) with a 0600 file."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def apply_staged(
    staging_dir: str | Path,
    config_file: str | Path,
    bundle: Path,
    generator_config: str | Path | None,
    apply: Apply = operate_module.apply,
    out: Callable[[str], None] = print,
    *,
    build: bool = True,
    lock_timeout: float = 540.0,
) -> int:
    """
    Applies what the wizard staged, but never while `ginnungagap update` is running: it waits for the update lock (up to
    `lock_timeout` seconds, the service unit's own limit is 600) and, when that runs out, leaves the marker and the staged
    file where they are and returns 1, so nothing is lost and the submission is taken up again by the next trigger.
    """
    if (
        not (Path(staging_dir) / MARKER).exists()
        and not (Path(staging_dir) / MARKER).is_symlink()
    ):
        return 0  # nothing staged: nothing to wait for
    try:
        with statelock.hold(timeout=lock_timeout, out=out):
            return _apply_staged(
                staging_dir,
                config_file,
                bundle,
                generator_config,
                apply,
                out,
                build=build,
            )
    except statelock.LockTimeout as exc:
        out(f"the staged configuration is kept for the next try: {exc}")
        return 1


def _apply_staged(
    staging_dir: str | Path,
    config_file: str | Path,
    bundle: Path,
    generator_config: str | Path | None,
    apply: Apply,
    out: Callable[[str], None],
    *,
    build: bool,
) -> int:
    """
    0: nothing was staged, or it was and is now the live configuration, applied. 1: something was staged but was not
    valid (kept aside as `ginnungagap.yml.rejected`, same name firstboot.py uses for the same reason) or `apply` itself
    failed - either way the marker and the staged file are always consumed, so a bad submission is never retried in a
    loop; the admin has to submit again.
    """
    staging_dir = Path(staging_dir)
    marker = staging_dir / MARKER
    staged = staging_dir / STAGED
    if not marker.exists() and not marker.is_symlink():
        return 0
    text = None if staged.is_symlink() else _read_regular(staged)
    if text is None:
        out(
            f"{marker} exists but {staged} is missing, not a plain text file or too large - ignoring it"
        )
        marker.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        return 1
    config: dict[str, Any] | None
    try:
        config = config_module.normalise(yaml.safe_load(text))
        problems: list[str] = []
    except yaml.YAMLError:
        config, problems = None, ["The submitted configuration is not valid YAML."]
    except config_module.ConfigError as exc:
        config, problems = None, list(exc.problems)

    marker.unlink(missing_ok=True)
    staged.unlink(missing_ok=True)
    if config is None:
        out(
            "the configuration staged by the setup wizard is not valid - the current one is kept:"
        )
        for problem in problems:
            out(f"  - {problem}")
        _write_private(staging_dir / REJECTED, text)
        return 1

    config_file = Path(config_file)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_file.with_name(config_file.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(config_file)
    out(
        f"configuration from the setup wizard accepted, writing {config_file} and applying it"
    )
    return apply(config_file, bundle, generator_config, out=out, build=build)
