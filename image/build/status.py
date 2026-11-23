"""Atomic, redacted, local build status consumed by the CLI and optional UI."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import names
from .ports import PermanentError


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermanentError("refusing to replace a status symlink")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def latest(state_dir: Path) -> dict[str, Any] | None:
    return read_json(Path(state_dir) / "latest.json")


def list_builds(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(state_dir) / "builds"
    if not root.is_dir():
        return []
    found = [
        v
        for p in root.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and (v := read_json(p / "summary.json")) is not None
    ]
    return sorted(found, key=lambda v: str(v.get("finished_at", "")), reverse=True)[
        :limit
    ]


def show(state_dir: Path, version: str) -> dict[str, Any] | None:
    names.check_version(version)
    return read_json(Path(state_dir) / "builds" / version / "summary.json")


def run_log(state_dir: Path, run_id: str) -> Path:
    if names.parse_run_id(run_id) is None:
        raise PermanentError("invalid run id")
    root = (Path(state_dir) / "runs").resolve()
    path = root / run_id / "run.log"
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise PermanentError("run log leaves the state directory")
    return path
