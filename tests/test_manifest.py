import copy
import json
import random
from datetime import UTC, datetime

import pytest

from ginnungagap import manifest as mf
from tests.helpers import fake_release


@pytest.fixture
def release(tmp_path):
    return fake_release.make_release(tmp_path / "r")


@pytest.fixture
def data(release):
    return json.loads((release / "components.json").read_text())


def problems_of(data) -> list[str]:
    with pytest.raises(mf.ManifestError) as caught:
        mf.validate(data)
    return caught.value.problems


def test_a_valid_manifest_loads(release):
    manifest = mf.load(release / "components.json")
    assert manifest.version == "1.3.0" and manifest.tag == "v1.3.0"
    assert manifest.channel == "stable"
    assert list(manifest.components) == list(mf.COMPONENT_NAMES)
    operator = manifest.components["operator"]
    assert operator.services == ("ginnungagap",) and operator.repo_name == "Igdrasyl"
    assert not manifest.data_migration
    assert manifest.to_dict()["schema"] == 1


def test_the_json_schema_file_agrees_on_the_valid_fixture(data):
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path

    schema = json.loads(
        (
            Path(__file__).resolve().parent.parent / "schema" / "components.schema.json"
        ).read_text()
    )
    jsonschema.validate(data, schema)
    broken = copy.deepcopy(data)
    broken["components"]["operator"]["sha256"] = "XYZ"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)


def test_the_schema_file_is_valid_json_with_the_five_components():
    from pathlib import Path

    schema = json.loads(
        (
            Path(__file__).resolve().parent.parent / "schema" / "components.schema.json"
        ).read_text()
    )
    assert set(schema["properties"]["components"]["properties"]) == set(
        mf.COMPONENT_NAMES
    )


def test_every_problem_is_reported_at_once(data):
    data["components"]["operator"]["sha256"] = "nothex"
    data["components"]["builder"]["size"] = 0
    data["appliance"]["released"] = "yesterday"
    problems = problems_of(data)
    assert len(problems) == 3
    assert any("components.operator.sha256" in p for p in problems)
    assert any("components.builder.size" in p for p in problems)
    assert any("appliance.released" in p for p in problems)


@pytest.mark.parametrize(
    "mutate, text",
    [
        (lambda d: d.update(schema=2), "schema 2"),
        (lambda d: d.update(schema="1"), "schema"),
        (lambda d: d.update(schema=0), "schema"),
        (lambda d: d["components"].pop("builder"), "missing builder"),
        (lambda d: d["components"].update(extra={}), "unknown component"),
        (
            lambda d: d["components"]["operator"].update(tag="v9.9.9"),
            "components.operator.tag",
        ),
        (
            lambda d: d["components"]["operator"].update(asset="../evil.tar.gz"),
            "components.operator.asset",
        ),
        (
            lambda d: d["components"]["operator"].update(repo="HTL3R-Xeonis/Other"),
            "components.operator.asset",  # the repository is only a label, but the asset has to be named after it
        ),
        (
            lambda d: d["components"]["operator"].update(repo="Other"),
            "components.operator.repo",
        ),
        (
            lambda d: d["components"]["operator"].update(commit="abc"),
            "components.operator.commit",
        ),
        (
            lambda d: d["components"]["operator"].update(services=["rm -rf"]),
            "unknown service",
        ),
        (
            lambda d: d["components"]["operator"].update(services=[]),
            "components.operator.services",
        ),
        (
            lambda d: d["components"]["operator"].update(data_migration="yes"),
            "data_migration",
        ),
        (
            lambda d: d["components"]["operator"].update(version="1.2"),
            "components.operator.version",
        ),
        (lambda d: d["appliance"].update(version="2.0.0"), "appliance.tag"),
        (lambda d: d["appliance"].update(channel="beta"), "appliance.channel"),
        (lambda d: d["appliance"].update(channel="prerelease"), "must be stable"),
        (lambda d: d["appliance"].update(min_installed="soon"), "min_installed"),
        (lambda d: d.update(images=[]), "images"),
        (
            lambda d: d["components"]["appliance"].update(
                version="1.4.0",
                tag="v1.4.0",
                asset="Ginnungagap-Appliance-1.4.0.tar.gz",
            ),
            "differs from appliance.version",
        ),
    ],
)
def test_each_rule_is_checked(data, mutate, text):
    mutate(data)
    assert any(text in p for p in problems_of(data))


