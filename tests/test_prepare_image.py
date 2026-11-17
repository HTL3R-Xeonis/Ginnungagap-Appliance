import subprocess
from pathlib import Path

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
                 "SSH host keys, the machine id", "authorized SSH keys, passwordless sudo", "clean logs"):  # fmt: skip
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
