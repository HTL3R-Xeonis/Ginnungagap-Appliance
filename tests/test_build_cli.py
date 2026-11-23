from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest
import yaml
from helpers import fake_release

from image import build_image as cli
from image.build.config import ConfigError, parse_config


@pytest.fixture
def config_file(tmp_path):
    config = {
        "esxi": {"datastore": "build-datastore", "network": "build-network"},
        "paths": {"state_dir": str(tmp_path / "state")},
    }
    path = tmp_path / "build-host.yml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_status_needs_no_secrets_or_network(config_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "runtime", lambda *a: pytest.fail("status contacted ESXi"))
    assert cli.main(["--config", str(config_file), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["latest"] is None


def test_status_exposes_running_job_beside_previous_summary(tmp_path, monkeypatch):
    config = parse_config(
        {
            "esxi": {"datastore": "ds", "network": "net"},
            "paths": {"state_dir": str(tmp_path)},
        }
    )
    previous = {"run_id": "previous", "outcome": "interrupted"}
    (tmp_path / "latest.json").write_text(json.dumps(previous))
    current = {"run_id": "new", "status": "running", "phases": {"P4": "running"}}
    monkeypatch.setattr(cli, "list_runs", lambda path: [SimpleNamespace(data=current)])
    result = cli.status(config)
    assert result["latest"] == previous
    assert result["current"] == current


def test_manual_options_are_passed_to_the_pipeline(config_file, monkeypatch):
    recorded = []
    outcome = SimpleNamespace(status="stopped", reason="P8", exit_code=0, run_id=None)

    def execute(config, version, options):
        recorded.append((version, options))
        return outcome

    monkeypatch.setattr(cli, "execute", execute)
    assert (
        cli.main(
            [
                "--config",
                str(config_file),
                "run",
                "--release",
                "v1.3.1-rc.1",
                "--allow-prerelease",
                "--allow-unsigned",
                "--stop-after",
                "P8",
                "--keep",
            ]
        )
        == 0
    )
    version, options = recorded[0]
    assert version == "1.3.1-rc.1"
    assert options.allow_prerelease and options.allow_unsigned and options.keep
    assert options.stop_after == "P8"


def test_catalog_refresh_requires_an_explicit_manual_resume(config_file, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        cli,
        "execute",
        lambda *a: (
            recorded.append(a)
            or SimpleNamespace(
                status="stopped", reason="done", exit_code=0, run_id=None
            )
        ),
    )
    arguments = [
        "--config",
        str(config_file),
        "run",
        "--release",
        "v1.3.1",
        "--refresh-catalog",
    ]
    assert cli.main(arguments) == 2
    assert not recorded
    assert cli.main(arguments + ["--resume", "1.3.1-20261120T100000Z"]) == 0
    assert recorded[0][2].refresh_catalog is True
    assert recorded[0][2].resume == "1.3.1-20261120T100000Z"
    with pytest.raises(cli.PermanentError, match="requires --resume"):
        cli.RunOptions(refresh_catalog=True)


@pytest.mark.parametrize(
    "extra",
    [
        {"refresh_catalog": True},
        {"refresh_catalog": True, "resume": "1.3.1-20261120T100000Z"},
    ],
)
def test_automatic_queue_cannot_inject_catalog_refresh(config_file, extra):
    config = cli.load_config(config_file, check_secrets=False)
    cli.write_json(
        config.paths.state_dir / "queue/pending.json",
        {
            "version": "1.3.1",
            "queued_at": "now",
            **extra,
        },
    )
    with pytest.raises(cli.PermanentError, match="queue record"):
        cli.work(config)


@pytest.mark.parametrize("value", ["../bad", "1.3.1-r2", "1.3.1;echo bad"])
def test_invalid_release_cannot_reach_the_pipeline(config_file, monkeypatch, value):
    monkeypatch.setattr(
        cli, "execute", lambda *a: pytest.fail("invalid release executed")
    )
    assert cli.main(["--config", str(config_file), "run", "--release", value]) == 2


def test_poll_queues_once_and_worker_never_gets_manual_exceptions(
    config_file, monkeypatch
):
    config = cli.load_config(config_file, check_secrets=False)
    release = SimpleNamespace(
        select_release=lambda *a, **k: SimpleNamespace(version="1.3.1")
    )
    publish = SimpleNamespace(TemplateApi=lambda url: object())
    modules = {"image.build.release": release, "image.build.publish": publish}
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: modules[name])
    assert cli.poll(config) == 0
    pending = config.paths.state_dir / "queue/pending.json"
    first = pending.read_bytes()
    assert cli.poll(config) == 0
    assert pending.read_bytes() == first
    seen = []

    def execute(cfg, version, options):
        seen.append((version, options))
        return SimpleNamespace(
            status="deferred", reason="lab busy", run_id=None, exit_code=3
        )

    monkeypatch.setattr(cli, "execute", execute)
    assert cli.work(config) == 3
    assert not pending.exists()
    assert not (pending.parent / "active.json").exists()
    assert seen[0][0] == "1.3.1"
    assert not seen[0][1].allow_unsigned and not seen[0][1].allow_prerelease
    assert seen[0][1].refresh_catalog is False


def test_crashed_worker_recovers_the_active_record(config_file, monkeypatch):
    config = cli.load_config(config_file, check_secrets=False)
    active = config.paths.state_dir / "queue/active.json"
    cli.write_json(active, {"version": "1.3.1", "queued_at": "2026-11-20T00:00:00Z"})
    monkeypatch.setattr(
        cli,
        "execute",
        lambda *a: SimpleNamespace(
            status="published", reason="done", run_id=None, exit_code=0
        ),
    )
    assert cli.work(config) == 0
    assert not active.exists()


def test_auto_worker_rejects_prereleases_and_injected_options(config_file):
    config = cli.load_config(config_file, check_secrets=False)
    pending = config.paths.state_dir / "queue/pending.json"
    cli.write_json(pending, {"version": "1.3.1-rc.1", "queued_at": "now"})
    with pytest.raises(cli.PermanentError, match="prereleases"):
        cli.work(config)
    cli.write_json(
        pending, {"version": "1.3.1", "queued_at": "now", "allow_unsigned": True}
    )
    with pytest.raises(cli.PermanentError, match="queue record"):
        cli.work(config)


def test_pause_does_not_queue_or_contact_release_source(config_file, monkeypatch):
    config = cli.load_config(config_file, check_secrets=False)
    config.paths.state_dir.mkdir()
    (config.paths.state_dir / "PAUSE").touch()
    # Module import is harmless; source methods must not be called.
    monkeypatch.setattr(cli.importlib, "import_module", lambda *a: object())
    assert cli.poll(config) == 3
    assert not (config.paths.state_dir / "queue").exists()


@pytest.mark.parametrize(
    "command,args", [("logs", ["../../etc/shadow"]), ("show", ["../bad"])]
)
def test_status_paths_cannot_escape_state(config_file, command, args):
    assert cli.main(["--config", str(config_file), command, *args]) == 2


def test_self_update_requires_root_before_fetching(config_file, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert (
        cli.main(["--config", str(config_file), "self-update", "--release", "v1.3.1"])
        == 2
    )


def test_explicit_catalog_login_override_is_validated():
    data = {
        "esxi": {"datastore": "ds", "network": "net"},
        "image": {"ssh_user": "imageadmin"},
    }
    assert parse_config(data).image.ssh_user == "imageadmin"
    data["image"]["ssh_user"] = "root@other-host"
    with pytest.raises(ConfigError, match="ssh_user"):
        parse_config(data)


def test_systemd_credential_overrides_original_token_path(
    config_file, tmp_path, monkeypatch
):
    directory = tmp_path / "credentials"
    directory.mkdir()
    (directory / "github-token").write_text("not-used-by-this-test")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    seen = []
    monkeypatch.setattr(
        cli, "poll", lambda config: seen.append(config.github.token_file) or 0
    )
    assert cli.main(["--config", str(config_file), "poll"]) == 0
    assert seen == [directory / "github-token"]


def test_signed_self_update_extracts_versioned_roots_before_atomic_activation(
    tmp_path, monkeypatch
):
    release = fake_release.make_release(
        tmp_path / "release", appliance="1.4.0", sign=True
    )
    manifest = json.loads((release / "components.json").read_text())
    asset = release / manifest["components"]["appliance"]["asset"]
    with tarfile.open(asset) as archive:
        files = {
            m.name.split("/", 1)[1]: archive.extractfile(m).read()
            for m in archive
            if m.isfile()
        }
    files["image/build_image.py"] = b"print('trusted tooling')\n"
    fake_release.write_tarball(asset, "Ginnungagap-Appliance-1.4.0", files)
    manifest["components"]["appliance"].update(
        sha256=hashlib.sha256(asset.read_bytes()).hexdigest(), size=asset.stat().st_size
    )
    (release / "components.json").write_text(json.dumps(manifest) + "\n")
    summed = ["components.json", *(c["asset"] for c in manifest["components"].values())]
    (release / "SHA256SUMS").write_text(
        "".join(
            f"{fake_release.sha256_of(release / name)}  {name}\n"
            for name in sorted(summed)
        )
    )
    fake_release.sign_file(
        fake_release.keys_of(release).parent / "release_key",
        release / "components.json",
    )
    root = tmp_path / "installed"
    root.mkdir()
    config = parse_config(
        {
            "esxi": {"datastore": "ds", "network": "net"},
            "paths": {
                "state_dir": str(tmp_path / "state"),
                "allowed_signers": str(fake_release.keys_of(release)),
            },
        }
    )
    monkeypatch.setattr(cli, "TOOLING_ROOT", root)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    created = []
    monkeypatch.setattr(
        cli.venv,
        "EnvBuilder",
        lambda **kw: SimpleNamespace(
            create=lambda directory: created.append(directory)
        ),
    )
    original_run = subprocess.run
    commands = []

    def run(command, **kwargs):
        if command[0] == "ssh-keygen":
            return original_run(command, **kwargs)
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.self_update(config, "1.4.0", release) == 0
    assert (root / "current").resolve() == root / "versions/1.4.0"
    assert (root / "current/appliance/image/build_image.py").read_bytes() == files[
        "image/build_image.py"
    ]
    assert (root / "current/builder").is_dir()
    assert created == [root / "versions/1.4.0/venv"]
    assert commands[0][1:4] == ["-m", "pip", "install"]
    assert commands[-1][-1] == "--help"
