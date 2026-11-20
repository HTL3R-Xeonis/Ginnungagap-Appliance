"""
The phase runner of the image build (plan sections 4 and 6): P0-P16 as registered callables, the run state, the retry
classification, the cleanup that always runs, the janitor, the lock, the PAUSE file, dry run and stop-after.
This module knows NO phase body: WP2-WP7 register theirs (see "Registering a phase"); it only runs them in order.

REGISTERING A PHASE
    from image.build import pipeline                      (or `from . import pipeline` inside the package)

    @pipeline.phase("P7", describe=lambda ctx: "ssh VM: ginnungagap update --apply ...")
    def install(ctx: pipeline.PhaseContext) -> dict | None:
        ...                       # raise TransientError / PermanentError / Blocked / Deferred, or just return
        return {"installed": "1.3.0"}     # JSON-able facts: stored in state.json, read by later phases as ctx.results["P7"]

    The decorator registers in `pipeline.DEFAULT_REGISTRY`; `Pipeline(registry=...)` takes any `PhaseRegistry` (tests build
    their own). `register(id, fn, describe=None, replace=False)` is the same without the decorator. Phase ids are exactly
    P0..P16 (PHASES). P15 (cleanup) and P16 (summary) have built-in defaults and need no registration. P16 is not part
    of the loop: it runs ONCE at the end for every outcome that has a run directory (ctx.summary.result/reason/failed_phase
    are final and the cleanup is done).
    A run needs every phase up to its last one registered (`stop_after` P8 needs P0-P8); a missing one is a PipelineError
    before anything starts. A dry run lists a missing phase as "not implemented yet".

WHAT A PHASE MAY RELY ON (PhaseContext, one per run, `ctx.phase` / `ctx.attempt` / `ctx.logger` change per phase)
    ctx.esxi ctx.remote ctx.share ctx.templates ctx.clock     the ports (ports.py); in a dry run, P0-P3 get read-only views
    ctx.config  ctx.names (names.for_version)  ctx.version  ctx.run_id  ctx.options (RunOptions)
    ctx.run_dir  ctx.subdir("release")                        0700 folders below runs/<id>/; a phase never writes elsewhere
    ctx.results["P2"]                                         facts earlier phases returned (also after --resume)
    ctx.note("ova_sha256", "...")                             a fact for the summary (summary.facts), no secret
    ctx.logger                                                log ONLY through it (it redacts secrets, writes runs/<id>/NN-name.log)
    ctx.secret(value)                                         registers a secret value for redaction (the ESXi password is known already)
    ctx.track_vm(name) / track_datastore_folder(ds, folder)   cleanup by NAME the moment a thing is created (BEFORE the next
    ctx.track_share_folder(folder) / track_path(path)         step), so a crash or exception right after cannot leak it;
    ctx.on_cleanup(...)                                       persisted in state.json so the janitor can finish it after a crash
    ctx.add_diagnostic(label, fn(dir))                        saves evidence (screenshot, journal) into runs/<id>/diag/ on failure

    A phase must be safe to run again from its start after the cleanup of the failed attempt: the pipeline retries a
    transient failure by cleaning up everything the restarted phases tracked and running them again with a NEW base VM.

ERRORS -> WHAT THE PIPELINE DOES (classify)
    TransientError, TimeoutError, ConnectionError   cleanup of the phases to restart, ONE more attempt from P4 (the phase that
                                                     makes the base VM; the plan's "from P5" means "after the checks");
                                                     a transient failure before P4 repeats just that phase.
                                                     RetryPolicy.max_attempts (limits.max_attempts, default 2) attempts per run.
    PermanentError and any other Exception           no retry. (An exception that is no BuildError is a bug: traceback in the log.)
    Blocked                                          no retry, status `blocked`
    Deferred (also AlreadyRunning, PAUSE, lab busy)  status `deferred`: nothing was created, try again at the next tick
    Interrupted (SIGTERM/SIGINT/SIGHUP)              status `interrupted`; cleanup runs, a second signal during it is ignored
    A failure AFTER P14 (the catalog entry is written) is only a warning: the image is published, it is never un-published.

RUN RESULT (RunOutcome.status, exit_code)
    published 0 | dry-run 0 | stopped 0 | failed 1 | blocked 2 | deferred 3 | interrupted 4

RUN STATE: <state_dir>/runs/<run id>/
    state.json     schema 1: run_id, version, status (running|published|failed|blocked|deferred|interrupted|stopped|dry-run|crashed),
                   pid, started_at, finished_at, options, attempt, phases{P0..P16: status pending|running|ok|failed|warning|skipped|
                   planned, started_at, ended_at, seconds, attempt, result, error}, failure{phase, kind, message}, resources[...]
                   (every cleanup, status open|cleaned|kept|final|failed), kept, keep_until, events[...]
    summary.json   the BuildSummary of the run; the P16 phase of WP5 writes the permanent copies
    NN-name.log    one log per phase, run.log for everything;  diag/  evidence of failed attempts;  key/ release/ ...  the phases' folders
    <state_dir>/lock    flock, one build at a time;  <state_dir>/PAUSE   an existing file defers every real run

RESUME (`RunOptions.resume = run id`): only a run that failed/was interrupted/crashed AFTER P11 (P0-P11 ok) can continue; it restarts at
P12 (RESUME_FROM), the artifacts of P0-P11 (the exported OVA) are the phases' business to re-check. Earlier phases start over: a new run.

DRY RUN: P0-P3 really run with READ-ONLY views of the ports (a mutating call raises DryRunViolation); every later phase is only
described (`describe(ctx)`); nothing is created on any host and no PAUSE/lab-busy deferral is applied (it is reported in the summary).
STOP-AFTER: the run stops after that phase with status `stopped`; the cleanup runs as after a failure, `keep` leaves the VMs.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from . import names as names_module
from . import ports
from .config import BuildHostConfig
from .ports import (
    AlreadyRunning,
    Blocked,
    BuildError,
    Clock,
    Deferred,
    DryRunViolation,
    EsxiPort,
    Interrupted,
    PermanentError,
    RemotePort,
    ShareApiPort,
    SystemClock,
    TemplateApiPort,
)

STATE_SCHEMA = 1
LOCK_NAME = "lock"
PAUSE_NAME = "PAUSE"
RUNS_DIR = "runs"
RESUME_FROM = "P12"
RETRY_FROM = "P4"  # a transient failure from here on restarts at P4: the phase that makes a new base VM
PUBLISH_PHASE = (
    "P14"  # after it the image is in the catalog; later failures are warnings
)
CLEANUP_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 30.0
STATUSES_RESUMABLE = ("failed", "interrupted", "crashed")
EXIT_CODES = {
    "published": 0,
    "dry-run": 0,
    "stopped": 0,
    "failed": 1,
    "blocked": 2,
    "deferred": 3,
    "interrupted": 4,
}


class PipelineError(PermanentError):
    """The pipeline cannot start: a phase is not registered, a stop-after/resume value is wrong. Nothing was created."""


# ---------- the phases ----------


@dataclass(frozen=True)
class PhaseSpec:
    id: str
    name: str  # file name part of the log: `<NN>-<name>.log`
    read_only: bool = False  # runs for real in a dry run


PHASES: tuple[PhaseSpec, ...] = (
    PhaseSpec("P0", "pick", read_only=True),
    PhaseSpec("P1", "preflight", read_only=True),
    PhaseSpec("P2", "download", read_only=True),
    PhaseSpec("P3", "choose_base", read_only=True),
    PhaseSpec("P4", "deploy_base"),
    PhaseSpec("P5", "guestinfo_boot"),
    PhaseSpec("P6", "upload_release"),
    PhaseSpec("P7", "install"),
    PhaseSpec("P8", "prepare"),
    PhaseSpec("P9", "shutdown"),
    PhaseSpec("P10", "export"),
    PhaseSpec("P11", "inspect"),
    PhaseSpec("P12", "stage"),
    PhaseSpec("P13", "acceptance"),
    PhaseSpec("P14", "publish"),
    PhaseSpec("P15", "cleanup"),
    PhaseSpec("P16", "summary"),
)
PHASE_IDS = tuple(spec.id for spec in PHASES)
_SPEC = {spec.id: spec for spec in PHASES}


def phase_index(phase_id: str) -> int:
    try:
        return PHASE_IDS.index(phase_id)
    except ValueError:
        raise PipelineError(
            f"{phase_id!r} is not a phase ({PHASE_IDS[0]}..{PHASE_IDS[-1]})"
        ) from None


PhaseFn = Callable[["PhaseContext"], Mapping[str, Any] | None]
DescribeFn = Callable[["PhaseContext"], str]


@dataclass(frozen=True)
class RegisteredPhase:
    id: str
    fn: PhaseFn
    describe: DescribeFn | None = None


class PhaseRegistry:
    """The phase bodies by id. WP2-WP7 register theirs; the pipeline never imports them."""

    def __init__(self) -> None:
        self._phases: dict[str, RegisteredPhase] = {}

    def register(
        self,
        phase_id: str,
        fn: PhaseFn,
        *,
        describe: DescribeFn | None = None,
        replace: bool = False,
    ) -> PhaseFn:
        phase_index(phase_id)
        if phase_id in self._phases and not replace:
            raise PipelineError(
                f"phase {phase_id} is registered already (replace=True to override)"
            )
        self._phases[phase_id] = RegisteredPhase(phase_id, fn, describe)
        return fn

    def phase(
        self,
        phase_id: str,
        *,
        describe: DescribeFn | None = None,
        replace: bool = False,
    ) -> Callable[[PhaseFn], PhaseFn]:
        """Decorator form of `register`."""

        def decorate(fn: PhaseFn) -> PhaseFn:
            return self.register(phase_id, fn, describe=describe, replace=replace)

        return decorate

    def get(self, phase_id: str) -> RegisteredPhase | None:
        return self._phases.get(phase_id)

    def registered(self) -> list[str]:
        return [pid for pid in PHASE_IDS if pid in self._phases]

    def missing(self, up_to: str = "P14") -> list[str]:
        """Phases up to `up_to` that have no body (P15/P16 have built-in defaults and are never missing)."""
        last = phase_index(up_to)
        return [
            pid
            for pid in PHASE_IDS[: last + 1]
            if pid not in self._phases and pid not in ("P15", "P16")
        ]


DEFAULT_REGISTRY = PhaseRegistry()
phase = DEFAULT_REGISTRY.phase
register_phase = DEFAULT_REGISTRY.register


# ---------- secrets out of logs ----------

_TOKEN_SHAPES = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
MIN_SECRET_LENGTH = 4


class Redactor:
    """Replaces registered secret values, GitHub token shapes and private key blocks with `***`."""

    def __init__(self, secrets: Any = ()) -> None:
        self._secrets: list[str] = []
        for value in secrets:
            self.add(value)

    def add(self, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LENGTH and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def redact_deep(self, value: Any) -> Any:
        """`redact` applied to every string inside a json-like value."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_deep(v) for v in value]
        return value

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = _PRIVATE_KEY.sub("***", text)
        return _TOKEN_SHAPES.sub("***", text)


