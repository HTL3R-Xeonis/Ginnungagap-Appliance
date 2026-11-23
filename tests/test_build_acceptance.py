"""Acceptance gates exercise all three boots and reject missing proof."""

import base64
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from ginnungagap.config import normalise
from image.build import acceptance as accept
from image.build.config import (
    BuildHostConfig,
    EsxiConfig,
    ManagementNetwork,
    PathsConfig,
)
from image.build.pipeline import RunOptions
from image.build.ports import (
    Blocked,
    CommandResult,
    KeyPair,
    PermanentError,
    RemoteTarget,
    Timeout,
    Unreachable,
)
from tests.helpers.fake_esxi import FakeClock, FakeEsxi

MANIFEST = {
    "components": {
        name: {"version": "1.4.0"}
        for name in (
            "appliance",
            "builder",
            "operator",
            "generator",
            "template-services",
        )
    }
}


def snapshot(version="1.4.0"):
    return {
        "services": ["ginnungagap", "gateway"],
        "healthchecked_services": ["ginnungagap", "gateway"],
        "containers": [
            {"Service": name, "State": "running", "Health": "healthy"}
            for name in ("ginnungagap", "gateway")
        ],
        "images": ["ginn-app:latest", "caddy:2"],
        "expected_images": ["ginn-app:latest", "caddy:2"],
        "installed": {
            "appliance": {"version": version},
            "components": {
                name: {"version": version} for name in MANIFEST["components"]
            },
        },
        "versions_program": version,
        "history": [],
        "check_exit": 0,
        "check_network_warning": False,
        "banner_version_ok": True,
        "banner_ssh_ok": True,
        "password_mode": "deployer",
        "password_leaked": False,
        "token_files": 0,
        "status_exit": 0,
        "versions_first": "Appliance release: v" + version,
    }


class Guest:
    def __init__(self):
        self.payload = None
        self.version = "1.4.0"
        self.persist = True
        self.calls = []
        self.trust = None
        self.trust_digest_override = None
        self.boot_number = 0
        self.reboot_units = []
        self.observed_boot_ids = []

    def generate_key(self, directory, comment):
        directory.mkdir(parents=True)
        key = directory / "id_ed25519"
        key.write_text("throwaway-test-key")
        key.chmod(0o600)
        public = key.with_suffix(".pub")
        public.write_text("ssh-ed25519 dGVzdA== " + comment)
        return KeyPair(key, public.read_text())

    def wait_ssh(self, target, timeout):
        self.calls.append("ssh-ready")

    def close_master(self, target):
        self.calls.append("ssh-master-closed")

    def put(self, target, local, remote, *, recursive=False):
        assert local.is_dir() and recursive
        self.calls.append("upload-release")

    def run(self, target, argv, *, stdin=None, **kwargs):
        if stdin is None:
            self.calls.append(list(argv))
            if "--apply" in argv:
                self.version = "1.4.0"
            elif "--rollback" in argv:
                self.version = "1.3.0"
            return CommandResult(0)
        payload = json.loads(stdin)
        action = payload["action"]
        self.calls.append(action)
        if action == "boot_identity":
            value = {"boot_id": f"00000000-0000-4000-8000-{self.boot_number:012d}"}
        elif action == "schedule_reboot":
            self.boot_number += 1
            self.reboot_units.append(payload["unit"])
            value = {"scheduled": True}
        elif action == "reboot_observation":
            boot_id = f"00000000-0000-4000-8000-{self.boot_number:012d}"
            self.observed_boot_ids.append(boot_id)
            value = {
                "boot_id": boot_id,
                "container_count": 9,
                "compose_ready": True,
                "ordering_cycles": 0,
            }
        elif action == "snapshot":
            value = snapshot(self.version)
        elif action == "create_state":
            self.payload = payload
            value = {"ok": True}
        elif action == "check_state":
            value = {"marker": self.persist, "admin": self.persist}
        elif action == "ova_create":
            value = {"size": 5242880}
        elif action == "install_update_trust":
            self.trust = base64.b64decode(payload["contents"], validate=True)
            value = {
                "sha256": self.trust_digest_override
                or hashlib.sha256(self.trust).hexdigest()
            }
        else:
            value = {"ok": True}
        return CommandResult(0, json.dumps(value))


class Http:
    def __init__(self, ip):
        self.basic = None
        self.configured = False
        self.session = False
        self.username = None
        self.calls = []

    def request(self, path, *, method="GET", form=None, port=None, headers=None):
        self.calls.append((path, method, bool(self.basic), port))
        body = {}
        status = 200
        hdr = {}
        if path == "/":
            if method == "POST":
                self.configured = True
            else:
                return accept.HttpResult(200, b"<title>Ginnungagap - Setup</title>", {})
        elif path == "/setup/":
            status = 200 if self.basic else 401
        elif path == "/appliance/health":
            status = 200 if self.configured else 503
            body = {"status": "ok" if self.configured else "unconfigured"}
        elif port == 8088:
            if path.endswith("/absent.ova"):
                status = 404
            elif method == "HEAD":
                hdr = {"content-length": "5242880", "accept-ranges": "bytes"}
            else:
                return accept.HttpResult(206, b"0123456789", {})
        elif path == "/health":
            body = {"status": "ok", "version": "1.4.0"}
        elif path == "/gateway/health":
            status = 404
        elif path == "/login":
            self.session = True
            self.username = form["username"]
        elif path == "/me":
            body = {"username": self.username}
            status = 200 if self.session else 401
        elif path == "/admin/updates":
            body = {
                "release": {"version": "1.4.0"},
                "result": {"id": "request1", "state": "done"},
            }
        elif path == "/admin/updates/check":
            status = 202
            body = {"id": "request1"}
        return accept.HttpResult(status, json.dumps(body).encode(), hdr)


