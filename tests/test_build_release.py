"""release/build_release.py: builds the asset set of an appliance release from local fixtures, no network but a local fake."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ginnungagap import manifest as manifest_module

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_release", ROOT / "release" / "build_release.py"
)
build_release = importlib.util.module_from_spec(_spec)
sys.modules["build_release"] = build_release
_spec.loader.exec_module(build_release)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILES = {
    "appliance": ("ginnungagap/version.py", '__version__ = "{v}"\n'),
    "operator": ("app/version.py", '"""x"""\n__version__ = "{v}"\n'),
    "builder": (
        "pyproject.toml",
        '[project]\nname = "topology-builder"\nversion = "{v}"\n',
    ),
    "template-services": ("VERSION", "{v}\n"),
    "generator": ("VERSION", "{v}\n"),
}
VERSIONS = {
    "appliance": "1.3.0",
    "operator": "1.3.0",
    "builder": "1.0.0",
    "template-services": "1.0.0",
    "generator": "1.0.0",
}
OWNER = "HTL3R-Xeonis"


def make_tarball(folder: Path, component: str, version: str, *, declared: str | None = None, top: str | None = None,
                 extra: dict[str, bytes] | None = None, commit: str | None = COMMIT) -> Path:  # fmt: skip
    """A tarball shaped like `git archive --prefix=<Repo>-<version>/` made it (the commit in the pax header)."""
    repo = manifest_module.REPO_NAMES[component]
    top = top or f"{repo}-{version}"
    path = folder / f"{repo}-{version}.tar.gz"
    name, template = FILES[component]
    members = {f"{top}/{name}": template.format(v=declared or version).encode(), f"{top}/README": b"hello\n", **{f"{top}/{k}" if not k.startswith(("/", "..")) else k: v for k, v in (extra or {}).items()}}  # fmt: skip
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": commit} if commit else None,
    ) as archive:
        for member_name, data in members.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as packed,
    ):
        packed.write(buffer.getvalue())
    return path


def pins_text(versions: dict[str, str] | None = None, *, extra_head: str = "") -> str:
    versions = versions or VERSIONS
    lines = ["appliance:", f"  notes: 'a note'{extra_head}", "components:"]
    for name, repo in manifest_module.REPO_NAMES.items():
        lines += [
            f"  {name}:",
            f"    repo: {OWNER}/{repo}",
            f"    tag: v{versions[name]}",
        ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def work(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name, version in VERSIONS.items():
        make_tarball(sources, name, version)
    pins = tmp_path / "pins.yml"
    pins.write_text(pins_text())
    return {"sources": sources, "pins": pins, "out": tmp_path / "out", "tmp": tmp_path}


def build(work, **kwargs):
    return build_release.build(
        work["pins"],
        work["out"],
        sources=work["sources"],
        released="2026-12-01T10:00:00Z",
        **kwargs,
    )


def test_a_release_is_built_from_the_five_tarballs_and_the_result_is_a_valid_manifest(
    work,
):
    document = build(work)
    out = work["out"]
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["components.json", "SHA256SUMS", *(f"{r}-{VERSIONS[n]}.tar.gz" for n, r in manifest_module.REPO_NAMES.items())]
    )  # fmt: skip
    manifest = manifest_module.load(
        out / "components.json"
    )  # the appliance's own validator
    assert (
        manifest.version == "1.3.0"
        and manifest.tag == "v1.3.0"
        and manifest.channel == "stable"
    )
    assert manifest.released == "2026-12-01T10:00:00Z" and manifest.notes == "a note"
    assert document == json.loads((out / "components.json").read_text())
    operator = manifest.components["operator"]
    assert operator.repo == f"{OWNER}/Igdrasyl" and operator.version == "1.3.0"
    assert operator.commit == COMMIT and operator.services == ("ginnungagap",)
    tarball = out / operator.asset
    assert (
        operator.sha256 == hashlib.sha256(tarball.read_bytes()).hexdigest()
        and operator.size == tarball.stat().st_size
    )
    assert manifest.components["template-services"].services == (
        "template-esxi",
        "collector-esxi",
        "template-gns3",
        "collector-gns3",
    )
    assert not manifest.data_migration and "min_installed" not in document["appliance"]
    assert not list(work["tmp"].glob(".release-*"))  # the working folder is gone


def test_sha256sums_lists_every_other_file_in_sha256sum_format(work):
    build(work)
    lines = (work["out"] / "SHA256SUMS").read_text().splitlines()
    names = [line.split("  ", 1)[1] for line in lines]
    assert (
        names == sorted(names)
        and "components.json" in names
        and "SHA256SUMS" not in names
        and len(names) == 6
    )
    for line in lines:
        checksum, name = line.split("  ", 1)
        assert checksum == hashlib.sha256((work["out"] / name).read_bytes()).hexdigest()
    if shutil.which("sha256sum"):
        assert (
            subprocess.run(
                ["sha256sum", "-c", "--quiet", "SHA256SUMS"],
                cwd=work["out"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )


def test_the_same_inputs_give_byte_identical_output(work, tmp_path):
    build(work)
    first = {p.name: p.read_bytes() for p in work["out"].iterdir()}
    again = {**work, "out": tmp_path / "out2"}
    build(again)
    assert {p.name: p.read_bytes() for p in again["out"].iterdir()} == first


def test_a_release_candidate_is_on_the_prerelease_channel(work):
    versions = {**VERSIONS, "appliance": "1.3.0-rc.1"}
    make_tarball(work["sources"], "appliance", "1.3.0-rc.1")
    work["pins"].write_text(pins_text(versions))
    manifest = build_release.build(work["pins"], work["out"], sources=work["sources"])
    assert (
        manifest["appliance"]["channel"] == "prerelease"
        and manifest["appliance"]["tag"] == "v1.3.0-rc.1"
    )
    assert manifest["appliance"]["released"].endswith(
        "Z"
    )  # the time of the build when none is given


def test_min_installed_notes_and_data_migration_come_from_the_pin_file(work):
    text = pins_text(extra_head="\n  min_installed: 1.2.0").replace(
        "    tag: v1.3.0\n", "    tag: v1.3.0\n    data_migration: true\n"
    )
    work["pins"].write_text(text)
    document = build(work)
    assert document["appliance"]["min_installed"] == "1.2.0"
    assert document["components"]["operator"]["data_migration"] is True
    assert document["components"]["builder"]["data_migration"] is False
    assert manifest_module.load(work["out"] / "components.json").data_migration is True


def test_each_version_file_format_is_read():
    read = build_release.version_in_file
    assert read("appliance", '"""x"""\n\n__version__ = "1.3.0"\n') == "1.3.0"
    assert read("operator", "__version__ = '1.3.0'\n") == "1.3.0"
    assert read("builder", '[project]\nversion = "1.0.0"\n') == "1.0.0"
    assert (
        read("builder", "[tool]\nx = 1\n") is None
        and read("builder", "not toml [[") is None
    )
    assert (
        read("generator", "1.0.0\n\n") == "1.0.0"
        and read("template-services", "  \n") is None
    )
    assert read("appliance", "# nothing\n") is None


def test_a_tag_that_differs_from_the_version_file_is_refused_and_nothing_is_written(
    work,
):
    make_tarball(work["sources"], "operator", "1.3.0", declared="1.2.0")
    with pytest.raises(
        build_release.ReleaseError, match=r"Igdrasyl.*v1\.3\.0.*says 1\.2\.0"
    ):
        build(work)
    assert not work["out"].exists() and not list(work["tmp"].glob(".release-*"))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"top": "Wrong-1.0.0"}, "below Igdrasyl-1.3.0/"),
        ({"extra": {"../escape": b"x"}}, "leaves the folder"),
        ({"extra": {"/etc/passwd": b"x"}}, "leaves the folder"),
        ({"commit": None}, "no commit id"),
    ],
)
def test_a_tarball_with_the_wrong_layout_is_refused(work, kwargs, message):
    make_tarball(work["sources"], "operator", "1.3.0", **kwargs)
    with pytest.raises(build_release.ReleaseError, match=message):
        build(work)


def test_a_tarball_without_the_version_file_or_not_a_tarball_is_refused(work):
    path = work["sources"] / "Igdrasyl-1.3.0.tar.gz"
    path.write_bytes(b"this is not a tarball")
    with pytest.raises(build_release.ReleaseError, match="not a readable"):
        build(work)
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w:gz",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": COMMIT},
    ) as archive:
        info = tarfile.TarInfo("Igdrasyl-1.3.0/README")
        archive.addfile(info, io.BytesIO(b""))
    path.write_bytes(buffer.getvalue())
    with pytest.raises(build_release.ReleaseError, match=r"app/version\.py is missing"):
        build(work)


def test_a_missing_tarball_names_the_file_and_the_ways_to_get_it(work):
    (work["sources"] / "Mimyr-1.0.0.tar.gz").unlink()
    with pytest.raises(
        build_release.ReleaseError,
        match=r"Mimyr-1\.0\.0\.tar\.gz.*--sources.*--checkouts.*--fetch",
    ):
        build(work)


def test_a_non_empty_output_folder_is_refused_unless_forced(work):
    work["out"].mkdir()
    (work["out"] / "old").write_text("x")
    with pytest.raises(build_release.ReleaseError, match="not empty"):
        build(work)
    assert (work["out"] / "old").exists()
    build(work, force=True)
    assert (
        not (work["out"] / "old").exists()
        and (work["out"] / "components.json").is_file()
    )


def test_the_pin_file_reports_every_problem_at_once(tmp_path):
    pins = tmp_path / "p.yml"
    pins.write_text(
        "appliance: {min_installed: soon}\ncomponents:\n  operator: {repo: x/Wrong, tag: 1.3.0}\n  builder: {repo: o/TopologyBuilder, tag: v1.0.0, data_migration: 'yes'}\n  extra: {}\n"
    )
    with pytest.raises(build_release.ReleaseError) as raised:
        build_release.load_pins(pins)
    text = str(raised.value)
    for fragment in ("components.appliance: missing", "components.operator.repo", "components.operator.tag", "components.builder.data_migration", "components.template-services: missing", "unknown component(s) extra", "appliance.min_installed"):  # fmt: skip
        assert fragment in text
    pins.write_text("- a list\n")
    with pytest.raises(build_release.ReleaseError, match="components"):
        build_release.load_pins(pins)


def test_the_build_side_is_strict_about_names_and_refuses_the_old_ones(tmp_path):
    # reading a manifest is lenient (consistency), building one is where the names are enforced
    pins = tmp_path / "p.yml"
    entries = "".join(
        f"  {name}: {{repo: HTL3R-Xeonis/{repo}, tag: v1.0.0}}\n"
        for name, repo in manifest_module.REPO_NAMES.items()
    )
    pins.write_text("components:\n" + entries)
    assert set(build_release.load_pins(pins)["components"]) == set(
        manifest_module.COMPONENT_NAMES
    )
    pins.write_text(
        "components:\n" + entries.replace("Hugyn-and-Munyn", "Docker-Server-Files")
    )
    with pytest.raises(
        build_release.ReleaseError,
        match=r"components\.template-services\.repo: must be <owner>/Hugyn-and-Munyn",
    ):
        build_release.load_pins(pins)
    pins.write_text(
        "components:\n" + entries.replace("template-services:", "docker-server-files:")
    )
    with pytest.raises(
        build_release.ReleaseError,
        match=r"(?s)template-services: missing.*docker-server-files",
    ):
        build_release.load_pins(pins)


def test_the_pin_file_of_this_repository_is_valid_and_pins_the_appliance_version_it_contains():
    pins = build_release.load_pins(ROOT / "release" / "components.in.yml")
    from ginnungagap.version import __version__

    assert pins["components"]["appliance"]["tag"] == f"v{__version__}"
    assert pins["components"]["appliance"]["repo"] == f"{OWNER}/Ginnungagap-Appliance"


# ---- the appliance's own tarball comes from git ----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}  # fmt: skip
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_appliance_tarball_is_a_reproducible_git_archive_of_the_tag(work):
    repo = work["tmp"] / "appliance-checkout"
    (repo / "ginnungagap").mkdir(parents=True)
    (repo / "ginnungagap" / "version.py").write_text('__version__ = "1.3.0"\n')
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v1.3.0")
    commit = _git(repo, "rev-parse", "HEAD")
    (work["sources"] / "Ginnungagap-Appliance-1.3.0.tar.gz").unlink()
    document = build(work, appliance_dir=repo)
    assert document["components"]["appliance"]["commit"] == commit
    first = (work["out"] / "Ginnungagap-Appliance-1.3.0.tar.gz").read_bytes()
    with tarfile.open(work["out"] / "Ginnungagap-Appliance-1.3.0.tar.gz") as archive:
        assert (
            "Ginnungagap-Appliance-1.3.0/ginnungagap/version.py" in archive.getnames()
        )
    shutil.rmtree(work["out"])
    build(work, appliance_dir=repo)
    assert (work["out"] / "Ginnungagap-Appliance-1.3.0.tar.gz").read_bytes() == first
    # a later commit that raises the version without a new tag: the tag still decides, and the check catches a mismatch
    (repo / "ginnungagap" / "version.py").write_text('__version__ = "1.3.1"\n')
    _git(repo, "commit", "-q", "-am", "two")
    shutil.rmtree(work["out"])
    with pytest.raises(build_release.ReleaseError, match="says 1.3.1"):
        build(work, appliance_dir=repo, ref="HEAD")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_component_can_be_archived_from_a_checkout_at_its_tag(work):
    checkouts = work["tmp"] / "checkouts"
    repo = checkouts / "Igdrasyl"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "version.py").write_text('__version__ = "1.3.0"\n')
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v1.3.0")
    (work["sources"] / "Igdrasyl-1.3.0.tar.gz").unlink()
    document = build(work, checkouts=checkouts)
    assert document["components"]["operator"]["commit"] == _git(
        repo, "rev-parse", "HEAD"
    )
    _git(repo, "tag", "-d", "v1.3.0")
    shutil.rmtree(work["out"])
    with pytest.raises(
        build_release.ReleaseError, match="git rev-parse --verify failed"
    ):
        build(work, checkouts=checkouts)


# ---- tampering ------------------------------------------------------------------------------------------------------


def test_verify_accepts_what_build_made_and_names_every_kind_of_tampering(work):
    build(work)
    out = work["out"]
    assert build_release.verify(out).version == "1.3.0"
    pristine = {p.name: p.read_bytes() for p in out.iterdir()}

    def restore():
        for name, data in pristine.items():
            (out / name).write_bytes(data)

    tarball = out / "Igdrasyl-1.3.0.tar.gz"
    tarball.write_bytes(tarball.read_bytes()[:-1] + b"X")
    with pytest.raises(
        build_release.ReleaseError,
        match=r"Igdrasyl-1\.3\.0\.tar\.gz: the checksum differs from components\.json",
    ):
        build_release.verify(out)
    restore()
    sums = (
        (out / "SHA256SUMS")
        .read_text()
        .replace(
            hashlib.sha256(
                (out / "TopologyBuilder-1.0.0.tar.gz").read_bytes()
            ).hexdigest(),
            "0" * 64,
        )
    )
    (out / "SHA256SUMS").write_text(sums)
    with pytest.raises(
        build_release.ReleaseError,
        match="TopologyBuilder-1.0.0.tar.gz: the checksum differs from SHA256SUMS",
    ):
        build_release.verify(out)
    restore()
    (out / "components.json").write_text(
        (out / "components.json").read_text().replace('"size": ', '"size": 1', 1)
    )
    with pytest.raises(build_release.ReleaseError, match="bytes, components.json says"):
        build_release.verify(out)
    restore()
    (out / "Hugyn-and-Munyn-1.0.0.tar.gz").unlink()
    with pytest.raises(
        build_release.ReleaseError, match="Hugyn-and-Munyn-1.0.0.tar.gz: missing"
    ):
        build_release.verify(out)
    restore()
    (out / "components.json").write_text("{not json")
    with pytest.raises(build_release.ReleaseError, match="components.json"):
        build_release.verify(out)


# ---- signing --------------------------------------------------------------------------------------------------------

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"
)


def _key(folder: Path, name: str = "release_key") -> Path:
    key = folder / name
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "ginnungagap-release",
            "-f",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    return key


def _signers(folder: Path, key: Path) -> Path:
    public = key.with_name(key.name + ".pub").read_text().split()
    path = folder / "allowed_signers"
    path.write_text(
        f'ginnungagap-release namespaces="ginnungagap-release" {public[0]} {public[1]}\n'
    )
    return path


@needs_ssh_keygen
def test_a_signed_release_verifies_with_the_public_key_and_not_with_another_or_after_a_change(
    work,
):
    key = _key(work["tmp"])
    build(work, sign_key=key)
    out = work["out"]
    assert (out / "components.json.sig").is_file()
    assert (
        "components.json.sig" not in (out / "SHA256SUMS").read_text()
    )  # the signature is checked, not summed
    signers = _signers(work["tmp"], key)
    assert build_release.verify(out, signers).version == "1.3.0"
    stranger = work["tmp"] / "stranger"
    stranger.mkdir()
    wrong = _signers(stranger, _key(stranger))
    with pytest.raises(build_release.ReleaseError, match="signature is not valid"):
        build_release.verify(out, wrong)
    document = json.loads((out / "components.json").read_text())
    document["appliance"]["notes"] = "changed after signing"
    (out / "components.json").write_text(json.dumps(document, indent=2) + "\n")
    build_release.write_sums(
        out
    )  # even with a consistent SHA256SUMS the signature catches it: the checksums come from the same place as the files
    with pytest.raises(build_release.ReleaseError, match="signature is not valid"):
        build_release.verify(out, signers)


@needs_ssh_keygen
def test_an_unsigned_release_does_not_pass_a_signature_check_and_the_key_is_never_printed(
    work, capsys
):
    build(work)
    key = _key(work["tmp"])
    with pytest.raises(
        build_release.ReleaseError, match=r"components\.json\.sig: missing"
    ):
        build_release.verify(work["out"], _signers(work["tmp"], key))
    shutil.rmtree(work["out"])
    assert (
        build_release.main(
            [
                "--pins",
                str(work["pins"]),
                "--out",
                str(work["out"]),
                "--sources",
                str(work["sources"]),
                "--sign",
                str(key),
            ]
        )
        == 0
    )
    printed = capsys.readouterr()
    assert (
        "BEGIN OPENSSH PRIVATE KEY" not in printed.out + printed.err
        and "NOT signed" not in printed.out
    )


def test_a_missing_or_broken_key_is_refused_before_anything_is_built(
    work, tmp_path, capsys
):
    assert (
        build_release.main(
            [
                "--pins",
                str(work["pins"]),
                "--out",
                str(work["out"]),
                "--sources",
                str(work["sources"]),
                "--sign",
                str(tmp_path / "nokey"),
            ]
        )
        == 1
    )
    assert "--sign" in capsys.readouterr().err and not work["out"].exists()
    if shutil.which("ssh-keygen"):
        broken = tmp_path / "broken"
        broken.write_text("not a key\n")
        with pytest.raises(build_release.ReleaseError, match="signing failed"):
            build(work, sign_key=broken)
        assert not work["out"].exists()


# ---- release notes and the command line -----------------------------------------------------------------------------


def test_the_release_notes_list_what_changed_since_the_previous_release(work, tmp_path):
    build(work)
    previous = json.loads((work["out"] / "components.json").read_text())
    previous["appliance"]["version"] = "1.2.9"
    previous["components"]["operator"]["version"] = "1.2.0"
    previous["components"]["appliance"]["version"] = "1.2.9"
    (tmp_path / "previous.json").write_text(json.dumps(previous))
    notes = build_release.release_notes(
        json.loads((work["out"] / "components.json").read_text()),
        tmp_path / "previous.json",
    )
    assert "# Ginnungagap appliance 1.3.0" in notes and "Update of 1.2.9" in notes
    assert "- operator (HTL3R-Xeonis/Igdrasyl): 1.2.0 -> 1.3.0" in notes
    assert "- appliance (HTL3R-Xeonis/Ginnungagap-Appliance): 1.2.9 -> 1.3.0" in notes
    assert "builder" not in notes  # unchanged components are not listed
    plain = build_release.release_notes(
        json.loads((work["out"] / "components.json").read_text()), None
    )
    assert (
        "- builder (HTL3R-Xeonis/TopologyBuilder): 1.0.0" in plain and "a note" in plain
    )


def test_the_command_line_builds_verifies_and_reports_errors_with_exit_codes(
    work, capsys
):
    notes = work["tmp"] / "notes.md"
    argv = ["--pins", str(work["pins"]), "--out", str(work["out"]), "--sources", str(work["sources"]), "--released", "2026-12-01T10:00:00Z", "--notes-file", str(notes)]  # fmt: skip
    assert build_release.main(argv) == 0
    printed = capsys.readouterr().out
    assert (
        "Release 1.3.0 (stable)" in printed
        and "components.json" in printed
        and "NOT signed" in printed
    )
    assert notes.read_text().startswith("# Ginnungagap appliance 1.3.0")
    assert "notes.md" not in [
        p.name for p in work["out"].iterdir()
    ]  # not a release asset
    assert build_release.main(["--verify", str(work["out"])]) == 0
    assert "release 1.3.0 is consistent" in capsys.readouterr().out
    assert build_release.main(argv) == 1 and "not empty" in capsys.readouterr().err
    assert (
        build_release.main(["--pins", str(work["pins"])]) == 1
        and "--out" in capsys.readouterr().err
    )
    (work["out"] / "SHA256SUMS").unlink()
    assert build_release.main(["--verify", str(work["out"])]) == 1


def test_the_script_runs_as_a_program_from_any_folder(work, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "release" / "build_release.py"), "--pins", str(work["pins"]), "--out", str(work["out"]), "--sources", str(work["sources"])],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert (work["out"] / "components.json").is_file()


# ---- --fetch against a local fake of the GitHub API (no real network) -------------------------------------------------


class FakeGitHub:
    """/repos/<o>/<r>/releases/tags/<tag> and an asset download that redirects to a second path which refuses an Authorization header."""

    def __init__(
        self,
        sources: Path,
        token: str | None,
        *,
        drop_sums: bool = False,
        bad_sum: bool = False,
    ):
        self.seen: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                auth = self.headers.get("Authorization")
                outer.seen.append((self.path, auth))
                if self.path.startswith("/signed/"):
                    if auth is not None:
                        return self._send(
                            400, b"the signed address refuses Authorization"
                        )
                    name = self.path.removeprefix("/signed/")
                    data = (sources / name.removesuffix(".sha256")).read_bytes()
                    if name.endswith(".sha256"):
                        return self._send(
                            200,
                            (
                                ("0" * 64)
                                if bad_sum
                                else hashlib.sha256(data).hexdigest()
                            ).encode()
                            + b"  "
                            + name.encode()
                            + b"\n",
                        )
                    return self._send(200, data)
                parts = self.path.split("/")
                if (
                    len(parts) == 7
                    and parts[3] == "releases"
                    and parts[4] == "download"
                ):
                    # github.com/<owner>/<repo>/releases/download/<tag>/<file>: no API, no token, a private repository says 404
                    name = parts[6]
                    if (
                        token is not None
                        or not (sources / name.removesuffix(".sha256")).is_file()
                        or (drop_sums and name.endswith(".sha256"))
                    ):
                        return self._send(404, b"not found")
                    return self._send(302, headers={"Location": f"/signed/{name}"})
                if token is not None and auth != f"Bearer {token}":
                    return self._send(401, b"bad credentials")
                if (
                    len(parts) == 7
                    and parts[1] == "repos"
                    and parts[4] == "releases"
                    and parts[5] == "tags"
                ):
                    version = parts[6].removeprefix("v")
                    assets = []
                    for path in sorted(sources.glob(f"*-{version}.tar.gz")):
                        assets.append({"name": path.name, "id": len(assets) + 1})
                        if not drop_sums:
                            assets.append(
                                {"name": path.name + ".sha256", "id": len(assets) + 1}
                            )
                    body = json.dumps({"assets": assets}).encode()
                    outer.assets = {a["id"]: a["name"] for a in assets}
                    return self._send(200, body)
                if len(parts) == 7 and parts[5] == "assets":
                    return self._send(
                        302,
                        headers={"Location": f"/signed/{outer.assets[int(parts[6])]}"},
                    )
                return self._send(404, b"not found")

        self.assets: dict[int, str] = {}
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def no_waiting_for_releases(monkeypatch):
    """The tests that expect a 404 must not wait the five minutes the real build waits for a release that is not there yet."""
    monkeypatch.setattr(build_release, "RELEASE_WAIT_SECONDS", 0)


@pytest.fixture
def fake_github(work):
    servers = []

    def start(token=None, **kwargs):
        server = FakeGitHub(work["sources"], token, **kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _fetch_work(work):
    """A --sources folder with only the appliance's own tarball (the appliance is archived from git, never fetched)."""
    only_appliance = work["tmp"] / "only-appliance"
    only_appliance.mkdir(exist_ok=True)
    shutil.copy(work["sources"] / "Ginnungagap-Appliance-1.3.0.tar.gz", only_appliance)
    return only_appliance


