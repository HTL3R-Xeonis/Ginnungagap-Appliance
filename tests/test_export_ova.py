"""The OVA export scripts in image/, tested without an ESXi: everything that does not need one is a plain function."""

import base64
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("pyVmomi")

IMAGE = Path(__file__).resolve().parent.parent / "image"
if str(IMAGE) not in sys.path:
    sys.path.insert(0, str(IMAGE))

import appliance_common as common


def load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", IMAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = load("export_ova")
describe = load("describe_template")
guestinfo = load("set_guestinfo")

OVF = """<?xml version="1.0"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1" xmlns:rasd="r">
  <References>
    <File ovf:href="App-disk0.vmdk" ovf:id="file1" ovf:size="9"/>
  </References>
  <NetworkSection>
    <Info>The list of logical networks</Info>
    <Network ovf:name="PG-MGMT"><Description>The PG-MGMT network</Description></Network>
  </NetworkSection>
  <Item>
    <rasd:Connection>PG-MGMT</rasd:Connection>
    <rasd:Description>E1000 ethernet adapter on &quot;PG-MGMT&quot;</rasd:Description>
  </Item>
</Envelope>
"""


# ---------- OVF ----------


def test_the_network_is_renamed_everywhere_in_the_descriptor():
    text = export.rewrite_networks(OVF, "Network")
    assert 'ovf:name="Network"' in text
    assert "<rasd:Connection>Network</rasd:Connection>" in text
    assert (
        "&quot;Network&quot;" in text
        and "<Description>The Network network</Description>" in text
    )
    assert "PG-MGMT" not in text


def test_a_second_network_gets_a_numbered_name_and_special_characters_stay_valid_xml():
    ovf = OVF.replace(
        "</NetworkSection>", '<Network ovf:name="A &amp; B"/></NetworkSection>'
    ).replace("</Item>", "<rasd:Connection>A &amp; B</rasd:Connection></Item>")
    assert export.network_names(ovf) == ["PG-MGMT", "A & B"]
    text = export.rewrite_networks(ovf, 'Lab "net"')
    assert (
        'ovf:name="Lab &quot;net&quot;"' in text
        and 'ovf:name="Lab &quot;net&quot;-2"' in text
    )
    assert "<rasd:Connection>Lab &quot;net&quot;-2</rasd:Connection>" in text


def test_a_descriptor_without_networks_is_left_alone():
    assert export.rewrite_networks("<Envelope/>", "Network") == "<Envelope/>"


def test_only_the_files_the_descriptor_refers_to_are_kept():
    assert export.referenced_files(OVF, ["App-disk0.vmdk", "app.nvram"]) == [
        "App-disk0.vmdk"
    ]


# ---------- manifest and tar ----------


def test_the_manifest_lists_every_file_with_its_sha256(tmp_path):
    a, b = tmp_path / "a.ovf", tmp_path / "b.vmdk"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert export.manifest_text([a, b]) == (
        f"SHA256(a.ovf)= {hashlib.sha256(b'one').hexdigest()}\n"
        f"SHA256(b.vmdk)= {hashlib.sha256(b'two').hexdigest()}\n"
    )


def make_members(tmp_path, names=("x.ovf", "x.mf", "x-disk0.vmdk")):
    members = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(name.encode() * 100)
        members.append(path)
    return members


def test_the_ova_is_a_ustar_tar_with_the_descriptor_first(tmp_path):
    members = make_members(tmp_path)
    ova = tmp_path / "x.ova"
    export.pack_ova(ova, members)
    assert (
        ova.read_bytes()[257:265] == b"ustar\x0000"
    )  # the ustar magic and version (GNU would be "ustar  \0")
    with tarfile.open(ova) as archive:
        assert [m.name for m in archive.getmembers()] == [
            "x.ovf",
            "x.mf",
            "x-disk0.vmdk",
        ]
        assert archive.extractfile("x-disk0.vmdk").read() == b"x-disk0.vmdk" * 100
        assert all(
            m.uid == 0 and m.gid == 0 and m.mode == 0o644 for m in archive.getmembers()
        )