@pytest.fixture
def ctx(tmp_path):
    clock = FakeClock()
    result = {
        "P2": {"manifest": MANIFEST, "release_dir": str(tmp_path / "release")},
        "P3": {
            "url": "http://images/base.ova",
            "version": "1.3.0",
            "username": "junioradmin",
        },
        "P5": {"target": {"user": "junioradmin"}},
        "P12": {"url": "http://images/new.ova"},
    }
    (tmp_path / "release").mkdir()
    trust = tmp_path / "daemon-allowed-signers"
    trust.write_bytes(b"release@example ssh-ed25519 ZGFlbW9uLXBpbm5lZC10cnVzdA==\n")
    context = SimpleNamespace(
        run_id="1.4.0-20261120T100000Z",
        version="1.4.0",
        run_dir=tmp_path,
        config=BuildHostConfig(
            esxi=EsxiConfig(datastore="datastore1", network="VM Network"),
            paths=PathsConfig(allowed_signers=trust),
        ),
        options=RunOptions(),
        esxi=FakeEsxi(clock=clock, datastore="datastore1"),
        remote=Guest(),
        clock=clock,
        results=result,
        summary=SimpleNamespace(warnings=[]),
        tracked=[],
        secrets=[],
    )
    context.result_of = lambda key: result[key]
    context.track_vm = lambda name: context.tracked.append(("vm", name))
    context.track_datastore_folder = lambda datastore, folder: context.tracked.append(
        ("folder", folder)
    )
    context.track_path = lambda path: context.tracked.append(("path", str(path)))
    context.secret = context.secrets.append
    context.note = lambda key, value: None

    def subdir(name):
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        return path

    context.subdir = subdir
    return context


def test_minimal_config_is_valid_and_contains_only_reserved_external_address():
    value = yaml.safe_load(accept._MINIMAL.read_text())
    normalise(value)
    assert value["generator"]["llm_servers"][0]["url"].startswith("http://192.0.2.")
    assert value["generator"]["llm_servers"][0]["name"] == "imgbuild-marker-3c9e1f7a"


def test_complete_snapshot_proves_every_required_check():
    checks = accept.validate_snapshot(snapshot(), MANIFEST, "1.4.0")
    assert checks and all(c["required"] and c["passed"] for c in checks)


@pytest.mark.parametrize(
    "mutation", [None, "old-file", "old-version", "not-current", "extra-record"]
)
def test_fresh_boot_history_allows_only_the_current_rollback_seed(mutation):
    data = snapshot()
    data["installed"]["entry"] = "20261120T100000Z-1.4.0.json"
    data["history"] = [
        {"file": data["installed"]["entry"], "version": "1.4.0", "current": True}
    ]
    item = data["history"][0]
    if mutation == "old-file":
        item["file"] = "20261119T100000Z-1.3.0.json"
    elif mutation == "old-version":
        item["version"] = "1.3.0"
    elif mutation == "not-current":
        item["current"] = False
    elif mutation == "extra-record":
        data["history"].append(dict(item))
    check = next(
        c
        for c in accept.validate_snapshot(data, MANIFEST, "1.4.0")
        if c["name"].startswith("update history")
    )
    assert check["passed"] is (mutation is None)


@pytest.mark.parametrize(
    "field",
    [
        "services",
        "containers",
        "healthchecked_services",
        "images",
        "expected_images",
        "installed",
        "versions_program",
        "history",
        "banner_version_ok",
        "banner_ssh_ok",
        "password_mode",
        "password_leaked",
        "token_files",
        "status_exit",
        "versions_first",
    ],
)
def test_a_missing_observation_never_counts_as_success(field):
    value = snapshot()
    del value[field]
    assert any(
        not c["passed"] for c in accept.validate_snapshot(value, MANIFEST, "1.4.0")
    )


@pytest.mark.parametrize(
    "health,checked,passed",
    [
        ("healthy", False, True),
        ("", False, True),
        (None, False, True),
        ("unhealthy", False, False),
        ("starting", False, False),
        ("", True, False),
    ],
)
def test_service_health_obeys_actual_container_healthcheck_contract(
    health, checked, passed
):
    data = snapshot()
    data["containers"][1]["Health"] = health
    if not checked:
        data["healthchecked_services"].remove("gateway")
    check = accept.validate_snapshot(data, MANIFEST, "1.4.0")[0]
    assert check["passed"] is passed


