import os
import stat
import subprocess
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
    kw.setdefault("inspect", lambda args: None)  # no Docker volume exists
    code = operate.apply(
        config_file, bundle, generator_sources, run=run, out=lines.append, **kw
    )
    return code, lines


def test_apply_renders_then_starts_with_a_build_and_removes_orphans(
    config_file, bundle, generator_sources
):
    run = Recorder()
    code, _ = run_apply(config_file, bundle, generator_sources, run)
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
    assert (bundle / "rendered" / "ova-store" / "Caddyfile").is_file()
    assert not any("ova-deploy" in " ".join(call) for call in run.calls)


def test_no_build_and_no_start_are_honoured(config_file, bundle, generator_sources):
    run = Recorder()
    run_apply(config_file, bundle, generator_sources, run, build=False)
    assert run.calls[0][-3:] == ["--no-build", "--pull", "never"]
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
    changed["appliance"]["public_url"] = "https://ginn.lab"
    changed["appliance"]["tls"] = {"mode": "self-signed"}  # changes only the Caddyfile
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


def test_the_ova_servers_own_file_is_owned_by_its_service():
    assert operate.FILE_OWNERS["ova-store/Caddyfile"] == "ova-store"
    assert "ova-store" in operate.STORE_USERS
    assert "ova-deploy" not in operate.STORE_USERS


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


def test_docker_is_run_from_the_bundle_folder_by_default(
    config_file, bundle, generator_sources, monkeypatch
):
    """compose reads COMPOSE_FILE relative to the working directory: from any other folder it can't find compose.yml."""
    seen = []

    def fake_run(args, check, cwd, **_):
        seen.append((args[:2], cwd))
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(operate.subprocess, "run", fake_run)
    monkeypatch.chdir(config_file.parent)  # not the bundle
    code = operate.apply(
        config_file,
        bundle,
        generator_sources,
        fetch=healthy_fetch,
        sleep=lambda s: None,
        out=lambda m: None,
    )
    assert code == 0
    assert seen and all(cwd == bundle for _, cwd in seen)


# ---------- images for an appliance image ----------


def test_build_images_builds_and_pulls_every_profile_and_leaves_no_rendered_files(
    bundle, generator_sources
):
    run = Recorder()
    lines: list[str] = []
    assert (
        operate.build_images(bundle, generator_sources, run=run, out=lines.append) == 0
    )
    build, pull = run.calls
    assert build[6:] == ["--profile", "*", "build"]
    assert pull[6:] == ["--profile", "*", "pull", "--ignore-buildable"]
    assert not (
        bundle / "rendered"
    ).exists()  # nothing of the throw-away configuration stays in the image


class Inspector:
    """Stands in for the read-only docker calls: answers by the command."""

    def __init__(self, needed, present):
        self.needed, self.present = needed, present
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if "config" in args:
            return self.needed
        return self.present


NEEDED = "ginnungagap-ginnungagap\ncaddy:2\nghcr.io/x/y:1.0\nregistry:5000/z\n"
PRESENT = (
    "ginnungagap-ginnungagap:latest\ncaddy:2\nghcr.io/x/y:1.0\nregistry:5000/z:latest\n"
    "ginnungagap-ova-deploy:latest\ncaddy:1\n<none>:<none>\n"
)


def test_build_images_can_remove_every_tagged_image_the_compose_project_does_not_use(
    bundle, generator_sources
):
    run = Recorder()
    inspect = Inspector(NEEDED, PRESENT)
    lines: list[str] = []
    code = operate.build_images(
        bundle,
        generator_sources,
        run=run,
        out=lines.append,
        remove_other=True,
        inspect=inspect,
    )
    assert code == 0
    removed = [c for c in run.calls if c[:3] == ["docker", "image", "rm"]]
    assert removed == [
        ["docker", "image", "rm", "caddy:1"],
        ["docker", "image", "rm", "ginnungagap-ova-deploy:latest"],
    ]
    # the needed images are asked for while the rendered environment still exists, with every profile
    assert inspect.calls[0][-4:] == ["--profile", "*", "config", "--images"]
    assert not (bundle / "rendered").exists()