def _fetch(work, server, token=None, **kwargs):
    return build_release.build(
        work["pins"],
        work["out"],
        sources=_fetch_work(work),
        fetch=True,
        api=server.url,
        token=token,
        api_mode="always",
        **kwargs,
    )


def test_fetch_downloads_each_release_asset_verifies_its_checksum_and_sends_the_token_only_to_the_api(
    work, fake_github
):
    server = fake_github(token="secret-token")
    document = _fetch(work, server, "secret-token")
    assert document["components"]["operator"]["commit"] == COMMIT
    api_calls = [
        (path, auth) for path, auth in server.seen if not path.startswith("/signed/")
    ]
    signed = [(path, auth) for path, auth in server.seen if path.startswith("/signed/")]
    assert api_calls and all(auth == "Bearer secret-token" for _, auth in api_calls)
    assert signed and all(
        auth is None for _, auth in signed
    )  # the signed download address never gets the token
    assert any(
        "/repos/HTL3R-Xeonis/Igdrasyl/releases/tags/v1.3.0" in path
        for path, _ in api_calls
    )
    assert build_release.verify(work["out"]).version == "1.3.0"


def test_fetch_needs_no_token_for_a_public_repository(work, fake_github):
    server = fake_github(token=None)
    _fetch(work, server, None)
    assert all(auth is None for _, auth in server.seen)


