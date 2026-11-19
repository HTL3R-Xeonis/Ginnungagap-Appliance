"""The host side of the web update button: a request file in, `ginnungagap update` out, a status file for the panel."""

import json
import os

import pytest
import yaml

from ginnungagap import __main__ as cli
from ginnungagap import statelock
from ginnungagap import update as up
from ginnungagap import update_request as ur
from tests.helpers.update_world import World

REQUEST_ID = "3f9c1a2b4d5e6f70"


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    return World(tmp_path, example, monkeypatch)


@pytest.fixture
def requests_dir(tmp_path):
    folder = tmp_path / "update-requests"
    folder.mkdir()
    return folder


def point_at(world, release):
    """The web request has no --offline-dir: the source is the configuration's (a folder, so no network)."""
    config = yaml.safe_load(world.config_file.read_text())
    config["update"] = {
        **config["update"],
        "source": "directory",
        "directory": str(release),
    }
    world.config_file.write_text(yaml.safe_dump(config))


def request(action="check", **fields):
    data = {
        "id": REQUEST_ID,
        "action": action,
        "requested_by": "alice",
        "role": "admin",
        **fields,
    }
    if action == "apply":
        data.setdefault("version", "1.3.0")
        data.setdefault("confirmed", True)
    return data


def put(requests_dir, data):
    (requests_dir / ur.REQUEST).write_text(
        data if isinstance(data, str) else json.dumps(data)
    )


def run(world, requests_dir, **kwargs):
    return ur.process(
        requests_dir,
        world.layout.update_dir,
        world.config_file,
        world.bundle,
        env=world.env,
        layout=world.layout,
        out=world.out.append,
        **kwargs,
    )


def status(world):
    return json.loads((world.layout.update_dir / ur.STATUS).read_text())


class Recorder:
    """A stand-in for run_update that records what it was asked, instead of updating anything."""

    def __init__(self, code=0):
        self.code, self.calls, self.environment = code, [], []

    def __call__(self, options, config_file=None, **kwargs):
        self.calls.append((options, config_file, kwargs))
        self.environment.append(os.environ.get("GINNUNGAGAP_UPDATE_REQUESTER"))
        return self.code


# ---- what a request may say ----


def test_no_request_means_nothing_to_do(world, requests_dir):
    nothing = Recorder()
    assert run(world, requests_dir, run=nothing) == 0
    assert nothing.calls == [] and not (world.layout.update_dir / ur.STATUS).exists()


def test_a_valid_check_and_a_valid_apply_parse():
    assert ur.parse_request(json.dumps(request("check")))["action"] == "check"
    parsed = ur.parse_request(json.dumps(request("apply")))
    assert parsed["version"] == "1.3.0" and parsed["confirmed"] is True


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ({"extra": 1}, "unknown fields: extra"),
        ({"id": "short"}, "no valid id"),
        ({"id": "../../etc/passwd"}, "no valid id"),
        ({"action": "rollback"}, "only check and apply"),
        ({"action": "apply", "version": "latest", "confirmed": True}, "version number"),
        ({"action": "apply", "version": None, "confirmed": True}, "version number"),
        ({"action": "apply", "version": "1.3.0", "confirmed": False}, "confirmation"),
        ({"action": "apply", "version": "1.3.0"}, "confirmation"),
        ({"requested_by": "a b"}, "who asked"),
        ({"requested_by": 7}, "who asked"),
        ({"role": "user"}, "only an administrator"),
        ({"role": None}, "only an administrator"),
        ({"version": "1.3.0"}, "check takes no version"),
        ({"confirmed": True}, "check takes no version"),
    ],
)
def test_anything_but_exactly_what_the_button_writes_is_refused(mutation, reason):
    data = {**request("check"), **mutation}
    with pytest.raises(ur.Refused, match=reason):
        ur.parse_request(json.dumps(data))


@pytest.mark.parametrize("text", ["{", "[]", '"check"', "null", ""])
def test_a_request_that_is_not_a_json_object_is_refused(text):
    with pytest.raises(ur.Refused):
        ur.parse_request(text)