def test_missing_service_cannot_pass_by_omitting_its_healthcheck():
    data = snapshot()
    data["healthchecked_services"] = ["ginnungagap"]
    data["containers"].pop()
    assert accept.validate_snapshot(data, MANIFEST, "1.4.0")[0]["passed"] is False


def test_unhealthy_service_old_component_or_old_image_blocks_acceptance():
    for mutation in (
        lambda x: x["containers"][0].update(Health="starting"),
        lambda x: x["installed"]["components"]["operator"].update(version="1.3.0"),
        lambda x: x["images"].append("ginn-app:rollback-1.3.0"),
    ):
        data = snapshot()
        mutation(data)
        assert any(
            not c["passed"] for c in accept.validate_snapshot(data, MANIFEST, "1.4.0")
        )


def test_unconfigured_boot_has_no_guestinfo_and_tests_real_auth_transition(ctx):
    http = Http("192.0.2.1")
    checks = accept.boot_a(ctx, "http://images/new.ova", lambda ip: http)
    assert all(c["passed"] for c in checks)
    assert not any(call.name == "set_extra_config" for call in ctx.esxi.calls)
    assert ("/setup/", "POST", False, None) in http.calls
    assert ("/setup/", "POST", True, None) in http.calls
    assert (ctx.run_dir / "accept-a-console.png").read_bytes().startswith(b"\x89PNG")
    assert not ctx.esxi.vms


def static_network(ctx):
    network = ManagementNetwork(
        build_address="192.0.2.184",
        acceptance_address="192.0.2.185",
        prefix=24,
        gateway="192.0.2.1",
        dns=("192.0.2.53",),
    )
    ctx.config = replace(
        ctx.config,
        image=replace(ctx.config.image, management_network=network),
    )
    return network


@pytest.mark.parametrize("boot", ["a", "b", "c"])
def test_reserved_network_is_injected_before_boot_and_only_old_base_uses_bootstrap(
    ctx, monkeypatch, boot
):
    network = static_network(ctx)
    captured = {}
    configured = boot != "a"
    original_set = ctx.esxi.set_extra_config
    original_power = ctx.esxi.power_on
    calls = []

    def record(name, options):
        captured.update(options)
        original_set(name, options)

    def power(name):
        assert (
            json.loads(captured["guestinfo.ginnungagap.network"])["address"]
            == network.acceptance_address
        )
        calls.append("power")
        original_power(name)
        if boot != "c":
            ctx.esxi.vm(name).ip = network.acceptance_address

    def bootstrap(context, name, user, key_file, known_hosts, address):
        assert boot == "c" and calls == ["power"]
        assert user == "junioradmin" and key_file.exists()
        calls.append("bootstrap")
        ctx.esxi.vm(name).ip = address
        return RemoteTarget(address, user, key_file, known_hosts)

    monkeypatch.setattr(ctx.esxi, "set_extra_config", record)
    monkeypatch.setattr(ctx.esxi, "power_on", power)
    monkeypatch.setattr(
        accept.remote, "configure_static_guest", bootstrap, raising=False
    )
    name, ip, target = accept._new_vm(
        ctx, boot, "http://images/image.ova", configured=configured
    )
    assert ip == network.acceptance_address
    assert json.loads(captured["guestinfo.ginnungagap.network"]) == {
        "address": network.acceptance_address,
        "prefix": 24,
        "gateway": "192.0.2.1",
        "dns": ["192.0.2.53"],
    }
    assert calls == (["power", "bootstrap"] if boot == "c" else ["power"])
    if boot == "a":
        # No config, SSH key, password or sudo override: this remains a truly
        # unconfigured boot with its independently generated login password.
        assert set(captured) == {"guestinfo.ginnungagap.network"}
        assert target is None and not ctx.remote.calls and not ctx.secrets
    else:
        assert target.host == network.acceptance_address
        assert "guestinfo.ginnungagap.config" in captured
        assert "guestinfo.ginnungagap.ssh_authorized_key" in captured
    accept._remove_vm(ctx, name)
    assert not ctx.esxi.vms


@pytest.mark.parametrize("boot", ["a", "b"])
def test_static_acceptance_does_not_silently_accept_a_dhcp_address(
    ctx, monkeypatch, boot
):
    static_network(ctx)

    def no_bootstrap(*args):
        pytest.fail("new image must use native first boot, not a link-local fallback")

    monkeypatch.setattr(
        accept.remote, "configure_static_guest", no_bootstrap, raising=False
    )
    with pytest.raises(PermanentError, match="reserved static management address"):
        accept._new_vm(ctx, boot, "http://images/image.ova", configured=boot != "a")