def test_fetch_explains_a_refused_token_without_printing_it(work, fake_github):
    server = fake_github(token="right")
    with pytest.raises(build_release.ReleaseError) as raised:
        _fetch(work, server, "wrong-token-value")
    assert "HTTP 401" in str(raised.value) and "token" in str(raised.value)
    assert "wrong-token-value" not in str(raised.value)
    assert not work["out"].exists()


def test_fetch_refuses_a_download_that_does_not_match_the_releases_own_checksum(
    work, fake_github
):
    with pytest.raises(
        build_release.ReleaseError, match=r"does not match the release's own"
    ):
        _fetch(work, fake_github(bad_sum=True))
    assert not work["out"].exists()


def test_fetch_refuses_a_release_without_a_checksum_asset_or_without_the_tarball(
    work, fake_github
):
    with pytest.raises(
        build_release.ReleaseError,
        match=r"no usable TopologyBuilder-1\.0\.0\.tar\.gz\.sha256|no usable .*sha256",
    ):
        _fetch(work, fake_github(drop_sums=True))
    (work["sources"] / "Igdrasyl-1.3.0.tar.gz").unlink()
    with pytest.raises(build_release.ReleaseError, match=r"HTTP 404|no asset"):
        _fetch(work, fake_github())


def test_a_release_for_another_tag_than_the_pinned_one_is_refused(work, capsys):
    with pytest.raises(
        build_release.ReleaseError,
        match=r"for the tag v1\.4\.0.*pins the appliance at v1\.3\.0",
    ):
        build(work, expect_tag="v1.4.0")
    build(work, expect_tag="v1.3.0")
    shutil.rmtree(work["out"])
    argv = [
        "--pins",
        str(work["pins"]),
        "--out",
        str(work["out"]),
        "--sources",
        str(work["sources"]),
    ]
    assert (
        build_release.main([*argv, "--tag", "v9.9.9"]) == 1
        and "v9.9.9" in capsys.readouterr().err
    )


