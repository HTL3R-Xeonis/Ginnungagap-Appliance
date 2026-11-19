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
