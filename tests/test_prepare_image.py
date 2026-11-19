import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "image" / "prepare-image.sh"


def run(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_it_refuses_to_do_anything_without_yes():
    result = run()
    assert result.returncode == 1 and "--yes" in result.stderr


def test_the_dry_run_lists_every_step_and_changes_nothing():
    result = run("--dry-run")
    assert result.returncode == 0, result.stderr
    steps = result.stdout.splitlines()
    assert all(line.startswith("would: ") for line in steps)
    text = result.stdout
    for part in ("build and pull the images", "containers and volumes of the project 'ginnungagap'", "first-boot marker and the generated SSH password",
                 "SSH host keys, the machine id", "remove the login password hash, shadow backups",
                 "authorized SSH keys, passwordless sudo", "clean logs"):  # fmt: skip
        assert part in text
    assert "zeros" not in text  # only on request
    assert "stop the setup watcher" in text
    assert "remove staged and rejected setup files" in text
    assert "zeros" in run("--dry-run", "--zero-free-space").stdout


def test_the_project_name_can_be_chosen():
    assert "'other'" in run("--dry-run", "--project", "other").stdout


def test_an_unknown_option_is_refused():
    assert run("--frobnicate").returncode == 1


def test_the_generated_ssh_password_is_forgotten_but_the_sshd_drop_in_stays():
    script = SCRIPT.read_text()
    assert "rm -f /var/lib/ginnungagap/ssh-access.json" in script
    assert "sshd_config.d" not in script.replace(
        "the sshd drop-in that allows password login stays", ""
    )


def test_the_images_of_an_older_bundle_are_removed_after_the_containers_and_before_the_build_cache():
    text = run("--dry-run").stdout
    order = [
        text.index("remove the containers and volumes"),
        text.index("build and pull the images of every service"),
        text.index(
            "remove every image that is not part of the current compose project"
        ),
        text.index("remove the build cache and untagged image layers"),
    ]
    assert order == sorted(order)
    script = SCRIPT.read_text()
    assert "ginnungagap build-images --remove-other-images" in script


def test_root_is_locked_and_the_journal_of_the_build_is_deleted_before_the_free_space_is_zeroed():
    script = SCRIPT.read_text()
    assert "usermod --password '!' root" in script
    # journalctl never vacuums the directories of earlier machine ids and keeps active files: everything is deleted
    assert "rm -rf /var/log/journal/*" in script
    assert "systemctl stop systemd-journald.socket" in script
    text = run("--dry-run", "--zero-free-space").stdout
    assert text.index("delete the logs of every build boot") < text.index("zeros")


# ---- the update state of the image: the installed release stays, everything of this machine's update history goes


def test_the_dry_run_lists_the_release_and_update_cleanup_steps_before_the_build_cache():
    text = run("--dry-run").stdout
    assert "record the installed release" in text and "update access token" in text
    order = [
        text.index("build and pull the images of every service"),
        text.index("record the installed release"),
        text.index("remove the update access token"),
        text.index("remove the build cache"),
        text.index("remove the configuration, the rendered files"),
    ]
    assert order == sorted(
        order
    )  # the token's place is read from the configuration before that is deleted


@pytest.fixture
def machine(tmp_path):
    """The two update functions of the script, run against a throw-away /etc and /var/lib with a fake docker and command."""
    script = SCRIPT.read_text()
    start, end = (
        script.index("state_dir() {"),
        script.index("remove_build_leftovers() {"),
    )
    functions = script[start:end].replace("/etc/ginnungagap", str(tmp_path / "etc"))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "image ls" ]; then\n'
        "  printf 'ginnungagap-setup:latest\\nginnungagap-setup:rollback-1.2.0\\ncaddy:2\\n'\n"
        'else echo "$@" >>"$FAKE_LOG"; fi\n'
    )
    (fakebin / "ginnungagap").write_text(
        '#!/bin/sh\necho "ginnungagap $@" >>"$FAKE_LOG"\n'
    )
    for tool in (fakebin / "docker", fakebin / "ginnungagap"):
        tool.chmod(0o755)
    runner = tmp_path / "functions.sh"
    runner.write_text(
        'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n' + functions + '\n"$@"\n'
    )
    etc = tmp_path / "etc"
    etc.mkdir()
    state = tmp_path / "var"
    (state / "update").mkdir(parents=True)
    (etc / "install.env").write_text(f"GINNUNGAGAP_UPDATE_DIR={state}/update\n")
    log = tmp_path / "log"

    def call(function):
        return subprocess.run(
            ["bash", str(runner), function],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{fakebin}:{os.environ['PATH']}",
                "FAKE_LOG": str(log),
            },
        )

    return {"etc": etc, "state": state, "call": call, "log": log}