class _RedactFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self.redactor.redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        return True


# ---------- small helpers ----------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def jsonable(value: Any) -> Any:
    """`value` as something json can store: paths and times as text, sets sorted, dataclasses as dicts, the rest as text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return str(value)


def _write_json(path: Path, data: Any) -> None:
    """Atomic: a reader sees the old or the new file, never half of one (also after a power loss)."""
    temp = path.with_name(path.name + ".partial")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------- classification ----------


@dataclass(frozen=True)
class Failure:
    kind: str  # transient | permanent | blocked | deferred | interrupted
    message: str
    type: str = ""
    detail: str | None = None

    @property
    def status(self) -> str:
        return {
            "transient": "failed",
            "permanent": "failed",
            "blocked": "blocked",
            "deferred": "deferred",
            "interrupted": "interrupted",
        }[self.kind]


def classify(exc: BaseException) -> Failure:
    """What an exception means for the run (see the module docstring)."""
    name = type(exc).__name__
    if isinstance(exc, Interrupted | KeyboardInterrupt):
        return Failure("interrupted", str(exc) or name, name)
    if isinstance(exc, Blocked):
        return Failure("blocked", exc.message, name, exc.detail)
    if isinstance(exc, Deferred):
        return Failure("deferred", exc.message, name, exc.detail)
    if isinstance(exc, BuildError):
        return Failure(
            "transient" if exc.transient else "permanent", exc.message, name, exc.detail
        )
    if isinstance(exc, TimeoutError | ConnectionError):
        return Failure("transient", f"{name}: {exc}", name)
    return Failure("permanent", f"unexpected {name}: {exc}", name)


# ---------- the lock ----------


class BuildLock:
    """`flock` on `<state_dir>/lock`: one build (or janitor) at a time. The kernel drops it when the process dies, so it is never stale."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def holder(self) -> dict[str, Any] | None:
        data = _read_json(self.path)
        return data if isinstance(data, dict) else None

    def acquire(self, run_id: str, clock: Clock | None = None) -> None:
        """:raises AlreadyRunning: "build X is running"."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            holder = self.holder() or {}
            who = holder.get("run_id", "another build")
            pid = holder.get("pid")
            raise AlreadyRunning(
                f"build {who} is running" + (f" (pid {pid})" if pid else "")
            ) from None
        self._fd = fd
        moment = (clock or SystemClock()).now()
        os.ftruncate(fd, 0)
        os.write(
            fd,
            (
                json.dumps(
                    {"run_id": run_id, "pid": os.getpid(), "started_at": _iso(moment)}
                )
                + "\n"
            ).encode(),
        )

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


# ---------- run state ----------


class StateFile:
    """`state.json` of a run: a dict that saves itself atomically."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, run_dir: Path) -> StateFile | None:
        data = _read_json(run_dir / "state.json")
        if not isinstance(data, dict):
            return None
        return cls(run_dir / "state.json", data)

    def save(self) -> None:
        _write_json(self.path, jsonable(self.data))

    def phase(self, phase_id: str) -> dict[str, Any]:
        return self.data["phases"][phase_id]


