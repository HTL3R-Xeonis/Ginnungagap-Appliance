import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def place(tmp_path):
    repos = tmp_path / "repos"
    for name in (
        "Igdrasyl",
        "TopologyBuilder",
        "Hugyn-and-Munyn",
        "Mimyr",
    ):
        (repos / name).mkdir(parents=True)
    return {
        "repos": repos,
        "prefix": tmp_path / "opt",
        "config": tmp_path / "etc",
        "bin": tmp_path / "bin",
        "units": tmp_path / "units",
        "staging": tmp_path / "staging",
        "sshd": tmp_path / "sshd_config.d",
        "state": tmp_path / "state",
    }


def install(place, *extra, check=True, path=None, script=None):
    command = [
        "bash",
        str(script or ROOT / "install.sh"),
        "--prefix",
        str(place["prefix"]),
        "--config-dir",
        str(place["config"]),
        "--bin-dir",
        str(place["bin"]),
        "--unit-dir",
        str(place["units"]),
        "--staging-dir",
        str(place["staging"]),
        "--sshd-dropin-dir",
        str(place["sshd"]),
        "--state-dir",
        str(place["state"]),
        "--repos-dir",
        str(place["repos"]),
        "--no-start",
        *extra,
    ]
    env = {**os.environ, **({"PATH": path} if path else {})}
    return subprocess.run(command, capture_output=True, text=True, check=False, env=env)


def test_installs_program_command_config_and_unit(place):
    result = install(place)
    assert result.returncode == 0, result.stderr
    app = place["prefix"] / "app"
    for member in (
        "ginnungagap/__main__.py",
        "compose.yml",
        "compose.legacy-ports.yml",
        "compose.template-share.yml",
        "docker/operator.Dockerfile",
        "ginnungagap.example.yml",
    ):
        assert (app / member).is_file(), member
    assert not list(app.rglob("__pycache__"))
    config = place["config"] / "ginnungagap.yml"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert config.read_text() == (ROOT / "ginnungagap.example.yml").read_text()
    env = (place["config"] / "install.env").read_text()
    assert f"PYTHONPATH={app}" in env and f"REPOS_DIR={place['repos']}" in env
    assert f"GINNUNGAGAP_GENERATOR_CONFIG={place['repos']}/Mimyr/config" in env
    unit = (place["units"] / "ginnungagap.service").read_text()
    assert (
        f"ExecStart={place['bin']}/ginnungagap apply {place['config']}/ginnungagap.yml --no-build"
        in unit
    )
    assert "Requires=docker.service" in unit and "WantedBy=multi-user.target" in unit
    assert "edit it" in result.stdout.lower()
    assert (
        "systemctl enable ginnungagap.service" in result.stdout
    )  # not enabled before the example has been edited


def test_the_installed_command_runs_the_installed_program(place, tmp_path):
    install(place)
    config = place["config"] / "ginnungagap.yml"
    elsewhere = (
        tmp_path / "elsewhere"
    )  # not the checkout: python -m would otherwise import the checkout from the current folder
    elsewhere.mkdir()
    result = subprocess.run(
        [str(place["bin"] / "ginnungagap"), "validate", str(config)],
        capture_output=True,
        text=True,
        check=False,
        cwd=elsewhere,
    )
    assert result.returncode == 0, result.stderr
    assert "is valid" in result.stdout
    shutil.rmtree(place["prefix"] / "app" / "ginnungagap")
    broken = subprocess.run(
        [str(place["bin"] / "ginnungagap"), "validate", str(config)],
        capture_output=True,
        text=True,
        check=False,
        cwd=elsewhere,
    )
    assert broken.returncode != 0  # so it really was the installed copy that ran


def test_a_second_run_updates_the_code_but_keeps_the_edited_config_and_rendered_files(
    place,
):
    install(place)
    config = place["config"] / "ginnungagap.yml"
    config.write_text("# my edits\n" + config.read_text())
    rendered = place["prefix"] / "app" / "rendered"
    rendered.mkdir()
    (rendered / "compose.env").write_text("keep me\n")
    (place["prefix"] / "app" / "ginnungagap" / "stale.py").write_text("old\n")
    result = install(place)
    assert result.returncode == 0, result.stderr
    assert config.read_text().startswith("# my edits")
    assert (rendered / "compose.env").read_text() == "keep me\n"
    assert not (place["prefix"] / "app" / "ginnungagap" / "stale.py").exists()
    assert "edit it" not in result.stdout.lower()


