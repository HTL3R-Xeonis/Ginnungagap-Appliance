"""release/build_images.py: tags and pushes the service images and reads their digests back (a fake Docker, nothing is built)."""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from ginnungagap import manifest as mf
from ginnungagap import operate
from ginnungagap.version import __version__
from tests.helpers.fake_release import make_release

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_images", ROOT / "release" / "build_images.py"
)
build_images = importlib.util.module_from_spec(_spec)
sys.modules["build_images"] = build_images
_spec.loader.exec_module(build_images)

REGISTRY = "ghcr.io/htl3r-xeonis"


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.local = {f"ginnungagap-{s}:latest" for s in mf.IMAGE_SERVICES}
        self.pushed = {}
        self.fail = []

    def run(self, args):
        self.calls.append(args)
        if any(pattern in " ".join(args) for pattern in self.fail):
            return 1
        if args[:2] == ["docker", "push"]:
            repository = args[2].rsplit(":", 1)[0]
            self.pushed[repository] = f"{repository}@sha256:{'cd' * 32}"
        return 0

    def inspect(self, args):
        if args[:3] == ["docker", "image", "inspect"] and "{{.Id}}" in args:
            return "sha256:id" if args[-1] in self.local else None
        if args[:3] == ["docker", "image", "inspect"] and "RepoDigests" in " ".join(
            args
        ):
            repository = args[-1].rsplit(":", 1)[0]
            found = self.pushed.get(repository)
            return json.dumps([found] if found else [])
        return None


def test_every_service_image_is_tagged_pushed_and_its_digest_read_back():
    docker = FakeDocker()
    images = build_images.publish(
        "1.3.6", REGISTRY, True, docker.run, docker.inspect, lambda text: None
    )
    assert list(images) == list(mf.IMAGE_SERVICES)
    for service, entry in images.items():
        assert entry["ref"] == f"{REGISTRY}/ginnungagap-{service}@sha256:{'cd' * 32}"
        assert entry["tag"] == f"{REGISTRY}/ginnungagap-{service}:1.3.6"
        assert mf.IMAGE_REF.match(entry["ref"])  # what the appliance will accept
    commands = [" ".join(c) for c in docker.calls]
    assert (
        f"docker tag ginnungagap-setup:latest {REGISTRY}/ginnungagap-setup:1.3.6"
        in commands
    )
    assert f"docker push {REGISTRY}/ginnungagap-setup:1.3.6" in commands


def test_without_push_nothing_is_pushed_and_the_list_is_empty():
    docker = FakeDocker()
    images = build_images.publish(
        "1.3.6", REGISTRY, False, docker.run, docker.inspect, lambda text: None
    )
    assert images == {}
    assert not [c for c in docker.calls if c[:2] == ["docker", "push"]]


def test_a_missing_image_a_failed_push_or_a_missing_digest_stops_with_the_service_named():
    docker = FakeDocker()
    docker.local.discard("ginnungagap-generator:latest")
    with pytest.raises(
        build_images.ImageError, match="generator: the image .* was not built"
    ):
        build_images.publish(
            "1.3.6", REGISTRY, True, docker.run, docker.inspect, lambda text: None
        )
    docker = FakeDocker()
    docker.fail.append("docker push ghcr.io/htl3r-xeonis/ginnungagap-setup:1.3.6")
    with pytest.raises(build_images.ImageError, match="setup: pushing"):
        build_images.publish(
            "1.3.6", REGISTRY, True, docker.run, docker.inspect, lambda text: None
        )
    docker = FakeDocker()
    docker.run = lambda args: (
        0
    )  # nothing is recorded as pushed: Docker then knows no digest
    with pytest.raises(build_images.ImageError, match="knows no digest"):
        build_images.publish(
            "1.3.6", REGISTRY, True, docker.run, docker.inspect, lambda text: None
        )


def test_the_main_function_reports_a_missing_folder(tmp_path, capsys):
    code = build_images.main(
        [
            "--repos-dir",
            str(tmp_path / "none"),
            "--version",
            "1.0.0",
            "--out",
            str(tmp_path / "i.json"),
        ]
    )
    assert code == 1 and "is not a folder" in capsys.readouterr().err