@pytest.mark.parametrize("boot", ["a", "b", "c"])
def test_static_acceptance_waits_through_stale_tools_address_without_using_it(
    ctx, monkeypatch, boot
):
    network = static_network(ctx)
    answers = iter(["192.0.2.235", "192.0.2.235", network.acceptance_address])
    reads = []
    ssh_addresses = []

    def tools(name, timeout):
        address = next(answers)
        reads.append((address, timeout))
        return address

    def bootstrap(context, name, user, key_file, known_hosts, address):
        assert boot == "c" and address == network.acceptance_address
        ssh_addresses.append(address)
        return RemoteTarget(address, user, key_file, known_hosts)

    monkeypatch.setattr(ctx.esxi, "wait_for_ip", tools)
    monkeypatch.setattr(accept.remote, "configure_static_guest", bootstrap)
    monkeypatch.setattr(
        ctx.remote,
        "wait_ssh",
        lambda target, timeout: ssh_addresses.append(target.host),
    )
    name, address, target = accept._new_vm(
        ctx, boot, "http://images/image.ova", configured=boot != "a"
    )
    assert address == network.acceptance_address and len(reads) == 3
    assert all(timeout <= 30 for _, timeout in reads)
    assert ssh_addresses == ([] if boot == "a" else [network.acceptance_address])
    assert target is None if boot == "a" else target.host == network.acceptance_address
    accept._remove_vm(ctx, name)


@pytest.mark.parametrize("observed", ["192.0.2.235", "hidden-not-an-address"])
def test_wrong_static_tools_address_has_absolute_deadline_and_no_ssh(
    ctx, monkeypatch, observed
):
    network = static_network(ctx)
    reads = []
    started = []

    def tools(name, timeout):
        if not started:
            started.append(ctx.clock.monotonic())
        reads.append(timeout)
        return observed

    monkeypatch.setattr(ctx.esxi, "wait_for_ip", tools)
    with pytest.raises(
        PermanentError, match="reserved static management address"
    ) as failure:
        accept._new_vm(ctx, "b", "http://images/image.ova", configured=True)
    assert ctx.clock.monotonic() - started[0] == accept.BOOT_TIMEOUT
    assert all(timeout <= 30 for timeout in reads)
    assert "ssh-ready" not in ctx.remote.calls
    assert f"expected {network.acceptance_address}" in str(failure.value)
    assert "hidden" not in str(failure.value)
    assert (
        f"last observed {observed}" in str(failure.value)
        if observed == "192.0.2.235"
        else "last observed invalid address" in str(failure.value)
    )


def test_absent_static_tools_address_uses_remaining_deadline_for_each_slice(
    ctx, monkeypatch
):
    budgets = []
    started = ctx.clock.monotonic()

    def absent(name, timeout):
        budgets.append((ctx.clock.monotonic() - started, timeout))
        ctx.clock.advance(timeout)
        raise Timeout("no VMware Tools address")

    monkeypatch.setattr(ctx.esxi, "wait_for_ip", absent)
    with pytest.raises(Timeout, match="expected 192.0.2.185; last observed no address"):
        accept._reserved_address(ctx, "vm", "192.0.2.185", timeout=50)
    assert ctx.clock.monotonic() - started == 50
    assert budgets == [(0, 30), (35, 15)]
    assert not ctx.remote.calls


def test_nonstatic_acceptance_keeps_single_tools_wait_and_connects_to_its_address(
    ctx, monkeypatch
):
    reads = []
    ssh_addresses = []

    def tools(name, timeout):
        reads.append(timeout)
        return "192.0.2.235"

    monkeypatch.setattr(ctx.esxi, "wait_for_ip", tools)
    monkeypatch.setattr(
        ctx.remote,
        "wait_ssh",
        lambda target, timeout: ssh_addresses.append(target.host),
    )
    name, address, target = accept._new_vm(
        ctx, "b", "http://images/image.ova", configured=True
    )
    assert address == target.host == "192.0.2.235"
    assert reads == [accept.BOOT_TIMEOUT] and ssh_addresses == [address]
    accept._remove_vm(ctx, name)


def test_configured_boot_checks_range_admin_update_and_state_survival(ctx):
    http = Http("192.0.2.1")
    http.configured = True
    checks = accept.boot_b(ctx, "http://images/new.ova", MANIFEST, lambda ip: http)
    assert all(c["passed"] for c in checks)
    assert "create_state" in ctx.remote.calls and "apply" in ctx.remote.calls
    assert "check_state" in ctx.remote.calls and "ova_remove" in ctx.remote.calls
    assert not ctx.esxi.vms
    calls = [call.name for call in ctx.esxi.calls]
    assert calls.index("set_extra_config") < calls.index("power_on")
    assert ctx.remote.boot_number == 4
    assert len(set(ctx.remote.observed_boot_ids)) == 4
    assert len(set(ctx.remote.reboot_units)) == 4
    assert ctx.remote.calls.count("ssh-master-closed") == 4
    assert sum(check["name"].startswith("configured reboot ") for check in checks) == 4


def test_operator_readiness_waits_for_exact_version_on_real_appliance_route(ctx):
    class Starting(Http):
        def __init__(self):
            super().__init__("192.0.2.1")
            self.configured = True
            self.responses = [
                accept.HttpResult(503, b"starting", {}),
                accept.HttpResult(200, b'{"version":"1.3.0"}', {}),
                accept.HttpResult(200, b'{"version":"1.4.0"}', {}),
            ]
            self.observed = []
            self.observed_at = []

        def request(self, path, **kwargs):
            normal = super().request(path, **kwargs)
            if path == "/health":
                assert self.responses, "must stop polling after the exact version"
                result = self.responses.pop(0)
                self.observed.append(result)
                self.observed_at.append(ctx.clock.monotonic())
                return result
            return normal

    http = Starting()
    checks = accept.boot_b(ctx, "http://images/new.ova", MANIFEST, lambda ip: http)
    assert all(check["passed"] for check in checks)
    assert len(http.observed) == 3
    assert http.observed_at[-1] - http.observed_at[0] == 2 * accept.POLL
    assert not any(call[0] == "/gateway/health" for call in http.calls)
    assert ctx.remote.boot_number == 4