def runs_dir(state_dir: Path) -> Path:
    return state_dir / RUNS_DIR


def list_runs(state_dir: Path) -> list[StateFile]:
    """Every run with a readable state.json, oldest first (the run id starts with the version, the stamp sorts by time)."""
    found = []
    base = runs_dir(state_dir)
    if base.is_dir():
        for child in sorted(base.iterdir()):
            state = StateFile.load(child) if child.is_dir() else None
            if state is not None:
                found.append(state)
    return sorted(found, key=lambda s: str(s.data.get("started_at", "")))


def attempts_in_window(
    state_dir: Path, version: str, now: datetime, window_hours: float = 24
) -> int:
    """
    Build attempts of a release in the last `window_hours`: the attempts of every run of that version that ended in
    failed/interrupted/crashed (a deferred, blocked, dry or stopped run is not an attempt). The poll uses it to stop
    enqueuing a release after `limits.max_attempts_per_release` (A11).
    """
    since = now - timedelta(hours=window_hours)
    total = 0
    for state in list_runs(state_dir):
        data = state.data
        started = _parse_iso(data.get("started_at"))
        if (
            data.get("version") == version
            and data.get("status") in STATUSES_RESUMABLE
            and started is not None
            and started >= since
        ):
            total += int(data.get("attempt", 1))
    return total


# ---------- options, summary, outcome ----------


@dataclass(frozen=True)
class RunOptions:
    """The flags of `ginnungagap-image-build run` (section 10). The pipeline itself uses dry_run, stop_after, keep, force_busy, resume."""

    dry_run: bool = False
    stop_after: str | None = None
    keep: bool = False
    force_busy: bool = False
    resume: str | None = None
    # for the phases:
    base: str | None = None
    release_dir: Path | None = None
    allow_prerelease: bool = False
    allow_unsigned: bool = False
    retry: bool = False
    clean_base: bool = False
    skip_janitor: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: float = RETRY_BACKOFF_SECONDS


@dataclass
class BuildSummary:
    """The facts of a run. The pipeline fills run_id/version/result/reason/times/phases; the phases add `facts` through `ctx.note`."""

    run_id: str
    version: str
    result: str = "running"
    reason: str = ""
    failed_phase: str | None = None
    started_at: str = ""
    finished_at: str = ""
    attempts: int = 1
    published: bool = False
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    cleanup_warnings: list[str] = field(default_factory=list)
    plan: list[dict[str, str]] = field(
        default_factory=list
    )  # dry run: what each later phase would do

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass
class RunOutcome:
    status: str
    reason: str
    run_id: str | None
    run_dir: Path | None
    summary: BuildSummary | None
    failed_phase: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------- cleanup ----------


@dataclass
class _Cleanup:
    rid: int
    kind: str
    ref: str
    label: str
    fn: Callable[[], None]
    when: str  # "always" | "failure"
    keepable: bool
    phase: int