@pytest.fixture
def prebuilt_bundle(tmp_path):
    folder = tmp_path / "installed"
    folder.mkdir()
    release = make_release(tmp_path / "release", appliance=__version__)
    data = json.loads((release / "components.json").read_text())
    data["images"] = {
        service: {"ref": f"{REGISTRY}/ginnungagap-{service}@sha256:{number:064x}"}
        for number, service in enumerate(mf.IMAGE_SERVICES, start=1)
    }
    (folder / "components.json").write_text(json.dumps(data))
    shutil.copyfile(ROOT / "compose.yml", folder / "compose.yml")
    return folder, data


class PrebuiltDocker:
    """Docker's registry refs and compose aliases are separate names for one ID."""

    def __init__(self, manifest):
        self.refs = {
            item["ref"]: f"sha256:{number + 100:064x}"
            for number, item in enumerate(manifest["images"].values())
        }
        self.local = {}
        self.calls = []
        self.fail = None
        self.bad_digest = False
        self.bad_id = False
        self.wrong_tag = False
        self.change_on_upstream = False

    def run(self, args):
        self.calls.append(list(args))
        if self.fail and self.fail in " ".join(args):
            return 1
        if args[:2] == ["docker", "pull"]:
            self.local[args[2]] = self.refs[args[2]]
        elif args[:2] == ["docker", "tag"]:
            self.local[args[3]] = (
                "sha256:" + "f" * 64 if self.wrong_tag else self.local[args[2]]
            )
        elif args[:3] == ["docker", "image", "rm"]:
            self.local.pop(args[3], None)
        elif args[-2:] == ["pull", "--ignore-buildable"] and self.change_on_upstream:
            self.local["ginnungagap-ginnungagap:latest"] = "sha256:" + "f" * 64
        return 0

    def inspect(self, args):
        if args[:3] == ["docker", "image", "inspect"]:
            if args[4] == "{{.Id}}":
                return "not-an-image-id" if self.bad_id else self.local.get(args[-1])
            if args[4] == "{{json .RepoDigests}}":
                return json.dumps([] if self.bad_digest else [args[-1]])
        if args[-2:] == ["config", "--images"]:
            return "\n".join(
                [f"ginnungagap-{s}:latest" for s in mf.IMAGE_SERVICES] + ["caddy:2"]
            )
        if args[:3] == ["docker", "image", "ls"]:
            return "\n".join(self.local)
        raise AssertionError(args)


def strict_prepare(folder, docker, generator_sources, lines, *, remove_other=True):
    return operate.build_images(
        folder,
        generator_sources,
        run=docker.run,
        inspect=docker.inspect,
        out=lines.append,
        prebuilt_only=True,
        remove_other=remove_other,
    )


def test_strict_prebuilt_pulls_all_seven_signed_refs_proves_tags_and_untags_registry(
    prebuilt_bundle, generator_sources
):
    folder, data = prebuilt_bundle
    docker = PrebuiltDocker(data)
    docker.local["retired:latest"] = "sha256:" + "e" * 64
    lines = []
    assert strict_prepare(folder, docker, generator_sources, lines) == 0
    pulls = [c[2] for c in docker.calls if c[:2] == ["docker", "pull"]]
    assert pulls == [data["images"][s]["ref"] for s in mf.IMAGE_SERVICES]
    assert not any("build" in c for c in docker.calls)
    assert any(c[-2:] == ["pull", "--ignore-buildable"] for c in docker.calls)
    assert not any("@sha256:" in name for name in docker.local)
    assert "retired:latest" not in docker.local
    markers = [x for x in lines if x.startswith("IMGBUILD_PREBUILT_AUDIT=")]
    assert len(markers) == 1
    audit = json.loads(markers[0].split("=", 1)[1])
    assert audit["schema"] == 1 and audit["policy"] == "prebuilt-only"
    assert (
        audit["manifest_sha256"]
        == hashlib.sha256((folder / "components.json").read_bytes()).hexdigest()
    )
    assert audit["appliance_version"] == __version__
    assert audit["registry_aliases_removed"] is True
    assert audit["upstream_pulled"] is True
    assert set(audit["services"]) == set(mf.IMAGE_SERVICES)
    for service, observed in audit["services"].items():
        assert observed == {
            "ref": data["images"][service]["ref"],
            "compose_tag": f"ginnungagap-{service}:latest",
            "image_id": docker.local[f"ginnungagap-{service}:latest"],
        }
    assert not (folder / "rendered").exists()


