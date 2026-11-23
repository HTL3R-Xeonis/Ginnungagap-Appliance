"""The image build's skeleton: names, config, ports, the phase runner (order, retry, cleanup, resume, lock, dry run, janitor)."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import stat
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml

from image.build import config as config_module
from image.build import names, pipeline, ports
from image.build.ports import (
    POWERED_ON,
    AlreadyExists,
    AlreadyRunning,
    Blocked,
    Deferred,
    DryRunViolation,
    EsxiPort,
    Interrupted,
    PermanentError,
    RemotePort,
    ShareApiPort,
    TemplateApiPort,
    TransientError,
    Unreachable,
)
from tests.helpers.fake_esxi import FakeClock, FakeEsxi

PASSWORD = "hunter2-the-esxi-password"
VERSION = "1.3.0"


# ---------- the world a test runs in ----------


@dataclass
class World:
    tmp: Path
    clock: FakeClock
    esxi: FakeEsxi
    remote: Any
    share: Any
    templates: Any
    config: config_module.BuildHostConfig
    trace: list[str] = field(default_factory=list)
    events: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def state_dir(self) -> Path:
        return self.tmp / "state"

    def registry(self, **overrides) -> pipeline.PhaseRegistry:
        registry = pipeline.PhaseRegistry()
        for spec in pipeline.PHASES:
            if spec.id in ("P15", "P16"):
                continue
            body = overrides.pop(spec.id, None) or STANDARD[spec.id]
            registry.register(
                spec.id, self._traced(spec.id, body), describe=DESCRIBE.get(spec.id)
            )
        assert not overrides, f"not a phase: {overrides}"
        return registry

    def _traced(self, pid, body):
        def run(ctx):
            self.trace.append(pid)
            return body(ctx)

        return run

    def pipeline(self, registry=None, **kwargs) -> pipeline.Pipeline:
        hooks = pipeline.PipelineHooks(
            notify=lambda event, payload: self.events.append((event, payload))
        )
        return pipeline.Pipeline(
            config=self.config,
            esxi=self.esxi,
            remote=self.remote,
            share=self.share,
            templates=self.templates,
            clock=self.clock,
            registry=registry or self.registry(),
            hooks=kwargs.pop("hooks", hooks),
            secrets=[PASSWORD],
            **kwargs,
        )

    def run(
        self, version=VERSION, registry=None, pipeline_kwargs=None, **options
    ) -> pipeline.RunOutcome:
        runner = self.pipeline(registry, **(pipeline_kwargs or {}))
        self.clock.advance(1)  # a new run id each time
        return runner.run(version, pipeline.RunOptions(**options))

    def run_dirs(self) -> list[Path]:
        base = self.state_dir / "runs"
        return sorted(base.iterdir()) if base.exists() else []

    def state(self, outcome) -> dict:
        return json.loads((outcome.run_dir / "state.json").read_text())


@pytest.fixture
def world(tmp_path) -> World:
    clock = FakeClock()
    config = config_module.parse_config(
        {
            "esxi": {"datastore": "Datastore", "network": "VM Network"},
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    share = create_autospec(ShareApiPort, instance=True)
    share.list_folders.return_value = []
    share.remove_folder.return_value = True
    templates = create_autospec(TemplateApiPort, instance=True)
    templates.list_templates.return_value = []
    esxi = FakeEsxi(clock=clock)
    esxi.add_vm("GNS3VM5", power_state=POWERED_ON)
    return World(
        tmp_path,
        clock,
        esxi,
        create_autospec(RemotePort, instance=True),
        share,
        templates,
        config,
    )


# ---------- the standard phase bodies: what the real ones will do, with the fake ----------


def p1(ctx):
    names.check_free(
        ctx.names,
        template_names=[t.name for t in ctx.templates.list_templates()],
        share_folders=ctx.share.list_folders(),
    )
    ctx.esxi.check_login()


def p3(ctx):
    return {"base": "ginnungagap-appliance-v1.2.1", "url": "http://ova.test/base.ova"}


def p4(ctx):
    vm = names.build_vm_name(ctx.run_id)
    ctx.track_vm(vm)  # BEFORE the import: an import that fails half-way is cleaned too
    ctx.esxi.import_ova(ctx.results["P3"]["url"], vm, "Datastore", ["VM Network"])
    ctx.esxi.set_annotation(vm, names.annotation(ctx.run_id))
    return {"vm": vm}


def p5(ctx):
    vm = ctx.results["P4"]["vm"]
    ctx.esxi.set_extra_config(
        vm,
        {
            "guestinfo.ginnungagap.config": "c2VjcmV0",
            "guestinfo.ginnungagap.ssh_authorized_key": "k",
        },
    )
    ctx.esxi.power_on(vm)
    return {"ip": ctx.esxi.wait_for_ip(vm, 180)}


def p9(ctx):
    vm = ctx.results["P4"]["vm"]
    ctx.esxi.shutdown_guest(vm)
    assert ctx.esxi.wait_powered_off(vm, 300)
    return {"cleared": ctx.esxi.clear_guestinfo(vm)}


def p10(ctx):
    vm = ctx.results["P4"]["vm"]
    exported = ctx.esxi.export_ova(vm, ctx.subdir("export"), ctx.names.stem)
    ctx.esxi.delete_vm(vm)
    return {"ova": exported.path, "sha256": exported.sha256}


def p12(ctx):
    ctx.share.make_folder(ctx.names.folder)
    ctx.track_share_folder(ctx.names.folder)
    return {"folder": ctx.names.folder, "ova": ctx.results["P10"]["ova"]}


def p13(ctx):
    vm = names.accept_vm_name(ctx.run_id, "a")
    ctx.track_vm(vm)
    ctx.esxi.import_ova("http://ova.test/new.ova", vm, "Datastore", ["VM Network"])
    ctx.esxi.set_annotation(vm, names.annotation(ctx.run_id))
    ctx.esxi.power_on(vm)
    return {"boots": ["a"]}


def nothing(ctx):
    return None


STANDARD = {pid: nothing for pid in pipeline.PHASE_IDS}
STANDARD.update(P1=p1, P3=p3, P4=p4, P5=p5, P9=p9, P10=p10, P12=p12, P13=p13)
DESCRIBE = {
    "P4": lambda ctx: f"import the base OVA as {names.build_vm_name(ctx.run_id)}"
}
ALL = [pid for pid in pipeline.PHASE_IDS if pid not in ("P15", "P16")]


def base_state(world) -> set[str]:
    return set(world.esxi.vm_names())


# ====================================================================================================== names


@pytest.mark.parametrize("version", ["1.3.0", "1.3.0-rc.1", "10.20.300", "0.0.1"])
def test_names_follow_the_store_rules(version):
    n = names.for_version(version)
    assert n.template == f"ginnungagap-appliance-v{version}"
    assert n.folder == n.stem == f"Ginnungagap_Appliance_v{version}"
    assert n.yml == f"Ginnungagap_Appliance_v{version}.yml"
    assert n.template.startswith(names.TEMPLATE_PREFIX)
    names.check_template_name(n.template)


@pytest.mark.parametrize(
    "bad",
    [
        "1.3.0-r2",
        "v1.3.0",
        "1.3",
        "1.3.0-rc",
        "1.3.0-beta.1",
        "1.3.0\n",
        " 1.3.0",
        "1.3.0-rc.1-r2",
        "",
        "１.３.０",
    ],
)
def test_a_revision_suffix_or_a_malformed_version_is_refused(bad):
    with pytest.raises(names.InvalidVersion):
        names.for_version(bad)


def test_the_store_file_name_is_the_stem_and_the_capital_hash():
    sha = "ab" * 32
    file = names.for_version(VERSION).ova(sha)
    assert file == f"Ginnungagap_Appliance_v1.3.0_{'AB' * 32}.ova"
    assert names.parse_store_file_name(file) == (
        "Ginnungagap_Appliance_v1.3.0",
        "AB" * 32,
    )
    assert names.parse_store_file_name("Ginnungagap_Appliance_v1.3.0.ova") is None
    with pytest.raises(ValueError):
        names.store_file_name("x", "abc")


def test_run_ids_vm_names_and_annotation_round_trip(world):
    run = names.run_id("1.3.0-rc.1", world.clock.now())
    assert run == "1.3.0-rc.1-20261120T100000Z"
    assert names.parse_run_id(run) == ("1.3.0-rc.1", "20261120T100000Z")
    assert names.build_vm_name(run) == "imgbuild-v1.3.0-rc.1-20261120T100000Z"
    assert names.accept_vm_name(run, "b") == "imgaccept-v1.3.0-rc.1-20261120T100000Z-b"
    assert names.parse_annotation(names.annotation(run)) == run
    assert names.parse_annotation("hello\n" + names.annotation(run)) == run
    assert names.parse_annotation("ginnungagap-image-build run=../../etc") is None
    assert names.parse_annotation(None) is None
    with pytest.raises(ValueError):
        names.accept_vm_name(run, "z")
    with pytest.raises(ValueError):
        names.build_vm_name("not-a-run")


def test_only_the_two_prefixes_are_ours():
    for ours in [
        "imgbuild-v1.3.0-20261120T100000Z",
        "imgaccept-v1-x-a",
        "imgbuild-v1.3.0-x_1",
    ]:
        assert names.is_ours_vm(ours)
    for other in [
        "GNS3VM5",
        "imgbuild",
        "imgbuild-",
        "imgbuilder-1",
        "my-imgbuild-1",
        "ginnungagap-appliance-v1.3.0",
        "IMGBUILD-x",
        "",
    ]:
        assert not names.is_ours_vm(other), other
    assert names.is_ours_folder("imgbuild-v1.3.0-x_2")
    assert not names.is_ours_folder("Ginnungagap_Appliance_v1.3.0")


def test_never_overwrite_refuses_every_taken_name():
    n = names.for_version(VERSION)
    names.check_free(
        n,
        template_names=["ginnungagap-appliance-v1.2.1"],
        share_folders=["Ginnungagap_Appliance_v1.2.1"],
    )
    with pytest.raises(AlreadyExists, match="already in the store"):
        names.check_free(n, template_names=[n.template])
    with pytest.raises(AlreadyExists, match="share"):
        names.check_free(n, share_folders=[n.folder])
    run = "1.3.0-20261120T100000Z"
    with pytest.raises(AlreadyExists, match="imgaccept"):
        names.check_free(n, vm_names=[names.accept_vm_name(run, "c")], run=run)


def test_the_ova_url_encodes_every_part_like_the_importer():
    url = names.ova_url(
        "http://10.20.20.181:80/", "Ginnungagap_Appliance_v1.3.0", "a b_AB.ova"
    )
    assert url == "http://10.20.20.181:80/Ginnungagap_Appliance_v1.3.0/a%20b_AB.ova"
    assert names.ova_url("http://h", "", "f.ova") == "http://h/f.ova"


# ====================================================================================================== config

MINIMAL = {"esxi": {"datastore": "ds", "network": "net"}}


def test_the_minimal_config_gets_the_plan_defaults():
    c = config_module.parse_config(MINIMAL)
    assert (c.esxi.host, c.esxi.allowed_hosts, c.esxi.allowed_running_vms) == (
        "10.20.20.205",
        ("10.20.20.205",),
        ("GNS3VM5",),
    )
    assert (c.github.repo, c.github.channel, c.poll_minutes) == (
        "HTL3R-Xeonis/Ginnungagap-Appliance",
        "stable",
        15,
    )
    assert (c.template_api, c.ova_base_url) == (
        "http://10.20.20.171:8000",
        "http://10.20.20.181:80",
    )
    assert (c.share.mode, str(c.share.path)) == ("nfs", "/mnt/esxi_templates")
    assert (
        c.image.cpu,
        c.image.memory_gb,
        c.image.disk_gb,
        c.image.install_method,
    ) == (4, 4, 20, "update")
    assert (
        c.acceptance.upgrade_test,
        c.acceptance.extra_hosts,
        c.keep_failed,
        c.notify.command,
    ) == (True, (), False, "")
    assert (
        c.limits.build_disk_free_gb,
        c.limits.share_free_gb,
        c.limits.datastore_free_gb,
    ) == (25, 10, 50)
    assert (
        str(c.esxi.password_file)
        == "/etc/ginnungagap-image-build/secrets/esxi-205.password"
    )


def test_datastore_and_network_are_the_only_required_keys():
    with pytest.raises(config_module.ConfigError) as info:
        config_module.parse_config({})
    assert info.value.problems == [
        "esxi.datastore is required",
        "esxi.network is required",
    ]


def test_an_unknown_key_is_an_error_at_every_level():
    for doc in (
        {**MINIMAL, "pol_minutes": 5},
        {"esxi": {**MINIMAL["esxi"], "pasword_file": "/x"}},
        {**MINIMAL, "github": {"repo": "a/b", "tokenfile": "/x"}},
        {**MINIMAL, "limits": {"max_attemps": 3}},
    ):
        with pytest.raises(config_module.ConfigError, match="unknown key"):
            config_module.parse_config(doc)


def test_every_problem_is_reported_at_once():
    with pytest.raises(config_module.ConfigError) as info:
        config_module.parse_config(
            {
                "poll_minutes": 0,
                "keep_failed": "yes",
                "esxi": {"datastore": "ds", "network": "net", "host": "10.0.0.1"},
                "template_api": "http://user:pw@x:8000",
                "ova_base_url": "ftp://x",
                "share": {"mode": "ftp"},
                "extra": 1,
            }
        )
    text = "\n".join(info.value.problems)
    for expected in (
        "poll_minutes",
        "keep_failed",
        "esxi.host 10.0.0.1 is not in esxi.allowed_hosts",
        "template_api",
        "ova_base_url",
        "share.mode",
        "unknown key extra",
    ):
        assert expected in text


@pytest.mark.parametrize(
    "doc",
    [
        {"poll_minutes": True},
        {"poll_minutes": "15"},
        {"image": {"cpu": 2.5}},
        {"acceptance": {"upgrade_test": "true"}},
        {
            "esxi": {
                "datastore": "ds",
                "network": "net",
                "allowed_hosts": "10.20.20.205",
            }
        },
        {
            "esxi": {
                "datastore": "ds",
                "network": "net",
                "password_file": "relative/path",
            }
        },
        {"esxi": {"datastore": "ds", "network": "net", "password_file": "/etc/../x"}},
        {"notify": {"command": "relative.sh"}},
        {"github": {"repo": "no-slash"}},
        {"github": {"channel": "nightly"}},
        {"image": {"install_method": "magic"}},
        {"share": {"mode": "ssh"}},
        {"limits": {"orphan_age_hours": -1}},
        "just text",
    ],
)
def test_wrong_types_and_values_are_errors(doc):
    base = {"esxi": {"datastore": "ds", "network": "net"}}
    if isinstance(doc, dict):
        merged = {
            **base,
            **{
                k: (
                    {**base.get(k, {}), **v} if isinstance(v, dict) and k in base else v
                )
                for k, v in doc.items()
            },
        }
    else:
        merged = doc
    with pytest.raises(config_module.ConfigError):
        config_module.parse_config(merged)


def test_ssh_share_mode_needs_its_host_user_and_key_file():
    ok = config_module.parse_config(
        {
            **MINIMAL,
            "share": {
                "mode": "ssh",
                "ssh_host": "10.20.20.172",
                "ssh_user": "imgbuild",
                "ssh_key_file": "/etc/x/share.key",
            },
        }
    )
    assert ok.share.ssh_host == "10.20.20.172" and ok.share.mode == "ssh"
    with pytest.raises(config_module.ConfigError) as info:
        config_module.parse_config({**MINIMAL, "share": {"mode": "ssh"}})
    assert len(info.value.problems) == 3


def test_urls_lose_their_trailing_slash():
    c = config_module.parse_config(
        {
            **MINIMAL,
            "ova_base_url": "http://10.20.20.181/",
            "template_api": "https://t.test:8000/",
        }
    )
    assert (c.ova_base_url, c.template_api) == (
        "http://10.20.20.181",
        "https://t.test:8000",
    )


def write_file(path: Path, text: str, mode: int) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def config_file(tmp_path, password_file: Path, **extra) -> Path:
    doc = {
        "esxi": {
            "datastore": "ds",
            "network": "net",
            "password_file": str(password_file),
        },
        **extra,
    }
    return write_file(tmp_path / "build-host.yml", yaml.safe_dump(doc), 0o644)


def test_load_config_reads_the_file_and_checks_the_secret_mode(tmp_path):
    secret = write_file(tmp_path / "esxi.password", PASSWORD + "\n", 0o600)
    cfg = config_module.load_config(config_file(tmp_path, secret))
    assert cfg.esxi.password_file == secret
    assert config_module.read_secret(secret) == PASSWORD  # without the final newline


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_secret_readable_by_others_is_refused(tmp_path, mode):
    secret = write_file(tmp_path / "esxi.password", PASSWORD, mode)
    with pytest.raises(config_module.ConfigError, match="chmod 600") as info:
        config_module.load_config(config_file(tmp_path, secret))
    assert PASSWORD not in str(info.value)
    with pytest.raises(config_module.SecretFileError):
        config_module.read_secret(secret)


def test_a_missing_empty_or_linked_secret_is_refused(tmp_path):
    with pytest.raises(config_module.ConfigError, match="does not exist"):
        config_module.load_config(config_file(tmp_path, tmp_path / "nope"))
    empty = write_file(tmp_path / "empty", "", 0o600)
    with pytest.raises(config_module.ConfigError, match="empty"):
        config_module.load_config(config_file(tmp_path, empty))
    real = write_file(tmp_path / "real", PASSWORD, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(config_module.ConfigError, match="not a plain file"):
        config_module.load_config(config_file(tmp_path, link))


def test_the_optional_token_is_checked_only_when_it_exists(tmp_path):
    secret = write_file(tmp_path / "esxi.password", PASSWORD, 0o600)
    cfg_path = config_file(
        tmp_path, secret, github={"token_file": str(tmp_path / "token")}
    )
    config_module.load_config(cfg_path)  # no token file: the normal case
    write_file(tmp_path / "token", "ghp_" + "a" * 30, 0o644)
    with pytest.raises(config_module.ConfigError, match="token"):
        config_module.load_config(cfg_path)
    (tmp_path / "token").chmod(0o600)
    assert config_module.load_config(cfg_path).github.token_file == tmp_path / "token"


def test_a_systemd_credential_wins_over_the_configured_path(tmp_path):
    configured = write_file(tmp_path / "configured", "from-the-path", 0o600)
    creds = tmp_path / "creds"
    creds.mkdir()
    write_file(creds / "esxi205", "from-the-credential", 0o400)
    env = {"CREDENTIALS_DIRECTORY": str(creds)}
    assert (
        config_module.read_secret(configured, credential_name="esxi205", environ=env)
        == "from-the-credential"
    )
    assert (
        config_module.read_secret(configured, credential_name="esxi205", environ={})
        == "from-the-path"
    )
    # a root-only file the service user cannot see is fine when the credential exists
    hidden = tmp_path / "hidden"
    assert (
        config_module.load_config(
            config_file(tmp_path, hidden), environ=env
        ).esxi.password_file
        == hidden
    )


def test_load_config_reports_a_missing_or_broken_file(tmp_path):
    with pytest.raises(config_module.ConfigError, match="can't be read"):
        config_module.load_config(tmp_path / "absent.yml")
    broken = write_file(tmp_path / "b.yml", "esxi: [unclosed", 0o644)
    with pytest.raises(config_module.ConfigError, match="not valid YAML"):
        config_module.load_config(broken, check_secrets=False)


def test_the_config_holds_no_secret_value():
    fields = {f for f in config_module.EsxiConfig.__dataclass_fields__}
    assert "password" not in fields and "password_file" in fields


# ====================================================================================================== ports


def test_every_port_method_is_classified_read_only_or_mutating_exactly_once():
    for protocol, (read_only, mutating) in ports.PORT_CLASSIFICATION.items():
        members = {
            n
            for n in vars(protocol)
            if not n.startswith("_")
            and (
                callable(getattr(protocol, n))
                or isinstance(getattr(protocol, n), property)
            )
        }
        assert read_only | mutating == members, protocol.__name__
        assert not read_only & mutating, protocol.__name__


def test_fake_esxi_has_every_method_of_the_port_with_the_same_parameters():
    fake = FakeEsxi()
    assert isinstance(fake, EsxiPort)
    for name in ports.ESXI_READ_ONLY | ports.ESXI_MUTATING:
        wanted = getattr(EsxiPort, name)
        got = getattr(type(fake), name)
        if isinstance(wanted, property):
            continue
        assert list(inspect.signature(wanted).parameters) == list(
            inspect.signature(got).parameters
        ), name


def test_the_error_classes_carry_the_retry_decision():
    assert (
        TransientError.transient
        and ports.Unreachable.transient
        and ports.Timeout.transient
        and ports.ToolsUnavailable.transient
    )
    for permanent in (
        PermanentError,
        ports.AuthFailed,
        ports.NotFound,
        AlreadyExists,
        ports.NotOurs,
        ports.CommandFailed,
        Blocked,
        DryRunViolation,
    ):
        assert not permanent.transient
    assert issubclass(AlreadyRunning, Deferred) and not issubclass(
        Deferred, PermanentError
    )
    assert not issubclass(Interrupted, Exception)
    failed = ports.CommandFailed(["docker", "ps"], 125, stderr="boom")
    assert (
        failed.exit_code == 125
        and "docker ps" in failed.message
        and failed.detail == "boom"
    )


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (TransientError("x"), "transient"),
        (Unreachable("x"), "transient"),
        (PermanentError("x"), "permanent"),
        (Blocked("sig"), "blocked"),
        (Deferred("busy"), "deferred"),
        (AlreadyRunning("x"), "deferred"),
        (Interrupted(15), "interrupted"),
        (TimeoutError("t"), "transient"),
        (ConnectionResetError("r"), "transient"),
        (RuntimeError("bug"), "permanent"),
        (KeyError("k"), "permanent"),
        (FileNotFoundError("f"), "permanent"),
    ],
)
def test_classify(exc, kind):
    assert pipeline.classify(exc).kind == kind


def test_a_read_only_view_refuses_what_changes_something():
    esxi = FakeEsxi()
    view = ports.read_only(esxi, ports.ESXI_READ_ONLY, "esxi")
    assert view.list_vms() == [] and view.host == "10.20.20.205"
    with pytest.raises(DryRunViolation, match="import_ova"):
        view.import_ova("http://x/a.ova", "v", "Datastore", [])
    with pytest.raises(DryRunViolation):
        view.vms = {}
    assert esxi.mutating_calls() == []


# ====================================================================================================== fake esxi


def test_fake_esxi_models_import_power_guestinfo_shutdown_and_delete():
    esxi = FakeEsxi()
    info = esxi.import_ova(
        "http://ova.test/a%20b.ova", "imgbuild-x", "Datastore", ["VM Network"]
    )
    assert info.folder == "imgbuild-x" and not info.powered_on
    assert esxi.imports[0].url == "http://ova.test/a%20b.ova"
    esxi.set_extra_config(
        "imgbuild-x", {"guestinfo.ginnungagap.config": "TOPSECRET", "other": "1"}
    )
    assert "TOPSECRET" not in repr(
        esxi.calls
    )  # the call log never holds guestinfo values
    esxi.power_on("imgbuild-x")
    ip = esxi.wait_for_ip("imgbuild-x", 60)
    assert ip.startswith("10.20.20.") and esxi.screenshot("imgbuild-x").startswith(
        b"\x89PNG"
    )
    esxi.shutdown_guest("imgbuild-x")
    assert esxi.wait_powered_off("imgbuild-x", 60)
    assert esxi.clear_guestinfo("imgbuild-x") == ["guestinfo.ginnungagap.config"]
    assert esxi.get_extra_config("imgbuild-x") == {"other": "1"}
    assert (
        esxi.delete_vm("imgbuild-x") is True and esxi.delete_vm("imgbuild-x") is False
    )
    assert esxi.list_datastore_folders("Datastore") == []


def test_fake_esxi_picks_a_numbered_folder_when_the_name_is_taken_and_refuses_to_delete_a_used_one():
    esxi = FakeEsxi()
    esxi.add_folder("imgbuild-x")
    info = esxi.import_ova("u", "imgbuild-x", "Datastore", ["VM Network"])
    assert info.folder == "imgbuild-x_1"
    with pytest.raises(PermanentError):
        esxi.delete_datastore_folder("Datastore", "imgbuild-x_1")
    esxi.delete_datastore_folder("Datastore", "imgbuild-x")
    esxi.delete_datastore_folder("Datastore", "imgbuild-x")  # gone is fine


def test_fake_esxi_fault_injection_skip_times_match_and_side_effect():
    esxi = FakeEsxi()
    esxi.add_vm("a")
    esxi.inject("power_on", Unreachable("down"), skip=1, times=2)
    esxi.power_on("a")
    for _ in range(2):
        with pytest.raises(Unreachable):
            esxi.power_on("a")
    esxi.power_on("a")
    esxi.inject(
        "import_ova",
        TransientError("lease"),
        side_effect=lambda: esxi.add_folder("imgbuild-orphan"),
    )
    with pytest.raises(TransientError):
        esxi.import_ova("u", "imgbuild-q", "Datastore", [])
    assert "imgbuild-orphan" in esxi.list_datastore_folders("Datastore")
    assert esxi.calls_named("import_ova")[0].outcome == "TransientError"


def test_fake_esxi_waits_cost_fake_time_and_can_fail():
    clock = FakeClock()
    esxi = FakeEsxi(clock=clock)
    esxi.add_vm("a", power_state=POWERED_ON, ip="10.0.0.5").shutdown_hangs = True
    esxi.shutdown_guest("a")
    before = clock.monotonic()
    assert (
        esxi.wait_powered_off("a", 300) is False and clock.monotonic() - before == 300
    )
    esxi.add_vm("b", power_state=POWERED_ON).never_gets_ip = True
    esxi.vm("b").ip = None
    with pytest.raises(ports.Timeout):
        esxi.wait_for_ip("b", 60)
    esxi.add_vm("off")
    with pytest.raises(ports.ToolsUnavailable):
        esxi.shutdown_guest("off")


# ====================================================================================================== phase runner: happy path


def test_every_phase_runs_in_order_and_the_run_is_recorded(world):
    before = base_state(world)
    outcome = world.run()
    assert outcome.status == "published" and outcome.exit_code == 0 and outcome.ok
    assert world.trace == ALL
    state = world.state(outcome)
    assert (
        state["status"] == "published"
        and state["schema"] == 1
        and state["failure"] is None
    )
    assert [state["phases"][p]["status"] for p in pipeline.PHASE_IDS] == [
        "ok"
    ] * 17  # P16 too: it runs once, at the end
    assert state["phases"]["P15"]["result"]["leftover_vms"] == []
    assert state["phases"]["P4"]["result"] == {
        "vm": names.build_vm_name(outcome.run_id)
    }
    assert outcome.run_dir.name == outcome.run_id == "1.3.0-20261120T100001Z"
    assert (
        base_state(world) == before
    )  # the host is as we found it: base and accept VM are gone
    assert sorted(p.name for p in outcome.run_dir.glob("*.log")) == sorted(
        ["run.log"]
        + [
            f"{i:02d}-{s.name}.log"
            for i, s in enumerate(pipeline.PHASES)
            if s.id != "P16"
        ]
    )
    world.share.remove_folder.assert_not_called()  # the staged folder is the product of a success
    assert [r["status"] for r in state["resources"] if r["kind"] == "share_folder"] == [
        "final"
    ]
    summary = json.loads((outcome.run_dir / "summary.json").read_text())
    assert summary["result"] == "published" and summary["published"] is True
    assert summary["phases"]["P8"]["status"] == "ok" and summary["attempts"] == 1
    assert [e for e, _ in world.events] == ["started", "succeeded"]


def test_the_phase_results_are_stored_as_json_and_visible_to_later_phases(world):
    seen = {}

    def p6(ctx):
        seen.update(
            ctx.results["P10"] if "P10" in ctx.results else {"p4": ctx.results["P4"]}
        )
        return {"path": Path("/tmp/x"), "when": ctx.clock.now(), "set": {"b", "a"}}

    outcome = world.run(registry=world.registry(P6=p6))
    assert seen == {"p4": {"vm": names.build_vm_name(outcome.run_id)}}
    stored = world.state(outcome)["phases"]["P6"]["result"]
    assert stored["path"] == "/tmp/x" and stored["set"] == ["a", "b"]
    assert stored["when"].startswith("2026-11-20T10:") and stored["when"].endswith("Z")


def test_a_phase_that_returns_something_else_than_a_mapping_fails_the_run(world):
    outcome = world.run(registry=world.registry(P6=lambda ctx: "oops"))
    assert outcome.status == "failed" and "must return a mapping" in outcome.reason


def test_the_registry_refuses_unknown_and_double_registration():
    registry = pipeline.PhaseRegistry()
    registry.register("P7", nothing)
    with pytest.raises(pipeline.PipelineError, match="already"):
        registry.register("P7", nothing)
    registry.register("P7", nothing, replace=True)
    with pytest.raises(pipeline.PipelineError, match="not a phase"):
        registry.register("P17", nothing)

    @registry.phase("P8")
    def decorated(ctx):
        return None

    assert registry.registered() == ["P7", "P8"]
    assert registry.missing("P9") == ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P9"]
    assert registry.missing("P16") == [
        p for p in pipeline.PHASE_IDS[:15] if p not in ("P7", "P8")
    ]


def test_a_run_needs_every_phase_up_to_its_last_one(world):
    registry = pipeline.PhaseRegistry()
    for pid in ("P0", "P1", "P2", "P3"):
        registry.register(pid, nothing)
    outcome = world.run(registry=registry)
    assert (
        outcome.status == "blocked"
        and "P4 (deploy_base)" in outcome.reason
        and outcome.run_dir is None
    )
    assert world.run_dirs() == [] and world.esxi.calls == []
    assert world.run(registry=registry, stop_after="P3").status == "stopped"
    assert (
        world.run(registry=registry, dry_run=True).status == "dry-run"
    )  # a dry run lists the rest as not implemented


def test_a_bad_version_or_stop_after_is_blocked_before_anything_is_created(world):
    assert world.run("1.3.0-r2").status == "blocked"
    assert world.run(stop_after="P99").status == "blocked"
    assert world.run_dirs() == [] and world.esxi.calls == []


# ====================================================================================================== retry


def flaky(
    pid_trace, fail_times, error=lambda: TransientError("the lease expired"), then=None
):
    state = {"n": 0}

    def body(ctx):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise error()
        return then(ctx) if then else None

    return body


def test_a_transient_failure_is_retried_once_from_the_base_vm_with_a_clean_host(world):
    before = base_state(world)
    outcome = world.run(registry=world.registry(P7=flaky(world.trace, 1)))
    assert outcome.status == "published"
    assert world.trace == ALL[:8] + ALL[4:]  # P0-P7 fail at P7, then P4-P14 again
    state = world.state(outcome)
    assert state["attempt"] == 2 and outcome.summary.attempts == 2
    assert [e["event"] for e in state["events"]].count("retry") == 1
    imports = world.esxi.imports
    assert (
        len(imports) == 3 and imports[0].vm_name == imports[1].vm_name
    )  # a fresh import of the base VM
    names_called = world.esxi.names_called()
    first_delete = names_called.index("delete_vm")
    assert (
        first_delete < [i for i, n in enumerate(names_called) if n == "import_ova"][1]
    )  # the old base VM was deleted before the new import
    assert (
        world.clock.sleeps[-1] == pipeline.RETRY_BACKOFF_SECONDS
        or pipeline.RETRY_BACKOFF_SECONDS in world.clock.sleeps
    )
    assert base_state(world) == before
    assert (
        outcome.run_dir / "diag"
    ).exists() is False  # no diagnostics registered, none written


def test_a_transient_failure_before_the_base_vm_repeats_only_that_phase(world):
    outcome = world.run(registry=world.registry(P2=flaky(world.trace, 1)))
    assert outcome.status == "published"
    assert world.trace[:5] == ["P0", "P1", "P2", "P2", "P3"]
    assert world.trace.count("P4") == 1


def test_a_builtin_network_error_is_transient_too(world):
    outcome = world.run(
        registry=world.registry(
            P6=flaky(world.trace, 1, error=lambda: ConnectionResetError("reset"))
        )
    )
    assert outcome.status == "published" and world.trace.count("P6") == 2


def test_a_transient_failure_that_keeps_coming_ends_after_the_attempt_limit(world):
    before = base_state(world)
    outcome = world.run(registry=world.registry(P7=flaky(world.trace, 99)))
    assert (
        outcome.status == "failed"
        and outcome.exit_code == 1
        and outcome.failed_phase == "P7"
    )
    assert world.trace.count("P7") == 2 and world.trace.count("P4") == 2
    state = world.state(outcome)
    assert (
        state["failure"]["kind"] == "transient"
        and state["phases"]["P7"]["status"] == "failed"
    )
    assert base_state(world) == before
    assert [e for e, _ in world.events] == ["started", "failed"]


@pytest.mark.parametrize(
    "error",
    [
        PermanentError("the signature is wrong"),
        ports.AuthFailed("no"),
        AlreadyExists("taken"),
        RuntimeError("a bug"),
        KeyError("k"),
        ValueError("v"),
    ],
)
def test_a_permanent_failure_is_never_retried_and_leaves_nothing(world, error):
    before = base_state(world)
    outcome = world.run(
        registry=world.registry(P7=flaky(world.trace, 99, error=lambda: error))
    )
    assert (
        outcome.status == "failed"
        and world.trace.count("P7") == 1
        and world.trace.count("P4") == 1
    )
    assert world.state(outcome)["failure"]["kind"] == "permanent"
    assert base_state(world) == before


def test_an_unexpected_exception_logs_its_traceback_but_a_build_error_does_not(world):
    outcome = world.run(
        registry=world.registry(
            P7=flaky(world.trace, 99, error=lambda: RuntimeError("a bug"))
        )
    )
    assert "Traceback" in (outcome.run_dir / "run.log").read_text()
    outcome = world.run(
        registry=world.registry(
            P7=flaky(world.trace, 99, error=lambda: PermanentError("expected"))
        )
    )
    assert "Traceback" not in (outcome.run_dir / "run.log").read_text()


def test_blocked_and_deferred_phases_end_the_run_with_their_own_status(world):
    blocked = world.run(
        registry=world.registry(
            P2=flaky(world.trace, 99, error=lambda: Blocked("no signature"))
        )
    )
    assert (blocked.status, blocked.exit_code, blocked.reason) == (
        "blocked",
        2,
        "no signature",
    )
    assert world.trace.count("P2") == 1
    deferred = world.run(
        registry=world.registry(
            P1=flaky(world.trace, 99, error=lambda: Deferred("the share is full"))
        )
    )
    assert (deferred.status, deferred.exit_code) == ("deferred", 3)
    assert [e for e, _ in world.events][-2:] == ["started", "deferred"] or world.events[
        -1
    ][0] == "deferred"


# ====================================================================================================== cleanup


def test_a_failure_in_acceptance_removes_the_vms_and_the_staged_share_folder(world):
    before = base_state(world)

    def broken_acceptance(ctx):
        p13(ctx)
        raise PermanentError("the wizard did not answer")

    outcome = world.run(registry=world.registry(P13=broken_acceptance))
    assert outcome.status == "failed" and outcome.failed_phase == "P13"
    assert base_state(world) == before
    world.share.remove_folder.assert_called_once_with(
        names.for_version(VERSION).folder,
        marker_name=names.RUN_MARKER,
        marker_text=outcome.run_id,
    )
    states = {
        r["kind"] + ":" + r["ref"]: r["status"]
        for r in world.state(outcome)["resources"]
    }
    assert set(states.values()) == {"cleaned"}


def test_every_phase_failing_once_leaves_the_host_as_it_was(world):
    before = base_state(world)
    for pid in ALL:
        world.share.reset_mock()
        outcome = world.run(
            registry=world.registry(
                **{pid: flaky(world.trace, 99, error=lambda: PermanentError("boom"))}
            )
        )
        assert outcome.status == "failed" and outcome.failed_phase == pid, pid
        assert base_state(world) == before, pid
        assert world.esxi.list_datastore_folders("Datastore") == ["GNS3VM5"], pid
        if (
            pid in ("P13", "P14")
        ):  # the folder exists (and is ours) from P12 on; a P12 that fails first created nothing
            world.share.remove_folder.assert_called_once()
        else:
            world.share.remove_folder.assert_not_called()


def test_an_exception_right_after_the_import_cannot_leak_the_vm(world):
    # the VM exists but the phase died before it could do anything else: tracked before the import, so it is cleaned
    def import_then_die(ctx):
        vm = names.build_vm_name(ctx.run_id)
        ctx.track_vm(vm)
        ctx.esxi.import_ova("u", vm, "Datastore", ["VM Network"])
        raise PermanentError("died")

    outcome = world.run(registry=world.registry(P4=import_then_die))
    assert outcome.status == "failed" and base_state(world) == {"GNS3VM5"}


def test_sigterm_during_a_phase_cleans_up_and_ends_as_interrupted(world):
    before = base_state(world)
    old = signal.getsignal(signal.SIGTERM)

    def killed(ctx):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("not reached: the handler raises")

    outcome = world.run(registry=world.registry(P8=killed))
    assert (outcome.status, outcome.exit_code, outcome.failed_phase) == (
        "interrupted",
        4,
        "P8",
    )
    assert base_state(world) == before
    state = world.state(outcome)
    assert (
        state["status"] == "interrupted" and state["phases"]["P8"]["status"] == "failed"
    )
    assert signal.getsignal(signal.SIGTERM) is old  # the handler is gone again
    assert world.trace[-1] == "P8"  # nothing ran afterwards


def test_sigint_and_an_interrupted_exception_are_handled_the_same_way(world):
    outcome = world.run(
        registry=world.registry(P8=lambda ctx: os.kill(os.getpid(), signal.SIGINT))
    )
    assert outcome.status == "interrupted"
    outcome = world.run(
        registry=world.registry(P8=flaky(world.trace, 1, error=lambda: Interrupted(15)))
    )
    assert outcome.status == "interrupted" and base_state(world) == {"GNS3VM5"}


def test_a_second_signal_during_the_cleanup_does_not_stop_it(world):
    def killed(ctx):
        # registered after the VM, so it runs FIRST in the cleanup and sends the second signal
        ctx.on_cleanup(
            "test", "second-signal", lambda: os.kill(os.getpid(), signal.SIGTERM)
        )
        os.kill(os.getpid(), signal.SIGTERM)

    outcome = world.run(registry=world.registry(P8=killed))
    assert outcome.status == "interrupted" and base_state(world) == {"GNS3VM5"}
    assert "ignored" in (outcome.run_dir / "run.log").read_text()


def test_a_failing_cleanup_is_retried_logged_and_left_to_the_janitor(world):
    outcome_failed = world.run  # readability
    world.esxi.inject("delete_vm", Unreachable("esxi went away"), times=2)
    outcome = outcome_failed(
        registry=world.registry(
            P7=flaky(world.trace, 99, error=lambda: PermanentError("boom"))
        )
    )
    assert outcome.status == "failed"
    assert any("cleanup of vm" in w for w in outcome.summary.cleanup_warnings)
    resource = world.state(outcome)["resources"][0]
    assert resource["status"] == "failed" and "esxi went away" in resource["error"]
    assert names.build_vm_name(outcome.run_id) in base_state(world)
    # the next start of the service: the janitor finishes the job
    report = world.pipeline().run_janitor()
    assert report.deleted_vms == [names.build_vm_name(outcome.run_id)] and base_state(
        world
    ) == {"GNS3VM5"}


def test_diagnostics_are_saved_before_the_cleanup_and_only_on_failure(world):
    def with_diag(ctx):
        vm = ctx.results["P4"]["vm"]
        ctx.add_diagnostic(
            "screenshot",
            lambda folder: (folder / "console.png").write_bytes(
                ctx.esxi.screenshot(vm)
            ),
        )
        ctx.add_diagnostic("broken", lambda folder: 1 / 0)

    ok = world.run(registry=world.registry(P6=with_diag))
    assert ok.status == "published" and not (ok.run_dir / "diag").exists()

    def die(ctx):
        with_diag(ctx)
        ctx.esxi.power_on(ctx.results["P4"]["vm"])
        raise PermanentError("boom")

    bad = world.run(registry=world.registry(P6=die))
    assert (
        (bad.run_dir / "diag" / "final" / "console.png")
        .read_bytes()
        .startswith(b"\x89PNG")
    )  # taken while the VM still existed
    assert "diagnostic broken failed" in (bad.run_dir / "run.log").read_text()


def test_track_path_deletes_inside_the_state_dir_and_refuses_outside(world):
    outside = world.tmp / "elsewhere"
    outside.mkdir()

    def paths(ctx):
        keys = ctx.subdir("key")
        (keys / "id").write_text("PRIVATE")
        ctx.track_path(keys)
        with pytest.raises(PermanentError, match="not below"):
            ctx.track_path(outside)
        with pytest.raises(PermanentError, match="not below"):
            ctx.track_path(ctx.state_dir)
        return {"key": keys}

    outcome = world.run(registry=world.registry(P6=paths))
    assert (
        outcome.status == "published"
        and not (outcome.run_dir / "key").exists()
        and outside.exists()
    )


def test_a_failure_after_the_catalog_entry_is_only_a_warning(world):
    def bad_publish_followup(ctx):
        raise PermanentError("cleanup helper broke")

    registry = world.registry()
    registry.register("P15", bad_publish_followup)
    outcome = world.run(registry=registry)
    assert outcome.status == "published" and outcome.exit_code == 0
    assert any("after the image was published" in w for w in outcome.summary.warnings)
    state = world.state(outcome)
    assert state["phases"]["P15"]["status"] == "warning" and state["failure"] is None
    assert base_state(world) == {"GNS3VM5"}  # the safety net cleaned anyway


def test_a_signal_after_the_catalog_entry_never_removes_the_published_folder(world):
    registry = world.registry()
    registry.register("P15", lambda ctx: os.kill(os.getpid(), signal.SIGTERM))
    outcome = world.run(registry=registry)
    assert outcome.status == "published" and any(
        "after publishing" in w for w in outcome.summary.warnings
    )
    world.share.remove_folder.assert_not_called()
    assert [
        r["status"]
        for r in world.state(outcome)["resources"]
        if r["kind"] == "share_folder"
    ] == ["final"]
    assert base_state(world) == {"GNS3VM5"}


def test_a_crash_after_the_catalog_entry_is_not_undone_by_the_janitor(world):
    # P14 is done (the folder is `final` in state.json) but the process died before the status was written
    outcome = world.run()
    path = outcome.run_dir / "state.json"
    state = json.loads(path.read_text())
    state["status"] = "running"
    path.write_text(json.dumps(state))
    world.share.reset_mock()
    report = world.pipeline().run_janitor()
    world.share.remove_folder.assert_not_called()
    assert (
        report.crashed_runs == [outcome.run_id] and report.deleted_share_folders == []
    )


def test_a_failing_summary_phase_never_changes_the_result(world):
    seen = []
    registry = world.registry()
    registry.register("P16", lambda ctx: seen.append(ctx.summary.result) or 1 / 0)
    outcome = world.run(registry=registry)
    assert outcome.status == "published" and seen == ["published"]
    assert any("P16 summary failed" in w for w in outcome.summary.warnings)


def test_the_summary_phase_also_runs_for_a_failed_run_and_sees_the_result(world):
    seen = []
    registry = world.registry(
        P7=flaky(world.trace, 99, error=lambda: PermanentError("boom"))
    )
    registry.register(
        "P16", lambda ctx: seen.append((ctx.summary.result, ctx.summary.failed_phase))
    )
    world.run(registry=registry)
    assert seen == [("failed", "P7")]


def test_the_notify_hook_may_fail_without_harm(world):
    def exploding(event, payload):
        raise RuntimeError("mail server down")

    outcome = world.run(
        pipeline_kwargs={"hooks": pipeline.PipelineHooks(notify=exploding)}
    )
    assert outcome.status == "published"


# ====================================================================================================== resume and state


def test_a_run_that_failed_after_the_export_resumes_at_stage(world):
    outcome = world.run(
        registry=world.registry(
            P13=flaky(world.trace, 1, error=lambda: PermanentError("wizard timeout"))
        )
    )
    assert outcome.status == "failed"
    world.trace.clear()
    seen = {}

    def stage(ctx):
        seen["ova"] = ctx.results["P10"]["ova"]  # restored from state.json
        return p12(ctx)

    resumed = world.run(registry=world.registry(P12=stage), resume=outcome.run_id)
    assert resumed.status == "published" and resumed.run_id == outcome.run_id
    assert world.trace == ["P12", "P13", "P14"]
    assert Path(seen["ova"]).name == "Ginnungagap_Appliance_v1.3.0.ova"
    state = world.state(resumed)
    assert state["status"] == "published" and state["failure"] is None
    assert any(e["event"] == "resumed" for e in state["events"])


@pytest.mark.parametrize("resume_from", ["P11", "P12"])
@pytest.mark.parametrize("old_status", ["failed", "running"])
def test_resume_records_current_process_before_work_and_keeps_original_start(
    world, monkeypatch, resume_from, old_status
):
    failed = (
        inspection_failed_run(world)
        if resume_from == "P11"
        else world.run(
            registry=world.registry(
                P13=flaky(world.trace, 1, error=lambda: PermanentError("timeout"))
            )
        )
    )
    path = failed.run_dir / "state.json"
    previous = world.state(failed)
    previous.update(pid=987654321, host="previous-worker", status=old_status)
    path.write_text(json.dumps(previous))
    monkeypatch.setattr(pipeline.os, "getpid", lambda: 123456789)
    monkeypatch.setattr(pipeline.socket, "gethostname", lambda: "current-worker")
    seen = []

    def inspect_owner(ctx):
        seen.append(json.loads(path.read_text()))
        return STANDARD[resume_from](ctx)

    resumed = world.run(
        registry=world.registry(**{resume_from: inspect_owner}), resume=failed.run_id
    )
    assert resumed.status == "published"
    assert len(seen) == 1
    for current in (seen[0], world.state(resumed)):
        assert current["pid"] == 123456789
        assert current["host"] == "current-worker"
        assert current["started_at"] == previous["started_at"]
    assert seen[0]["status"] == "running"
    assert any(event["event"] == "resumed" for event in seen[0]["events"])
    summary = json.loads((resumed.run_dir / "summary.json").read_text())
    assert summary["started_at"] == previous["started_at"]


@pytest.mark.parametrize("invalid", ["earlier-phase", "version", "published", "export"])
def test_invalid_resume_never_rewrites_process_or_existing_state(world, invalid):
    failed = inspection_failed_run(world)
    path = failed.run_dir / "state.json"
    previous = world.state(failed)
    previous.update(pid=987654321, host="previous-worker", status="running")
    version = VERSION
    if invalid == "earlier-phase":
        previous["phases"]["P8"]["status"] = "failed"
    elif invalid == "version":
        version = "1.3.1"
    elif invalid == "published":
        previous["status"] = "published"
    else:
        Path(previous["phases"]["P10"]["result"]["path"]).unlink()
    path.write_text(json.dumps(previous))
    original_bytes = path.read_bytes()
    with pytest.raises(pipeline.PipelineError):
        world.pipeline()._resume_state(
            failed.run_id, version, pipeline.RunOptions(resume=failed.run_id)
        )
    assert path.read_bytes() == original_bytes


def test_a_run_that_did_not_get_past_the_install_cannot_be_resumed(world):
    outcome = world.run(
        registry=world.registry(
            P8=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        )
    )
    resumed = world.run(resume=outcome.run_id)
    assert (
        resumed.status == "blocked"
        and "P8" in resumed.reason
        and "new run" in resumed.reason
    )


def test_resume_refuses_a_published_run_an_unknown_run_and_another_version(world):
    done = world.run()
    assert "ended as published" in world.run(resume=done.run_id).reason
    assert "no run" in world.run(resume="1.3.0-20200101T000000Z").reason
    failed = world.run(
        registry=world.registry(
            P13=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        )
    )
    assert "not of 1.3.1" in world.run("1.3.1", resume=failed.run_id).reason


def test_a_run_that_crashed_is_resumable_and_its_recorded_share_folder_is_removed_first(
    world,
):
    outcome = world.run(
        registry=world.registry(
            P13=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        )
    )
    path = outcome.run_dir / "state.json"
    state = json.loads(path.read_text())
    state["status"] = "running"  # as if the process had been killed: no cleanup ran
    state["resources"] = [
        {
            "id": 0,
            "kind": "share_folder",
            "ref": "Ginnungagap_Appliance_v1.3.0",
            "phase": "P12",
            "when": "failure",
            "data": {"marker_name": ".imgbuild-run", "marker_text": outcome.run_id},
            "status": "open",
        }
    ]
    path.write_text(json.dumps(state))
    world.share.reset_mock()
    resumed = world.run(resume=outcome.run_id)
    assert resumed.status == "published"
    world.share.remove_folder.assert_any_call(
        "Ginnungagap_Appliance_v1.3.0",
        marker_name=".imgbuild-run",
        marker_text=outcome.run_id,
    )


def test_state_json_is_written_atomically_and_updated_while_the_run_goes_on(world):
    snapshots = []

    def look(ctx):
        data = json.loads((ctx.run_dir / "state.json").read_text())
        snapshots.append(
            (
                data["status"],
                data["phases"]["P5"]["status"],
                data["phases"]["P6"]["status"],
                data["phases"]["P4"]["status"],
            )
        )

    world.run(registry=world.registry(P6=look))
    assert snapshots == [("running", "ok", "running", "ok")]
    assert not list(world.state_dir.rglob("*.partial"))


def test_attempts_in_window_counts_failed_builds_of_the_release_only(world):
    world.run(registry=world.registry(P7=flaky(world.trace, 99)))  # failed, 2 attempts
    world.run(
        registry=world.registry(
            P8=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        )
    )  # failed, 1 attempt
    world.run(
        "1.3.1",
        registry=world.registry(
            P8=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        ),
    )
    world.run(dry_run=True)
    world.run(stop_after="P5")
    now = world.clock.now()
    assert pipeline.attempts_in_window(world.state_dir, VERSION, now) == 3
    assert pipeline.attempts_in_window(world.state_dir, "1.3.1", now) == 1
    assert (
        pipeline.attempts_in_window(world.state_dir, VERSION, now + timedelta(hours=25))
        == 0
    )


# ====================================================================================================== lock, PAUSE, lab busy


def test_a_second_build_is_refused_while_the_lock_is_held(world):
    lock = pipeline.BuildLock(world.state_dir / "lock")
    lock.acquire("1.2.9-20261120T090000Z")
    outcome = world.run()
    assert (outcome.status, outcome.exit_code) == ("deferred", 3)
    assert (
        outcome.reason == f"build 1.2.9-20261120T090000Z is running (pid {os.getpid()})"
    )
    assert world.run_dirs() == [] and world.esxi.calls == [] and world.trace == []
    with pytest.raises(AlreadyRunning):
        pipeline.BuildLock(world.state_dir / "lock").acquire("x")
    assert world.events[-1][0] == "deferred"
    lock.release()
    assert world.run().status == "published"
    assert pipeline.BuildLock(world.state_dir / "lock").holder() in (
        None,
        {},
    )  # released and emptied


def test_the_lock_is_free_again_after_a_failed_run(world):
    world.run(
        registry=world.registry(
            P7=flaky(world.trace, 99, error=lambda: RuntimeError("x"))
        )
    )
    lock = pipeline.BuildLock(world.state_dir / "lock")
    lock.acquire("next")
    lock.release()


def test_the_janitor_command_does_not_run_while_a_build_holds_the_lock(world):
    seen = {}

    def during(ctx):
        try:
            world.pipeline().run_janitor()
        except AlreadyRunning as exc:
            seen["refused"] = exc.message

    world.run(registry=world.registry(P6=during))
    assert "is running" in seen["refused"]


def test_the_pause_file_defers_every_real_run_without_touching_the_lab(world):
    world.state_dir.mkdir()
    (world.state_dir / "PAUSE").write_text("")
    outcome = world.run()
    assert outcome.status == "deferred" and "PAUSE" in outcome.reason
    assert (
        world.trace == []
        and world.run_dirs() == []
        and world.esxi.mutating_calls() == []
    )
    (world.state_dir / "PAUSE").unlink()
    assert world.run().status == "published"


def test_a_powered_on_vm_outside_the_allow_list_means_the_lab_is_busy(world):
    world.esxi.add_vm("somebody-test-vm", power_state=POWERED_ON)
    outcome = world.run()
    assert (
        outcome.status == "deferred"
        and "somebody-test-vm" in outcome.reason
        and "GNS3VM5" not in outcome.reason
    )
    assert world.trace == [] and world.esxi.mutating_calls() == []
    forced = world.run(force_busy=True)
    assert forced.status == "published"


def test_powered_off_vms_and_our_own_leftovers_do_not_make_the_lab_busy(world):
    world.esxi.add_vm("an-off-vm")
    world.esxi.add_vm(
        "imgbuild-v1.2.1-20261119T000000Z",
        power_state=POWERED_ON,
        annotation="ginnungagap-image-build run=1.2.1-20261119T000000Z",
    )
    assert world.run().status == "published"


def test_the_lab_busy_check_is_a_replaceable_hook(world):
    hooks = pipeline.PipelineHooks(lab_busy=lambda esxi, config: "a human said so")
    outcome = world.run(pipeline_kwargs={"hooks": hooks})
    assert outcome.status == "deferred" and outcome.reason == "a human said so"


def test_an_unreachable_host_before_the_start_is_a_deferral_not_a_failure(world):
    world.esxi.inject("list_vms", Unreachable("connection refused"), times=None)
    outcome = world.run()
    assert outcome.status == "deferred" and "not ready" in outcome.reason


def test_a_host_outside_the_allow_list_is_never_touched(world):
    world.esxi = FakeEsxi(host="10.20.20.83", clock=world.clock)
    outcome = world.run()
    assert outcome.status == "failed" and "not in esxi.allowed_hosts" in outcome.reason
    assert (
        world.esxi.calls_named("list_vms") == [] and world.esxi.mutating_calls() == []
    )
    with pytest.raises(PermanentError, match="refuses"):
        world.pipeline().run_janitor()


# ====================================================================================================== dry run and stop-after


def test_a_dry_run_runs_p0_to_p3_for_real_describes_the_rest_and_creates_nothing(world):
    outcome = world.run(dry_run=True)
    assert (outcome.status, outcome.exit_code) == ("dry-run", 0)
    assert world.trace == ["P0", "P1", "P2", "P3"]
    assert world.esxi.mutating_calls() == [] and world.esxi.vm_names() == ["GNS3VM5"]
    assert world.share.mock_calls and not any(
        c[0] in ("make_folder", "put_file", "put_text", "remove_folder")
        for c in world.share.mock_calls
    )
    world.templates.refresh.assert_not_called()
    plan = {row["phase"]: row["text"] for row in outcome.summary.plan}
    assert list(plan) == [p for p in pipeline.PHASE_IDS if pipeline.phase_index(p) >= 4]
    assert plan["P4"] == f"import the base OVA as {names.build_vm_name(outcome.run_id)}"
    assert plan["P8"] == "would run prepare" and plan["P15"].startswith(
        "delete the VMs"
    )
    state = world.state(outcome)
    assert (
        state["status"] == "dry-run"
        and state["phases"]["P7"]["status"] == "planned"
        and state["phases"]["P3"]["status"] == "ok"
    )
    assert not list(world.run_dirs()[0].glob("*.partial")) and world.events == []


def test_a_dry_run_ignores_pause_and_busy_but_reports_them(world):
    world.state_dir.mkdir()
    (world.state_dir / "PAUSE").write_text("")
    world.esxi.add_vm("somebody", power_state=POWERED_ON)
    outcome = world.run(dry_run=True)
    assert outcome.status == "dry-run"
    gate = outcome.summary.facts["gate"]
    assert (
        "PAUSE" in gate["paused"]
        and "somebody" in gate["lab_busy"]
        and gate["applied"] is False
    )


def test_a_dry_run_takes_the_lock_and_does_not_run_the_janitor(world):
    world.esxi.add_vm(
        "imgbuild-v1.0.0-20250101T000000Z",
        annotation="ginnungagap-image-build run=1.0.0-20250101T000000Z",
    )
    world.run(dry_run=True)
    assert "imgbuild-v1.0.0-20250101T000000Z" in world.esxi.vm_names()
    lock = pipeline.BuildLock(world.state_dir / "lock")
    lock.acquire("x")
    assert world.run(dry_run=True).status == "deferred"
    lock.release()


def test_a_phase_that_reaches_for_a_mutating_call_in_a_dry_run_is_caught(world):
    def sneaky(ctx):
        ctx.esxi.import_ova("http://x/a.ova", "imgbuild-x", "Datastore", ["VM Network"])

    outcome = world.run(registry=world.registry(P2=sneaky), dry_run=True)
    assert (
        outcome.status == "failed" and "import_ova changes something" in outcome.reason
    )
    assert world.esxi.vm_names() == ["GNS3VM5"]

    def sneaky_track(ctx):
        ctx.track_vm("imgbuild-x")

    assert (
        "creates nothing"
        in world.run(registry=world.registry(P2=sneaky_track), dry_run=True).reason
    )


def test_stop_after_runs_through_that_phase_and_cleans_up(world):
    outcome = world.run(stop_after="P8")
    assert (outcome.status, outcome.exit_code) == ("stopped", 0)
    assert world.trace == ALL[:9]
    assert world.esxi.vm_names() == ["GNS3VM5"]
    state = world.state(outcome)
    assert state["status"] == "stopped" and state["phases"]["P9"]["status"] == "pending"
    assert [e for e, _ in world.events] == ["started"]


def test_keep_leaves_the_vm_for_a_human_and_the_janitor_waits_24_hours(world):
    outcome = world.run(stop_after="P8", keep=True)
    vm = names.build_vm_name(outcome.run_id)
    assert vm in world.esxi.vm_names()
    state = world.state(outcome)
    assert state["kept"] is True
    assert state["keep_until"] == (world.clock.now() + timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert [r["status"] for r in state["resources"]] == ["kept"]
    report = world.pipeline().run_janitor()
    assert (
        report.kept == [vm] and report.deleted_vms == [] and vm in world.esxi.vm_names()
    )
    world.clock.advance(25 * 3600)
    report = world.pipeline().run_janitor()
    assert report.deleted_vms == [vm] and world.esxi.vm_names() == ["GNS3VM5"]


def test_keep_failed_in_the_config_keeps_the_vms_of_a_failed_run(world):
    world.config = config_module.parse_config(
        {
            "esxi": {"datastore": "Datastore", "network": "VM Network"},
            "paths": {"state_dir": str(world.state_dir)},
            "keep_failed": True,
        }
    )
    outcome = world.run(
        registry=world.registry(
            P8=flaky(world.trace, 99, error=lambda: PermanentError("x"))
        )
    )
    assert (
        names.build_vm_name(outcome.run_id) in world.esxi.vm_names()
        and world.state(outcome)["kept"] is True
    )


# ====================================================================================================== the janitor


def seed_dead_run(world, run_id, status="running", annotation=True) -> str:
    """A run of a dead process: its state.json says `running`, its base VM is still on the host."""
    run_dir = world.state_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "version": run_id.split("-")[0],
                "status": status,
                "started_at": "2026-11-19T10:00:00Z",
                "phases": {},
                "resources": [],
            }
        )
    )
    vm = names.build_vm_name(run_id)
    world.esxi.add_vm(
        vm,
        power_state=POWERED_ON,
        annotation=names.annotation(run_id) if annotation else "",
        created=world.clock.now() - timedelta(hours=30),
    )
    return vm


def seed_export(
    world, run_id, *, status="failed", validated=True, resource_status="open"
):
    """A persisted P10 result, or an interrupted export without a successful P10."""
    seed_dead_run(world, run_id, status=status)
    run_dir = world.state_dir / "runs" / run_id
    directory = run_dir / "export"
    directory.mkdir()
    ova = directory / "image.ova"
    ova.write_bytes(b"verified export " + run_id.encode())
    path = run_dir / "state.json"
    state = json.loads(path.read_text())
    if validated:
        state["phases"]["P10"] = {
            "status": "ok",
            "result": {
                "path": str(ova),
                "size": ova.stat().st_size,
                "sha256": hashlib.sha256(ova.read_bytes()).hexdigest(),
            },
        }
    state["resources"].append(
        {
            "id": 0,
            "kind": "export_artifact",
            "ref": str(directory),
            "phase": "P10",
            "when": "always",
            "status": resource_status,
        }
    )
    path.write_text(json.dumps(state))
    return ova


def retained_export(ctx):
    """Small deterministic stand-in for a successful, validated production P10."""
    from image.build.esxi import cleanup_export_artifacts

    directory = ctx.run_dir / "export"
    ctx.on_cleanup(
        "export_artifact",
        str(directory),
        lambda: cleanup_export_artifacts(
            ctx.state_dir, directory, published=ctx.summary.published
        ),
    )
    directory.mkdir(exist_ok=True)
    ova = directory / "image.ova"
    assert not ova.exists(), "a rebuilt attempt must not reuse the prior export"
    ova.write_bytes(f"export attempt {ctx.attempt}".encode())
    ctx.esxi.delete_vm(ctx.results["P4"]["vm"])
    return {
        "path": str(ova),
        "ova": str(ova),
        "size": ova.stat().st_size,
        "sha256": hashlib.sha256(ova.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("resource_status", ["open", "failed", "cleaned", "retained"])
def test_janitor_retains_only_the_newest_validated_failed_export(
    world, resource_status
):
    old = seed_export(world, "1.2.8-20261119T100000Z", resource_status=resource_status)
    newest = seed_export(
        world, "1.2.9-20261119T110000Z", resource_status=resource_status
    )
    # A directory alone does not prove ownership; even a failed foreign export is untouchable.
    seed_dead_run(world, "1.2.7-20261119T090000Z", status="failed")
    foreign = world.state_dir / "runs" / "1.2.7-20261119T090000Z" / "export"
    foreign.mkdir()
    (foreign / "foreign.ova").write_bytes(b"not recorded as ours")
    report = world.pipeline().run_janitor()
    assert report.clean
    assert newest.is_file() and foreign.is_dir() and not old.parent.exists()
    assert str(newest.parent) in report.kept
    state = json.loads((newest.parent.parent / "state.json").read_text())
    assert state["resources"][0]["status"] == "retained"
    # Cleaned/retained resource records must still participate in the next retention sweep.
    next_ova = seed_export(world, "1.3.0-20261120T100000Z")
    world.pipeline().run_janitor()
    assert next_ova.is_file() and not newest.parent.exists() and foreign.exists()


@pytest.mark.parametrize(
    "damage", ["unvalidated", "same_size_corruption", "truncated", "missing"]
)
def test_janitor_discards_incomplete_or_changed_exports_without_evicting_valid_one(
    world, damage
):
    good = seed_export(world, "1.2.8-20261119T100000Z")
    bad = seed_export(
        world, "1.2.9-20261119T110000Z", validated=damage != "unvalidated"
    )
    if damage == "same_size_corruption":
        bad.write_bytes(b"x" * bad.stat().st_size)
    elif damage == "truncated":
        bad.write_bytes(b"short")
    elif damage == "missing":
        bad.unlink()
    report = world.pipeline().run_janitor()
    assert report.clean and good.is_file() and not bad.parent.exists()


def test_janitor_respects_active_run_keep_and_foreign_export_paths(world):
    active_run = "1.3.0-20261120T100000Z"
    active = seed_export(world, active_run, status="running")
    kept = seed_export(world, "1.2.9-20261119T110000Z")
    kept_state = kept.parent.parent / "state.json"
    data = json.loads(kept_state.read_text())
    data.update(
        kept=True, keep_until=(world.clock.now() + timedelta(hours=1)).isoformat()
    )
    kept_state.write_text(json.dumps(data))
    precious = world.tmp / "published-store"
    precious.mkdir()
    (precious / "saved.ova").write_bytes(b"published image")
    seed_dead_run(world, "1.2.8-20261119T100000Z", status="failed")
    foreign_state = world.state_dir / "runs" / "1.2.8-20261119T100000Z" / "state.json"
    data = json.loads(foreign_state.read_text())
    data["resources"] = [
        {"kind": "export_artifact", "ref": str(precious), "status": "open"}
    ]
    foreign_state.write_text(json.dumps(data))
    report = pipeline.Janitor(
        world.config,
        world.esxi,
        world.clock,
        world.state_dir,
        world.pipeline().log,
        current_run=active_run,
    ).run()
    assert report.clean and active.is_file() and kept.is_file()
    assert (precious / "saved.ova").read_bytes() == b"published image"
    assert (
        json.loads((active.parent.parent / "state.json").read_text())["status"]
        == "running"
    )


def test_janitor_never_follows_symlinked_run_state_or_export(world):
    good = seed_export(world, "1.2.8-20261119T100000Z")
    linked = seed_export(world, "1.2.9-20261119T110000Z")
    linked.unlink()
    linked.parent.rmdir()
    precious = world.tmp / "precious"
    precious.mkdir()
    (precious / "foreign.ova").write_bytes(b"foreign")
    linked.parent.symlink_to(precious, target_is_directory=True)
    # A run symlink and state-file symlink must not cause foreign state writes either.
    foreign_run = world.tmp / "foreign-run"
    foreign_run.mkdir()
    original = json.dumps({"run_id": "1.2.7-20261119T090000Z", "status": "running"})
    (foreign_run / "state.json").write_text(original)
    (world.state_dir / "runs" / "1.2.7-20261119T090000Z").symlink_to(
        foreign_run, target_is_directory=True
    )
    state_link_run = world.state_dir / "runs" / "1.2.6-20261119T080000Z"
    state_link_run.mkdir()
    (state_link_run / "state.json").symlink_to(foreign_run / "state.json")
    report = world.pipeline().run_janitor()
    assert report.clean and good.is_file()
    assert (
        linked.parent.is_symlink()
        and (precious / "foreign.ova").read_bytes() == b"foreign"
    )
    assert (foreign_run / "state.json").read_text() == original


def test_janitor_crash_after_publish_removes_private_export_and_preserves_store(world):
    ova = seed_export(world, "1.3.0-20261120T100000Z", status="running")
    path = ova.parent.parent / "state.json"
    state = json.loads(path.read_text())
    state["phases"]["P14"] = {"status": "ok"}
    state["resources"].append(
        {
            "kind": "share_folder",
            "ref": "Ginnungagap_Appliance_v1.3.0",
            "status": "open",
        }
    )
    path.write_text(json.dumps(state))
    report = world.pipeline().run_janitor()
    world.share.remove_folder.assert_not_called()
    assert report.clean and not ova.parent.exists()
    state = json.loads(path.read_text())
    assert state["resources"][0]["status"] == "cleaned"
    assert state["resources"][1]["status"] == "final"


def test_resume_preserves_requested_older_export_and_restores_success_cleanup(world):
    failed = world.run(
        registry=world.registry(
            P10=retained_export,
            P13=flaky(
                world.trace, 99, error=lambda: PermanentError("acceptance failed")
            ),
        )
    )
    old = Path(world.state(failed)["phases"]["P10"]["result"]["path"])
    newer = seed_export(world, "1.3.1-20261120T110000Z")
    seen = []

    def stage(ctx):
        seen.append(Path(ctx.results["P10"]["path"]).read_bytes())
        return p12(ctx)

    resumed = world.run(registry=world.registry(P12=stage), resume=failed.run_id)
    assert resumed.status == "published" and seen == [b"export attempt 1"]
    assert not old.parent.exists() and not newer.parent.exists()


def test_failed_resume_retains_verified_export_for_another_resume(world):
    registry = world.registry(
        P10=retained_export,
        P13=flaky(world.trace, 99, error=lambda: PermanentError("acceptance failed")),
    )
    failed = world.run(registry=registry)
    ova = Path(world.state(failed)["phases"]["P10"]["result"]["path"])
    resumed = world.run(registry=registry, resume=failed.run_id)
    assert resumed.status == "failed" and ova.read_bytes() == b"export attempt 1"
    world.pipeline().run_janitor()
    assert ova.is_file()
    assert world.run(resume=failed.run_id).status == "published"
    assert not ova.parent.exists()


def test_transient_retry_discards_only_prior_attempt_export(world):
    world.trace.clear()
    outcome = world.run(
        registry=world.registry(
            P10=retained_export,
            P11=flaky(world.trace, 1),
            P13=flaky(world.trace, 99, error=lambda: PermanentError("final failure")),
        )
    )
    assert outcome.status == "failed" and world.state(outcome)["attempt"] == 2
    ova = Path(world.state(outcome)["phases"]["P10"]["result"]["path"])
    assert ova.read_bytes() == b"export attempt 2"
    assert world.trace.count("P10") == 2


def test_the_janitor_deletes_only_prefixed_objects(world):
    esxi = world.esxi
    dead_vm = seed_dead_run(world, "1.2.9-20261119T100000Z")
    old_orphan = "imgaccept-v1.2.9-20261119T100000Z-b"
    esxi.add_vm(
        old_orphan, annotation="", created=world.clock.now() - timedelta(hours=7)
    )
    young_orphan = "imgbuild-v1.2.9-20261120T095500Z"
    esxi.add_vm(
        young_orphan, annotation="", created=world.clock.now() - timedelta(minutes=5)
    )
    unknown_age = "imgbuild-v0.0.1-unknown"
    esxi.add_vm(unknown_age, annotation="", created=None).created = None
    untouchable = [
        esxi.add_vm("my-imgbuild-1", power_state=POWERED_ON).name,
        esxi.add_vm(
            "imgbuilder",
            annotation="ginnungagap-image-build run=1.2.9-20261119T100000Z",
        ).name,
        esxi.add_vm("ginnungagap-appliance-v1.2.1").name,
        esxi.add_vm("imgbuild").name,
    ]
    esxi.add_folder("imgbuild-orphan")
    esxi.add_folder("imgbuild-orphan_1")
    esxi.add_folder("imgaccept-orphan_2")
    esxi.add_folder("Ginnungagap_Appliance_v1.2.1")
    esxi.add_folder("some-other-folder")
    esxi.vm(
        "imgbuilder"
    ).folder = "imgbuild-registered-vm-folder"  # a registered VM lives in a folder with our prefix
    esxi.add_folder("imgbuild-registered-vm-folder")

    report = world.pipeline().run_janitor()

    assert sorted(report.deleted_vms) == sorted([dead_vm, old_orphan])
    assert sorted(report.deleted_folders) == [
        "imgaccept-orphan_2",
        "imgbuild-orphan",
        "imgbuild-orphan_1",
    ]
    assert sorted(report.kept) == sorted([young_orphan, unknown_age])
    assert report.crashed_runs == ["1.2.9-20261119T100000Z"] and report.clean
    remaining = set(esxi.vm_names())
    assert {"GNS3VM5", young_orphan, unknown_age, *untouchable} <= remaining
    assert remaining.isdisjoint({dead_vm, old_orphan})
    assert {
        "Ginnungagap_Appliance_v1.2.1",
        "some-other-folder",
        "imgbuild-registered-vm-folder",
    } <= set(esxi.list_datastore_folders("Datastore"))
    deleted_by_call = {c.args["name"] for c in esxi.calls_named("delete_vm")}
    assert deleted_by_call == {dead_vm, old_orphan}
    state = json.loads(
        (world.state_dir / "runs" / "1.2.9-20261119T100000Z" / "state.json").read_text()
    )
    assert state["status"] == "crashed" and state["failure"]["kind"] == "crashed"


def test_the_janitor_runs_at_the_start_of_every_real_run(world):
    dead_vm = seed_dead_run(world, "1.2.9-20261119T100000Z")
    outcome = world.run()
    assert outcome.status == "published" and dead_vm not in world.esxi.vm_names()
    assert outcome.run_id not in [
        d.name
        for d in world.run_dirs()
        if json.loads((d / "state.json").read_text())["status"] == "crashed"
    ]
    skipped = seed_dead_run(world, "1.2.8-20261119T100000Z")
    world.run(pipeline_kwargs=None, skip_janitor=True)
    assert skipped in world.esxi.vm_names()


def test_the_janitor_cleans_what_a_crashed_run_recorded_but_only_inside_its_own_area(
    world,
):
    seed_dead_run(world, "1.2.9-20261119T100000Z")
    run_dir = world.state_dir / "runs" / "1.2.9-20261119T100000Z"
    key = run_dir / "key"
    key.mkdir()
    (key / "id").write_text("PRIVATE")
    precious = world.tmp / "precious.txt"
    precious.write_text("keep me")
    state = json.loads((run_dir / "state.json").read_text())
    state["resources"] = [
        {
            "id": 0,
            "kind": "share_folder",
            "ref": "Ginnungagap_Appliance_v1.2.9",
            "phase": "P12",
            "when": "failure",
            "data": {
                "marker_name": ".imgbuild-run",
                "marker_text": "1.2.9-20261119T100000Z",
            },
            "status": "open",
        },
        {
            "id": 1,
            "kind": "share_folder",
            "ref": "SomeoneElsesFolder",
            "phase": "P12",
            "when": "failure",
            "data": {},
            "status": "open",
        },
        {
            "id": 2,
            "kind": "path",
            "ref": str(key),
            "phase": "P5",
            "when": "always",
            "data": {},
            "status": "open",
        },
        {
            "id": 3,
            "kind": "path",
            "ref": str(precious),
            "phase": "P5",
            "when": "always",
            "data": {},
            "status": "open",
        },
        {
            "id": 4,
            "kind": "path",
            "ref": str(world.state_dir / "runs"),
            "phase": "P5",
            "when": "always",
            "data": {},
            "status": "open",
        },
    ]
    (run_dir / "state.json").write_text(json.dumps(state))
    report = world.pipeline().run_janitor()
    world.share.remove_folder.assert_called_once_with(
        "Ginnungagap_Appliance_v1.2.9",
        marker_name=".imgbuild-run",
        marker_text="1.2.9-20261119T100000Z",
    )
    assert report.deleted_share_folders == [
        "Ginnungagap_Appliance_v1.2.9"
    ] and report.removed_paths == [str(key)]
    assert (
        not key.exists()
        and precious.read_text() == "keep me"
        and (world.state_dir / "runs").exists()
    )
    assert (
        json.loads((run_dir / "state.json").read_text())["resources"][0]["status"]
        == "cleaned"
    )


def test_a_janitor_error_is_reported_and_does_not_stop_the_rest(world):
    first = seed_dead_run(world, "1.2.9-20261119T100000Z")
    second = seed_dead_run(world, "1.2.8-20261119T100000Z")
    world.esxi.inject(
        "delete_vm",
        PermanentError("locked by a snapshot task"),
        match=lambda c: c.args["name"] == first,
        times=None,
    )
    report = world.pipeline().run_janitor()
    assert (
        report.deleted_vms == [second]
        and not report.clean
        and "snapshot task" in report.errors[0]
    )
    assert first in world.esxi.vm_names()


# ====================================================================================================== no secret anywhere


def test_the_password_never_reaches_a_log_a_state_file_or_the_summary(world):
    def leaky(ctx):
        ctx.logger.info(f"logging in with {PASSWORD}")
        ctx.note("debug", f"the password is {PASSWORD}")
        ctx.secret("another-secret-value")
        ctx.logger.warning("also another-secret-value and ghp_" + "a" * 30)
        raise PermanentError(
            f"login failed for password {PASSWORD}", detail=f"detail {PASSWORD}"
        )

    outcome = world.run(registry=world.registry(P7=leaky))
    assert outcome.status == "failed"
    for path in outcome.run_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="ignore")
            assert PASSWORD not in text, path
            assert (
                "another-secret-value" not in text and "ghp_" + "a" * 30 not in text
            ), path
    assert PASSWORD not in outcome.reason and PASSWORD not in json.dumps(
        outcome.summary.to_dict()
    )
    assert PASSWORD not in repr(world.events)
    assert "***" in (outcome.run_dir / "run.log").read_text()


def test_the_redactor_handles_keys_tokens_and_short_values():
    redactor = pipeline.Redactor(["abc", "long-enough-secret"])
    text = redactor.redact(
        "abc long-enough-secret -----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY----- github_pat_"
        + "A" * 30
    )
    assert (
        text == "abc *** *** ***"
    )  # a value under 4 characters is not registered (it would mangle every line)


def test_the_stat_of_the_run_folder_is_private(world):
    outcome = world.run()
    assert stat.S_IMODE(outcome.run_dir.stat().st_mode) == 0o700


def inspection_failed_run(world):
    return world.run(
        registry=world.registry(
            P10=retained_export,
            P11=flaky(
                world.trace, 99, error=lambda: PermanentError("inspection rejected")
            ),
        )
    )


def test_failed_inspection_resumes_at_p11_with_exact_validated_export(world):
    failed = inspection_failed_run(world)
    ova = Path(world.state(failed)["phases"]["P10"]["result"]["path"])
    before = ova.read_bytes()
    world.trace.clear()
    seen = []

    def inspect_export(ctx):
        seen.append(Path(ctx.results["P10"]["path"]).read_bytes())
        assert ctx.results["P5"]
        return {"checks": [{"name": "strict inspection", "passed": True}]}

    resumed = world.run(
        registry=world.registry(P11=inspect_export), resume=failed.run_id
    )
    assert resumed.status == "published"
    assert world.trace == ["P11", "P12", "P13", "P14"]
    assert seen == [before] and not ova.parent.exists()
    current = world.state(resumed)
    assert current["phases"]["P11"]["status"] == "ok"
    assert any(
        event["event"] == "resumed" and event.get("phase") == "P11"
        for event in current["events"]
    )


@pytest.mark.parametrize("phase_status", ["pending", "running"])
def test_crashed_or_pending_inspection_can_restart_from_validated_p10(
    world, phase_status
):
    failed = inspection_failed_run(world)
    path = failed.run_dir / "state.json"
    state = world.state(failed)
    state["status"] = "crashed"
    state["phases"]["P11"] = {"status": phase_status}
    path.write_text(json.dumps(state))
    world.trace.clear()
    resumed = world.run(resume=failed.run_id)
    assert resumed.status == "published" and world.trace == ["P11", "P12", "P13", "P14"]


def test_resumed_inspection_still_blocks_publication_and_retains_export(world):
    failed = inspection_failed_run(world)
    ova = Path(world.state(failed)["phases"]["P10"]["result"]["path"])
    world.trace.clear()
    world.share.reset_mock()
    resumed = world.run(
        registry=world.registry(
            P11=flaky(
                world.trace, 99, error=lambda: PermanentError("private key residue")
            )
        ),
        resume=failed.run_id,
    )
    assert resumed.status == "failed" and resumed.failed_phase == "P11"
    assert (
        world.trace == ["P11"]
        and world.state(resumed)["phases"]["P11"]["status"] == "failed"
    )
    world.share.make_folder.assert_not_called()
    assert ova.read_bytes() == b"export attempt 1"


@pytest.mark.parametrize(
    "damage",
    [
        "changed",
        "missing",
        "size",
        "hash",
        "outside",
        "untracked",
        "symlink",
        "unvalidated",
    ],
)
def test_inspection_resume_requires_recorded_owned_unchanged_validated_export(
    world, damage
):
    failed = inspection_failed_run(world)
    path = failed.run_dir / "state.json"
    state = world.state(failed)
    p10 = state["phases"]["P10"]
    facts = p10["result"]
    ova = Path(facts["path"])
    if damage == "changed":
        original = ova.read_bytes()
        ova.write_bytes(b"X" + original[1:])  # same size, changed SHA-256
    elif damage == "missing":
        ova.unlink()
    elif damage == "size":
        facts["size"] += 1
    elif damage == "hash":
        facts["sha256"] = "0" * 64
    elif damage == "outside":
        foreign = world.tmp / "foreign.ova"
        foreign.write_bytes(ova.read_bytes())
        facts["path"] = str(foreign)
    elif damage == "untracked":
        state["resources"] = [
            resource
            for resource in state["resources"]
            if resource.get("kind") != "export_artifact"
        ]
    elif damage == "symlink":
        foreign = world.tmp / "foreign.ova"
        ova.rename(foreign)
        ova.symlink_to(foreign)
    elif damage == "unvalidated":
        p10["status"] = "failed"
    path.write_text(json.dumps(state))
    world.trace.clear()
    resumed = world.run(resume=failed.run_id)
    assert (
        resumed.status == "blocked" and "new run" in resumed.reason and not world.trace
    )


@pytest.mark.parametrize("earlier_phase", ["P0", "P8", "P9", "P10"])
def test_inspection_resume_cannot_skip_an_earlier_failed_phase(world, earlier_phase):
    failed = inspection_failed_run(world)
    path = failed.run_dir / "state.json"
    state = world.state(failed)
    state["phases"][earlier_phase]["status"] = "failed"
    path.write_text(json.dumps(state))
    world.trace.clear()
    resumed = world.run(resume=failed.run_id)
    assert (
        resumed.status == "blocked"
        and earlier_phase in resumed.reason
        and not world.trace
    )
