"""
`ginnungagap update` and the names of before the code names (PLAN_component_codenames.md, section 6): checkouts with the old
folder names are adopted under the new ones, a state written with the old keys and folders is moved once, manifests of old
releases still load, and the code names and old keys are accepted after --component.
"""

import json
import os
import shutil

import pytest

from ginnungagap import manifest as mf
from ginnungagap import update as up
from tests.helpers import fake_release
from tests.helpers.update_world import World

OLD_FOLDERS = {
    "operator": "TopologyOperator",
    "builder": "TopologyBuilder",
    "template-services": "Docker-Server-Files",
    "generator": "topology-generator",
}
NEW_FOLDERS = {k: mf.CHECKOUT_NAMES[k] for k in OLD_FOLDERS}


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


def updater_of(world):
    return up.Updater(
        world.layout,
        up.Settings(),
        up.Options(action="versions"),
        world.env,
        None,
        world.config_file,
        world.bundle,
    )


def fresh_checkouts(world, layout):
    shutil.rmtree(world.repos)
    world.make_old_installation(layout=layout)


# ---- (c) adopt reads both folder layouts ----


@pytest.mark.parametrize("layout", ["old", "new", "mixed"])
def test_adopt_records_the_checkouts_under_the_current_names_whatever_they_are_called(
    world, layout
):
    fresh_checkouts(world, layout)
    assert world.update("adopt") == 0
    for key, folder in NEW_FOLDERS.items():
        assert (
            os.readlink(world.layout.components / folder)
            == f"../releases/{folder}/adopted-1"
        )
        assert (world.layout.releases / folder / "adopted-1").is_dir()
        assert not (world.layout.components / OLD_FOLDERS[key]).exists()
        assert not (world.layout.releases / OLD_FOLDERS[key]).exists()
    state = world.installed()
    assert state["adopted"] is True
    assert list(state["components"]) == list(mf.COMPONENT_NAMES)
    assert state["dirs"]["generator"] == "adopted-1"
    assert state["components"]["generator"]["repo"] == "Mimyr"
    env = (world.config_dir / "install.env").read_text()
    assert f"GINNUNGAGAP_GENERATOR_CONFIG={world.layout.components}/Mimyr/config" in env
    # the generator's config came from wherever the checkout was
    assert (world.layout.components / "Mimyr" / "config").is_dir()
    # nothing in the old folder was touched
    assert (world.repos / "Ginnungagap-Appliance" / ".git").is_dir()


def test_adopt_with_both_names_present_takes_the_current_one(world):
    fresh_checkouts(world, "new")
    (world.repos / "TopologyOperator").mkdir()
    (world.repos / "TopologyOperator" / "marker").write_text("old\n")
    assert world.update("adopt") == 0
    assert not (world.layout.releases / "Igdrasyl" / "adopted-1" / "marker").exists()


def test_adopt_says_which_names_it_looks_for_when_a_checkout_is_missing(world):
    shutil.rmtree(world.repos / "TopologyBuilder")
    assert world.update("adopt") == up.EXIT_ERROR
    text = world.text()
    assert (
        "Igdrasyl" in text and "Hugyn-and-Munyn" in text and "TopologyBuilder" in text
    )


def test_the_first_apply_from_a_mixed_layout_updates_everything(world):
    fresh_checkouts(world, "mixed")
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    installed = world.installed()
    assert installed["previous"] == "adopted"
    assert installed["dirs"]["generator"] == "1.0.0"
    assert os.readlink(world.layout.components / "Mimyr") == "../releases/Mimyr/1.0.0"


# ---- (b) manifests of old releases and of a future rename ----


def test_a_release_built_with_the_old_keys_and_names_still_installs_as_the_roles(world):
    fresh_checkouts(world, "old")
    old = world.release("old", appliance="1.3.0", legacy=True)
    raw = json.loads((old / "components.json").read_text())
    assert (
        "docker-server-files" in raw["components"]
        and "template-services" not in raw["components"]
    )
    assert world.update("apply", offline=old) == 0
    installed = world.installed()
    assert list(installed["components"]) == list(mf.COMPONENT_NAMES)
    # the tarball of the old release unpacked below its own (old) top folder and landed in the current folder
    assert (world.layout.releases / "Hugyn-and-Munyn" / "1.0.0" / "VERSION").is_file()
    # the state keeps the label of the release, and reads as the current name
    assert installed["components"]["template-services"]["repo"].endswith(
        "/Docker-Server-Files"
    )
    loaded = mf.InstalledState.from_dict(installed)
    assert loaded.components["template-services"]["repo"].endswith("/Hugyn-and-Munyn")


