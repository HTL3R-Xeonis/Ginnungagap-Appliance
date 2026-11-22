"""release/build_images.py: tags and pushes the service images and reads their digests back (a fake Docker, nothing is built)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ginnungagap import manifest as mf

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
