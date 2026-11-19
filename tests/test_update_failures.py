"""The failure matrix of the update plan (8.4), the interrupted updates, rollback, and the safety rules."""

import json
import os
import shutil
import tarfile

import pytest

from ginnungagap import releases as rl
from ginnungagap import statelock
from ginnungagap import update as up
from tests.helpers import fake_release
from tests.helpers.fake_github import FakeGitHub
from tests.helpers.update_world import Crash, World

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
UP_FLAGS = "up -d --no-deps --no-build --pull never"


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


@pytest.fixture
def installed(world):
    """The appliance after a first update to 1.3.0 (so: with a recorded release, an update state and a history)."""
    a = world.release("a", appliance="1.3.0")
    assert world.update("apply", offline=a) == 0
    world.docker.calls.clear()
    world.out.clear()
    world.apply.calls.clear()
    return a


def b_release(world, **kwargs):
    kwargs.setdefault("appliance", "1.3.1")
    kwargs.setdefault("components", {"template-services": "1.0.1"})
    return world.release("b", **kwargs)


def nothing_changed(world, state_before=None):
    """No transaction, no staging, no new release folder, the links where they were."""
    assert not world.layout.transaction.exists()
    assert (
        not list(world.layout.downloads.glob("*"))
        if world.layout.downloads.exists()
        else True
    )
    if state_before is not None:
        assert world.installed() == state_before


def links(world):
    return {p.name: os.readlink(p) for p in sorted(world.layout.components.iterdir())}


# ---- before anything changes: exit 1 and 4 ----


def test_no_network_is_exit_1_with_the_token_free_message_and_nothing_changed(world):
    with FakeGitHub([]) as fake:
        url = fake.url
    world.env.api_base = url
    assert world.update("apply") == up.EXIT_ERROR
    assert "could not reach 127.0.0.1" in world.text()
    assert not world.layout.transaction.exists() and not world.layout.installed.exists()
    assert world.docker.compose() == []
    assert not world.layout.components.exists()  # not even adopted: nothing changed


def test_check_failure_is_recorded_for_the_web_view(world):
    with FakeGitHub([]) as fake:
        url = fake.url
    world.env.api_base = url
    assert world.update("check") == up.EXIT_ERROR
    record = json.loads(world.layout.available.read_text())
    assert record["update_available"] is False and "could not reach" in record["error"]


@pytest.mark.parametrize(
    "mode, words",
    [
        ("rate_limit", "rate limit is used up"),
        ("unauthorized", "refused the stored token"),
        ("server_error", "HTTP 503"),
        ("not_found", "HTTP 404"),
    ],
)
def test_github_trouble_is_exit_1_and_plain(world, mode, words):
    with FakeGitHub([world.release("a")]) as fake:
        fake.mode = mode
        world.env.api_base = fake.url
        assert world.update("apply") == up.EXIT_ERROR
    assert words in world.text()
    assert not world.layout.transaction.exists() and world.docker.compose() == []


def test_a_checksum_mismatch_is_exit_4_and_everything_staged_is_gone(world):
    a = world.release("a")
    tarball = a / "Mjonyr-1.0.0.tar.gz"
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF  # same size, different content
    tarball.write_bytes(bytes(data))
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "does not match the checksum" in world.text()
    assert not world.layout.transaction.exists()
    assert not list(world.layout.downloads.glob("*"))
    assert (
        not list((world.layout.releases).rglob("1.0.0"))
        and not world.layout.installed.exists()
    )
    assert world.docker.compose() == []