# ---- --fetch without the API: the predictable download addresses (no rate limit) ----------------------------------------


def _fetch_web(work, server, token=None, **kwargs):
    return build_release.build(
        work["pins"],
        work["out"],
        sources=_fetch_work(work),
        fetch=True,
        api=server.url,
        web=server.url,
        token=token,
        **kwargs,
    )


def test_fetch_without_a_token_uses_the_download_addresses_and_not_one_api_call(
    work, fake_github
):
    server = fake_github(token=None)
    document = _fetch_web(work, server)
    assert document["components"]["operator"]["commit"] == COMMIT
    paths = [path for path, _ in server.seen]
    assert not [
        p for p in paths if p.startswith("/repos/")
    ]  # the API was not asked at all
    assert any(
        p == "/HTL3R-Xeonis/Igdrasyl/releases/download/v1.3.0/Igdrasyl-1.3.0.tar.gz"
        for p in paths
    )
    assert any(p.endswith("Igdrasyl-1.3.0.tar.gz.sha256") for p in paths)
    assert any(
        p.startswith("/signed/") for p in paths
    )  # followed to the signed address
    assert all(auth is None for _, auth in server.seen)
    assert build_release.verify(work["out"]).version == "1.3.0"


def test_fetch_with_a_token_keeps_the_api_path(work, fake_github):
    server = fake_github(token="secret-token")
    _fetch_web(work, server, "secret-token")
    paths = [path for path, _ in server.seen]
    assert any(p.startswith("/repos/") for p in paths)
    assert not any("/releases/download/" in p for p in paths)