@pytest.mark.parametrize(
    "problem", ["missing", "empty", "partial", "bad-ref", "version", "compose"]
)
def test_strict_prebuilt_refuses_incomplete_or_inconsistent_installed_metadata(
    prebuilt_bundle, generator_sources, problem
):
    folder, data = prebuilt_bundle
    docker = PrebuiltDocker(data)
    if problem == "missing":
        (folder / "components.json").unlink()
    elif problem == "compose":
        (folder / "compose.yml").write_text("services: {}\n")
    else:
        if problem == "empty":
            data["images"] = {}
        elif problem == "partial":
            data["images"].pop("setup")
        elif problem == "bad-ref":
            data["images"]["setup"]["ref"] = "ghcr.io/example/mutable:latest"
        elif problem == "version":
            data = json.loads(
                (
                    make_release(folder.parent / "other-release", appliance="9.9.9")
                    / "components.json"
                ).read_text()
            )
        (folder / "components.json").write_text(json.dumps(data))
    lines = []
    assert strict_prepare(folder, docker, generator_sources, lines) == 1
    assert not docker.calls
    assert not (folder / "rendered").exists()
    assert not any(x.startswith("IMGBUILD_PREBUILT_AUDIT=") for x in lines)


@pytest.mark.parametrize(
    "failure", ["pull", "tag", "digest", "image-id", "wrong-tag", "upstream", "drift"]
)
def test_strict_prebuilt_never_builds_or_deletes_images_on_pull_or_identity_failure(
    prebuilt_bundle, generator_sources, failure
):
    folder, data = prebuilt_bundle
    docker = PrebuiltDocker(data)
    if failure == "pull":
        docker.fail = "docker pull"
    elif failure == "tag":
        docker.fail = "docker tag"
    elif failure == "digest":
        docker.bad_digest = True
    elif failure == "image-id":
        docker.bad_id = True
    elif failure == "wrong-tag":
        docker.wrong_tag = True
    elif failure == "upstream":
        docker.fail = "pull --ignore-buildable"
    else:
        docker.change_on_upstream = True
    lines = []
    assert strict_prepare(folder, docker, generator_sources, lines) == 1
    assert not any("build" in c or "prune" in c for c in docker.calls)
    assert not any(c[:3] == ["docker", "image", "rm"] for c in docker.calls)
    assert not any(x.startswith("IMGBUILD_PREBUILT_AUDIT=") for x in lines)
    assert not (folder / "rendered").exists()


def test_strict_prebuilt_alias_removal_failure_never_forces_or_emits_success(
    prebuilt_bundle, generator_sources
):
    folder, data = prebuilt_bundle
    docker = PrebuiltDocker(data)
    docker.fail = "docker image rm"
    lines = []
    assert strict_prepare(folder, docker, generator_sources, lines) == 1
    assert not any("--force" in c or "build" in c or "prune" in c for c in docker.calls)
    assert not any(x.startswith("IMGBUILD_PREBUILT_AUDIT=") for x in lines)


def test_strict_prebuilt_requires_the_rendered_compose_to_use_verified_local_aliases(
    prebuilt_bundle, generator_sources
):
    folder, data = prebuilt_bundle
    docker = PrebuiltDocker(data)
    original = docker.inspect
    docker.inspect = lambda args: (
        "unrelated:latest" if "config" in args else original(args)
    )
    lines = []
    assert strict_prepare(folder, docker, generator_sources, lines) == 1
    assert not docker.calls
