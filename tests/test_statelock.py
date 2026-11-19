"""The lock an update and an apply share (ginnungagap/statelock.py)."""

from __future__ import annotations

import os

import pytest

from ginnungagap import statelock


def test_the_lock_is_a_private_file_in_the_update_state_folder(tmp_path):
    target = tmp_path / "state" / "lock"
    with statelock.hold(target) as locked:
        assert locked is True
    assert target.is_file() and target.stat().st_mode & 0o777 == 0o600


def test_the_default_place_follows_the_environment_and_is_the_documented_one(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(tmp_path))
    assert statelock.lock_path() == tmp_path / "lock"
    monkeypatch.delenv("GINNUNGAGAP_UPDATE_DIR")
    assert str(statelock.lock_path()) == "/var/lib/ginnungagap/update/lock"


def test_a_second_holder_times_out_and_gets_the_lock_after_the_first_let_go(tmp_path):
    target = tmp_path / "lock"
    with (
        statelock.hold(target),
        pytest.raises(statelock.LockTimeout),
        statelock.hold(target, timeout=0.1, poll=0.02, out=lambda m: None),
    ):
        pytest.fail("must not get the lock")
    with statelock.hold(target, timeout=0.1) as again:
        assert again is True


def test_the_lock_is_released_when_the_block_raises(tmp_path):
    target = tmp_path / "lock"
    with pytest.raises(ValueError), statelock.hold(target):
        raise ValueError
    with statelock.hold(target, timeout=0.1):
        pass


def test_without_a_state_folder_the_block_runs_unlocked(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with statelock.hold(
        blocker / "sub" / "lock"
    ) as locked:  # its parent is a file: can't be made
        assert locked is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_folder_runs_unlocked(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir(mode=0o500)
    with statelock.hold(folder / "lock") as locked:
        assert locked is False