def _use_fake_http_transport(monkeypatch, factory=Http):
    clients = []

    def request(client, path, **kwargs):
        if not hasattr(client, "test_transport"):
            transport = factory(client.base)
            transport.configured = bool(clients)  # Boot A is really unconfigured.
            client.test_transport = transport
            clients.append(transport)
        client.test_transport.basic = client.basic
        return client.test_transport.request(path, **kwargs)

    monkeypatch.setattr(accept.HttpClient, "request", request)
    return clients


@pytest.mark.parametrize(
    "response,error,observation",
    [
        (
            accept.HttpResult(200, b'{"version":"1.3.0","secret":"hidden"}', {}),
            PermanentError,
            "observed HTTP 200, version='1.3.0'",
        ),
        (
            accept.HttpResult(503, b"hidden", {}),
            PermanentError,
            "observed HTTP 503, version=None",
        ),
        (
            accept.HttpResult(0, b"hidden", {}),
            Unreachable,
            "observed HTTP 0, version=None",
        ),
        (
            accept.HttpResult(200, b'{"version":"hidden"}', {}),
            PermanentError,
            "observed HTTP 200, version=None",
        ),
    ],
)
def test_operator_readiness_deadline_keeps_diagnostics_and_partial_report(
    ctx, monkeypatch, response, error, observation
):
    class Mismatch(Http):
        def __init__(self, ip):
            super().__init__(ip)
            self.observed_at = []

        def request(self, path, **kwargs):
            normal = super().request(path, **kwargs)
            if path == "/health":
                self.observed_at.append(ctx.clock.monotonic())
                return response
            return normal

    clients = _use_fake_http_transport(monkeypatch, Mismatch)
    with pytest.raises(error, match="expected Operator version='1.4.0'") as failure:
        accept.acceptance_phase(ctx)
    assert observation in str(failure.value)
    assert "hidden" not in str(failure.value)
    assert (
        clients[1].observed_at[-1] - clients[1].observed_at[0] == accept.CHECK_TIMEOUT
    )
    report = json.loads((ctx.run_dir / "acceptance.json").read_text())
    assert report["passed"] is False
    assert any(c["name"].startswith("a: ") and c["passed"] for c in report["checks"])
    version = next(c for c in report["checks"] if c["name"] == "b: Operator version")
    assert version["passed"] is False and observation in version["detail"]
    assert "hidden" not in json.dumps(report)
    assert all(
        call[0] != "/gateway/health" for client in clients for call in client.calls
    )
    assert not ctx.esxi.vms


def test_failed_configured_gate_keeps_completed_four_reboots_in_report(
    ctx, monkeypatch
):
    _use_fake_http_transport(monkeypatch)
    original = ctx.remote.run

    def run(target, argv, **kwargs):
        result = original(target, argv, **kwargs)
        payload = json.loads(kwargs["stdin"]) if kwargs.get("stdin") else {}
        if payload.get("action") == "snapshot":
            data = json.loads(result.stdout)
            data["status_exit"] = 1
            return CommandResult(0, json.dumps(data))
        return result

    ctx.remote.run = run
    with pytest.raises(PermanentError, match="configured boot failed"):
        accept.acceptance_phase(ctx)
    report = json.loads((ctx.run_dir / "acceptance.json").read_text())
    assert report["passed"] is False and ctx.remote.boot_number == 4
    reboots = [
        c for c in report["checks"] if c["name"].startswith("b: configured reboot ")
    ]
    assert len(reboots) == 4 and all(c["passed"] for c in reboots)
    assert any(
        c["name"].startswith("b: ") and not c["passed"] for c in report["checks"]
    )
    assert not ctx.esxi.vms


def test_failed_third_reboot_keeps_first_two_reboot_rows(ctx, monkeypatch):
    _use_fake_http_transport(monkeypatch)
    original = ctx.remote.run

    def run(target, argv, **kwargs):
        result = original(target, argv, **kwargs)
        payload = json.loads(kwargs["stdin"]) if kwargs.get("stdin") else {}
        if payload.get("action") == "schedule_reboot" and ctx.remote.boot_number == 3:
            return CommandResult(0, '{"scheduled":false}')
        return result

    ctx.remote.run = run
    with pytest.raises(PermanentError, match="reboot was not scheduled"):
        accept.acceptance_phase(ctx)
    report = json.loads((ctx.run_dir / "acceptance.json").read_text())
    reboots = [
        c for c in report["checks"] if c["name"].startswith("b: configured reboot ")
    ]
    assert len(reboots) == 2 and all(c["passed"] for c in reboots)
    assert report["passed"] is False and not ctx.esxi.vms


