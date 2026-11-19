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
