"""
The findings of the live acceptance against the real release v1.3.0 (stage 5):
1. the first `--rollback` of a machine made from an image failed (the history record of the adopted state was cleaned out)
2. the same version number with other code was never applied, while installed.json claimed the release's commit
plus the small ones (one audit line for a refused apply, the offline check does not overwrite available.json).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ginnungagap import manifest as mf
from ginnungagap import update as up
from tests.helpers import fake_release
from tests.helpers.update_world import World

DIFFERENT = mf.SAME_VERSION_DIFFERENT_CODE
NOT_RECORDED = mf.SAME_VERSION_CODE_NOT_RECORDED
FLAGS = "up -d --no-deps --no-build --pull never"


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


def manifest_of(release: Path) -> mf.Manifest:
    return mf.validate(json.loads((release / "components.json").read_text()))


def installed_from(release: Path) -> mf.InstalledState:
    manifest = manifest_of(release)
    dirs = {name: c.version for name, c in manifest.components.items()}
    return mf.state_from_manifest(manifest, dirs, "2026-12-01T10:00:00Z", None)


def kinds(changes):
    return {c.component: c.kind for c in changes}


def make_git(folder: Path, commit: str) -> None:
    (folder / ".git").mkdir(parents=True, exist_ok=True)
    (folder / ".git" / "HEAD").write_text(commit + "\n")


def cells(world, name: str) -> list[str]:
    """The cells of the table row of a component: name, installed, available, change, restarts."""
    row = next(r for r in world.text().splitlines() if r.startswith(name))
    return re.split(r"\s{2,}", row.strip())


def build_moves_the_image(world) -> None:
    """A build makes a new image: the tag `latest` of every built service points to another id afterwards."""

    def hook(args):
        if args[:2] == ["docker", "compose"] and "build" in args:
            for service in args[args.index("build") + 1 :]:
                world.docker.ids[f"ginnungagap-{service}:latest"] = (
                    f"sha256:rebuilt-{service}"
                )

    world.docker.on_run.append(hook)


# ---- the plan: what counts as the same code ----


def test_the_same_version_with_the_same_commit_and_checksum_is_the_same(tmp_path):
    a = fake_release.make_release(tmp_path / "a")
    changes = mf.diff(installed_from(a), manifest_of(a))
    assert {c.kind for c in changes} == {"same"}
    assert not any(c.changed or c.reason for c in changes)


def test_the_same_version_with_another_commit_is_a_replace_with_a_reason(tmp_path):
    a = fake_release.make_release(tmp_path / "a")
    b = fake_release.make_release(tmp_path / "b", code={"operator": "fixed"})
    changes = {c.component: c for c in mf.diff(installed_from(a), manifest_of(b))}
    assert changes["operator"].kind == "replace" and changes["operator"].changed
    assert changes["operator"].reason == DIFFERENT
    assert changes["operator"].from_version == changes["operator"].to_version
    assert {n for n, c in changes.items() if c.kind == "same"} == {
        "appliance",
        "builder",
        "template-services",
        "generator",
    }


@pytest.mark.parametrize("field", ["sha256", "commit"])
def test_either_recorded_field_alone_decides(tmp_path, field):
    a = fake_release.make_release(tmp_path / "a")
    state = installed_from(a)
    state.components["builder"][field] = "0" * (64 if field == "sha256" else 40)
    changes = {c.component: c for c in mf.diff(state, manifest_of(a))}
    assert changes["builder"].kind == "replace"
    assert changes["builder"].reason == DIFFERENT
    # the other one missing is fine as long as what is recorded matches
    other = "commit" if field == "sha256" else "sha256"
    state = installed_from(a)
    del state.components["builder"][other]
    assert kinds(mf.diff(state, manifest_of(a)))["builder"] == "same"


def test_a_record_without_a_checksum_and_a_commit_is_replaced_even_at_the_same_version(
    tmp_path,
):
    a = fake_release.make_release(tmp_path / "a")
    state = installed_from(a)
    state.adopted = True
    for entry in state.components.values():
        entry.pop("sha256"), entry.pop("commit")
    changes = mf.diff(state, manifest_of(a))
    assert {c.kind for c in changes} == {"replace"}
    assert {c.reason for c in changes} == {NOT_RECORDED}


def test_an_adopted_checkout_at_the_commit_of_the_release_is_the_same(tmp_path):
    a = fake_release.make_release(tmp_path / "a")
    manifest = manifest_of(a)
    state = installed_from(a)
    state.adopted = True
    for name, entry in state.components.items():
        entry.pop("sha256")
        entry["commit"] = manifest.components[name].commit
    state.components["generator"]["commit"] = "1" * 40
    result = kinds(mf.diff(state, manifest))
    assert result["generator"] == "replace"
    assert {k for n, k in result.items() if n != "generator"} == {"same"}


def test_mixed_versions_and_code(tmp_path):
    a = fake_release.make_release(tmp_path / "a")
    b = fake_release.make_release(
        tmp_path / "b",
        components={"builder": "1.0.1"},
        code={"operator": "fixed", "template-services": "fixed"},
    )
    result = kinds(mf.diff(installed_from(a), manifest_of(b)))
    assert result == {
        "appliance": "same",
        "operator": "replace",
        "builder": "patch",
        "template-services": "replace",
        "generator": "same",
    }
    # a partial update leaves the rest alone whatever it is
    result = kinds(mf.diff(installed_from(a), manifest_of(b), only={"operator"}))
    assert result["operator"] == "replace" and result["template-services"] == "skipped"


def test_a_newer_version_is_never_given_a_reason(tmp_path):
    a = fake_release.make_release(tmp_path / "a")
    b = fake_release.make_release(tmp_path / "b", components={"builder": "1.0.1"})
    builder = next(
        c
        for c in mf.diff(installed_from(a), manifest_of(b))
        if c.component == "builder"
    )
    assert builder.kind == "patch" and builder.reason == ""


# ---- reading the commit of a checkout ----


COMMIT = "4240db5" + "a" * 33


def test_the_commit_of_a_checkout_is_read_without_git(tmp_path):
    loose = tmp_path / "loose"
    (loose / ".git" / "refs" / "heads").mkdir(parents=True)
    (loose / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (loose / ".git" / "refs" / "heads" / "main").write_text(COMMIT + "\n")
    assert up.read_git_commit(loose) == COMMIT

    packed = tmp_path / "packed"
    (packed / ".git").mkdir(parents=True)
    (packed / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (packed / ".git" / "packed-refs").write_text(
        f"# pack-refs\n{'b' * 40} refs/heads/other\n{COMMIT} refs/heads/main\n"
    )
    assert up.read_git_commit(packed) == COMMIT

    detached = tmp_path / "detached"
    make_git(detached, COMMIT)
    assert up.read_git_commit(detached) == COMMIT

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {detached / '.git'}\n")
    assert up.read_git_commit(linked) == COMMIT


def test_no_repository_or_an_odd_one_gives_none(tmp_path):
    assert up.read_git_commit(tmp_path / "missing") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert up.read_git_commit(plain) is None
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    assert up.read_git_commit(broken) is None  # no HEAD
    (broken / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    assert up.read_git_commit(broken) is None  # the branch is nowhere
    (broken / ".git" / "HEAD").write_text("ref: ../../../etc/passwd\n")
    assert up.read_git_commit(broken) is None
    (broken / ".git" / "HEAD").write_text("not a commit\n")
    assert up.read_git_commit(broken) is None


# ---- adopting records what it can ----


def test_adopt_records_the_commit_of_a_checkout_that_has_one(world):
    make_git(world.repos / "TopologyOperator", COMMIT)
    assert world.update("adopt") == 0
    components = world.installed()["components"]
    assert components["operator"]["commit"] == COMMIT
    # the others have no repository (or none that can be read): nothing is claimed
    assert all("commit" not in components[n] for n in components if n != "operator")
    assert all("sha256" not in entry for entry in components.values())


def test_an_adopted_machine_replaces_a_component_of_the_same_version_unless_the_commit_is_the_releases(
    world,
):
    a = world.release("a", appliance="1.3.0")
    release_commit = manifest_of(a).components["operator"].commit
    make_git(world.repos / "TopologyOperator", release_commit)
    assert world.update("adopt") == 0
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    assert (
        cells(world, "operator")[3] == "-"
    )  # at the commit of the release: nothing to do
    assert cells(world, "builder")[3] == "replace"  # its version is unknown


def test_a_checkout_at_another_commit_is_other_code_under_the_same_number(world):
    a = world.release("a", appliance="1.3.0")
    make_git(world.repos / "TopologyOperator", "9" * 40)
    assert world.update("adopt") == 0
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    assert cells(world, "operator")[3] == f"replace ({DIFFERENT})"


def test_the_dry_run_of_a_first_update_shows_the_same_version_replace(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a, dry_run=True) == 0
    row = next(r for r in world.text().splitlines() if "operator (Igdrasyl)" in r)
    assert (
        f"replace ({NOT_RECORDED})" in row
    )  # 1.2.1 on both sides, no commit on the checkout


# ---- applying the same version with other code, and going back ----


@pytest.fixture
def installed(world):
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    world.docker.calls.clear()
    world.out.clear()
    world.apply.calls.clear()
    return a


def test_the_same_version_with_other_code_is_shown_applied_and_rolled_back_by_id(
    world, installed
):
    old_commit = world.installed()["components"]["operator"]["commit"]
    old_image = world.docker.ids["ginnungagap-ginnungagap:latest"]
    build_moves_the_image(world)
    b = world.release("b", appliance="1.3.0", code={"operator": "fixed"})

    assert world.update("check", offline=b) == up.EXIT_AVAILABLE
    row = next(r for r in world.text().splitlines() if "operator (Igdrasyl)" in r)
    assert f"replace ({DIFFERENT})" in row and "ginnungagap" in row
    record = json.loads(world.layout.available_offline.read_text())
    operator = next(c for c in record["changes"] if c["component"] == "operator")
    assert operator["change"] == "replace" and operator["reason"] == DIFFERENT
    assert record["update_available"] is True

    assert world.update("apply", offline=b) == 0
    # only the service of that component is rebuilt and restarted
    commands = world.docker.compose()
    assert [c.split()[1:] for c in commands if c.startswith("build")] == [
        ["ginnungagap"]
    ]
    assert [c for c in commands if c.startswith("up")] == [f"{FLAGS} ginnungagap"]
    assert not any(c.startswith("bash ") for c in world.docker.commands())  # no tooling
    # the new sources have their own folder; the running ones stay for the way back
    state = world.installed()
    new_commit = manifest_of(b).components["operator"].commit
    assert new_commit != old_commit
    assert state["components"]["operator"]["commit"] == new_commit
    assert (
        state["components"]["operator"]["sha256"]
        == manifest_of(b).components["operator"].sha256
    )
    assert (
        state["dirs"]["operator"] == "1.2.1.r2" and state["dirs"]["builder"] == "1.0.0"
    )
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/1.2.1.r2"
    assert (world.layout.releases / "Igdrasyl" / "1.2.1.r2" / "CODE.txt").is_file()
    assert not (world.layout.releases / "Igdrasyl" / "1.2.1" / "CODE.txt").exists()
    assert world.docker.ids["ginnungagap-ginnungagap:latest"] != old_image

    # and now it is the same
    world.out.clear()
    assert world.update("check", offline=b) == 0
    assert "Nothing to update" in world.text()

    world.docker.calls.clear()
    assert world.update("rollback") == 0
    state = world.installed()
    assert state["components"]["operator"]["commit"] == old_commit
    assert state["dirs"]["operator"] == "1.2.1"
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/1.2.1"
    assert world.docker.ids["ginnungagap-ginnungagap:latest"] == old_image
    assert (
        "docker tag ginnungagap-ginnungagap:rollback-1.3.0 ginnungagap-ginnungagap:latest"
        in world.docker.commands()
    )


def test_a_second_update_of_the_same_version_does_not_touch_the_running_folder(
    world, installed
):
    build_moves_the_image(world)
    assert (
        world.update(
            "apply",
            offline=world.release("b", appliance="1.3.0", code={"operator": "one"}),
        )
        == 0
    )
    assert world.installed()["dirs"]["operator"] == "1.2.1.r2"
    running = world.layout.releases / "Igdrasyl" / "1.2.1.r2"
    marker = running / "marker.txt"
    marker.write_text("running\n")
    assert (
        world.update(
            "apply",
            offline=world.release("c", appliance="1.3.0", code={"operator": "two"}),
        )
        == 0
    )
    assert world.installed()["dirs"]["operator"] == "1.2.1"
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/1.2.1"
    assert (
        world.layout.releases / "Igdrasyl" / "1.2.1" / "CODE.txt"
    ).read_text() == "two\n"
    assert marker.exists()  # the one it replaced is the way back


def test_a_failed_update_of_the_same_version_leaves_the_running_sources_alone(
    world, installed
):
    b = world.release(
        "b", appliance="1.3.0", code={"operator": "fixed"}, break_service="ginnungagap"
    )
    world.docker.fail.append((" build ", 1))
    before = world.installed()
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert world.installed() == before
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/1.2.1"
    assert (world.layout.releases / "Igdrasyl" / "1.2.1" / "README.md").is_file()
    assert not (world.layout.releases / "Igdrasyl" / "1.2.1.r2").exists()


def test_mixed_release_rebuilds_exactly_the_services_of_what_differs(world, installed):
    b = world.release(
        "b",
        appliance="1.3.0",
        components={"builder": "1.0.1"},
        code={"generator": "fixed"},
    )
    assert world.update("check", offline=b) == up.EXIT_AVAILABLE
    assert cells(world, "builder")[3] == "patch"
    assert cells(world, "generator")[3] == f"replace ({DIFFERENT})"
    assert cells(world, "operator")[3] == "-"
    world.docker.calls.clear()
    assert world.update("apply", offline=b) == 0
    build = next(c for c in world.docker.compose() if c.startswith("build"))
    assert sorted(build.split()[1:]) == ["generator", "ginnungagap"]


def test_the_same_release_again_changes_nothing(world, installed):
    assert world.update("apply", offline=installed) == 0
    assert "Nothing to update" in world.text()
    assert world.docker.compose() == []


# ---- the first rollback of a machine made from an image ----


def make_image(world) -> dict:
    """What prepare-image.sh leaves: the adopted release recorded, its history folder gone."""
    assert world.update("adopt") == 0
    shutil.rmtree(world.layout.history)
    return world.installed()


def test_the_first_rollback_works_when_the_history_was_cleaned_out_of_the_image(world):
    image = make_image(world)
    assert image["entry"] and not world.layout.history.exists()
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    state = world.installed()
    # the record of the state that was replaced is written when the new one is recorded
    assert state["previous_entry"] == image["entry"]
    assert (world.layout.history / image["entry"]).is_file()
    assert (
        json.loads((world.layout.history / image["entry"]).read_text())["appliance"][
            "version"
        ]
        == "adopted"
    )
    world.out.clear()
    assert world.update("rollback") == 0
    assert "missing or damaged" not in world.text()
    restored = world.installed()
    assert restored["adopted"] is True and restored["appliance"]["version"] == "adopted"
    assert restored["dirs"]["operator"] == "adopted-1"
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/adopted-1"


def test_a_machine_that_already_has_the_broken_record_is_repaired_by_the_rollback(
    world,
):
    """installed.json names `<time>-adopted.json`, the file is gone (what the v1.3.0 image did to its first update)."""
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    state = world.installed()
    record = world.layout.history / state["previous_entry"]
    assert record.name.endswith("-adopted.json")
    record.unlink()

    # looking never writes
    assert world.update("rollback", dry_run=True) == 0
    assert not record.exists()
    assert "Rollback from 1.3.0 to adopted" in world.text()

    world.out.clear()
    assert world.update("rollback") == 0
    assert "rebuilt it from the adopted checkouts" in world.text()
    assert record.is_file()  # written back under its name
    restored = world.installed()
    assert restored["adopted"] is True
    assert restored["components"]["operator"]["version"] == "1.2.1"
    assert world.link_target("Igdrasyl") == "../releases/Igdrasyl/adopted-1"
    assert (
        world.link_target("TopologyBuilder") == "../releases/TopologyBuilder/adopted-1"
    )


def test_a_damaged_record_is_treated_like_a_missing_one(world):
    assert world.update("apply", offline=world.release("a", appliance="1.3.0")) == 0
    record = world.layout.history / world.installed()["previous_entry"]
    record.write_text("{ not json")
    assert world.update("rollback") == 0
    assert json.loads(record.read_text())["appliance"]["version"] == "adopted"


def test_a_previous_state_that_cannot_be_restored_says_so_and_leaves_the_machine_as_it_is(
    world,
):
    assert world.update("apply", offline=world.release("a", appliance="1.3.0")) == 0
    assert (
        world.update(
            "apply",
            offline=world.release(
                "b", appliance="1.3.1", components={"template-services": "1.0.1"}
            ),
        )
        == 0
    )
    state_before = world.installed()
    (world.layout.history / state_before["previous_entry"]).unlink()
    world.docker.calls.clear()
    world.out.clear()
    world.err.clear()
    assert world.update("rollback") == up.EXIT_ERROR
    message = world.text()
    assert "is missing or damaged and can't be rebuilt" in message
    assert "Nothing was changed" in message and "--allow-downgrade" in message
    assert world.installed() == state_before
    assert world.docker.calls == [] and not world.layout.transaction.exists()
    # still recoverable: a later update works, and its rollback goes back to the release before it
    c = world.release("c", appliance="1.3.2", components={"template-services": "1.0.2"})
    assert world.update("apply", offline=c) == 0
    assert world.update("rollback") == 0
    assert world.installed()["appliance"]["version"] == "1.3.1"


# ---- the image script keeps exactly the record installed.json names ----


def _cleanup(tmp_path, update: Path):
    """Runs remove_update_leftovers of the script against the state folder `update` (fake docker, throw-away /etc)."""
    from tests.test_prepare_image import SCRIPT

    script = SCRIPT.read_text()
    start, end = (
        script.index("state_dir() {"),
        script.index("remove_build_leftovers() {"),
    )
    etc = tmp_path / "scriptetc"
    etc.mkdir()
    (etc / "install.env").write_text(f"GINNUNGAGAP_UPDATE_DIR={update}\n")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "docker").write_text("#!/bin/sh\nexit 0\n")
    (fakebin / "docker").chmod(0o755)
    runner = tmp_path / "functions.sh"
    runner.write_text(
        'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
        + script[start:end].replace("/etc/ginnungagap", str(etc))
        + '\n"$@"\n'
    )
    return subprocess.run(
        ["bash", str(runner), "remove_update_leftovers"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
    )


def test_prepare_image_keeps_the_history_record_of_the_installed_state_and_nothing_else(
    tmp_path,
):
    update = tmp_path / "var" / "update"
    (update / "history").mkdir(parents=True)
    entry = "20261119T215909Z-adopted.json"
    (update / "installed.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "appliance": {"version": "adopted"},
                "entry": entry,
                "previous": "1.2.0",
                "previous_entry": "20260101T000000Z-1.2.0.json",
            }
        )
    )
    for name in (entry, "20260101T000000Z-1.2.0.json", "20261120T010101Z-1.3.0.json"):
        (update / "history" / name).write_text("{}")
    result = _cleanup(tmp_path, update)
    assert result.returncode == 0, result.stderr
    assert [p.name for p in (update / "history").iterdir()] == [entry]
    kept = json.loads((update / "installed.json").read_text())
    assert kept["entry"] == entry
    assert kept["previous"] is None and kept["previous_entry"] is None


def test_prepare_image_without_a_recorded_state_removes_the_history_folder(tmp_path):
    update = tmp_path / "var" / "update"
    (update / "history").mkdir(parents=True)
    (update / "history" / "20261119T215909Z-adopted.json").write_text("{}")
    assert _cleanup(tmp_path, update).returncode == 0
    assert not (update / "history").exists()


def test_prepare_image_never_follows_an_entry_name_out_of_the_history(tmp_path):
    update = tmp_path / "var" / "update"
    (update / "history").mkdir(parents=True)
    (update / "installed.json").write_text(
        json.dumps({"schema": 1, "appliance": {"version": "1"}, "entry": "../x.json"})
    )
    (update / "x.json").write_text("{}")
    (update / "history" / "a.json").write_text("{}")
    assert _cleanup(tmp_path, update).returncode == 0
    assert not (update / "history").exists() and (update / "x.json").exists()


def test_the_image_story_end_to_end_with_the_real_script_and_the_real_updater(
    world, tmp_path
):
    """Adopt, run the script's cleanup against that very state folder, then update and roll back."""
    assert world.update("adopt") == 0
    # an older history file of this machine, which must go
    (world.layout.history / "20250101T000000Z-1.0.0.json").write_text("{}")
    result = _cleanup(tmp_path, world.layout.update_dir)
    assert result.returncode == 0, result.stderr
    entry = world.installed()["entry"]
    assert [p.name for p in world.layout.history.iterdir()] == [entry]
    assert world.update("apply", offline=world.release("a", appliance="1.3.0")) == 0
    assert world.update("rollback") == 0
    assert world.installed()["adopted"] is True