def test_a_wrong_size_and_a_sha256sums_mismatch_are_refused(world):
    a = world.release("a")
    (a / "Mjonyr-1.0.0.tar.gz").write_bytes(b"short")
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "bytes" in world.text()
    b = world.release("b")
    sums = (b / "SHA256SUMS").read_text()
    lines = [line for line in sums.splitlines() if "Mjonyr" not in line]
    lines = [
        ("0" * 64 + "  Igdrasyl-1.2.1.tar.gz") if "Igdrasyl" in line else line
        for line in lines
    ]
    (b / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    world.out.clear()
    world.err.clear()
    assert world.update("apply", offline=b) == up.EXIT_REFUSED
    assert "SHA256SUMS" in world.text()
    assert not list(world.layout.downloads.glob("*"))


def test_a_manifest_that_disagrees_with_sha256sums_is_refused(world):
    a = world.release("a")
    sums = (a / "SHA256SUMS").read_text().splitlines()
    sums = [
        f"{'0' * 64}  components.json" if x.endswith("  components.json") else x
        for x in sums
    ]
    (a / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    assert world.update("check", offline=a) == up.EXIT_REFUSED
    assert "components.json does not match" in world.text()


def test_not_enough_disk_is_exit_4(world):
    a = world.release("a")
    world.free = 10 * 1024 * 1024
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "not enough disk space" in world.text()
    assert not list(world.layout.downloads.glob("*")) and world.docker.compose() == []


def test_an_unhealthy_appliance_blocks_the_update_unless_forced(world, installed):
    b = b_release(world)
    world.fetch_code = 503
    assert world.update("apply", offline=b) == up.EXIT_REFUSED
    assert "not healthy" in world.text()
    assert world.docker.compose() == []
    assert world.update("apply", offline=b, force=True) == 0


def test_docker_not_answering_is_exit_1_and_nothing_changed(world):
    a = world.release("a")
    world.docker.docker_up = False
    assert world.update("apply", offline=a) == up.EXIT_ERROR
    assert "Docker does not answer" in world.text()
    assert not world.layout.components.exists()


def test_a_second_update_at_the_same_time_is_exit_4(world):
    a = world.release("a")
    with statelock.hold(world.layout.lock, timeout=0):
        assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "another update or apply is running" in world.text()
    assert world.docker.compose() == []


def test_apply_and_rollback_need_root_but_a_check_does_not(world):
    a = world.release("a")
    world.root_user = False
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "run it as root" in world.text()
    assert world.update("rollback") == up.EXIT_REFUSED
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE


def test_an_old_release_needs_the_flag_and_min_installed_is_honoured(world, installed):
    older = world.release("old", appliance="1.2.0")
    assert world.update("apply", offline=older) == up.EXIT_REFUSED
    assert "--allow-downgrade" in world.text()
    assert world.update("apply", offline=older, allow_downgrade=True) == 0
    assert world.installed()["appliance"]["version"] == "1.2.0"
    far = world.release("far", appliance="2.0.0", min_installed="1.9.0")
    world.err.clear()
    assert world.update("apply", offline=far) == up.EXIT_REFUSED
    assert "only be installed from 1.9.0 or newer" in world.text()


def test_a_prerelease_needs_the_channel_and_no_backup_is_refused_for_a_data_migration(
    world,
):
    rc = world.release("rc", appliance="1.4.0-rc.1")
    assert world.update("apply", offline=rc) == up.EXIT_ERROR
    assert "add --channel prerelease" in world.text()
    migrating = world.release(
        "m", appliance="1.4.0", components={"operator": "1.3.0"}, data_migration=True
    )
    world.err.clear()
    assert world.update("apply", offline=migrating, no_backup=True) == up.EXIT_REFUSED
    assert "backup is required" in world.text()
    assert world.update("apply", offline=rc, channel="prerelease") == 0
    assert world.installed()["appliance"]["version"] == "1.4.0-rc.1"


def test_a_partial_update_touches_only_the_selected_component(world, installed):
    b = b_release(
        world,
        appliance="1.4.0",
        components={"template-services": "1.0.1", "generator": "1.0.1"},
    )
    assert world.update("apply", offline=b, components=["generator"]) == 0
    assert "not the combination that was tested together" in world.text()
    commands = world.docker.compose()
    assert next(c for c in commands if c.startswith("build")) == "build generator"
    state = world.installed()
    assert state["partial"] is True and state["appliance"]["version"] == "1.3.0"
    assert state["components"]["generator"]["version"] == "1.0.1"
    assert state["components"]["template-services"]["version"] == "1.0.0"
    assert world.update("apply", offline=b, components=["nonsense"]) == up.EXIT_ERROR


# ---- a failure while the update is under way: rolled back, exit 2 ----


def test_a_failing_build_flips_the_links_back_and_touches_no_running_service(
    world, installed
):
    before = world.installed()
    link_before = links(world)
    b = b_release(world)
    world.docker.fail = [(" build ", 1)]
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    text = world.text()
    assert (
        "The update failed: building the images failed" in text
        and "Rolled back" in text
    )
    assert links(world) == link_before
    assert not any(
        c.startswith("up") for c in world.docker.compose()
    )  # nothing was restarted
    # the old images are the latest ones again
    assert (
        "docker tag ginnungagap-setup:rollback-1.3.0 ginnungagap-setup:latest"
        in world.docker.commands()
    )
    nothing_changed(world, before)
    assert not (world.layout.releases / "Hugyn-and-Munyn" / "1.0.1").exists()
    audit = [(a["action"], a["result"]) for a in world.audit()]
    assert audit[-2:] == [("apply", "started"), ("apply", "rolled_back")]


def test_a_container_that_does_not_become_healthy_rolls_everything_back(
    world, installed
):
    before = world.installed()
    link_before = links(world)
    b = b_release(world)
    world.docker.health["template-gns3"] = "running unhealthy"
    original = world.docker.inspect

    def heal_after_rollback(args):
        rolled_back = any(
            c.startswith("docker tag")
            and "rollback-1.3.0 " in c
            and c.endswith(":latest")
            for c in world.docker.commands()
        )
        if "ps" in args and args[-1] == "template-gns3" and rolled_back:
            return "running healthy"
        return original(args)

    world.env.inspect = heal_after_rollback
    world.docker.on_run = []
    updater_runs = world.docker
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "template-gns3 is not healthy" in world.text()
    assert links(world) == link_before
    ups = [c for c in updater_runs.compose() if c.startswith("up")]
    assert f"{UP_FLAGS} template-esxi template-gns3" in ups
    assert (
        len([c for c in ups if c.endswith("template-esxi template-gns3")]) == 2
    )  # restarted, then restarted back
    nothing_changed(world, before)
    assert world.audit()[-1]["result"] == "rolled_back"


def test_the_final_health_gate_failing_rolls_back(world, installed):
    before = world.installed()
    b = b_release(world)
    world.apply.codes = [1, 0]  # unhealthy after the update, fine after the rollback
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "not healthy after the update" in world.text()
    assert len(world.apply.calls) == 2
    nothing_changed(world, before)


def test_a_rollback_that_fails_too_is_exit_3_and_the_journal_stays(world, installed):
    b = b_release(world)
    world.docker.health["template-gns3"] = (
        "running unhealthy"  # stays unhealthy: the old one does not come back either
    )
    assert world.update("apply", offline=b) == up.EXIT_ROLLBACK_FAILED
    tx = json.loads(world.layout.transaction.read_text())
    assert tx["phase"] == "rollback_failed"
    text = world.text()
    assert (
        "The rollback failed too" in text
        and "ginnungagap update --resume" in text
        and "rollback-1.3.0" in text
    )
    assert world.audit()[-1]["result"] == "rollback_failed"
    # a further apply is refused until that is dealt with
    world.err.clear()
    assert world.update("apply", offline=b) == up.EXIT_REFUSED
    assert "did not finish" in world.text()


def test_the_tooling_install_failing_rolls_back_and_reinstalls_the_old_tooling(
    world, installed
):
    b = b_release(world)
    world.docker.fail = [("1.3.1/install.sh", 1)]
    assert world.update("apply", offline=b) == up.EXIT_ROLLED_BACK
    assert "install.sh failed" in world.text()
    bashes = [c for c in world.docker.commands() if c.startswith("bash ")]
    assert "/Ginnungagap-Appliance/1.3.1/install.sh" in bashes[0]
    assert (
        "/Ginnungagap-Appliance/1.3.0/install.sh" in bashes[1]
    )  # the previous tooling is put back


def test_no_backup_flag_skips_the_backup_and_a_failing_backup_stops_before_any_change(
    world, installed
):
    b = b_release(world)
    world.backups.clear()
    assert world.update("apply", offline=b, no_backup=True) == 0
    assert world.backups == [] and world.installed()["backup"] is None
    c = world.release("c", appliance="1.3.2", components={"template-services": "1.0.2"})
    world.backup_ok = False
    before = world.installed()
    assert world.update("apply", offline=c) == up.EXIT_ERROR
    assert "backup failed" in world.text()
    nothing_changed(world, before)
    assert not (world.layout.releases / "Hugyn-and-Munyn" / "1.0.2").exists()


# ---- power loss in the middle: `update --resume` ----


@pytest.mark.parametrize(
    "crash_on, selected, phase, tooling",
    [
        ("backup", [], "downloaded", False),
        ("docker tag", ["template-services"], "unpacked", False),
        (" build ", ["template-services"], "switched", False),
        (f"{UP_FLAGS} ginnungagap", ["operator"], "restarting", False),
        ("bash ", [], "tooling", True),
        (" build ", [], "switched", True),
    ],
)
def test_resume_after_a_crash_in_every_phase(
    world, installed, crash_on, selected, phase, tooling
):
    before = world.installed()
    link_before = links(world)
    release = b_release(
        world, components={"template-services": "1.0.1", "operator": "1.3.0"}
    )
    if crash_on == "backup":

        def crashing(*args, **kwargs):
            raise Crash("power loss during the backup")

        world.env.backup = crashing
    else:
        world.docker.crash_on = crash_on
    with pytest.raises(Crash):
        world.update("apply", offline=release, components=selected)
    tx = json.loads(world.layout.transaction.read_text())
    assert tx["phase"] == phase and tx["tooling_done"] is tooling
    world.docker.crash_on = None
    world.env.backup = world.fake_backup
    world.out.clear()
    assert world.update("resume") == 0
    assert not world.layout.transaction.exists()
    assert links(world) == link_before and world.installed() == before
    assert not (world.layout.releases / "Hugyn-and-Munyn" / "1.0.1").exists()
    assert not list(world.layout.downloads.glob("*"))
    if phase in ("downloaded", "unpacked"):
        assert "discarding it" in world.text()
        assert world.audit()[-1]["result"] == "discarded"
        assert not any(
            c.startswith("docker tag")
            and ":latest" in c.split()[-1]
            and "rollback" in c.split()[-2]
            for c in world.docker.commands()
        )
    else:
        assert "Rolled back" in world.text()
        assert [a["result"] for a in world.audit() if a["action"] == "resume"] == [
            "rolling_back"
        ]
        assert world.audit()[-1]["result"] == "rolled_back"
        reinstalled = [
            c
            for c in world.docker.commands()
            if c.startswith("bash ") and "1.3.0/install.sh" in c
        ]
        assert bool(reinstalled) is tooling


def test_resume_with_nothing_pending_does_nothing(world):
    assert world.update("resume") == 0
    assert "No update is waiting" in world.text()


def test_resume_needs_root(world):
    world.root_user = False
    assert world.update("resume") == up.EXIT_REFUSED


# ---- rollback on request ----


def test_rollback_goes_back_one_release_without_the_network(world, installed):
    b = b_release(world)
    assert world.update("apply", offline=b) == 0
    assert world.installed()["appliance"]["version"] == "1.3.1"
    world.docker.calls.clear()
    world.apply.calls.clear()
    assert world.update("rollback") == 0
    state = world.installed()
    assert state["appliance"]["version"] == "1.3.0" and state["previous"] is None
    assert (
        os.readlink(world.layout.components / "Hugyn-and-Munyn")
        == "../releases/Hugyn-and-Munyn/1.0.0"
    )
    commands = world.docker.commands()
    assert (
        "docker tag ginnungagap-template-esxi:rollback-1.3.0 ginnungagap-template-esxi:latest"
        in commands
    )
    assert any(
        c.startswith("bash ") and "/Ginnungagap-Appliance/1.3.0/install.sh" in c
        for c in commands
    )
    assert f"{UP_FLAGS} template-esxi template-gns3" in world.docker.compose()
    assert not any(
        c.startswith("build") for c in world.docker.compose()
    )  # no build: no network
    assert len(world.apply.calls) == 1 and world.restores == []
    assert [a["action"] for a in world.audit()][-2:] == ["rollback", "rollback"]
    assert world.audit()[-1]["result"] == "ok"


def test_rollback_with_nothing_to_go_back_to(world):
    assert world.update("rollback") == up.EXIT_ERROR
    assert "no earlier release recorded" in world.text()


def test_rollback_dry_run_changes_nothing(world, installed):
    assert world.update("apply", offline=b_release(world)) == 0
    world.docker.calls.clear()
    before = links(world)
    assert world.update("rollback", dry_run=True) == 0
    assert (
        "Dry run" in world.text()
        and links(world) == before
        and world.docker.calls == []
    )


def test_rolling_back_a_data_migration_restores_the_backup(world, installed):
    m = world.release(
        "m", appliance="1.4.0", data_migration=True, components={"operator": "1.3.0"}
    )
    world.operator_reports = "1.3.0"
    assert world.update("apply", offline=m) == 0
    assert world.installed()["data_migration"] is True
    assert world.update("rollback") == 0
    assert world.restores and world.restores[0].endswith("ginnungagap-backup-x")


def test_a_failed_update_with_a_data_migration_restores_the_data_too(world, installed):
    m = world.release(
        "m", appliance="1.4.0", data_migration=True, components={"operator": "1.3.0"}
    )
    world.docker.fail = [(" build ", 1)]
    assert world.update("apply", offline=m) == up.EXIT_ROLLED_BACK
    assert len(world.restores) == 1


def test_a_rollback_without_its_image_rebuilds_from_the_previous_source(
    world, installed
):
    assert world.update("apply", offline=b_release(world)) == 0
    world.docker.images.discard("ginnungagap-template-esxi:rollback-1.3.0")
    world.docker.calls.clear()
    assert world.update("rollback") == 0
    assert "No rollback image for template-esxi" in world.text()
    assert "build template-esxi" in world.docker.compose()


# ---- adopt, the token, no-start ----


def test_adopt_records_the_checkouts_and_is_idempotent(world):
    assert world.update("adopt") == 0
    state = world.installed()
    assert state["adopted"] is True and state["appliance"]["version"] == "adopted"
    assert state["components"]["operator"]["version"] == "1.2.1"
    assert state["components"]["builder"]["version"] == "unknown"
    assert (
        os.readlink(world.layout.components / "Igdrasyl")
        == "../releases/Igdrasyl/adopted-1"
    )
    assert (world.layout.components / "Igdrasyl" / "app" / "version.py").is_file()
    assert (
        "docker tag ginnungagap-ginnungagap:latest ginnungagap-ginnungagap:rollback-adopted"
        in world.docker.commands()
    )
    world.out.clear()
    assert world.update("adopt") == 0 and "already recorded" in world.text()
    assert world.audit()[0]["action"] == "adopt"


def test_adopt_without_checkouts_says_so(world, monkeypatch):
    shutil.rmtree(world.repos)
    assert world.update("adopt") == up.EXIT_ERROR
    assert "nothing to adopt" in world.text()


def test_set_token_writes_a_private_file_and_never_echoes_it(world):
    world.env.read_secret = lambda: TOKEN + "\n"
    assert world.update("set-token") == 0
    assert oct(world.token_file.stat().st_mode & 0o777) == "0o600"
    assert rl.read_token(world.token_file) == TOKEN
    assert TOKEN not in world.text() and TOKEN not in "".join(world.journal)
    assert all(
        TOKEN not in line for line in world.layout.audit_log.read_text().splitlines()
    )
    world.env.read_secret = lambda: "has spaces in it"
    world.err.clear()
    assert world.update("set-token") == up.EXIT_ERROR
    assert "has spaces" not in world.text() and "nothing was stored" in world.text()
    world.root_user = False
    assert world.update("set-token") == up.EXIT_REFUSED


def test_versions_shows_the_token_state_without_the_token(world):
    assert world.update("versions") == 0
    assert "token: none (anonymous)" in world.text()
    rl.write_token(world.token_file, TOKEN)
    world.out.clear()
    assert world.update("versions") == 0
    assert "token: set" in world.text() and TOKEN not in world.text()
    world.token_file.chmod(0o644)
    world.out.clear()
    assert world.update("versions") == 0
    assert "unusable" in world.text() and TOKEN not in world.text()


def test_no_start_unpacks_and_builds_but_restarts_nothing(world, installed):
    b = b_release(world)
    world.backups.clear()
    assert world.update("apply", offline=b, no_start=True) == 0
    commands = world.docker.compose()
    assert any(c.startswith("build") for c in commands) and not any(
        c.startswith("up") for c in commands
    )
    assert (
        world.apply.calls == [] and world.backups == []
    )  # no health gate, no backup: nothing runs
    assert world.installed()["appliance"]["version"] == "1.3.1"


def test_a_fresh_machine_with_no_checkouts_installs_everything_with_no_start(world):
    shutil.rmtree(world.repos)
    shutil.rmtree(
        world.bundle / "rendered"
    )  # nothing rendered yet: the images are built the way build-images does
    monkey_free = world.release("a")
    assert world.update("apply", offline=monkey_free, no_start=True) == 0
    state = world.installed()
    assert state["appliance"]["version"] == "1.3.0" and state["previous"] is None
    assert (
        os.readlink(world.layout.components / "Igdrasyl")
        == "../releases/Igdrasyl/1.2.1"
    )
    assert any("--profile * build" in c for c in world.docker.compose())
    assert any(c.startswith("bash ") for c in world.docker.commands())


# ---- through the (fake) GitHub, anonymously and with a token ----


def test_a_whole_update_through_the_fake_github_is_anonymous_by_default(world):
    release = world.release("a")
    with FakeGitHub([release]) as fake:
        world.env.api_base = fake.url
        assert world.update("apply") == 0
        assert fake.requests and all(r.authorization is None for r in fake.requests)
    assert world.installed()["appliance"]["version"] == "1.3.0"
    assert "source: github" in world.text() or "github HTL3R-Xeonis" in world.text()
    # the ETag of the release list was kept for the next check
    assert json.loads(world.layout.etag_cache.read_text())


def test_a_token_is_used_for_the_api_only_and_never_printed(world):
    release = world.release("a")
    rl.write_token(world.token_file, TOKEN)
    with FakeGitHub([release], token=TOKEN) as fake:
        world.env.api_base = fake.url
        assert world.update("apply") == 0
        api = [r for r in fake.requests if not r.path.startswith("/__download/")]
        download = [r for r in fake.requests if r.path.startswith("/__download/")]
        assert api and all(r.authorization == f"Bearer {TOKEN}" for r in api)
        assert download and all(r.authorization is None for r in download)
        fake.mode = "unauthorized"
        world.err.clear()
        assert world.update("check") == up.EXIT_ERROR
    everything = (
        world.text() + "".join(world.journal) + world.layout.audit_log.read_text()
    )
    assert TOKEN not in everything


def test_a_private_repository_without_a_token_says_how_to_store_one(world):
    release = world.release("a")
    with FakeGitHub([release], token=TOKEN) as fake:
        world.env.api_base = fake.url
        assert world.update("check") == up.EXIT_ERROR
    assert "--set-token" in world.text()


# ---- signatures ----

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="no ssh-keygen"
)


def trust(world, release):
    target = world.layout.trust_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fake_release.keys_of(release).read_text())