def test_not_an_object_and_bad_json_are_refused():
    assert problems_of([]) == ["the manifest must be a JSON object"]
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        mf.loads("{nope")
    with pytest.raises(mf.ManifestError, match="can't be read"):
        mf.load("/does/not/exist.json")


def test_unknown_keys_are_ignored_so_a_newer_release_may_add_some(data):
    data["something_new"] = 1
    data["components"]["operator"]["extra"] = True
    assert mf.validate(data).version == "1.3.0"


def test_a_pre_release_must_be_on_the_prerelease_channel(tmp_path):
    rc = fake_release.make_release(tmp_path / "rc", appliance="1.3.0-rc.1")
    manifest = mf.load(rc / "components.json")
    assert manifest.channel == "prerelease" and manifest.tag == "v1.3.0-rc.1"


def test_semver_ordering_incl_release_candidates():
    order = [
        "1.2.9",
        "1.3.0-alpha",
        "1.3.0-alpha.1",
        "1.3.0-rc.1",
        "1.3.0-rc.2",
        "1.3.0-rc.10",
        "1.3.0",
        "1.3.1",
        "1.10.0",
        "2.0.0",
    ]
    shuffled = random.Random(7).sample(order, len(order))
    assert sorted(shuffled, key=mf.semver_key) == order


@pytest.mark.parametrize(
    "bad",
    ["1.3", "v1.3.0", "adopted", "unknown", "1.03.0", "1.3.0+build", "", "1.3.0-"],
)
def test_not_a_version_is_refused(bad):
    assert not mf.is_semver(bad)
    with pytest.raises(ValueError):
        mf.semver_key(bad)


@pytest.mark.parametrize(
    "old, new, kind",
    [
        (None, "1.0.0", "new"),
        ("1.0.0", "1.0.0", "same"),
        ("1.0.0", "1.0.1", "patch"),
        ("1.0.1", "1.1.0", "minor"),
        ("1.9.9", "2.0.0", "major"),
        ("1.3.0-rc.1", "1.3.0", "pre"),
        ("1.3.0", "1.2.0", "downgrade"),
        ("unknown", "1.0.0", "replace"),
        ("adopted", "1.0.0", "replace"),
    ],
)
def test_version_kind(old, new, kind):
    assert mf.version_kind(old, new) == kind


def installed_for(release, **versions):
    manifest = mf.load(release / "components.json")
    state = mf.state_from_manifest(
        manifest,
        {n: manifest.components[n].version for n in mf.COMPONENT_NAMES},
        "2026-11-19T10:00:00Z",
        None,
    )
    for name, version in versions.items():
        state.components[name]["version"] = version
    return state


def test_diff_names_every_component_and_its_services(tmp_path, release):
    newer = fake_release.make_release(
        tmp_path / "n",
        appliance="1.4.0",
        components={"template-services": "1.0.1", "operator": "1.3.0"},
    )
    changes = {
        c.component: c
        for c in mf.diff(installed_for(release), mf.load(newer / "components.json"))
    }
    assert [c.component for c in changes.values()] == list(mf.COMPONENT_NAMES)
    assert changes["appliance"].kind == "minor" and changes["appliance"].services == (
        "setup",
    )
    assert changes["operator"].kind == "minor"
    assert (
        changes["template-services"].kind == "patch"
        and "template-esxi" in changes["template-services"].services
    )
    assert changes["builder"].kind == "same" and not changes["builder"].changed


def test_diff_with_nothing_installed_and_for_a_partial_update(tmp_path, release):
    target = mf.load(release / "components.json")
    assert {c.kind for c in mf.diff(None, target)} == {"new"}
    state = installed_for(release, operator="1.0.0", builder="0.9.0")
    changes = {c.component: c for c in mf.diff(state, target, only={"operator"})}
    assert changes["operator"].kind == "minor"
    assert changes["builder"].kind == "skipped" and not changes["builder"].changed


