"""Real adapter safety and SSH transport behavior without touching lab hosts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from image.build import esxi, names, ports, remote
from tests.helpers.fake_esxi import FakeClock

RUN = "1.3.0-20261120T120000Z"
VM = names.build_vm_name(RUN)


def connection(vm=None):
    obj = Mock()
    obj.get_all_vms.return_value = [] if vm is None else [vm]
    return obj


def virtual_machine(name=VM, power="poweredOff", annotation=""):
    task = NS(info=NS(state="success", result=None))
    vm = Mock()
    vm.name = name
    vm.runtime.powerState = power
    vm.config = NS(
        annotation=annotation,
        createDate=None,
        files=NS(vmPathName=f"[ds] {name}/{name}.vmx"),
        extraConfig=[],
    )
    vm.guest = NS(ipAddress="192.0.2.3", toolsRunningStatus="guestToolsRunning")
    vm.PowerOnVM_Task.return_value = task
    vm.PowerOffVM_Task.return_value = task
    vm.Destroy_Task.return_value = task
    vm.ReconfigVM_Task.return_value = task
    return vm


def test_adapter_is_lazy_and_exact_even_with_a_numbered_alias():
    conn = connection(virtual_machine(VM + " (2)"))
    factory = Mock(return_value=conn)
    adapter = esxi.EsxiAdapter(
        "192.0.2.1", "root", "secret", connection_factory=factory
    )
    factory.assert_not_called()
    assert adapter.host == "192.0.2.1"
    assert adapter.get_vm(VM) is None
    factory.assert_called_once_with("192.0.2.1", 443, "root", "secret")


def test_adapter_rejects_foreign_vm_before_power_or_delete():
    vm = virtual_machine("important-router", power="poweredOn")
    adapter = esxi.EsxiAdapter(
        "192.0.2.1", "root", "secret", connection_factory=lambda *args: connection(vm)
    )
    for operation in (
        adapter.power_on,
        adapter.power_off,
        adapter.delete_vm,
        adapter.shutdown_guest,
    ):
        with pytest.raises(ports.NotOurs):
            operation(vm.name)
    vm.PowerOffVM_Task.assert_not_called()
    vm.Destroy_Task.assert_not_called()


def test_adapter_refuses_namespace_vm_annotated_by_another_tool():
    vm = virtual_machine(annotation="someone else")
    adapter = esxi.EsxiAdapter(
        "192.0.2.1", "root", "secret", connection_factory=lambda *args: connection(vm)
    )
    with pytest.raises(ports.NotOurs):
        adapter.delete_vm(VM)


def test_adapter_import_refuses_collision_and_passes_timeout_to_importer():
    vm = virtual_machine()
    conn = connection(vm)
    importer = Mock()
    importer.return_value.import_ova.return_value = vm
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        connection_factory=lambda *args: conn,
        importer_factory=importer,
    )
    with pytest.raises(ports.AlreadyExists):
        adapter.import_ova("http://ova.test/x", VM, "ds", ["net"])
    importer.assert_not_called()
    conn.get_all_vms.return_value = []
    got = adapter.import_ova("http://ova.test/x", VM, "ds", ["net"], timeout=120)
    assert got.name == VM and got.power_state == "poweredOff"
    importer.return_value.import_ova.assert_called_once_with(
        "http://ova.test/x", VM, "ds", ["net"], timeout=120
    )


def test_export_password_only_in_environment_and_no_overwrite(tmp_path):
    vm = virtual_machine()
    conn = connection(vm)
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        (tmp_path / "image.ova").write_bytes(b"ova")
        return NS(returncode=0)

    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "sensitive-password",
        connection_factory=lambda *args: conn,
        runner=run,
    )
    result = adapter.export_ova(VM, tmp_path, "image")
    assert result.size == 3
    assert "sensitive-password" not in repr(seen[0][0])
    assert seen[0][1]["env"]["ESXI_PASSWORD"] == "sensitive-password"
    with pytest.raises(ports.AlreadyExists):
        adapter.export_ova(VM, tmp_path, "image")
    assert len(seen) == 1


def test_error_classification_does_not_leak_password():
    factory = Mock(side_effect=OSError("secret-password in a socket failure"))
    adapter = esxi.EsxiAdapter(
        "192.0.2.1", "root", "secret-password", connection_factory=factory
    )
    with pytest.raises(ports.Unreachable) as caught:
        adapter.list_vms()
    assert "secret-password" not in str(caught.value)


def test_guestinfo_clear_requires_powered_off_and_preserves_other_keys():
    pytest.importorskip("pyVmomi")
    vm = virtual_machine(power="poweredOn")
    vm.config.extraConfig = [
        NS(key="guestinfo.ginnungagap.config", value="secret"),
        NS(key="foreign.key", value="keep"),
    ]
    conn = connection(vm)
    adapter = esxi.EsxiAdapter(
        "192.0.2.1", "root", "secret", connection_factory=lambda *args: conn
    )
    with pytest.raises(ports.PermanentError):
        adapter.clear_guestinfo(VM)
    vm.ReconfigVM_Task.assert_not_called()
    vm.runtime.powerState = "poweredOff"
    assert adapter.clear_guestinfo(VM) == ["guestinfo.ginnungagap.config"]
    spec = vm.ReconfigVM_Task.call_args.kwargs["spec"]
    assert [(item.key, item.value) for item in spec.extraConfig] == [
        ("guestinfo.ginnungagap.config", "")
    ]


def target(tmp_path):
    return ports.RemoteTarget(
        "192.0.2.1", "admin", tmp_path / "key", tmp_path / "known_hosts"
    )


def test_ssh_command_carries_sensitive_env_and_stdin_only_in_stdin(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return NS(returncode=0, stdout=b"answer", stderr=b"")

    adapter = remote.SshRemote(runner=run)
    result = adapter.run(
        target(tmp_path),
        ["program", "arg with spaces"],
        env={"TOKEN": "secret-value"},
        stdin=b"private content",
    )
    assert result.stdout == "answer"
    argv, kw = seen[0]
    assert "secret-value" not in repr(argv) and "private content" not in repr(argv)
    payload = json.loads(kw["input"])
    assert payload["env"] == {"TOKEN": "secret-value"}
    assert "ControlMaster=auto" in argv
    assert f"ControlPath={remote.SshRemote._control_path(target(tmp_path))}" in argv
    other = ports.RemoteTarget(
        "192.0.2.2", "admin", tmp_path / "key", tmp_path / "known_hosts"
    )
    assert remote.SshRemote._control_path(other) != remote.SshRemote._control_path(
        target(tmp_path)
    )


def test_ssh_failure_does_not_echo_untrusted_server_diagnostics(tmp_path):
    adapter = remote.SshRemote(
        runner=lambda *a, **k: NS(
            returncode=255, stdout=b"", stderr=b"private-password"
        )
    )
    with pytest.raises(ports.Unreachable) as caught:
        adapter.run(target(tmp_path), ["true"])
    assert "private-password" not in str(caught.value)


def test_prepare_requires_successful_status_and_marker_after_key_removal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        remote.uuid, "uuid4", lambda: type("Nonce", (), {"hex": "fresh"})()
    )
    seen = []
    adapter = remote.SshRemote(clock=FakeClock())
    states = iter(
        [
            {
                "fields": {
                    "ActiveState": "active",
                    "Result": "success",
                    "ExecMainStatus": "0",
                },
                "log": "working",
                "status": 0,
            },
            {
                "fields": {
                    "ActiveState": "inactive",
                    "Result": "success",
                    "ExecMainStatus": "0",
                },
                "log": "Ready.",
                "status": 0,
                "completion": {"nonce": "fresh", "returncode": 0},
            },
        ]
    )

    def run(t, argv, **kwargs):
        seen.append(argv)
        if argv[:2] == ["python3", "-c"]:
            return ports.CommandResult(0, json.dumps(next(states)))
        return ports.CommandResult(0)

    adapter.run = run
    result = adapter.run_detached(
        target(tmp_path),
        "imgprep-test",
        ["/prepare.sh"],
        log_path="/run/imgprep.log",
        timeout=100,
        success_marker="Ready.",
        poll_seconds=1,
    )
    assert result.ok
    probe = next(argv[2] for argv in seen if argv[:2] == ["python3", "-c"])
    assert "['sudo'" not in probe  # sudoers is deleted by prepare-image


def test_prepare_disconnect_is_not_assumed_success(tmp_path):
    adapter = remote.SshRemote(clock=FakeClock())

    def run(t, argv, **kwargs):
        if argv[:2] == ["python3", "-c"]:
            raise ports.Unreachable("lost")
        return ports.CommandResult(0)

    adapter.run = run
    with pytest.raises(ports.Timeout):
        adapter.run_detached(
            target(tmp_path),
            "imgprep-test",
            ["/prepare.sh"],
            log_path="/run/imgprep.log",
            timeout=2,
            poll_seconds=1,
        )


def test_forget_removes_keys_even_when_master_shutdown_times_out(tmp_path):
    t = target(tmp_path)
    for p in (
        t.key_file,
        t.key_file.with_suffix(".pub"),
        t.known_hosts,
        remote.SshRemote._control_path(t),
    ):
        p.write_text("secret")
    adapter = remote.SshRemote(
        runner=Mock(side_effect=subprocess.TimeoutExpired("ssh", 1))
    )
    with pytest.raises(ports.Timeout):
        adapter.forget(t)
    assert not any(tmp_path.iterdir())


def test_production_length_key_paths_use_short_private_distinct_control_sockets(
    tmp_path,
):
    import os
    import socket

    prefix = (
        tmp_path
        / "var/lib/ginnungagap-image-build/runs/1.3.5-rc.20261121-20261121T122346Z"
    )
    first = ports.RemoteTarget(
        "10.20.20.235",
        "junioradmin",
        prefix / "key-attempt-1/id_ed25519",
        prefix / "known_hosts",
    )
    second = ports.RemoteTarget(
        "10.20.20.235",
        "junioradmin",
        prefix / "key-attempt-2/id_ed25519",
        prefix / "known_hosts",
    )
    path = remote.SshRemote._control_path(first)
    assert path != remote.SshRemote._control_path(second)
    assert len(os.fsencode(path)) + 17 < 108  # OpenSSH's bind suffix also fits.
    assert path.parent.stat().st_uid == os.getuid()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    temporary = str(path) + ".test-bind"
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.bind(temporary)
    finally:
        Path(temporary).unlink(missing_ok=True)


def hardware_network(vm, addresses, *, matched=True, guest_nics=True):
    vim = pytest.importorskip("pyVmomi").vim
    vm.config.hardware = NS(
        device=[vim.vm.device.VirtualVmxnet3(macAddress="00:50:56:aa:bb:cc")]
    )
    vm.guest.ipAddress = "172.18.0.1"
    vm.guest.net = (
        []
        if not guest_nics
        else [
            NS(
                macAddress="02:42:ac:12:00:01", ipAddress=["172.18.0.1"], connected=True
            ),
            NS(
                macAddress="00-50-56-AA-BB-CC" if matched else "00:50:56:aa:bb:dd",
                ipAddress=addresses,
                connected=True,
            ),
        ]
    )
    return vm


def test_wait_for_ip_ignores_primary_docker_bridge_and_matches_hardware_mac():
    vm = hardware_network(
        virtual_machine(), ["fe80::250:56ff:feaa:bbcc", "10.20.20.235"]
    )
    conn = connection(vm)
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        clock=FakeClock(),
        connection_factory=lambda *args: conn,
    )
    assert adapter.wait_for_ip(VM, 4) == "10.20.20.235"
    conn.get_vm_ip_address.assert_not_called()


@pytest.mark.parametrize(
    "addresses,matched,guest_nics",
    [
        (["10.20.20.235"], False, True),
        (["10.20.20.235"], True, False),
        (
            [
                "fe80::1",
                "2001:db8::1",
                "169.254.1.2",
                "127.0.0.1",
                "0.0.0.0",
                "224.0.0.1",
            ],
            True,
            True,
        ),
        (["10.20.20.235", "10.20.20.236"], True, True),
    ],
)
def test_wait_for_ip_never_falls_back_to_primary_when_nic_report_is_unusable(
    addresses, matched, guest_nics
):
    vm = hardware_network(
        virtual_machine(), addresses, matched=matched, guest_nics=guest_nics
    )
    conn = connection(vm)
    clock = FakeClock()
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        clock=clock,
        connection_factory=lambda *args: conn,
    )
    before = clock.monotonic()
    with pytest.raises(ports.Timeout):
        adapter.wait_for_ip(VM, 4)
    assert clock.monotonic() - before == 4
    conn.get_vm_ip_address.assert_not_called()


def test_wait_for_ip_waits_for_matching_tools_nic_and_allows_other_management_subnets():
    vm = hardware_network(virtual_machine(), ["192.168.87.25"])
    vm.guest.net[1].ipAddress = []
    conn = connection(vm)
    clock = FakeClock()
    original_sleep = clock.sleep

    def sleep(seconds):
        original_sleep(seconds)
        vm.guest.net[1].ipAddress = ["192.168.87.25"]

    clock.sleep = sleep
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        clock=clock,
        connection_factory=lambda *args: conn,
    )
    assert adapter.wait_for_ip(VM, 4) == "192.168.87.25"


def test_wait_for_link_local_matches_actual_hardware_not_docker_interfaces():
    vm = hardware_network(
        virtual_machine(), ["fe80::250:56ff:feaa:bbcc", "10.20.20.184"]
    )
    vm.guest.net[0].ipAddress.append("fe80::42:acff:fe12:1")
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        clock=FakeClock(),
        connection_factory=lambda *args: connection(vm),
    )
    assert adapter.wait_for_link_local(VM, 4) == "fe80::250:56ff:feaa:bbcc"


def test_wait_for_link_local_never_uses_nonmatching_or_global_ipv6():
    vm = hardware_network(virtual_machine(), ["2001:db8::1", "10.20.20.184"])
    vm.guest.net[0].ipAddress.append("fe80::42:acff:fe12:1")
    adapter = esxi.EsxiAdapter(
        "192.0.2.1",
        "root",
        "secret",
        clock=FakeClock(),
        connection_factory=lambda *args: connection(vm),
    )
    with pytest.raises(ports.Timeout, match="hardware-NIC link-local"):
        adapter.wait_for_link_local(VM, 4)
