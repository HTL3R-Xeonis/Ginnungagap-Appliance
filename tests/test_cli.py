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