def test_uninstall_removes_program_command_and_unit_but_keeps_the_config(place):
    install(place)
    result = install(place, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (place["prefix"] / "app").exists()
    assert not place["prefix"].exists()  # the empty folder is removed with it
    assert not (place["bin"] / "ginnungagap").exists()
    assert not (place["units"] / "ginnungagap.service").exists()
    assert (place["config"] / "ginnungagap.yml").is_file()


def test_no_systemd_writes_no_unit(place):
    assert install(place, "--no-systemd").returncode == 0
    assert not (place["units"] / "ginnungagap.service").exists()


def test_a_missing_repository_stops_before_anything_is_written(place):
    shutil.rmtree(place["repos"] / "Mimyr")
    result = install(place)
    assert result.returncode == 1 and "Mimyr" in result.stderr
    assert not place["prefix"].exists() and not place["config"].exists()


OLD_NAMES = (
    "TopologyOperator",
    "Mjonyr",
    "Docker-Server-Files",
    "topology-generator",
)


def use_old_names(place, only=None):
    for new, old in zip(
        ("Igdrasyl", "TopologyBuilder", "Hugyn-and-Munyn", "Mimyr"),
        OLD_NAMES,
        strict=True,
    ):
        if only is None or new in only:
            (place["repos"] / new).rename(place["repos"] / old)


def test_checkouts_under_the_old_names_install_and_point_to_adopt(place):
    use_old_names(place)
    result = install(place)
    assert result.returncode == 0, result.stderr
    assert "update --adopt" in result.stdout and "Igdrasyl" in result.stdout
    env = (place["config"] / "install.env").read_text()
    assert f"REPOS_DIR={place['repos']}" in env
    # the generator's config is looked for where the generator really is
    assert (
        f"GINNUNGAGAP_GENERATOR_CONFIG={place['repos']}/topology-generator/config"
        in env
    )


def test_checkouts_under_the_old_names_are_not_started_by_install(place, tmp_path):
    use_old_names(place)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\nexit 0\n")  # the checks for Docker pass
    (fake / "docker").chmod(0o755)
    command = [
        "bash",
        str(ROOT / "install.sh"),
        "--prefix",
        str(place["prefix"]),
        "--config-dir",
        str(place["config"]),
        "--bin-dir",
        str(place["bin"]),
        "--unit-dir",
        str(place["units"]),
        "--staging-dir",
        str(place["staging"]),
        "--sshd-dropin-dir",
        str(place["sshd"]),
        "--state-dir",
        str(place["state"]),
        "--repos-dir",
        str(place["repos"]),
    ]
    place["config"].mkdir(parents=True)
    # an existing (here: invalid) configuration would make the final `apply` fail, so a clean exit shows it was not run
    (place["config"] / "ginnungagap.yml").write_text("version: 1\n")
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "update --adopt" in result.stdout


def test_a_mixed_folder_installs_too_and_the_current_name_wins(place):
    use_old_names(place, only={"Mimyr"})
    result = install(place)
    assert result.returncode == 0, result.stderr
    assert "update --adopt" in result.stdout
    (
        place["repos"] / "Mimyr"
    ).mkdir()  # both names present: the current one is used, nothing to adopt
    again = install(place)
    assert again.returncode == 0 and "update --adopt" not in again.stdout
    assert (
        f"GINNUNGAGAP_GENERATOR_CONFIG={place['repos']}/Mimyr/config"
        in (place["config"] / "install.env").read_text()
    )


def test_the_current_names_install_without_a_word_about_old_ones(place):
    result = install(place)
    assert result.returncode == 0 and "adopt" not in result.stdout


def test_without_no_start_docker_is_required(place, tmp_path):
    only_basics = tmp_path / "fakebin"
    only_basics.mkdir()
    for tool in ("bash", "python3", "dirname", "cd"):
        found = shutil.which(tool)
        if found:
            (only_basics / tool).symlink_to(found)
    command = [
        "bash",
        str(ROOT / "install.sh"),
        "--prefix",
        str(place["prefix"]),
        "--config-dir",
        str(place["config"]),
        "--bin-dir",
        str(place["bin"]),
        "--unit-dir",
        str(place["units"]),
        "--repos-dir",
        str(place["repos"]),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(only_basics), "HOME": str(tmp_path)},
    )
    assert result.returncode == 1 and "docker is required" in result.stderr
    assert not place["prefix"].exists()