def test_packing_refuses_what_is_not_a_valid_ova(tmp_path):
    members = make_members(tmp_path)
    with pytest.raises(ValueError, match="first member"):
        export.pack_ova(tmp_path / "a.ova", [members[1], members[0]])
    with pytest.raises(ValueError, match="first member"):
        export.pack_ova(tmp_path / "a.ova", [])
    long_name = tmp_path / ("n" * 101 + ".vmdk")
    long_name.write_bytes(b"x")
    with pytest.raises(ValueError, match="100 characters"):
        export.pack_ova(tmp_path / "a.ova", [members[0], long_name])
    umlaut = tmp_path / "größe.vmdk"
    umlaut.write_bytes(b"x")
    with pytest.raises(ValueError, match="ASCII"):
        export.pack_ova(tmp_path / "a.ova", [members[0], umlaut])


# ---------- space ----------


def test_the_space_check_compares_with_what_is_free(tmp_path):
    needed = export.estimate_needed(10 * 2**30)
    assert needed == 20 * 2**30 + export.SAFETY_MARGIN
    export.check_free_space(
        tmp_path, needed, usage=lambda _: SimpleNamespace(free=needed)
    )
    with pytest.raises(OSError, match="not enough free space"):
        export.check_free_space(
            tmp_path, needed, usage=lambda _: SimpleNamespace(free=needed - 1)
        )


# ---------- lease ----------


class FakeLease:
    def __init__(self, states, error=None):
        self.states = list(states)
        self.error = error
        self.progress: list[int] = []
        self.completed = self.aborted = False

    @property
    def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def HttpNfcLeaseProgress(self, percent):
        self.progress.append(percent)

    def HttpNfcLeaseComplete(self):
        self.completed = True

    def HttpNfcLeaseAbort(self, fault=None):
        self.aborted = True


def test_waiting_for_the_lease_ends_when_it_is_ready():
    sleeps = []
    export.wait_for_lease(
        FakeLease(["initializing", "initializing", "ready"]), sleep=sleeps.append
    )
    assert len(sleeps) == 2


def test_a_failed_or_ended_lease_is_an_error_with_the_servers_message():
    with pytest.raises(export.LeaseError, match="no datastore access"):
        export.wait_for_lease(
            FakeLease(["error"], SimpleNamespace(msg="no datastore access"))
        )
    with pytest.raises(export.LeaseError, match="is done"):
        export.wait_for_lease(FakeLease(["done"]))


def test_a_lease_that_never_gets_ready_times_out():
    ticks = iter(range(0, 1000, 50))
    with pytest.raises(export.LeaseError, match="in time"):
        export.wait_for_lease(
            FakeLease(["initializing"]),
            timeout=120,
            sleep=lambda s: None,
            clock=lambda: next(ticks),
        )


def test_the_keeper_reports_progress_until_it_is_left():
    lease = FakeLease(["ready"])
    with export.LeaseKeeper(lease, interval=0.01) as keeper:
        keeper.set_percent(250)  # clamped: only the end of the export reports 100
        import time

        time.sleep(0.1)
    assert lease.progress and set(lease.progress) <= {0, 99}
    count = len(lease.progress)
    import time

    time.sleep(0.05)
    assert len(lease.progress) == count  # stopped


def test_a_failing_keep_alive_is_remembered_not_raised():
    class Broken(FakeLease):
        def HttpNfcLeaseProgress(self, percent):
            raise RuntimeError("lease gone")

    with export.LeaseKeeper(Broken(["ready"]), interval=0.01) as keeper:
        import time

        time.sleep(0.05)
    assert isinstance(keeper.failure, RuntimeError)


# ---------- download ----------


def test_a_download_is_written_in_chunks_and_counted(tmp_path):
    data = bytes(range(256)) * 1000
    seen = []
    written = export.download_stream(
        io.BytesIO(data),
        tmp_path / "d.vmdk",
        chunk_size=4096,
        on_bytes=seen.append,
        expected=len(data),
    )
    assert written == len(data) == sum(seen)
    assert max(seen) <= 4096 and (tmp_path / "d.vmdk").read_bytes() == data


def test_a_truncated_download_is_an_error(tmp_path):
    with pytest.raises(OSError, match="10 bytes received, 20 expected"):
        export.download_stream(io.BytesIO(b"x" * 10), tmp_path / "d.vmdk", expected=20)


def test_the_star_in_a_lease_url_is_the_host_and_files_get_stable_names():
    assert (
        export.target_url("https://*/nfc/52/disk-0.vmdk", "10.0.0.1")
        == "https://10.0.0.1/nfc/52/disk-0.vmdk"
    )
    disk = SimpleNamespace(disk=True, url="https://*/x")
    other = SimpleNamespace(disk=False, url="https://*/nfc/5/my%20vm.nvram")
    assert export.local_name("App", disk, 0) == "App-disk0.vmdk"
    assert export.local_name("App", other, 1) == "my_vm.nvram"


