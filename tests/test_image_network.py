import json
import os
import subprocess

import pytest

from ginnungagap import image_network as net

CONFIG = {
    "address": "10.20.20.184",
    "prefix": 24,
    "gateway": "10.20.20.254",
    "dns": ["1.1.1.1", "8.8.8.8"],
}


@pytest.fixture
def guest(tmp_path):
    for relative in (
        "sys/class/net/ens192/device",
        "sys/class/net/lo",
        "run",
        "etc/NetworkManager/system-connections",
        "etc/systemd/network",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    for path in tmp_path.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    return tmp_path


def executor(calls, *, nm=True, fail_up=False):
    def run(args, **kwargs):
        calls.append(args)
        code = 0
        if args[-1] == "NetworkManager.service" and not nm:
            code = 3
        if fail_up and args[:3] == ["nmcli", "connection", "up"]:
            code = 1
        return subprocess.CompletedProcess(args, code, "", "")

    return run


@pytest.mark.parametrize("nm", [True, False])
def test_static_override_preserves_original_profile_and_restore_does_not_reconfigure(
    guest, nm
):
    calls = []
    original = guest / (
        "etc/NetworkManager/system-connections/dhcp.nmconnection"
        if nm
        else "etc/systemd/network/10-dhcp.network"
    )
    original.write_bytes(b"original DHCP profile\n")
    original.chmod(0o600)
    run = executor(calls, nm=nm)
    value = net.apply_network(
        CONFIG, root=guest, runner=run, which=lambda _: "/usr/bin/nmcli" if nm else None
    )
    assert value["address"] == CONFIG["address"] and value["interface"] == "ens192"
    override = guest / (net.NM_FILE if nm else net.NETWORKD_FILE)
    assert override.is_file()
    assert "10.20.20.184/24" in override.read_text()
    if nm:
        assert calls[0] == ["systemctl", "start", "NetworkManager.service"]
        assert "addr-gen-mode=0" in override.read_text()
    backup = guest / net.BACKUP
    assert backup.stat().st_mode & 0o777 == 0o600
    first_backup = backup.read_bytes()
    count = len(calls)
    assert net.apply_network(CONFIG, root=guest, runner=run)["changed"] is False
    assert backup.read_bytes() == first_backup and len(calls) == count
    assert net.restore_network(root=guest)
    assert not override.exists() and not backup.exists()
    assert original.read_bytes() == b"original DHCP profile\n"
    assert original.stat().st_mode & 0o777 == 0o600
    assert len(calls) == count
    assert not net.restore_network(root=guest)


def test_command_failure_removes_only_our_override_and_keeps_original(guest):
    original = guest / "etc/NetworkManager/system-connections/dhcp.nmconnection"
    original.write_text("DHCP remains")
    with pytest.raises(RuntimeError):
        net.apply_network(
            CONFIG,
            root=guest,
            runner=executor([], fail_up=True),
            which=lambda _: "nmcli",
        )
    assert original.read_text() == "DHCP remains"
    assert not (guest / net.NM_FILE).exists() and not (guest / net.BACKUP).exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-interface",
        "existing-override",
        "changed-override",
        "linked-parent",
        "unsafe-backup",
        "invalid-subnet",
    ],
)
def test_network_refuses_ambiguous_or_unsafe_changes(guest, mutation):
    if mutation == "linked-parent" and os.name != "posix":
        pytest.skip("requires symlinks")
    calls = []
    run = executor(calls)
    config = dict(CONFIG)
    if mutation == "extra-interface":
        (guest / "sys/class/net/ens224/device").mkdir(parents=True)
    elif mutation == "existing-override":
        (guest / net.NM_FILE).write_text("someone else's file")
    elif mutation == "linked-parent":
        parent = guest / "etc/NetworkManager/system-connections"
        parent.rmdir()
        parent.symlink_to(guest / "etc/systemd/network", target_is_directory=True)
    elif mutation == "invalid-subnet":
        config["gateway"] = "192.0.2.1"
    else:
        net.apply_network(config, root=guest, runner=run, which=lambda _: "nmcli")
        if mutation == "changed-override":
            (guest / net.NM_FILE).write_text("external change")
            with pytest.raises(ValueError):
                net.restore_network(root=guest)
        else:
            (guest / net.BACKUP).chmod(0o644)
    with pytest.raises(ValueError):
        net.apply_network(config, root=guest, runner=run, which=lambda _: "nmcli")


def test_standalone_scripts_compile_and_bootstrap_accepts_public_json():
    for script in (net.bootstrap_script(), net.restore_script()):
        compile(script, "standalone-network", "exec")
    assert "sys.argv[1]" in net.bootstrap_script()
    assert "sys.stdin.read()" in net.bootstrap_script()
    assert net.validate_network(json.loads(json.dumps(CONFIG))) == CONFIG


@pytest.mark.parametrize(
    "changes",
    [
        {"prefix": True},
        {"address": 1},
        {"prefix": 0},
        {"address": "10.20.20.0"},
        {"address": "10.20.20.254"},
        {"dns": []},
        {"dns": ["not-ip"]},
        {"gateway": "127.0.0.1"},
    ],
)
def test_invalid_network_is_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        net.validate_network({**CONFIG, **changes})
