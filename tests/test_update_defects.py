"""
The four defects the live test of the update mechanism found (a throw-away appliance VM), reproduced with a fake Docker and fixed:
1. the Operator's state is lost on every container recreate (carried over before the recreate; see also test_state_migration.py)
2. the first update of an adopted appliance could not be rolled back (the old install.sh does not know --state-dir/--manifest)
3. after kill -9 an orphaned `docker compose build` moved :latest, so a rollback restored the NEW image and reported success
4. a crash-looping service passed the health step (`running` with an empty health field)
plus the small findings (line-buffered output).
"""

import json
import os
import subprocess
import sys

import pytest

from ginnungagap import update as up
from tests.helpers.update_world import Crash, World

BUILD = " build "


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


@pytest.fixture
def installed(world):
    """The appliance after a first update to 1.3.0 (with a recorded release, an update state and a history)."""
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    world.docker.calls.clear()
    world.out.clear()
    world.apply.calls.clear()
    return a


def b_release(world, **kwargs):
    kwargs.setdefault("appliance", "1.3.1")
    kwargs.setdefault("components", {"template-services": "1.0.1"})
    return world.release("b", **kwargs)


def updater(world) -> up.Updater:
    from ginnungagap import config as config_module

    config = config_module.load(world.config_file)
    return up.Updater(
        world.layout,
        up.Settings.from_config(config),
        up.Options(action="apply", yes=True),
        world.env,
        config,
        world.config_file,
        world.bundle,
        None,
    )


def transaction(world) -> dict:
    return json.loads(world.layout.transaction.read_text())


def tag_commands(world):
    return [c for c in world.docker.commands() if c.startswith("docker tag")]


# ---- defect 4: a service that crash-loops is not healthy ----


def test_a_crash_looping_operator_is_caught_at_the_service_step_not_at_the_final_gate(
    world, installed
):
    b = b_release(world, components={"operator": "1.3.0"})
    world.operator_reports = "1.3.0"
    world.docker.crash_loop = {"ginnungagap"}
    world.docker.health["ginnungagap"] = (
        "running starting"  # `running` at the moment of the first look
    )

    def stop_looping_after_rollback(args):
        if (
            args[:2] == ["docker", "tag"]
            and args[3] == "ginnungagap-ginnungagap:latest"
        ):
            world.docker.crash_loop = set()
            world.docker.health["ginnungagap"] = "running healthy"
            world.operator_reports = "1.2.1"  # the old Operator answers again

    world.docker.on_run = [stop_looping_after_rollback]
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "ginnungagap is not healthy: it keeps restarting" in world.text()
    # the 120 s final gate (operate.apply) never ran for the update (the service step caught it); the rollback's apply was
    # done by the restored program
    assert world.apply.calls == [] and len(world.apply_installed.calls) == 1
    assert world.audit()[-1]["result"] == "rolled_back"


def test_running_without_a_health_answer_yet_is_not_healthy(world):
    updater_ = updater(world)
    world.docker.health["ginnungagap"] = "running starting"
    problem = updater_.wait_service("ginnungagap", 20)
    assert problem and "is not healthy 20 s after it was restarted" in problem
    assert "health starting" in problem


def test_a_service_without_a_health_check_must_stay_up_for_a_while(world):
    updater_ = updater(world)
    world.docker.health["gateway"] = "running"  # no health check: the field is `none`
    assert updater_.wait_service("gateway", 60) is None
    # ... and one that restarts by itself in that time is not up
    world.docker.crash_loop = {"gateway"}
    problem = updater_.wait_service("gateway", 60)
    assert problem and "keeps restarting" in problem


def test_healthy_is_what_a_service_with_a_health_check_needs(world):
    updater_ = updater(world)
    world.docker.health["ginnungagap"] = "running healthy"
    assert updater_.wait_service("ginnungagap", 20) is None
    world.docker.health["ginnungagap"] = "running unhealthy"
    assert "health check fails" in updater_.wait_service("ginnungagap", 20)
    world.docker.health["ginnungagap"] = "exited none"
    assert "stopped (exited)" in updater_.wait_service("ginnungagap", 20)


def test_restarts_of_the_old_container_do_not_count_but_new_ones_do(world):
    updater_ = updater(world)
    world.docker.restarts["ginnungagap"] = 3
    before = updater_.probe_service("ginnungagap")
    assert before is not None and before.restarts == 3
    assert (
        updater_.wait_service("ginnungagap", 20, before) is None
    )  # the same container, no new restart
    world.docker.restarts["ginnungagap"] = 4
    assert "keeps restarting" in updater_.wait_service("ginnungagap", 20, before)
    # a recreated container starts counting from zero, whatever the old one had
    other = up.Probe("cid-other", "running", "healthy", 9)
    world.docker.restarts["ginnungagap"] = 0
    assert updater_.wait_service("ginnungagap", 20, other) is None