class CleanupStack:
    """
    What a run created, newest first. `when="always"`: removed after success too (VMs, keys, work folders);
    `when="failure"`: removed only when the run did not publish (the staged share folder is the product of a success).
    `keepable`: skipped when `keep` is on. Every entry is recorded in state.json (kind, ref, data) so the janitor can finish the
    job after a crash. A cleanup that raises is retried once and then logged as a warning; it never hides the original error.
    """

    def __init__(
        self,
        state: StateFile,
        logger: Callable[[], logging.Logger],
        clock: Clock,
        redactor: Redactor,
    ) -> None:
        self.entries: list[_Cleanup] = []
        self._state = state
        self._logger = logger
        self._clock = clock
        self._redactor = redactor
        self.phase = 0

    def add(
        self,
        kind: str,
        ref: str,
        fn: Callable[[], None],
        *,
        when: str = "always",
        keepable: bool = False,
        label: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if when not in ("always", "failure"):
            raise ValueError("when is 'always' or 'failure'")
        resources = self._state.data.setdefault("resources", [])
        rid = len(resources)
        resources.append(
            {
                "id": rid,
                "kind": kind,
                "ref": ref,
                "phase": PHASE_IDS[self.phase],
                "when": when,
                "keepable": keepable,
                "data": jsonable(data or {}),
                "status": "open",
            }
        )
        self.entries.append(
            _Cleanup(
                rid, kind, ref, label or f"{kind} {ref}", fn, when, keepable, self.phase
            )
        )
        self._state.save()

    def _mark(self, rid: int, status: str, message: str | None = None) -> None:
        record = self._state.data["resources"][rid]
        record["status"] = status
        if message:
            record["error"] = message
        self._state.save()

    def commit(self) -> None:
        """
        The image is published: what only a failed run removes (the staged share folder) is now the result. It is marked `final`
        and dropped from the stack, so neither a late failure, nor a signal, nor the janitor after a crash ever removes it.
        """
        for entry in [e for e in self.entries if e.when == "failure"]:
            self.entries.remove(entry)
            self._mark(entry.rid, "final")

    def run(self, mode: str, *, since: int = 0, keep: bool = False) -> list[str]:
        """
        mode `success`: run the `always` entries, keep the `failure` ones as the result; `failure`/`retry`: run everything.
        Only entries of phases >= `since`. Returns the warnings of the cleanups that failed.
        """
        warnings: list[str] = []
        log = self._logger()
        for entry in [e for e in reversed(self.entries) if e.phase >= since]:
            self.entries.remove(entry)
            if mode == "success" and entry.when == "failure":
                self._mark(entry.rid, "final")
                continue
            if keep and entry.keepable and mode != "retry":
                log.warning(f"kept for a human look: {entry.label}")
                self._mark(entry.rid, "kept")
                continue
            for attempt in range(1, CLEANUP_ATTEMPTS + 1):
                try:
                    entry.fn()
                except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                    text = self._redactor.redact(f"{type(exc).__name__}: {exc}")
                    if attempt == CLEANUP_ATTEMPTS:
                        warnings.append(f"cleanup of {entry.label} failed: {text}")
                        log.error(f"cleanup of {entry.label} failed: {text}")
                        self._mark(entry.rid, "failed", text)
                    else:
                        log.warning(
                            f"cleanup of {entry.label} failed ({text}); trying again"
                        )
                        self._clock.sleep(2.0)
                else:
                    log.info(f"cleaned up: {entry.label}")
                    self._mark(entry.rid, "cleaned")
                    break
        return warnings


# ---------- the context a phase gets ----------


@dataclass
class PhaseContext:
    """Everything a phase may use. One object per run; `phase`, `attempt` and `logger` change while the run goes on."""

    run_id: str
    version: str
    names: names_module.ImageNames
    config: BuildHostConfig
    options: RunOptions
    run_dir: Path
    state_dir: Path
    esxi: EsxiPort
    remote: RemotePort
    share: ShareApiPort
    templates: TemplateApiPort
    clock: Clock
    logger: logging.Logger
    redactor: Redactor
    summary: BuildSummary
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempt: int = 1
    phase: str = ""
    baseline_vms: frozenset[str] = frozenset()
    diagnostics: list[tuple[str, Callable[[Path], None]]] = field(default_factory=list)
    cleanup: CleanupStack | None = None

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run

    def subdir(self, name: str) -> Path:
        """`runs/<id>/<name>`, created 0700."""
        path = self.run_dir / name
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def note(self, key: str, value: Any) -> None:
        self.summary.facts[key] = self.redactor.redact_deep(jsonable(value))

    def secret(self, value: str | None) -> None:
        self.redactor.add(value)

    def result_of(self, phase_id: str) -> dict[str, Any]:
        """The facts an earlier phase returned. :raises PermanentError: it returned none (it did not run)."""
        if phase_id not in self.results:
            raise PermanentError(f"phase {phase_id} has not produced a result")
        return self.results[phase_id]

    # -- cleanup registration --
    def _stack(self) -> CleanupStack:
        if self.dry_run or self.cleanup is None:
            raise DryRunViolation(
                "a dry run creates nothing, so there is nothing to track"
            )
        return self.cleanup

    def on_cleanup(
        self,
        kind: str,
        ref: str,
        fn: Callable[[], None],
        *,
        when: str = "always",
        keepable: bool = False,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Generic form: `kind` and `ref` are what state.json shows (the janitor only acts on the kinds below)."""
        self._stack().add(kind, ref, fn, when=when, keepable=keepable, data=data)

    def track_vm(self, name: str) -> None:
        """Delete the VM (`esxi.delete_vm`) when the run ends or the attempt is retried. `--keep` leaves it."""
        self.on_cleanup("vm", name, lambda: self.esxi.delete_vm(name), keepable=True)

    def track_datastore_folder(self, datastore: str, folder: str) -> None:
        self.on_cleanup(
            "datastore_folder",
            folder,
            lambda: self.esxi.delete_datastore_folder(datastore, folder),
            keepable=True,
            data={"datastore": datastore},
        )

    def track_share_folder(
        self,
        folder: str,
        marker_name: str = names_module.RUN_MARKER,
        marker_text: str | None = None,
    ) -> None:
        """Remove a staged folder of the share when the run does NOT publish (never when it succeeds). The marker proves it is ours."""
        text = marker_text if marker_text is not None else self.run_id
        self.on_cleanup(
            "share_folder",
            folder,
            lambda: self.share.remove_folder(
                folder, marker_name=marker_name, marker_text=text
            ),
            when="failure",
            data={"marker_name": marker_name, "marker_text": text},
        )

    def track_path(self, path: str | Path) -> None:
        """Delete a file or folder below the state dir (keys, the raw disk, work folders). Refuses a path outside it."""
        target = Path(path)
        root = self.state_dir.resolve()
        resolved = target.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise PermanentError(f"refusing to track {target}: not below {root}")
        self.on_cleanup("path", str(target), lambda: _remove_path(target))

    def add_diagnostic(self, label: str, fn: Callable[[Path], None]) -> None:
        """`fn(dir)` saves evidence into `runs/<id>/diag/<attempt>/` when the run (or an attempt) fails, before the cleanup."""
        self.diagnostics.append((label, fn))

    def run_cleanup(self) -> list[str]:
        """What the default P15 does: the success-time cleanup (honours --keep). Warnings are added to the summary."""
        if self.cleanup is None:
            return []
        warnings = self.cleanup.run("success", keep=self.options.keep)
        self.summary.cleanup_warnings.extend(warnings)
        return warnings

    def leftover_vms(self) -> list[str]:
        """VMs on the host now that were not there when the run started (empty = the host is as we found it)."""
        return sorted(
            vm.name for vm in self.esxi.list_vms() if vm.name not in self.baseline_vms
        )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=False)


# ---------- the hooks ----------


def default_lab_busy(esxi: EsxiPort, config: BuildHostConfig) -> str | None:
    """
    The reason the lab is busy, None when it is free: a powered-on VM on the build host that is neither on
    `esxi.allowed_running_vms` nor one of ours (imgbuild-/imgaccept-: leftovers are the janitor's business).
    """
    allowed = set(config.esxi.allowed_running_vms)
    busy = sorted(
        vm.name
        for vm in esxi.list_vms()
        if vm.powered_on
        and vm.name not in allowed
        and not names_module.is_ours_vm(vm.name)
    )
    if busy:
        return "lab busy: powered-on VMs on the build host: " + ", ".join(busy[:10])
    return None


@dataclass
class PipelineHooks:
    """
    `lab_busy(esxi, config) -> reason | None` replaces default_lab_busy (the lab-busy check hook);
    `notify(event, summary_dict)` gets started / succeeded / failed / blocked / deferred (WP5's notify.command runner plugs in here).
    """

    lab_busy: Callable[[EsxiPort, BuildHostConfig], str | None] | None = None
    notify: Callable[[str, dict[str, Any]], None] | None = None


# ---------- the janitor ----------


@dataclass
class JanitorReport:
    deleted_vms: list[str] = field(default_factory=list)
    deleted_folders: list[str] = field(default_factory=list)
    deleted_share_folders: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    crashed_runs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.errors


class Janitor:
    """
    Removes what a dead run left behind. It touches ONLY names that start with `imgbuild-` / `imgaccept-` (VMs and datastore
    folders of the allowed host), the share folders and paths that a crashed run recorded in its own state.json (a share
    folder only with its marker), and it marks `running` runs of dead processes as `crashed`. It refuses a host that is not on
    `esxi.allowed_hosts`. The caller holds the build lock, so no run is alive.
      * a VM whose notes name a run: deleted, unless that run was started with `keep` and its keep time (24 h) is not over
      * a VM without a run in its notes (the crash fell between import and tag): deleted only when older than
        `limits.orphan_age_hours`; a VM of unknown age is kept
      * a datastore folder of ours that no registered VM uses: deleted
    """

    def __init__(
        self,
        config: BuildHostConfig,
        esxi: EsxiPort,
        clock: Clock,
        state_dir: Path,
        logger: logging.Logger,
        share: ShareApiPort | None = None,
        current_run: str | None = None,
    ) -> None:
        self.config = config
        self.esxi = esxi
        self.clock = clock
        self.state_dir = state_dir
        self.log = logger
        self.share = share
        self.current_run = current_run

    def _kept(self, state: StateFile | None) -> bool:
        if state is None or not state.data.get("kept"):
            return False
        until = _parse_iso(state.data.get("keep_until"))
        return until is not None and self.clock.now() < until

    def run(self) -> JanitorReport:
        report = JanitorReport()
        if self.esxi.host not in self.config.esxi.allowed_hosts:
            raise PermanentError(
                f"the janitor refuses to touch {self.esxi.host}: it is not in esxi.allowed_hosts"
            )
        runs = {
            str(s.data.get("run_id")): s
            for s in list_runs(self.state_dir)
            if s.data.get("run_id") != self.current_run
        }
        for run_id, state in runs.items():
            if state.data.get("status") == "running":
                state.data["status"] = "crashed"
                state.data["finished_at"] = _iso(self.clock.now())
                state.data["failure"] = {
                    "phase": "?",
                    "kind": "crashed",
                    "message": "the process died without cleaning up",
                }
                state.save()
                report.crashed_runs.append(run_id)
                self.log.warning(
                    f"run {run_id} was running but its process is gone: marked crashed"
                )
        self._vms(report, runs)
        self._folders(report, runs)
        self._recorded_resources(report, runs)
        return report

    def _vms(self, report: JanitorReport, runs: dict[str, StateFile]) -> None:
        now = self.clock.now()
        orphan_age = timedelta(hours=self.config.limits.orphan_age_hours)
        allowed_running = set(self.config.esxi.allowed_running_vms)
        for vm in self.esxi.list_vms():
            if not names_module.is_ours_vm(vm.name) or vm.name in allowed_running:
                continue
            run_id = names_module.parse_annotation(vm.annotation)
            if run_id == self.current_run and run_id is not None:
                continue
            state = runs.get(run_id) if run_id else None
            if self._kept(state):
                report.kept.append(vm.name)
                self.log.info(
                    f"kept for a human look until {state.data.get('keep_until')}: VM {vm.name}"
                )  # type: ignore[union-attr]
                continue
            if run_id is None:
                if vm.created is None or now - vm.created < orphan_age:
                    report.kept.append(vm.name)
                    self.log.warning(
                        f"VM {vm.name} has no run in its notes and is not older than "
                        f"{self.config.limits.orphan_age_hours:g} h (or its age is unknown): left alone"
                    )
                    continue
            elif state is not None and state.data.get("kept"):
                self.log.warning(
                    f"the keep time of run {run_id} is over: deleting VM {vm.name}"
                )
            try:
                self.esxi.delete_vm(vm.name)
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                report.errors.append(f"VM {vm.name}: {type(exc).__name__}: {exc}")
                self.log.error(f"could not delete VM {vm.name}: {exc}")
            else:
                report.deleted_vms.append(vm.name)
                self.log.info(f"janitor deleted VM {vm.name}")

    def _folders(self, report: JanitorReport, runs: dict[str, StateFile]) -> None:
        datastore = self.config.esxi.datastore
        try:
            registered = {vm.folder for vm in self.esxi.list_vms()}
            folders = self.esxi.list_datastore_folders(datastore)
        except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
            report.errors.append(f"datastore {datastore}: {type(exc).__name__}: {exc}")
            return
        for folder in folders:
            if not names_module.is_ours_folder(folder) or folder in registered:
                continue
            try:
                self.esxi.delete_datastore_folder(datastore, folder)
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                report.errors.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                self.log.error(f"could not delete the datastore folder {folder}: {exc}")
            else:
                report.deleted_folders.append(folder)
                self.log.info(f"janitor deleted the datastore folder {folder}")

    def _recorded_resources(
        self, report: JanitorReport, runs: dict[str, StateFile]
    ) -> None:
        """Share folders and paths a crashed/failed run still had open. Never for a run that is kept or that is the current one."""
        base = runs_dir(self.state_dir).resolve()
        for run_id, state in runs.items():
            if self._kept(state):
                continue
            changed = False
            for record in state.data.get("resources", []):
                if record.get("status") not in ("open", "failed"):
                    continue
                kind, ref = record.get("kind"), str(record.get("ref"))
                try:
                    if kind == "share_folder" and self.share is not None:
                        data = record.get("data") or {}
                        if not ref.startswith(names_module.FOLDER_PREFIX):
                            continue
                        self.share.remove_folder(
                            ref,
                            marker_name=str(
                                data.get("marker_name", names_module.RUN_MARKER)
                            ),
                            marker_text=str(data.get("marker_text", run_id)),
                        )
                        report.deleted_share_folders.append(ref)
                    elif kind == "path":
                        target = Path(ref)
                        resolved = target.resolve()
                        if resolved == base or not resolved.is_relative_to(base):
                            continue
                        _remove_path(target)
                        report.removed_paths.append(ref)
                    elif kind in ("vm", "datastore_folder"):
                        continue  # handled by name above, which is the stronger check
                    else:
                        continue
                except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                    report.errors.append(f"{kind} {ref}: {type(exc).__name__}: {exc}")
                else:
                    record["status"] = "cleaned"
                    changed = True
            if changed:
                state.save()


# ---------- signals ----------


class _SignalGuard:
    """SIGTERM/SIGINT/SIGHUP raise `Interrupted` in the main thread; while masked (the cleanup) they are only logged."""

    SIGNALS = tuple(
        s for s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)) if s
    )

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger
        self.masked = False
        self._old: dict[int, Any] = {}

    def _handle(self, signum: int, frame: Any) -> None:
        if self.masked:
            self.log.warning(f"signal {signum} ignored: the cleanup is running")
            return
        raise Interrupted(signum)

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            for signum in self.SIGNALS:
                self._old[signum] = signal.signal(signum, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for signum, old in self._old.items():
            signal.signal(signum, old)
        self._old.clear()


# ---------- the pipeline ----------


class Pipeline:
    def __init__(
        self,
        *,
        config: BuildHostConfig,
        esxi: EsxiPort,
        remote: RemotePort,
        share: ShareApiPort,
        templates: TemplateApiPort,
        clock: Clock | None = None,
        registry: PhaseRegistry | None = None,
        hooks: PipelineHooks | None = None,
        state_dir: Path | None = None,
        secrets: Any = (),
        install_signals: bool = True,
        policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config
        self.esxi = esxi
        self.remote = remote
        self.share = share
        self.templates = templates
        self.clock = clock or SystemClock()
        self.registry = registry or DEFAULT_REGISTRY
        self.hooks = hooks or PipelineHooks()
        self.state_dir = Path(state_dir or config.paths.state_dir)
        self.redactor = Redactor(secrets)
        self.install_signals = install_signals
        self.policy = policy or RetryPolicy(max_attempts=config.limits.max_attempts)
        self.log = logging.getLogger("imgbuild.run")
        self.log.setLevel(logging.DEBUG)

    # -- public --
    def run(self, version: str, options: RunOptions | None = None) -> RunOutcome:
        """Runs (or dry-runs, or resumes) the build of one release. Never raises for a failed build: the outcome says what happened."""
        options = options or RunOptions()
        try:
            image_names = names_module.for_version(version)
            if options.stop_after is not None:
                phase_index(options.stop_after)
            self._check_registered(options)
        except (names_module.InvalidVersion, PipelineError) as exc:
            return RunOutcome("blocked", exc.message, None, None, None)
        lock = BuildLock(self.state_dir / LOCK_NAME)
        now = self.clock.now()
        run_id = options.resume or names_module.run_id(version, now)
        if not options.resume:
            while (
                runs_dir(self.state_dir) / run_id
            ).exists():  # two runs in one second
                now += timedelta(seconds=1)
                run_id = names_module.run_id(version, now)
        try:
            lock.acquire(run_id, self.clock)
        except AlreadyRunning as exc:
            self._notify("deferred", {"version": version, "reason": exc.message})
            return RunOutcome("deferred", exc.message, None, None, None)
        try:
            return self._locked_run(version, image_names, options, run_id, now)
        finally:
            lock.release()

    def run_janitor(self) -> JanitorReport:
        """The `janitor` command: takes the lock (so it never deletes the VMs of a running build), then cleans. :raises AlreadyRunning:"""
        lock = BuildLock(self.state_dir / LOCK_NAME)
        lock.acquire("janitor", self.clock)
        try:
            return self._janitor()
        finally:
            lock.release()

    # -- setup --
    def _check_registered(self, options: RunOptions) -> None:
        last = options.stop_after or "P16"
        needed = "P14" if phase_index(last) > phase_index("P14") else last
        if options.dry_run:
            needed = "P3"
        missing = self.registry.missing(needed)
        if missing:
            raise PipelineError(
                "phases not registered: "
                + ", ".join(f"{pid} ({_SPEC[pid].name})" for pid in missing)
            )

    def _janitor(self) -> JanitorReport:
        return Janitor(
            self.config,
            self.esxi,
            self.clock,
            self.state_dir,
            self.log,
            share=self.share,
        ).run()

    def _new_state(
        self, run_id: str, version: str, options: RunOptions, now: datetime
    ) -> StateFile:
        run_dir = runs_dir(self.state_dir) / run_id
        run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        data: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "run_id": run_id,
            "version": version,
            "status": "dry-run" if options.dry_run else "running",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _iso(now),
            "finished_at": None,
            "options": jsonable(asdict(options)),
            "attempt": 1,
            "phases": {
                spec.id: {"name": spec.name, "status": "pending"} for spec in PHASES
            },
            "failure": None,
            "resources": [],
            "kept": False,
            "keep_until": None,
            "events": [],
        }
        state = StateFile(run_dir / "state.json", data)
        state.save()
        return state

    def _resume_state(
        self, run_id: str, version: str, options: RunOptions
    ) -> StateFile:
        run_dir = runs_dir(self.state_dir) / run_id
        state = StateFile.load(run_dir)
        if state is None:
            raise PipelineError(f"there is no run {run_id} to resume")
        data = state.data
        if data.get("version") != version:
            raise PipelineError(
                f"run {run_id} is a build of {data.get('version')}, not of {version}"
            )
        if data.get("status") == "running":
            data["status"] = "crashed"  # the lock is ours, so its process is gone
        if data.get("status") not in STATUSES_RESUMABLE:
            raise PipelineError(
                f"run {run_id} ended as {data.get('status')}; only failed, interrupted or crashed runs can be resumed"
            )
        not_done = [
            pid
            for pid in PHASE_IDS[: phase_index(RESUME_FROM)]
            if data["phases"][pid]["status"] != "ok"
        ]
        if not_done:
            raise PipelineError(
                f"run {run_id} cannot be resumed: {', '.join(not_done)} did not finish "
                f"(only a run whose phases before {RESUME_FROM} are done can continue; start a new run)"
            )
        return state

    # -- the run --
    def _locked_run(
        self,
        version: str,
        image_names: names_module.ImageNames,
        options: RunOptions,
        run_id: str,
        now: datetime,
    ) -> RunOutcome:
        resumed = options.resume is not None
        view = self._ports_for(options)
        guard = _SignalGuard(self.log)
        if not self.install_signals:
            guard.SIGNALS = ()  # type: ignore[misc]
        with guard:
            try:
                baseline, gate = self._pre_gate(options, view[0])
            except Interrupted as exc:
                return self._refused("interrupted", classify(exc), version)
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                failure = classify(exc)
                return self._refused(failure.status, failure, version)
            try:
                state = (
                    self._resume_state(run_id, version, options)
                    if resumed
                    else self._new_state(run_id, version, options, now)
                )
            except PipelineError as exc:
                return RunOutcome("blocked", exc.message, run_id, None, None)
            run_dir = state.path.parent
            summary = BuildSummary(
                run_id=run_id, version=version, started_at=state.data["started_at"]
            )
            ctx = PhaseContext(
                run_id=run_id,
                version=version,
                names=image_names,
                config=self.config,
                options=options,
                run_dir=run_dir,
                state_dir=self.state_dir,
                esxi=view[0],
                remote=view[1],
                share=view[2],
                templates=view[3],
                clock=self.clock,
                logger=self.log,
                redactor=self.redactor,
                summary=summary,
                baseline_vms=baseline,
            )
            ctx.note("gate", gate)
            ctx.cleanup = CleanupStack(
                state, lambda: ctx.logger, self.clock, self.redactor
            )
            run_filter = _RedactFilter(self.redactor)
            self.log.addFilter(run_filter)
            run_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
            run_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self.log.addHandler(run_handler)
            try:
                failure: Failure | None = None
                stopped = False
                try:
                    try:
                        failure, stopped = self._phases(ctx, state, resumed)
                    except (Interrupted, KeyboardInterrupt) as exc:
                        failure = classify(exc)
                        self._record_failure(state, ctx.phase, failure)
                        entry = state.data["phases"].get(ctx.phase)
                        if entry is not None and entry.get("status") == "running":
                            entry["status"] = "failed"
                            entry["error"] = self._error_record(failure)
                            state.save()
                except (
                    Interrupted,
                    KeyboardInterrupt,
                ) as exc:  # a second signal while the first was recorded
                    failure = classify(exc)
                finally:
                    guard.masked = True  # from here on a signal is only logged: the cleanup must finish
                return self._finish(ctx, state, failure, stopped)
            finally:
                self.log.removeHandler(run_handler)
                run_handler.close()
                self.log.removeFilter(run_filter)

    def _refused(self, status: str, failure: Failure, version: str) -> RunOutcome:
        """The run ended before it had a run directory (the gate): deferred (PAUSE, lab busy, host not ready), failed or interrupted."""
        reason = self.redactor.redact(failure.message)
        self.log.warning(f"{version}: {status}: {reason}")
        event = {
            "deferred": "deferred",
            "failed": "failed",
            "interrupted": "failed",
            "blocked": "blocked",
        }[status]
        self._notify(event, {"version": version, "result": status, "reason": reason})
        return RunOutcome(status, reason, None, None, None)

    def _ports_for(self, options: RunOptions) -> tuple[Any, Any, Any, Any]:
        if not options.dry_run:
            return self.esxi, self.remote, self.share, self.templates
        return (
            ports.read_only(self.esxi, ports.ESXI_READ_ONLY, "esxi"),
            ports.read_only(self.remote, ports.REMOTE_READ_ONLY, "remote"),
            ports.read_only(self.share, ports.SHARE_READ_ONLY, "share"),
            ports.read_only(self.templates, ports.TEMPLATE_READ_ONLY, "templates"),
        )

    # -- gate --
    def _pre_gate(
        self, options: RunOptions, esxi: Any
    ) -> tuple[frozenset[str], dict[str, Any]]:
        """
        Before a run directory exists: the allow list, the PAUSE file, the janitor, the baseline of the host, lab busy.
        :raises Deferred: PAUSE or lab busy, or a transient problem of the host (nothing was created).
        :raises PermanentError: the host is not on the allow list.
        A dry run applies none of the deferrals (it reports them) and runs no janitor.
        """
        host = esxi.host
        if host not in self.config.esxi.allowed_hosts:
            raise PermanentError(
                f"{host} is not in esxi.allowed_hosts: the build never touches that host"
            )
        pause = self.state_dir / PAUSE_NAME
        paused = (
            f"paused: the file {pause} exists (remove it to build)"
            if pause.exists()
            else None
        )
        busy_hook = self.hooks.lab_busy or default_lab_busy
        try:
            if options.dry_run:
                return frozenset(), {
                    "paused": paused,
                    "lab_busy": busy_hook(esxi, self.config),
                    "applied": False,
                }
            if paused:
                raise Deferred(paused)
            if not options.skip_janitor:
                try:
                    report = self._janitor()
                except ports.TransientError:
                    raise
                except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                    self.log.warning(
                        f"the janitor could not run: {self.redactor.redact(str(exc))}"
                    )
                else:
                    if not report.clean:
                        self.log.warning(
                            "the janitor left problems: " + "; ".join(report.errors)
                        )
            baseline = frozenset(vm.name for vm in esxi.list_vms())
            busy = None if options.force_busy else busy_hook(esxi, self.config)
            if busy:
                raise Deferred(busy)
            return baseline, {"paused": None, "lab_busy": None, "applied": True}
        except ports.TransientError as exc:
            raise Deferred(f"the build host is not ready: {exc.message}") from exc

    # -- the loop --
    def _phases(
        self, ctx: PhaseContext, state: StateFile, resumed: bool
    ) -> tuple[Failure | None, bool]:
        """Runs the phases in order. Returns (the failure that ended the run or None, whether stop-after ended it early)."""
        options = ctx.options
        start = phase_index(RESUME_FROM) if resumed else 0
        if resumed:
            for pid in PHASE_IDS[:start]:
                ctx.results[pid] = dict(state.phase(pid).get("result") or {})
            for pid in PHASE_IDS[start:]:
                state.data["phases"][pid] = {
                    "name": _SPEC[pid].name,
                    "status": "pending",
                }
            state.data.update(
                status="running", failure=None, finished_at=None, attempt=1
            )
            self._event(state, "resumed", RESUME_FROM, f"resumed from {RESUME_FROM}")
        # P16 (summary) is not part of the loop: it runs once at the end for every outcome (see _finish)
        loop_end = phase_index("P15")
        last = (
            min(phase_index(options.stop_after), loop_end)
            if options.stop_after
            else loop_end
        )
        publish_idx = phase_index(PUBLISH_PHASE)
        retry_idx = phase_index(RETRY_FROM)
        state.save()
        if not options.dry_run:
            self._notify("started", {"run_id": ctx.run_id, "version": ctx.version})

        i = start
        while i <= last:
            spec = PHASES[i]
            try:
                self._do_phase(ctx, state, spec)
            except Interrupted:
                raise
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                failure = classify(exc)
                self._record_failure(state, spec.id, failure, exc)
                entry = state.phase(spec.id)
                entry["status"] = "failed"
                entry["error"] = self._error_record(failure)
                if i > publish_idx:
                    # the catalog entry is written: the image is published, whatever fails now is only a warning
                    warning = self.redactor.redact(
                        f"{spec.id} {spec.name} failed after the image was published: {failure.message}"
                    )
                    entry["status"] = "warning"
                    ctx.summary.warnings.append(warning)
                    self.log.warning(warning)
                    state.data["failure"] = None
                    state.save()
                    i += 1
                    continue
                state.save()
                if (
                    failure.kind == "transient"
                    and ctx.attempt < self.policy.max_attempts
                    and not options.dry_run
                ):
                    # nothing exists before P4, so a phase before it is repeated alone; from P4 on a NEW base VM is made
                    restart = max(start, min(retry_idx, i))
                    self._retry(ctx, state, failure, restart)
                    i = restart
                    continue
                return failure, False
            i += 1
        stopped = (
            options.stop_after is not None
            and phase_index(options.stop_after) < loop_end
        )
        return None, stopped

    def _retry(
        self, ctx: PhaseContext, state: StateFile, failure: Failure, restart: int
    ) -> None:
        self.log.warning(
            f"transient failure in {ctx.phase} (attempt {ctx.attempt}): {self.redactor.redact(failure.message)}; "
            f"cleaning up and starting again at {PHASE_IDS[restart]}"
        )
        self._diagnose(ctx, f"attempt{ctx.attempt}")
        assert ctx.cleanup is not None
        ctx.summary.cleanup_warnings.extend(ctx.cleanup.run("retry", since=restart))
        for pid in PHASE_IDS[restart:]:
            state.data["phases"][pid] = {"name": _SPEC[pid].name, "status": "pending"}
            ctx.results.pop(pid, None)
        ctx.attempt += 1
        state.data["attempt"] = ctx.attempt
        self._event(
            state,
            "retry",
            ctx.phase,
            f"attempt {ctx.attempt} from {PHASE_IDS[restart]}",
        )
        state.save()
        self.clock.sleep(self.policy.backoff_seconds)

    def _do_phase(self, ctx: PhaseContext, state: StateFile, spec: PhaseSpec) -> None:
        index = phase_index(spec.id)
        ctx.phase = spec.id
        assert ctx.cleanup is not None
        ctx.cleanup.phase = index
        registered = self.registry.get(spec.id)
        entry = state.phase(spec.id)
        if ctx.dry_run and not spec.read_only:
            text = self._describe(ctx, registered, spec)
            entry["status"] = "planned"
            ctx.summary.plan.append({"phase": spec.id, "name": spec.name, "text": text})
            state.save()
            return
        started = self.clock.monotonic()
        entry.update(
            status="running",
            started_at=_iso(self.clock.now()),
            attempt=ctx.attempt,
        )
        state.save()
        handler = logging.FileHandler(
            ctx.run_dir / f"{index:02d}-{spec.name}.log", encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)
        ctx.logger = self.log
        try:
            self.log.info(f"{spec.id} {spec.name}: start (attempt {ctx.attempt})")
            if registered is not None:
                result = registered.fn(ctx)
            elif spec.id == "P15":
                result = self._default_cleanup(ctx)
            else:
                result = (
                    None  # P16 without a body: the pipeline writes summary.json itself
                )
            if result is not None and not isinstance(result, Mapping):
                raise PermanentError(
                    f"phase {spec.id} returned {type(result).__name__}; it must return a mapping or None"
                )
            facts = self.redactor.redact_deep(jsonable(result or {}))
            ctx.results[spec.id] = facts
            entry.update(
                status="ok",
                ended_at=_iso(self.clock.now()),
                seconds=round(self.clock.monotonic() - started, 3),
                result=facts,
                error=None,
            )
            if spec.id == PUBLISH_PHASE:
                ctx.summary.published = True
                ctx.cleanup.commit()
            self.log.info(f"{spec.id} {spec.name}: ok")
            state.save()
        except BaseException:
            entry["ended_at"] = _iso(self.clock.now())
            entry["seconds"] = round(self.clock.monotonic() - started, 3)
            raise
        finally:
            self.log.removeHandler(handler)
            handler.close()

    def _describe(
        self, ctx: PhaseContext, registered: RegisteredPhase | None, spec: PhaseSpec
    ) -> str:
        if registered is None:
            if spec.id == "P15":
                return "delete the VMs, keys and work folders of this run (built in)"
            if spec.id == "P16":
                return "write summary.json (built in)"
            return "not implemented yet"
        if registered.describe is None:
            return f"would run {spec.name}"
        try:
            return str(registered.describe(ctx))
        except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
            return f"would run {spec.name} (no description: {type(exc).__name__})"

    def _default_cleanup(self, ctx: PhaseContext) -> dict[str, Any]:
        warnings = ctx.run_cleanup()
        leftover = []
        if not ctx.options.keep:
            try:
                leftover = ctx.leftover_vms()
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                warnings.append(f"could not compare the VMs with the start: {exc}")
            if leftover:
                ctx.summary.cleanup_warnings.append(
                    "VMs on the host that were not there at the start: "
                    + ", ".join(leftover)
                )
        return {"leftover_vms": leftover, "warnings": warnings}

    # -- failure records --
    def _error_record(self, failure: Failure) -> dict[str, Any]:
        return {
            "type": failure.type,
            "kind": failure.kind,
            "message": self.redactor.redact(failure.message),
        }

    def _record_failure(
        self,
        state: StateFile,
        phase_id: str,
        failure: Failure,
        exc: BaseException | None = None,
    ) -> None:
        message = self.redactor.redact(failure.message)
        state.data["failure"] = {
            "phase": phase_id,
            "kind": failure.kind,
            "message": message,
        }
        self._event(state, failure.kind, phase_id, message)
        if failure.kind in ("permanent", "transient"):
            self.log.error(f"{phase_id} failed ({failure.kind}): {message}")
            if failure.detail:
                self.log.error(self.redactor.redact(failure.detail))
            if exc is not None and not isinstance(exc, BuildError):
                self.log.error(
                    self.redactor.redact("".join(traceback.format_exception(exc)))
                )
        else:
            self.log.warning(f"{phase_id}: {failure.kind}: {message}")
        state.save()

    def _event(self, state: StateFile, event: str, phase_id: str, message: str) -> None:
        state.data["events"].append(
            {
                "at": _iso(self.clock.now()),
                "event": event,
                "phase": phase_id,
                "message": self.redactor.redact(message)[:500],
            }
        )

    # -- diagnostics, cleanup, summary --
    def _diagnose(self, ctx: PhaseContext, tag: str) -> None:
        if not ctx.diagnostics:
            return
        folder = ctx.run_dir / "diag" / tag
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for label, fn in ctx.diagnostics:
            try:
                fn(folder)
            except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
                self.log.warning(
                    f"diagnostic {label} failed: {self.redactor.redact(str(exc))}"
                )

    def _finish(
        self,
        ctx: PhaseContext,
        state: StateFile,
        failure: Failure | None,
        stopped: bool,
    ) -> RunOutcome:
        data = state.data
        summary = ctx.summary
        options = ctx.options
        if options.dry_run:
            status = "dry-run" if failure is None else failure.status
        elif failure is not None and summary.published:
            # the catalog entry exists: whatever went wrong afterwards (also a signal) is a warning
            summary.warnings.append(
                f"{failure.kind} after publishing: {failure.message}"
            )
            failure = None
            status = "published"
        elif failure is not None:
            status = failure.status
        elif stopped:
            status = "stopped"
        else:
            status = "published"
        assert ctx.cleanup is not None
        if options.dry_run and failure is None:
            summary.plan.append(
                {
                    "phase": "P16",
                    "name": "summary",
                    "text": self._describe(ctx, self.registry.get("P16"), _SPEC["P16"]),
                }
            )
            data["phases"]["P16"]["status"] = "planned"
        if not options.dry_run:
            if status in ("failed", "interrupted", "blocked", "stopped"):
                if status != "stopped":
                    self._diagnose(ctx, "final")
                keep = options.keep or (self.config.keep_failed and status == "failed")
            else:
                keep = False
            if status in ("deferred",):
                keep = False
            if status == "published":
                summary.cleanup_warnings.extend(
                    ctx.cleanup.run("success", keep=options.keep)
                )
            else:
                summary.cleanup_warnings.extend(ctx.cleanup.run("failure", keep=keep))
            kept = any(r["status"] == "kept" for r in data["resources"])
            data["kept"] = kept
            if kept:
                hours = self.config.limits.keep_hours
                data["keep_until"] = _iso(self.clock.now() + timedelta(hours=hours))
        data["status"] = status
        data["finished_at"] = _iso(self.clock.now())
        if failure is not None:
            data["failure"] = {
                "phase": data["failure"]["phase"] if data.get("failure") else ctx.phase,
                "kind": failure.kind,
                "message": self.redactor.redact(failure.message),
            }
        elif status == "published":
            data["failure"] = None
        summary.result = status
        summary.reason = self.redactor.redact(failure.message) if failure else ""
        summary.failed_phase = data["failure"]["phase"] if data.get("failure") else None
        summary.finished_at = data["finished_at"]
        summary.attempts = int(data.get("attempt", 1))
        summary.phases = {
            pid: {
                k: v
                for k, v in data["phases"][pid].items()
                if k in ("status", "seconds", "attempt")
            }
            for pid in PHASE_IDS
        }
        state.save()
        self._write_summary(ctx)
        if not options.dry_run:
            self._run_summary_phase(ctx, state)
        self._write_summary(ctx)
        event = {
            "published": "succeeded",
            "failed": "failed",
            "interrupted": "failed",
            "blocked": "blocked",
            "deferred": "deferred",
        }.get(status)
        if event and not options.dry_run:
            self._notify(event, summary.to_dict())
        return RunOutcome(
            status,
            summary.reason,
            ctx.run_id,
            ctx.run_dir,
            summary,
            summary.failed_phase,
        )

    def _run_summary_phase(self, ctx: PhaseContext, state: StateFile) -> None:
        """
        P16 for every outcome that has a run directory (the body decides what a failed/stopped run writes). It runs once,
        after the cleanup, with `ctx.summary.result` final. Its failure never changes the result.
        """
        entry = state.data["phases"]["P16"]
        registered = self.registry.get("P16")
        ctx.phase = "P16"
        started = self.clock.monotonic()
        entry.update(
            status="running", started_at=_iso(self.clock.now()), attempt=ctx.attempt
        )
        try:
            if registered is not None:
                registered.fn(ctx)
            entry["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
            message = self.redactor.redact(
                f"P16 summary failed: {type(exc).__name__}: {exc}"
            )
            entry["status"] = "warning"
            ctx.summary.warnings.append(message)
            self.log.error(message)
        entry["ended_at"] = _iso(self.clock.now())
        entry["seconds"] = round(self.clock.monotonic() - started, 3)
        state.save()

    def _write_summary(self, ctx: PhaseContext) -> None:
        _write_json(
            ctx.run_dir / "summary.json",
            self.redactor.redact_deep(ctx.summary.to_dict()),
        )

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if self.hooks.notify is None:
            return
        try:
            self.hooks.notify(event, payload)
        except Exception as exc:  # noqa: BLE001 - best effort, the original problem stays the one that is reported
            self.log.warning(f"the notify hook failed: {type(exc).__name__}")