def test_state_round_trip_and_history(tmp_path, release):
    state = installed_for(release)
    state.previous = "1.2.0"
    state.backup = "/var/lib/ginnungagap/backups/x"
    path = tmp_path / "state" / "installed.json"
    assert mf.load_installed(path) is None
    mf.save_installed(path, state)
    assert mf.load_installed(path) == state
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert not [
        p for p in path.parent.iterdir() if p.name != "installed.json"
    ]  # no temp file left

    moment = datetime(2026, 12, 1, 10, 0, 0, tzinfo=UTC)
    first = mf.add_history(tmp_path / "h", state, moment)
    second = mf.add_history(
        tmp_path / "h", state, moment
    )  # same second: never overwritten
    assert first != second
    entries = mf.list_history(tmp_path / "h")
    assert [e.version for e in entries] == ["1.3.0", "1.3.0"]
    assert entries[0].stamp == "20261201T100000Z"
    assert (
        entries[0].state.entry == first.name and entries[1].state.entry == second.name
    )
    assert mf.list_history(tmp_path / "nothing") == []


def test_a_damaged_state_file_is_reported(tmp_path):
    path = tmp_path / "installed.json"
    path.write_text("{")
    with pytest.raises(mf.ManifestError, match="can't be read"):
        mf.load_installed(path)
    path.write_text(
        json.dumps({"appliance": {"version": "1.0.0"}, "components": {"x": 1}})
    )
    with pytest.raises(mf.ManifestError, match="components must map"):
        mf.load_installed(path)
    path.write_text(
        json.dumps(
            {
                "appliance": {"version": "1.0.0"},
                "components": {},
                "dirs": {"operator": "../x"},
            }
        )
    )
    with pytest.raises(mf.ManifestError, match="dirs"):
        mf.load_installed(path)


def test_an_atomic_write_that_fails_leaves_the_old_file(tmp_path):
    path = tmp_path / "x.json"
    mf.write_json_atomic(path, {"a": 1})
    with pytest.raises(TypeError):
        mf.write_json_atomic(path, {"a": object()})
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


# ---- the names: roles as keys, old keys and names read, a name-independent check ----


def test_a_manifest_of_a_release_built_with_the_old_keys_and_names_is_read_as_the_roles(
    tmp_path,
):
    old = fake_release.make_release(tmp_path / "old", legacy=True)
    raw = json.loads((old / "components.json").read_text())
    assert {"docker-server-files", "topology-generator"} <= set(raw["components"])
    manifest = mf.load(old / "components.json")
    assert list(manifest.components) == list(mf.COMPONENT_NAMES)
    generator = manifest.components["generator"]
    assert generator.repo == "HTL3R-Xeonis/TopologyGenerator"
    assert (
        generator.repo_name == "TopologyGenerator"
    )  # the tarball's top folder of that release
    assert (
        generator.checkout == "Mimyr"
    )  # the folder on the appliance is this program's own name
    assert (
        manifest.components["template-services"].asset
        == "Docker-Server-Files-1.0.0.tar.gz"
    )
    # what is recorded from it uses the roles
    assert set(manifest.to_dict()["components"]) == set(mf.COMPONENT_NAMES)


def test_an_old_key_and_its_role_together_are_refused(data):
    data["components"]["docker-server-files"] = copy.deepcopy(
        data["components"]["template-services"]
    )
    assert any("same component" in p for p in problems_of(data))


def test_a_name_the_program_has_never_heard_of_is_fine_when_the_asset_is_named_after_it(
    data,
):
    entry = data["components"]["builder"]
    entry["repo"] = "SomeOwner/Brand-New-Name"
    entry["asset"] = "Brand-New-Name-1.0.0.tar.gz"
    manifest = mf.validate(data)
    assert manifest.components["builder"].repo_name == "Brand-New-Name"


@pytest.mark.parametrize(
    "repo, asset",
    [
        (
            "o/Brand-New-Name",
            "TopologyBuilder-1.0.0.tar.gz",
        ),  # the asset belongs to another repository
        ("o/Brand-New-Name", "Brand-New-Name-9.9.9.tar.gz"),  # another version
        ("o/Brand-New-Name", "Brand-New-Name-1.0.0.zip"),
        ("Brand-New-Name", "Brand-New-Name-1.0.0.tar.gz"),  # no owner
        ("o/../x", "..-1.0.0.tar.gz"),
        ("o/a/b", "b-1.0.0.tar.gz"),
    ],
)
def test_the_asset_has_to_be_named_after_the_repository_the_manifest_gives(
    data, repo, asset
):
    data["components"]["builder"].update(repo=repo, asset=asset)
    assert any(p.startswith("components.builder.") for p in problems_of(data))