def test_an_unknown_option_is_refused(place):
    result = install(place, "--frobnicate")
    assert result.returncode == 1 and "unknown option" in result.stderr


def test_python_is_new_enough_for_the_checks():
    assert sys.version_info >= (3, 12)


def test_the_first_boot_unit_is_written_but_the_service_waits_for_a_configuration(
    place,
):
    assert install(place).returncode == 0
    first = (place["units"] / "ginnungagap-firstboot.service").read_text()
    assert f"ExecStart={place['bin']}/ginnungagap firstboot" in first
    assert "Before=ssh.service ginnungagap.service" in first
    assert "ConditionPathExists=!/var/lib/ginnungagap/firstboot.done" in first
    assert "SuccessExitStatus=1" in first  # no configuration yet is a normal boot
    service = (place["units"] / "ginnungagap.service").read_text()
    assert "ginnungagap-firstboot.service" in service
    assert f"ConditionPathExists={place['config']}/ginnungagap.yml" in service
    env = (place["config"] / "install.env").read_text()
    assert f"GINNUNGAGAP_CONFIG_DIR={place['config']}" in env
    assert (place["config"] / "ginnungagap.example.yml").is_file()


def test_image_mode_creates_no_configuration_and_does_not_start_anything(place):
    result = install(place, "--image")
    assert result.returncode == 0, result.stderr
    assert not (
        place["config"] / "ginnungagap.yml"
    ).exists()  # the first boot brings it
    assert (place["config"] / "ginnungagap.example.yml").is_file()
    assert "appliance image" in result.stdout
    boot = (place["units"] / "ginnungagap-bootstrap.service").read_text()
    assert "ExecStartPost=-/sbin/agetty --reload" in boot
    wizard = place["units"] / "ginnungagap-setup-apply.service"
    apply_line = next(
        line
        for line in wizard.read_text().splitlines()
        if line.startswith("ExecStart=")
    )
    assert apply_line.endswith(" --no-build")
    assert install(place).returncode == 0
    assert apply_line in wizard.read_text()


def test_uninstall_removes_both_units(place):
    install(place)
    install(place, "--uninstall")
    assert not (place["units"] / "ginnungagap-firstboot.service").exists()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="as root it would really install packages"
)
def test_install_packages_needs_root(place):
    result = install(place, "--install-packages")
    assert result.returncode == 1 and "needs root" in result.stderr
    assert not place["prefix"].exists()


def test_the_final_apply_is_not_run_from_the_folder_the_installer_was_started_in():
    """From inside a checkout, `python3 -m ginnungagap` imports the checkout (the current folder comes first) and renders into it."""
    script = (ROOT / "install.sh").read_text()
    assert '(cd / && "$WRAPPER" apply' in script
    assert '\n  "$WRAPPER" apply' not in script


def test_every_install_gets_the_console_text_unit_that_runs_after_the_network(place):
    assert install(place).returncode == 0
    unit = (place["units"] / "ginnungagap-issue.service").read_text()
    assert f"ExecStart={place['bin']}/ginnungagap issue" in unit
    assert "After=network-online.target ginnungagap-firstboot.service" in unit
    assert "Type=oneshot" in unit and "WantedBy=multi-user.target" in unit


def test_a_changed_password_rewrites_the_console_text_through_a_path_unit(place):
    assert install(place).returncode == 0
    unit = (place["units"] / "ginnungagap-issue.path").read_text()
    assert "PathChanged=/etc/shadow" in unit
    assert "Unit=ginnungagap-issue-changed.service" in unit
    assert "WantedBy=multi-user.target" in unit
    changed = (place["units"] / "ginnungagap-issue-changed.service").read_text()
    assert f"ExecStart={place['bin']}/ginnungagap issue --clear-screens" in changed