# ---- one audit line, and the offline check apart from the online one ----


def test_a_refused_apply_leaves_one_audit_line(world):
    a = world.release("a")
    tarball = a / "TopologyBuilder-1.0.0.tar.gz"
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF
    tarball.write_bytes(bytes(data))
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    applies = [r for r in world.audit() if r["action"] == "apply"]
    assert [r["result"] for r in applies] == ["started", "refused"]
    assert "does not match the checksum" in applies[-1]["detail"]
    assert len(world.journal) == len(world.audit())


def test_a_failure_that_is_no_refusal_is_one_failed_line(world):
    a = world.release("a")
    world.backup_ok = False
    assert world.update("apply", offline=a) == up.EXIT_ERROR
    assert [r["result"] for r in world.audit() if r["action"] == "apply"] == [
        "started",
        "failed",
    ]


def test_an_offline_check_does_not_overwrite_the_result_of_the_online_one(world):
    online = {"checked_at": "2026-12-01T09:00:00Z", "source": "github", "marker": 1}
    world.layout.update_dir.mkdir(parents=True)
    world.layout.available.write_text(json.dumps(online))
    a = world.release("a", appliance="1.3.0")
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    assert json.loads(world.layout.available.read_text()) == online
    offline = json.loads(world.layout.available_offline.read_text())
    assert offline["update_available"] is True and "directory" in offline["source"]
    # a failing offline check is recorded there too
    assert world.update("check", offline=world.tmp / "nothing-here") != 0
    assert json.loads(world.layout.available.read_text()) == online
    assert json.loads(world.layout.available_offline.read_text())["error"]