@needs_ssh_keygen
def test_a_valid_signature_is_verified_with_ssh_keygen(world):
    a = world.release("a", sign=True)
    trust(world, a)
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    assert "signature: valid (key SHA256:" in world.text()
    assert world.update("apply", offline=a) == 0


@needs_ssh_keygen
def test_a_tampered_manifest_fails_the_signature_with_exit_4(world):
    a = world.release("a", sign=True)
    trust(world, a)
    fake_release.rewrite_manifest(a, lambda d: d["appliance"].update(notes="tampered"))
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "signature of components.json is not valid" in world.text()
    assert not world.layout.components.exists() and not list(
        world.layout.downloads.glob("*")
    )


@needs_ssh_keygen
def test_a_missing_signature_is_refused_once_a_key_is_trusted(world):
    a = world.release("a", sign=True)
    trust(world, a)
    (a / "components.json.sig").unlink()
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "has no signature" in world.text()


def test_the_signature_policy_values(world):
    a = world.release("a")
    assert world.update("check", offline=a) == up.EXIT_AVAILABLE
    assert "NOT verified" in world.text()  # auto, no key: checksums only, said loudly
    world.layout.trust_file.parent.mkdir(parents=True)
    world.layout.trust_file.write_text("# only a comment, no key yet\n")
    world.out.clear()
    assert (
        world.update("check", offline=a) == up.EXIT_AVAILABLE
    )  # a placeholder file is no trust
    settings = up.Settings(require_signature=True)
    options = up.Options(action="check", offline_dir=str(a))
    updater = up.Updater(
        world.layout, settings, options, world.env, bundle=world.bundle
    )
    assert updater.dispatch() == up.EXIT_REFUSED
    assert "no key is trusted" in world.text()
    off = up.Updater(
        world.layout,
        up.Settings(require_signature=False),
        options,
        world.env,
        bundle=world.bundle,
    )
    world.out.clear()
    assert (
        off.dispatch() == up.EXIT_AVAILABLE and "signature: not checked" in world.text()
    )


