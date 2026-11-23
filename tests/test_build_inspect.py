"""Inspection rejects seeded leftovers and never extracts archive paths."""

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from image.build import inspect_image as inspect
from image.build.ports import PermanentError


def write(root, name, content=b""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


@pytest.fixture
def clean_tree(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    write(root, "etc/shadow", "root:!:1:0:99999:7:::\nadmin:*:1:0:99999:7:::\n")
    write(root, "etc/machine-id")
    write(root, "etc/sudoers", "root ALL=(ALL:ALL) ALL\n")
    write(root, "etc/ginnungagap/install.env", "PYTHONPATH=/opt/ginnungagap/app\n")
    write(root, "var/lib/ginnungagap/update/installed.json", '{"previous":null}')
    write(
        root,
        "var/lib/docker/image/overlay2/repositories.json",
        json.dumps({"Repositories": {"ginn-app": {"ginn-app:latest": "sha256:a"}}}),
    )
    return root


def test_clean_tree_and_docker_volume_index_are_accepted(clean_tree):
    write(clean_tree, "var/lib/docker/volumes/metadata.db", b"index")
    checks = inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    assert checks and all(item["passed"] for item in checks), checks


def test_network_inspection_uses_the_exact_temporary_profile_paths_and_keeps_original_profiles(
    clean_tree,
):
    from ginnungagap import image_network

    assert set(inspect.TEMPORARY_NETWORK_FILES) == {
        image_network.NM_FILE,
        image_network.NETWORKD_FILE,
        image_network.BACKUP,
    }
    write(
        clean_tree, "etc/systemd/network/10-original.network", "[Network]\nDHCP=yes\n"
    )
    write(
        clean_tree,
        "etc/NetworkManager/system-connections/original.nmconnection",
        "[ipv4]\nmethod=manual\n",
    )
    assert all(
        check["passed"]
        for check in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    )


@pytest.mark.parametrize("relative", inspect.TEMPORARY_NETWORK_FILES)
@pytest.mark.parametrize("linked", [False, True])
def test_temporary_network_profile_or_dangling_link_blocks_the_export(
    clean_tree, relative, linked
):
    if linked and os.name != "posix":
        pytest.skip("requires symlinks")
    path = write(clean_tree, relative)  # even an empty regular override must fail
    if linked:
        path.unlink()
        path.symlink_to(clean_tree / "does-not-exist")
    checks = inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    assert not next(
        c for c in checks if c["name"] == "no temporary management network overrides"
    )["passed"]


@pytest.mark.parametrize("same_image", [True, False])
def test_digest_alias_is_ignored_only_for_the_same_tagged_image(clean_tree, same_image):
    path = clean_tree / "var/lib/docker/image/overlay2/repositories.json"
    path.write_text(
        json.dumps(
            {
                "Repositories": {
                    "ginn-app": {
                        "ginn-app:latest": "sha256:a",
                        "ginn-app@sha256:" + "a" * 64: "sha256:a"
                        if same_image
                        else "sha256:b",
                    }
                }
            }
        )
    )
    check = next(
        c
        for c in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
        if c["name"] == "exact compose image set"
    )
    assert check["passed"] is same_image


def test_digest_without_named_tag_is_not_a_hidden_image():
    digest = "ginn-app@sha256:" + "a" * 64
    assert inspect.repository_tags(
        {"Repositories": {"ginn-app": {digest: "sha256:a"}}}
    ) == {digest}


def current_history_fixture(root):
    name = "20261120T100000Z-1.4.0.json"
    installed = {
        "entry": name,
        "appliance": {"version": "1.4.0"},
        "components": {"appliance": {"version": "1.4.0"}},
        "previous": None,
        "previous_entry": None,
        "backup": None,
    }
    write(root, "var/lib/ginnungagap/update/installed.json", json.dumps(installed))
    retained = {
        **installed,
        "previous": "1.3.0",
        "previous_entry": "old.json",
        "backup": "/var/lib/ginnungagap/backups/old",
    }
    path = write(
        root, "var/lib/ginnungagap/update/history/" + name, json.dumps(retained)
    )
    return installed, path


def test_only_the_exact_current_rollback_seed_is_valid_history(clean_tree):
    current_history_fixture(clean_tree)
    checks = inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    assert all(item["passed"] for item in checks), checks


@pytest.mark.parametrize(
    "mutation",
    [
        "older-file",
        "different-version",
        "different-component",
        "invalid-json",
        "directory",
        "symlink",
    ],
)
def test_old_or_substituted_runtime_history_fails(clean_tree, mutation):
    if mutation == "symlink" and os.name != "posix":
        pytest.skip("symlink needs POSIX")
    installed, path = current_history_fixture(clean_tree)
    if mutation == "older-file":
        write(
            clean_tree,
            "var/lib/ginnungagap/update/history/20261119T100000Z-1.3.0.json",
            "{}",
        )
    elif mutation == "invalid-json":
        path.write_text("invalid")
    elif mutation == "directory":
        path.unlink()
        path.mkdir()
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(clean_tree / "var/lib/ginnungagap/update/installed.json")
    else:
        retained = json.loads(path.read_text())
        if mutation == "different-version":
            retained["appliance"]["version"] = "1.3.0"
        else:
            retained["components"]["appliance"]["version"] = "1.3.0"
        path.write_text(json.dumps(retained))
    assert not inspect._current_history_only(clean_tree, installed)


@pytest.mark.parametrize(
    ("path", "contents"),
    [
        ("etc/shadow", b"root:$6$hash:1:0:99999:7:::\n"),
        ("etc/shadow-", b""),
        ("var/backups/shadow.bak", b""),
        ("etc/ssh/ssh_host_ed25519_key", b"key"),
        ("home/admin/.ssh/authorized_keys", b""),
        ("etc/sudoers.d/deployer", b"admin ALL=(ALL) NOPASSWD:ALL\n"),
        ("home/admin/.bash_history", b""),
        ("etc/ginnungagap/ginnungagap.yml", b""),
        ("var/lib/ginnungagap/ssh-access.json", b""),
        ("var/lib/ginnungagap/firstboot.done", b""),
        ("var/lib/ginnungagap/update/transaction.json", b""),
        ("var/lib/ginnungagap/update/history/old.json", b"{}"),
        ("var/lib/docker/containers/container/config.json", b"{}"),
        ("var/lib/docker/volumes/leftover/_data/file", b"x"),
        ("var/log/journal/machine/boot.journal", b"x"),
        ("zero.fill", b""),
        ("etc/machine-id", b"not-reset"),
        ("var/lib/ginnungagap/update/installed.json", b'{"previous":"1.2.0"}'),
    ],
)
def test_each_seeded_leftover_blocks_inspection(clean_tree, path, contents):
    write(clean_tree, path, contents)
    assert any(
        not item["passed"]
        for item in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    )


def test_rollback_tag_or_missing_image_index_is_not_an_exact_image_set(clean_tree):
    path = clean_tree / "var/lib/docker/image/overlay2/repositories.json"
    path.write_text(
        json.dumps(
            {
                "Repositories": {
                    "ginn-app": {
                        "ginn-app:latest": "sha256:a",
                        "ginn-app:rollback-1.2.0": "sha256:b",
                    }
                }
            }
        )
    )
    assert not next(
        c
        for c in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
        if c["name"] == "exact compose image set"
    )["passed"]
    path.unlink()
    assert not next(
        c
        for c in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
        if c["name"] == "exact compose image set"
    )["passed"]


@pytest.mark.skipif(os.name != "posix", reason="symlink ownership check needs POSIX")
def test_linked_shadow_cannot_read_the_build_hosts_file(clean_tree, tmp_path):
    outside = write(tmp_path, "host-shadow", b"root:!:1:0:99999:7:::\n")
    shadow = clean_tree / "etc/shadow"
    shadow.unlink()
    shadow.symlink_to(outside)
    assert not inspect.inspect_tree(clean_tree, ["ginn-app:latest"])[0]["passed"]


@pytest.mark.parametrize(
    "forbidden",
    [
        b"this-run-public-key",
        inspect.CONFIG_MARKER.encode(),
        b"ghp_" + b"a" * 36,
        b"github_pat_" + b"a" * 70,
        b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 80,
    ],
)
def test_raw_scan_rejects_real_secrets_even_across_chunks(tmp_path, forbidden):
    raw = write(
        tmp_path, "disk.raw", b"x" * (inspect._RAW_CHUNK - 4) + forbidden + b"x"
    )
    checks = inspect.inspect_raw(raw, ["this-run-public-key", inspect.CONFIG_MARKER])
    assert any(not check["passed"] for check in checks)
    assert all(forbidden.decode() not in check["detail"] for check in checks)


def test_private_key_offsets_are_exact_deduplicated_and_never_print_key_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(inspect, "_RAW_CHUNK", 64)
    key = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 80 + b"!"
    offset = 61
    raw = write(tmp_path, "disk.raw", b"x" * offset + key + b"y" * 300)
    result = next(
        c
        for c in inspect.inspect_raw(raw, [])
        if c["name"] == "no private keys on raw disk"
    )
    assert not result["passed"]
    assert json.loads(result["detail"].removeprefix("byte_offsets=")) == [offset]
    assert "BEGIN" not in result["detail"] and "A" * 32 not in result["detail"]


def test_raw_private_key_diagnostics_are_limited_to_first_sixteen_offsets(tmp_path):
    key = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 40 + b"!"
    raw = write(tmp_path, "disk.raw", key * 20)
    result = next(
        c
        for c in inspect.inspect_raw(raw, [])
        if c["name"] == "no private keys on raw disk"
    )
    assert json.loads(result["detail"].removeprefix("byte_offsets=")) == [
        i * len(key) for i in range(16)
    ]


def test_private_key_files_report_only_relative_paths_and_whole_file_fingerprints(
    clean_tree, monkeypatch
):
    monkeypatch.setattr(inspect, "_RAW_CHUNK", 64)
    key = b"x" * 61 + b"-----BEGIN RSA PRIVATE KEY-----\n" + b"A" * 80 + b"!"
    write(clean_tree, "opt/fixture/key.pem", key)
    result = next(
        c
        for c in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
        if c["name"] == "no private-key files in mounted filesystem"
    )
    assert not result["passed"]
    assert json.loads(result["detail"]) == {
        "files": [
            {"path": "opt/fixture/key.pem", "sha256": hashlib.sha256(key).hexdigest()}
        ],
        "unreadable": [],
    }
    assert "BEGIN" not in result["detail"] and "A" * 32 not in result["detail"]


@pytest.mark.skipif(os.name != "posix", reason="no-follow scanner requires POSIX")
def test_private_key_file_scan_never_follows_a_link_outside_the_guest(
    clean_tree, tmp_path
):
    outside = write(
        tmp_path, "host-private.pem", b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 80
    )
    link = clean_tree / "opt/linked-private.pem"
    link.parent.mkdir()
    link.symlink_to(outside)
    assert all(
        c["passed"] for c in inspect.inspect_tree(clean_tree, ["ginn-app:latest"])
    )


def test_private_key_file_scan_caps_report_and_fails_closed_on_unreadable_files(
    clean_tree, monkeypatch
):
    path = write(clean_tree, "opt/unreadable", b"not logged")
    original = os.open

    def open_file(name, *args, **kwargs):
        if name == path:
            raise PermissionError("not logged")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    found, unreadable = inspect._private_key_files(clean_tree, [path])
    assert found == [] and unreadable == ["opt/unreadable"]
    monkeypatch.setattr(os, "open", original)
    paths = [
        write(
            clean_tree,
            f"opt/private-{n}.pem",
            b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 80,
        )
        for n in range(20)
    ]
    found, unreadable = inspect._private_key_files(clean_tree, paths)
    assert len(found) == 16 and unreadable == []


def test_bare_redactor_patterns_in_source_code_are_not_credentials(tmp_path):
    raw = write(
        tmp_path, "disk.raw", b"source redacts ghp_ and github_pat_ and -----BEGIN"
    )
    assert all(row["passed"] for row in inspect.inspect_raw(raw, []))


def test_shipped_inspector_and_config_fixture_do_not_look_like_run_residue(tmp_path):
    source = Path(inspect.__file__).read_bytes()
    source += Path(inspect.__file__).with_name("minimal-config.yml").read_bytes()
    raw = write(tmp_path, "source.raw", source)
    assert all(
        check["passed"]
        for check in inspect.inspect_raw(raw, ["unique-not-shipped-run-marker"])
    )


def make_ova(path, members):
    with tarfile.open(path, "w") as bundle:
        for name, contents, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(contents) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/shadow"
            bundle.addfile(info, io.BytesIO(contents) if info.isfile() else None)


def test_extract_writes_only_fixed_disk_name_not_archive_paths(tmp_path):
    ova = tmp_path / "image.ova"
    make_ova(
        ova,
        [
            ("image.ovf", b"ovf", tarfile.REGTYPE),
            ("../../outside.vmdk", b"disk-data", tarfile.REGTYPE),
        ],
    )
    destination = tmp_path / "work"
    destination.mkdir()
    disk = inspect.extract_vmdk(ova, destination)
    assert disk == destination / "disk.vmdk"
    assert disk.read_bytes() == b"disk-data"
    assert not (tmp_path / "outside.vmdk").exists()


@pytest.mark.parametrize(
    "members",
    [
        [
            ("disk.vmdk", b"disk", tarfile.REGTYPE),
            ("image.ovf", b"ovf", tarfile.REGTYPE),
        ],
        [("image.ovf", b"ovf", tarfile.REGTYPE), ("disk.vmdk", b"", tarfile.SYMTYPE)],
        [
            ("image.ovf", b"ovf", tarfile.REGTYPE),
            ("one.vmdk", b"1", tarfile.REGTYPE),
            ("two.vmdk", b"2", tarfile.REGTYPE),
        ],
    ],
)
def test_unsupported_or_linked_archive_disk_is_rejected(tmp_path, members):
    ova = tmp_path / "image.ova"
    make_ova(ova, members)
    with pytest.raises(ValueError):
        inspect.extract_vmdk(ova, tmp_path)


def test_missing_required_tree_inputs_fail_closed(tmp_path):
    assert any(not check["passed"] for check in inspect.inspect_tree(tmp_path, []))


def test_only_private_complete_inspection_request_is_accepted(tmp_path):
    request = write(
        tmp_path,
        "request.json",
        '{"expected_images":["ginn-app:latest"],"needles":["do-not-log"]}',
    )
    request.chmod(0o600)
    if os.name == "posix":
        assert inspect._request(request)["expected_images"] == ["ginn-app:latest"]
        request.chmod(0o644)
        with pytest.raises(ValueError):
            inspect._request(request)
    request.write_text('{"expected_images":[],"needles":[]}')
    request.chmod(0o600)
    with pytest.raises(ValueError):
        inspect._request(request)


@pytest.mark.parametrize(
    "report",
    [
        {
            "passed": True,
            "checks": [{"name": "required", "passed": True, "required": True}],
        },
        {"passed": True, "checks": []},
        {
            "passed": True,
            "checks": [{"name": "optional", "passed": True, "required": False}],
        },
        {
            "passed": False,
            "checks": [{"name": "secret scan", "passed": False, "required": True}],
        },
    ],
)
def test_privileged_phase_requires_real_proof_and_removes_its_private_request(
    tmp_path, monkeypatch, report
):
    import subprocess

    from image.build import config, remote

    runs = tmp_path / "runs"
    run = runs / "1.4.0-20261120T100000Z"
    (run / "export").mkdir(parents=True)
    public = write(run, "id_ed25519.pub", "ssh-ed25519 dGVzdA== acceptance")
    results = {
        "P10": {"path": str(run / "export/image.ova")},
        "P8": {"images": ["ginn-app:latest"]},
        "P5": {
            "config_marker": remote.config_marker(
                SimpleNamespace(run_id=run.name, attempt=1)
            ),
            "key_file": str(public).removesuffix(".pub"),
        },
    }
    ctx = SimpleNamespace(
        run_id=run.name,
        attempt=1,
        run_dir=run,
        results=results,
        config=SimpleNamespace(
            esxi=SimpleNamespace(password_file=tmp_path / "password")
        ),
        result_of=lambda name: results[name],
        track_path=lambda path: None,
        secret=lambda value: None,
        note=lambda name, value: None,
    )
    monkeypatch.setattr(inspect, "RUNS_ROOT", runs)
    monkeypatch.setattr(config, "read_secret", lambda *args, **kw: "must-not-appear")

    def helper(argv, **kw):
        assert argv[-1] == run.name and len(argv) == 4
        request = run / "inspection-request.json"
        data = json.loads(request.read_text())
        assert "must-not-appear" in data["needles"]
        assert data["expected_images"] == ["ginn-app:latest"]
        if os.name == "posix":
            assert request.stat().st_mode & 0o077 == 0
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(subprocess, "run", helper)
    if report["passed"] and report["checks"] and report["checks"][0]["required"]:
        assert inspect.inspect_phase(ctx)["passed"]
    else:
        with pytest.raises(PermanentError):
            inspect.inspect_phase(ctx)
    assert not (run / "inspection-request.json").exists()
    assert json.loads((run / "inspection.json").read_text()) == report


@pytest.mark.parametrize("mutation", [None, "marker-from-resume", "key-from-resume"])
def test_resume_inspects_the_exported_second_attempt_not_the_new_counter(
    tmp_path, monkeypatch, mutation
):
    import subprocess

    from image.build import config, remote

    runs = tmp_path / "runs"
    run = runs / "1.4.0-20261120T100000Z"
    (run / "export").mkdir(parents=True)
    generated = yaml.safe_load(
        remote.minimal_config(
            SimpleNamespace(
                run_id=run.name, attempt=2, options=SimpleNamespace(allow_unsigned=True)
            )
        )
    )
    marker = generated["generator"]["llm_servers"][0]["name"]
    public_text = "ssh-ed25519 dGVzdA== original-attempt-two"
    public = write(run, "evidence/public-attempt-2.pub", public_text)
    public.chmod(0o600)
    (run / "state.json").write_text(
        json.dumps(
            {
                "run_id": run.name,
                "phases": {"P5": {"status": "ok", "attempt": 2}},
            }
        )
    )
    results = {
        "P10": {"path": str(run / "export/image.ova")},
        "P8": {"images": ["ginn-app:latest"]},
        "P5": {
            "config_marker": marker,
            "key_file": str(run / "deleted-private/id_ed25519"),
            "public_key_file": str(public),
        },
    }
    ctx = SimpleNamespace(
        run_id=run.name,
        attempt=1,
        run_dir=run,
        results=results,
        config=SimpleNamespace(
            esxi=SimpleNamespace(password_file=tmp_path / "password")
        ),
        result_of=lambda name: results[name],
        track_path=lambda path: None,
        secret=lambda value: None,
        note=lambda *args: None,
    )
    if mutation == "marker-from-resume":
        results["P5"]["config_marker"] = remote.config_marker(ctx)
    elif mutation == "key-from-resume":
        wrong = write(run, "evidence/public-attempt-1.pub", public_text)
        wrong.chmod(0o600)
        results["P5"]["public_key_file"] = str(wrong)
    monkeypatch.setattr(inspect, "RUNS_ROOT", runs)
    monkeypatch.setattr(
        config, "read_secret", lambda *args, **kwargs: "password-needle"
    )
    calls = []

    def helper(argv, **kwargs):
        request = json.loads((run / "inspection-request.json").read_text())
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
        assert inspect.inspect_phase(ctx)["passed"]
        assert calls
    else:
        with pytest.raises(PermanentError):
            inspect.inspect_phase(ctx)
        assert not calls


@pytest.mark.parametrize("attempt", [None, True, 0, -1, "2", 1001])
def test_inspection_refuses_invalid_recorded_attempt_even_with_a_valid_resume_counter(
    tmp_path, attempt
):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(
        json.dumps(
            {"run_id": "run", "phases": {"P5": {"status": "ok", "attempt": attempt}}}
        )
    )
    with pytest.raises(PermanentError, match="recorded build attempt"):
        inspect._artifact_attempt(SimpleNamespace(run_dir=run, run_id="run", attempt=1))


def public_fixture_policy(monkeypatch, contents=None):
    pem = (
        b"-----BEGIN RSA PRIVATE KEY-----"
        + b"\n"
        + b"A" * 80
        + b"\n"
        + b"-----END RSA PRIVATE KEY-----"
    )
    contents = pem if contents is None else contents
    entries = [
        {
            "path": "usr/lib/libfixture.so",
            "sha256": hashlib.sha256(contents).hexdigest(),
            "key_sha256": [hashlib.sha256(pem).hexdigest()],
        }
    ]
    monkeypatch.setattr(inspect, "_public_key_policy", lambda: entries)
    return pem


def test_pinned_vendor_public_keys_have_verified_provenance_and_no_key_material():
    data = json.loads(
        Path(inspect.__file__).with_name("public-test-keys.json").read_text()
    )
    assert data["schema"] == 1
    assert sum(len(entry["key_sha256"]) for entry in data["files"]) == 10
    assert all(entry["source_url"].startswith("https://") for entry in data["files"])
    assert len(data["verification"]["packages"]) == 3
    assert "BEGIN " not in json.dumps(data)


def test_exact_public_vector_is_accepted_across_arbitrary_stream_boundaries(
    monkeypatch,
):
    pem = public_fixture_policy(monkeypatch)
    scanner = inspect._PrivateKeyScanner()
    data = b"binary-prefix" + pem + b"\x00binary-suffix"
    for offset in range(0, len(data), 17):
        scanner.feed(data[offset : offset + 17])
    scanner.feed(b"", final=True)
    assert scanner.saw_private and not scanner.unknown and not scanner.offsets


def test_known_public_vector_file_requires_exact_vendor_location_and_whole_file_hash(
    clean_tree, monkeypatch
):
    pem = public_fixture_policy(monkeypatch)
    vendor = write(clean_tree, "usr/lib/libfixture.so", pem)
    layer = write(
        clean_tree, "var/lib/docker/overlay2/layer-123/diff/usr/lib/libfixture.so", pem
    )
    identity = write(clean_tree, "etc/secret.pem", pem)
    changed = write(clean_tree, "elsewhere/usr/lib/libfixture.so", pem)
    findings, errors = inspect._private_key_files(
        clean_tree, [vendor, layer, identity, changed]
    )
    assert not errors
    assert {item["path"] for item in findings} == {
        "etc/secret.pem",
        "elsewhere/usr/lib/libfixture.so",
    }
    vendor.write_bytes(pem + b"modified ELF")
    findings, errors = inspect._private_key_files(clean_tree, [vendor])
    assert findings and not errors


def test_extra_unknown_private_key_is_not_hidden_by_vendor_file_exception(
    clean_tree, monkeypatch
):
    pem = public_fixture_policy(monkeypatch)
    unknown = pem.replace(b"A" * 80, b"B" * 80)
    public_fixture_policy(monkeypatch, pem + unknown)
    path = write(clean_tree, "usr/lib/libfixture.so", pem + unknown)
    findings, errors = inspect._private_key_files(clean_tree, [path])
    assert findings and not errors


@pytest.mark.parametrize(
    "change", ["mutated", "truncated", "wrong-footer", "legacy-encrypted"]
)
def test_unknown_or_truncated_key_is_never_a_public_fixture(monkeypatch, change):
    pem = public_fixture_policy(monkeypatch)
    data = {
        "mutated": pem.replace(b"A" * 80, b"B" * 80),
        "truncated": pem.split(b"-----END")[0],
        "wrong-footer": pem.replace(b"END RSA", b"END EC"),
        "legacy-encrypted": pem.replace(
            b"A" * 80,
            b"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,123\n" + b"A" * 80,
        ),
    }[change]
    scanner = inspect._PrivateKeyScanner()
    scanner.feed(data)
    scanner.feed(b"", final=True)
    assert scanner.unknown and scanner.offsets == {0}


def test_raw_known_vector_can_never_hide_adjacent_machine_key(tmp_path, monkeypatch):
    pem = public_fixture_policy(monkeypatch)
    unknown = pem.replace(b"A" * 80, b"B" * 80)
    path = write(tmp_path, "raw.img", pem + b"\x00" + unknown)
    checks = inspect.inspect_raw(path, [])
    private = next(c for c in checks if c["name"] == "no private keys on raw disk")
    assert not private["passed"]
    assert str(len(pem) + 1) in private["detail"]
    assert "BEGIN" not in private["detail"]


def test_oversized_incomplete_key_does_not_grow_stream_buffer(monkeypatch):
    pem = public_fixture_policy(monkeypatch)
    scanner = inspect._PrivateKeyScanner()
    scanner.feed(pem.split(b"-----END")[0])
    for _ in range(100):
        scanner.feed(b"A" * 1024)
        assert len(scanner.buffer) <= inspect._MAX_PUBLIC_PEM + 1024
    scanner.feed(b"", final=True)
    assert scanner.unknown


@pytest.mark.parametrize(
    "mutation", [None, "outside-vendor", "truncated", "unknown-body"]
)
def test_exact_pinned_legacy_encrypted_public_fixture_stays_narrow(
    tmp_path, monkeypatch, mutation
):
    pem = (
        b"-----BEGIN RSA PRIVATE KEY-----"
        + b"\n"
        + b"Proc-Type: 4,ENCRYPTED\nDEK-Info: DES-EDE3-CBC,0123456789ABCDEF\n\n"
        + b"A" * 80
        + b"\n"
        + b"-----END RSA PRIVATE KEY-----"
    )
    relative = "usr/share/doc/libnet-ssleay-perl/examples/server_key.pem"
    entries = [
        {
            "path": relative,
            "sha256": hashlib.sha256(pem).hexdigest(),
            "key_sha256": [hashlib.sha256(pem).hexdigest()],
        }
    ]
    monkeypatch.setattr(inspect, "_public_key_policy", lambda: entries)
    contents = pem
    if mutation == "outside-vendor":
        relative = "etc/private/server_key.pem"
    elif mutation == "truncated":
        contents = pem.split(b"-----END")[0]
    elif mutation == "unknown-body":
        contents = pem.replace(b"A" * 80, b"B" * 80)
    path = write(tmp_path, relative, contents)
    files, errors = inspect._private_key_files(tmp_path, [path])
    assert not errors
    assert bool(files) == (mutation is not None)
    scanner = inspect._PrivateKeyScanner()
    for index in range(0, len(contents), 13):
        scanner.feed(contents[index : index + 13])
    scanner.feed(b"", final=True)
    # A known public block copied to an identity path is rejected by the file gate.
    assert scanner.unknown == (mutation in ("truncated", "unknown-body"))
