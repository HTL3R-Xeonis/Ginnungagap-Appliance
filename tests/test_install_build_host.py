from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_stages_disabled_units_and_preserves_user_configuration(tmp_path):
    target = tmp_path / "root"
    command = [
        "bash",
        str(ROOT / "image/install-build-host.sh"),
        "--root",
        str(target),
        "--no-packages",
        "--no-venv",
    ]
    environment = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    }
    first = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        umask=0o077,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    config = target / "etc/ginnungagap-image-build/build-host.yml"
    config.write_text(
        config.read_text().replace("poll_minutes: 15", "poll_minutes: 23")
    )
    second = subprocess.run(
        command, capture_output=True, text=True, check=False, env=environment
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "poll_minutes: 23" in config.read_text()
    dropin = (
        target / "etc/systemd/system/ginnungagap-image-build-poll.timer.d/interval.conf"
    )
    assert "OnUnitActiveSec=23min" in dropin.read_text()
    assert not list(target.rglob("*.wants")), "installer must not enable units"
    status_unit = target / "etc/systemd/system/ginnungagap-image-build-status.service"
    assert "serve-status" in status_unit.read_text()
    assert "[Install]" in status_unit.read_text()
    assert "ginnungagap-image-build-status.service" in first.stdout
    assert "status_http: true" in first.stdout
    assert "not enabled" in first.stdout
    assert (target / "opt/ginnungagap-image-build/current").is_symlink()
    snapshot = (target / "opt/ginnungagap-image-build/current").resolve()
    assert snapshot.stat().st_mode & 0o777 == 0o755
    assert (snapshot / "appliance/image").stat().st_mode & 0o005 == 0o005
    assert (target / "usr/local/bin/ginnungagap-image-build").stat().st_mode & 0o111
    sudoers = target / "etc/sudoers.d/ginnungagap-image-build"
    assert sudoers.stat().st_mode & 0o777 == 0o440
    assert "/tooling/image/build/inspect-disk.sh *" in sudoers.read_text()
    assert (
        "root root"
        in (target / "etc/tmpfiles.d/ginnungagap-image-build.conf").read_text()
    )


def test_staging_installer_cannot_install_packages_or_enable_services(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "image/install-build-host.sh"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "staging requires" in result.stderr