def test_going_back_to_a_release_built_with_the_old_names_works(world):
    new_a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=new_a) == 0
    old = world.release("old", appliance="1.2.9", legacy=True)
    assert world.update("apply", offline=old, allow_downgrade=True) == 0
    assert world.installed()["appliance"]["version"] == "1.2.9"
    assert world.update("rollback") == 0
    assert world.installed()["appliance"]["version"] == "1.3.0"


def test_a_future_rename_of_a_component_repository_does_not_lock_the_appliance_out(
    world,
):
    release = world.release(
        "future", appliance="1.3.0", repo_names={"builder": "Mjonyr-Next"}
    )
    manifest = json.loads((release / "components.json").read_text())
    assert manifest["components"]["builder"]["asset"] == "Mjonyr-Next-1.0.0.tar.gz"
    assert world.update("apply", offline=release) == 0
    # the folder on the appliance keeps this program's own name for the component (compose.yml builds from it)
    assert (world.layout.releases / "Mjonyr" / "1.0.0" / "pyproject.toml").is_file()
    assert (
        world.installed()["components"]["builder"]["repo"] == "HTL3R-Xeonis/Mjonyr-Next"
    )


# ---- (d) the update state of an install from before the code names ----


def to_pre_rename_install(world):
    """What a machine looked like whose update state was written before the code names."""
    renamed = {
        "template-services": "docker-server-files",
        "generator": "topology-generator",
    }

    def old_keys(mapping):
        return {renamed.get(k, k): v for k, v in mapping.items()}

    for key, new in NEW_FOLDERS.items():
        old = OLD_FOLDERS[key]
        (world.layout.releases / new).rename(world.layout.releases / old)
        link = world.layout.components / new
        folder = os.path.basename(os.readlink(link))
        link.unlink()
        (world.layout.components / old).symlink_to(f"../releases/{old}/{folder}")
    files = [world.layout.installed, *world.layout.history.glob("*.json")]
    for path in files:
        data = json.loads(path.read_text())
        data["components"] = old_keys(data["components"])
        data["dirs"] = old_keys(data["dirs"])
        for key, entry in data["components"].items():
            for new_key, old_key in renamed.items():
                if key == old_key:
                    old_repo = fake_release.OLD_REPOS[new_key]
                    entry["repo"] = f"HTL3R-Xeonis/{old_repo}"
                    entry["asset"] = f"{old_repo}-{entry['version']}.tar.gz"
        path.write_text(json.dumps(data))
    env = world.config_dir / "install.env"
    env.write_text(env.read_text().replace("/Mimyr/", "/topology-generator/"))