# ---- defect 3: a killed engine, an orphaned build, a moved tag ----


def crash_in_build(world, release):
    world.docker.crash_on = BUILD
    with pytest.raises(Crash):
        world.update("apply", offline=release)
    world.docker.crash_on = None
    assert transaction(world)["phase"] == "switched"


def test_resume_stops_a_build_the_killed_engine_left_running_before_it_rolls_back(
    world, installed
):
    crash_in_build(world, b_release(world))
    world.orphans = [4242]
    original = world.stop_builds

    def recording(bundle):
        world.docker.calls.append(["<stopped the orphaned build>"])
        return original(bundle)

    world.env.stop_builds = recording
    assert world.update("resume") == 0
    assert world.stopped_builds == [world.bundle]
    commands = world.docker.commands()
    stopped = commands.index("<stopped the orphaned build>")
    first_tag_back = next(
        i
        for i, c in enumerate(commands)
        if c.startswith("docker tag") and c.endswith(":latest")
    )
    assert stopped < first_tag_back
    assert "Stopped 1 process(es)" in world.text()


def test_the_rollback_puts_the_old_image_back_by_id_even_when_latest_moved(
    world, installed
):
    old_id = world.docker.ids["ginnungagap-template-esxi:latest"]
    crash_in_build(world, b_release(world))
    # the orphaned build finished after the engine died and moved the tag to the new image
    world.docker.ids["ginnungagap-template-esxi:latest"] = "sha256:NEW-IMAGE"
    world.docker.ids["ginnungagap-template-gns3:latest"] = "sha256:NEW-IMAGE"
    assert world.update("resume") == 0
    assert world.docker.ids["ginnungagap-template-esxi:latest"] == old_id
    assert (
        f"docker tag {old_id} ginnungagap-template-esxi:latest"
        in world.docker.commands()
    )
    assert world.audit()[-1]["result"] == "rolled_back"


def test_a_rollback_that_ends_on_the_wrong_operator_version_is_not_reported_as_rolled_back(
    world, installed
):
    b = b_release(world, components={"operator": "1.3.0"})
    world.operator_reports = "1.3.0"
    world.apply.codes = [1, 0]  # unhealthy after the update, healthy after the rollback
    # ... but the Operator that answers after the rollback is still the new one (the described situation)
    assert world.update("apply", offline=b) == up.EXIT_ROLLBACK_FAILED
    text = world.text()
    assert "reports version 1.3.0, expected 1.2.1" in text
    assert "Rolled back:" not in text
    assert transaction(world)["phase"] == "rollback_failed"
    assert world.audit()[-1]["result"] == "rollback_failed"
    assert (
        world.installed()["appliance"]["version"] == "1.3.0"
    )  # the record never claims a rollback that did not happen
    # once the old Operator really answers, --resume finishes the rollback
    world.operator_reports = "1.2.1"
    world.out.clear()
    assert world.update("resume") == 0
    assert not world.layout.transaction.exists()
    assert world.audit()[-1]["result"] == "rolled_back"


def test_a_requested_rollback_to_the_wrong_operator_version_fails_and_says_so(
    world, installed
):
    b = b_release(world, components={"operator": "1.3.0"})
    world.operator_reports = "1.3.0"
    assert world.update("apply", offline=b) == 0
    # the Operator keeps reporting 1.3.0 after the rollback
    assert world.update("rollback") == up.EXIT_ROLLBACK_FAILED
    assert "did not bring the old Operator back" in world.text()
    assert world.installed()["appliance"]["version"] == "1.3.1"
    assert (
        world.audit()[-1]["action"] == "rollback"
        and world.audit()[-1]["result"] == "failed"
    )


# the process table of a machine where the engine died in the middle of `docker compose build`
BUNDLE = "/opt/ginnungagap/app"


def compose_args(bundle=BUNDLE, *rest):
    return [
        "docker-compose",
        "compose",
        "--project-directory",
        bundle,
        "--env-file",
        "x/compose.env",
        *rest,
    ]


