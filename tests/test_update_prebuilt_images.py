"""`ginnungagap update` with a release that carries prebuilt images (stage 2): pulled by digest, built only as the fallback."""

import pytest
import yaml

from ginnungagap import manifest as mf
from ginnungagap import operate
from ginnungagap import update as up
from tests.helpers import fake_release
from tests.helpers.update_world import World

DIGEST = "0123456789abcdef" * 4


def ref(service: str, digest: str = DIGEST) -> str:
    return f"ghcr.io/htl3r-xeonis/ginnungagap-{service}@sha256:{digest}"


def with_images(release, services):
    fake_release.rewrite_manifest(
        release,
        lambda data: data["images"].update(
            {s: {"ref": ref(s), "tag": f"ginnungagap-{s}:1.3.1"} for s in services}
        ),
    )
    return release


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


CHANGED = [
    "setup",
    "template-esxi",
    "collector-esxi",
    "template-gns3",
    "collector-gns3",
]


def first_then_update(world, images):
    """The first apply adopts the old checkouts (everything built); then the release with these images is applied."""
    assert world.update("apply", offline=world.release("a", appliance="1.3.0")) == 0
    world.docker.calls.clear()
    b = world.release("b", appliance="1.3.1", components={"template-services": "1.0.1"})
    with_images(b, images)
    return b


def pulls(world):
    return [c for c in world.docker.commands() if c.startswith("docker pull")]


def builds(world):
    found = [c for c in world.docker.compose() if c.startswith("build")]
    return found[0].split()[1:] if found else []


# ---- the manifest ----


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"ref": "docker.io/library/caddy@sha256:" + DIGEST}, "must be ghcr.io"),
        ({"ref": "ghcr.io/htl3r-xeonis/x:latest"}, "must be ghcr.io"),
        ({"ref": "--help"}, "must be ghcr.io"),
        ({"ref": "ghcr.io/x/y@sha256:abc"}, "must be ghcr.io"),
        ({"tag": "x"}, "ref: missing or not text"),
        ("ghcr.io/x", "ref: missing or not text"),
    ],
)
def test_an_image_reference_must_be_this_registry_by_digest(tmp_path, entry, message):
    release = fake_release.make_release(tmp_path / "r")
    fake_release.rewrite_manifest(
        release, lambda data: data["images"].update({"generator": entry})
    )
    with pytest.raises(mf.ManifestError, match=message):
        mf.load(release / "components.json")


def test_only_the_services_with_a_built_image_may_have_one(tmp_path):
    release = fake_release.make_release(tmp_path / "r")
    fake_release.rewrite_manifest(
        release,
        lambda data: data["images"].update({"gateway": {"ref": ref("gateway")}}),
    )
    with pytest.raises(mf.ManifestError, match="images.gateway: not a service"):
        mf.load(release / "components.json")


def test_a_good_image_list_is_read_and_an_empty_one_is_fine(tmp_path):
    release = with_images(fake_release.make_release(tmp_path / "r"), mf.IMAGE_SERVICES)
    assert set(mf.load(release / "components.json").images) == set(mf.IMAGE_SERVICES)
    plain = fake_release.make_release(tmp_path / "p")
    assert mf.load(plain / "components.json").images == {}


def test_the_image_services_are_the_services_of_compose_that_are_built():
    compose = yaml.safe_load(
        (operate.bundle_dir() / "compose.yml").read_text(encoding="utf-8")
    )
    built = {name for name, svc in compose["services"].items() if "build" in svc}
    assert built == set(mf.IMAGE_SERVICES)


# ---- pulling instead of building ----


def test_the_prebuilt_images_are_pulled_by_digest_and_nothing_is_built(world):
    first_then_update(world, CHANGED)
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    assert sorted(pulls(world)) == sorted(f"docker pull {ref(s)}" for s in CHANGED)
    tags = world.docker.commands()
    for service in CHANGED:
        assert f"docker tag {ref(service)} ginnungagap-{service}:latest" in tags
    assert builds(world) == []
    assert (
        "pulling the image of setup (ghcr.io/htl3r-xeonis/ginnungagap-setup)"
        in world.text()
    )
    # the rest of the update is the same: restarts and the health gate
    assert any(c.startswith("up ") for c in world.docker.compose())
    assert len(world.apply.calls) == 2


def test_the_old_image_is_still_kept_as_the_rollback_target_before_the_pull(world):
    first_then_update(world, CHANGED)
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    commands = world.docker.commands()
    keep = commands.index(
        "docker tag ginnungagap-setup:latest ginnungagap-setup:rollback-1.3.0"
    )
    assert keep < commands.index(f"docker pull {ref('setup')}")


def test_a_service_whose_image_can_not_be_pulled_is_built_here(world):
    first_then_update(world, CHANGED)
    world.docker.fail.append((f"docker pull {ref('collector-esxi')}", 1))
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    assert builds(world) == ["collector-esxi"]
    assert "could not get the image of collector-esxi; it is built here" in world.text()
    assert len(pulls(world)) == len(CHANGED)


def test_a_service_without_an_image_in_the_release_is_built_here(world):
    first_then_update(world, ["setup", "template-esxi"])
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    assert sorted(builds(world)) == sorted(
        ["collector-esxi", "template-gns3", "collector-gns3"]
    )
    assert len(pulls(world)) == 2


def test_build_local_ignores_the_prebuilt_images(world):
    first_then_update(world, CHANGED)
    assert (
        world.update("apply", offline=world.tmp / "releases" / "b", build_local=True)
        == 0
    )
    assert pulls(world) == []
    assert sorted(builds(world)) == sorted(CHANGED)


def test_a_release_without_images_is_built_as_before(world):
    first_then_update(world, [])
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    assert pulls(world) == []
    assert sorted(builds(world)) == sorted(CHANGED)


def test_the_warning_names_the_registry_when_the_images_are_pulled(world):
    first_then_update(world, CHANGED)
    world.env = up.Env(**{**world.env.__dict__, "reachable": lambda host: False})
    assert world.update("apply", offline=world.tmp / "releases" / "b") == 0
    text = world.text()
    assert "ghcr.io can't be reached from this machine; the images are pulled" in text
    assert "pypi.org" not in text