def test_a_state_with_the_old_keys_is_read_as_it_is_without_touching_the_disk(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    to_pre_rename_install(world)
    before = world.layout.installed.read_text()
    world.out.clear()
    assert world.update("versions") == 0
    assert "template-services" in world.text() and "Hugyn and Munyn" in world.text()
    assert world.layout.installed.read_text() == before  # reading renames nothing


def test_the_next_update_moves_an_old_state_to_the_current_names_and_rollback_still_works(
    world,
):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    to_pre_rename_install(world)
    b = world.release("b", appliance="1.3.1", components={"template-services": "1.0.1"})
    assert world.update("apply", offline=b) == 0
    assert "current component names" in world.text()
    releases, components = world.layout.releases, world.layout.components
    for key, new in NEW_FOLDERS.items():
        old = OLD_FOLDERS[key]
        assert (releases / new).is_dir() and not (releases / old).exists()
        assert (components / new).is_symlink()
        # the old name stays as a link to the new one, so the compose file of an older release finds its sources
        assert os.readlink(components / old) == new
        assert (components / old).resolve() == (components / new).resolve()
    state = world.installed()
    assert list(state["components"]) == list(mf.COMPONENT_NAMES)
    assert state["components"]["template-services"]["version"] == "1.0.1"
    assert state["components"]["generator"]["repo"] == "HTL3R-Xeonis/Mimyr"
    env = (world.config_dir / "install.env").read_text()
    assert f"GINNUNGAGAP_GENERATOR_CONFIG={components}/Mimyr/config" in env
    assert any(r["action"] == "rename-components" for r in world.audit())
    # back to the release that was installed before the rename: links flip back, old and new name find the same sources
    assert world.update("rollback") == 0
    assert world.installed()["appliance"]["version"] == "1.3.0"
    assert (
        os.readlink(components / "Hugyn-and-Munyn")
        == "../releases/Hugyn-and-Munyn/1.0.0"
    )
    assert (
        components / "Docker-Server-Files" / "VERSION"
    ).read_text().strip() == "1.0.0"
    assert (components / "topology-generator" / "config").is_dir()


def test_the_move_to_the_current_names_runs_once_and_can_be_repeated_after_a_crash(
    world,
):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    to_pre_rename_install(world)
    # a crash after the folder was renamed but before the links were: the second run finishes the job
    (world.layout.releases / "TopologyBuilder").rename(world.layout.releases / "Mjonyr")
    updater = updater_of(world)
    assert updater.migrate_legacy_layout() is True
    assert (world.layout.components / "Mjonyr").resolve() == (
        world.layout.releases / "Mjonyr" / "1.0.0"
    ).resolve()
    assert updater.migrate_legacy_layout() is False


def test_a_state_in_the_current_names_is_left_alone(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    before = sorted(p.name for p in world.layout.components.iterdir())
    updater = updater_of(world)
    assert updater.migrate_legacy_layout() is False
    assert sorted(p.name for p in world.layout.components.iterdir()) == before


# ---- (e) --component accepts keys, code names and old keys ----


@pytest.mark.parametrize(
    "given, key",
    [
        ("template-services", "template-services"),
        ("docker-server-files", "template-services"),
        ("Hugyn-and-Munyn", "template-services"),
        ("hugyn", "template-services"),
        ("munyn", "template-services"),
        ("Docker-Server-Files", "template-services"),
        ("topology-generator", "generator"),
        ("Mimyr", "generator"),
        ("igdrasyl", "operator"),
        ("TopologyOperator", "operator"),
        ("Mjonyr", "builder"),
        ("builder", "builder"),
        ("Ginnungagap-Appliance", "appliance"),
    ],
)
def test_a_component_can_be_named_by_key_code_name_repository_or_old_key(given, key):
    assert up._check_component_names([given]) == [key]


def test_two_names_of_one_component_count_once_and_an_unknown_name_is_refused():
    assert up._check_component_names(["hugyn", "munyn", "docker-server-files"]) == [
        "template-services"
    ]
    with pytest.raises(
        up.UpdateError, match=r"unknown component nope.*operator \(Igdrasyl\)"
    ):
        up._check_component_names(["nope"])


def test_a_partial_update_with_a_code_name_updates_that_component(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    b = world.release(
        "b",
        appliance="1.3.1",
        components={"generator": "1.0.1", "template-services": "1.0.1"},
    )
    assert world.update("apply", offline=b, components=["mimyr"]) == 0
    state = world.installed()
    assert state["components"]["generator"]["version"] == "1.0.1"
    assert state["components"]["template-services"]["version"] == "1.0.0"


# ---- the code names in the tables people read ----


def test_the_check_table_shows_the_code_name_next_to_the_component(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    text = world.text()
    for shown in (
        "operator (Igdrasyl)",
        "builder (Mjonyr)",
        "template-services (Hugyn and Munyn)",
        "generator (Mimyr)",
    ):
        assert shown in text
    assert "appliance (" not in text  # the product has no code name


def test_the_json_output_keeps_the_plain_keys(world, capsys):
    a = world.release("a", appliance="1.3.0")
    assert world.update("check", offline=a, json=True) == up.EXIT_AVAILABLE
    data = json.loads(capsys.readouterr().out)
    assert {c["component"] for c in data["changes"]} == set(mf.COMPONENT_NAMES)