def test_a_request_can_not_carry_the_dangerous_options():
    for key in (
        "force",
        "no_backup",
        "allow_downgrade",
        "offline_dir",
        "component",
        "source",
        "channel",
        "restore_data",
    ):
        with pytest.raises(ur.Refused, match="unknown fields"):
            ur.parse_request(json.dumps({**request("apply"), key: True}))


# ---- the hand-off ----


def test_a_refused_request_is_consumed_logged_and_starts_nothing(world, requests_dir):
    put(requests_dir, {**request("check"), "role": "user"})
    nothing = Recorder()
    assert run(world, requests_dir, run=nothing) == 1
    assert nothing.calls == []
    assert not (requests_dir / ur.REQUEST).exists()  # never retried in a loop
    result = status(world)
    assert result["state"] == "refused" and "administrator" in result["message"]
    assert (
        world.audit()[-1]["action"] == "web-request"
        and world.audit()[-1]["result"] == "refused"
    )


def test_a_symlinked_request_is_never_followed(world, requests_dir, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(request("check")))
    (requests_dir / ur.REQUEST).symlink_to(secret)
    nothing = Recorder()
    assert run(world, requests_dir, run=nothing) == 1
    assert nothing.calls == [] and not (requests_dir / ur.REQUEST).is_symlink()
    assert secret.exists() and status(world)["state"] == "refused"


def test_an_oversized_request_is_ignored(world, requests_dir):
    put(requests_dir, {**request("check"), "pad": "x" * (ur.MAX_REQUEST_BYTES + 10)})
    nothing = Recorder()
    assert run(world, requests_dir, run=nothing) == 1
    assert nothing.calls == [] and status(world)["state"] == "refused"


def test_a_check_runs_the_check_as_the_person_who_asked(world, requests_dir):
    put(requests_dir, request("check"))
    recorder = Recorder(code=up.EXIT_AVAILABLE)
    assert (
        run(world, requests_dir, run=recorder) == 0
    )  # "an update is available" is no failure
    options, config_file, kwargs = recorder.calls[0]
    assert options.action == "check" and options.version is None and not options.yes
    assert config_file == world.config_file
    assert kwargs["env"].requester == "web:alice"
    assert recorder.environment == ["web:alice"]  # the re-executed program inherits it
    assert os.environ.get("GINNUNGAGAP_UPDATE_REQUESTER") is None  # and it is put back
    assert not (requests_dir / ur.REQUEST).exists()
    result = status(world)
    assert result["state"] == "done" and result["outcome"] == "update_available"
    assert result["requested_by"] == "alice" and result["id"] == REQUEST_ID
    assert result["exit_code"] == up.EXIT_AVAILABLE and result["finished"]


def test_an_apply_asks_for_exactly_the_confirmed_version_and_nothing_more(
    world, requests_dir
):
    put(requests_dir, request("apply", version="1.4.0"))
    recorder = Recorder()
    assert run(world, requests_dir, run=recorder) == 0
    options = recorder.calls[0][0]
    assert (options.action, options.version, options.yes) == ("apply", "1.4.0", True)
    assert not (options.force or options.no_backup or options.allow_downgrade)
    assert not (options.no_start or options.dry_run or options.restore_data)
    assert (
        options.offline_dir is None
        and options.components == []
        and options.channel is None
    )
    assert status(world)["outcome"] == "installed"
    assert status(world)["message"] == "Updated to release 1.4.0."


def test_a_crash_of_the_update_is_reported_not_swallowed(world, requests_dir):
    def explode(options, config_file=None, **kwargs):
        raise RuntimeError("disk on fire")

    put(requests_dir, request("apply"))
    assert run(world, requests_dir, run=explode) == 1
    result = status(world)
    assert result["state"] == "failed" and "disk on fire" in result["message"]
    assert result["finished"]


@pytest.mark.parametrize(
    "code, state, outcome",
    [
        (up.EXIT_REFUSED, "refused", "refused"),
        (up.EXIT_ERROR, "failed", "failed"),
        (up.EXIT_ROLLED_BACK, "failed", "rolled_back"),
        (up.EXIT_ROLLBACK_FAILED, "failed", "rollback_failed"),
    ],
)
def test_every_exit_code_of_an_apply_ends_in_a_state_the_panel_knows(
    world, requests_dir, code, state, outcome
):
    put(requests_dir, request("apply"))
    assert run(world, requests_dir, run=Recorder(code=code)) == 1
    assert (status(world)["state"], status(world)["outcome"]) == (state, outcome)


