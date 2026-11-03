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
        "TopologyOperator",
        "TopologyBuilder",
        "Docker-Server-Files",
        "topology-generator",
    ):
        (repos / name).mkdir(parents=True)
    return {
        "repos": repos,
        "prefix": tmp_path / "opt",
        "config": tmp_path / "etc",
        "bin": tmp_path / "bin",
        "units": tmp_path / "units",
    }


def install(place, *extra, check=True, path=None):
    command = [
        "bash",
        str(ROOT / "install.sh"),
        "--prefix", str(place["prefix"]),
        "--config-dir", str(place["config"]),
        "--bin-dir", str(place["bin"]),
        "--unit-dir", str(place["units"]),
        "--repos-dir", str(place["repos"]),
        "--no-start",
        *extra,
    ]  # fmt: skip
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
    assert (
        f"GINNUNGAGAP_GENERATOR_CONFIG={place['repos']}/topology-generator/config"
        in env
    )
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
    shutil.rmtree(place["repos"] / "topology-generator")
    result = install(place)
    assert result.returncode == 1 and "topology-generator" in result.stderr
    assert not place["prefix"].exists() and not place["config"].exists()


def test_without_no_start_docker_is_required(place, tmp_path):
    only_basics = tmp_path / "fakebin"
    only_basics.mkdir()
    for tool in ("bash", "python3", "dirname", "cd"):
        found = shutil.which(tool)
        if found:
            (only_basics / tool).symlink_to(found)
    command = ["bash", str(ROOT / "install.sh"), "--prefix", str(place["prefix"]), "--config-dir", str(place["config"]), "--bin-dir", str(place["bin"]), "--unit-dir", str(place["units"]), "--repos-dir", str(place["repos"])]  # fmt: skip
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