def test_an_installed_state_with_the_old_keys_and_names_reads_as_the_current_ones():
    old = {
        "schema": 1,
        "appliance": {"version": "1.3.0"},
        "components": {
            "appliance": {"repo": "o/Ginnungagap-Appliance", "version": "1.3.0"},
            "operator": {
                "repo": "o/TopologyOperator",
                "version": "1.2.1",
                "asset": "TopologyOperator-1.2.1.tar.gz",
            },
            "docker-server-files": {
                "repo": "o/Docker-Server-Files",
                "version": "1.0.0",
                "asset": "Docker-Server-Files-1.0.0.tar.gz",
            },
            "topology-generator": {"repo": "TopologyGenerator", "version": "unknown"},
        },
        "dirs": {
            "docker-server-files": "1.0.0",
            "topology-generator": "1.0.0",
            "operator": "adopted-1",
        },
    }
    assert mf.has_legacy_names(old)
    state = mf.InstalledState.from_dict(old)
    assert list(state.components) == [
        "appliance",
        "operator",
        "template-services",
        "generator",
    ]
    assert state.dirs == {
        "template-services": "1.0.0",
        "generator": "1.0.0",
        "operator": "adopted-1",
    }
    assert state.components["operator"] == {
        "repo": "o/Igdrasyl",
        "version": "1.2.1",
        "asset": "Igdrasyl-1.2.1.tar.gz",
    }
    assert (
        state.components["generator"]["repo"] == "Mimyr"
    )  # a bare name (adopted) stays bare
    assert not mf.has_legacy_names(state.to_dict())
    assert (
        old["components"]["operator"]["repo"] == "o/TopologyOperator"
    )  # the input is not changed


def test_a_state_that_is_already_current_is_not_legacy(release, data):
    state = mf.state_from_manifest(mf.validate(data), {}, "2026-12-01T10:00:00Z", None)
    assert not mf.has_legacy_names(state.to_dict())


def test_a_state_with_an_unknown_dirs_key_is_still_refused():
    with pytest.raises(mf.ManifestError, match="dirs must map"):
        mf.InstalledState.from_dict(
            {"appliance": {"version": "1"}, "components": {}, "dirs": {"nope": "1"}}
        )


def test_resolve_component_knows_keys_old_keys_code_names_and_repositories():
    for key in mf.COMPONENT_NAMES:
        assert mf.resolve_component(key) == key
        assert mf.resolve_component(mf.REPO_NAMES[key].upper()) == key
    assert mf.resolve_component("  Docker-Server-Files ") == "template-services"
    assert mf.resolve_component("TopologyGenerator") == "generator"
    assert mf.resolve_component("hugyn-and-munyn") == "template-services"
    assert (
        mf.resolve_component("Hugyn and Munyn".replace(" ", "-")) == "template-services"
    )
    assert mf.resolve_component("nothing") is None and mf.resolve_component("") is None


def test_the_display_names_put_the_code_name_next_to_the_role():
    assert mf.display_name("operator") == "operator (Igdrasyl)"
    assert mf.display_name("template-services") == "template-services (Hugyn and Munyn)"
    assert mf.display_name("appliance") == "appliance"
    assert mf.service_display_name("template-esxi") == "template-esxi (Hugyn)"
    assert mf.service_display_name("collector-gns3") == "collector-gns3 (Munyn)"
    assert mf.service_display_name("ova-store") == "ova-store"


def test_the_release_version_is_the_installed_release_else_the_programs_own(
    tmp_path, release
):
    from ginnungagap.version import __version__

    folder = tmp_path / "update"
    assert mf.release_version(folder) == __version__  # nothing recorded yet
    state = installed_for(release)
    state.appliance_version = "1.9.0"
    mf.save_installed(folder / "installed.json", state)
    assert mf.release_version(folder) == "1.9.0"
    state.appliance_version = "adopted"
    state.adopted = True
    mf.save_installed(folder / "installed.json", state)
    assert mf.release_version(folder) == __version__  # adopted checkouts are no release
    (folder / "installed.json").write_text("{not json")
    assert (
        mf.release_version(folder) == __version__
    )  # a damaged record never breaks the line


def test_the_release_version_follows_the_update_folder_of_the_environment(
    tmp_path, release, monkeypatch
):
    state = installed_for(release)
    state.appliance_version = "2.0.0"
    mf.save_installed(tmp_path / "installed.json", state)
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(tmp_path))
    assert mf.release_version() == "2.0.0"
