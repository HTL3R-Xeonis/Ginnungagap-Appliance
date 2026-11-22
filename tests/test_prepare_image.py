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
    for part in (
        "build and pull the images",
        "containers and volumes of the project 'ginnungagap'",
        "first-boot marker and the generated SSH password",
        "SSH host keys, the machine id",
        "remove the login password hash, shadow backups",
        "authorized SSH keys, passwordless sudo",
        "clean logs",
    ):
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
    for name in (
        "transaction.json",
        "audit.log",
        "available.json",
        "web-status.json",
        "lock",
    ):
        (update / name).write_text("x")
    (state / "update-requests").mkdir()
    (state / "update-requests" / "request.json").write_text("{}")
    (state / "backups" / "pre-update-1").mkdir(parents=True)
    (state / "backups" / "pre-update-1" / "ginnungagap.yml").write_text(
        "api_key: secret"
    )
    for version in ("1.0.0", "1.1.0"):
        (state / "releases" / "Igdrasyl" / version).mkdir(parents=True)
    (state / "components").mkdir()
    (state / "components" / "Igdrasyl").symlink_to("../releases/Igdrasyl/1.1.0")
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
        "web-status.json",
        "lock",
    ):
        assert not (update / gone).exists(), gone
    assert list((state / "update-requests").iterdir()) == []
    assert list((state / "backups").iterdir()) == []
    assert (
        not (etc / "update-token").exists() and not (etc / "elsewhere-token").exists()
    )
    assert (state / "releases" / "Igdrasyl" / "1.1.0").is_dir()
    assert not (state / "releases" / "Igdrasyl" / "1.0.0").exists()
    assert (state / "components" / "Igdrasyl").is_symlink()
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


# ---- build leftovers of the image builder, and the host name of the base VM ----


def _functions(tmp_path, *names, home="", replace=None):
    """The named functions of the script, run against throw-away /root, /etc/hostname, /etc/hosts and a fake getent."""
    script = SCRIPT.read_text()
    body = ""
    for name in names:
        start = script.index(f"{name}() {{")
        body += script[start : script.index("\n}\n", start) + 3]
    for real, fake in (replace or {}).items():
        body = body.replace(real, str(fake))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    (fakebin / "getent").write_text(
        f'#!/bin/sh\n[ -n "{home}" ] && echo "login:x:1000:1000::{home}:/bin/bash"\nexit 0\n'
    )
    (fakebin / "hostname").write_text("#!/bin/sh\nexit 0\n")
    for tool in (fakebin / "getent", fakebin / "hostname"):
        tool.chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_text(
        "set -euo pipefail\nDEFAULT_HOSTNAME=ginnungagap\n" + body + '\n"$@"\n'
    )

    def call(function):
        return subprocess.run(
            ["bash", str(runner), function],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
        )

    return call


def test_the_image_builders_leftovers_in_root_and_the_login_home_go_and_nothing_else(
    tmp_path,
):
    root, home = tmp_path / "root", tmp_path / "home" / "junioradmin"
    (home / "Desktop").mkdir(parents=True)
    (root / ".ssh").mkdir(parents=True)
    (root / ".docker").mkdir()
    (home / ".docker").mkdir()
    gone = [
        root / ".docker" / "config.json",  # registry logins of the image builder
        home / ".docker" / "config.json",
        root / "prepare.log",
        root / "build.log",
        root / "old-repos-listing",
        root / "install.sh.bak",
        root / ".lesshst",
        root / ".python_history",
        home / "prepare.log",
        home / "old-repos-listing",
        home / ".lesshst",
        home / ".sudo_as_admin_successful",
    ]
    kept = [
        root
        / ".docker"
        / "daemon.json",  # only the login file, not the rest of the folder
        root / ".bashrc",
        root / "notes.txt",
        root / ".ssh" / "old.bak",  # only the top level of /root is cleaned
        home / "Desktop" / "debian-template.desktop",
        home
        / "Desktop"
        / "prepare.log",  # below the home's top level: not a known place
        home / ".bashrc",
        home / "thesis.log",  # real files of the account stay, whatever their name
        home / "important.bak",
    ]
    for path in (*gone, *kept):
        path.write_text("x")
    call = _functions(
        tmp_path,
        "remove_known_files",
        "remove_known_folders",
        "login_home",
        "remove_image_builder_files",
        home=home,
        replace={"/root": root},
    )
    result = call("remove_image_builder_files")
    assert result.returncode == 0, result.stderr
    assert [p for p in gone if p.exists()] == []
    assert [p for p in kept if not p.exists()] == []