def test_progress_never_reaches_100_before_the_end():
    assert export.percent_done(50, 100) == 50
    assert export.percent_done(100, 100) == 99
    assert export.percent_done(5, 0) == 0


# ---------- the whole export against stand-ins ----------


class FakeOvfManager:
    def __init__(self, descriptor=OVF, errors=()):
        self.descriptor, self.errors, self.cdp = descriptor, list(errors), None

    def CreateDescriptor(self, obj, cdp):
        self.cdp = cdp
        return SimpleNamespace(ovfDescriptor=self.descriptor, error=self.errors)


class FakeVm:
    name = "app-vm"

    def __init__(self, lease, power="poweredOff", committed=1000):
        self.lease = lease
        self.runtime = SimpleNamespace(powerState=power)
        self.summary = SimpleNamespace(storage=SimpleNamespace(committed=committed))

    def ExportVm(self):
        return self.lease


class FakeSession:
    def __init__(self, manager):
        self.manager = manager

    def RetrieveContent(self):
        return SimpleNamespace(ovfManager=self.manager)


def fake_lease():
    lease = FakeLease(["ready"])
    lease.info = SimpleNamespace(
        deviceUrl=[
            SimpleNamespace(
                key="/vm/disk1",
                url="https://*/nfc/1/disk-0.vmdk",
                disk=True,
                fileSize=9,
            ),
            SimpleNamespace(
                key="/vm/nvram", url="https://*/nfc/1/vm.nvram", disk=False, fileSize=3
            ),
        ]
    )
    return lease


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(payloads):
    seen = []

    def open_url(url):
        seen.append(url)
        return FakeResponse(payloads[url.rsplit("/", 1)[1]])

    open_url.seen = seen
    return open_url


def run_export(tmp_path, vm, opener, manager=None, **kw):
    return export.export_vm(
        FakeSession(manager or FakeOvfManager()), vm, "10.0.0.1", tmp_path / "out", "App", "Network",
        chunk_size=4, log=lambda m: None, opener=opener, **kw,
    )  # fmt: skip


def test_the_export_makes_an_ova_with_descriptor_manifest_and_disk(tmp_path):
    lease = fake_lease()
    opener = opener_for({"disk-0.vmdk": b"123456789", "vm.nvram": b"nvr"})
    ova = run_export(tmp_path, FakeVm(lease), opener)

    assert ova == tmp_path / "out" / "App.ova"
    with tarfile.open(ova) as archive:
        names = [m.name for m in archive.getmembers()]
        assert names == [
            "App.ovf",
            "App.mf",
            "App-disk0.vmdk",
        ]  # the nvram is not in the descriptor: left out
        ovf = archive.extractfile("App.ovf").read().decode()
        manifest = archive.extractfile("App.mf").read().decode()
        assert archive.extractfile("App-disk0.vmdk").read() == b"123456789"
    assert 'ovf:name="Network"' in ovf and "PG-MGMT" not in ovf
    assert (
        f"SHA256(App-disk0.vmdk)= {hashlib.sha256(b'123456789').hexdigest()}"
        in manifest
    )
    assert f"SHA256(App.ovf)= {hashlib.sha256(ovf.encode()).hexdigest()}" in manifest
    assert opener.seen == [
        "https://10.0.0.1/nfc/1/disk-0.vmdk",
        "https://10.0.0.1/nfc/1/vm.nvram",
    ]
    assert lease.completed and not lease.aborted
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "App.ova"
    ]  # no temporary files left


def test_the_descriptor_is_asked_for_with_the_downloaded_files(tmp_path):
    manager = FakeOvfManager()
    run_export(
        tmp_path,
        FakeVm(fake_lease()),
        opener_for({"disk-0.vmdk": b"123456789", "vm.nvram": b"nvr"}),
        manager,
    )
    assert manager.cdp.name == "App"
    assert [(f.deviceId, f.path, f.size) for f in manager.cdp.ovfFiles] == [
        ("/vm/disk1", "App-disk0.vmdk", 9),
        ("/vm/nvram", "vm.nvram", 3),
    ]