def test_build_images_keeps_every_image_unless_asked(bundle, generator_sources):
    run = Recorder()
    operate.build_images(
        bundle,
        generator_sources,
        run=run,
        out=lambda m: None,
        inspect=Inspector("", ""),
    )
    assert not any(c[:3] == ["docker", "image", "rm"] for c in run.calls)


@pytest.mark.parametrize(
    ("needed", "present"), [("", PRESENT), (NEEDED, None), (None, PRESENT)]
)
def test_no_image_is_removed_when_a_list_cannot_be_read(
    bundle, generator_sources, needed, present
):
    run = Recorder()
    lines: list[str] = []
    code = operate.build_images(
        bundle,
        generator_sources,
        run=run,
        out=lines.append,
        remove_other=True,
        inspect=Inspector(needed, present),
    )
    assert code == 1
    assert not any(c[:3] == ["docker", "image", "rm"] for c in run.calls)
    assert any("no image was removed" in line for line in lines)
    assert not (bundle / "rendered").exists()


def test_a_failing_image_removal_fails_the_build_step(bundle, generator_sources):
    run = Recorder([0, 0, 1])  # build, pull, then the first removal
    code = operate.build_images(
        bundle,
        generator_sources,
        run=run,
        out=lambda m: None,
        remove_other=True,
        inspect=Inspector(NEEDED, PRESENT),
    )
    assert code == 1


def test_build_images_stops_at_the_first_failure_and_still_cleans_up(
    bundle, generator_sources
):
    run = Recorder([1])
    lines: list[str] = []
    assert (
        operate.build_images(bundle, generator_sources, run=run, out=lines.append) == 1
    )
    assert len(run.calls) == 1 and "building the images failed" in lines
    assert not (bundle / "rendered").exists()


def test_build_images_needs_the_generators_files(bundle, tmp_path):
    run = Recorder()
    assert (
        operate.build_images(bundle, tmp_path / "nothing", run=run, out=lambda m: None)
        == 1
    )
    assert run.calls == []


OLD_NFS_VOLUME = '{"device":":/mnt/pool/templates","o":"addr=10.20.20.180,ro,nfsvers=4,soft,timeo=100","type":"nfs"}'


def test_a_template_store_volume_with_old_options_is_recreated_before_start(
    config_file, bundle, generator_sources
):
    run = Recorder()
    seen = []

    def inspect(args):
        seen.append(args)
        return OLD_NFS_VOLUME  # the volume was made for an NFS store, the config now says local

    code, lines = run_apply(
        config_file, bundle, generator_sources, run, inspect=inspect
    )
    assert code == 0
    volume_lookup = next(a for a in seen if a[:3] == ["docker", "volume", "inspect"])
    assert volume_lookup[-1] == "ginnungagap_template-store"
    commands = run.commands()
    assert commands[0].endswith(
        "rm -sf template-esxi collector-esxi ova-store ginnungagap"
    )
    assert commands[1] == "docker volume rm ginnungagap_template-store"
    assert commands[2].startswith(
        "up -d --remove-orphans"
    )  # only then the services start
    assert any("template store changed" in line for line in lines)


def test_a_template_store_volume_that_matches_is_left_alone(
    config_file, bundle, generator_sources
):
    run_apply(config_file, bundle, generator_sources, Recorder())
    wanted = operate._wanted_store_options(bundle / "rendered")
    assert wanted is not None and wanted["type"] == "none"
    run = Recorder()
    run_apply(
        config_file,
        bundle,
        generator_sources,
        run,
        inspect=lambda args: __import__("json").dumps(wanted),
    )
    assert len(run.calls) == 1  # just the up