def test_find_orphaned_builds_finds_the_build_and_everything_it_started(monkeypatch):
    me = os.getpid()
    table = {
        1: (0, ["systemd"]),
        me: (1, ["python", "-m", "ginnungagap", "update", "--resume"]),
        200: (
            1,
            compose_args(BUNDLE, "build", "ginnungagap"),
        ),  # the orphan: parent is init now
        201: (200, ["docker-buildx", "bake"]),
        202: (201, ["buildkit-helper"]),
        300: (
            1,
            compose_args("/some/other/appliance", "build", "x"),
        ),  # another project
        400: (1, compose_args(BUNDLE, "up", "-d", "ginnungagap")),  # not a build
        500: (1, ["sleep", "1000"]),
    }
    assert up.find_orphaned_builds(__import__("pathlib").Path(BUNDLE), table) == [
        200,
        201,
        202,
    ]
    # never the engine itself, nor anything above it, even if its own command line looks like a build
    table[me] = (1, compose_args(BUNDLE, "build", "x"))
    assert me not in up.find_orphaned_builds(__import__("pathlib").Path(BUNDLE), table)
    assert up.find_orphaned_builds(__import__("pathlib").Path(BUNDLE), {}) == []


def test_stop_orphaned_builds_asks_first_and_kills_what_does_not_stop(monkeypatch):
    from pathlib import Path

    table = {200: (1, compose_args(BUNDLE, "build", "x")), 201: (200, ["buildx"])}
    sent = []

    def kill(pid, sig):
        sent.append((pid, int(sig)))
        if int(sig) == 9:  # only a hard kill ends them
            table.pop(pid, None)

    monkeypatch.setattr(up.os, "kill", kill)
    clock = [0.0]

    def monotonic():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    stopped = up.stop_orphaned_builds(
        Path(BUNDLE),
        wait_seconds=2,
        sleep=sleep,
        monotonic=monotonic,
        processes=lambda: dict(table),
    )
    assert stopped == [200, 201]
    assert (200, 15) in sent and (201, 15) in sent  # SIGTERM first
    assert (200, 9) in sent and (201, 9) in sent
    assert table == {}