# ---- unpacking hostile archives ----


@pytest.mark.parametrize(
    "kind", ["parent", "absolute", "symlink", "hardlink", "device", "two-tops", "big"]
)
def test_safe_extract_refuses_every_hostile_archive_and_leaves_nothing(tmp_path, kind):
    tarball = tmp_path / "evil.tar.gz"
    fake_release.hostile_tarball(tarball, kind)
    destination = tmp_path / "releases"
    limit = 1024 * 1024 if kind == "big" else 10 * 1024 * 1024
    with pytest.raises(up.UpdateError) as caught:
        up.safe_extract(tarball, destination, "Repo-1.0.0", limit)
    assert caught.value.code == up.EXIT_REFUSED
    assert list(destination.iterdir()) == []  # not even the scratch folder
    assert (
        not (tmp_path / "escaped.txt").exists()
        and not (tmp_path.parent / "escaped.txt").exists()
    )


def test_safe_extract_unpacks_a_good_archive_and_a_damaged_one_is_refused(tmp_path):
    good = tmp_path / "good.tar.gz"
    fake_release.write_tarball(good, "Repo-1.0.0", {"a/b.txt": "hello"})
    target = up.safe_extract(good, tmp_path / "out", "Repo-1.0.0", 1000)
    assert (target / "a" / "b.txt").read_text() == "hello"
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    with pytest.raises(up.UpdateError, match="not a usable tar.gz"):
        up.safe_extract(bad, tmp_path / "out2", "Repo-1.0.0", 1000)
    wrong = tmp_path / "wrong.tar.gz"
    with tarfile.open(wrong, "w:gz"):
        pass
    with pytest.raises(up.UpdateError, match="has no folder"):
        up.safe_extract(wrong, tmp_path / "out3", "Repo-1.0.0", 1000)


