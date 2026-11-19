"""`ginnungagap update` against a fake appliance (no Docker, no network): the happy paths."""

import json
import os

import pytest

from ginnungagap import manifest as mf
from ginnungagap import update as up
from tests.helpers.update_world import World


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


def operators_only(world):
    """Release A: what the first `--apply` installs on top of the old checkouts."""
    return world.release("a", appliance="1.3.0")


# ---- check, list, versions ----


def test_check_reports_an_available_update_with_exit_10(world):
    a = operators_only(world)
    code = world.update("check", offline=a)
    assert code == up.EXIT_AVAILABLE
    text = world.text()
    assert "Available: v1.3.0 (stable, 2026-12-01)" in text
    assert "source: directory" in text
    assert "operator" in text and "ginnungagap (about 10-20 s of downtime)" in text
    assert "signature: NOT verified" in text and "data migration: no" in text
    record = json.loads(world.layout.available.read_text())
    assert (
        record["update_available"] is True and record["available"]["version"] == "1.3.0"
    )
    assert record["error"] is None
    assert world.docker.calls == []  # a check never touches Docker


def test_check_with_nothing_new_is_exit_0(world):
    a = operators_only(world)
    assert world.update("apply", offline=a) == 0
    world.out.clear()
    assert world.update("check", offline=a) == 0
    assert "Nothing to update" in world.text()
    assert json.loads(world.layout.available.read_text())["update_available"] is False


def test_check_json_is_machine_readable(world, capsys):
    a = operators_only(world)
    assert world.update("check", offline=a, json=True) == up.EXIT_AVAILABLE
    data = json.loads(capsys.readouterr().out)
    assert data["update_available"] is True and data["available"] == "1.3.0"
    assert {c["component"] for c in data["changes"]} == set(mf.COMPONENT_NAMES)


def test_list_versions_and_history(world, capsys):
    a = operators_only(world)
    assert world.update("list", offline=a) == 0
    assert "1.3.0" in world.text() and "stable" in world.text()
    world.out.clear()
    assert world.update("versions") == 0
    assert "not recorded" in world.text() and "--adopt" in world.text()
    assert world.update("apply", offline=a) == 0
    world.out.clear()
    assert world.update("versions") == 0
    assert "v1.3.0" in world.text() and "operator" in world.text()
    world.out.clear()
    assert world.update("history") == 0
    assert "1.3.0  (current)" in world.text() and "adopted" in world.text()
    assert world.update("versions", json=True) == 0
    assert (
        json.loads(capsys.readouterr().out)["installed"]["appliance"]["version"]
        == "1.3.0"
    )


# ---- the first apply: adopting the old checkouts, then updating ----


def test_the_first_apply_adopts_the_old_checkouts_and_updates_everything(world):
    a = operators_only(world)
    assert world.update("apply", offline=a) == 0
    installed = world.installed()
    assert (
        installed["appliance"]["version"] == "1.3.0"
        and installed["previous"] == "adopted"
    )
    assert installed["backup"] and world.backups
    assert installed["components"]["operator"]["version"] == "1.2.1"
    # every component but the operator (1.2.1 = 1.2.1) was 'unknown' and is replaced
    link = world.layout.components
    assert os.readlink(link / "TopologyBuilder") == "../releases/TopologyBuilder/1.0.0"
    assert (
        os.readlink(link / "topology-generator")
        == "../releases/topology-generator/1.0.0"
    )
    assert (
        os.readlink(link / "TopologyOperator")
        == "../releases/TopologyOperator/adopted-1"
    )
    assert (link / "TopologyBuilder" / "pyproject.toml").is_file()
    # the old checkout was copied (without .git), never touched
    assert (
        world.layout.releases / "Ginnungagap-Appliance" / "adopted-1" / "install.sh"
    ).is_file()
    assert not (
        world.layout.releases / "Ginnungagap-Appliance" / "adopted-1" / ".git"
    ).exists()
    assert (world.repos / "Ginnungagap-Appliance" / ".git").is_dir()
    # install.env now points at the components folder
    env = (world.config_dir / "install.env").read_text()
    assert (
        f"REPOS_DIR={link}" in env
        and f"GINNUNGAGAP_GENERATOR_CONFIG={link}/topology-generator/config" in env
    )
    assert os.environ["REPOS_DIR"] == str(link)


