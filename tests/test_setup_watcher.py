"""The host side of the wizard's hand-off (ginnungagap/setup_watcher.py): never trusts the container's own validation,
never half-writes the live configuration, and always consumes the marker so a bad submission is not retried forever."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ginnungagap import setup_watcher as watcher


@pytest.fixture
def staging(tmp_path) -> Path:
    folder = tmp_path / "staging"
    folder.mkdir()
    return folder


@pytest.fixture
def config_file(tmp_path) -> Path:
    return tmp_path / "etc-ginnungagap" / "ginnungagap.yml"


def stage(staging: Path, text: str) -> None:
    (staging / watcher.STAGED).write_text(text, encoding="utf-8")
    (staging / watcher.MARKER).write_text("", encoding="utf-8")


VALID = {
    "appliance": {"public_url": "http://ginn.lab"},
    "generator": {"llm_servers": [{"name": "o", "url": "http://10.0.0.1:11434", "model": "m"}]},
}  # fmt: skip


def test_nothing_staged_is_a_no_op(staging, config_file, tmp_path):
    calls = []
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: calls.append(1) or 0
    )
    assert code == 0 and calls == [] and not config_file.exists()


def test_a_valid_staged_configuration_becomes_the_live_one_and_apply_is_called(
    staging, config_file, tmp_path
):
    stage(staging, yaml.safe_dump(VALID))
    calls = []

    def fake_apply(file, bundle, generator_config, *, out, build):
        calls.append((Path(file), bundle, generator_config, build))
        return 0

    code = watcher.apply_staged(
        staging, config_file, tmp_path, "/gen/config", apply=fake_apply
    )
    assert code == 0
    assert yaml.safe_load(config_file.read_text()) == VALID
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert calls == [(config_file, tmp_path, "/gen/config", True)]
    assert not (staging / watcher.MARKER).exists()
    assert not (staging / watcher.STAGED).exists()


def test_an_invalid_staged_configuration_is_rejected_and_the_live_one_is_untouched(
    staging, config_file, tmp_path
):
    config_file.parent.mkdir(parents=True)
    config_file.write_text("# the one that is already running\n", encoding="utf-8")
    stage(staging, yaml.safe_dump({"servises": {}}))
    lines = []
    calls = []
    code = watcher.apply_staged(
        staging,
        config_file,
        tmp_path,
        None,
        apply=lambda *a, **k: calls.append(1) or 0,
        out=lines.append,
    )
    assert code == 1 and calls == []
    assert config_file.read_text() == "# the one that is already running\n"
    assert not (staging / watcher.MARKER).exists()
    assert not (staging / watcher.STAGED).exists()
    assert (staging / watcher.REJECTED).is_file()
    assert "servises" in (staging / watcher.REJECTED).read_text()
    assert any("servises" in line for line in lines)


def test_invalid_yaml_is_rejected_the_same_way(staging, config_file, tmp_path):
    stage(staging, "not: valid: yaml: [")
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: 0
    )
    assert code == 1
    assert not config_file.exists()


def test_a_marker_without_a_staged_file_is_removed_and_reported(
    staging, config_file, tmp_path
):
    (staging / watcher.MARKER).write_text("", encoding="utf-8")
    lines = []
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: 0, out=lines.append
    )
    assert code == 1
    assert not (staging / watcher.MARKER).exists()
    assert any("is missing" in line for line in lines)


def test_apply_failing_still_leaves_the_configuration_written(
    staging, config_file, tmp_path
):
    """apply() itself only starts containers; the configuration it was given to apply is already the live one by the
    time it runs, same as `ginnungagap apply` from the command line - a failed `docker compose up` doesn't undo that."""
    stage(staging, yaml.safe_dump(VALID))
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: 1
    )
    assert code == 1
    assert config_file.is_file()


def test_a_symlinked_staged_file_is_never_followed(staging, config_file, tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("root-only: do not leak", encoding="utf-8")
    (staging / watcher.STAGED).symlink_to(secret)
    (staging / watcher.MARKER).write_text("", encoding="utf-8")
    calls = []
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: calls.append(a) or 0
    )
    assert code == 1 and calls == []
    assert not config_file.exists()
    assert not (
        staging / watcher.REJECTED
    ).exists()  # the target's content was never copied out
    assert not (staging / watcher.MARKER).exists()
    assert secret.read_text(encoding="utf-8") == "root-only: do not leak"


def test_a_planted_symlink_at_the_rejected_name_is_replaced_not_followed(
    staging, config_file, tmp_path
):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (staging / watcher.REJECTED).symlink_to(victim)
    stage(staging, "not: [valid")
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: 0
    )
    assert code == 1
    assert victim.read_text(encoding="utf-8") == "keep me"
    rejected = staging / watcher.REJECTED
    assert not rejected.is_symlink() and "not: [valid" in rejected.read_text(
        encoding="utf-8"
    )


def test_an_oversized_staged_file_is_refused(staging, config_file, tmp_path):
    stage(staging, "x" * (watcher.MAX_STAGED_BYTES + 1))
    calls = []
    code = watcher.apply_staged(
        staging, config_file, tmp_path, None, apply=lambda *a, **k: calls.append(a) or 0
    )
    assert code == 1 and calls == [] and not config_file.exists()