def test_failed_upgrade_gate_keeps_prior_boots_and_upgrade_rows(ctx, monkeypatch):
    _use_fake_http_transport(monkeypatch)
    ctx.remote.trust_digest_override = "0" * 64
    with pytest.raises(PermanentError, match="upgrade trust failed"):
        accept.acceptance_phase(ctx)
    report = json.loads((ctx.run_dir / "acceptance.json").read_text())
    assert report["passed"] is False
    assert (
        sum(c["name"].startswith("b: configured reboot ") for c in report["checks"])
        == 4
    )
    failed = next(
        c
        for c in report["checks"]
        if c["name"] == "c: upgrade uses daemon-pinned signing trust"
    )
    assert failed["passed"] is False
    assert not ctx.esxi.vms


def test_admin_update_poll_waits_through_null_and_running_status_for_its_request(ctx):
    class PendingUpdates(Http):
        def __init__(self, ip):
            super().__init__(ip)
            self.configured = True
            self.requested = False
            self.release_reads = 0
            self.results = [
                None,
                {"id": "previous-request", "state": "done"},
                {"id": "request1", "state": "running"},
                {"id": "request1", "state": "done"},
            ]
            self.observed = []

        def request(self, path, **kwargs):
            response = super().request(path, **kwargs)
            if path == "/admin/updates/check":
                self.requested = True
            elif path == "/admin/updates":
                body = response.json()
                if not self.requested:
                    self.release_reads += 1
                    # The API may expose no installed-state object yet.
                    body["release"] = (
                        None if self.release_reads == 1 else body["release"]
                    )
                    body["result"] = None
                else:
                    assert self.results, (
                        "polling continued after the matching done result"
                    )
                    body["result"] = self.results.pop(0)
                    self.observed.append(body["result"])
                return accept.HttpResult(200, json.dumps(body).encode(), {})
            return response

    http = PendingUpdates("192.0.2.1")
    checks = accept.boot_b(ctx, "http://images/new.ova", MANIFEST, lambda ip: http)
    assert all(check["passed"] for check in checks)
    assert http.release_reads == 2
    assert http.observed[0] is None
    assert [result["state"] for result in http.observed[1:]] == [
        "done",
        "running",
        "done",
    ]
    assert http.observed[-1] == {"id": "request1", "state": "done"}
    assert not http.results


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"result": None},
        {"result": []},
        {"result": True},
        {"result": "pending"},
    ],
)
def test_nullable_or_nonobject_update_result_is_never_completion(body):
    response = accept.HttpResult(200, json.dumps(body).encode(), {})
    assert accept._web_check_done(response, "request1") is False


@pytest.mark.parametrize(
    "mutation",
    [
        "stale-id",
        "repeated-id",
        "eight-containers",
        "unhealthy",
        "ordering-cycle",
        "missing-journal-proof",
        "invalid-id",
    ],
)
def test_configured_reboot_requires_new_kernel_and_complete_healthy_startup(
    ctx, monkeypatch, mutation
):
    target = RemoteTarget(
        "192.0.2.185", "junioradmin", ctx.run_dir / "key", ctx.run_dir / "known_hosts"
    )
    original = ctx.remote.run

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        request = json.loads(kwargs["stdin"])
        if request["action"] != "reboot_observation":
            return result
        data = json.loads(result.stdout)
        if mutation == "stale-id":
            data["boot_id"] = "00000000-0000-4000-8000-000000000000"
        elif mutation == "repeated-id" and ctx.remote.boot_number >= 2:
            data["boot_id"] = "00000000-0000-4000-8000-000000000001"
        elif mutation == "eight-containers":
            data["container_count"] = 8
        elif mutation == "unhealthy":
            data["compose_ready"] = False
        elif mutation == "ordering-cycle":
            data["ordering_cycles"] = 1
        elif mutation == "missing-journal-proof":
            del data["ordering_cycles"]
        elif mutation == "invalid-id":
            data["boot_id"] = "not-a-kernel-boot-id"
        return CommandResult(0, json.dumps(data))

    monkeypatch.setattr(ctx.remote, "run", mutate)
    start = ctx.clock.monotonic()
    with pytest.raises(PermanentError, match="configured reboot"):
        accept.configured_reboots(ctx, target, timeout=10)
    assert ctx.clock.monotonic() - start <= 10
    if mutation == "repeated-id":
        assert ctx.remote.boot_number == 2


def test_configured_reboot_reconnection_failure_has_a_deadline(ctx, monkeypatch):
    target = RemoteTarget(
        "192.0.2.185", "junioradmin", ctx.run_dir / "key", ctx.run_dir / "known_hosts"
    )

    def disconnected(*args):
        raise Unreachable("guest is unreachable")

    monkeypatch.setattr(ctx.remote, "wait_ssh", disconnected)
    start = ctx.clock.monotonic()
    with pytest.raises(Unreachable, match="configured reboot 1.*deadline"):
        accept.configured_reboots(ctx, target, timeout=10)
    assert ctx.clock.monotonic() - start == 10
    assert ctx.remote.boot_number == 1


