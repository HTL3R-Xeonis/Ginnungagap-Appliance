"""Protected systemd credential mounts and durable detached completion."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from image.build import config, ports, release, remote
from tests.helpers.fake_esxi import FakeClock


def metadata(mode, uid=0, gid=0, nlink=1):
    return NS(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=nlink, st_size=5)


def credential_mount(monkeypatch, *, readonly=True, parent_mode=0o550):
    file = metadata(stat.S_IFREG | 0o440)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda p: file if p.name == "esxi205" else metadata(stat.S_IFDIR | parent_mode),
    )
    monkeypatch.setattr(
        os, "statvfs", lambda p: NS(f_flag=os.ST_RDONLY if readonly else 0)
    )
    return file


def test_readonly_root_owned_systemd_credential_is_accepted(monkeypatch):
    credential_mount(monkeypatch)
    path = Path("/run/credentials/image-build.service/esxi205")
    assert config.check_secret_file(path) == path


@pytest.mark.parametrize(
    "path,readonly,parent_mode",
    [
        ("/tmp/credentials/service/esxi205", True, 0o550),
        ("/run/credentials/service/esxi205", False, 0o550),
        ("/run/credentials/service/esxi205", True, 0o570),
        ("/run/credentials/service/../esxi205", True, 0o550),
    ],
)
def test_ordinary_or_unprotected_group_readable_files_stay_rejected(
    monkeypatch, path, readonly, parent_mode
):
    credential_mount(monkeypatch, readonly=readonly, parent_mode=parent_mode)
    with pytest.raises(config.SecretFileError):
        config.check_secret_file(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("st_uid", 1000),
        ("st_gid", 1000),
        ("st_nlink", 2),
        ("st_mode", stat.S_IFREG | 0o444),
    ],
)
def test_systemd_exception_requires_exact_metadata(monkeypatch, field, value):
    file = credential_mount(monkeypatch)
    setattr(file, field, value)
    with pytest.raises(config.SecretFileError):
        config.check_secret_file("/run/credentials/service/esxi205")


def test_build_source_token_uses_checked_credential_reader(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("unused")
    seen = []
    monkeypatch.setattr(
        release, "read_secret", lambda p: seen.append(p) or "  example_token-123\n"
    )
    source = release.BuildGitHubSource("org/repo", token_file=path)
    assert source.token == "example_token-123"
    assert source.token == "example_token-123"
    assert seen == [path]


@pytest.mark.parametrize("token", ["", "two words", "line\nline"])
def test_build_source_rejects_bad_token_contents(tmp_path, monkeypatch, token):
    path = tmp_path / "token"
    path.write_text("unused")
    monkeypatch.setattr(release, "read_secret", lambda p: token)
    with pytest.raises(release.SourceError):
        _ = release.BuildGitHubSource("org/repo", token_file=path).token


@pytest.mark.parametrize(
    "completion,marker,error",
    [
        ({"nonce": "fresh", "returncode": 0}, "Ready.", None),
        ({"nonce": "fresh", "returncode": 7}, "Ready.", ports.CommandFailed),
        ({"nonce": "old", "returncode": 0}, "Ready.", ports.PermanentError),
        ({"nonce": "fresh", "returncode": True}, "Ready.", ports.PermanentError),
        ({"nonce": "fresh", "returncode": 0}, "working", ports.PermanentError),
        (None, "Ready.", ports.Timeout),
    ],
)
def test_detached_result_survives_collected_unit_but_requires_fresh_metadata(
    tmp_path, monkeypatch, completion, marker, error
):
    monkeypatch.setattr(remote.uuid, "uuid4", lambda: NS(hex="fresh"))
    adapter = remote.SshRemote(clock=FakeClock())
    seen = []

    def run(target, argv, **kwargs):
        seen.append(argv)
        return ports.CommandResult(
            0,
            json.dumps(
                {"fields": {}, "status": 1, "log": marker, "completion": completion}
            ),
        )

    adapter.run = run
    args = (NS(), "imgprep-test", ["/prepare.sh"])
    kwargs = {
        "log_path": "/run/imgprep-test.log",
        "timeout": 2,
        "success_marker": "Ready.",
        "poll_seconds": 1,
    }
    if error:
        with pytest.raises(error):
            adapter.run_detached(*args, **kwargs)
    else:
        assert adapter.run_detached(*args, **kwargs).ok
    unit_command = next(c for c in seen if "systemd-run" in c)
    assert "--collect" in unit_command


def management_settings(**overrides):
    values = {
        "build_address": "10.20.20.184",
        "acceptance_address": "10.20.20.185",
        "prefix": 24,
        "gateway": "10.20.20.254",
        "dns": ["1.1.1.1", "8.8.8.8"],
    }
    values.update(overrides)
    return {
        "esxi": {"datastore": "ds", "network": "VM Network"},
        "image": {"management_network": values},
    }


def test_static_network_is_optional_and_typed():
    assert (
        config.parse_config(
            {"esxi": {"datastore": "ds", "network": "net"}}
        ).image.management_network
        is None
    )
    network = config.parse_config(management_settings()).image.management_network
    assert network.build_address == "10.20.20.184"
    assert network.acceptance_address == "10.20.20.185"
    assert network.dns == ("1.1.1.1", "8.8.8.8")


@pytest.mark.parametrize(
    "changes",
    [
        {"build_address": "10.20.20.185"},
        {"build_address": "10.20.20.0"},
        {"acceptance_address": "10.20.20.255"},
        {"gateway": "10.20.21.254"},
        {"gateway": "10.20.20.184"},
        {"prefix": 33},
        {"prefix": True},
        {"dns": []},
        {"dns": ["not-an-ip"]},
        {"dns": ["2001:db8::53"]},
        {"dns": ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"]},
        {"build_address": "fe80::1"},
        {"acceptance_address": "127.0.0.1"},
        {"unexpected": "typo"},
    ],
)
def test_static_network_rejects_invalid_or_overlapping_addresses(changes):
    with pytest.raises(config.ConfigError):
        config.parse_config(management_settings(**changes))


@pytest.mark.parametrize(
    "addresses,expected",
    [
        ([("ens160", "10.20.20.183")], "ens160"),
        ([("ens160", "10.20.20.184")], None),
        ([("ens160", "10.20.21.183")], None),
        ([("ens160", "10.20.20.183"), ("ens192", "10.20.20.186")], None),
    ],
)
def test_worker_link_local_zone_requires_one_interface_and_does_not_reuse_worker_ip(
    monkeypatch, addresses, expected
):
    data = [
        {
            "ifname": name,
            "flags": ["UP"],
            "addr_info": [{"family": "inet", "local": address}],
        }
        for name, address in addresses
    ]
    monkeypatch.setattr(
        remote.subprocess, "run", lambda *args, **kw: NS(stdout=json.dumps(data))
    )
    network = config.parse_config(management_settings()).image.management_network
    if expected:
        assert remote._worker_interface(network) == expected
    else:
        with pytest.raises(ports.PermanentError):
            remote._worker_interface(network)