def test_an_image_boots_to_the_text_console_where_the_login_text_is_shown():
    script = (ROOT / "install.sh").read_text()
    image_branch = script[
        script.index('if [ "$IMAGE" = 1 ]; then\n      # an appliance') :
    ]
    assert "systemctl set-default multi-user.target" in image_branch[:400]


def test_an_image_switches_ssh_password_login_on_through_a_drop_in(place):
    assert install(place, "--image").returncode == 0
    dropin = place["sshd"] / "10-ginnungagap.conf"
    assert "PasswordAuthentication yes" in dropin.read_text()
    assert "PermitRootLogin no" in dropin.read_text()
    assert dropin.stat().st_mode & 0o777 == 0o644


def test_a_plain_install_leaves_sshd_alone(place):
    assert install(place).returncode == 0
    assert not place["sshd"].exists()


def test_uninstall_removes_the_console_unit_and_the_drop_in(place):
    install(place, "--image")
    install(place, "--uninstall")
    assert not (place["units"] / "ginnungagap-issue.service").exists()
    assert not (place["units"] / "ginnungagap-issue.path").exists()
    assert not (place["units"] / "ginnungagap-issue-changed.service").exists()
    assert not (place["sshd"] / "10-ginnungagap.conf").exists()


def test_the_script_keeps_the_install_of_the_drop_in_to_image_mode():
    script = (ROOT / "install.sh").read_text()
    assert (
        'if [ "$IMAGE" = 1 ]; then\n  install -d -m 0755 "$SSHD_DROPIN_DIR"' in script
    )


# ---- `ginnungagap update`: state folders, units, trust key, release version


@pytest.fixture
def checkout(tmp_path):
    """A copy of this checkout that a test may change (a version file, a signers file) without touching the real one."""
    copy = tmp_path / "checkout"
    copy.mkdir()
    shutil.copy(ROOT / "install.sh", copy / "install.sh")
    for name in (
        "compose.yml",
        "compose.legacy-ports.yml",
        "compose.template-share.yml",
        "ginnungagap.example.yml",
    ):
        shutil.copy(ROOT / name, copy / name)
    for folder in ("ginnungagap", "docker"):
        shutil.copytree(
            ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__")
        )
    return copy


def test_the_update_state_folders_are_made_private_where_they_hold_state(place):
    assert install(place).returncode == 0
    state = place["state"]
    assert stat.S_IMODE((state / "update").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "backups").stat().st_mode) == 0o700
    for name in ("releases", "components"):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o755
    env = (place["config"] / "install.env").read_text()
    assert f"GINNUNGAGAP_UPDATE_DIR={state}/update" in env
    assert (place["staging"]).is_dir()  # the wizard's staging folder is unchanged


def test_the_default_state_folder_keeps_the_staging_folder_where_it_always_was():
    script = (ROOT / "install.sh").read_text()
    assert "STATE_DIR=/var/lib/ginnungagap\n" in script
    assert "STAGING_DIR=${STAGING_DIR:-$STATE_DIR/setup}" in script


def test_no_token_file_is_created_and_the_state_never_holds_one(place):
    assert install(place).returncode == 0
    assert not (place["config"] / "update-token").exists()
    written = [
        p
        for p in [*place["state"].rglob("*"), *place["config"].rglob("*")]
        if p.is_file()
    ]
    assert not [p for p in written if "token" in p.name]


def test_a_second_run_keeps_the_update_state(place):
    install(place)
    (place["state"] / "update" / "installed.json").write_text("{}\n")
    (place["state"] / "releases" / "keep").mkdir()
    assert install(place).returncode == 0
    assert (place["state"] / "update" / "installed.json").read_text() == "{}\n"
    assert (place["state"] / "releases" / "keep").is_dir()