def test_a_marker_lost_during_apply_fails_and_still_cleans_its_vm(ctx):
    http = Http("192.0.2.1")
    http.configured = True
    ctx.remote.persist = False
    with pytest.raises(PermanentError, match="survive apply"):
        accept.boot_b(ctx, "http://images/new.ova", MANIFEST, lambda ip: http)
    assert not ctx.esxi.vms


def test_upgrade_is_a_real_update_and_rollback_not_a_second_fresh_install(ctx):
    ctx.remote.version = "1.3.0"
    http = Http("192.0.2.1")
    http.configured = True
    checks = accept.boot_c(ctx, MANIFEST, lambda ip: http)
    assert all(c["passed"] for c in checks)
    commands = [call for call in ctx.remote.calls if isinstance(call, list)]
    assert any("--apply" in call and "--offline-dir" in call for call in commands)
    assert any("--rollback" in call for call in commands)
    assert "upload-release" in ctx.remote.calls
    assert ctx.remote.trust == ctx.config.paths.allowed_signers.read_bytes()
    apply_index = next(
        i
        for i, call in enumerate(ctx.remote.calls)
        if isinstance(call, list) and "--apply" in call
    )
    assert ctx.remote.calls.index("install_update_trust") < apply_index
    assert not ctx.esxi.vms


@pytest.mark.parametrize("wrong_component", [False, True])
def test_rollback_compares_real_adopted_base_components_not_catalog_label(
    ctx, wrong_component
):
    ctx.remote.version = "1.3.0"
    http = Http("192.0.2.1")
    http.configured = True
    original_run = ctx.remote.run
    snapshots = []

    def run(target, argv, *, stdin=None, **kwargs):
        result = original_run(target, argv, stdin=stdin, **kwargs)
        if stdin is not None and json.loads(stdin)["action"] == "snapshot":
            body = json.loads(result.stdout)
            snapshots.append(body)
            if len(snapshots) in (1, 3):
                body["installed"]["appliance"]["version"] = "adopted"
                body["installed"]["components"]["builder"]["commit"] = "a" * 40
                if len(snapshots) == 3 and wrong_component:
                    body["installed"]["components"]["builder"]["commit"] = "b" * 40
            return CommandResult(0, json.dumps(body))
        return result

    ctx.remote.run = run
    if wrong_component:
        with pytest.raises(PermanentError, match="exact captured base identity"):
            accept.boot_c(ctx, MANIFEST, lambda ip: http)
    else:
        checks = accept.boot_c(ctx, MANIFEST, lambda ip: http)
        assert all(c["passed"] for c in checks)
    assert len(snapshots) == 3
    assert not ctx.esxi.vms


@pytest.mark.parametrize("unsigned", [False, True])
@pytest.mark.parametrize("prerelease", [False, True])
def test_upgrade_guest_config_and_command_honor_explicit_flags(
    ctx, unsigned, prerelease
):
    ctx.options = RunOptions(allow_unsigned=unsigned, allow_prerelease=prerelease)
    ctx.remote.version = "1.3.0"
    captured = {}
    original = ctx.esxi.set_extra_config

    def record(name, options):
        captured.update(options)
        original(name, options)

    ctx.esxi.set_extra_config = record
    # A release may ship malicious trust; it must never become the guest's trust.
    (ctx.run_dir / "release" / "allowed_signers").write_bytes(
        b"release-supplied-untrusted"
    )
    if unsigned:
        ctx.config.paths.allowed_signers.unlink()
    http = Http("192.0.2.1")
    http.configured = True
    checks = accept.boot_c(ctx, MANIFEST, lambda ip: http)
    config = yaml.safe_load(base64.b64decode(captured["guestinfo.ginnungagap.config"]))
    normalise(config)
    assert config["update"]["require_signature"] is (not unsigned)
    assert config["update"]["channel"] == ("prerelease" if prerelease else "stable")
    apply = next(
        call
        for call in ctx.remote.calls
        if isinstance(call, list) and "--apply" in call
    )
    assert apply[apply.index("--channel") + 1] == config["update"]["channel"]
    if unsigned:
        assert ctx.remote.trust is None
        assert any(
            "explicit manual unsigned override" in check["name"] for check in checks
        )
    else:
        assert ctx.remote.trust == ctx.config.paths.allowed_signers.read_bytes()


@pytest.mark.parametrize("contents", [None, b"", b"# no trusted signers\n\n"])
def test_signed_upgrade_refuses_missing_or_empty_daemon_trust_before_boot(
    ctx, contents
):
    if contents is None:
        ctx.config.paths.allowed_signers.unlink()
    else:
        ctx.config.paths.allowed_signers.write_bytes(contents)
    with pytest.raises(Blocked, match="daemon-pinned allowed_signers"):
        accept.boot_c(ctx, MANIFEST)
    assert not ctx.esxi.imports
    assert not ctx.remote.calls