def test_a_failed_download_aborts_the_lease_and_leaves_nothing_behind(tmp_path):
    lease = fake_lease()

    def broken(url):
        raise OSError("connection reset")

    with pytest.raises(OSError, match="connection reset"):
        run_export(tmp_path, FakeVm(lease), broken)
    assert lease.aborted and not lease.completed
    assert list((tmp_path / "out").iterdir()) == []


def test_a_descriptor_error_aborts_the_lease(tmp_path):
    lease = fake_lease()
    manager = FakeOvfManager(
        errors=[SimpleNamespace(localizedMessage="disk not found")]
    )
    with pytest.raises(RuntimeError, match="disk not found"):
        run_export(
            tmp_path,
            FakeVm(lease),
            opener_for({"disk-0.vmdk": b"1", "vm.nvram": b"2"}),
            manager,
        )
    assert lease.aborted and list((tmp_path / "out").iterdir()) == []


def test_a_running_vm_is_not_exported(tmp_path):
    lease = fake_lease()
    with pytest.raises(RuntimeError, match="shut it down first"):
        run_export(tmp_path, FakeVm(lease, power="poweredOn"), opener_for({}))
    assert not lease.completed and not lease.aborted


def test_too_little_space_stops_before_the_lease_is_taken(tmp_path):
    lease = fake_lease()
    huge = FakeVm(lease, committed=10**18)
    with pytest.raises(OSError, match="not enough free space"):
        run_export(tmp_path, huge, opener_for({}))
    assert not lease.aborted and not lease.completed


# ---------- descriptor for the real vSphere types ----------


def test_the_descriptor_request_uses_the_vsphere_types_and_reports_server_errors():
    manager = FakeOvfManager()
    text = export.descriptor_for(
        manager, object(), "App", [("k1", "App-disk0.vmdk", 5)]
    )
    assert text == OVF
    assert manager.cdp.ovfFiles[0].deviceId == "k1"
    with pytest.raises(RuntimeError, match="boom"):
        export.descriptor_for(
            FakeOvfManager(errors=[SimpleNamespace(localizedMessage="boom")]),
            object(),
            "App",
            [],
        )


# ---------- shared helpers ----------


def test_the_password_comes_from_the_environment_only_and_a_missing_one_is_explained_without_a_value():
    assert common.password_from_env({"ESXI_PASSWORD": "geheim"}) == "geheim"
    with pytest.raises(SystemExit) as raised:
        common.password_from_env({})
    assert "ESXI_PASSWORD" in str(raised.value) and "never from an argument" in str(
        raised.value
    )
    with pytest.raises(SystemExit):
        common.password_from_env({"ESXI_PASSWORD": ""})


def test_the_scripts_take_no_password_argument():
    for module in (export, guestinfo):
        with pytest.raises(SystemExit) as raised:
            module.main(
                [
                    "--host",
                    "h",
                    "--vm",
                    "v",
                    "--out",
                    "o",
                    "--config",
                    "c",
                    "--password",
                    "x",
                ]
            )
        assert raised.value.code == 2  # argparse: unrecognised argument


def test_waiting_for_a_task_reports_the_servers_message():
    done = SimpleNamespace(info=SimpleNamespace(state="success"))
    common.wait_for_task(done)
    failed = SimpleNamespace(
        info=SimpleNamespace(state="error", error=SimpleNamespace(msg="out of disk"))
    )
    with pytest.raises(RuntimeError, match="out of disk"):
        common.wait_for_task(failed)
    states = iter(["queued", "running", "success"])

    class Moving:
        @property
        def info(self):
            return SimpleNamespace(state=next(states))

    sleeps = []
    common.wait_for_task(Moving(), sleep=sleeps.append)
    assert len(sleeps) == 2