def test_the_check_timer_the_check_service_and_the_resume_unit_are_written(place):
    assert install(place).returncode == 0
    service = (place["units"] / "ginnungagap-update-check.service").read_text()
    assert f"ExecStart={place['bin']}/ginnungagap update --check" in service
    assert (
        "SuccessExitStatus=10" in service
    )  # "an update is available" is not a failure
    assert "ExecCondition=" in service and "update-check-daily" in service
    assert f"{place['config']}/ginnungagap.yml" in service
    assert "--apply" not in service  # it only looks
    timer = (place["units"] / "ginnungagap-update-check.timer").read_text()
    assert "OnCalendar=daily" in timer and "WantedBy=timers.target" in timer
    assert "Unit=ginnungagap-update-check.service" in timer
    resume = (place["units"] / "ginnungagap-update-resume.service").read_text()
    assert f"ConditionPathExists={place['state']}/update/transaction.json" in resume
    assert f"ExecStart={place['bin']}/ginnungagap update --resume" in resume
    assert "Before=ginnungagap.service" in resume


def test_the_check_condition_runs_for_real_and_follows_the_configuration(place):
    assert install(place).returncode == 0
    service = (place["units"] / "ginnungagap-update-check.service").read_text()
    condition = next(
        line for line in service.splitlines() if line.startswith("ExecCondition=")
    ).removeprefix("ExecCondition=")
    config = place["config"] / "ginnungagap.yml"
    base = "generator:\n  llm_servers:\n    - {name: o1, url: 'http://ollama:11434', model: m}\n"

    def run():
        return subprocess.run(
            ["sh", "-c", condition.removeprefix("/bin/sh -c ").strip("'")],
            capture_output=True,
            text=True,
            check=False,
        )

    config.write_text(base)
    assert run().returncode == 1  # off by default
    config.write_text(base + "update: {check: daily}\n")
    assert run().returncode == 0, run().stderr


def test_the_new_units_are_removed_by_uninstall_but_the_state_stays(place):
    install(place)
    install(place, "--uninstall")
    for name in ("update-check.service", "update-check.timer", "update-resume.service"):
        assert not (place["units"] / f"ginnungagap-{name}").exists()
    assert (place["state"] / "update").is_dir() and (
        place["config"] / "ginnungagap.yml"
    ).is_file()