def test_fetch_api_mode_never_and_always(work, fake_github):
    server = fake_github(token=None)
    _fetch_web(work, server, api_mode="always")
    assert any(p.startswith("/repos/") for p, _ in server.seen)
    server = fake_github(token=None)
    shutil.rmtree(work["out"], ignore_errors=True)
    _fetch_web(work, server, api_mode="never")
    assert not any(p.startswith("/repos/") for p, _ in server.seen)
    shutil.rmtree(work["out"], ignore_errors=True)
    with pytest.raises(build_release.ReleaseError, match="--api-mode"):
        _fetch_web(work, server, api_mode="sometimes")


def test_fetch_falls_back_to_the_api_when_the_download_address_fails_but_not_on_404(
    work, fake_github
):
    server = fake_github(token=None)
    # nothing listens on the web address: not a 404, so the API is asked
    document = build_release.build(
        work["pins"],
        work["out"],
        sources=_fetch_work(work),
        fetch=True,
        api=server.url,
        web="http://127.0.0.1:9",
        token=None,
    )
    assert document["appliance"]["version"] == "1.3.0"
    assert any(p.startswith("/repos/") for p, _ in server.seen)
    # a private repository answers 404 on the web side: said so, not retried through the API
    shutil.rmtree(work["out"], ignore_errors=True)
    private = fake_github(token="right")
    with pytest.raises(build_release.ReleaseError, match="HTTP 404.*token"):
        _fetch_web(work, private)
    assert not any(p.startswith("/repos/") for p, _ in private.seen)


