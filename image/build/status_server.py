"""
Optional, read-only HTTP status page of the image build worker (`ginnungagap-image-build serve-status`).

Off by default (`status_http: false`), standard library only. Two paths, nothing else:

    GET /status.json   the document below          GET /healthz   {"ok": true}

DOCUMENT (schema 1, a contract with the consumer, the Igdrasyl admin "Images" row; never rename a key):

    {"schema": 1, "generated_at": "<UTC ISO-8601 Z>", "worker": "<hostname>",
     "state": "building" | "paused" | "idle",   building: a run with state.json status `running` exists (the rule of the CLI
                                                `status`), else paused: a PAUSE file exists, else idle
     "paused": true | false,
     "active": null | {"run_id", "release", "phase", "started_at"},
     "latest": null | BUILD,                      from latest.json
     "builds": [BUILD, ...]}                      newest first, at most 20, from `status.list_builds`

    BUILD = {"version", "result": published|failed|blocked|deferred|interrupted|stopped|dry-run,
             "started_at", "finished_at", "template", "reason", "acceptance": {"passed", "total"} | null}

Where the values come from (the real files of `pipeline.py` / `publish.py`):
    BUILD     builds/<version>/summary.json and latest.json: version, result, started_at, finished_at, reason (only for a result
              other than published), acceptance.checks[*].passed (passed = number of true, total = number of checks);
              template is the store name of the version and only set for a published image.
    active    runs/<run id>/state.json: run_id, version (-> release), started_at and the phase whose status is `running`.

NOTHING is passed through. Every field is read explicitly, checked against its expected shape (a version, a run id, a timestamp,
a known result) and every free text is cleaned of control characters, scrubbed of URLs, IPv4 addresses, absolute paths and
`password=...`-like pairs, and cut to 200 characters. A missing, corrupt or unexpected source never raises: the field is `null`
(or the list empty) and the page still answers. Secrets, credential or key paths, ESXi addresses, log text and tracebacks are
never read, so they cannot be returned. Only GET and HEAD are served, there is no CORS header (the consumer is a server), and the
request path is only compared with the two names above: never used as a file name, never a directory listing.
"""

from __future__ import annotations

import json
import re
import signal
import socket
import sys
import threading
import unicodedata
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import names, status
from .pipeline import list_runs

SCHEMA = 1
MAX_BUILDS = 20
MAX_TEXT = 200
MAX_STATE_FILE = 1024 * 1024
MAX_CONNECTIONS = 32
REQUEST_TIMEOUT_SECONDS = 10
RESULTS = frozenset(
    {
        "published",
        "failed",
        "blocked",
        "deferred",
        "interrupted",
        "stopped",
        "dry-run",
    }
)
_RESULT_ALIASES = {"crashed": "interrupted"}
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?",
    re.ASCII,
)
_PHASE = re.compile(r"P\d{1,2}", re.ASCII)
_SCRUB = (
    (re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,15}://\S+"), "<url>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<address>"),
    (
        re.compile(
            r"(?i)\b(pass(?:word|wd)?|secret|token|credential|api[_-]?key|private[_-]?key)\b\s*[=:]\s*\S+"
        ),
        r"\1=<hidden>",
    ),
    (re.compile(r"(?<![\w.:/-])/(?:[\w.@+~%-]+/?)+"), "<path>"),
)


# ---------- cleaning ----------


def clean_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """A short, single-line text, or None. Control characters become spaces, the result has at most `limit` characters."""
    if not isinstance(value, str):
        return None
    text = "".join(
        " " if ch in "\n\r\t" else ch
        for ch in value[: limit * 20]
        if ch in "\n\r\t" or unicodedata.category(ch)[0] != "C"
    )
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def clean_reason(value: Any) -> str | None:
    """`clean_text` after the scrub of everything that looks like an address, a path, a URL or a secret value."""
    text = clean_text(value, MAX_TEXT * 10)
    if text is None:
        return None
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return clean_text(text)


def _timestamp(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None
        else None
    )


def _version(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and names.VERSION.fullmatch(value) is not None
        else None
    )


def _result(value: Any) -> str:
    value = _RESULT_ALIASES.get(value, value) if isinstance(value, str) else None
    # An unknown result must never look like a success.
    return value if value in RESULTS else "failed"


def _acceptance(value: Any) -> dict[str, int] | None:
    checks = value.get("checks") if isinstance(value, dict) else None
    if not isinstance(checks, list) or not checks:
        return None
    rows = [check for check in checks if isinstance(check, dict)]
    if len(rows) != len(checks):
        return None
    return {
        "passed": sum(1 for check in rows if check.get("passed") is True),
        "total": len(rows),
    }


# ---------- the document ----------


def build_entry(summary: Any) -> dict[str, Any] | None:
    """One BUILD of a `summary.json` / `latest.json` dictionary; None when it has no valid version."""
    if not isinstance(summary, dict):
        return None
    version = _version(summary.get("version"))
    if version is None:
        return None
    result = _result(summary.get("result"))
    published = result == "published"
    return {
        "version": version,
        "result": result,
        "started_at": _timestamp(summary.get("started_at")),
        "finished_at": _timestamp(summary.get("finished_at")),
        "template": names.for_version(version).template if published else None,
        "reason": None if published else clean_reason(summary.get("reason")),
        "acceptance": _acceptance(summary.get("acceptance")),
    }


