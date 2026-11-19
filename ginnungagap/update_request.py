"""
The host side of the web "Check for updates" button (Igdrasyl's admin panel, section Updates; Idynoun does the work).

The Operator's container has no Docker socket and no root. It mounts the update state read-only (to show what is installed,
what a check found and how a running update is doing) and one request folder read-write. An admin's click writes ONE small
file there (`request.json`); the host's own `ginnungagap-update-request.path` unit sees it and runs this module as root - the
same hand-off pattern as the setup wizard (ginnungagap/setup_watcher.py), and the update itself is the one command line
`ginnungagap update` runs, nothing of it is re-implemented here.

The request folder is writable by the container, so everything in it is untrusted input to a root process: the file is read
without following links, size-capped, consumed at once and parsed strictly (only the keys below, only `check` or `apply`,
for an apply only the version the admin was shown and an explicit confirmation). A request can never choose a source, a
folder, `--force`, `--no-backup` or a downgrade: all it can do is what an admin typing `ginnungagap update --check` or
`ginnungagap update --apply --version X --yes` could do, and that installs a signed release from the configured source
only. Every request - accepted or refused - leaves a line in the update audit log naming who asked (`web:<user name>`), and
the result of the last one is written to `web-status.json` in the update state folder for the panel to show.

An apply is never started by anything but such a request: the daily check timer only looks, it never installs.
"""

from __future__ import annotations

import json
import os
import re
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ginnungagap import manifest as mf
from ginnungagap import setup_watcher
from ginnungagap import update as update_module

REQUEST = "request.json"
STATUS = "web-status.json"
MAX_REQUEST_BYTES = 4096
MAX_LOG_LINES = 30
MAX_LOG_LINE = 300
MAX_MESSAGE = 600
ACTIONS = ("check", "apply")
KEYS = frozenset({"id", "action", "requested_by", "role", "version", "confirmed", "at"})
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_USER = re.compile(r"^[A-Za-z0-9._-]{1,60}$")  # the Operator's own user name rule

RunUpdate = Callable[..., int]


class Refused(Exception):
    """A request that is not acted on; the text is what the panel shows."""


def parse_request(text: str) -> dict[str, Any]:
    """The request as a dictionary, or :raises Refused: it is not exactly what the Operator's button writes."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Refused("the request is not valid JSON") from exc
    if not isinstance(data, dict):
        raise Refused("the request is not a JSON object")
    unknown = sorted(set(data) - KEYS)
    if unknown:
        raise Refused(f"the request has unknown fields: {', '.join(unknown)}")
    request_id = data.get("id")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise Refused("the request has no valid id")
    action = data.get("action")
    if action not in ACTIONS:
        raise Refused(f"the request asks for '{action}'; only check and apply exist")
    user = data.get("requested_by")
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise Refused("the request does not name who asked")
    if data.get("role") != "admin":
        raise Refused("only an administrator may ask for an update")
    if action == "apply":
        version = data.get("version")
        if not isinstance(version, str) or not mf.is_semver(version):
            raise Refused(
                "an apply names the release the admin was shown (a version number)"
            )
        if data.get("confirmed") is not True:
            raise Refused("an apply needs the admin's explicit confirmation")
    elif data.get("version") is not None or data.get("confirmed") is not None:
        raise Refused("a check takes no version and no confirmation")
    return data


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Capture:
    """Everything the update prints, kept (the last lines only) for the status file and passed on to the journal."""

    def __init__(self, out: Callable[[str], None]) -> None:
        self.lines: list[str] = []
        self.errors: list[str] = []
        self._out = out

    def say(self, text: str = "") -> None:
        self._out(text)
        self.lines.extend(line for line in str(text).splitlines() if line.strip())

    def err(self, text: str) -> None:
        self._out(text)
        self.errors.extend(line for line in str(text).splitlines() if line.strip())
        self.lines.extend(line for line in str(text).splitlines() if line.strip())

    def tail(self) -> list[str]:
        return [_short(line, MAX_LOG_LINE) for line in self.lines[-MAX_LOG_LINES:]]


def _outcome(action: str, code: int) -> tuple[str, str]:
    """(state, outcome) for an exit code of `ginnungagap update`: state is what the panel's busy indicator needs."""
    if action == "check" and code in (
        update_module.EXIT_OK,
        update_module.EXIT_AVAILABLE,
    ):
        return (
            "done",
            "update_available"
            if code == update_module.EXIT_AVAILABLE
            else "up_to_date",
        )
    if code == update_module.EXIT_OK:
        return "done", "installed"
    if code == update_module.EXIT_ROLLED_BACK:
        return "failed", "rolled_back"
    if code == update_module.EXIT_ROLLBACK_FAILED:
        return "failed", "rollback_failed"
    if code == update_module.EXIT_REFUSED:
        return "refused", "refused"
    return "failed", "failed"


