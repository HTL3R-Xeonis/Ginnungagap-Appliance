"""The pinned, pull-based appliance image builder. No command enables a timer."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

# Also works as an installed, isolated Python script (-I); never import tooling from a release being built.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ginnungagap.releases import SourceError
from image.build import names
from image.build.config import (
    DEFAULT_CONFIG_FILE,
    BuildHostConfig,
    ConfigError,
    load_config,
    read_secret,
)
from image.build.pipeline import (
    BuildLock,
    PhaseRegistry,
    Pipeline,
    Redactor,
    RunOptions,
    list_runs,
)
from image.build.ports import (
    AlreadyRunning,
    BuildError,
    Deferred,
    PermanentError,
    SystemClock,
)

TOOLING_ROOT = Path("/opt/ginnungagap-image-build")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build and accept an appliance image from a verified release."
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="Build a named release; unsigned/prerelease exceptions are manual only",
    )
    run.add_argument("--release", required=True)
    for flag in (
        "dry-run",
        "keep",
        "force-busy",
        "allow-prerelease",
        "allow-unsigned",
        "retry",
        "clean-base",
        "refresh-catalog",
    ):
        run.add_argument("--" + flag, action="store_true")
    run.add_argument("--stop-after", choices=[f"P{i}" for i in range(17)])
    run.add_argument("--resume")
    run.add_argument("--base")
    run.add_argument("--release-dir", type=Path)
    run.add_argument("--json", action="store_true")
    sub.add_parser("poll", help="Queue the newest unbuilt stable signed release")
    sub.add_parser("work", help=argparse.SUPPRESS)
    sub.add_parser("janitor", help="Clean only stale build-owned resources")
    for command in ("status", "list"):
        sub.add_parser(command).add_argument("--json", action="store_true")
    sub.add_parser(
        "serve-status",
        help="Serve the optional read-only status page until SIGTERM (needs status_http: true)",
    )
    sub.add_parser("logs").add_argument("run")
    sub.add_parser("show").add_argument("version")
    update = sub.add_parser(
        "self-update",
        help="Root-only manual replacement of pinned tooling from a signed release",
    )
    update.add_argument("--release", required=True)
    update.add_argument("--release-dir", type=Path)
    return p


def version(value: str) -> str:
    return names.check_version(value.removeprefix("v"))


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 16 * 1024 * 1024
    ):
        raise PermanentError(f"refusing unreadable or oversized state file {path}")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PermanentError(f"cannot read state file {path}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, scratch = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(scratch, path)
    finally:
        Path(scratch).unlink(missing_ok=True)


def runtime(config: BuildHostConfig, options: RunOptions) -> Pipeline:
    """Assemble the production ports and all phase bodies, lazily for read-only status commands."""
    if config.share.mode == "nfs" and not os.path.ismount(config.share.path):
        raise Deferred(
            "the template share is not mounted; image builds wait for its mount"
        )
    esxi = importlib.import_module("image.build.esxi")
    remote = importlib.import_module("image.build.remote")
    install = importlib.import_module("image.build.phases_install")
    release = importlib.import_module("image.build.release")
    publish = importlib.import_module("image.build.publish")
    acceptance = importlib.import_module("image.build.acceptance")
    inspection = importlib.import_module("image.build.inspect_image")
    clock = SystemClock()
    password = read_secret(config.esxi.password_file, credential_name="esxi205")
    registry = PhaseRegistry()
    release.register_phases(registry)
    esxi.register_phases(registry)
    install.register_phases(registry)
    inspection.register_phases(registry)
    publish.register_phases(registry)
    acceptance.register_phases(registry)
    share = (
        publish.LocalShare(config.share.path, config.ova_base_url)
        if config.share.mode == "nfs"
        else publish.SshShare(
            config.share.path,
            config.ova_base_url,
            host=config.share.ssh_host,
            user=config.share.ssh_user,
            key_file=config.share.ssh_key_file,
        )
    )
    return Pipeline(
        config=config,
        esxi=esxi.EsxiAdapter(
            config.esxi.host, config.esxi.user, password, clock=clock
        ),
        remote=remote.SshRemote(clock=clock),
        share=share,
        templates=publish.TemplateApi(config.template_api),
        registry=registry,
        clock=clock,
        secrets=[password],
    )


def execute(config: BuildHostConfig, wanted: str, options: RunOptions) -> Any:
    worker = runtime(config, options)
    try:
        return worker.run(wanted, options)
    finally:
        worker.esxi.close()


def poll(config: BuildHostConfig) -> int:
    if config.github.channel != "stable":
        raise PermanentError(
            "automatic image builds require the stable release channel"
        )
    release = importlib.import_module("image.build.release")
    publish = importlib.import_module("image.build.publish")
    state = config.paths.state_dir
    if (state / "PAUSE").exists():
        print("deferred: PAUSE file exists")
        return 3
    # Poll does not connect to ESXi and cannot pass unsigned or prerelease exceptions to the worker.
    picked = release.select_release(
        config, publish.TemplateApi(config.template_api), state_dir=state
    )
    if picked is None:
        print("no unbuilt stable release")
        return 0
    lock = BuildLock(state / "queue" / "lock")
    lock.acquire("poll")
    try:
        if (state / "queue" / "pending.json").exists() or (
            state / "queue" / "active.json"
        ).exists():
            print("a release is already queued")
            return 0
        write_json(
            state / "queue" / "pending.json",
            {
                "version": version(picked.version),
                "queued_at": SystemClock().now().isoformat(),
            },
        )
    finally:
        lock.release()
    print(f"queued v{picked.version}")
    return 0


def work(config: BuildHostConfig) -> int:
    state = config.paths.state_dir
    lock = BuildLock(state / "queue" / "lock")
    lock.acquire("claim")
    pending = state / "queue" / "pending.json"
    active = state / "queue" / "active.json"
    try:
        job = read_json(active) or read_json(pending)
        if job is None:
            print("queue empty")
            return 0
        if not isinstance(job, dict) or set(job) != {"version", "queued_at"}:
            raise PermanentError("invalid image build queue record")
        wanted = version(job["version"])
        if "-" in wanted:
            raise PermanentError("automatic image builds refuse prereleases")
        if not active.exists():
            os.replace(pending, active)
    finally:
        lock.release()
    # A host deferral is re-evaluated by the next poll; retaining a PathExists trigger would spin the service.
    try:
        outcome = execute(config, wanted, RunOptions())
        print_outcome(outcome)
        return outcome.exit_code
    finally:
        active.unlink(missing_ok=True)


def status(config: BuildHostConfig) -> dict[str, Any]:
    state = config.paths.state_dir
    runs = list_runs(state)
    latest = read_json(state / "latest.json")
    if latest is None and runs:
        latest = read_json(runs[-1].path.parent / "summary.json") or runs[-1].data
    return {
        "paused": (state / "PAUSE").exists(),
        "queued": read_json(state / "queue" / "pending.json"),
        "active": read_json(state / "queue" / "active.json"),
        "current": next(
            (run.data for run in reversed(runs) if run.data.get("status") == "running"),
            None,
        ),
        "latest": latest,
    }


def serve_status(config: BuildHostConfig) -> int:
    """The optional read-only HTTP status page, in the foreground until SIGTERM."""
    if not config.status_http:
        print(
            "the status page is off: set status_http: true in build-host.yml to serve it",
            file=sys.stderr,
        )
        return 2
    status_server = importlib.import_module("image.build.status_server")
    status_server.serve(config.paths.state_dir, config.status_bind, config.status_port)
    return 0


def print_outcome(outcome: Any, *, as_json: bool = False) -> None:
    if as_json:
        value = (
            outcome.summary.to_dict()
            if outcome.summary is not None
            else {"result": outcome.status, "reason": outcome.reason}
        )
        print(json.dumps(Redactor().redact_deep(value), indent=2))
    else:
        print(Redactor().redact(f"{outcome.status}: {outcome.reason}"))
        if outcome.run_id:
            print(f"run: {outcome.run_id}")


def self_update(config: BuildHostConfig, wanted: str, release_dir: Path | None) -> int:
    """The OLD pinned verifier validates all bytes before any new tooling can be executed."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise PermanentError("self-update requires root; it never runs automatically")
    from ginnungagap import manifest as manifest_module
    from ginnungagap.update import safe_extract, verify_with_ssh_keygen
    from image.build import release

    lock = BuildLock(config.paths.state_dir / "lock")
    lock.acquire("self-update")
    try:
        source = release.make_source(config, RunOptions(release_dir=release_dir))
        candidates = source.list_releases("stable")
        picked = next(
            (r for r in candidates if r.version == wanted and not r.prerelease), None
        )
        if picked is None:
            raise PermanentError("the requested stable tooling release was not found")
        with tempfile.TemporaryDirectory(
            prefix=".self-update-", dir=TOOLING_ROOT
        ) as scratch:
            folder = Path(scratch)
            for name in ("components.json", "components.json.sig", "SHA256SUMS"):
                source.fetch(picked, name, folder, max_bytes=release.CONTROL_LIMIT)
            declared = manifest_module.load(folder / "components.json")
            valid, _reason = verify_with_ssh_keygen(
                (folder / "components.json").read_bytes(),
                folder / "components.json.sig",
                config.paths.allowed_signers,
            )
            if not valid or declared.version != wanted or declared.tag != f"v{wanted}":
                raise PermanentError(
                    "the tooling release signature or version is not trusted"
                )
            for component in declared.components.values():
                if component.size > release.ASSET_LIMIT:
                    raise PermanentError("tooling component exceeds the download limit")
                source.fetch(picked, component.asset, folder, max_bytes=component.size)
            checked = release.verify_release_directory(
                folder, wanted, config.paths.allowed_signers
            )
            manifest = manifest_module.validate(checked["manifest"])
            component = manifest.components["appliance"]
            unpacked = safe_extract(
                folder / component.asset,
                folder / "unpacked",
                f"{component.repo_name}-{component.version}",
                2 * 1024**3,
            )
            if not (unpacked / "image" / "build_image.py").is_file():
                raise PermanentError("the signed release has no image builder tooling")
            destination = TOOLING_ROOT / "versions" / wanted
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise PermanentError("that tooling version is already installed")
            builder = manifest.components["builder"]
            unpacked_builder = safe_extract(
                folder / builder.asset,
                folder / "builder-unpacked",
                f"{builder.repo_name}-{builder.version}",
                2 * 1024**3,
            )
            destination.mkdir()
            destination.chmod(0o755)
            previous_umask = os.umask(0o022)
            try:
                shutil.copytree(unpacked, destination / "appliance", symlinks=False)
                shutil.copytree(
                    unpacked_builder, destination / "builder", symlinks=False
                )
                venv.EnvBuilder(with_pip=True).create(destination / "venv")
                python = destination / "venv/bin/python"
                subprocess.run(
                    [str(python), "-m", "pip", "install", str(destination / "builder")],
                    check=True,
                    timeout=900,
                )
                subprocess.run(
                    [
                        str(python),
                        "-I",
                        str(destination / "appliance/image/build_image.py"),
                        "--help",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    timeout=30,
                )
                # Tooling and its pinned Builder/venv move together; the service is never updated automatically.
                link = TOOLING_ROOT / ".current-new"
                link.unlink(missing_ok=True)
                link.symlink_to(destination, target_is_directory=True)
                os.replace(link, TOOLING_ROOT / "current")
            except BaseException:
                shutil.rmtree(destination)
                raise
            finally:
                os.umask(previous_umask)
        print(f"pinned image build tooling updated to v{wanted}")
        return 0
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config, check_secrets=False)
        credentials = os.environ.get("CREDENTIALS_DIRECTORY")
        if credentials:
            token = Path(credentials) / "github-token"
            share_key = Path(credentials) / "sharekey"
            if token.is_file():
                config = replace(
                    config, github=replace(config.github, token_file=token)
                )
            if share_key.is_file():
                config = replace(
                    config, share=replace(config.share, ssh_key_file=share_key)
                )
        if args.command == "run":
            opts = RunOptions(
                **{
                    k: getattr(args, k)
                    for k in (
                        "dry_run",
                        "stop_after",
                        "keep",
                        "force_busy",
                        "resume",
                        "base",
                        "release_dir",
                        "allow_prerelease",
                        "allow_unsigned",
                        "retry",
                        "clean_base",
                        "refresh_catalog",
                    )
                }
            )
            outcome = execute(config, version(args.release), opts)
            print_outcome(outcome, as_json=args.json)
            return outcome.exit_code
        if args.command == "poll":
            return poll(config)
        if args.command == "work":
            return work(config)
        if args.command == "janitor":
            worker = runtime(config, RunOptions())
            try:
                report = worker.run_janitor()
                print(json.dumps(asdict(report), indent=2))
                return 0 if report.clean else 1
            finally:
                worker.esxi.close()
        if args.command == "self-update":
            return self_update(config, version(args.release), args.release_dir)
        if args.command == "serve-status":
            return serve_status(config)
        if args.command == "status":
            body = status(config)
        elif args.command == "list":
            body = [s.data for s in list_runs(config.paths.state_dir)]
        elif args.command == "show":
            body = read_json(
                config.paths.state_dir
                / "builds"
                / version(args.version)
                / "summary.json"
            )
        else:
            if not names.RUN_ID.fullmatch(args.run):
                raise PermanentError("invalid run id")
            path = config.paths.state_dir / "runs" / args.run / "run.log"
            if path.is_symlink() or not path.is_file():
                raise PermanentError("no log for that run")
            with path.open() as log:
                for line in log:
                    print(Redactor().redact(line.rstrip()))
            return 0
        print(json.dumps(Redactor().redact_deep(body), indent=2))
        return 0
    except (AlreadyRunning, Deferred) as exc:
        print(Redactor().redact(str(exc)), file=sys.stderr)
        return 3
    except (
        BuildError,
        ConfigError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        SourceError,
    ) as exc:
        print(Redactor().redact(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
