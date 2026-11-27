"""Bootstrap, installation parity, preparation and archive-validation regressions."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from image.build import config, esxi, names, pipeline, remote
from image.build import phases_install as install
from image.build.ports import (
    Blocked,
    CommandFailed,
    CommandResult,
    PermanentError,
    ToolsUnavailable,
)
from tests.helpers.fake_esxi import FakeClock, FakeEsxi
from tests.helpers.fake_release import make_release
from tests.helpers.fake_remote import FakeRemote

RUN = "1.3.0-20261120T120000Z"


@pytest.mark.skipif(os.name != "posix", reason="guest filesystem symlinks need POSIX")
@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "active-repos",
        "active-link",
        "linked-boundary",
        "linked-entry",
        "unknown-entry",
        "missing-role",
        "wrong-owner",
    ],
)
def test_prepare_removes_only_proven_inactive_legacy_sources(tmp_path, mutation):
    legacy = tmp_path / "srv/repos"
    legacy.mkdir(parents=True)
    repos = [
        "Ginnungagap-Appliance",
        "Igdrasyl",
        "TopologyBuilder",
        "Hugyn-and-Munyn",
        "Mimyr",
    ]
    roles = ["appliance", "operator", "builder", "template-services", "generator"]
    links = tmp_path / "var/lib/ginnungagap/components"
    links.mkdir(parents=True)
    for repo in repos:
        (legacy / repo).mkdir()
        (legacy / repo / "old-source").write_text("old fixture")
        active = tmp_path / "var/lib/ginnungagap/releases" / repo / "1.3.5"
        active.mkdir(parents=True)
        (active / "keep").write_text("active source")
        (links / repo).symlink_to(active)
    (legacy / "components.json").write_text("{}")
    env = tmp_path / "etc/ginnungagap/install.env"
    env.parent.mkdir(parents=True)
    env.write_text("REPOS_DIR=" + str(links) + "\n")
    state = tmp_path / "var/lib/ginnungagap/update/installed.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"components": {role: {} for role in roles}}))
    if mutation == "active-repos":
        env.write_text("REPOS_DIR=" + str(legacy) + "\n")
    elif mutation == "active-link":
        (links / repos[0]).unlink()
        (links / repos[0]).symlink_to(legacy / repos[0])
    elif mutation == "linked-boundary":
        saved = tmp_path / "saved"
        legacy.rename(saved)
        legacy.symlink_to(saved)
    elif mutation == "linked-entry":
        shutil.rmtree(legacy / repos[0])
        (legacy / repos[0]).symlink_to(links / repos[0])
    elif mutation == "unknown-entry":
        (legacy / "personal-project").mkdir()
    elif mutation == "missing-role":
        state.write_text('{"components":{}}')
    namespace = {
        "pathlib": __import__("pathlib"),
        "json": json,
        "shutil": shutil,
        "shlex": shlex,
    }
    exec(install._LEGACY_SOURCE_CLEANUP, namespace)  # noqa: S102 - execute the pinned guest helper against a disposable tree
    if mutation is None:
        namespace["cleanup_legacy_sources"](tmp_path, owner_uid=os.geteuid())
        assert not legacy.exists()
    else:
        with pytest.raises(RuntimeError):
            namespace["cleanup_legacy_sources"](
                tmp_path,
                owner_uid=os.geteuid() + (1 if mutation == "wrong-owner" else 0),
            )
        assert legacy.exists()
    assert all(
        (links / repo / "keep").is_file()
        for repo in repos
        if not (mutation == "active-link" and repo == repos[0])
    )
    # The exact tested helper executes before zero-fill in the actual wrapper.
    compile(install._PREPARE, "prepare-wrapper", "exec")
    assert "restore_network()" in install._PREPARE
    assert install._PREPARE.index("cleanup_legacy_sources()") < install._PREPARE.index(
        "subprocess.run([sys.argv[1]"
    )


@pytest.fixture
def ctx(tmp_path):
    state = tmp_path / "state"
    run = state / "runs" / RUN
    run.mkdir(parents=True)
    release = make_release(run / "release")
    manifest = json.loads((release / "components.json").read_text())
    vm = names.build_vm_name(RUN)
    clock = FakeClock()
    host = FakeEsxi(clock=clock)
    host.add_vm(vm, ip="192.0.2.3", tools_running=True)
    result = NS(
        config=config.parse_config(
            {
                "esxi": {"datastore": "Datastore", "network": "VM Network"},
                "image": {"ssh_user": "junioradmin"},
                "paths": {"state_dir": str(state)},
            }
        ),
        clock=clock,
        esxi=host,
        remote=FakeRemote(),
        run_id=RUN,
        version="1.3.0",
        attempt=1,
        run_dir=run,
        state_dir=state,
        names=names.for_version("1.3.0"),
        options=pipeline.RunOptions(allow_unsigned=True),
        logger=logging.getLogger("test-build-install"),
        summary=NS(published=False),
        results={
            "P2": {"directory": str(release), "manifest": manifest},
            "P3": {"url": "http://ova.test/base.ova", "username": "junioradmin"},
            "P4": {"vm": vm},
        },
    )
    result.tracked = []
    result.secrets = []
    result.cleanups = []
    result.track_path = lambda path: result.tracked.append(Path(path))
    result.track_vm = lambda vm: result.tracked.append(vm)
    result.track_datastore_folder = lambda ds, folder: result.tracked.append(
        (ds, folder)
    )
    result.on_cleanup = lambda kind, ref, fn, **kw: result.cleanups.append(
        (kind, ref, fn)
    )
    result.secret = result.secrets.append
    result.result_of = lambda phase: result.results[phase]
    result.remote.respond(
        ["sudo", "-n", "test", "-f", "/var/lib/ginnungagap/firstboot.done"]
    )
    return result


def bootstrap(ctx):
    ctx.results["P5"] = remote.bootstrap_base(ctx)
    ctx.results["P6"] = remote.upload_release(ctx)
    return ctx.results["P5"]


def installed_response(ctx):
    return {
        "installed": {
            "appliance": {"version": ctx.version},
            "components": ctx.results["P2"]["manifest"]["components"],
        }
    }


def responses(ctx, update_help="usage: update --apply --no-start"):
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "update", "--help"],
        CommandResult(0, update_help),
    )
    ctx.remote.respond(["sudo", "-n", "env"])
    ctx.remote.respond(["sudo", "-n", "python3"])
    ctx.remote.respond(["sudo", "-n", "/srv/repos/Ginnungagap-Appliance/install.sh"])
    ctx.remote.respond(["sudo", "-n", "ginnungagap", "build-images"])
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "update", "--versions", "--json"],
        CommandResult(0, json.dumps(installed_response(ctx))),
    )
    ctx.remote.respond(
        ["sudo", "-n", "sh"],
        CommandResult(0, json.dumps(["ginnungagap:latest", "caddy:2"])),
    )


def strict_release(ctx):
    ctx.config = replace(
        ctx.config, image=replace(ctx.config.image, install_method="checkouts")
    )
    manifest = ctx.results["P2"]["manifest"]
    manifest["images"] = {
        service: {
            "ref": f"ghcr.io/htl3r-xeonis/ginnungagap-{service}@sha256:{number:064x}"
        }
        for number, service in enumerate(sorted(install._PREBUILT_SERVICES), 1)
    }
    path = Path(ctx.results["P2"]["directory"]) / "components.json"
    path.write_text(json.dumps(manifest))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ctx.results["P2"]["manifest_sha256"] = digest
    return {
        "schema": 1,
        "policy": "prebuilt-only",
        "appliance_version": ctx.version,
        "manifest_sha256": digest,
        "services": {
            service: {
                "ref": entry["ref"],
                "image_id": f"sha256:{number + 100:064x}",
                "compose_tag": f"ginnungagap-{service}:latest",
            }
            for number, (service, entry) in enumerate(manifest["images"].items())
        },
        "registry_aliases_removed": True,
        "upstream_pulled": True,
    }


def audit_output(audit):
    return install.PREBUILT_AUDIT_MARKER + json.dumps(audit) + "\n"


def strict_responses(
    ctx,
    audit,
    *,
    build_help="--prebuilt-only",
    prepare_help="--prebuilt-only",
    output=None,
):
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "build-images", "--help"],
        CommandResult(0, build_help),
    )
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "build-images", "--prebuilt-only"],
        CommandResult(0, audit_output(audit) if output is None else output),
    )
    ctx.remote.respond(
        [
            "sudo",
            "-n",
            "/var/lib/ginnungagap/components/Ginnungagap-Appliance/image/prepare-image.sh",
            "--help",
        ],
        CommandResult(0, prepare_help),
    )
    images = [item["compose_tag"] for item in audit["services"].values()]
    ctx.remote.respond(
        ["sudo", "-n", "sh"],
        CommandResult(0, json.dumps(images + ["caddy:2", "nginx:alpine"])),
    )
    responses(ctx)
    ctx.remote.prepared = CommandResult(
        0, audit_output(audit) + install.PREPARED_MARKER
    )


def test_guestinfo_and_key_are_installed_before_first_power_on(ctx):
    bootstrap(ctx)
    operations = ctx.esxi.names_called()
    assert (
        operations.index("set_extra_config")
        < operations.index("power_on")
        < operations.index("wait_for_ip")
    )
    saved = ctx.results["P5"]
    assert saved["target"]["user"] == "junioradmin"
    assert Path(saved["key_file"]).exists()
    assert json.loads(json.dumps(saved)) == saved
    assert all(secret not in repr(ctx.remote.calls) for secret in ctx.secrets)


@pytest.mark.parametrize(
    "mutation", [None, "wrong-marker", "static-marker", "missing-public"]
)
def test_inspection_uses_generated_marker_and_public_evidence_after_private_cleanup(
    ctx, monkeypatch, mutation
):
    from image.build import inspect_image

    bootstrap(ctx)
    saved = ctx.results["P5"]
    generated = yaml.safe_load(Path(saved["config_path"]).read_bytes())
    marker = generated["generator"]["llm_servers"][0]["name"]
    assert saved["config_marker"] == marker
    public = Path(saved["public_key_file"])
    public_text = public.read_text().strip()
    assert public.parent == ctx.run_dir / "evidence"
    assert public.parent not in ctx.tracked and public not in ctx.tracked
    if os.name == "posix":
        assert public.stat().st_mode & 0o777 == 0o600
    for _, _, cleanup in ctx.cleanups:
        cleanup()
    shutil.rmtree(Path(saved["key_file"]).parent)
    assert not Path(saved["key_file"]).exists()
    assert not Path(saved["key_file"] + ".pub").exists()
    assert public.read_text().strip() == public_text
    if mutation == "missing-public":
        public.unlink()
    elif mutation == "wrong-marker":
        saved["config_marker"] = remote.config_marker(
            NS(run_id="other-run", attempt=ctx.attempt)
        )
    elif mutation == "static-marker":
        saved["config_marker"] = inspect_image.CONFIG_MARKER
    (ctx.run_dir / "export").mkdir()
    ctx.results["P10"] = {"path": str(ctx.run_dir / "export/image.ova")}
    ctx.results["P8"] = {"images": ["ginnungagap:latest"]}
    ctx.note = lambda *args: None
    monkeypatch.setattr(inspect_image, "RUNS_ROOT", ctx.run_dir.parent)
    monkeypatch.setattr(
        config, "read_secret", lambda *args, **kwargs: "password-needle"
    )
    calls = []

    def helper(argv, **kwargs):
        request = json.loads((ctx.run_dir / "inspection-request.json").read_bytes())
        assert request["needles"] == [marker, public_text, "password-needle"]
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "passed": True,
                    "checks": [{"name": "required", "passed": True, "required": True}],
                }
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", helper)
    if mutation is None:
        assert inspect_image.inspect_phase(ctx)["passed"]
        assert calls
    else:
        with pytest.raises(PermanentError):
            inspect_image.inspect_phase(ctx)
        assert not calls


def test_missing_login_fails_before_creating_key_or_powering_on(ctx):
    from dataclasses import replace

    ctx.config = replace(ctx.config, image=replace(ctx.config.image, ssh_user=None))
    ctx.results["P3"].pop("username")
    with pytest.raises(PermanentError, match="ssh_user"):
        remote.bootstrap_base(ctx)
    assert not ctx.remote.calls
    assert "power_on" not in ctx.esxi.names_called()


def test_upload_detects_a_truncated_remote_asset(ctx):
    ctx.results["P5"] = remote.bootstrap_base(ctx)
    real = ctx.remote.put

    def truncated(target, local, destination, **kwargs):
        real(target, local, destination, **kwargs)
        ctx.remote.files[target.host, destination] = b""

    ctx.remote.put = truncated
    with pytest.raises(PermanentError, match="sizes"):
        remote.upload_release(ctx)


def test_update_uses_verified_offline_release_no_start_and_checks_all_commits(ctx):
    bootstrap(ctx)
    responses(ctx)
    result = install.install_release(ctx)
    command = next(
        c["argv"]
        for c in ctx.remote.calls
        if c["method"] == "run" and "--apply" in c["argv"]
    )
    assert "--offline-dir" in command and "--no-start" in command and "--yes" in command
    assert "GINNUNGAGAP_INSTALL_ARGS=--image" in command
    assert result["installed"] == ctx.version and len(result["components"]) == 5
    assert result["images"] == ["ginnungagap:latest", "caddy:2"]
    assert result["image_policy"] == "source"
    assert result["image_refs"] == {} and result["prebuilt_audit"] is None
    altered = installed_response(ctx)
    altered["installed"]["components"] = {
        **altered["installed"]["components"],
        "builder": {
            **altered["installed"]["components"]["builder"],
            "commit": "0" * 40,
        },
    }
    with pytest.raises(PermanentError, match="verified manifest"):
        install.verify_installed(
            json.dumps(altered), ctx.results["P2"]["manifest"], ctx.version
        )


@pytest.mark.parametrize(
    ("help_text", "build_local"),
    [
        ("usage: update --apply --no-start", False),
        ("options:\n  --build-local  build here instead of pulling\n", True),
        ("options:\n  --build-locally\n", False),
    ],
)
def test_factory_forces_source_build_only_when_installed_updater_advertises_it(
    ctx, help_text, build_local
):
    bootstrap(ctx)
    responses(ctx, help_text)
    install.install_release(ctx)
    commands = [c["argv"] for c in ctx.remote.calls if c["method"] == "run"]
    help_call = ["sudo", "-n", "ginnungagap", "update", "--help"]
    apply = next(command for command in commands if "--apply" in command)
    assert commands.index(help_call) < commands.index(apply)
    assert ("--build-local" in apply) is build_local
    assert all("--build-local" not in c for c in commands if c is not apply)


def test_failed_updater_capability_probe_never_starts_an_apply(ctx):
    bootstrap(ctx)
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "update", "--help"], CommandResult(2)
    )
    responses(ctx)
    with pytest.raises(CommandFailed):
        install.install_release(ctx)
    assert not any("--apply" in c.get("argv", []) for c in ctx.remote.calls)


def test_checkouts_fallback_uses_release_tarballs_and_records_manifest(ctx):
    from dataclasses import replace

    ctx.config = replace(
        ctx.config, image=replace(ctx.config.image, install_method="checkouts")
    )
    bootstrap(ctx)
    responses(ctx)
    result = install.install_release(ctx)
    commands = [c["argv"] for c in ctx.remote.calls if c["method"] == "run"]
    assert any(
        "--manifest" in c and "--image" in c and "--no-start" in c for c in commands
    )
    assert ["sudo", "-n", "ginnungagap", "build-images"] in commands
    assert (
        result["appliance_dir"]
        == "/var/lib/ginnungagap/components/Ginnungagap-Appliance"
    )
    assert any(
        c[:4] == ["sudo", "-n", "sh", "-c"] and "state_from_manifest" in c[4]
        for c in commands
    )


def test_prepare_checks_image_set_in_privileged_unit_before_sudoers_disappears(ctx):
    bootstrap(ctx)
    responses(ctx)
    ctx.results["P7"] = install.install_release(ctx)
    result = install.prepare_image(ctx)
    call = next(c for c in ctx.remote.calls if c["method"] == "run_detached")
    assert call["log_path"].startswith("/run/")
    assert call["argv"][0] == "python3"
    assert "docker" in call["argv"][2]
    assert result["images"] == ctx.results["P7"]["images"]
    assert result["image_policy"] == "source"
    assert result["image_refs"] == {} and result["prebuilt_audit"] is None
    assert call["argv"][-1] == ctx.results["P6"]["release_dir"]
    assert "--prebuilt-only" not in call["argv"]
    ctx.remote.prepared = CommandResult(0, "Ready. Shut this machine down now")
    with pytest.raises(PermanentError, match="preparation"):
        install.prepare_image(ctx)


def test_strict_release_requires_explicit_checkouts_before_any_guest_mutation(ctx):
    strict_release(ctx)
    ctx.config = replace(
        ctx.config, image=replace(ctx.config.image, install_method="update")
    )
    ctx.options = pipeline.RunOptions(allow_unsigned=False)
    bootstrap(ctx)
    before_calls = deepcopy(ctx.remote.calls)
    before_files = dict(ctx.remote.files)
    with pytest.raises(Blocked, match="explicit image.install_method=checkouts"):
        install.install_release(ctx)
    assert ctx.remote.calls == before_calls and ctx.remote.files == before_files


@pytest.mark.parametrize(
    "damage", ["missing", "extra", "foreign", "tag", "wrong-service", "not-object"]
)
def test_partial_or_unpinned_prebuilt_claims_never_start_installation(ctx, damage):
    strict_release(ctx)
    images = ctx.results["P2"]["manifest"]["images"]
    if damage == "missing":
        images.pop("setup")
    elif damage == "extra":
        images["unknown"] = images["setup"]
    elif damage == "foreign":
        images["setup"]["ref"] = "ghcr.io/another/setup@sha256:" + "a" * 64
    elif damage == "tag":
        images["setup"]["ref"] = "ghcr.io/htl3r-xeonis/ginnungagap-setup:1.3.8"
    elif damage == "wrong-service":
        images["setup"] = images["generator"]
    else:
        ctx.results["P2"]["manifest"]["images"] = []
    bootstrap(ctx)
    before = deepcopy(ctx.remote.calls)
    with pytest.raises(Blocked, match="prebuilt"):
        install.install_release(ctx)
    assert ctx.remote.calls == before


def test_strict_checkouts_use_capability_and_signed_refs_without_build_fallback(ctx):
    audit = strict_release(ctx)
    bootstrap(ctx)
    strict_responses(ctx, audit)
    ctx.results["P7"] = install.install_release(ctx)
    result = install.prepare_image(ctx)
    commands = [c["argv"] for c in ctx.remote.calls if c["method"] == "run"]
    install_call = next(c for c in commands if "--manifest" in c)
    help_call = ["sudo", "-n", "ginnungagap", "build-images", "--help"]
    pull_call = ["sudo", "-n", "ginnungagap", "build-images", "--prebuilt-only"]
    assert (
        commands.index(install_call)
        < commands.index(help_call)
        < commands.index(pull_call)
    )
    assert not any("--apply" in c or "--build-local" in c for c in commands)
    assert ["sudo", "-n", "ginnungagap", "build-images"] not in commands
    detached = next(c for c in ctx.remote.calls if c["method"] == "run_detached")
    assert detached["argv"][-1] == "--prebuilt-only"
    assert detached["argv"][-2] == ctx.results["P6"]["release_dir"]
    assert "*sys.argv[4:]" in detached["argv"][2]
    for phase in (ctx.results["P7"], result):
        assert phase["image_policy"] == "prebuilt-only"
        assert phase["image_refs"] == {
            service: entry["ref"] for service, entry in audit["services"].items()
        }
        assert phase["prebuilt_audit"] == audit


@pytest.mark.parametrize(
    "help_text", ["usage: build-images", "--prebuilt-only-fallback"]
)
def test_strict_installation_never_pulls_without_exact_capability(ctx, help_text):
    audit = strict_release(ctx)
    bootstrap(ctx)
    strict_responses(ctx, audit, build_help=help_text)
    with pytest.raises(Blocked, match="lacks prebuilt-only"):
        install.install_release(ctx)
    commands = [c["argv"] for c in ctx.remote.calls if c["method"] == "run"]
    assert not any("--prebuilt-only" in c for c in commands)
    assert ["sudo", "-n", "ginnungagap", "build-images"] not in commands


def test_failed_strict_cli_probe_cannot_fall_back_to_build(ctx):
    audit = strict_release(ctx)
    bootstrap(ctx)
    ctx.remote.respond(
        ["sudo", "-n", "ginnungagap", "build-images", "--help"], CommandResult(2)
    )
    strict_responses(ctx, audit)
    with pytest.raises(CommandFailed):
        install.install_release(ctx)
    assert not any("--prebuilt-only" in c.get("argv", []) for c in ctx.remote.calls)


@pytest.mark.parametrize("phase", ["P7", "P8"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "manifest",
        "version",
        "ref",
        "image-id",
        "tag",
        "missing-service",
        "upstream",
        "aliases",
        "extra-field",
        "extra-service-field",
    ],
)
def test_strict_audit_must_prove_every_signed_image_and_no_fallback(ctx, phase, damage):
    audit = strict_release(ctx)
    broken = deepcopy(audit)
    if damage == "manifest":
        broken["manifest_sha256"] = "0" * 64
    elif damage == "version":
        broken["appliance_version"] = "9.9.9"
    elif damage == "ref":
        broken["services"]["setup"]["ref"] = "wrong"
    elif damage == "image-id":
        broken["services"]["setup"]["image_id"] = "not-an-image-id"
    elif damage == "tag":
        broken["services"]["setup"]["compose_tag"] = "foreign:latest"
    elif damage == "missing-service":
        broken["services"].pop("setup")
    elif damage == "upstream":
        broken["upstream_pulled"] = False
    elif damage == "aliases":
        broken["registry_aliases_removed"] = False
    elif damage == "extra-field":
        broken["unexpected"] = "untrusted"
    elif damage == "extra-service-field":
        broken["services"]["setup"]["unexpected"] = "untrusted"
    output = audit_output(broken)
    if damage == "missing":
        output = "All images are in the local image store.\n"
    elif damage == "duplicate":
        output += output
    bootstrap(ctx)
    strict_responses(ctx, audit, output=output if phase == "P7" else None)
    if phase == "P7":
        with pytest.raises(PermanentError, match="prebuilt image audit"):
            install.install_release(ctx)
    else:
        ctx.results["P7"] = install.install_release(ctx)
        ctx.remote.prepared = CommandResult(0, output + install.PREPARED_MARKER)
        with pytest.raises(PermanentError, match="prebuilt image audit"):
            install.prepare_image(ctx)


def test_strict_preparation_rejects_missing_capability_before_detached_cleanup(ctx):
    audit = strict_release(ctx)
    bootstrap(ctx)
    strict_responses(ctx, audit, prepare_help="--prebuilt-only-optional")
    ctx.results["P7"] = install.install_release(ctx)
    with pytest.raises(Blocked, match="prepare script lacks prebuilt-only"):
        install.prepare_image(ctx)
    assert not any(c["method"] == "run_detached" for c in ctx.remote.calls)


def test_strict_preparation_rejects_image_identity_drift_after_valid_pull(ctx):
    audit = strict_release(ctx)
    bootstrap(ctx)
    strict_responses(ctx, audit)
    ctx.results["P7"] = install.install_release(ctx)
    changed = deepcopy(audit)
    changed["services"]["setup"]["image_id"] = "sha256:" + "f" * 64
    ctx.remote.prepared = CommandResult(
        0, audit_output(changed) + install.PREPARED_MARKER
    )
    with pytest.raises(PermanentError, match="identity changed"):
        install.prepare_image(ctx)


@pytest.mark.parametrize("damage", ["file", "metadata"])
def test_strict_installation_requires_original_verified_manifest_bytes(ctx, damage):
    strict_release(ctx)
    bootstrap(ctx)
    if damage == "file":
        (Path(ctx.results["P2"]["directory"]) / "components.json").write_text("{}")
    else:
        ctx.results["P2"]["manifest_sha256"] = "0" * 64
    before = deepcopy(ctx.remote.calls)
    with pytest.raises(Blocked, match="differ from the verified release manifest"):
        install.install_release(ctx)
    assert ctx.remote.calls == before


def test_shutdown_clears_guestinfo_and_does_not_boot_prepared_image(ctx):
    bootstrap(ctx)
    ctx.esxi.inject("shutdown_guest", ToolsUnavailable("no tools"))
    before = ctx.esxi.names_called().count("power_on")
    result = esxi.shutdown_base(ctx)
    assert result["powered_off"]
    assert "power_off" in ctx.esxi.names_called()
    assert not any(ctx.esxi.get_extra_config(ctx.results["P4"]["vm"]).values())
    assert ctx.esxi.names_called().count("power_on") == before


def ova(path, *, corrupt=False, descriptor_first=True):
    descriptor = b'<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"><References><File ovf:href="disk.vmdk" /></References></Envelope>'
    disk = b"disk-content"
    values = {"image.ovf": descriptor, "disk.vmdk": disk}
    manifest = "".join(
        f"SHA256({name})= {hashlib.sha256(value).hexdigest()}\n"
        for name, value in values.items()
    ).encode()
    if corrupt:
        disk = b"changed"
    ordered = [("image.ovf", descriptor), ("image.mf", manifest), ("disk.vmdk", disk)]
    if not descriptor_first:
        ordered.reverse()
    with tarfile.open(path, "w") as archive:
        for name, value in ordered:
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))


def test_ova_verification_checks_all_hashes_and_ovf_first(tmp_path):
    path = tmp_path / "image.ova"
    ova(path)
    install.verify_ova(path, minimum_bytes=0)
    ova(path, corrupt=True)
    with pytest.raises(PermanentError, match="validation"):
        install.verify_ova(path, minimum_bytes=0)
    ova(path, descriptor_first=False)
    with pytest.raises(PermanentError):
        install.verify_ova(path, minimum_bytes=0)
    path.write_bytes(b"not a tar")
    with pytest.raises(PermanentError):
        install.verify_ova(path, minimum_bytes=0)


def recorded_export(root, run_id, *, status="failed", validated=True, tracked=True):
    directory = root / "runs" / run_id / "export"
    directory.mkdir(parents=True)
    ova = directory / "image.ova"
    ova.write_bytes(b"validated image " + run_id.encode())
    state = {
        "run_id": run_id,
        "status": status,
        "started_at": run_id.split("-")[-1],
        "phases": {},
        "resources": [],
    }
    if validated:
        state["phases"]["P10"] = {
            "status": "ok",
            "result": {
                "path": str(ova),
                "size": ova.stat().st_size,
                "sha256": hashlib.sha256(ova.read_bytes()).hexdigest(),
            },
        }
    if tracked:
        state["resources"] = [
            {"kind": "export_artifact", "ref": str(directory), "status": "open"}
        ]
    (directory.parent / "state.json").write_text(json.dumps(state))
    return directory


def test_successful_export_is_deleted_only_after_publish_and_failure_remains_resumable(
    tmp_path,
):
    current = recorded_export(tmp_path, RUN, status="running")
    previous = recorded_export(tmp_path, "1.2.0-20261119T120000Z")
    active = recorded_export(tmp_path, "1.4.0-20261121T120000Z", status="running")
    foreign = recorded_export(tmp_path, "1.1.0-20261118T120000Z", tracked=False)
    esxi.cleanup_export_artifacts(tmp_path, current, published=False)
    assert (
        (current / "image.ova").exists()
        and not previous.exists()
        and active.exists()
        and foreign.exists()
    )
    esxi.cleanup_export_artifacts(tmp_path, current, published=True)
    assert not current.exists()


@pytest.mark.parametrize("damage", ["partial", "modified"])
def test_final_failure_partial_or_changed_ova_does_not_evict_previous_verified_export(
    tmp_path, damage
):
    good = recorded_export(tmp_path, "1.2.0-20261119T120000Z")
    current = recorded_export(
        tmp_path, RUN, status="running", validated=damage != "partial"
    )
    if damage == "modified":
        ova = current / "image.ova"
        ova.write_bytes(b"x" * ova.stat().st_size)
    esxi.cleanup_export_artifacts(tmp_path, current)
    assert good.exists() and not current.exists()


def test_final_failure_cleanup_refuses_unrecorded_or_symlinked_export(tmp_path):
    unrecorded = recorded_export(tmp_path, RUN, tracked=False)
    for published in (False, True):
        with pytest.raises(esxi.NotOurs, match="unrecorded"):
            esxi.cleanup_export_artifacts(tmp_path, unrecorded, published=published)
    outside = tmp_path / "foreign-export"
    outside.mkdir()
    (outside / "important.ova").write_bytes(b"foreign")
    unrecorded.rename(unrecorded.with_name("saved-export"))
    unrecorded.symlink_to(outside, target_is_directory=True)
    with pytest.raises(esxi.NotOurs):
        esxi.cleanup_export_artifacts(tmp_path, unrecorded, published=True)
    assert (outside / "important.ova").read_bytes() == b"foreign"


def test_p4_tracks_cleanup_before_import_and_returns_datastore(ctx):
    ctx.esxi.delete_vm(ctx.results["P4"]["vm"])
    trace = []
    real = ctx.esxi.import_ova
    ctx.track_vm = lambda name: trace.append("track")

    def imported(*args, **kwargs):
        trace.append("import")
        return real(*args, **kwargs)

    ctx.esxi.import_ova = imported
    result = esxi.deploy_base(ctx)
    assert trace == ["track", "import"]
    assert result["datastore"] == "Datastore"


def test_invalid_export_is_not_deleted_before_diagnostics_can_read_it(ctx):
    name = ctx.results["P4"]["vm"]
    with pytest.raises(PermanentError, match="implausible size"):
        esxi.export_base(ctx)
    assert ctx.esxi.get_vm(name) is not None
    assert any(kind == "export_artifact" for kind, _, _ in ctx.cleanups)


def test_guestinfo_clear_cli_does_not_require_configuration_or_power_on(monkeypatch):
    pytest.importorskip("pyVmomi")
    from unittest.mock import Mock

    from image import set_guestinfo

    vm = Mock()
    vm.runtime.powerState = "poweredOff"
    vm.config.extraConfig = [
        NS(key="guestinfo.ginnungagap.config", value="private"),
        NS(key="foreign.key", value="leave"),
    ]
    monkeypatch.setattr(
        set_guestinfo.common, "password_from_env", lambda: "not-printed"
    )
    monkeypatch.setattr(set_guestinfo.common, "connect", lambda *a: object())
    monkeypatch.setattr(set_guestinfo.common, "find_vm", lambda *a: vm)
    monkeypatch.setattr(set_guestinfo.common, "wait_for_task", lambda *a: None)
    assert (
        set_guestinfo.main(
            ["--host", "192.0.2.1", "--vm", names.build_vm_name(RUN), "--clear"]
        )
        == 0
    )
    spec = vm.ReconfigVM_Task.call_args.args[0]
    assert [(item.key, item.value) for item in spec.extraConfig] == [
        ("guestinfo.ginnungagap.config", "")
    ]
    vm.PowerOnVM_Task.assert_not_called()
    with pytest.raises(SystemExit):
        set_guestinfo.main(
            ["--host", "192.0.2.1", "--vm", "vm", "--clear", "--power-on"]
        )


@pytest.mark.skipif(os.name == "nt", reason="the guest recorder creates Unix symlinks")
def test_checkout_recorder_writes_real_manifest_state_after_build(
    ctx, tmp_path, monkeypatch
):
    from ginnungagap import manifest as mf

    sources = tmp_path / "repos"
    state = tmp_path / "guest-state"
    env = tmp_path / "install.env"
    env.write_text("REPOS_DIR=/old\nPRESERVED=yes\n")
    for c in ctx.results["P2"]["manifest"]["components"].values():
        source = sources / c["repo"].split("/")[-1]
        source.mkdir(parents=True)
        (source / "verified-source").write_text(c["sha256"])
    code = install._RECORD_CHECKOUTS.replace(
        "pathlib.Path('/srv/repos')", f"pathlib.Path({str(sources)!r})"
    ).replace(
        "pathlib.Path('/etc/ginnungagap/install.env')", f"pathlib.Path({str(env)!r})"
    )
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(state / "update"))
    subprocess.run(
        [sys.executable, "-c", code, ctx.results["P2"]["directory"]], check=True
    )
    recorded = mf.load_installed(state / "update/installed.json")
    assert recorded.appliance_version == ctx.version and recorded.previous is None
    assert set(recorded.components) == set(ctx.results["P2"]["manifest"]["components"])
    assert all(
        (state / "components" / mf.CHECKOUT_NAMES[role]).is_symlink()
        for role in recorded.components
    )
    assert (
        "PRESERVED=yes" in env.read_text()
        and str(state / "components") in env.read_text()
    )


def test_bootstrap_checks_root_private_firstboot_marker_with_sudo(ctx):
    remote.bootstrap_base(ctx)
    probes = [
        c
        for c in ctx.remote.calls
        if c["method"] == "run"
        and "/var/lib/ginnungagap/firstboot.done" in c.get("argv", [])
    ]
    assert probes
    assert probes[0]["argv"][:3] == ["sudo", "-n", "test"]


@pytest.mark.parametrize(
    "run_id", ["1.3.5-rc.20261121-20261121T122346Z", "1.3.5-20261121T122346Z"]
)
@pytest.mark.parametrize("allow_unsigned", [True, False])
def test_generated_bootstrap_configuration_passes_real_appliance_validator(
    ctx, run_id, allow_unsigned
):
    import re

    import yaml

    from ginnungagap.config import normalise

    ctx.run_id = run_id
    ctx.options = pipeline.RunOptions(allow_unsigned=allow_unsigned)
    config = yaml.safe_load(remote.minimal_config(ctx))
    checked = normalise(config)
    marker = checked["generator"]["llm_servers"][0]["name"]
    assert re.fullmatch(r"[A-Za-z0-9_-]+", marker)
    assert marker == remote.config_marker(ctx)
    result = remote.bootstrap_base(ctx)
    assert result["config_marker"] == marker
    ctx.attempt += 1
    assert remote.config_marker(ctx) != marker


def test_rejected_firstboot_configuration_fails_without_waiting_or_echoing_guest_text(
    ctx,
):
    ctx.remote.responses.insert(
        0,
        [
            ("sudo", "-n", "test", "-f", "/var/lib/ginnungagap/firstboot.done"),
            CommandResult(1),
            None,
        ],
    )
    ctx.remote.respond(
        ["sudo", "-n", "test", "-f", "/var/lib/ginnungagap/firstboot.problems"],
        CommandResult(0, "private guest diagnostic"),
    )
    before = ctx.clock.monotonic()
    with pytest.raises(
        PermanentError, match="rejected its first-boot configuration"
    ) as caught:
        remote.bootstrap_base(ctx)
    assert ctx.clock.monotonic() - before < 10  # only fake IP discovery, no 180s wait
    assert "private guest diagnostic" not in str(caught.value)


@pytest.fixture
def static_ctx(ctx, monkeypatch):
    from dataclasses import replace

    network = config.ManagementNetwork(
        "10.20.20.184", "10.20.20.185", 24, "10.20.20.254", ("1.1.1.1", "8.8.8.8")
    )
    ctx.config = replace(
        ctx.config, image=replace(ctx.config.image, management_network=network)
    )
    monkeypatch.setattr(remote, "_worker_interface", lambda network: "ens160")
    ctx.remote.respond(
        ["python3", "-c"],
        CommandResult(
            0,
            json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "10.20.20.184", "prefixlen": 24}
                        ]
                    }
                ]
            ),
        ),
    )
    return ctx


def test_static_bootstrap_uses_link_local_then_verifies_reserved_ipv4_and_preserves_key(
    static_ctx,
):
    ctx = static_ctx
    result = remote.bootstrap_base(ctx)
    assert result["target"]["host"] == "10.20.20.184"
    assert "wait_for_link_local" in ctx.esxi.names_called()
    assert "wait_for_ip" not in ctx.esxi.names_called()
    guests = ctx.esxi.get_extra_config(ctx.results["P4"]["vm"])
    assert (
        json.loads(guests["guestinfo.ginnungagap.network"])["address"] == "10.20.20.184"
    )
    waits = [c["host"] for c in ctx.remote.calls if c["method"] == "wait_ssh"]
    assert waits[0].startswith("fe80::") and waits[0].endswith("%ens160")
    assert waits[1:] == ["10.20.20.184", "10.20.20.184"]
    assert Path(result["key_file"]).exists()
    closed = [c["host"] for c in ctx.remote.calls if c["method"] == "close_master"]
    assert closed == [waits[0]]
    detached = next(c for c in ctx.remote.calls if c["method"] == "run_detached")
    assert json.loads(detached["argv"][3])["address"] == "10.20.20.184"
    assert "IMGBUILD_NETWORK_APPLIED" in detached["argv"][2]


def test_static_bootstrap_rejects_unconfirmed_address_without_disclosing_or_deleting_key(
    static_ctx,
):
    ctx = static_ctx
    ctx.remote.responses.insert(0, [("python3", "-c"), CommandResult(0, "[]"), None])
    with pytest.raises(PermanentError, match="did not confirm"):
        remote.bootstrap_base(ctx)
    assert any(c["method"] == "close_master" for c in ctx.remote.calls)
    assert list((ctx.run_dir / "key-attempt-1").glob("id_ed25519"))