def test_fetch_by_download_address_still_refuses_a_wrong_checksum_and_a_missing_one(
    work, fake_github
):
    with pytest.raises(
        build_release.ReleaseError, match=r"does not match the release's own"
    ):
        _fetch_web(work, fake_github(bad_sum=True))
    assert not work["out"].exists()
    with pytest.raises(build_release.ReleaseError, match=r"no usable .*sha256"):
        _fetch_web(work, fake_github(drop_sums=True))
    assert not work["out"].exists()


# ---- a component release that is not published yet is waited for --------------------------------------------------------


class _Clock:
    """Time that only moves when the build sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(build_release.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(build_release.time, "sleep", clock.sleep)
    return clock


def _scripted(monkeypatch, answers):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(build_release, "fetch_release_asset", fake)
    return calls


def _wait(wait=300):
    return build_release.wait_for_release_asset(
        "o/r", "v1.0.0", "x.tar.gz", "http://api", None, wait=wait
    )


def test_a_release_that_appears_while_the_build_waits_is_taken(monkeypatch, clock):
    missing = build_release.ReleaseError("HTTP 404", 404)
    calls = _scripted(monkeypatch, [missing, missing, b"data"])
    assert _wait() == b"data"
    assert len(calls) == 3 and clock.sleeps == [build_release.RELEASE_POLL_SECONDS] * 2


def test_a_release_that_never_appears_fails_after_the_wait_with_the_404(
    monkeypatch, clock
):
    calls = _scripted(monkeypatch, [build_release.ReleaseError("HTTP 404", 404)])
    with pytest.raises(build_release.ReleaseError, match="HTTP 404"):
        _wait(wait=300)
    assert 300 - build_release.RELEASE_POLL_SECONDS <= clock.now <= 300
    assert len(calls) == 300 // build_release.RELEASE_POLL_SECONDS + 1


def test_only_a_404_is_waited_for(monkeypatch, clock):
    calls = _scripted(monkeypatch, [build_release.ReleaseError("HTTP 500", 500)])
    with pytest.raises(build_release.ReleaseError, match="HTTP 500"):
        _wait()
    assert len(calls) == 1 and clock.sleeps == []


def test_the_default_wait_is_five_minutes():
    source = (ROOT / "release" / "build_release.py").read_text()
    assert "RELEASE_WAIT_SECONDS = 300" in source