def test_sha256_of_a_file(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc" * 1_000_000)
    assert common.sha256_of(path) == hashlib.sha256(b"abc" * 1_000_000).hexdigest()


def test_a_missing_vm_and_a_missing_password_end_the_export_cleanly(
    monkeypatch, capsys
):
    monkeypatch.delenv("ESXI_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as raised:
        export.main(["--host", "h", "--vm", "v", "--out", "o"])
    assert "ESXI_PASSWORD" in str(raised.value)
    monkeypatch.setenv("ESXI_PASSWORD", "geheim")
    monkeypatch.setattr(common, "connect", lambda *a, **k: object())
    monkeypatch.setattr(common, "find_vm", lambda *a, **k: None)
    assert export.main(["--host", "h", "--vm", "v", "--out", "o"]) == 1
    assert "no VM named v" in capsys.readouterr().err


# ---------- describe_template ----------


SHA = "90f49bb6de625960d9443b47c3d1c1e7a0d5a90a0f07902ddb91662e8a218b0f"


def test_the_store_file_name_is_the_name_and_the_capital_hash():
    assert (
        describe.store_file_name("Ginnungagap_Appliance", SHA)
        == f"Ginnungagap_Appliance_{SHA.upper()}.ova"
    )
    assert (
        describe.store_file_name(f"Ginnungagap_Appliance_{SHA}", SHA)
        == f"Ginnungagap_Appliance_{SHA.upper()}.ova"
    )


def test_the_size_is_written_like_the_store_does():
    assert (
        describe.size_text(3875076096) == "3,61GB"
    )  # the GNS3 VM's entry in the store
    assert describe.size_text(1024**3) == "1,00GB"  # GiB, two decimals, a comma


def template(**over):
    values = {
        "name": "ginnungagap-appliance",
        "category": "Appliance",
        "tags": ["appliance", "linux"],
        "os_family": "Linux",
        "os_version": "Debian 13",
        "cpu": 2,
        "memory_gb": 4,
        "disk_gb": 20,
        "users": ["junioradmin"],
        "hostname": "ginnungagap",
        "domain": None,
        "note": describe.DEFAULT_NOTE,
    }
    values.update(over)
    return describe.build_template(SHA, f"X_{SHA.upper()}.ova", 4913867264, **values)


def test_the_description_has_every_field_the_collector_requires_and_no_password():
    entry = template()
    for field in (
        "id",
        "name",
        "file",
        "category",
        "os",
        "cpu",
        "memory",
        "disk",
        "hostname",
    ):
        assert entry[field] not in (None, ""), field
    assert entry["id"] == SHA.upper() and entry["size"] == "4,58GB"
    assert "passwords" not in entry and "note" in entry


def test_the_yaml_is_in_the_stores_layout_and_reads_back():
    text = describe.render_yaml(template(note="Login: change it: before use"))
    assert text.startswith("version: 1\ntemplates:\n  - id: ")
    assert "    tags:\n      - appliance\n      - linux\n" in text
    assert "    domain: null\n" in text
    parsed = yaml.safe_load(text)
    assert parsed["templates"][0]["note"] == "Login: change it: before use"
    assert parsed["templates"][0]["os"] == {"family": "Linux", "version": "Debian 13"}


def test_describing_an_ova_renames_it_and_writes_the_yaml_next_to_it(tmp_path, capsys):
    ova = tmp_path / "Ginnungagap_Appliance.ova"
    ova.write_bytes(b"not really an ova")
    sha = hashlib.sha256(b"not really an ova").hexdigest().upper()
    assert (
        describe.main(
            [
                str(ova),
                "--rename",
                "--write",
                "--cpu",
                "4",
                "--user",
                "a",
                "--user",
                "b",
            ]
        )
        == 0
    )
    renamed = tmp_path / f"Ginnungagap_Appliance_{sha}.ova"
    assert renamed.is_file() and not ova.exists()
    written = yaml.safe_load((tmp_path / "Ginnungagap_Appliance.yml").read_text())[
        "templates"
    ][0]
    assert (
        written["file"] == renamed.name
        and written["id"] == sha
        and written["cpu"] == 4
        and written["users"] == ["a", "b"]
    )


def test_describing_without_rename_prints_and_says_what_the_store_expects(
    tmp_path, capsys
):
    ova = tmp_path / "a.ova"
    ova.write_bytes(b"x")
    assert describe.main([str(ova)]) == 0
    captured = capsys.readouterr()
    assert "version: 1" in captured.out and "use --rename" in captured.err
    assert ova.exists()
    assert describe.main([str(tmp_path / "missing.ova")]) == 1


# ---------- set_guestinfo ----------


CONFIG = b"version: 1\ngenerator:\n  hosted:\n    gemini: {api_key: secret-key-123}\n"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPfwF4f5kYEYkRaWXh9LOq0adm3V8Rl7nFapcovr1QQY lab"


def test_the_config_goes_into_the_guestinfo_key_as_base64():
    options = guestinfo.guestinfo_options(CONFIG)
    assert base64.b64decode(options["guestinfo.ginnungagap.config"]) == CONFIG
    assert options["guestinfo.ginnungagap.config.encoding"] == "base64"
    assert set(options) == {
        "guestinfo.ginnungagap.config",
        "guestinfo.ginnungagap.config.encoding",
    }


def test_hostname_and_ssh_key_are_optional_and_checked():
    options = guestinfo.guestinfo_options(
        CONFIG, ssh_key=KEY + "\n", hostname="ginn-01.lab"
    )
    assert options["guestinfo.ginnungagap.ssh_authorized_key"] == KEY
    assert options["guestinfo.ginnungagap.hostname"] == "ginn-01.lab"
    for bad_name in ("-bad", "has space", "a..b", ""):
        with pytest.raises(ValueError, match="host name"):
            guestinfo.guestinfo_options(CONFIG, hostname=bad_name)
    for bad_key in ("hello", KEY + "\nssh-rsa AAAA x", "ssh-ed25519"):
        with pytest.raises(ValueError, match="SSH key"):
            guestinfo.guestinfo_options(CONFIG, ssh_key=bad_key)


def test_an_empty_or_huge_config_is_refused():
    with pytest.raises(ValueError, match="empty"):
        guestinfo.guestinfo_options(b" \n")
    with pytest.raises(ValueError, match="too big"):
        guestinfo.guestinfo_options(b"x" * 50000)


def test_the_reconfiguration_sets_extra_config_entries():
    spec = guestinfo.config_spec({"guestinfo.a": "1", "guestinfo.b": "2"})
    assert [(o.key, o.value) for o in spec.extraConfig] == [
        ("guestinfo.a", "1"),
        ("guestinfo.b", "2"),
    ]


def test_only_the_key_names_are_printed_never_the_values(tmp_path, monkeypatch, capsys):
    config = tmp_path / "ginnungagap.yml"
    config.write_bytes(CONFIG)
    sent = {}

    class Vm:
        runtime = SimpleNamespace(powerState="poweredOff")

        def ReconfigVM_Task(self, spec):
            sent["options"] = {o.key: o.value for o in spec.extraConfig}
            return SimpleNamespace(info=SimpleNamespace(state="success"))

        def PowerOnVM_Task(self):
            sent["powered_on"] = True
            return SimpleNamespace(info=SimpleNamespace(state="success"))

    monkeypatch.setenv("ESXI_PASSWORD", "esxi-secret-pw")
    monkeypatch.setattr(common, "connect", lambda *a, **k: object())
    monkeypatch.setattr(common, "find_vm", lambda *a, **k: Vm())
    assert (
        guestinfo.main(
            [
                "--host",
                "h",
                "--vm",
                "v",
                "--config",
                str(config),
                "--power-on",
                "--hostname",
                "ginn",
            ]
        )
        == 0
    )
    out = capsys.readouterr()
    shown = out.out + out.err
    assert (
        "guestinfo.ginnungagap.config" in shown
        and "guestinfo.ginnungagap.hostname" in shown
    )
    for secret in (
        "secret-key-123",
        encode := guestinfo.encode_config(CONFIG),
        "esxi-secret-pw",
    ):
        assert secret not in shown
    assert (
        base64.b64decode(sent["options"]["guestinfo.ginnungagap.config"]) == CONFIG
        and sent["powered_on"]
    )
    assert encode  # (the encoded value is what was sent)


def test_a_config_file_that_cannot_be_used_ends_cleanly_before_any_connection(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        common, "connect", lambda *a, **k: pytest.fail("must not connect")
    )
    assert (
        guestinfo.main(
            ["--host", "h", "--vm", "v", "--config", str(tmp_path / "nope.yml")]
        )
        == 1
    )
    empty = tmp_path / "e.yml"
    empty.write_text("")
    assert guestinfo.main(["--host", "h", "--vm", "v", "--config", str(empty)]) == 1
    assert "empty" in capsys.readouterr().err


def test_the_guest_os_type_of_the_descriptor_can_be_set():
    ovf = '<OperatingSystemSection ovf:id="1" vmw:osType="otherGuest"><Info>x</Info></OperatingSystemSection>'
    assert 'vmw:osType="debian12_64Guest"' in export.rewrite_os_type(
        ovf, "debian12_64Guest"
    )
    assert 'ovf:id="1"' in export.rewrite_os_type(ovf, "debian12_64Guest")


def test_a_guest_os_type_that_could_break_the_xml_is_refused():
    with pytest.raises(ValueError):
        export.rewrite_os_type("<a/>", 'x" evil="1')


# ---------- resource settings a vCenter-managed host refuses, and correcting an OVA afterwards ----------

_OVF_WITH_SHARES = """<Envelope>
  <VirtualHardwareSection>
    <Item>
      <rasd:ElementName>4 virtual CPU(s)</rasd:ElementName>
    </Item>
    <vmw:Config ovf:required="false" vmw:key="cpuAllocation.shares.level" vmw:value="normal"/>
    <vmw:Config ovf:required="false" vmw:key="cpuAllocation.shares.shares" vmw:value="4000"/>
    <vmw:Config ovf:required="false" vmw:key="memoryAllocation.reservation" vmw:value="0"/>
    <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="bios"/>
    <vmw:Config ovf:required="false" vmw:key="nestedHVEnabled" vmw:value="false"/>
  </VirtualHardwareSection>
  <OperatingSystemSection ovf:id="1" vmw:osType="otherGuest"/>
  <NetworkSection><Network ovf:name="PG-MGMT"><Description>The PG-MGMT network</Description></Network></NetworkSection>
  <File ovf:href="disk.vmdk" ovf:id="file1" ovf:size="4"/>
</Envelope>
"""


def test_cpu_and_memory_allocation_settings_are_removed_and_everything_else_stays():
    out = export.strip_resource_settings(_OVF_WITH_SHARES)
    assert "cpuAllocation" not in out and "memoryAllocation" not in out
    assert 'vmw:key="firmware"' in out and 'vmw:key="nestedHVEnabled"' in out
    assert "4 virtual CPU(s)" in out


def _write_ova(path: Path, ovf: str) -> None:
    folder = path.parent / "src"
    folder.mkdir(exist_ok=True)
    (folder / "old.ovf").write_text(ovf, encoding="utf-8")
    (folder / "disk.vmdk").write_bytes(b"DISK")
    (folder / "old.mf").write_text("stale manifest\n", encoding="utf-8")
    export.pack_ova(path, [folder / "old.ovf", folder / "old.mf", folder / "disk.vmdk"])


def test_an_existing_ova_is_corrected_with_a_fresh_manifest_and_the_descriptor_first(
    tmp_path,
):
    source = tmp_path / "in.ova"
    _write_ova(source, _OVF_WITH_SHARES)
    target = tmp_path / "out.ova"
    export.repack_ova(
        source, target, os_type="debian12_64Guest", network_name="Network"
    )
    with tarfile.open(target) as archive:
        members = [m.name for m in archive.getmembers()]
        ovf = archive.extractfile("old.ovf").read().decode()
        manifest = archive.extractfile("old.mf").read().decode()
    assert members[0] == "old.ovf" and set(members) == {
        "old.ovf",
        "old.mf",
        "disk.vmdk",
    }
    assert (
        "cpuAllocation" not in ovf
        and 'vmw:osType="debian12_64Guest"' in ovf
        and 'ovf:name="Network"' in ovf
    )
    assert f"SHA256(disk.vmdk)= {hashlib.sha256(b'DISK').hexdigest()}" in manifest
    assert f"SHA256(old.ovf)= {hashlib.sha256(ovf.encode()).hexdigest()}" in manifest
    assert "stale" not in manifest


def test_repack_refuses_to_overwrite_its_own_source(tmp_path):
    source = tmp_path / "in.ova"
    _write_ova(source, _OVF_WITH_SHARES)
    with pytest.raises(ValueError):
        export.repack_ova(source, source)


def test_repack_from_the_command_line_needs_no_host(tmp_path, capsys):
    source = tmp_path / "in.ova"
    _write_ova(source, _OVF_WITH_SHARES)
    out = tmp_path / "out"
    out.mkdir()
    assert (
        export.main(["--repack", str(source), "--out", str(out), "--name", "Fixed"])
        == 0
    )
    assert (out / "Fixed.ova").is_file() and "sha256" in capsys.readouterr().out


def test_passwordless_sudo_is_only_asked_for_when_requested():
    guestinfo = load("set_guestinfo")
    assert (
        "guestinfo.ginnungagap.admin_sudo_nopasswd"
        not in guestinfo.guestinfo_options(b"a: 1")
    )
    asked = guestinfo.guestinfo_options(b"a: 1", sudo_nopasswd=True)
    assert asked["guestinfo.ginnungagap.admin_sudo_nopasswd"] == "true"