def _fill(machine):
    state, etc = machine["state"], machine["etc"]
    update = state / "update"
    (update / "installed.json").write_text(
        json.dumps(
            {"schema": 1, "previous": "1.2.0", "appliance": {"version": "1.3.0"}}
        )
    )
    (update / "history").mkdir()
    (update / "history" / "x.json").write_text("{}")
    (update / "downloads" / "1.3.0").mkdir(parents=True)
    for name in ("transaction.json", "audit.log", "available.json", "lock"):
        (update / name).write_text("x")
    (state / "backups" / "pre-update-1").mkdir(parents=True)
    (state / "backups" / "pre-update-1" / "ginnungagap.yml").write_text(
        "api_key: secret"
    )
    for version in ("1.0.0", "1.1.0"):
        (state / "releases" / "TopologyOperator" / version).mkdir(parents=True)
    (state / "components").mkdir()
    (state / "components" / "TopologyOperator").symlink_to(
        "../releases/TopologyOperator/1.1.0"
    )
    (etc / "update-token").write_text("ghp_default")
    (etc / "ginnungagap.yml").write_text(
        f"update:\n  token_file: {etc}/elsewhere-token\n"
    )
    (etc / "elsewhere-token").write_text("ghp_configured")


def test_the_image_keeps_its_installed_release_and_loses_everything_else_of_the_update_history(
    machine,
):
    _fill(machine)
    result = machine["call"]("remove_update_leftovers")
    assert result.returncode == 0, result.stderr
    state, etc = machine["state"], machine["etc"]
    update = state / "update"
    kept = json.loads((update / "installed.json").read_text())
    assert kept["appliance"]["version"] == "1.3.0" and kept["previous"] is None
    assert (update / "installed.json").stat().st_mode & 0o777 == 0o600
    for gone in (
        "history",
        "downloads",
        "transaction.json",
        "audit.log",
        "available.json",
        "lock",
    ):
        assert not (update / gone).exists(), gone
    assert list((state / "backups").iterdir()) == []
    assert (
        not (etc / "update-token").exists() and not (etc / "elsewhere-token").exists()
    )
    assert (state / "releases" / "TopologyOperator" / "1.1.0").is_dir()
    assert not (state / "releases" / "TopologyOperator" / "1.0.0").exists()
    assert (state / "components" / "TopologyOperator").is_symlink()
    assert "image rm ginnungagap-setup:rollback-1.2.0" in machine["log"].read_text()
    assert "ginnungagap-setup:latest" not in machine["log"].read_text()


def test_no_token_survives_even_without_any_update_state(machine):
    (machine["etc"] / "update-token").write_text("ghp_default")
    assert machine["call"]("remove_update_leftovers").returncode == 0
    assert not (machine["etc"] / "update-token").exists()


def test_a_machine_that_was_not_built_by_an_update_is_adopted_and_one_that_was_is_left_alone(
    machine,
):
    assert machine["call"]("record_release").returncode == 0
    assert machine["log"].read_text().strip() == "ginnungagap update --adopt --yes"
    machine["log"].unlink()
    (machine["state"] / "update" / "installed.json").write_text("{}")
    assert machine["call"]("record_release").returncode == 0
    assert not machine["log"].exists()


def test_the_script_never_touches_a_token_value_it_only_removes_files():
    script = SCRIPT.read_text()
    assert "update-token" in script and "--set-token" not in script