def test_a_failing_volume_removal_stops_before_the_start(
    config_file, bundle, generator_sources
):
    run = Recorder([0, 1])  # rm of the containers works, removing the volume does not
    code, lines = run_apply(
        config_file, bundle, generator_sources, run, inspect=lambda args: OLD_NFS_VOLUME
    )
    assert code == 1
    assert "recreating the template store volume failed" in lines
    assert not any("up -d" in c for c in run.commands())


def test_the_services_stopped_for_the_store_volume_are_exactly_the_ones_that_mount_it():
    bundle = Path(operate.__file__).resolve().parent.parent
    users = {
        name
        for file in ("compose.yml", "compose.template-share.yml")
        for name, service in yaml.safe_load((bundle / file).read_text())[
            "services"
        ].items()
        if any(
            str(volume).startswith(f"{operate.STORE_VOLUME}:")
            for volume in service.get("volumes", [])
        )
    }
    assert users == set(operate.STORE_USERS)


def test_apply_creates_a_missing_local_template_store_folder(
    config_file, bundle, generator_sources, tmp_path
):
    made = []
    code, lines = run_apply(
        config_file, bundle, generator_sources, Recorder(), make_store_dir=made.append
    )
    assert code == 0
    assert made == [tmp_path / "store"]
    assert any("template store folder" in line for line in lines)


def test_apply_leaves_an_existing_local_store_alone(
    config_file, bundle, generator_sources, tmp_path
):
    (tmp_path / "store").mkdir()
    made = []
    run_apply(
        config_file, bundle, generator_sources, Recorder(), make_store_dir=made.append
    )
    assert made == []


def test_apply_does_not_create_a_folder_for_an_nfs_store(
    config_file, bundle, generator_sources
):
    data = yaml.safe_load(config_file.read_text())
    data["templates"]["store"] = {
        "type": "nfs",
        "nfs": {"server": "10.0.0.1", "export": "/templates"},
    }
    config_file.write_text(yaml.safe_dump(data))
    made = []
    run_apply(
        config_file, bundle, generator_sources, Recorder(), make_store_dir=made.append
    )
    assert made == []


def test_a_failed_up_does_not_make_the_next_apply_skip_recreating_the_gateway(
    config_file, bundle, generator_sources
):
    rendered = bundle / "rendered"
    rendered.mkdir()
    (rendered / "Caddyfile").write_text("# bootstrap\n", encoding="utf-8")
    (rendered / "compose.env").write_text(
        "COMPOSE_PROFILES=gateway,setup\n", encoding="utf-8"
    )

    code, _ = run_apply(config_file, bundle, generator_sources, Recorder(codes=[1]))
    assert code == 1  # `docker compose up` failed after the files were already rendered

    run = Recorder()
    code, lines = run_apply(config_file, bundle, generator_sources, run)
    assert code == 0
    assert any("--force-recreate" in c and "gateway" in c for c in run.commands())
    assert any("Recreated because" in line and "gateway" in line for line in lines)

    run = Recorder()
    run_apply(config_file, bundle, generator_sources, run)
    assert not any("--force-recreate" in c for c in run.commands())  # now it is applied


def test_apply_behind_a_login_is_not_a_failure(config_file, bundle, generator_sources):
    def behind_login(url):
        return 401, "", None

    code, lines = run_apply(
        config_file, bundle, generator_sources, Recorder(), fetch=behind_login
    )
    assert code == 0
    assert any("ginnungagap status --user" in line for line in lines)


def test_apply_still_fails_when_a_service_is_down_behind_a_login(
    config_file, bundle, generator_sources
):
    def mixed(url):
        return (
            (401, "", None)
            if url.endswith("/health") and "/templates" not in url
            else (502, "", None)
        )

    code, _ = run_apply(
        config_file, bundle, generator_sources, Recorder(), fetch=mixed, wait_seconds=0
    )
    assert code == 1