def _read_state_file(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_STATE_FILE:
            return None
    except OSError:
        return None
    return status.read_json(path)


def _active_run(state_dir: Path) -> dict[str, Any] | None:
    """The newest run with status `running`, the same rule the CLI `status` uses for `current`."""
    for run in reversed(list_runs(state_dir)):
        data = run.data
        if data.get("status") != "running":
            continue
        run_id = data.get("run_id")
        phases = data.get("phases")
        phase = None
        if isinstance(phases, dict):
            phase = next(
                (
                    pid
                    for pid, entry in phases.items()
                    if isinstance(entry, dict)
                    and entry.get("status") == "running"
                    and isinstance(pid, str)
                    and _PHASE.fullmatch(pid)
                ),
                None,
            )
        return {
            "run_id": run_id
            if isinstance(run_id, str) and names.RUN_ID.fullmatch(run_id)
            else None,
            "release": _version(data.get("version")),
            "phase": phase,
            "started_at": _timestamp(data.get("started_at")),
        }
    return None


def _paused(state_dir: Path) -> bool:
    try:
        return (state_dir / "PAUSE").exists()
    except OSError:
        return False


def _guarded(function, default):
    try:
        return function()
    except Exception:  # noqa: BLE001 - a broken source file must never break the page
        return default


def _builds(state_dir: Path) -> list[dict[str, Any]]:
    found = (build_entry(item) for item in status.list_builds(state_dir, MAX_BUILDS))
    return [entry for entry in found if entry is not None][:MAX_BUILDS]


def build_document(state_dir: Path, *, hostname: str | None = None) -> dict[str, Any]:
    """The `/status.json` document of a state directory. Never raises."""
    state_dir = Path(state_dir)
    active = _guarded(lambda: _active_run(state_dir), None)
    paused = _guarded(lambda: _paused(state_dir), False)
    latest = _guarded(
        lambda: build_entry(_read_state_file(state_dir / "latest.json")), None
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worker": clean_text(hostname or socket.gethostname()),
        "state": "building" if active else "paused" if paused else "idle",
        "paused": bool(paused),
        "active": active,
        "latest": latest,
        "builds": _guarded(lambda: _builds(state_dir), []),
    }


# ---------- the server ----------

ALLOWED = "GET, HEAD"
PATHS = ("/status.json", "/healthz")


class _Handler(BaseHTTPRequestHandler):
    server_version = "ginnungagap-image-status"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def version_string(self) -> str:
        return self.server_version

    def __getattr__(self, name: str):
        # Every method but GET and HEAD gets the same 405 instead of the stdlib's 501.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _send(
        self,
        code: int,
        body: dict[str, Any],
        *,
        head: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def send_error(self, code: int, message=None, explain=None) -> None:
        # Protocol errors (oversized line, bad request) get a JSON body too, never the stdlib's HTML with the request echoed.
        try:
            phrase = HTTPStatus(code).phrase.lower()
        except ValueError:
            phrase = "error"
        self.log_message("request refused with %d", code)
        try:
            self._send(
                code, {"error": phrase}, head=getattr(self, "command", "") == "HEAD"
            )
        except OSError:
            pass

    def _method_not_allowed(self) -> None:
        self._send(405, {"error": "method not allowed"}, extra={"Allow": ALLOWED})

    def _answer(self, head: bool) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/healthz":
                self._send(200, {"ok": True}, head=head)
            elif path == "/status.json":
                try:
                    document = build_document(self.server.state_dir)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001 - never a traceback to the client
                    self.log_message("document failed (%s)", type(exc).__name__)
                    self._send(500, {"error": "internal error"}, head=head)
                else:
                    self._send(200, document, head=head)
            else:
                self._send(404, {"error": "not found"}, head=head)
        except (OSError, ValueError) as exc:
            # A vanished client, nothing to answer; never a traceback or a path in the log.
            self.log_message("not answered (%s)", type(exc).__name__)

    def do_GET(self) -> None:
        self._answer(head=False)

    def do_HEAD(self) -> None:
        self._answer(head=True)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # The default would log the whole request line including the query string.
        path = ascii(urlsplit(getattr(self, "path", "")).path[:100])
        self.log_message("%s %s %s", self.command, path, code)

    def log_message(self, format: str, *args: Any) -> None:
        line = format % args
        sys.stderr.write(
            f"{self.address_string()} - [{self.log_date_time_string()}] {line}\n"
        )


class StatusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], state_dir: Path) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.state_dir = Path(state_dir)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(
                request
            )  # too many open connections: drop, do not queue
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(state_dir: Path, bind: str, port: int) -> StatusServer:
    return StatusServer((bind, port), state_dir)


def serve(state_dir: Path, bind: str, port: int) -> None:
    """Serve in the foreground until SIGTERM or SIGINT (main thread only)."""
    server = make_server(state_dir, bind, port)

    def stop(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    print(
        f"status page on http://{bind}:{server.server_address[1]}/status.json (read-only)",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