def test_the_happy_path_runs_exactly_these_docker_commands_in_this_order(world):
    a = operators_only(world)
    assert world.update("apply", offline=a) == 0
    commands = world.docker.compose()
    build = next(c for c in commands if c.startswith("build"))
    assert build.split()[1:] == [
        "generator",
        "collector-esxi",
        "collector-gns3",
        "template-esxi",
        "template-gns3",
        "setup",
        "ginnungagap",
    ]
    ups = [c for c in commands if c.startswith("up")]
    flags = "-d --no-deps --no-build --pull never"
    assert ups == [
        f"up {flags} generator",
        f"up {flags} collector-esxi collector-gns3",
        f"up {flags} template-esxi template-gns3",
        f"up {flags} setup",
        f"up {flags} ginnungagap",
    ]
    # old images are kept as the rollback target before anything is built
    tags = [c for c in world.docker.commands() if c.startswith("docker tag")]
    assert (
        "docker tag ginnungagap-ginnungagap:latest ginnungagap-ginnungagap:rollback-adopted"
        in tags
    )
    order = world.docker.commands()
    assert order.index(tags[0]) < next(i for i, c in enumerate(order) if " build " in c)
    # the final health gate is the existing apply, offline
    assert len(world.apply.calls) == 1
    assert world.apply.calls[0]["build"] is False
    assert world.apply.calls[0]["config"] == world.config_file
    # the tooling was installed from the new release, then the NEW program continued
    install = next(c for c in world.docker.commands() if c.startswith("bash "))
    assert "/releases/Ginnungagap-Appliance/1.3.0/install.sh --repos-dir" in install
    assert f"--manifest {world.layout.downloads}/1.3.0/components.json" in install
    assert f"--state-dir {world.root}" in install and "--no-start" in install
    assert f"--prefix {world.bundle.parent}" in install
    assert len(world.reexecs) == 1


def test_only_what_changed_is_rebuilt_and_restarted(world):
    a = operators_only(world)
    assert world.update("apply", offline=a) == 0
    world.docker.calls.clear()
    b = world.release(
        "b", appliance="1.3.1", components={"docker-server-files": "1.0.1"}
    )
    assert world.update("apply", offline=b) == 0
    commands = world.docker.compose()
    # appliance changed too (1.3.0 -> 1.3.1): setup and the four template/collector services, nothing else
    build = next(c for c in commands if c.startswith("build")).split()[1:]
    assert sorted(build) == sorted(
        ["setup", "template-esxi", "collector-esxi", "template-gns3", "collector-gns3"]
    )
    assert not any(
        "ginnungagap" in c.split()[-1:] for c in commands if c.startswith("up")
    )
    installed = world.installed()
    assert (
        installed["appliance"]["version"] == "1.3.1"
        and installed["previous"] == "1.3.0"
    )
    assert installed["components"]["docker-server-files"]["version"] == "1.0.1"
    assert (
        os.readlink(world.layout.components / "Docker-Server-Files")
        == "../releases/Docker-Server-Files/1.0.1"
    )
    # the previous version stays for the rollback, nothing older
    assert (world.layout.releases / "Docker-Server-Files" / "1.0.0").is_dir()
    assert not (world.layout.releases / "Docker-Server-Files" / "adopted-1").exists()
    assert not world.layout.transaction.exists() and not list(
        world.layout.downloads.glob("*")
    )


def test_a_release_without_any_changed_service_changes_nothing(world):
    a = operators_only(world)
    assert world.update("apply", offline=a) == 0
    world.docker.calls.clear()
    assert world.update("apply", offline=a) == 0
    assert "Nothing to update" in world.text()
    assert world.docker.compose() == []


def test_the_operators_version_is_checked_after_the_restart(world):
    a = operators_only(world)
    world.operator_reports = (
        "1.2.1"  # the new operator would still be 1.2.1 here (unchanged): no check
    )
    assert world.update("apply", offline=a) == 0
    b = world.release("b", appliance="1.4.0", components={"operator": "1.3.0"})
    world.operator_reports = "1.2.1"  # the old container is still answering
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "reports version 1.2.1, expected 1.3.0" in world.text()
    world.operator_reports = "1.3.0"
    world.err.clear()
    assert world.update("apply", offline=b) == 0


# ---- the other actions that change nothing ----


def test_dry_run_changes_nothing(world):
    a = operators_only(world)
    before = sorted(
        p
        for p in world.tmp.rglob("*")
        if "releases" not in p.relative_to(world.tmp).parts[:1]
    )
    world.root_user = False  # a dry run needs no root
    assert world.update("apply", offline=a, dry_run=True) == 0
    assert "Plan:" in world.text() and "Dry run" in world.text()
    after = sorted(
        p
        for p in world.tmp.rglob("*")
        if "releases" not in p.relative_to(world.tmp).parts[:1]
    )
    assert before == after
    assert world.docker.calls == [] and not world.layout.root.exists()


def test_a_confirmation_is_asked_and_a_no_changes_nothing(world):
    a = operators_only(world)
    world.confirm = False
    assert world.update("apply", offline=a, yes=False) == up.EXIT_REFUSED
    assert "not confirmed" in world.text()
    assert not world.layout.installed.exists() and not world.layout.transaction.exists()
    assert world.docker.compose() == []


def test_json_output_of_an_apply_keeps_stdout_clean(world, capsys):
    a = operators_only(world)
    assert world.update("apply", offline=a, json=True) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "installed": "1.3.0"}
    assert "Downloading" in captured.err  # progress goes to stderr in --json mode


def test_a_refused_update_in_json_mode_says_why(world, capsys):
    a = operators_only(world)
    world.free = 1
    assert world.update("apply", offline=a, json=True) == up.EXIT_REFUSED
    data = json.loads(capsys.readouterr().out)
    assert (
        data["ok"] is False and data["exit_code"] == 4 and "disk space" in data["error"]
    )