def _audit(
    update_dir: Path, who: str, action: str, result: str, detail: str, now: datetime
) -> None:
    """The same JSON line the update engine writes (ts, who, action, result, detail), for what never reaches the engine."""
    record = {
        "ts": mf.timestamp(now),
        "who": who,
        "action": action,
        "result": result,
        "detail": _short(detail, 300),
    }
    try:
        update_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            update_dir / "audit.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _write_status(update_dir: Path, status: dict[str, Any]) -> None:
    try:
        mf.write_json_atomic(update_dir / STATUS, status, mode=0o644)
    except OSError:
        pass  # the panel then shows no result; the audit log and the journal still have it


@contextmanager
def _requester_in_environment(who: str):
    """The re-executed program of an update that replaces the tooling reads the requester from the environment."""
    key = "GINNUNGAGAP_UPDATE_REQUESTER"
    before = os.environ.get(key)
    os.environ[key] = who
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before


def _refuse(
    update_dir: Path,
    out: Callable[[str], None],
    now: Callable[[], datetime],
    reason: str,
    message: str,
) -> int:
    moment = mf.timestamp(now())
    out(f"update request refused: {reason}")
    _audit(update_dir, "web:unknown", "web-request", "refused", reason, now())
    _write_status(
        update_dir,
        {
            "id": None,
            "action": None,
            "requested_by": None,
            "version": None,
            "state": "refused",
            "outcome": "refused",
            "started": moment,
            "finished": moment,
            "exit_code": None,
            "message": message,
            "log": [],
        },
    )
    return 1


def process(
    requests_dir: str | Path,
    update_dir: str | Path,
    config_file: str | Path,
    bundle: Path,
    run: RunUpdate = update_module.run_update,
    out: Callable[[str], None] = print,
    *,
    env: update_module.Env | None = None,
    layout: update_module.Layout | None = None,
    now: Callable[[], datetime] = mf.now_utc,
) -> int:
    """
    Acts on the request the Operator's button left, if any. 0: nothing was asked, or it was done (a check that found an
    update counts); 1: the request was refused or the update failed - the panel shows why, from `web-status.json`.
    """
    requests_dir, update_dir = Path(requests_dir), Path(update_dir)
    request_file = requests_dir / REQUEST
    if not request_file.exists() and not request_file.is_symlink():
        return 0
    text = setup_watcher._read_regular(request_file, MAX_REQUEST_BYTES)
    # consumed whatever it turns out to be: a bad one is never retried in a loop
    request_file.unlink(missing_ok=True)
    if text is None:
        return _refuse(
            update_dir,
            out,
            now,
            f"{request_file} is not a plain text file of at most {MAX_REQUEST_BYTES} bytes",
            "The request file could not be read.",
        )
    try:
        request = parse_request(text)
    except Refused as exc:
        return _refuse(
            update_dir, out, now, str(exc), f"The request was refused: {exc}."
        )

    who = f"web:{request['requested_by']}"
    action: str = request["action"]
    version: str | None = request.get("version")
    started = mf.timestamp(now())
    status: dict[str, Any] = {
        "id": request["id"],
        "action": action,
        "requested_by": request["requested_by"],
        "version": version,
        "state": "running",
        "outcome": None,
        "started": started,
        "finished": None,
        "exit_code": None,
        "message": "",
        "log": [],
    }
    _write_status(update_dir, status)
    _audit(
        update_dir,
        who,
        "web-request",
        f"{action}:accepted",
        f"id {request['id']}",
        now(),
    )
    out(
        f"update request {request['id']}: {action}{' ' + version if version else ''} for {who}"
    )

    capture = Capture(out)
    options = update_module.Options(
        action=action,
        yes=action == "apply",
        version=version,
    )
    environment = env or update_module.Env()
    environment.out, environment.err = capture.say, capture.err
    environment.requester = who
    layout = layout or update_module.Layout(
        update_dir=update_dir,
        root=update_dir.parent,
        config_dir=Path(config_file).parent,
    )
    try:
        with _requester_in_environment(who):
            code = run(
                options,
                config_file,
                env=environment,
                layout=layout,
                bundle=bundle,
            )
    except Exception as exc:  # noqa: BLE001 - the status file must always say how it ended, whatever went wrong
        out(traceback.format_exc())
        code = update_module.EXIT_ERROR
        capture.err(f"unexpected error: {type(exc).__name__}: {exc}")
    state, outcome = _outcome(action, code)
    finished = mf.timestamp(now())
    problem = (
        capture.errors[-1]
        if capture.errors
        else (capture.lines[-1] if capture.lines else "")
    )
    message = {
        "update_available": "An update is available.",
        "up_to_date": "Everything is up to date.",
        "installed": f"Updated to release {version}." if version else "Updated.",
        "rolled_back": "The update failed and was rolled back; the appliance runs the release it ran before.",
    }.get(outcome) or (problem or f"The update ended with exit code {code}.")
    _write_status(
        update_dir,
        {
            **status,
            "state": state,
            "outcome": outcome,
            "finished": finished,
            "exit_code": code,
            "message": _short(message, MAX_MESSAGE),
            "log": capture.tail(),
        },
    )
    _audit(update_dir, who, "web-request", f"{action}:{outcome}", f"exit {code}", now())
    return 0 if state == "done" else 1