def test_stop_orphaned_builds_does_nothing_when_there_is_none(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(up.os, "kill", lambda *a: pytest.fail("killed something"))
    assert (
        up.stop_orphaned_builds(Path(BUNDLE), processes=lambda: {1: (0, ["init"])})
        == []
    )


def test_the_real_process_table_can_be_read():
    table = up._read_processes()
    assert os.getpid() in table and table[os.getpid()][1]


# ---- defect 2: the first update of an adopted appliance can be rolled back ----


def failing_first_update(world):
    """The pre-update appliance (adopted by the first apply), a release whose build fails after the tooling was replaced."""
    world.docker.fail = [(BUILD, 1)]
    return world.update("apply", offline=world.release("a", appliance="1.3.0"))


def test_a_failed_build_right_after_the_adoption_rolls_back_cleanly(world):
    (world.repos / "Ginnungagap-Appliance" / "ginnungagap").mkdir(exist_ok=True)
    (world.repos / "Ginnungagap-Appliance" / "ginnungagap" / "marker.txt").write_text(
        "adopted program\n"
    )
    assert failing_first_update(world) == up.EXIT_ROLLED_BACK
    assert (
        world.docker.unknown_options == []
    )  # the old install.sh never met --state-dir or --manifest
    bashes = [c for c in world.docker.commands() if c.startswith("bash ")]
    assert (
        len(bashes) == 1
    )  # only the new release's script ran; the old one is not run at all
    assert "--state-dir" in bashes[0] and "--manifest" in bashes[0]
    assert "predates the updater" in world.text()
    assert (
        world.bundle / "ginnungagap" / "marker.txt"
    ).read_text() == "adopted program\n"  # its program is back
    assert not world.layout.transaction.exists()
    assert "Rolled back" in world.text() and "The rollback failed" not in world.text()
    assert world.audit()[-1]["result"] == "rolled_back"
    # and the next update is not refused
    world.docker.fail = []
    assert world.update("apply", offline=world.release("a2", appliance="1.3.0")) == 0


def test_a_previous_install_script_that_fails_leaves_its_program_files_copied_back(
    world, installed
):
    """The install.sh of a normal earlier release failing in a rollback: the program files are copied back by hand."""
    b = b_release(world)
    world.docker.fail = [(BUILD, 1), ("1.3.0/install.sh", 1)]
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "install.sh failed; putting its program files back by hand" in world.text()
    assert (
        world.bundle / "ginnungagap" / "version.py"
    ).read_text() == '__version__ = "1.3.0"\n'
    assert not world.layout.transaction.exists()


def test_only_an_install_script_made_for_the_updater_is_run(world, tmp_path):
    old = tmp_path / "old.sh"
    old.write_text(
        "#!/bin/sh\n# options: --repos-dir --config-dir --prefix --no-start\n"
    )
    new = tmp_path / "new.sh"
    new.write_text("#!/bin/sh\n# options: --repos-dir --state-dir --manifest\n")
    assert up.Updater.knows_the_updater(old) is False
    assert up.Updater.knows_the_updater(new) is True
    assert up.Updater.knows_the_updater(tmp_path / "missing.sh") is False


@pytest.mark.parametrize("how", ["resume", "rollback"])
def test_a_rollback_that_failed_can_be_finished_later(world, how):
    """exit 3 and a journal in phase rollback_failed: once the cause is gone, --resume (or --rollback) finishes the job."""
    b = world.release("a", appliance="1.3.0", components={"template-services": "1.0.1"})
    world.docker.health["template-gns3"] = (
        "running unhealthy"  # the old one does not come back either
    )
    assert world.update("apply", offline=b) == up.EXIT_ROLLBACK_FAILED
    assert transaction(world)["phase"] == "rollback_failed"
    assert (
        world.update("apply", offline=b) == up.EXIT_REFUSED
    )  # nothing new is started on top of it
    assert "--resume" in world.text()
    world.docker.health["template-gns3"] = "running healthy"
    world.out.clear()
    assert world.update(how) == 0
    assert not world.layout.transaction.exists()
    assert "Rolled back" in world.text()
    assert world.audit()[-1]["result"] == "rolled_back"
    world.docker.health = {}
    assert (
        world.update("apply", offline=world.release("a2", appliance="1.3.0")) == 0
    )  # not locked out


# ---- defect 1 in the update flow: the Operator's state goes to the volume before the Operator is recreated ----


def test_the_update_carries_the_operators_state_over_before_it_restarts_the_operator(
    world, installed
):
    world.docker.layer = {"users.json", "session_secret.key", "config_snippets"}
    b = b_release(world, components={"operator": "1.3.0"})
    world.operator_reports = "1.3.0"
    assert world.update("apply", offline=b) == 0
    assert world.docker.volume == {
        "users.json",
        "session_secret.key",
        "config_snippets",
    }
    commands = world.docker.commands()
    migrated = next(i for i, c in enumerate(commands) if "/migrate:ro" in c)
    restarted = next(
        i
        for i, c in enumerate(commands)
        if " up -d --no-deps" in c and c.endswith("ginnungagap")
    )
    assert migrated < restarted
    assert "Carried the Operator's state over" in world.text()


def test_an_update_stops_when_the_state_cannot_be_carried_over_and_nothing_was_restarted(
    world, installed
):
    world.docker.layer = {"users.json"}
    world.docker.fail = [("docker cp", 1)]
    b = b_release(world, components={"operator": "1.3.0"})
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "could not be carried over" in world.text()
    assert world.docker.volume == set()
    assert not world.layout.transaction.exists()
    # the Operator was never restarted with the new image (nothing but the rollback's own steps touch it)
    restarts = [
        c
        for c in world.docker.commands()
        if " up -d --no-deps" in c and c.endswith("ginnungagap")
    ]
    assert restarts == []


# ---- small findings ----


def test_the_engine_prints_line_by_line_also_into_a_pipe():
    code = "from ginnungagap.__main__ import _line_buffered; import sys; _line_buffered(); print(sys.stdout.line_buffering)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "True"


def test_a_closed_pipe_ends_the_command_quietly(monkeypatch, capfd):
    from ginnungagap import __main__ as cli

    def broken(argv=None):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "main", broken)
    assert cli._run() == 141
    assert "Traceback" not in capfd.readouterr().err


def test_the_rollback_applies_through_the_restored_program_in_a_new_process(
    tmp_path, monkeypatch
):
    """`apply` after a tooling rollback is the installed command run as a new process, in the bundle folder, no build."""
    seen = {}

    def fake_run(command, check, cwd):
        seen.update(command=command, check=check, cwd=cwd)
        return subprocess.CompletedProcess(command, 7)

    wrapper = tmp_path / "ginnungagap"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setenv("GINNUNGAGAP_WRAPPER", str(wrapper))
    monkeypatch.setattr(up.subprocess, "run", fake_run)
    bundle = tmp_path / "app"
    bundle.mkdir()

    code = up._default_apply_with_installed_program(
        "/etc/ginnungagap/ginnungagap.yml", bundle, "/gen", wait_seconds=60, build=False
    )

    assert code == 7
    assert seen["command"] == [
        str(wrapper),
        "apply",
        "/etc/ginnungagap/ginnungagap.yml",
        "--no-build",
        "--generator-config",
        "/gen",
    ]
    assert seen["cwd"] == bundle and seen["check"] is False

    # without the wrapper the module is run with the same interpreter
    monkeypatch.setenv("GINNUNGAGAP_WRAPPER", str(tmp_path / "missing"))
    up._default_apply_with_installed_program("/c.yml", bundle, None)
    assert seen["command"][1:4] == ["-m", "ginnungagap", "apply"]
    assert "--generator-config" not in seen["command"]