def test_a_hostile_tarball_in_a_release_fails_the_update_cleanly(world):
    a = world.release("a")
    fake_release.hostile_tarball(a / "Mjonyr-1.0.0.tar.gz", "parent", "Mjonyr-1.0.0")
    fake_release.rewrite_manifest(
        a,
        lambda d: d["components"]["builder"].update(
            sha256=fake_release.sha256_of(a / "Mjonyr-1.0.0.tar.gz"),
            size=(a / "Mjonyr-1.0.0.tar.gz").stat().st_size,
        ),
    )
    sums = (a / "SHA256SUMS").read_text().splitlines()
    sums = [
        f"{fake_release.sha256_of(a / 'Mjonyr-1.0.0.tar.gz')}  Mjonyr-1.0.0.tar.gz"
        if "Mjonyr" in x
        else x
        for x in sums
    ]
    (a / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    assert world.update("apply", offline=a) == up.EXIT_REFUSED
    assert "unsafe path" in world.text()
    assert not world.layout.transaction.exists() and not list(
        world.layout.releases.rglob("1.0.0")
    )


# ---- helpers of the engine ----


def test_rewrite_install_env_keeps_everything_else(tmp_path):
    path = tmp_path / "install.env"
    path.write_text("A=1\nREPOS_DIR=/old\nB=2\n")
    path.chmod(0o640)
    assert up.rewrite_install_env(path, tmp_path / "components")
    text = path.read_text()
    assert (
        "A=1\n" in text
        and "B=2\n" in text
        and f"REPOS_DIR={tmp_path}/components\n" in text
    )
    assert f"GINNUNGAGAP_GENERATOR_CONFIG={tmp_path}/components/Mimyr/config" in text
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    assert not up.rewrite_install_env(tmp_path / "missing", tmp_path)


def test_parse_sums_accepts_the_sha256sum_formats():
    digest = "a" * 64
    assert up.parse_sums(f"{digest}  one.tar.gz\n{digest} *two.tar.gz\njunk\n") == {
        "one.tar.gz": digest,
        "two.tar.gz": digest,
    }


def test_the_audit_log_names_who_and_what(world, monkeypatch):
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_REQUESTER", "web:alice")
    a = world.release("a")
    assert world.update("apply", offline=a) == 0
    records = world.audit()
    assert [r["result"] for r in records if r["action"] == "apply"] == ["started", "ok"]
    assert all(r["who"] == "web:alice" and r["ts"].endswith("Z") for r in records)
    assert records[-1]["to"] == "1.3.0"
    assert len(world.journal) == len(records)
    assert oct(world.layout.audit_log.stat().st_mode & 0o777) == "0o600"