def test_without_repos_dir_a_rerun_keeps_the_recorded_one_and_a_new_install_uses_the_components_folder(
    place, tmp_path
):
    install(place)
    # re-run without --repos-dir: the recorded folder stays (the script's own parent folder has no repositories)
    command = [
        "bash",
        str(ROOT / "install.sh"),
        "--prefix",
        str(place["prefix"]),
        "--config-dir",
        str(place["config"]),
        "--bin-dir",
        str(place["bin"]),
        "--unit-dir",
        str(place["units"]),
        "--staging-dir",
        str(place["staging"]),
        "--state-dir",
        str(place["state"]),
        "--no-start",
    ]
    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode == 0, again.stderr
    assert (
        f"REPOS_DIR={place['repos']}" in (place["config"] / "install.env").read_text()
    )
    # a new install: the folder `ginnungagap update` fills (symlinks to the unpacked releases) is used when there is one
    fresh = {
        **place,
        "config": tmp_path / "etc2",
        "prefix": tmp_path / "opt2",
        "state": tmp_path / "state2",
    }
    for repo in (
        "Igdrasyl",
        "TopologyBuilder",
        "Hugyn-and-Munyn",
        "Mimyr",
    ):
        target = tmp_path / "releases" / repo
        target.mkdir(parents=True)
        (fresh["state"] / "components").mkdir(parents=True, exist_ok=True)
        (fresh["state"] / "components" / repo).symlink_to(target)
    command2 = [
        c if c != str(place["config"]) else str(fresh["config"]) for c in command
    ]
    command2 = [
        str(fresh["prefix"])
        if c == str(place["prefix"])
        else str(fresh["state"])
        if c == str(place["state"])
        else c
        for c in command2
    ]
    result = subprocess.run(command2, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert (
        f"REPOS_DIR={fresh['state']}/components"
        in (fresh["config"] / "install.env").read_text()
    )


def test_the_installed_program_knows_its_release_version(place, checkout):
    (checkout / "ginnungagap" / "version.py").write_text(
        '"""x"""\n\n__version__ = "1.3.0"\n'
    )
    assert install(place, script=checkout / "install.sh").returncode == 0
    assert (place["prefix"] / "app" / "VERSION").read_text() == "1.3.0\n"
    (checkout / "ginnungagap" / "version.py").write_text('__version__ = "1.3.1"\n')
    assert install(place, script=checkout / "install.sh").returncode == 0
    assert (place["prefix"] / "app" / "VERSION").read_text() == "1.3.1\n"


def test_the_manifest_of_the_release_is_kept_next_to_the_program_and_a_plain_run_drops_a_stale_one(
    place, tmp_path
):
    manifest = tmp_path / "components.json"
    manifest.write_text('{"schema": 1}\n')
    assert install(place, "--manifest", str(manifest)).returncode == 0
    assert (
        place["prefix"] / "app" / "components.json"
    ).read_text() == '{"schema": 1}\n'
    assert install(place).returncode == 0
    assert not (place["prefix"] / "app" / "components.json").exists()
    missing = install(place, "--manifest", str(tmp_path / "nope.json"))
    assert missing.returncode == 1 and "--manifest" in missing.stderr


def test_a_signers_file_with_a_key_is_installed_and_one_without_is_not(place, checkout):
    (checkout / "release").mkdir()
    signers = checkout / "release" / "allowed_signers"
    signers.write_text("# no key yet\n\n")
    assert install(place, script=checkout / "install.sh").returncode == 0
    trust = place["config"] / "update-trust"
    assert (
        trust.is_dir() and not (trust / "allowed_signers").exists()
    )  # `auto` would otherwise demand a signature nobody can make
    signers.write_text(
        '# the key\nginnungagap-release namespaces="ginnungagap-release" ssh-ed25519 AAAAC3Nza\n'
    )
    assert install(place, script=checkout / "install.sh").returncode == 0
    assert "ssh-ed25519" in (trust / "allowed_signers").read_text()
    signers.write_text("# rotated away\n")
    assert install(place, script=checkout / "install.sh").returncode == 0
    assert (
        "ssh-ed25519" in (trust / "allowed_signers").read_text()
    )  # a file without a key never removes the installed one


def test_the_web_panels_request_folder_is_private_and_named_in_install_env(place):
    assert install(place).returncode == 0
    folder = place["state"] / "update-requests"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    env = (place["config"] / "install.env").read_text()
    assert f"GINNUNGAGAP_UPDATE_REQUEST_DIR={folder}" in env
    assert list(folder.iterdir()) == []  # install never leaves a request behind


def test_a_request_from_the_web_panel_is_picked_up_by_a_path_unit_that_only_looks_or_installs_what_was_confirmed(
    place,
):
    assert install(place).returncode == 0
    path_unit = (place["units"] / "ginnungagap-update-request.path").read_text()
    assert f"PathExists={place['state']}/update-requests/request.json" in path_unit
    assert "WantedBy=multi-user.target" in path_unit
    service = (place["units"] / "ginnungagap-update-request.service").read_text()
    command = next(l for l in service.splitlines() if l.startswith("ExecStart="))
    assert command == (
        f"ExecStart={place['bin']}/ginnungagap apply-update-request "
        f"--requests-dir {place['state']}/update-requests --config-dir {place['config']}"
    )
    assert (
        "--apply" not in service and "--yes" not in service
    )  # what to do is in the request, validated by the program
    assert (
        "SuccessExitStatus=1" in service
    )  # a refused request is shown in the panel, not a failed unit
    assert "Type=oneshot" in service


def test_the_request_units_are_removed_by_uninstall_and_the_folder_stays(place):
    install(place)
    install(place, "--uninstall")
    for name in ("update-request.path", "update-request.service"):
        assert not (place["units"] / f"ginnungagap-{name}").exists()
    assert (place["state"] / "update-requests").is_dir()


def test_the_path_unit_is_enabled_and_started_without_a_reboot():
    script = (ROOT / "install.sh").read_text()
    assert (
        "ginnungagap-update-resume.service ginnungagap-update-request.path >/dev/null"
        in script
    )
    assert "systemctl start ginnungagap-update-request.path" in script