def test_signed_upgrade_rejects_changed_guest_trust_before_update_and_cleans(ctx):
    ctx.remote.trust_digest_override = "0" * 64
    http = Http("192.0.2.1")
    http.configured = True
    with pytest.raises(PermanentError, match="daemon-pinned signing trust"):
        accept.boot_c(ctx, MANIFEST, lambda ip: http)
    assert not any(
        isinstance(call, list) and "--apply" in call for call in ctx.remote.calls
    )
    assert not ctx.esxi.vms


def test_manual_upgrade_override_never_changes_shared_fixture_or_other_boot_config(ctx):
    ctx.options = RunOptions(allow_unsigned=True, allow_prerelease=True)
    before = accept._MINIMAL.read_bytes()
    assert yaml.safe_load(accept._acceptance_config(ctx, "b")) == yaml.safe_load(before)
    assert accept._MINIMAL.read_bytes() == before


def test_http_wait_is_bounded_even_when_a_guest_never_answers(ctx):
    broken = SimpleNamespace(request=lambda *args, **kw: accept.HttpResult(0, b"", {}))
    start = ctx.clock.monotonic()
    with pytest.raises(Unreachable, match="HTTP endpoint"):
        accept.wait_http(
            ctx, broken, "/appliance/health", lambda r: r.status == 200, timeout=10
        )
    assert ctx.clock.monotonic() - start == 10


def test_empty_or_optional_only_success_cannot_hide_a_required_failure():
    with pytest.raises(PermanentError):
        accept._required([], "acceptance")
    with pytest.raises(PermanentError):
        accept._required(
            [{"name": "optional", "passed": True, "required": False}], "acceptance"
        )
    with pytest.raises(PermanentError):
        accept._required(
            [{"name": "state", "passed": False, "required": True}], "acceptance"
        )


def test_journal_probe_does_not_log_its_own_search_phrase_via_sudo():
    # sudo may journal the entire Python argv. Its diagnostic command must not
    # become evidence of the startup failure it searches for.
    assert "ordering cycle" not in accept._GUEST_PROBE.lower()


@pytest.mark.parametrize("actual_cycle", [False, True])
@pytest.mark.parametrize(
    "health_variant",
    ["healthy", "no-check-empty", "no-check-unhealthy", "check-empty", "none-disabled"],
)
def test_real_journal_probe_ignores_its_sudo_command_and_counts_mixed_case_failure(
    actual_cycle,
    health_variant,
):
    harness = r"""
import io,json,sys,types
request=json.load(sys.stdin)
sys.stdin=io.StringIO(json.dumps(request))
class Path:
 def __init__(self,value): self.value=str(value)
 def __truediv__(self,other): return Path(self.value+'/'+other)
 def read_text(self):
  return {'/etc/ginnungagap/install.env':'PYTHONPATH=/app\n',
   '/app/rendered/compose.env':'',
   '/proc/sys/kernel/random/boot_id':'00000000-0000-4000-8000-000000000001'}[self.value]
def run(argv,**kwargs):
 if argv[0]=='journalctl': output=request['journal']
 elif argv[:2]==['docker','ps']: output='\n'.join('id'+str(n) for n in range(9))
 elif argv[:2]==['docker','inspect']:
  n=int(argv[2],16)
  test={'Test':['CMD','true']}
  if n==0 and request['health_variant'].startswith('no-check'): test=None
  elif n==0 and request['health_variant']=='none-disabled': test={'Test':['NONE']}
  output=json.dumps([{'Config':{'Healthcheck':test}}])
 elif argv[-2:]==['config','--services']: output='\n'.join('service'+str(n) for n in range(9))
 elif argv[-4:]==['ps','--all','--format','json']:
  containers=[{'ID':f'{n:012x}','Service':'service'+str(n),'State':'running','Health':'healthy'} for n in range(9)]
  if request['health_variant'] in ('no-check-empty','check-empty','none-disabled'): containers[0]['Health']=''
  elif request['health_variant']=='no-check-unhealthy': containers[0]['Health']='unhealthy'
  output=json.dumps(containers)
 else: raise RuntimeError('unexpected guest command')
 return types.SimpleNamespace(returncode=0,stdout=output,stderr='')
sys.modules['pathlib']=types.SimpleNamespace(Path=Path)
sys.modules['subprocess']=types.SimpleNamespace(run=run)
exec(compile(request['code'],'pinned-guest-probe','exec'))
"""
    journal = "sudo: COMMAND=/usr/bin/python3 -c " + accept._GUEST_PROBE.replace(
        "\n", " "
    )
    if actual_cycle:
        journal += "\nsystemd: Found OrDeRiNg CyClE on docker.service/start\n"
    result = subprocess.run(
        [sys.executable, "-c", harness],
        input=json.dumps(
            {
                "action": "reboot_observation",
                "journal": journal,
                "code": accept._GUEST_PROBE,
                "health_variant": health_variant,
            }
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    observation = json.loads(result.stdout)
    assert observation["ordering_cycles"] == int(actual_cycle)
    assert observation["container_count"] == 9
    assert observation["compose_ready"] is (
        health_variant not in ("no-check-unhealthy", "check-empty")
    )
    assert ("service0" in observation["healthchecked_services"]) is (
        health_variant in ("healthy", "check-empty")
    )