# ---- against the real engine (fake Docker, a release folder as the source) ----


def test_a_real_check_leaves_the_result_the_panel_reads(world, requests_dir):
    point_at(world, world.release("a", appliance="1.3.0"))
    put(requests_dir, request("check"))
    assert run(world, requests_dir) == 0
    available = json.loads(world.layout.available.read_text())
    assert available["update_available"] is True
    assert available["available"]["version"] == "1.3.0"
    result = status(world)
    assert result["outcome"] == "update_available"
    assert result["message"] == "An update is available."
    assert any("operator" in line for line in result["log"])
    assert world.docker.calls == []  # looking never touches Docker


def test_a_real_apply_installs_the_release_and_audits_who_asked(world, requests_dir):
    point_at(world, world.release("a", appliance="1.3.0"))
    world.confirm = False  # no question may be asked: the admin confirmed in the panel
    put(requests_dir, request("apply", version="1.3.0"))
    assert run(world, requests_dir) == 0
    assert world.installed()["appliance"]["version"] == "1.3.0"
    assert status(world)["outcome"] == "installed"
    who = {entry["who"] for entry in world.audit()}
    assert who == {"web:alice"}  # the engine's own lines and the request's lines
    results = [e["result"] for e in world.audit() if e["action"] == "web-request"]
    assert results == ["apply:accepted", "apply:installed"]
    assert {"apply"} <= {e["action"] for e in world.audit()}


def test_an_apply_of_a_release_that_is_not_offered_changes_nothing(world, requests_dir):
    point_at(world, world.release("a", appliance="1.3.0"))
    put(requests_dir, request("apply", version="9.9.9"))
    assert run(world, requests_dir) == 1
    assert status(world)["state"] == "failed"
    assert not world.layout.installed.exists() and world.docker.calls == []


def test_a_failing_update_is_rolled_back_and_the_panel_is_told(world, requests_dir):
    point_at(world, world.release("a", appliance="1.3.0"))
    world.docker.fail = [(" build ", 1)]
    put(requests_dir, request("apply", version="1.3.0"))
    assert run(world, requests_dir) == 1
    result = status(world)
    assert (result["state"], result["outcome"]) == ("failed", "rolled_back")
    assert result["exit_code"] == up.EXIT_ROLLED_BACK
    assert "rolled back" in result["message"]


def test_an_apply_while_another_update_runs_is_refused(world, requests_dir):
    point_at(world, world.release("a", appliance="1.3.0"))
    put(requests_dir, request("apply", version="1.3.0"))
    world.layout.update_dir.mkdir(parents=True, exist_ok=True)
    with statelock.hold(world.layout.lock, timeout=0):
        assert run(world, requests_dir) == 1
    assert status(world)["state"] == "refused"
    assert "another update" in status(world)["message"]
    assert world.docker.calls == []


def test_the_log_in_the_status_is_short(world, requests_dir):
    def chatty(options, config_file=None, env=None, **kwargs):
        for number in range(200):
            env.out(f"line {number} " + "x" * 500)
        return 0

    put(requests_dir, request("check"))
    run(world, requests_dir, run=chatty)
    log = status(world)["log"]
    assert len(log) == ur.MAX_LOG_LINES and all(len(x) <= ur.MAX_LOG_LINE for x in log)
    assert log[-1].startswith("line 199")


# ---- the command ----


def test_the_command_hands_its_folders_to_the_module(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli.update_request_module,
        "process",
        lambda *args, **kwargs: seen.append(args) or 0,
    )
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(tmp_path / "update"))
    code = cli.main(
        [
            "apply-update-request",
            "--requests-dir",
            str(tmp_path / "r"),
            "--config-dir",
            str(tmp_path / "etc"),
        ]
    )
    assert code == 0
    requests, update_dir, config_file, _bundle = seen[0]
    assert str(requests) == str(tmp_path / "r")
    assert str(update_dir) == str(tmp_path / "update")
    assert str(config_file) == str(tmp_path / "etc" / "ginnungagap.yml")
