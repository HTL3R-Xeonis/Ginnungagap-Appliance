import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from ginnungagap import operate


class Recorder:
    """Stands in for Docker: records every command, answers with the next prepared exit code (default 0)."""

    def __init__(self, codes=()):
        self.calls: list[list[str]] = []
        self.codes = list(codes)

    def __call__(self, args):
        self.calls.append(args)
        return self.codes.pop(0) if self.codes else 0

    def commands(self):
        return [
            " ".join(c[c.index("up") if "up" in c else 0 :])
            if c[:2] == ["docker", "compose"]
            else " ".join(c)
            for c in self.calls
        ]


@pytest.fixture
def bundle(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    return folder


@pytest.fixture
def config_file(tmp_path, example):
    example["templates"]["store"]["local"]["path"] = str(tmp_path / "store")
    path = tmp_path / "ginnungagap.yml"
    path.write_text(yaml.safe_dump(example))
    return path


def healthy_fetch(url):
    path = "/" + url.split("/", 3)[3]
    bodies = {
        "/health": {"warning": None},
        "/templates/esxi/health": {"templates": 1},
        "/templates/gns3/health": {"templates": 1, "servers": 1},
        "/generator/health": {"servers": 1},
    }
    import json

    return 200, json.dumps(bodies[path]), None


def run_apply(config_file, bundle, generator_sources, run, **kw):
    lines: list[str] = []
    kw.setdefault("fetch", healthy_fetch)
    kw.setdefault("sleep", lambda s: None)
    code = operate.apply(
        config_file, bundle, generator_sources, run=run, out=lines.append, **kw
    )
    return code, lines


def test_apply_renders_then_starts_with_a_build_and_removes_orphans(
    config_file, bundle, generator_sources
):
    run = Recorder()
    code, lines = run_apply(config_file, bundle, generator_sources, run)
    assert code == 0
    assert (bundle / "rendered" / "compose.env").is_file()
    assert run.calls[0][:6] == [
        "docker",
        "compose",
        "--project-directory",
        str(bundle),
        "--env-file",
        str(bundle / "rendered" / "compose.env"),
    ]
    assert run.calls[0][6:] == ["up", "-d", "--remove-orphans", "--build"]
    assert len(run.calls) == 1  # the first render recreates nothing by hand
    assert any("ova-deploy" in line for line in lines)


def test_no_build_and_no_start_are_honoured(config_file, bundle, generator_sources):
    run = Recorder()
    run_apply(config_file, bundle, generator_sources, run, build=False)
    assert "--build" not in run.calls[0]
    run = Recorder()
    code, _ = run_apply(
        config_file, bundle / "other", generator_sources, run, start=False
    )
    assert code == 0 and run.calls == []


def test_an_invalid_config_starts_nothing(tmp_path, bundle, generator_sources):
    bad = tmp_path / "bad.yml"
    bad.write_text("servises: {}\n")
    run = Recorder()
    code, lines = run_apply(bad, bundle, generator_sources, run)
    assert code == 1 and run.calls == []
    assert any("servises" in line for line in lines)
    assert not (bundle / "rendered").exists()


def test_only_the_service_whose_file_changed_is_recreated(
    config_file, bundle, generator_sources
):
    run_apply(config_file, bundle, generator_sources, Recorder())
    changed = yaml.safe_load(config_file.read_text())
    changed["ova_deploy"]["external_url"] = (
        "http://other-host:8003"  # changes the Caddyfile (the /ova/ route) and operator.env
    )
    config_file.write_text(yaml.safe_dump(changed))
    run = Recorder()
    code, lines = run_apply(config_file, bundle, generator_sources, run)
    assert code == 0
    assert run.calls[1][6:] == [
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "gateway",
    ]  # not the app, not the generator
    assert any(
        "Recreated because their configuration changed: gateway" in line
        for line in lines
    )


def test_an_unchanged_config_recreates_nothing(config_file, bundle, generator_sources):
    run_apply(config_file, bundle, generator_sources, Recorder())
    run = Recorder()
    run_apply(config_file, bundle, generator_sources, run)
    assert len(run.calls) == 1


def test_a_failing_compose_up_is_reported(config_file, bundle, generator_sources):
    code, lines = run_apply(config_file, bundle, generator_sources, Recorder([1]))
    assert code == 1 and "docker compose up failed" in lines


def test_apply_waits_until_everything_is_healthy(
    config_file, bundle, generator_sources
):
    import json

    calls = {"n": 0}

    def slow_fetch(url):
        calls["n"] += 1
        if calls["n"] <= 5:  # the first round of checks: the app is not up yet
            return 502, "", None
        return healthy_fetch(url)

    sleeps = []
    code, _ = run_apply(
        config_file,
        bundle,
        generator_sources,
        Recorder(),
        fetch=slow_fetch,
        sleep=sleeps.append,
        wait_seconds=30,
    )
    assert code == 0 and sleeps  # it slept and asked again
    assert json  # (kept for the helper import)


def test_apply_gives_up_when_it_never_gets_healthy(
    config_file, bundle, generator_sources
):
    code, lines = run_apply(
        config_file,
        bundle,
        generator_sources,
        Recorder(),
        fetch=lambda url: (502, "", None),
        wait_seconds=0,
    )
    assert code == 1
    assert any("down" in line for line in lines)


# ---------- backup and restore ----------


def test_backup_copies_the_config_and_reads_the_data_volume_read_only(
    config_file, bundle, tmp_path
):
    run = Recorder()
    (bundle / "rendered").mkdir()
    (bundle / "rendered" / "compose.env").write_text("COMPOSE_PROJECT_NAME=myproj\n")
    lines: list[str] = []
    folder = operate.backup(
        config_file,
        bundle,
        tmp_path / "out",
        run=run,
        now=lambda: datetime(2026, 11, 2, 12, 30, 5, tzinfo=UTC),
        out=lines.append,
    )
    assert folder == tmp_path / "out" / "ginnungagap-backup-20261102-123005"
    assert (folder / "ginnungagap.yml").read_text() == config_file.read_text()
    assert stat.S_IMODE((folder / "ginnungagap.yml").stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    command = run.calls[0]
    assert "myproj_data:/data:ro" in command and f"{folder}:/backup" in command
    assert (
        f"chown {os.getuid()}:{os.getgid()} /backup/data.tgz" in command[-1]
        and "chmod 600" in command[-1]
    )


def test_a_failing_backup_returns_nothing(config_file, bundle, tmp_path):
    assert (
        operate.backup(
            config_file, bundle, tmp_path / "out", run=Recorder([1]), out=lambda m: None
        )
        is None
    )


def make_backup(tmp_path: Path) -> Path:
    folder = tmp_path / "bk"
    folder.mkdir()
    (folder / "data.tgz").write_bytes(b"x")
    return folder


def test_restore_stops_the_app_replaces_the_volume_and_starts_it_again(
    bundle, tmp_path
):
    run = Recorder()
    code = operate.restore(make_backup(tmp_path), bundle, run=run, out=lambda m: None)
    assert code == 0
    stop, replace, start = run.calls
    assert stop[6:] == ["stop", "ginnungagap", "collector-gns3"]
    assert (
        "ginnungagap_data:/data" in replace
        and "find /data -mindepth 1 -delete && tar xzf /backup/data.tgz -C /data"
        in replace[-1]
    )
    assert start[6:] == ["up", "-d", "ginnungagap", "collector-gns3"]


def test_restore_refuses_a_folder_that_is_not_a_backup(bundle, tmp_path):
    run = Recorder()
    assert operate.restore(tmp_path, bundle, run=run, out=lambda m: None) == 1
    assert run.calls == []


def test_a_failed_restore_does_not_start_the_services_on_half_written_data(
    bundle, tmp_path
):
    run = Recorder([0, 1])
    lines: list[str] = []
    assert (
        operate.restore(make_backup(tmp_path), bundle, run=run, out=lines.append) == 1
    )
    assert len(run.calls) == 2  # stop, the failed replace - no start
    assert any("left stopped" in line for line in lines)
