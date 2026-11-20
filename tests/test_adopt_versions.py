"""
`ginnungagap update --adopt` records the version of EVERY component, read from the file that the release workflow checks
(Operator app/version.py, Builder pyproject.toml, Mimyr and Hugyn-and-Munyn VERSION, Appliance ginnungagap/version.py), so
`update --versions` / `--check` never show 'unknown' for a checkout that has a version. A file that is missing or odd is
'unknown' with the reason, never a crash.
"""

import shutil

import pytest

from ginnungagap import manifest as mf
from ginnungagap import update as up
from tests.helpers.update_world import World

FOLDERS = {
    "appliance": "Ginnungagap-Appliance",
    "operator": "Igdrasyl",
    "builder": "TopologyBuilder",
    "template-services": "Hugyn-and-Munyn",
    "generator": "Mimyr",
}
LEGACY_FOLDERS = {
    "appliance": "Ginnungagap-Appliance",
    "operator": "TopologyOperator",
    "builder": "Mjonyr",
    "template-services": "Docker-Server-Files",
    "generator": "topology-generator",
}


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fill_versions(repos, folders):
    write(
        repos / folders["appliance"],
        "ginnungagap/version.py",
        '__version__ = "1.3.2"\n',
    )
    write(repos / folders["operator"], "app/version.py", '__version__ = "1.3.0"\n')
    write(
        repos / folders["builder"],
        "pyproject.toml",
        '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "topologybuilder"\nversion = "1.2.4"\n',
    )
    write(repos / folders["template-services"], "VERSION", "1.0.7\n")
    write(repos / folders["generator"], "VERSION", "1.1.0\n")


def recorded(world):
    return {n: c["version"] for n, c in world.installed()["components"].items()}


@pytest.mark.parametrize("layout", ["new", "old"])
def test_adopt_reads_the_version_of_every_component(world, layout):
    shutil.rmtree(world.repos)
    world.make_old_installation(layout=layout)
    fill_versions(world.repos, FOLDERS if layout == "new" else LEGACY_FOLDERS)
    assert world.update("adopt") == 0
    assert recorded(world) == {
        "appliance": "1.3.2",
        "operator": "1.3.0",
        "builder": "1.2.4",
        "template-services": "1.0.7",
        "generator": "1.1.0",
    }
    assert "unknown" not in world.text()
    assert world.update("versions") == 0
    assert "unknown" not in "\n".join(world.out[-12:])


def test_a_missing_or_odd_version_file_is_unknown_with_a_reason_and_adopt_still_works(
    world,
):
    fill_versions(world.repos, LEGACY_FOLDERS)
    (world.repos / "Mjonyr" / "pyproject.toml").write_text("this = [is not toml\n")
    (world.repos / "Docker-Server-Files" / "VERSION").unlink()
    (world.repos / "topology-generator" / "VERSION").write_bytes(b"\xff\xfe\x00\x01")
    (world.repos / "TopologyOperator" / "app" / "version.py").write_text("# nothing\n")
    shutil.rmtree(world.repos / "Ginnungagap-Appliance" / "ginnungagap")
    assert world.update("adopt") == 0
    assert set(recorded(world).values()) == {"unknown"}
    text = world.text()
    assert "pyproject.toml is not valid TOML" in text
    assert "VERSION is missing" in text
    assert "app/version.py declares no version" in text
    assert "ginnungagap/version.py is missing" in text


@pytest.mark.parametrize(
    ("name", "relative", "content", "version"),
    [
        ("builder", "pyproject.toml", '[project]\nversion = "2.0.0rc1"\n', "2.0.0rc1"),
        ("builder", "pyproject.toml", '[project]\nname = "x"\n', None),
        ("builder", "pyproject.toml", "[project]\nversion = 3\n", None),
        ("builder", "pyproject.toml", 'project = "x"\n', None),
        ("builder", "pyproject.toml", "", None),
        ("generator", "VERSION", "  1.1.0 \n\n", "1.1.0"),
        ("generator", "VERSION", "", None),
        ("generator", "VERSION", "1.1.0\nsecond line\n", None),
        ("generator", "VERSION", "v 1 <script>\n", None),
        ("generator", "VERSION", "9" * 200, None),
        ("template-services", "VERSION", "1.0.7-rc.1+build.5", "1.0.7-rc.1+build.5"),
        ("operator", "app/version.py", "__version__ = '1.3.0'  # comment\n", "1.3.0"),
        ("appliance", "ginnungagap/version.py", "__version__: str\n", None),
    ],
)
def test_reading_one_version_file(tmp_path, name, relative, content, version):
    write(tmp_path, relative, content)
    found, why = up.read_component_version(name, tmp_path)
    assert found == version
    assert (why == "") == (version is not None)


def test_reading_never_raises_for_a_folder_instead_of_a_file_or_no_checkout(tmp_path):
    (tmp_path / "VERSION").mkdir()
    assert up.read_component_version("generator", tmp_path)[0] is None
    assert up.read_component_version("generator", tmp_path / "nowhere")[0] is None
    big = tmp_path / "big"
    write(big, "VERSION", "1" * (up._MAX_VERSION_FILE + 10))
    found, why = up.read_component_version("template-services", big)
    assert found is None and "too large" in why


def test_every_component_has_exactly_one_version_source():
    assert set(up.VERSION_SOURCES) == set(mf.COMPONENT_NAMES)