def test_without_an_offline_folder_the_check_writes_available_json(world):
    """`update.source: directory` in the configuration is a real source: the timer and the web panel read its result."""
    a = world.release("a", appliance="1.3.0")
    options = up.Options(action="check")
    settings = up.Settings(source="directory", directory=str(a))
    from ginnungagap import config as config_module

    updater = up.Updater(
        world.layout,
        settings,
        options,
        world.env,
        config_module.load(world.config_file),
        world.config_file,
        world.bundle,
        None,
    )
    assert updater.check() == up.EXIT_AVAILABLE
    assert world.layout.available.is_file()
    assert not world.layout.available_offline.exists()


# ---- an image built FROM a release carries the release's manifest ----


def write_version_files(root: Path, versions: dict[str, str]) -> None:
    """The version file of the given components in the checkouts (under whichever folder name the fake installation has)."""
    for name, version in versions.items():
        folder = next(
            root / candidate
            for candidate in (mf.CHECKOUT_NAMES[name], *mf.LEGACY_NAMES[name])
            if (root / candidate).is_dir()
        )
        target = folder / up.VERSION_SOURCES[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "builder":
            target.write_text(f'[project]\nversion = "{version}"\n')
        elif name in ("template-services", "generator"):
            target.write_text(version + "\n")
        else:
            target.write_text(f'__version__ = "{version}"\n')


def test_an_image_from_a_release_is_recorded_as_that_release(world):
    release = world.release("a", appliance="1.3.0")
    manifest = manifest_of(release)
    write_version_files(
        world.repos, {n: c.version for n, c in manifest.components.items()}
    )
    shutil.copy(release / "components.json", world.repos / "components.json")

    assert world.update("adopt") == 0

    state = world.installed()
    assert state["appliance"]["version"] == manifest.version
    assert state["appliance"].get("adopted") is not True
    for name, component in manifest.components.items():
        assert state["components"][name]["sha256"] == component.sha256
        assert state["components"][name]["commit"] == component.commit
    # the same release offers nothing to replace
    world.update("check", offline=release)
    assert "replace" not in world.text()


def test_only_the_components_of_the_manifests_version_take_its_checksums(world):
    release = world.release("a", appliance="1.3.0")
    manifest = manifest_of(release)
    write_version_files(
        world.repos,
        {
            "operator": manifest.components["operator"].version,
            "builder": "0.0.1",  # another version than the release's
        },
    )
    shutil.copy(release / "components.json", world.repos / "components.json")

    assert world.update("adopt") == 0

    state = world.installed()
    assert state["appliance"]["version"] == "adopted"
    assert (
        state["components"]["operator"]["sha256"]
        == manifest.components["operator"].sha256
    )
    assert "sha256" not in state["components"]["builder"]


def test_a_broken_manifest_changes_nothing(world):
    (world.repos / "components.json").write_text("{not json")

    assert world.update("adopt") == 0

    assert world.installed()["appliance"]["version"] == "adopted"
    assert "not a valid release manifest" in world.text()
