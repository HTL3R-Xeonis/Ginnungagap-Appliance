import pytest

from ginnungagap import __main__ as cli


def test_restore_without_yes_changes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        cli.operate, "restore", lambda *a, **k: pytest.fail("must not run")
    )
    assert cli.main(["restore", str(tmp_path)]) == 1
    assert "--yes" in capsys.readouterr().err


def test_restore_with_yes_hands_the_folder_to_the_restore(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli.operate, "restore", lambda folder, bundle: seen.append(folder) or 0
    )
    assert cli.main(["restore", str(tmp_path), "--yes"]) == 0
    assert seen == [str(tmp_path)]


def test_an_invalid_file_is_reported_with_every_problem(tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("servises: {}\nappliance: {http_port: nope}\n")
    assert cli.main(["validate", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "servises" in err and "http_port" in err


def test_show_hides_the_keys(tmp_path, capsys, example):
    import yaml

    example["generator"]["hosted"] = {"gemini": {"api_key": "sk-secret-123"}}
    path = tmp_path / "g.yml"
    path.write_text(yaml.safe_dump(example))
    assert cli.main(["show", str(path)]) == 0
    out = capsys.readouterr().out
    assert "gemini" in out and "sk-secret-123" not in out


def test_status_prints_the_appliance_release_as_its_first_line(
    tmp_path, capsys, monkeypatch, example
):
    import yaml

    from ginnungagap import status as status_module
    from ginnungagap.version import __version__

    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(tmp_path / "update"))
    path = tmp_path / "g.yml"
    path.write_text(yaml.safe_dump(example))
    rows = [status_module.Row("ginnungagap", "ok", "up")]
    monkeypatch.setattr(status_module, "check", lambda *a, **k: rows)
    assert cli.main(["status", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Ginnungagap appliance v{__version__}"
    assert "ginnungagap (Igdrasyl)" in lines[1]


def test_firstboot_command_hands_the_config_folder_to_the_first_boot(
    tmp_path, monkeypatch
):
    seen = []
    monkeypatch.setattr(cli.firstboot, "run", lambda folder: seen.append(folder) or 0)
    assert cli.main(["firstboot", "--config-dir", str(tmp_path)]) == 0
    assert seen == [str(tmp_path)]


@pytest.mark.parametrize("status", [0, 1])
def test_bootstrap_refreshes_the_console_only_after_starting(
    tmp_path, monkeypatch, status, console_refreshes
):
    steps = []

    def start(bundle, *, build):
        steps.append(("bootstrap", build))
        return status

    monkeypatch.setenv("GINNUNGAGAP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli.bootstrap_module, "run", start)

    assert cli.main(["bootstrap", "--no-build"]) == status
    assert steps == [("bootstrap", False)]
    assert console_refreshes == ([tmp_path] if status == 0 else [])


def test_build_images_command_is_wired(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli.operate,
        "build_images",
        lambda bundle, gen, remove_other=False: seen.append((gen, remove_other)) or 0,
    )
    assert cli.main(["build-images", "--generator-config", str(tmp_path)]) == 0
    assert seen == [(str(tmp_path), False)]  # other images stay unless asked for
    seen.clear()
    assert (
        cli.main(
            [
                "build-images",
                "--remove-other-images",
                "--generator-config",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert seen == [(str(tmp_path), True)]


def test_issue_command_rewrites_the_console_text(tmp_path, console_refreshes):
    assert cli.main(["issue", "--config-dir", str(tmp_path)]) == 0
    assert console_refreshes == [tmp_path]


def test_apply_staged_refreshes_the_console_whatever_the_result(
    tmp_path, monkeypatch, console_refreshes
):
    monkeypatch.setattr(cli.setup_watcher, "apply_staged", lambda *a, **k: 1)
    assert cli.main(["apply-staged", "--config-dir", str(tmp_path)]) == 1
    assert console_refreshes == [tmp_path]


def test_the_login_prompt_reload_clears_screens_only_when_asked(monkeypatch):
    import subprocess

    from ginnungagap import console

    cleared = []
    monkeypatch.setattr(console, "clear_idle_terminals", lambda: cleared.append(True))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    console.reload_login_prompt()
    assert cleared == []
    console.reload_login_prompt(clear_screens=True)
    assert cleared == [True]


# ---- `ginnungagap update` and `versions` ----


@pytest.fixture
def seen_update(monkeypatch):
    seen = {}

    def fake(options, config_file=None, **kwargs):
        seen["options"], seen["config"], seen["kwargs"] = options, config_file, kwargs
        return 10

    monkeypatch.setattr(cli.update_module, "run_update", fake)
    return seen


def test_update_defaults_to_a_check(seen_update):
    assert cli.main(["update"]) == 10  # the engine's exit code is the command's
    options = seen_update["options"]
    assert options.action == "check" and not options.yes and not options.dry_run
    assert options.wait == 120.0 and options.components == []


def test_every_update_option_reaches_the_engine(seen_update):
    argv = [
        "update", "--apply", "--offline-dir", "/mnt/r", "--version", "1.3.0", "--channel", "prerelease",
        "--component", "operator", "--component", "builder", "--yes", "--dry-run", "--no-backup", "--force",
        "--allow-downgrade", "--no-start", "--wait", "30", "--restore-data", "--json", "--config", "/x/g.yml",
    ]  # fmt: skip
    cli.main(argv)
    options = seen_update["options"]
    assert (options.action, options.offline_dir, options.version, options.channel) == (
        "apply", "/mnt/r", "1.3.0", "prerelease",
    )  # fmt: skip
    assert options.components == ["operator", "builder"]
    assert options.yes and options.dry_run and options.no_backup and options.force
    assert (
        options.allow_downgrade
        and options.no_start
        and options.json
        and options.restore_data
    )
    assert options.wait == 30.0 and seen_update["config"] == "/x/g.yml"


@pytest.mark.parametrize(
    "flag, action",
    [
        ("--check", "check"), ("--apply", "apply"), ("--rollback", "rollback"), ("--list", "list"),
        ("--versions", "versions"), ("--history", "history"), ("--adopt", "adopt"),
        ("--set-token", "set-token"), ("--resume", "resume"),
    ],
)  # fmt: skip
def test_each_update_action_is_one_flag(seen_update, flag, action):
    cli.main(["update", flag])
    assert seen_update["options"].action == action


def test_only_one_update_action_at_a_time(seen_update, capsys):
    with pytest.raises(SystemExit):
        cli.main(["update", "--apply", "--rollback"])
    assert "not allowed with" in capsys.readouterr().err


def test_the_boot_and_timer_units_call_names_that_exist(seen_update):
    for argv in (
        ["update", "--resume"],
        ["update", "--check"],
        ["update", "--adopt", "--yes"],
    ):
        cli.main(argv)


def test_versions_is_the_same_as_update_versions(seen_update):
    cli.main(["versions", "--json"])
    assert seen_update["options"].action == "versions" and seen_update["options"].json


def test_the_hidden_continue_option_selects_the_continue_action(seen_update):
    cli.main(["update", "--continue-tx", "20261201T100000Z"])
    options = seen_update["options"]
    assert options.action == "continue" and options.continue_id == "20261201T100000Z"


def test_an_unusable_configuration_for_a_plain_check_still_works_with_defaults(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv(
        "GINNUNGAGAP_UPDATE_DIR", str(tmp_path / "state" / "update")
    )  # never the machine's real one
    folder = tmp_path / "release"
    from tests.helpers import fake_release

    fake_release.make_release(folder)
    code = cli.main(
        [
            "update",
            "--check",
            "--offline-dir",
            str(folder),
            "--config",
            str(tmp_path / "missing.yml"),
        ]
    )
    assert code == 10 and "Available: v1.3.0" in capsys.readouterr().out