def test_the_template_makers_personal_folders_go_from_the_login_home_and_the_rest_stays(
    tmp_path,
):
    root, home = tmp_path / "root", tmp_path / "home" / "junioradmin"
    root.mkdir()
    gone = [
        home / ".mozilla" / "firefox" / "profile" / "places.sqlite",
        home / ".cache" / "mozilla" / "x",
        home / ".local" / "share" / "keyrings" / "login.keyring",
        home / ".local" / "share" / "evolution" / "mail",
        home / ".config" / "evolution" / "sources",
        home / ".config" / "goa-1.0" / "accounts.conf",
    ]
    kept = [
        home / "Desktop" / "debian-template.desktop",
        home / ".config" / "gtk-3.0" / "settings.ini",
        home / ".local" / "share" / "applications" / "a.desktop",
        home / ".ssh" / "authorized_keys",
        home / ".bashrc",
    ]
    for path in (*gone, *kept):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    call = _functions(
        tmp_path,
        "remove_known_files",
        "remove_known_folders",
        "login_home",
        "remove_image_builder_files",
        home=home,
        replace={"/root": root},
    )
    result = call("remove_image_builder_files")
    assert result.returncode == 0, result.stderr
    assert [p for p in gone if p.exists()] == []
    assert [p for p in kept if not p.exists()] == []
    assert not (home / ".mozilla").exists() and not (home / ".cache").exists()


def test_a_link_in_the_way_stops_the_folder_removal(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home" / "junioradmin"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "keyrings").mkdir(parents=True)
    (outside / "keyrings" / "precious").write_text("x")
    (outside / "cache").mkdir()
    (outside / "cache" / "precious").write_text("x")
    (home / ".local").mkdir(parents=True)
    (home / ".local" / "share").symlink_to(
        outside
    )  # .local/share/keyrings goes through a link
    (home / ".cache").symlink_to(outside / "cache")  # the folder itself is a link
    call = _functions(
        tmp_path,
        "remove_known_files",
        "remove_known_folders",
        "login_home",
        "remove_image_builder_files",
        home=home,
        replace={"/root": root},
    )
    assert call("remove_image_builder_files").returncode == 0
    assert (outside / "keyrings" / "precious").exists()
    assert (outside / "cache" / "precious").exists()
    assert (home / ".cache").is_symlink()


def test_a_login_home_that_is_missing_root_or_a_link_is_left_alone(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "prepare.log").write_text("x")
    real = tmp_path / "real"
    real.mkdir()
    (real / "prepare.log").write_text("x")
    (tmp_path / "linked").symlink_to(real)
    for home in (
        "",
        "/",
        "relative/home",
        tmp_path / "does-not-exist",
        tmp_path / "linked",
    ):
        call = _functions(
            tmp_path,
            "remove_known_files",
            "remove_known_folders",
            "login_home",
            "remove_image_builder_files",
            home=home,
            replace={"/root": root},
        )
        assert call("remove_image_builder_files").returncode == 0
        assert (real / "prepare.log").exists(), home
    assert not (root / "prepare.log").exists()


def test_the_builders_leftovers_are_removed_last_before_the_zero_fill_and_listed_in_the_dry_run():
    text = run("--dry-run", "--zero-free-space").stdout
    order = [
        text.index("clean logs, package caches"),
        text.index("stop the journal and delete the logs"),
        text.index("remove the image builder's leftovers"),
        text.index("fill the free disk space with zeros"),
    ]
    assert order == sorted(order)
    script = SCRIPT.read_text()
    # the log of this run is written by the caller (tee /root/prepare.log): the cleanup is one step, the last before the zero-fill
    assert script.count("remove_image_builder_files\n") == 1


def test_the_host_name_of_the_base_vm_is_reset_to_a_neutral_default(tmp_path):
    hostname, hosts, info = (
        tmp_path / "hostname",
        tmp_path / "hosts",
        tmp_path / "machine-info",
    )
    hostname.write_text("ginn-img130-base\n")
    hosts.write_text(
        "127.0.0.1\tlocalhost\n127.0.1.1\tginn-img130-base\n::1\tlocalhost ip6-localhost\n"
    )
    info.write_text('PRETTY_HOSTNAME="Base VM"\nICON_NAME=computer-vm\n')
    call = _functions(
        tmp_path,
        "reset_hostname",
        replace={
            "/etc/hostname": hostname,
            "/etc/hosts": hosts,
            "/etc/machine-info": info,
        },
    )
    assert call("reset_hostname").returncode == 0
    assert hostname.read_text() == "ginnungagap\n"
    lines = hosts.read_text().splitlines()
    assert "127.0.1.1\tginnungagap" in lines and "127.0.0.1\tlocalhost" in lines
    assert "ginn-img130-base" not in hosts.read_text()
    assert info.read_text() == "ICON_NAME=computer-vm\n"


def test_the_host_name_reset_copes_with_missing_files_and_a_name_on_the_loopback_line(
    tmp_path,
):
    hostname, hosts = tmp_path / "hostname", tmp_path / "hosts"
    replace = {
        "/etc/hostname": hostname,
        "/etc/hosts": hosts,
        "/etc/machine-info": tmp_path / "none",
    }
    call = _functions(tmp_path, "reset_hostname", replace=replace)
    assert call("reset_hostname").returncode == 0  # nothing existed before
    assert hostname.read_text() == "ginnungagap\n" and not hosts.exists()
    hostname.write_text("base.vm\n")
    hosts.write_text("127.0.0.1 localhost base.vm\n")
    assert call("reset_hostname").returncode == 0
    assert hosts.read_text() == "127.0.0.1 localhost ginnungagap\n"


def test_the_host_name_is_reset_after_the_identity_and_the_dry_run_names_it():
    text = run("--dry-run").stdout
    assert "reset the host name of the base VM to 'ginnungagap'" in text
    assert text.index("remove the SSH host keys") < text.index("reset the host name")
